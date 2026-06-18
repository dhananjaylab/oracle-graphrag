#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# start_servers.sh — NL-SQL Phase 3C full system startup
#
# Starts (in order):
#   1. Oracle MCP server    :8001
#   2. Neo4j MCP server     :8002
#   3. FastAPI backend      :8000  (connects MCP clients on startup)
#   4. Streamlit frontend   :8501
#
# Phase 3C adds POST /api/supervisor (SSE) to the backend.
# The Streamlit toggle "Supervisor mode" switches between linear + supervisor.
#
# CHANGED: the two MCP servers now run Streamable HTTP (stateless) instead of
# the legacy SSE transport — see mcp_servers/*/server.py. Their endpoint is
# now mounted at /mcp instead of /sse; only the printed banner below changed,
# the startup/health-check logic (TCP port checks) is unaffected.
#
# Usage:
#   chmod +x start_servers.sh && ./start_servers.sh
#   ./start_servers.sh --no-streamlit
#   ./start_servers.sh --oracle-port=8011 --neo4j-port=8012
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

ORACLE_MCP_HOST="${ORACLE_MCP_HOST:-0.0.0.0}"
ORACLE_MCP_PORT="${ORACLE_MCP_PORT:-8001}"
NEO4J_MCP_HOST="${NEO4J_MCP_HOST:-0.0.0.0}"
NEO4J_MCP_PORT="${NEO4J_MCP_PORT:-8002}"
BACKEND_HOST="${BACKEND_HOST:-0.0.0.0}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
STREAMLIT_PORT="${STREAMLIT_PORT:-8501}"
START_STREAMLIT=true
MCP_READY_WAIT=5
LOG_DIR="logs"

for arg in "$@"; do
    case $arg in
        --no-streamlit)    START_STREAMLIT=false ;;
        --oracle-port=*)   ORACLE_MCP_PORT="${arg#*=}" ;;
        --neo4j-port=*)    NEO4J_MCP_PORT="${arg#*=}" ;;
        --backend-port=*)  BACKEND_PORT="${arg#*=}" ;;
    esac
done

CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${CYAN}[nlsql]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $*"; }

wait_for_port() {
    local host=$1 port=$2 label=$3 n=0
    while ! nc -z "$host" "$port" 2>/dev/null; do
        sleep 1; n=$((n+1))
        [ $n -ge 30 ] && { warn "$label not ready after 30s (fallback active)"; return 1; }
    done
    ok "$label ready on :$port"
}

echo ""
log "NL-SQL Phase 3C — Starting all servers"
echo "════════════════════════════════════════"

command -v python3 &>/dev/null || { echo "python3 not found"; exit 1; }
command -v uvicorn  &>/dev/null || { echo "uvicorn not found — pip install uvicorn"; exit 1; }
[ -f ".env"          ] || { echo "❌ .env missing"; exit 1; }
[ -f "databases.yaml"] || { echo "❌ databases.yaml missing"; exit 1; }

mkdir -p "$LOG_DIR"
PID_FILE=".pids"; > "$PID_FILE"

cleanup() {
    echo ""; log "Shutting down…"
    [ -f "$PID_FILE" ] && while IFS= read -r pid; do
        kill "$pid" 2>/dev/null && ok "Killed PID $pid" || true
    done < "$PID_FILE"
    rm -f "$PID_FILE"; log "Done."
}
trap cleanup EXIT INT TERM

# 1. Oracle MCP
log "Starting Oracle MCP on :${ORACLE_MCP_PORT}… (transport=streamable-http, stateless)"
ORACLE_MCP_URL="http://localhost:${ORACLE_MCP_PORT}" \
python3 -m mcp_servers.oracle_mcp.server \
    --host "$ORACLE_MCP_HOST" --port "$ORACLE_MCP_PORT" \
    > "${LOG_DIR}/oracle_mcp.log" 2>&1 &
echo $! >> "$PID_FILE"

# 2. Neo4j MCP
log "Starting Neo4j MCP on :${NEO4J_MCP_PORT}… (transport=streamable-http, stateless)"
NEO4J_MCP_URL="http://localhost:${NEO4J_MCP_PORT}" \
python3 -m mcp_servers.neo4j_mcp.server \
    --host "$NEO4J_MCP_HOST" --port "$NEO4J_MCP_PORT" \
    > "${LOG_DIR}/neo4j_mcp.log" 2>&1 &
echo $! >> "$PID_FILE"

log "Waiting ${MCP_READY_WAIT}s for MCP servers…"
sleep "$MCP_READY_WAIT"
wait_for_port localhost "$ORACLE_MCP_PORT" "Oracle MCP" || true
wait_for_port localhost "$NEO4J_MCP_PORT"  "Neo4j MCP"  || true

# 3. FastAPI backend
log "Starting FastAPI backend on :${BACKEND_PORT}…"
ORACLE_MCP_URL="http://localhost:${ORACLE_MCP_PORT}" \
NEO4J_MCP_URL="http://localhost:${NEO4J_MCP_PORT}" \
uvicorn backend.main:app \
    --host "$BACKEND_HOST" --port "$BACKEND_PORT" --reload \
    > "${LOG_DIR}/backend.log" 2>&1 &
echo $! >> "$PID_FILE"
wait_for_port localhost "$BACKEND_PORT" "FastAPI backend"

# 4. Streamlit
if $START_STREAMLIT; then
    log "Starting Streamlit on :${STREAMLIT_PORT}…"
    streamlit run frontend/app.py \
        --server.port "$STREAMLIT_PORT" --server.headless true \
        > "${LOG_DIR}/streamlit.log" 2>&1 &
    echo $! >> "$PID_FILE"
    wait_for_port localhost "$STREAMLIT_PORT" "Streamlit"
fi

echo ""
echo "════════════════════════════════════════"
log "All servers running:"
echo ""
echo "    🖥  Frontend:     http://localhost:${STREAMLIT_PORT}"
echo "    🔌  API + docs:   http://localhost:${BACKEND_PORT}/docs"
echo "    🗄  Oracle MCP:   http://localhost:${ORACLE_MCP_PORT}/mcp"
echo "    🌐  Neo4j MCP:    http://localhost:${NEO4J_MCP_PORT}/mcp"
echo ""
echo "    Phase 3C endpoints:"
echo "    POST /api/query       — linear pipeline (fast, single-DB)"
echo "    POST /api/supervisor  — Gemini supervisor (SSE, multi-DB)"
echo ""
echo "    Toggle in Streamlit sidebar: 🤖 Supervisor mode"
echo ""
echo "    Logs: ${LOG_DIR}/   PIDs: ${PID_FILE}"
echo "════════════════════════════════════════"
log "Ctrl+C to stop all servers."
wait
