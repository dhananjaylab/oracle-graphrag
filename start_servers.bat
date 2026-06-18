@echo off
REM ─────────────────────────────────────────────────────────────────────────
REM start_servers.bat — NL-SQL Phase 3B full system startup (Windows)
REM
REM Starts:
REM   1. Oracle MCP server   :8001
REM   2. Neo4j MCP server    :8002
REM   3. FastAPI backend     :8000
REM   4. Streamlit frontend  :8501
REM
REM CHANGED: the two MCP servers now run Streamable HTTP (stateless) instead
REM of the legacy SSE transport — see mcp_servers/*/server.py. Their endpoint
REM is now mounted at /mcp instead of /sse; only the printed banner below
REM changed.
REM
REM Usage:
REM   start_servers.bat
REM   start_servers.bat --no-streamlit
REM ─────────────────────────────────────────────────────────────────────────

setlocal enabledelayedexpansion

set ORACLE_MCP_PORT=8001
set NEO4J_MCP_PORT=8002
set BACKEND_PORT=8000
set STREAMLIT_PORT=8501
set START_STREAMLIT=true
set LOG_DIR=logs

REM ── Parse args ────────────────────────────────────────────────────────────
for %%A in (%*) do (
    if "%%A"=="--no-streamlit" set START_STREAMLIT=false
)

REM ── Pre-flight ────────────────────────────────────────────────────────────
echo.
echo [nlsql] NL-SQL Phase 3B - Starting all servers
echo ================================================

if not exist ".env" (
    echo [ERROR] .env not found. Copy .env.example and fill credentials.
    exit /b 1
)
if not exist "databases.yaml" (
    echo [ERROR] databases.yaml not found. Run from project root.
    exit /b 1
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM ── 1. Oracle MCP Server ─────────────────────────────────────────────────
echo [nlsql] Starting Oracle MCP server on :%ORACLE_MCP_PORT% (transport=streamable-http, stateless)...
set ORACLE_MCP_URL=http://localhost:%ORACLE_MCP_PORT%
start "Oracle MCP" /B cmd /c "python -m mcp_servers.oracle_mcp.server --port %ORACLE_MCP_PORT% > %LOG_DIR%\oracle_mcp.log 2>&1"

REM ── 2. Neo4j MCP Server ──────────────────────────────────────────────────
echo [nlsql] Starting Neo4j MCP server on :%NEO4J_MCP_PORT% (transport=streamable-http, stateless)...
set NEO4J_MCP_URL=http://localhost:%NEO4J_MCP_PORT%
start "Neo4j MCP" /B cmd /c "python -m mcp_servers.neo4j_mcp.server --port %NEO4J_MCP_PORT% > %LOG_DIR%\neo4j_mcp.log 2>&1"

REM ── Wait for MCP servers ──────────────────────────────────────────────────
echo [nlsql] Waiting 6s for MCP servers to initialise...
timeout /t 6 /nobreak >nul

REM ── 3. FastAPI Backend ────────────────────────────────────────────────────
echo [nlsql] Starting FastAPI backend on :%BACKEND_PORT%...
start "FastAPI" /B cmd /c "uvicorn backend.main:app --host 0.0.0.0 --port %BACKEND_PORT% --reload > %LOG_DIR%\backend.log 2>&1"

timeout /t 4 /nobreak >nul

REM ── 4. Streamlit ─────────────────────────────────────────────────────────
if "%START_STREAMLIT%"=="true" (
    echo [nlsql] Starting Streamlit on :%STREAMLIT_PORT%...
    start "Streamlit" /B cmd /c "streamlit run frontend/app.py --server.port %STREAMLIT_PORT% --server.headless true > %LOG_DIR%\streamlit.log 2>&1"
    timeout /t 3 /nobreak >nul
)

REM ── Ready ─────────────────────────────────────────────────────────────────
echo.
echo ================================================
echo [nlsql] All servers running. Open:
echo.
echo     Frontend:   http://localhost:%STREAMLIT_PORT%
echo     API docs:   http://localhost:%BACKEND_PORT%/docs
echo     Oracle MCP: http://localhost:%ORACLE_MCP_PORT%/mcp
echo     Neo4j MCP:  http://localhost:%NEO4J_MCP_PORT%/mcp
echo.
echo     Logs: %LOG_DIR%\
echo ================================================
echo.
echo Press Ctrl+C in each window to stop individual servers.
pause
