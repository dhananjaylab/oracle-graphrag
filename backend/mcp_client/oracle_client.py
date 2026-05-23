"""
backend/mcp_client/oracle_client.py

OracleMCPClient — typed wrapper around MCPClientSession for Oracle tools.

Each method corresponds to one Oracle MCP tool.  If the MCP server is
unavailable (not started, crashed), the client falls back transparently
to calling backend.services.oracle_service directly so the query
pipeline degrades gracefully rather than failing hard.

Fallback behaviour is logged at WARNING level.
"""

from __future__ import annotations

import json
import logging

from backend.mcp_client.base import MCPClientSession

logger = logging.getLogger(__name__)

# ── MCP server URL (can be overridden via env) ─────────────────────────────────
import os
ORACLE_MCP_URL = os.getenv("ORACLE_MCP_URL", "http://localhost:8001")


class OracleMCPClient:
    """
    Typed MCP client for the Oracle MCP server.

    Instantiated as a module-level singleton in backend/mcp_client/__init__.py
    and connected during FastAPI startup.
    """

    def __init__(self, server_url: str = ORACLE_MCP_URL) -> None:
        self._session = MCPClientSession(server_url, name="oracle")

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        await self._session.connect()

    async def disconnect(self) -> None:
        await self._session.disconnect()

    async def ping(self) -> bool:
        return await self._session.ping()

    # ══════════════════════════════════════════════════════════════════════════
    # execute_query
    # ══════════════════════════════════════════════════════════════════════════

    async def execute_query(
        self,
        db_id:    str,
        sql:      str,
        max_rows: int = 1000,
    ) -> dict:
        """
        Execute a read-only SQL query via Oracle MCP server.

        Falls back to direct oracle_service.execute_sql() if MCP unavailable.

        Returns dict with keys: columns, rows, row_count, sql_executed,
        pii_warnings.
        """
        try:
            result = await self._session.call_tool("execute_query", {
                "db_id":    db_id,
                "sql":      sql,
                "max_rows": max_rows,
            })
            if isinstance(result, dict) and "error" in result:
                raise RuntimeError(result["error"])
            return result
        except Exception as exc:
            logger.warning("[oracle-mcp] execute_query fallback: %s", exc)
            return await self._fallback_execute(db_id, sql, max_rows)

    @staticmethod
    async def _fallback_execute(db_id: str, sql: str, max_rows: int) -> dict:
        import asyncio
        from backend.services import oracle_service
        return await asyncio.to_thread(oracle_service.execute_sql, db_id, sql, max_rows)

    # ══════════════════════════════════════════════════════════════════════════
    # explain_plan
    # ══════════════════════════════════════════════════════════════════════════

    async def explain_plan(self, db_id: str, sql: str) -> dict:
        """
        Run EXPLAIN PLAN via Oracle MCP server.

        Falls back to a no-cost result (cost=None, no flags) if MCP unavailable
        so ValidationAgent can still proceed without blocking.

        Returns dict: {cost, has_full_scan, has_cartesian, plan_text}
        """
        try:
            result = await self._session.call_tool("explain_plan", {
                "db_id": db_id,
                "sql":   sql,
            })
            if isinstance(result, dict) and "error" in result:
                raise RuntimeError(result["error"])
            return result
        except Exception as exc:
            logger.warning("[oracle-mcp] explain_plan fallback: %s", exc)
            return {
                "cost": None, "has_full_scan": False,
                "has_cartesian": False, "plan_text": "",
            }

    # ══════════════════════════════════════════════════════════════════════════
    # get_schema
    # ══════════════════════════════════════════════════════════════════════════

    async def get_schema(
        self,
        db_id:       str,
        schema_name: str = "",
    ) -> dict:
        """
        Retrieve enriched data-dictionary metadata via Oracle MCP server.

        Falls back to direct oracle_service.get_data_dictionary().

        Returns dict: {columns, foreign_keys, indexes, pk_map, view_names,
        row_counts}
        """
        try:
            result = await self._session.call_tool("get_schema", {
                "db_id":       db_id,
                "schema_name": schema_name,
            })
            if isinstance(result, dict) and "error" in result:
                raise RuntimeError(result["error"])
            return result
        except Exception as exc:
            logger.warning("[oracle-mcp] get_schema fallback: %s", exc)
            return await self._fallback_schema(db_id, schema_name)

    @staticmethod
    async def _fallback_schema(db_id: str, schema_name: str) -> dict:
        import asyncio
        from backend.services import oracle_service
        return await asyncio.to_thread(
            oracle_service.get_data_dictionary, db_id,
            schema=schema_name or None,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # list_databases
    # ══════════════════════════════════════════════════════════════════════════

    async def list_databases(self) -> list[dict]:
        """
        List all registered Oracle databases via MCP server.

        Falls back to db_manager.databases directly.
        """
        try:
            result = await self._session.call_tool("list_databases", {})
            if isinstance(result, list):
                return result
            raise RuntimeError(f"Unexpected response: {result}")
        except Exception as exc:
            logger.warning("[oracle-mcp] list_databases fallback: %s", exc)
            from backend.db_manager import db_manager
            return [
                {
                    "id": d.id, "name": d.name,
                    "description": d.description,
                    "schema": d.qualified_schema,
                    "configured": d.is_configured,
                }
                for d in db_manager.databases
            ]

    # ══════════════════════════════════════════════════════════════════════════
    # check_read_only
    # ══════════════════════════════════════════════════════════════════════════

    async def check_read_only(self, sql: str) -> dict:
        """
        Fast pre-flight check for DML/DDL keywords via MCP server.

        Falls back to direct oracle_service.validate_read_only().

        Returns dict: {valid: bool, forbidden_keywords: list[str]}
        """
        try:
            result = await self._session.call_tool("check_read_only", {"sql": sql})
            return result
        except Exception as exc:
            logger.warning("[oracle-mcp] check_read_only fallback: %s", exc)
            from backend.services import oracle_service
            try:
                oracle_service.validate_read_only(sql)
                return {"valid": True, "forbidden_keywords": []}
            except ValueError as ve:
                return {"valid": False, "forbidden_keywords": [str(ve)]}
