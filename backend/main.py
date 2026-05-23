"""
backend/main.py  (v2 + Phase 3B)

FastAPI application with a lifespan context that:
  1. Connects both MCP clients (Oracle + Neo4j) on startup
  2. Gracefully disconnects them on shutdown

MCP client connection failures are non-fatal — clients automatically
fall back to direct service calls when MCP servers are unavailable.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.routes import query, schema
from backend.services.neo4j_service import close_driver

logger = logging.getLogger(__name__)


# ── Lifespan: connect MCP clients on startup ───────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Start-up: connect Oracle MCP client and Neo4j MCP client.
    Shut-down: disconnect MCP clients and Neo4j driver.
    """
    from backend.mcp_client import oracle_mcp, neo4j_mcp

    # ── Connect MCP clients (best-effort; fallback to direct service) ──────
    for client, label in [(oracle_mcp, "Oracle MCP"), (neo4j_mcp, "Neo4j MCP")]:
        try:
            await client.connect()
            alive = await client.ping()
            logger.info("[%s] Connected — ping=%s", label, alive)
        except Exception as exc:
            logger.warning(
                "[%s] Could not connect (%s). "
                "Pipeline will use direct service fallback.",
                label, exc,
            )

    yield   # ← application runs here

    # ── Shutdown ───────────────────────────────────────────────────────────
    for client, label in [(oracle_mcp, "Oracle MCP"), (neo4j_mcp, "Neo4j MCP")]:
        try:
            await client.disconnect()
            logger.info("[%s] Disconnected", label)
        except Exception as exc:
            logger.warning("[%s] Error during disconnect: %s", label, exc)

    await close_driver()
    logger.info("[Neo4j driver] Closed")


# ── Application ────────────────────────────────────────────────────────────────

app = FastAPI(
    title       = "NL-SQL Banking API",
    description = (
        "Natural language → Oracle SQL · "
        "GraphRAG + Gemini + ValidationAgent + SelfHealingAgent + MCP"
    ),
    version     = "3.0.0",
    lifespan    = lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins  = ["http://localhost:8501"],
    allow_methods  = ["*"],
    allow_headers  = ["*"],
)

app.include_router(query.router,  prefix="/api")
app.include_router(schema.router, prefix="/api")


@app.exception_handler(Exception)
async def global_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(
        status_code=200,
        content={"error": True, "detail": str(exc)},
    )
