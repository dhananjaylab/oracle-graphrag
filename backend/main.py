"""
backend/main.py  (Phase 4)

Phase 4 changes vs Phase 3C:
  • lifespan connects pool-based oracle_mcp / neo4j_mcp
    (MCPConnectionPool instead of MCPClientSession)
  • /api/health (via schema.py) now returns pool stats + cache stats
  • Cache singletons are imported to ensure they are initialised on startup
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.routes import query, schema
from backend.routes.supervisor import router as supervisor_router
from backend.services.neo4j_service import close_driver

# Ensure cache singletons are initialised at import time
from backend.cache import embedding_cache, schema_cache, result_cache  # noqa: F401

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup: connect MCP pools ─────────────────────────────────────────
    from backend.mcp_client import oracle_mcp, neo4j_mcp
    for client, label in [(oracle_mcp, "Oracle MCP pool"), (neo4j_mcp, "Neo4j MCP pool")]:
        try:
            await client.connect()
            alive = await client.ping()
            logger.info("[%s] Connected — ping=%s  stats=%s",
                        label, alive, client.pool_stats)
        except Exception as exc:
            logger.warning("[%s] Unavailable (%s) — fallback active.", label, exc)

    yield

    # ── Shutdown: drain pools ──────────────────────────────────────────────
    from backend.mcp_client import oracle_mcp, neo4j_mcp
    for client, label in [(oracle_mcp, "Oracle MCP pool"), (neo4j_mcp, "Neo4j MCP pool")]:
        try:
            await client.disconnect()
        except Exception as exc:
            logger.warning("[%s] Disconnect error: %s", label, exc)
    await close_driver()


app = FastAPI(
    title       = "NL-SQL Banking API",
    description = "Natural language → Oracle SQL · GraphRAG + Gemini + MCP + Supervisor",
    version     = "4.0.0",
    lifespan    = lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins  = ["http://localhost:8501"],
    allow_methods  = ["*"],
    allow_headers  = ["*"],
)

app.include_router(query.router,      prefix="/api")
app.include_router(schema.router,     prefix="/api")
app.include_router(supervisor_router, prefix="/api")


@app.exception_handler(Exception)
async def global_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled: %s", exc)
    return JSONResponse(status_code=200, content={"error": True, "detail": str(exc)})
