"""
mcp_servers/neo4j_mcp/server.py  (Phase 4A — adds get_join_paths_batch)
                                  (+ Streamable HTTP / stateless migration)

Phase 4A change: new get_join_paths_batch tool that finds FK join paths
between ALL pairs of candidate tables in a single Cypher query, replacing
the N−1 sequential get_join_path loop in the query pipeline.

Streamable HTTP migration — same rationale as mcp_servers/oracle_mcp/server.py:
  • Built on the official SDK's `mcp.server.fastmcp.FastMCP` rather than the
    third-party `fastmcp` package, for a stable, documented host/port/
    stateless_http/json_response API.
  • stateless_http=True removes the in-memory session pinning that SSE
    required, so this server can sit behind a plain round-robin load
    balancer with multiple replicas — no sticky sessions, no shared
    session store.
  • Endpoint moves from /sse to /mcp.

All tool behavior is unchanged from the SSE version.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from mcp.server.fastmcp import FastMCP
import backend.services.neo4j_service as neo4j_svc

mcp = FastMCP(
    name           = "neo4j-mcp-server",
    stateless_http = True,
    json_response  = True,
)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: semantic_search
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def semantic_search(
    embedding_json: str,
    database_id:    str,
    top_k:          int = 12,
) -> str:
    """
    Vector cosine-similarity search on (:Table) and (:Column) nodes
    scoped to one database.

    Args:
        embedding_json: JSON-serialized list[float] — 3072-dim question embedding.
        database_id:    Database identifier (e.g. "fincore").
        top_k:          Number of nearest neighbours to return per index.

    Returns JSON: {tables, columns, cypher_used}
    """
    embedding: list[float] = json.loads(embedding_json)
    result = await neo4j_svc.semantic_schema_search(
        query_embedding=embedding,
        database_id=database_id,
        top_k=top_k,
    )
    return json.dumps(result, default=str)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: get_table_details
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def get_table_details(table_names_json: str, database_id: str) -> str:
    """
    Retrieve full column metadata for a list of tables from the graph.

    Args:
        table_names_json: JSON-serialized list[str] of table names.
        database_id:      Database identifier.

    Returns JSON: list of table objects with columns.
    """
    table_names: list[str] = json.loads(table_names_json)
    result = await neo4j_svc.get_table_details(table_names, database_id)
    return json.dumps(result, default=str)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: get_join_path  (single pair — kept for backward compat)
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def get_join_path(table1: str, table2: str, database_id: str) -> str:
    """
    Find the shortest FK-based join path between two tables.

    Args:
        table1:      Source table name.
        table2:      Target table name.
        database_id: Database identifier.

    Returns JSON: list of path objects (empty list = no path found).
    """
    result = await neo4j_svc.get_join_path(table1, table2, database_id)
    return json.dumps(result, default=str)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: get_join_paths_batch  (Phase 4A)
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def get_join_paths_batch(table_names_json: str, database_id: str) -> str:
    """
    Find shortest FK join paths between ALL pairs of candidate tables in a
    single Cypher query.

    Replaces the previous O(N) sequential get_join_path loop in the
    query pipeline. For N candidate tables, this issues one Neo4j query
    instead of N−1.

    Args:
        table_names_json: JSON-serialized list[str] of all candidate table names.
        database_id:      Database identifier.

    Returns JSON: flat list of path objects, one per reachable pair:
        [
          {
            "from_table":      "LOAN_MASTER",
            "to_table":        "BRANCH_MASTER",
            "table_sequence":  ["LOAN_MASTER", "BRANCH_MASTER"],
            "join_conditions": [{"from_col": "BRCH_CD", "to_col": "BRCH_CD"}]
          }, ...
        ]
    """
    table_names: list[str] = json.loads(table_names_json)
    result = await neo4j_svc.get_join_paths_batch(table_names, database_id)
    return json.dumps(result, default=str)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: get_cross_db_hints
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def get_cross_db_hints(table_names_json: str, database_id: str) -> str:
    """
    Return cross-database CROSS_DB_JOIN edges for candidate tables.

    Args:
        table_names_json: JSON-serialized list[str] of table names.
        database_id:      Source database identifier.

    Returns JSON: list of cross-DB link objects.
    """
    table_names: list[str] = json.loads(table_names_json)
    result = await neo4j_svc.get_cross_db_hints(table_names, database_id)
    return json.dumps(result, default=str)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: search_patterns
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def search_patterns(
    embedding_json: str,
    database_id:    str,
    top_k:          int   = 3,
    min_similarity: float = 0.85,
) -> str:
    """
    Find past QueryPattern nodes semantically similar to the current question.

    Args:
        embedding_json:  JSON-serialized list[float] question embedding.
        database_id:     Database identifier.
        top_k:           Max patterns to return.
        min_similarity:  Cosine similarity threshold (default 0.85).

    Returns JSON: list of matched pattern objects.
    """
    embedding: list[float] = json.loads(embedding_json)
    result = await neo4j_svc.search_similar_patterns(
        query_embedding=embedding,
        database_id=database_id,
        top_k=top_k,
        min_similarity=min_similarity,
    )
    return json.dumps(result, default=str)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: store_pattern
# ══════════════════════════════════════════════════════════════════════════════

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
    """
    Persist a successful NL→SQL exchange as a (:QueryPattern) node.

    Args:
        database_id:       Database identifier.
        nl_question:       Original natural language question.
        sql:               Executed Oracle SQL.
        schema_cypher:     Cypher queries used for schema discovery.
        tables_used_json:  JSON-serialized list[str] of table names.
        execution_ms:      Execution time in milliseconds.
        embedding_json:    JSON-serialized list[float] question embedding.

    Returns JSON: {"stored": true}
    """
    try:
        tables_used: list[str]   = json.loads(tables_used_json)
        embedding:   list[float] = json.loads(embedding_json)
        await neo4j_svc.store_query_pattern(
            database_id   = database_id,
            nl_question   = nl_question,
            sql           = sql,
            schema_cypher = schema_cypher,
            tables_used   = tables_used,
            execution_ms  = execution_ms,
            embedding     = embedding,
        )
        return json.dumps({"stored": True})
    except Exception as exc:
        return json.dumps({"stored": False, "error": str(exc)})


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: get_schema_summary
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def get_schema_summary() -> str:
    """
    Return all databases with their enriched tables and business domains.

    Returns JSON: {databases: [{id, name, description, table_count,
                                tables, domains}]}
    """
    result = await neo4j_svc.get_schema_summary()
    return json.dumps(result, default=str)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: record_feedback
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def record_feedback(
    nl_question:   str,
    database_id:   str,
    action:        str,
    corrected_sql: str = "",
) -> str:
    """
    Update a QueryPattern weight based on user feedback.

    action="increment"  → success_count + 1
    action="decrement"  → success_count - 1 (floor 0)
    action="correct"    → replace stored SQL, success_count + 2

    Returns JSON: {"updated": bool, "action": str}
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

    print(f"[Neo4j MCP] Starting on {args.host}:{args.port} "
          f"(transport=streamable-http, stateless_http=True)")
    print(f"[Neo4j MCP] MCP endpoint: http://{args.host}:{args.port}/mcp")

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.settings.stateless_http = True
    mcp.run(transport="streamable-http")
