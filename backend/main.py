"""
backend/main.py  (v2 + Phase 3B + Phase 3C)

Phase 3C adds:
  POST /api/supervisor  — SSE streaming supervisor endpoint
  Feature flag is handled on the client side (Streamlit toggle).
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.routes import query, schema
from backend.routes.supervisor import router as supervisor_router
from backend.services.neo4j_service import close_driver

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from backend.mcp_client import oracle_mcp, neo4j_mcp
    for client, label in [(oracle_mcp, "Oracle MCP"), (neo4j_mcp, "Neo4j MCP")]:
        try:
            await client.connect()
            alive = await client.ping()
            logger.info("[%s] Connected — ping=%s", label, alive)
        except Exception as exc:
            logger.warning("[%s] Unavailable (%s) — fallback active.", label, exc)
    yield
    for client, label in [(oracle_mcp, "Oracle MCP"), (neo4j_mcp, "Neo4j MCP")]:
        try:
            await client.disconnect()
        except Exception as exc:
            logger.warning("[%s] Disconnect error: %s", label, exc)
    await close_driver()


app = FastAPI(
    title       = "NL-SQL Banking API",
    description = "Natural language → Oracle SQL · GraphRAG + Gemini + MCP + Supervisor",
    version     = "3.1.0",
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
app.include_router(supervisor_router, prefix="/api")  # Phase 3C


@app.exception_handler(Exception)
async def global_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled: %s", exc)
    return JSONResponse(status_code=200, content={"error": True, "detail": str(exc)})
