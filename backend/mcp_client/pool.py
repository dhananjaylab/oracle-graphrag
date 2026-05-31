"""
backend/mcp_client/pool.py  (Phase 4C)

MCPConnectionPool — async connection pool for MCP SSE servers.

Replaces the single MCPClientSession + asyncio.Lock design that serialised
every concurrent request behind one lock. The pool:

  • Pre-creates min_size sessions at startup.
  • Grows on demand up to max_size (creates a new session when all
    available sessions are checked out and the pool is below max_size).
  • Validates sessions on check-in; discards dead ones silently.
  • Exposes pool.stats for /api/health reporting.
  • Wraps each tool call with an optional execution timeout.

Usage (in oracle_client / neo4j_client):
    pool = MCPConnectionPool("http://localhost:8001", "oracle", min_size=2, max_size=8)
    await pool.connect()
    result = await pool.call_tool("execute_query", {"db_id": "fincore", ...})
    await pool.disconnect()
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from backend.mcp_client.base import MCPClientSession

logger = logging.getLogger(__name__)

_DEFAULT_MIN   = int(os.getenv("MCP_POOL_MIN",   "2"))
_DEFAULT_MAX   = int(os.getenv("MCP_POOL_MAX",   "8"))
_CHECKOUT_WAIT = float(os.getenv("MCP_CHECKOUT_TIMEOUT_S", "10"))
_TOOL_TIMEOUT  = float(os.getenv("MCP_TOOL_TIMEOUT_S",     "60"))


class _PooledSession:
    """Thin wrapper that tracks checkout timestamp."""

    __slots__ = ("session", "checked_out_at")

    def __init__(self, session: MCPClientSession) -> None:
        self.session        = session
        self.checked_out_at: float | None = None


class MCPConnectionPool:
    """
    Async connection pool that owns a set of MCPClientSession objects.

    Lifecycle:
        pool = MCPConnectionPool(url, name)
        await pool.connect()         # called once at FastAPI startup
        result = await pool.call_tool("tool_name", args)
        await pool.disconnect()      # called at shutdown
    """

    def __init__(
        self,
        server_url: str,
        name:       str,
        min_size:   int = _DEFAULT_MIN,
        max_size:   int = _DEFAULT_MAX,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.name       = name
        self.min_size   = max(1, min_size)
        self.max_size   = max(self.min_size, max_size)

        self._available: list[_PooledSession] = []
        self._in_use:    list[_PooledSession] = []
        self._lock       = asyncio.Lock()
        self._connected  = False

        # Metrics
        self._total_calls      = 0
        self._total_errors     = 0
        self._total_created    = 0
        self._total_discarded  = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Create min_size sessions and warm the pool."""
        async with self._lock:
            for _ in range(self.min_size):
                ps = await self._create_session()
                if ps:
                    self._available.append(ps)
            self._connected = bool(self._available)
        if self._connected:
            logger.info("[pool:%s] Ready — %d/%d sessions", self.name,
                        len(self._available), self.max_size)
        else:
            logger.warning("[pool:%s] No sessions created — fallback active", self.name)

    async def disconnect(self) -> None:
        """Close all sessions gracefully."""
        async with self._lock:
            all_ps = self._available + self._in_use
            for ps in all_ps:
                try:
                    await ps.session.disconnect()
                except Exception:
                    pass
            self._available.clear()
            self._in_use.clear()
            self._connected = False
        logger.info("[pool:%s] Disconnected", self.name)

    # ── Public interface ───────────────────────────────────────────────────────

    async def call_tool(
        self,
        tool_name:        str,
        arguments:        dict[str, Any],
        timeout_s:        float = _TOOL_TIMEOUT,
    ) -> Any:
        """
        Check out a session, call the tool, return the session.
        Raises RuntimeError on timeout or repeated failure.
        """
        self._total_calls += 1
        try:
            async with self._checkout_session() as ps:
                try:
                    result = await asyncio.wait_for(
                        ps.session.call_tool(tool_name, arguments),
                        timeout=timeout_s,
                    )
                    return result
                except asyncio.TimeoutError:
                    # Mark session as bad so check-in discards it
                    await ps.session.disconnect()
                    raise RuntimeError(
                        f"[pool:{self.name}] {tool_name} timed out after {timeout_s}s"
                    )
        except Exception:
            self._total_errors += 1
            raise

    async def ping(self) -> bool:
        """Return True if at least one session in the pool is reachable."""
        try:
            async with self._checkout_session() as ps:
                return await ps.session.ping()
        except Exception:
            return False

    @property
    def stats(self) -> dict:
        return {
            "name":            self.name,
            "available":       len(self._available),
            "in_use":          len(self._in_use),
            "min_size":        self.min_size,
            "max_size":        self.max_size,
            "total_calls":     self._total_calls,
            "total_errors":    self._total_errors,
            "total_created":   self._total_created,
            "total_discarded": self._total_discarded,
            "error_rate":      round(
                self._total_errors / self._total_calls, 4
            ) if self._total_calls else 0.0,
        }

    # ── Internal helpers ───────────────────────────────────────────────────────

    @asynccontextmanager
    async def _checkout_session(self):
        """
        Async context manager that yields a _PooledSession.
        Creates a new one on demand if pool is below max_size.
        Blocks up to _CHECKOUT_WAIT if pool is full.
        """
        ps = await self._acquire(timeout=_CHECKOUT_WAIT)
        try:
            yield ps
        finally:
            await self._release(ps)

    async def _acquire(self, timeout: float) -> _PooledSession:
        deadline = time.monotonic() + timeout
        while True:
            async with self._lock:
                # Return an available session if one exists
                if self._available:
                    ps = self._available.pop()
                    ps.checked_out_at = time.monotonic()
                    self._in_use.append(ps)
                    return ps

                # Grow the pool if below max
                total = len(self._available) + len(self._in_use)
                if total < self.max_size:
                    ps = await self._create_session()
                    if ps:
                        ps.checked_out_at = time.monotonic()
                        self._in_use.append(ps)
                        return ps

            # Nothing available — wait briefly and retry
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"[pool:{self.name}] Pool exhausted — no session available "
                    f"after {timeout}s (max_size={self.max_size})"
                )
            await asyncio.sleep(min(0.2, remaining))

    async def _release(self, ps: _PooledSession) -> None:
        async with self._lock:
            try:
                self._in_use.remove(ps)
            except ValueError:
                pass  # already removed (e.g. after timeout)

            if ps.session._connected:
                ps.checked_out_at = None
                self._available.append(ps)
            else:
                # Session died — discard and replenish if below min
                self._total_discarded += 1
                logger.debug("[pool:%s] Dead session discarded", self.name)
                total = len(self._available) + len(self._in_use)
                if total < self.min_size:
                    new_ps = await self._create_session()
                    if new_ps:
                        self._available.append(new_ps)

    async def _create_session(self) -> "_PooledSession | None":
        """Create and connect a single new MCPClientSession. Returns None on failure."""
        session = MCPClientSession(self.server_url, name=self.name)
        try:
            await session.connect()
            self._total_created += 1
            logger.debug("[pool:%s] Session created (total_created=%d)",
                         self.name, self._total_created)
            return _PooledSession(session)
        except Exception as exc:
            logger.warning("[pool:%s] Failed to create session: %s", self.name, exc)
            return None

    # ── Convenience context manager ────────────────────────────────────────────

    async def __aenter__(self) -> "MCPConnectionPool":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.disconnect()
