"""
mcp_servers/neo4j_mcp/server.py  (Phase 4A + Streamable HTTP + health routes)

CHANGES vs previous version
────────────────────────────
  1. @mcp.custom_route("/health", methods=["GET"])
     Liveness probe — returns 200 as long as the process is up and the
     Neo4j driver has been initialised.

  2. @mcp.custom_route("/ready", methods=["GET"])
     Readiness probe — runs a lightweight Cypher (RETURN 1) against the
     live Neo4j instance. Returns 503 if Neo4j is unreachable so the load
     balancer removes this replica from rotation.

  3. All tool logic is unchanged from the previous version.
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

import backend.services.neo4j_service as neo4j_svc

mcp = FastMCP(
    name           = "neo4j-mcp-server",
    stateless_http = True,
    json_response  = True,
)

_SERVER_START = time.monotonic()


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

@mcp.tool()
async def semantic_search(embedding_json: str, database_id: str, top_k: int = 12) -> str:
    """
    Vector cosine-similarity search on (:Table) and (:Column) nodes.

    Args:
        embedding_json: JSON list[float] — 3072-dim question embedding.
        database_id:    Database identifier (e.g. "fincore").
        top_k:          Nearest neighbours per index.

    Returns JSON: {tables, columns, cypher_used}
    """
    embedding: list[float] = json.loads(embedding_json)
    result = await neo4j_svc.semantic_schema_search(
        query_embedding=embedding, database_id=database_id, top_k=top_k,
    )
    return json.dumps(result, default=str)


@mcp.tool()
async def get_table_details(table_names_json: str, database_id: str) -> str:
    """
    Full column metadata for a list of tables.

    Args:
        table_names_json: JSON list[str] of table names.
        database_id:      Database identifier.

    Returns JSON: list of table objects with columns.
    """
    table_names: list[str] = json.loads(table_names_json)
    result = await neo4j_svc.get_table_details(table_names, database_id)
    return json.dumps(result, default=str)


@mcp.tool()
async def get_join_path(table1: str, table2: str, database_id: str) -> str:
    """Shortest FK join path between two tables (single pair, kept for compat)."""
    result = await neo4j_svc.get_join_path(table1, table2, database_id)
    return json.dumps(result, default=str)


@mcp.tool()
async def get_join_paths_batch(table_names_json: str, database_id: str) -> str:
    """
    Shortest FK paths between ALL pairs of candidate tables in one Cypher query.

    Args:
        table_names_json: JSON list[str] of candidate table names.
        database_id:      Database identifier.

    Returns JSON: [{from_table, to_table, table_sequence, join_conditions}, …]
    """
    table_names: list[str] = json.loads(table_names_json)
    result = await neo4j_svc.get_join_paths_batch(table_names, database_id)
    return json.dumps(result, default=str)


@mcp.tool()
async def get_cross_db_hints(table_names_json: str, database_id: str) -> str:
    """Cross-database CROSS_DB_JOIN edges for candidate tables."""
    table_names: list[str] = json.loads(table_names_json)
    result = await neo4j_svc.get_cross_db_hints(table_names, database_id)
    return json.dumps(result, default=str)


@mcp.tool()
async def search_patterns(
    embedding_json: str,
    database_id:    str,
    top_k:          int   = 3,
    min_similarity: float = 0.85,
) -> str:
    """Past QueryPattern nodes similar to the current question embedding."""
    embedding: list[float] = json.loads(embedding_json)
    result = await neo4j_svc.search_similar_patterns(
        query_embedding=embedding, database_id=database_id,
        top_k=top_k, min_similarity=min_similarity,
    )
    return json.dumps(result, default=str)


@mcp.tool()
async def store_pattern(
    database_id:      str,
    nl_question:      str,
    sql:              str,
    schema_cypher:    str,
    tables_used_json: str,
    execution_ms:     int,
    embedding_json:   str,
) -> str:
    """Persist a successful NL→SQL exchange as a (:QueryPattern) node."""
    try:
        tables_used: list[str]   = json.loads(tables_used_json)
        embedding:   list[float] = json.loads(embedding_json)
        await neo4j_svc.store_query_pattern(
            database_id=database_id, nl_question=nl_question,
            sql=sql, schema_cypher=schema_cypher,
            tables_used=tables_used, execution_ms=execution_ms,
            embedding=embedding,
        )
        return json.dumps({"stored": True})
    except Exception as exc:
        return json.dumps({"stored": False, "error": str(exc)})


@mcp.tool()
async def get_schema_summary() -> str:
    """All databases with enriched tables and business domains."""
    result = await neo4j_svc.get_schema_summary()
    return json.dumps(result, default=str)


@mcp.tool()
async def record_feedback(
    nl_question:   str,
    database_id:   str,
    action:        str,
    corrected_sql: str = "",
) -> str:
    """
    Update a QueryPattern weight based on user feedback.
    action: "increment" | "decrement" | "correct"
    """
    try:
        updated = False
        if action == "increment":
            updated = await neo4j_svc.increment_pattern_success(nl_question, database_id)
        elif action == "decrement":
            updated = await neo4j_svc.decrement_pattern_success(nl_question, database_id)
        elif action == "correct" and corrected_sql.strip():
            updated = await neo4j_svc.update_pattern_sql(
                nl_question, database_id, corrected_sql.strip()
            )
        return json.dumps({"updated": updated, "action": action})
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
