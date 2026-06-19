"""
mcp_servers/oracle_mcp/server.py

Oracle MCP Server — Streamable HTTP, stateless, with /health probe.

CHANGES vs previous version
────────────────────────────
  1. @mcp.custom_route("/health", methods=["GET"])
     Returns {status, db_pool_stats, registered_databases} as JSON.
     This is the endpoint HAProxy / nginx / k8s liveness and readiness
     probes must point at — it is intentionally unauthenticated (the
     official FastMCP docs explicitly state that custom_route is designed
     for operational endpoints that sit outside the MCP auth boundary).

  2. @mcp.custom_route("/ready", methods=["GET"])
     Deeper check: actually acquires a connection from each Oracle pool
     and pings it. Returns HTTP 503 if any configured database is
     unreachable, so a load balancer can remove this replica from
     rotation until the database recovers.  HAProxy uses /health for
     fast liveness (TCP-level) and /ready for the slower readiness check.

  3. All tool logic is unchanged from the previous version.

Transport : Streamable HTTP, stateless_http=True
MCP path  : /mcp
Health    : /health  (GET, unauthenticated)
Readiness : /ready   (GET, unauthenticated)
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

from backend.db_manager import db_manager
from backend.services import oracle_service

# ── Server instance ────────────────────────────────────────────────────────────
mcp = FastMCP(
    name           = "oracle-mcp-server",
    stateless_http = True,
    json_response  = True,
)

_SERVER_START = time.monotonic()


# ══════════════════════════════════════════════════════════════════════════════
# OPERATIONAL ROUTES  (unauthenticated — for load balancer probes)
# ══════════════════════════════════════════════════════════════════════════════

@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """
    Liveness probe — fast, no Oracle connection required.
    Returns 200 as long as the process is running and db config is loaded.
    """
    dbs = [
        {
            "id":          d.id,
            "name":        d.name,
            "configured":  d.is_configured,
            "pool_min":    d.effective_pool_min,
            "pool_max":    d.effective_pool_max,
        }
        for d in db_manager.databases
    ]
    return JSONResponse({
        "status":     "healthy",
        "service":    "oracle-mcp-server",
        "uptime_s":   round(time.monotonic() - _SERVER_START, 1),
        "databases":  dbs,
    })


@mcp.custom_route("/ready", methods=["GET"])
async def ready(request: Request) -> JSONResponse:
    """
    Readiness probe — acquires a real Oracle connection per configured DB.
    Returns 503 if any database is unreachable so the load balancer can
    remove this replica from rotation until it recovers.
    """
    results: list[dict] = []
    all_ok = True

    for cfg in db_manager.databases:
        if not cfg.is_configured:
            results.append({"id": cfg.id, "ok": False, "error": "credentials not set"})
            all_ok = False
            continue
        try:
            pool = db_manager.get_pool(cfg.id)
            with pool.acquire() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM DUAL")
                    cur.fetchone()
            results.append({"id": cfg.id, "ok": True})
        except Exception as exc:
            results.append({"id": cfg.id, "ok": False, "error": str(exc)})
            all_ok = False

    status_code = 200 if all_ok else 503
    return JSONResponse(
        {"status": "ready" if all_ok else "not_ready", "databases": results},
        status_code=status_code,
    )


# ══════════════════════════════════════════════════════════════════════════════
# TOOLS
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
        db_id:    Database identifier (e.g. "fincore", "riskdb").
        sql:      Oracle SQL SELECT statement. Schema-qualified table names
                  recommended (SCHEMA.TABLE_NAME).
        max_rows: Maximum rows to return (default 1000).

    Returns:
        JSON string with keys: columns, rows, row_count, sql_executed, pii_warnings.
    """
    try:
        result = oracle_service.execute_sql(db_id, sql, max_rows)
        return json.dumps(result, default=str)
    except ValueError as exc:
        return json.dumps({"error": f"SQL safety check failed: {exc}"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@mcp.tool()
def explain_plan(db_id: str, sql: str) -> str:
    """
    Run Oracle EXPLAIN PLAN FOR <sql> and return cost metrics.
    Does NOT execute the query.

    Returns JSON: {cost, has_full_scan, has_cartesian, plan_text}
    """
    try:
        pool = db_manager.get_pool(db_id)
        with pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(f"EXPLAIN PLAN FOR {sql}")
                cur.execute("""
                    SELECT plan_table_output
                    FROM   TABLE(DBMS_XPLAN.DISPLAY('PLAN_TABLE', NULL, 'BASIC +COST +ROWS'))
                """)
                rows      = cur.fetchall()
                plan_text = "\n".join(r[0] for r in rows if r[0])

                cost: int | None = None
                cur.execute("""
                    SELECT NVL(cost, 0) FROM plan_table
                    WHERE id = 0 ORDER BY timestamp DESC FETCH FIRST 1 ROWS ONLY
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


@mcp.tool()
def get_schema(db_id: str, schema_name: str = "") -> str:
    """
    Pull enriched data-dictionary metadata from Oracle ALL_* views.
    Never reads actual business data.

    Returns JSON: {columns, foreign_keys, indexes, pk_map, view_names, row_counts}
    """
    try:
        result = oracle_service.get_data_dictionary(db_id, schema=schema_name or None)
        return json.dumps(result, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@mcp.tool()
def list_databases() -> str:
    """
    List all Oracle databases registered in databases.yaml.

    Returns JSON: [{id, name, description, schema, configured}, …]
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


@mcp.tool()
def check_read_only(sql: str) -> str:
    """
    Validate that SQL contains no DML / DDL keywords.
    No database connection required.

    Returns JSON: {valid, forbidden_keywords}
    """
    import re
    FORBIDDEN = {
        "INSERT","UPDATE","DELETE","DROP","CREATE","ALTER","TRUNCATE",
        "MERGE","GRANT","REVOKE","EXECUTE","EXEC","CALL",
        "COMMIT","ROLLBACK","SAVEPOINT","BEGIN","END",
    }
    cleaned = re.sub(r"'[^']*'", "''", sql)
    found   = FORBIDDEN & set(re.split(r"\W+", cleaned.upper()))
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
    print(f"[Oracle MCP]   MCP endpoint : http://{args.host}:{args.port}/mcp")
    print(f"[Oracle MCP]   Health probe : http://{args.host}:{args.port}/health")
    print(f"[Oracle MCP]   Ready probe  : http://{args.host}:{args.port}/ready")
    print(f"[Oracle MCP]   Databases    : {[d.id for d in db_manager.databases]}")

    mcp.run(transport="streamable-http", host=args.host, port=args.port)
