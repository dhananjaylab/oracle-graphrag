"""
backend/mcp_client/base.py

MCPClientSession — persistent Streamable HTTP MCP client.

CHANGED from the original SSE implementation:

  • Transport: mcp.client.streamable_http.streamablehttp_client instead of
    mcp.client.sse.sse_client, talking to the server's /mcp endpoint
    instead of /sse. Pairs with the server-side stateless_http=True
    migration in mcp_servers/*/server.py.

    Because the server is stateless, this session no longer NEEDS to be
    pinned to one specific server process for correctness — any replica
    can answer any call. The persistent-connection pattern below is kept
    for connection-reuse efficiency (amortizing TCP/TLS handshake cost
    across many tool calls), not because the protocol requires session
    affinity anymore. If you put a load balancer in front of multiple
    server replicas, you can do so with a plain round-robin policy.

  • Reconnects now use exponential backoff with jitter (mirrors the
    pattern already used for Gemini retries in
    backend/services/gemini_service.py) instead of retrying immediately,
    to avoid every pooled session hammering a recovering server at once.

  • Optional bearer-token auth: pass auth_token to the constructor (or
    let MCPConnectionPool forward it from ORACLE_MCP_TOKEN / NEO4J_MCP_TOKEN)
    to send an Authorization header on every request. This is plumbing
    only — it does not implement the OAuth 2.1 authorization-code flow
    that the MCP spec specifies for remote servers; it's a placeholder
    so a static service-to-service token (or a token refreshed by an
    external process) can flow through today. A full OAuth 2.1 resource
    server / authorization server integration is a separate piece of
    work — see mcp.client.auth.OAuthClientProvider in the official SDK
    if/when that's prioritized.

Design notes
────────────
• Uses contextlib.AsyncExitStack to own both the streamablehttp_client
  and ClientSession context managers, keeping them alive for the
  lifetime of the application rather than per-request.
• On any transport error (connection reset, timeout, server restart)
  the client disconnects, backs off, reconnects, and retries the tool
  call once.
• All MCP tool return values are expected to be JSON strings;
  call_tool() deserializes them automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from contextlib import AsyncExitStack
from typing import Any

logger = logging.getLogger(__name__)

# ── Reconnect backoff configuration ─────────────────────────────────────────
_RECONNECT_BASE_DELAY_S = 0.5
_RECONNECT_MAX_DELAY_S  = 8.0


def _reconnect_delay(attempt: int) -> float:
    """Exponential backoff with jitter, capped at _RECONNECT_MAX_DELAY_S."""
    base = _RECONNECT_BASE_DELAY_S * (2 ** max(0, attempt - 1))
    return min(base, _RECONNECT_MAX_DELAY_S) + random.uniform(0, 0.25)


class MCPClientSession:
    """
    Persistent Streamable HTTP MCP client session.

    Usage (in FastAPI lifespan):

        client = MCPClientSession("http://localhost:8001", name="oracle")
        await client.connect()
        ...
        result = await client.call_tool("execute_query",
                                        {"db_id": "fincore", "sql": "..."})
        ...
        await client.disconnect()
    """

    def __init__(
        self,
        server_url: str,
        name:       str = "mcp",
        auth_token: str | None = None,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.name       = name
        self._headers: dict[str, str] | None = (
            {"Authorization": f"Bearer {auth_token}"} if auth_token else None
        )
        self._lock      = asyncio.Lock()
        self._session   = None          # mcp.ClientSession
        self._get_session_id = None     # callable, set after connect (HTTP-specific)
        self._exit_stack: AsyncExitStack | None = None
        self._connected = False

    # ── Connection management ──────────────────────────────────────────────────

    async def connect(self) -> None:
        """Open a persistent Streamable HTTP connection and run the MCP initialize handshake."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        url = f"{self.server_url}/mcp"
        logger.info("[%s] Connecting to %s", self.name, url)
        self._exit_stack = AsyncExitStack()

        try:
            streams = await self._exit_stack.enter_async_context(
                streamablehttp_client(url, headers=self._headers)
            )
            read_stream, write_stream, get_session_id = streams
            self._get_session_id = get_session_id

            self._session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await self._session.initialize()
            self._connected = True

            # With stateless_http=True on the server, get_session_id() will
            # typically return None — that's expected, not an error: it
            # means the server isn't tracking per-client state, which is
            # exactly the property that makes horizontal scaling work.
            session_id = None
            try:
                session_id = self._get_session_id() if self._get_session_id else None
            except Exception:
                pass
            logger.info(
                "[%s] Connected and initialized (session_id=%s)",
                self.name, session_id or "none (stateless)",
            )
        except Exception as exc:
            await self._teardown()
            raise RuntimeError(
                f"[{self.name}] Failed to connect to {url}: {exc}"
            ) from exc

    async def disconnect(self) -> None:
        """Close the connection gracefully."""
        async with self._lock:
            await self._teardown()

    async def _teardown(self) -> None:
        if self._exit_stack:
            try:
                await self._exit_stack.aclose()
            except Exception as exc:
                logger.warning("[%s] Error during teardown: %s", self.name, exc)
        self._exit_stack      = None
        self._session         = None
        self._get_session_id  = None
        self._connected       = False

    # ── Tool invocation ────────────────────────────────────────────────────────

    async def call_tool(
        self,
        tool_name:  str,
        arguments:  dict[str, Any],
    ) -> Any:
        """
        Call a named MCP tool and return the deserialized response.

        Automatically reconnects once (with backoff) if the connection was
        lost between calls.

        Args:
            tool_name:  Registered tool name (e.g. "execute_query").
            arguments:  Tool input arguments as a plain dict.

        Returns:
            Parsed JSON (dict / list / str / int) from the tool's JSON response.

        Raises:
            RuntimeError: if both the initial call and the reconnection retry fail.
        """
        async with self._lock:
            for attempt in range(1, 3):   # max 2 attempts (initial + 1 reconnect)
                try:
                    if not self._connected:
                        await self._reconnect(attempt=attempt)

                    result  = await self._session.call_tool(tool_name, arguments)
                    content = result.content[0].text if result.content else "{}"
                    return self._parse(content)

                except (RuntimeError, ConnectionError, OSError) as exc:
                    logger.warning(
                        "[%s] Tool call failed (attempt %d/%d): %s",
                        self.name, attempt, 2, exc,
                    )
                    await self._teardown()
                    if attempt == 2:
                        raise RuntimeError(
                            f"[{self.name}] {tool_name} failed after reconnect: {exc}"
                        ) from exc

                except Exception as exc:
                    # Non-transport errors (bad arguments, tool logic) — do not retry
                    raise RuntimeError(
                        f"[{self.name}] {tool_name} raised: {exc}"
                    ) from exc

    async def _reconnect(self, attempt: int = 1) -> None:
        delay = _reconnect_delay(attempt)
        logger.info(
            "[%s] Reconnecting in %.2fs (attempt %d)…", self.name, delay, attempt,
        )
        await asyncio.sleep(delay)
        await self._teardown()
        await self.connect()

    @staticmethod
    def _parse(text: str) -> Any:
        """Best-effort JSON parse; return raw string on failure."""
        try:
            val = json.loads(text)
            if isinstance(val, str):
                val_stripped = val.strip()
                if (val_stripped.startswith("{") and val_stripped.endswith("}")) or \
                   (val_stripped.startswith("[") and val_stripped.endswith("]")):
                    try:
                        return json.loads(val_stripped)
                    except Exception:
                        pass
            return val
        except (json.JSONDecodeError, TypeError):
            return text

    # ── Health check ───────────────────────────────────────────────────────────

    async def ping(self) -> bool:
        """Return True if the server is reachable and tools are listable."""
        try:
            async with self._lock:
                if not self._connected:
                    await self._reconnect()
                tools = await self._session.list_tools()
                return bool(tools)
        except Exception:
            return False

    # ── Context manager protocol (optional convenience) ────────────────────────

    async def __aenter__(self) -> "MCPClientSession":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.disconnect()

    def __repr__(self) -> str:
        status = "connected" if self._connected else "disconnected"
        return f"MCPClientSession(name={self.name!r}, url={self.server_url!r}, {status})"
