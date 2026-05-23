"""
mcp_servers/neo4j_mcp/server.py

Neo4j MCP Server — Phase 3B
Exposes the enriched Neo4j schema graph as MCP tools callable by any
MCP-compatible client (FastAPI backend, future supervisor agents,
Claude Desktop, etc.).

Transport: SSE  (default port 8002)
All tools return JSON strings — the MCP client deserializes on its end.

Tools
─────
semantic_search       Vector similarity search on Tables + Columns
get_table_details     Full column metadata for named tables
get_join_path         Shortest FK-path between two tables
get_cross_db_hints    Cross-database CROSS_DB_JOIN edges for candidate tables
search_patterns       Find past QueryPatterns similar to a question embedding
store_pattern         Persist a successful NL→SQL as a QueryPattern node
get_schema_summary    Full database + table + domain listing for the UI
record_feedback       Increment / decrement / correct a QueryPattern

Usage
─────
    python -m mcp_servers.neo4j_mcp.server
"""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

# from mcp.server.fastmcp import FastMCP

from fastmcp import FastMCP

import backend.services.neo4j_service as neo4j_svc

mcp = FastMCP(
    name        = "neo4j-mcp-server",
    # description = (
    #     "Banking schema graph gateway — exposes vector-based schema discovery, "
    #     "FK join-path traversal, cross-database link hints, and the "
    #     "self-improving QueryPattern store/retrieve loop."
    # ),
)


# ── asyncio helper (FastMCP tools are sync; neo4j_svc is async) ───────────────

# def _run(coro):
#     """Run an async coroutine from a synchronous FastMCP tool handler."""
#     try:
#         loop = asyncio.get_event_loop()
#         if loop.is_running():
#             import concurrent.futures
#             with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
#                 future = pool.submit(asyncio.run, coro)
#                 return future.result()
#         return loop.run_until_complete(coro)
#     except RuntimeError:
#         return asyncio.run(coro)
    
def _run(coro):
    """Run an async coroutine from a synchronous FastMCP tool handler."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — safe to use asyncio.run()
        return asyncio.run(coro)
    
    # If we get here, a loop is running in this thread
    # This shouldn't happen with FastMCP, but handle it just in case
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


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

    Used as the GraphRAG schema-discovery step in the query pipeline.
    Returns the Cypher queries used so they can be stored in QueryPattern
    nodes for future audit and reuse.

    Args:
        embedding_json: JSON-serialized list[float] — 3072-dim
                        gemini-embedding-001 vector of the user question.
        database_id:    Database identifier (e.g. "fincore").
        top_k:          Number of nearest neighbours to return per index.

    Returns:
        JSON string:
        {
          "tables":      [ {table_name, description, is_view, row_count_approx,
                            score}, … ],
          "columns":     [ {table_name, column_name, description, is_pk,
                            cardinality_hint, score}, … ],
          "cypher_used": "<table search Cypher>\\n\\n<column search Cypher>"
        }
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
def get_table_details(table_names_json: str, database_id: str) -> str:
    """
    Retrieve full column metadata for a list of tables from the graph.

    Called after semantic_search to build the schema context string
    sent to Gemini for SQL generation.

    Args:
        table_names_json: JSON-serialized list[str] of table names.
        database_id:      Database identifier.

    Returns:
        JSON string — list of table objects:
        [
          {
            "table_name":        "LOAN_MASTER",
            "schema_name":       "FINCORE",
            "table_description": "…",
            "is_view":           false,
            "row_count_approx":  1500000,
            "pk_columns":        ["LOAN_ACCT_NO"],
            "domain_name":       "Lending",
            "columns": [
              {
                "name":             "LOAN_ACCT_NO",
                "data_type":        "VARCHAR2",
                "label":            "Loan Account Number",
                "description":      "…",
                "is_pk":            true,
                "is_unique":        true,
                "is_indexed":       true,
                "is_pii":           false,
                "cardinality_hint": "unique"
              }, …
            ]
          }, …
        ]
    """
    table_names: list[str] = json.loads(table_names_json)
    result = _run(neo4j_svc.get_table_details(table_names, database_id))
    return json.dumps(result, default=str)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: get_join_path
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_join_path(table1: str, table2: str, database_id: str) -> str:
    """
    Find the shortest FK-based join path between two tables using
    Neo4j shortestPath over (:Table)-[:FK_TO*1..5]->(:Table) edges.

    Args:
        table1:      Source table name.
        table2:      Target table name.
        database_id: Database identifier.

    Returns:
        JSON string — list of path objects (empty list = no path found):
        [
          {
            "table_sequence":  ["LOAN_MASTER", "BRANCH_MASTER"],
            "join_conditions": [ {"from_col": "BRCH_CD", "to_col": "BRCH_CD"} ]
          }
        ]
    """
    result = _run(neo4j_svc.get_join_path(table1, table2, database_id))
    return json.dumps(result, default=str)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: get_cross_db_hints
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_cross_db_hints(table_names_json: str, database_id: str) -> str:
    """
    Return cross-database CROSS_DB_JOIN edges for the candidate tables.

    These are advisory hints surfaced in the schema context to inform
    Gemini of known logical relationships that span multiple Oracle
    instances (e.g. LOAN_MASTER in fincore ↔ NPA_MASTER in riskdb).

    Args:
        table_names_json: JSON-serialized list[str] of table names.
        database_id:      Source database identifier.

    Returns:
        JSON string — list of cross-DB link objects:
        [
          {
            "from_table": "LOAN_MASTER",  "from_db": "fincore",
            "from_col":   "LOAN_ACCT_NO",
            "to_table":   "NPA_MASTER",   "to_db":   "riskdb",
            "to_col":     "LOAN_ACCT_NO",
            "description":"Loans classified as NPA in risk system"
          }, …
        ]
    """
    table_names: list[str] = json.loads(table_names_json)
    result = _run(neo4j_svc.get_cross_db_hints(table_names, database_id))
    return json.dumps(result, default=str)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: search_patterns
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def search_patterns(
    embedding_json: str,
    database_id:    str,
    top_k:          int   = 3,
    min_similarity: float = 0.85,
) -> str:
    """
    Find past QueryPattern nodes semantically similar to the current question.

    Returns stored SQL AND the schema_cypher that was used — so the pipeline
    reuses both the few-shot SQL example and the schema discovery Cypher.

    Args:
        embedding_json:  JSON-serialized list[float] — question embedding.
        database_id:     Database identifier.
        top_k:           Max patterns to return.
        min_similarity:  Cosine similarity threshold (0–1, default 0.85).

    Returns:
        JSON string — list of matched pattern objects:
        [
          {
            "nl_question":  "Show total loan disbursements by branch…",
            "sql":          "WITH qtr_disb AS (…) SELECT …",
            "schema_cypher":"CALL db.index.vector.queryNodes(…)",
            "tables_used":  ["LOAN_MASTER", "BRANCH_MASTER"],
            "success_count": 14,
            "score":         0.923
          }, …
        ]
    """
    embedding: list[float] = json.loads(embedding_json)
    result = _run(neo4j_svc.search_similar_patterns(
        query_embedding=embedding,
        database_id=database_id,
        top_k=top_k,
        min_similarity=min_similarity,
    ))
    return json.dumps(result, default=str)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: store_pattern
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def store_pattern(
    database_id:    str,
    nl_question:    str,
    sql:            str,
    schema_cypher:  str,
    tables_used_json: str,
    execution_ms:   int,
    embedding_json: str,
) -> str:
    """
    Persist a successful NL→SQL exchange as a (:QueryPattern) node.

    Called in the background after every successful query execution.
    Duplicate questions (same nl_question + database_id) increment
    success_count and update the stored SQL and schema_cypher to the
    latest successful version.

    Args:
        database_id:       Database identifier.
        nl_question:       Original natural language question.
        sql:               Executed Oracle SQL.
        schema_cypher:     All Cypher queries used for schema discovery
                           this request (preserved for future reuse).
        tables_used_json:  JSON-serialized list[str] of table names.
        execution_ms:      Execution time in milliseconds.
        embedding_json:    JSON-serialized list[float] — question embedding.

    Returns:
        JSON string:  {"stored": true}
    """
    try:
        tables_used: list[str]  = json.loads(tables_used_json)
        embedding:   list[float] = json.loads(embedding_json)
        _run(neo4j_svc.store_query_pattern(
            database_id  = database_id,
            nl_question  = nl_question,
            sql          = sql,
            schema_cypher= schema_cypher,
            tables_used  = tables_used,
            execution_ms = execution_ms,
            embedding    = embedding,
        ))
        return json.dumps({"stored": True})
    except Exception as exc:
        return json.dumps({"stored": False, "error": str(exc)})


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: get_schema_summary
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_schema_summary() -> str:
    """
    Return all databases with their enriched tables and business domains.

    Used by the Streamlit schema explorer sidebar and the /api/schema
    endpoint. Data comes entirely from Neo4j — no Oracle queries required
    at UI load time.

    Returns:
        JSON string:
        {
          "databases": [
            {
              "id":          "fincore",
              "name":        "Core Banking",
              "description": "…",
              "table_count": 42,
              "tables":  [ {name, description, is_view, row_count, domain} ],
              "domains": [ {name, hint} ]
            }, …
          ]
        }
    """
    result = _run(neo4j_svc.get_schema_summary())
    return json.dumps(result, default=str)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: record_feedback
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def record_feedback(
    nl_question:   str,
    database_id:   str,
    action:        str,
    corrected_sql: str = "",
) -> str:
    """
    Update a QueryPattern's weight based on user feedback.

    action="increment"  thumbs-up  → success_count + 1
    action="decrement"  thumbs-down → success_count - 1 (floor 0)
    action="correct"    user supplied correct SQL → replace stored SQL,
                        success_count + 2

    Patterns with higher success_count rank higher in future
    semantic searches and are preferred as few-shot examples.

    Args:
        nl_question:   The original NL question tied to the pattern.
        database_id:   Database identifier.
        action:        "increment" | "decrement" | "correct".
        corrected_sql: Required when action="correct".

    Returns:
        JSON string:  {"updated": true | false, "action": "<action>"}
    """
    try:
        updated = False
        if action == "increment":
            updated = _run(neo4j_svc.increment_pattern_success(nl_question, database_id))
        elif action == "decrement":
            updated = _run(neo4j_svc.decrement_pattern_success(nl_question, database_id))
        elif action == "correct" and corrected_sql.strip():
            updated = _run(neo4j_svc.update_pattern_sql(
                nl_question, database_id, corrected_sql.strip()
            ))
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
    mcp.run(transport="sse", host=args.host, port=args.port)
