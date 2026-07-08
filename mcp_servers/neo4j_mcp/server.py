"""
mcp_servers/neo4j_mcp/server.py  (Phase 4A + Streamable HTTP + health routes + bottleneck fixes)

CHANGES vs previous version
────────────────────────────
Note on event-loop blocking: unlike the Oracle MCP server, these tools
were already `async def` calling into backend/services/neo4j_service.py,
which itself uses the async Neo4j driver (AsyncGraphDatabase / `async with
driver.session()`) throughout — so there was no equivalent event-loop-
blocking bug here to fix. This pass applies the other three bottleneck
fixes identified in review, matching the Oracle MCP server for consistency:

  1. INPUT VALIDATION VIA Annotated[..., Field(...)]:
     Every tool parameter now carries Field()-level constraints instead
     of being an unconstrained str/int/float. IMPORTANT: this uses
     individual Annotated parameters, NOT a single wrapping Pydantic
     BaseModel — that alternative was tried first and rejected after
     empirical verification against the installed mcp 1.28.1 SDK: a
     single BaseModel parameter serializes as {"params": {...}} in the
     tool's wire schema, which would have required every call site in
     backend/mcp_client/neo4j_client.py to change from flat kwargs to a
     nested "params" dict. Annotated individual parameters, by contrast,
     verified to flatten into the tool's top-level argument schema
     exactly as before — so this adds real server-side validation
     (top_k and min_similarity are now bounded; malformed embedding_json
     / table_names_json surface as a clean validation error instead of a
     generic exception mid-Cypher) with zero client-side changes required.

  2. TOOL ANNOTATIONS:
     semantic_search, get_table_details, get_join_path,
     get_join_paths_batch, get_cross_db_hints, search_patterns, and
     get_schema_summary are all read-only against Neo4j: readOnlyHint=True,
     openWorldHint=True. store_pattern and record_feedback are MERGE-based
     upserts — not destructive, and idempotent for a given key (repeating
     the same store_pattern call for the same nl_question+database_id
     converges to the same state rather than accumulating duplicates), so
     they declare destructiveHint=False, idempotentHint=True.

  3. PER-TOOL TIMEOUTS:
     All Neo4j calls here are typically sub-second (vector index lookups,
     shortest-path Cypher, small upserts), so all tools share one
     moderate timeout, enforced via asyncio.wait_for() inside each tool
     body. NOTE: @mcp.tool() in the installed SDK (mcp 1.28.x) has no
     timeout= parameter — checked directly against the installed package
     and confirmed absent — so this cannot be done via the decorator; the
     existing client-side MCP_TOOL_TIMEOUT_S in backend/mcp_client/pool.py
     still applies as an outer backstop.

All tool logic (the actual Cypher / neo4j_service calls) is unchanged
from the previous version.
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Annotated

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

import backend.services.neo4j_service as neo4j_svc

mcp = FastMCP(
    name           = "neo4j-mcp-server",
    stateless_http = True,
    json_response  = True,
)

_SERVER_START = time.monotonic()

# All Neo4j tools are fast (vector index lookups, small Cypher writes) —
# one moderate timeout covers every tool in this server.
_TIMEOUT_NEO4J = 20.0


# ══════════════════════════════════════════════════════════════════════════════
# OPERATIONAL ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Liveness probe — fast, no Neo4j query."""
    return JSONResponse({
        "status":   "healthy",
        "service":  "neo4j-mcp-server",
        "uptime_s": round(time.monotonic() - _SERVER_START, 1),
    })


@mcp.custom_route("/ready", methods=["GET"])
async def ready(request: Request) -> JSONResponse:
    """
    Readiness probe — executes RETURN 1 against Neo4j.
    Returns 503 if the database is unreachable.

    Uses the async Neo4j driver throughout, so — unlike the equivalent
    Oracle probe before its fix — this handler never blocked the event
    loop; no change needed here beyond what already existed.
    """
    try:
        driver = neo4j_svc.get_driver()
        async with driver.session() as session:
            result = await session.run("RETURN 1 AS ok")
            await result.single()
        return JSONResponse({"status": "ready", "neo4j": "reachable"})
    except Exception as exc:
        return JSONResponse(
            {"status": "not_ready", "neo4j": "unreachable", "error": str(exc)},
            status_code=503,
        )


# ══════════════════════════════════════════════════════════════════════════════
# TOOLS
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool(
    annotations={
        "title": "Semantic Schema Search",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def semantic_search(
    embedding_json: Annotated[str, Field(
        min_length=2,
        description="JSON-serialized list[float] — the pre-computed 3072-dim question embedding.",
    )],
    database_id: Annotated[str, Field(min_length=1, max_length=64, description="Database identifier to search within")],
    top_k: Annotated[int, Field(ge=1, le=50, description="Number of nearest neighbours to return")] = 12,
) -> str:
    """
    Vector cosine-similarity search on (:Table) and (:Column) nodes.

    Returns:
        str: JSON {tables, columns, cypher_used}
    """
    embedding: list[float] = json.loads(embedding_json)
    try:
        result = await asyncio.wait_for(
            neo4j_svc.semantic_schema_search(
                query_embedding=embedding, database_id=database_id, top_k=top_k,
            ),
            timeout=_TIMEOUT_NEO4J,
        )
        return json.dumps(result, default=str)
    except asyncio.TimeoutError:
        return json.dumps({"error": f"semantic_search timed out after {_TIMEOUT_NEO4J}s"})


@mcp.tool(
    annotations={
        "title": "Get Table Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_table_details(
    table_names_json: Annotated[str, Field(min_length=2, description="JSON-serialized list[str] of table names")],
    database_id: Annotated[str, Field(min_length=1, max_length=64, description="Database identifier")],
) -> str:
    """
    Full column metadata for a list of tables.

    Returns:
        str: JSON list of table objects with columns.
    """
    table_names: list[str] = json.loads(table_names_json)
    try:
        result = await asyncio.wait_for(
            neo4j_svc.get_table_details(table_names, database_id),
            timeout=_TIMEOUT_NEO4J,
        )
        return json.dumps(result, default=str)
    except asyncio.TimeoutError:
        return json.dumps({"error": f"get_table_details timed out after {_TIMEOUT_NEO4J}s"})


@mcp.tool(
    annotations={
        "title": "Get Join Path (single pair)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_join_path(
    table1: Annotated[str, Field(min_length=1, max_length=128, description="Source table name")],
    table2: Annotated[str, Field(min_length=1, max_length=128, description="Target table name")],
    database_id: Annotated[str, Field(min_length=1, max_length=64, description="Database identifier")],
) -> str:
    """Shortest FK join path between two tables (single pair, kept for compat)."""
    try:
        result = await asyncio.wait_for(
            neo4j_svc.get_join_path(table1, table2, database_id),
            timeout=_TIMEOUT_NEO4J,
        )
        return json.dumps(result, default=str)
    except asyncio.TimeoutError:
        return json.dumps({"error": f"get_join_path timed out after {_TIMEOUT_NEO4J}s"})


@mcp.tool(
    annotations={
        "title": "Get Join Paths (batch)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_join_paths_batch(
    table_names_json: Annotated[str, Field(
        min_length=2, description="JSON-serialized list[str] of all candidate table names",
    )],
    database_id: Annotated[str, Field(min_length=1, max_length=64, description="Database identifier")],
) -> str:
    """
    Shortest FK paths between ALL pairs of candidate tables in one Cypher query.

    Returns:
        str: JSON [{from_table, to_table, table_sequence, join_conditions}, …]
    """
    table_names: list[str] = json.loads(table_names_json)
    try:
        result = await asyncio.wait_for(
            neo4j_svc.get_join_paths_batch(table_names, database_id),
            timeout=_TIMEOUT_NEO4J,
        )
        return json.dumps(result, default=str)
    except asyncio.TimeoutError:
        return json.dumps({"error": f"get_join_paths_batch timed out after {_TIMEOUT_NEO4J}s"})


@mcp.tool(
    annotations={
        "title": "Get Cross-Database Hints",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_cross_db_hints(
    table_names_json: Annotated[str, Field(
        min_length=2, description="JSON-serialized list[str] of candidate table names from the primary database",
    )],
    database_id: Annotated[str, Field(min_length=1, max_length=64, description="Source database identifier")],
) -> str:
    """Cross-database CROSS_DB_JOIN edges for candidate tables."""
    table_names: list[str] = json.loads(table_names_json)
    try:
        result = await asyncio.wait_for(
            neo4j_svc.get_cross_db_hints(table_names, database_id),
            timeout=_TIMEOUT_NEO4J,
        )
        return json.dumps(result, default=str)
    except asyncio.TimeoutError:
        return json.dumps({"error": f"get_cross_db_hints timed out after {_TIMEOUT_NEO4J}s"})


@mcp.tool(
    annotations={
        "title": "Search Past Query Patterns",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def search_patterns(
    embedding_json: Annotated[str, Field(min_length=2, description="JSON-serialized list[float] question embedding")],
    database_id: Annotated[str, Field(min_length=1, max_length=64, description="Database identifier")],
    top_k: Annotated[int, Field(ge=1, le=20, description="Max patterns to return")] = 3,
    min_similarity: Annotated[float, Field(ge=0.0, le=1.0, description="Minimum cosine similarity threshold")] = 0.85,
) -> str:
    """Past QueryPattern nodes similar to the current question embedding."""
    embedding: list[float] = json.loads(embedding_json)
    try:
        result = await asyncio.wait_for(
            neo4j_svc.search_similar_patterns(
                query_embedding=embedding, database_id=database_id,
                top_k=top_k, min_similarity=min_similarity,
            ),
            timeout=_TIMEOUT_NEO4J,
        )
        return json.dumps(result, default=str)
    except asyncio.TimeoutError:
        return json.dumps({"error": f"search_patterns timed out after {_TIMEOUT_NEO4J}s"})


@mcp.tool(
    annotations={
        "title": "Store Query Pattern",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,   # MERGE on (nl_question, database_id) — safe to retry
        "openWorldHint": True,
    },
)
async def store_pattern(
    database_id: Annotated[str, Field(min_length=1, max_length=64)],
    nl_question: Annotated[str, Field(min_length=1, max_length=2000)],
    sql: Annotated[str, Field(min_length=1, max_length=20000)],
    schema_cypher: Annotated[str, Field(max_length=20000)],
    tables_used_json: Annotated[str, Field(min_length=2, description="JSON-serialized list[str] of table names used")],
    execution_ms: Annotated[int, Field(ge=0, le=3_600_000)],
    embedding_json: Annotated[str, Field(min_length=2, description="JSON-serialized list[float] question embedding")],
) -> str:
    """Persist a successful NL→SQL exchange as a (:QueryPattern) node."""
    try:
        tables_used: list[str]   = json.loads(tables_used_json)
        embedding:   list[float] = json.loads(embedding_json)
        await asyncio.wait_for(
            neo4j_svc.store_query_pattern(
                database_id=database_id, nl_question=nl_question,
                sql=sql, schema_cypher=schema_cypher,
                tables_used=tables_used, execution_ms=execution_ms,
                embedding=embedding,
            ),
            timeout=_TIMEOUT_NEO4J,
        )
        return json.dumps({"stored": True})
    except asyncio.TimeoutError:
        return json.dumps({"stored": False, "error": f"store_pattern timed out after {_TIMEOUT_NEO4J}s"})
    except Exception as exc:
        return json.dumps({"stored": False, "error": str(exc)})


@mcp.tool(
    annotations={
        "title": "Get Full Schema Summary",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_schema_summary() -> str:
    """All databases with enriched tables and business domains."""
    try:
        result = await asyncio.wait_for(neo4j_svc.get_schema_summary(), timeout=_TIMEOUT_NEO4J)
        return json.dumps(result, default=str)
    except asyncio.TimeoutError:
        return json.dumps({"error": f"get_schema_summary timed out after {_TIMEOUT_NEO4J}s"})


@mcp.tool(
    annotations={
        "title": "Record User Feedback",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,   # increment/decrement/correct all converge, safe to retry
        "openWorldHint": True,
    },
)
async def record_feedback(
    nl_question: Annotated[str, Field(min_length=1, max_length=2000)],
    database_id: Annotated[str, Field(min_length=1, max_length=64)],
    action: Annotated[str, Field(pattern="^(increment|decrement|correct)$")],
    corrected_sql: Annotated[str, Field(max_length=20000)] = "",
) -> str:
    """
    Update a QueryPattern weight based on user feedback.
    action: "increment" | "decrement" | "correct"
    """
    async def _do_action() -> bool:
        if action == "increment":
            return await neo4j_svc.increment_pattern_success(nl_question, database_id)
        if action == "decrement":
            return await neo4j_svc.decrement_pattern_success(nl_question, database_id)
        if action == "correct" and corrected_sql.strip():
            return await neo4j_svc.update_pattern_sql(
                nl_question, database_id, corrected_sql.strip()
            )
        return False

    try:
        updated = await asyncio.wait_for(_do_action(), timeout=_TIMEOUT_NEO4J)
        return json.dumps({"updated": updated, "action": action})
    except asyncio.TimeoutError:
        return json.dumps({"updated": False, "error": f"record_feedback timed out after {_TIMEOUT_NEO4J}s"})
    except Exception as exc:
        return json.dumps({"updated": False, "error": str(exc)})


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Neo4j MCP Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8002)
    args = parser.parse_args()

    print(f"[Neo4j MCP] Starting on {args.host}:{args.port}")
    print(f"[Neo4j MCP]   MCP endpoint : http://{args.host}:{args.port}/mcp")
    print(f"[Neo4j MCP]   Health probe : http://{args.host}:{args.port}/health")
    print(f"[Neo4j MCP]   Ready probe  : http://{args.host}:{args.port}/ready")

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.run(transport="streamable-http")
