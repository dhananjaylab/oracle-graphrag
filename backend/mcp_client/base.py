"""
backend/mcp_client/base.py

MCPClientSession — persistent SSE-based MCP client.

Maintains a single long-lived SSE connection to an MCP server.
Reconnects automatically on network failures.
Thread-safe via asyncio.Lock for concurrent route handlers.

Design notes
────────────
• Uses contextlib.AsyncExitStack to own both the sse_client and
  ClientSession context managers, keeping them alive for the
  lifetime of the application rather than per-request.
• On any transport error (connection reset, timeout, server restart)
  the client disconnects, reconnects, and retries the tool call once.
• All MCP tool return values are expected to be JSON strings;
  call_tool() deserializes them automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from typing import Any

logger = logging.getLogger(__name__)


class MCPClientSession:
    """
    Persistent SSE-backed MCP client session.

    Usage (in FastAPI lifespan):

        client = MCPClientSession("http://localhost:8001", name="oracle")
        await client.connect()
        ...
        result = await client.call_tool("execute_query",
                                        {"db_id": "fincore", "sql": "..."})
        ...
        await client.disconnect()
    """

    def __init__(self, server_url: str, name: str = "mcp") -> None:
        self.server_url = server_url.rstrip("/")
        self.name       = name
        self._lock      = asyncio.Lock()
        self._session   = None          # mcp.ClientSession
        self._exit_stack: AsyncExitStack | None = None
        self._connected = False

    # ── Connection management ──────────────────────────────────────────────────

    async def connect(self) -> None:
        """Open a persistent SSE connection and run MCP initialize handshake."""
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        logger.info("[%s] Connecting to %s", self.name, self.server_url)
        self._exit_stack = AsyncExitStack()

        try:
            streams = await self._exit_stack.enter_async_context(
                sse_client(url=f"{self.server_url}/sse")
            )
            self._session = await self._exit_stack.enter_async_context(
                ClientSession(*streams)
            )
            await self._session.initialize()
            self._connected = True
            logger.info("[%s] Connected and initialized", self.name)
        except Exception as exc:
            await self._teardown()
            raise RuntimeError(
                f"[{self.name}] Failed to connect to {self.server_url}: {exc}"
            ) from exc

    async def disconnect(self) -> None:
        """Close the SSE connection gracefully."""
        async with self._lock:
            await self._teardown()

    async def _teardown(self) -> None:
        if self._exit_stack:
            try:
                await self._exit_stack.aclose()
            except Exception as exc:
                logger.warning("[%s] Error during teardown: %s", self.name, exc)
        self._exit_stack = None
        self._session    = None
        self._connected  = False

    # ── Tool invocation ────────────────────────────────────────────────────────

    async def call_tool(
        self,
        tool_name:  str,
        arguments:  dict[str, Any],
    ) -> Any:
        """
        Call a named MCP tool and return the deserialized response.

        Automatically reconnects once if the connection was lost between calls.

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
                        await self._reconnect()

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

    async def _reconnect(self) -> None:
        logger.info("[%s] Reconnecting…", self.name)
        await self._teardown()
        await self.connect()

    @staticmethod
    def _parse(text: str) -> Any:
        """Best-effort JSON parse; return raw string on failure."""
        try:
            return json.loads(text)
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
