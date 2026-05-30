"""
backend/tools/tool_executor.py  (Phase 3C)

Routes every Gemini function call to the correct MCP client.
Returns a uniform dict result suitable for feeding back into the
Gemini chat as a FunctionResponse.

All results are JSON-safe — no numpy types, no raw OracleDB objects.
"""

from __future__ import annotations

import json
import logging
import time

from backend.tools.function_definitions import ORACLE_TOOL_NAMES, NEO4J_TOOL_NAMES

logger = logging.getLogger(__name__)


async def execute_tool(tool_name: str, args: dict) -> dict:
    """
    Dispatch a tool call to the appropriate MCP client.

    Args:
        tool_name: Name of the Gemini function being called.
        args:      Arguments from the Gemini FunctionCall (already dict).

    Returns:
        dict with at minimum {"ok": bool}.
        On error: {"ok": False, "error": "<message>"}.
    """
    t_start = time.monotonic()
    try:
        if tool_name in ORACLE_TOOL_NAMES:
            result = await _dispatch_oracle(tool_name, args)
        elif tool_name in NEO4J_TOOL_NAMES:
            result = await _dispatch_neo4j(tool_name, args)
        else:
            return {"ok": False, "error": f"Unknown tool: {tool_name}"}

        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        logger.debug("[tool_executor] %s completed in %dms", tool_name, elapsed_ms)
        return {"ok": True, "elapsed_ms": elapsed_ms, **result}

    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        logger.warning("[tool_executor] %s failed in %dms: %s", tool_name, elapsed_ms, exc)
        return {"ok": False, "error": str(exc), "elapsed_ms": elapsed_ms}


# ── Oracle MCP dispatch ────────────────────────────────────────────────────────

async def _dispatch_oracle(tool_name: str, args: dict) -> dict:
    from backend.mcp_client import oracle_mcp

    if tool_name == "execute_query":
        return await oracle_mcp.execute_query(
            db_id    = args["db_id"],
            sql      = args["sql"],
            max_rows = int(args.get("max_rows", 1000)),
        )

    if tool_name == "explain_plan":
        return await oracle_mcp.explain_plan(
            db_id = args["db_id"],
            sql   = args["sql"],
        )

    if tool_name == "get_schema":
        return await oracle_mcp.get_schema(
            db_id       = args["db_id"],
            schema_name = args.get("schema_name", ""),
        )

    if tool_name == "list_databases":
        dbs = await oracle_mcp.list_databases()
        return {"databases": dbs}

    if tool_name == "check_read_only":
        return await oracle_mcp.check_read_only(sql=args["sql"])

    raise ValueError(f"Unhandled Oracle tool: {tool_name}")


# ── Neo4j MCP dispatch ─────────────────────────────────────────────────────────

async def _dispatch_neo4j(tool_name: str, args: dict) -> dict:
    from backend.mcp_client import neo4j_mcp

    if tool_name == "semantic_search":
        emb = _parse_json_list(args["embedding_json"])
        return await neo4j_mcp.semantic_search(
            query_embedding = emb,
            database_id     = args["database_id"],
            top_k           = int(args.get("top_k", 12)),
        )

    if tool_name == "get_table_details":
        tables = _parse_json_list(args["table_names_json"])
        rows   = await neo4j_mcp.get_table_details(
            table_names = tables,
            database_id = args["database_id"],
        )
        return {"table_details": rows}

    if tool_name == "get_join_path":
        path = await neo4j_mcp.get_join_path(
            table1      = args["table1"],
            table2      = args["table2"],
            database_id = args["database_id"],
        )
        return {"join_path": path}

    if tool_name == "get_join_paths_batch":
        tables = _parse_json_list(args["table_names_json"])
        paths  = await neo4j_mcp.get_join_paths_batch(
            table_names = tables,
            database_id = args["database_id"],
        )
        return {"join_paths": paths}

    if tool_name == "get_cross_db_hints":
        tables = _parse_json_list(args["table_names_json"])
        hints  = await neo4j_mcp.get_cross_db_hints(
            table_names = tables,
            database_id = args["database_id"],
        )
        return {"cross_db_hints": hints}

    if tool_name == "search_patterns":
        emb      = _parse_json_list(args["embedding_json"])
        patterns = await neo4j_mcp.search_patterns(
            query_embedding = emb,
            database_id     = args["database_id"],
            top_k           = int(args.get("top_k", 3)),
            min_similarity  = float(args.get("min_similarity", 0.85)),
        )
        return {"patterns": patterns}

    if tool_name == "store_pattern":
        emb     = _parse_json_list(args["embedding_json"])
        tables  = _parse_json_list(args["tables_used_json"])
        stored  = await neo4j_mcp.store_pattern(
            database_id   = args["database_id"],
            nl_question   = args["nl_question"],
            sql           = args["sql"],
            schema_cypher = args.get("schema_cypher", ""),
            tables_used   = tables,
            execution_ms  = int(args.get("execution_ms", 0)),
            embedding     = emb,
        )
        return {"stored": stored}

    if tool_name == "get_schema_summary":
        summary = await neo4j_mcp.get_schema_summary()
        return summary

    raise ValueError(f"Unhandled Neo4j tool: {tool_name}")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_json_list(value: str | list) -> list:
    """Parse a JSON-encoded list if it arrived as a string."""
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
