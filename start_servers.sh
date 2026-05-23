#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# start_servers.sh — NL-SQL Phase 3B full system startup
#
# Starts (in order):
#   1. Oracle MCP server      :8001
#   2. Neo4j MCP server       :8002
#   3. FastAPI backend        :8000  (waits for MCP servers to be ready)
#
# Usage:
#   chmod +x start_servers.sh
#   ./start_servers.sh
#   ./start_servers.sh --no-streamlit          # headless / CI mode
#   ./start_servers.sh --oracle-port 8011      # custom ports
#
# Stop all: Ctrl+C  or  kill $(cat .pids)
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
ORACLE_MCP_HOST="${ORACLE_MCP_HOST:-0.0.0.0}"
ORACLE_MCP_PORT="${ORACLE_MCP_PORT:-8001}"
NEO4J_MCP_HOST="${NEO4J_MCP_HOST:-0.0.0.0}"
NEO4J_MCP_PORT="${NEO4J_MCP_PORT:-8002}"
BACKEND_HOST="${BACKEND_HOST:-0.0.0.0}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
STREAMLIT_PORT="${STREAMLIT_PORT:-8501}"
START_STREAMLIT=true
MCP_READY_WAIT=5          # seconds to wait for MCP servers to initialize
LOG_DIR="logs"

# ── Argument parsing ──────────────────────────────────────────────────────────
for arg in "$@"; do
    case $arg in
        --no-streamlit)   START_STREAMLIT=false ;;
        --oracle-port=*)  ORACLE_MCP_PORT="${arg#*=}" ;;
        --neo4j-port=*)   NEO4J_MCP_PORT="${arg#*=}" ;;
        --backend-port=*) BACKEND_PORT="${arg#*=}" ;;
    esac
done

# ── Helpers ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'

log()  { echo -e "${CYAN}[nlsql]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $*"; }
err()  { echo -e "${RED}  ✗${NC} $*"; }

check_prereq() {
    command -v "$1" &>/dev/null || { err "Required: $1 not found"; exit 1; }
}

wait_for_port() {
    local host=$1 port=$2 label=$3 attempts=0 max=30
    while ! nc -z "$host" "$port" 2>/dev/null; do
        sleep 1
        attempts=$((attempts + 1))
        if [ $attempts -ge $max ]; then
            warn "$label did not start in ${max}s (fallback mode active)"
            return 1
        fi
    done
    ok "$label ready on :$port"
    return 0
}

# ── Pre-flight ────────────────────────────────────────────────────────────────
echo ""
log "NL-SQL Phase 3B — Starting all servers"
echo "════════════════════════════════════════"

check_prereq python3
check_prereq uvicorn

if [ ! -f ".env" ]; then
    err ".env not found. Copy .env.example and fill credentials."
    exit 1
fi
if [ ! -f "databases.yaml" ]; then
    err "databases.yaml not found. Run from project root."
    exit 1
fi

mkdir -p "$LOG_DIR"
PID_FILE=".pids"
> "$PID_FILE"

# ── Trap: kill all children on Ctrl+C ────────────────────────────────────────
cleanup() {
    echo ""
    log "Shutting down all servers…"
    if [ -f "$PID_FILE" ]; then
        while IFS= read -r pid; do
            kill "$pid" 2>/dev/null && ok "Killed PID $pid" || true
        done < "$PID_FILE"
        rm -f "$PID_FILE"
    fi
    log "All servers stopped."
}
trap cleanup EXIT INT TERM

# ── 1. Oracle MCP Server ──────────────────────────────────────────────────────
log "Starting Oracle MCP server on :${ORACLE_MCP_PORT}…"
ORACLE_MCP_URL="http://localhost:${ORACLE_MCP_PORT}" \
python3 -m mcp_servers.oracle_mcp.server \
    --host "$ORACLE_MCP_HOST" \
    --port "$ORACLE_MCP_PORT" \
    > "${LOG_DIR}/oracle_mcp.log" 2>&1 &
ORACLE_PID=$!
echo "$ORACLE_PID" >> "$PID_FILE"

# ── 2. Neo4j MCP Server ───────────────────────────────────────────────────────
log "Starting Neo4j MCP server on :${NEO4J_MCP_PORT}…"
NEO4J_MCP_URL="http://localhost:${NEO4J_MCP_PORT}" \
python3 -m mcp_servers.neo4j_mcp.server \
    --host "$NEO4J_MCP_HOST" \
    --port "$NEO4J_MCP_PORT" \
    > "${LOG_DIR}/neo4j_mcp.log" 2>&1 &
NEO4J_PID=$!
echo "$NEO4J_PID" >> "$PID_FILE"

# ── Wait for MCP servers ──────────────────────────────────────────────────────
log "Waiting ${MCP_READY_WAIT}s for MCP servers to initialize…"
sleep "$MCP_READY_WAIT"
wait_for_port localhost "$ORACLE_MCP_PORT" "Oracle MCP" || true
wait_for_port localhost "$NEO4J_MCP_PORT"  "Neo4j MCP"  || true

# ── 3. FastAPI Backend ────────────────────────────────────────────────────────
log "Starting FastAPI backend on :${BACKEND_PORT}…"
ORACLE_MCP_URL="http://localhost:${ORACLE_MCP_PORT}" \
NEO4J_MCP_URL="http://localhost:${NEO4J_MCP_PORT}" \
uvicorn backend.main:app \
    --host "$BACKEND_HOST" \
    --port "$BACKEND_PORT" \
    --reload \
    > "${LOG_DIR}/backend.log" 2>&1 &
BACKEND_PID=$!
echo "$BACKEND_PID" >> "$PID_FILE"

wait_for_port localhost "$BACKEND_PORT" "FastAPI backend"

# ── 4. Streamlit Frontend ─────────────────────────────────────────────────────
if $START_STREAMLIT; then
    log "Starting Streamlit frontend on :${STREAMLIT_PORT}…"
    streamlit run frontend/app.py \
        --server.port "$STREAMLIT_PORT" \
        --server.headless true \
        > "${LOG_DIR}/streamlit.log" 2>&1 &
    STREAMLIT_PID=$!
    echo "$STREAMLIT_PID" >> "$PID_FILE"
    wait_for_port localhost "$STREAMLIT_PORT" "Streamlit frontend"
fi

# ── Ready ─────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════"
log "All servers running. Open:"
echo ""
echo "    🖥️  Frontend:    http://localhost:${STREAMLIT_PORT}"
echo "    🔌 API:         http://localhost:${BACKEND_PORT}/docs"
echo "    🗄️  Oracle MCP:  http://localhost:${ORACLE_MCP_PORT}/sse"
echo "    🌐 Neo4j MCP:   http://localhost:${NEO4J_MCP_PORT}/sse"
echo ""
echo "    Logs:  ${LOG_DIR}/"
echo "    PIDs:  ${PID_FILE}"
echo ""
log "Press Ctrl+C to stop all servers."
echo "════════════════════════════════════════"

# Keep script alive — wait for all background processes
wait
