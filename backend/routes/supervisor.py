"""
backend/routes/supervisor.py  (Phase 3C)

POST /api/supervisor  — SSE streaming endpoint.

Streams SupervisorEvents as Server-Sent Events while the
Gemini supervisor loop runs. Each event is a named SSE frame
the Streamlit frontend consumes with httpx streaming.

SSE frame format:
    event: <event_type>
    data: <json-payload>
    \\n\\n

Event types:
    thinking    — supervisor started, routing info
    tool_call   — about to call an MCP tool
    tool_result — tool call completed
    sql         — execute_query succeeded, row count
    finish      — loop complete, full result payload
    error       — unrecoverable failure
"""

import asyncio
import json
import logging
from typing import AsyncGenerator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from backend.db_manager import db_manager
from backend.agents.supervisor_agent import supervisor_agent
from backend.models import SupervisorRequest
from backend.services.gemini_service import get_embedding

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/supervisor")
async def supervisor(request: SupervisorRequest):
    """
    Full agentic query endpoint — runs Gemini function calling loop
    and streams progress as Server-Sent Events.

    The client receives named SSE frames for each tool call, result,
    and the final completion event. The Streamlit UI renders these
    progressively without waiting for the full response.
    """
    return StreamingResponse(
        _stream_supervisor(request),
        media_type    = "text/event-stream",
        headers       = {
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "http://localhost:8501",
        },
    )


async def _stream_supervisor(request: SupervisorRequest) -> AsyncGenerator[str, None]:
    """
    Async generator that runs the supervisor and yields SSE-formatted strings.
    """
    # ── Pre-compute embedding (before starting the supervisor loop) ────────
    try:
        query_embedding: list[float] = await asyncio.to_thread(
            get_embedding, request.question
        )
    except Exception as exc:
        yield _sse("error", {"message": f"Embedding failed: {exc}"})
        return

    # ── Collect databases ──────────────────────────────────────────────────
    try:
        from backend.mcp_client import oracle_mcp
        databases = await oracle_mcp.list_databases()
    except Exception:
        databases = [
            {
                "id":          d.id,
                "name":        d.name,
                "description": d.description,
                "configured":  d.is_configured,
            }
            for d in db_manager.databases
        ]

    # ── Build compressed conversation context ─────────────────────────────
    conversation_history = request.conversation_history or []

    # ── Run supervisor and stream events ──────────────────────────────────
    try:
        async for event in supervisor_agent.run_stream(
            question             = request.question,
            query_embedding      = query_embedding,
            databases            = databases,
            conversation_history = conversation_history,
        ):
            yield _sse(event.event_type, event.data)
            await asyncio.sleep(0)   # yield control to the event loop

    except asyncio.CancelledError:
        logger.info("Supervisor stream cancelled by client")
        return
    except Exception as exc:
        logger.exception("Supervisor stream failed: %s", exc)
        yield _sse("error", {"message": str(exc)})


def _sse(event_type: str, data: dict) -> str:
    """Format a dict as a named SSE frame."""
    payload = json.dumps(data, default=str)
    return f"event: {event_type}\ndata: {payload}\n\n"
