"""
mcp_servers/oracle_mcp/server.py

Oracle MCP Server — Phase 3B
Exposes Oracle database capabilities as MCP tools callable by any
MCP-compatible client (FastAPI backend, future supervisor agents,
Claude Desktop, etc.).

Transport: SSE  (default port 8001)
All tools return JSON strings — the MCP client deserializes on its end.

Tools
─────
execute_query      Run a validated read-only SQL query and return results
explain_plan       Return EXPLAIN PLAN cost + full-scan / cartesian flags
get_schema         Pull enriched data dictionary for a database / schema
list_databases     List all registered databases and their config status
check_read_only    Validate that SQL contains no DML / DDL keywords

Usage
─────
    # standalone
    python -m mcp_servers.oracle_mcp.server

    # or via start_servers.sh
"""

import json
import sys
from pathlib import Path

# ── Project root on path so backend.* imports work ─────────────────────────────
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

# fastmcp>=0.2.0from mcp.server.fastmcp import FastMCP

from fastmcp import FastMCP

from backend.db_manager import db_manager
from backend.services import oracle_service

# ── Server instance ────────────────────────────────────────────────────────────
mcp = FastMCP(
    name        = "oracle-mcp-server",
    # description = (
    #     "Banking Oracle DB gateway — exposes read-only query execution, "
    #     "EXPLAIN PLAN cost estimation, and enriched schema metadata. "
    #     "All tools are data-privacy safe: no raw row data is returned "
    #     "beyond what the authenticated user explicitly queries."
    # ),
)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: execute_query
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def execute_query(db_id: str, sql: str, max_rows: int = 1000) -> str:
    """
    Execute a validated read-only SQL query against the specified Oracle database.

    Safety layers applied automatically:
      • Forbidden-keyword guard (INSERT / UPDATE / DELETE / DROP / …)
      • PII column detection and automatic SQL-level masking
      • FETCH FIRST {max_rows} ROWS ONLY injected when absent

    Args:
        db_id:    Database identifier matching an entry in databases.yaml
                  (e.g. "fincore", "riskdb").
        sql:      Oracle SQL SELECT statement. Schema-qualified table names
                  recommended (SCHEMA.TABLE_NAME).
        max_rows: Maximum rows to return (default 1000, hard cap).

    Returns:
        JSON string:
        {
          "columns":      ["COL1", "COL2", …],
          "rows":         [[val, …], …],
          "row_count":    <int>,
          "sql_executed": "<final SQL with limit and masking applied>",
          "pii_warnings": ["PII column 'X' masked", …]
        }

    Raises:
        Returns {"error": "<message>"} on Oracle or safety failures.
    """
    try:
        result = oracle_service.execute_sql(db_id, sql, max_rows)
        return json.dumps(result, default=str)
    except ValueError as exc:
        # Safety guard rejection
        return json.dumps({"error": f"SQL safety check failed: {exc}"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: explain_plan
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def explain_plan(db_id: str, sql: str) -> str:
    """
    Run Oracle EXPLAIN PLAN FOR <sql> and return cost metrics.

    Does NOT execute the query — only the optimizer plan is generated.
    Used by ValidationAgent (Phase 3A) to gate expensive queries before
    execution.

    Args:
        db_id: Database identifier.
        sql:   Oracle SQL to explain (SELECT statements only).

    Returns:
        JSON string:
        {
          "cost":           <int | null>,    /* root-node optimizer cost */
          "has_full_scan":  <bool>,          /* TABLE ACCESS FULL present */
          "has_cartesian":  <bool>,          /* CARTESIAN join present */
          "plan_text":      "<formatted>"    /* full DBMS_XPLAN output */
        }
    """
    try:
        pool = db_manager.get_pool(db_id)
        with pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(f"EXPLAIN PLAN FOR {sql}")
                cur.execute("""
                    SELECT plan_table_output
                    FROM   TABLE(
                               DBMS_XPLAN.DISPLAY('PLAN_TABLE', NULL, 'BASIC +COST +ROWS')
                           )
                """)
                rows      = cur.fetchall()
                plan_text = "\n".join(r[0] for r in rows if r[0])

                cost: int | None = None
                cur.execute("""
                    SELECT NVL(cost, 0)
                    FROM   plan_table
                    WHERE  id = 0
                    ORDER  BY timestamp DESC
                    FETCH  FIRST 1 ROWS ONLY
                """)
                cost_row = cur.fetchone()
                if cost_row and cost_row[0] is not None:
                    try:
                        cost = int(cost_row[0])
                    except (TypeError, ValueError):
                        pass

                cur.execute("DELETE FROM plan_table")
                conn.commit()

        return json.dumps({
            "cost":          cost,
            "has_full_scan": "TABLE ACCESS FULL" in plan_text.upper(),
            "has_cartesian": "CARTESIAN"          in plan_text.upper(),
            "plan_text":     plan_text,
        })
    except Exception as exc:
        return json.dumps({"error": str(exc), "cost": None,
                           "has_full_scan": False, "has_cartesian": False,
                           "plan_text": ""})


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: get_schema
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_schema(db_id: str, schema_name: str = "") -> str:
    """
    Pull enriched data-dictionary metadata from Oracle ALL_* views.

    Never reads actual business data — only structural metadata
    (table names, column names, data types, PKs, FKs, indexes,
    optimizer row counts, view flags).

    Args:
        db_id:       Database identifier.
        schema_name: Oracle schema name override (e.g. "FINCORE").
                     Leave blank to use the value from databases.yaml.

    Returns:
        JSON string:
        {
          "columns":      [ {owner, table_name, column_name, data_type,
                             is_pk, is_unique, is_indexed, is_view,
                             row_count, cardinality_hint, col_comment,
                             table_comment}, … ],
          "foreign_keys": [ {table_name, column_name, ref_table,
                             ref_column}, … ],
          "indexes":      [ {table_name, index_name, idx_cols,
                             uniqueness, index_type}, … ],
          "pk_map":       { "TABLE_NAME": ["COL1", …], … },
          "view_names":   ["VIEW1", …],
          "row_counts":   { "TABLE_NAME": <int>, … }
        }
    """
    try:
        result = oracle_service.get_data_dictionary(
            db_id, schema=schema_name or None
        )
        return json.dumps(result, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: list_databases
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def list_databases() -> str:
    """
    List all Oracle databases registered in databases.yaml.

    Returns:
        JSON string:
        [
          {
            "id":          "fincore",
            "name":        "Core Banking",
            "description": "…",
            "schema":      "FINCORE",
            "configured":  true          /* true if credentials set in .env */
          },
          …
        ]
    """
    dbs = [
        {
            "id":          d.id,
            "name":        d.name,
            "description": d.description,
            "schema":      d.qualified_schema,
            "configured":  d.is_configured,
        }
        for d in db_manager.databases
    ]
    return json.dumps(dbs)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: check_read_only
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def check_read_only(sql: str) -> str:
    """
    Validate that SQL contains no DML / DDL keywords before execution.

    Lightweight check — no database connection required.
    Used as a fast pre-flight gate before sending SQL to Oracle.

    Args:
        sql: Oracle SQL string to inspect.

    Returns:
        JSON string:
        {
          "valid":             true | false,
          "forbidden_keywords": ["DELETE", …]   /* empty if valid */
        }
    """
    import re
    FORBIDDEN = {
        "INSERT","UPDATE","DELETE","DROP","CREATE","ALTER","TRUNCATE",
        "MERGE","GRANT","REVOKE","EXECUTE","EXEC","CALL",
        "COMMIT","ROLLBACK","SAVEPOINT","BEGIN","END",
    }
    cleaned  = re.sub(r"'[^']*'", "''", sql)
    found    = FORBIDDEN & set(re.split(r"\W+", cleaned.upper()))
    return json.dumps({
        "valid":              len(found) == 0,
        "forbidden_keywords": sorted(found),
    })


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Oracle MCP Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    print(f"[Oracle MCP] Starting on {args.host}:{args.port}")
    print(f"[Oracle MCP] Registered databases: "
          f"{[d.id for d in db_manager.databases]}")

    mcp.run(transport="sse", host=args.host, port=args.port)