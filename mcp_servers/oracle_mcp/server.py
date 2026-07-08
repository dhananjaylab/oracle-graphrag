"""
mcp_servers/oracle_mcp/server.py

Oracle MCP Server — Streamable HTTP, stateless, with /health probe.

CHANGES vs previous version (bottleneck fixes)
────────────────────────────────────────────────
  1. EVENT-LOOP BLOCKING FIX (highest priority):
     Every tool that touches Oracle is now `async def` and wraps the
     blocking `oracledb` calls in `asyncio.to_thread()`. Previously these
     were plain `def` tools — the official mcp.server.fastmcp SDK does
     NOT auto-dispatch synchronous tool functions to a thread pool the
     way the third-party `fastmcp` package does (this is a known,
     currently-open SDK behavior, not a bug in this codebase); a `def`
     tool doing blocking I/O runs directly on the event loop and blocks
     EVERY other concurrent request this process is handling — including
     unrelated execute_query calls from other clients and the /health
     and /ready probes HAProxy depends on to know this replica is alive.
     This one change is what actually lets ORACLE_POOL_MAX concurrent
     connections get used concurrently, instead of serializing behind
     the event loop one call at a time.

  2. /ready NO LONGER BLOCKS THE EVENT LOOP:
     Was `async def` wrapping fully synchronous, blocking Oracle calls,
     run sequentially per configured database. Now each database's probe
     is wrapped in asyncio.to_thread() and all databases are checked
     concurrently via asyncio.gather(), so readiness polling no longer
     stalls in-flight tool calls on this replica.

  3. INPUT VALIDATION VIA Annotated[..., Field(...)]:
     Every tool parameter now carries Field()-level constraints (min/max
     length, numeric bounds) instead of being an unconstrained str/int.
     IMPORTANT: this uses individual Annotated parameters, NOT a single
     wrapping Pydantic BaseModel — that alternative was tried first and
     rejected after empirical verification: a single BaseModel parameter
     serializes as {"params": {...}} in the tool's wire schema (verified
     directly against the installed mcp 1.28.1 SDK), which would have
     required every call site in backend/mcp_client/oracle_client.py to
     change from flat kwargs to a nested "params" dict. Annotated
     individual parameters, by contrast, verified to flatten into the
     tool's top-level argument schema exactly as before — so this adds
     real server-side validation with zero client-side changes required.

  4. TOOL ANNOTATIONS:
     All five tools are read-only against Oracle, so all declare
     readOnlyHint=True, openWorldHint=True (they talk to an external
     system). check_read_only and list_databases are also idempotent
     (repeated calls with the same args have no additional effect on
     the world), so they additionally declare idempotentHint=True.

  5. PER-TOOL TIMEOUTS:
     execute_query, explain_plan, and get_schema wrap their Oracle work
     in asyncio.wait_for() with a generous ceiling (large queries can
     legitimately take tens of seconds); check_read_only and
     list_databases are pure in-memory operations with no timeout needed.
     NOTE: @mcp.tool() in the installed SDK (mcp 1.28.x) has no timeout=
     parameter — checked directly against the installed package and
     confirmed absent — so per-tool timeout is enforced manually inside
     each tool body via asyncio.wait_for() rather than via the decorator.
     The existing client-side MCP_TOOL_TIMEOUT_S in
     backend/mcp_client/pool.py still applies as an outer backstop.

  6. PAGINATION ON get_schema:
     get_schema previously pulled the ENTIRE Oracle data dictionary for
     a schema unconditionally — every table, column, index, and FK, with
     no bound. Added an optional table_names filter (mirrors what
     get_table_details already does on the Neo4j side) plus limit/offset
     over the table list, following the standard pagination shape
     (has_more, next_offset, total_count) so a caller can request a
     manageable slice instead of the whole dictionary. table_names and
     limit/offset are new OPTIONAL parameters with defaults, so existing
     callers that only pass db_id/sql/schema_name are unaffected.

Transport : Streamable HTTP, stateless_http=True
MCP path  : /mcp
Health    : /health  (GET, unauthenticated)
Readiness : /ready   (GET, unauthenticated)
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Annotated, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from mcp.server.fastmcp import FastMCP
from pydantic import Field
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

# ── Per-tool timeout tuning ──────────────────────────────────────────────────
# Oracle round trips — execute_query / explain_plan / get_schema — can
# legitimately run for tens of seconds on a large banking query. Enforced
# manually via asyncio.wait_for() inside each tool body (see note above:
# the installed SDK's @mcp.tool() has no timeout= parameter).
# check_read_only and list_databases are pure in-memory operations with
# no external I/O, so they need no explicit timeout.
_TIMEOUT_ORACLE = 90.0


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


async def _probe_one_db(cfg) -> dict:
    """
    Acquire a connection and run a trivial query for one database.
    Runs the blocking oracledb calls in a worker thread so this coroutine
    never holds the event loop — critical since /ready runs on the same
    process that's also serving concurrent tool calls.
    """
    if not cfg.is_configured:
        return {"id": cfg.id, "ok": False, "error": "credentials not set"}

    def _blocking_probe() -> None:
        pool = db_manager.get_pool(cfg.id)
        with pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM DUAL")
                cur.fetchone()

    try:
        await asyncio.to_thread(_blocking_probe)
        return {"id": cfg.id, "ok": True}
    except Exception as exc:
        return {"id": cfg.id, "ok": False, "error": str(exc)}


@mcp.custom_route("/ready", methods=["GET"])
async def ready(request: Request) -> JSONResponse:
    """
    Readiness probe — acquires a real Oracle connection per configured DB.
    Returns 503 if any database is unreachable so the load balancer can
    remove this replica from rotation until it recovers.

    All per-database probes run concurrently (asyncio.gather) and each
    one is offloaded to a worker thread (asyncio.to_thread), so this
    handler never blocks the event loop — a slow or hanging database no
    longer stalls every other in-flight request this replica is serving.
    """
    results = await asyncio.gather(*(
        _probe_one_db(cfg) for cfg in db_manager.databases
    ))
    all_ok = all(r["ok"] for r in results)

    status_code = 200 if all_ok else 503
    return JSONResponse(
        {"status": "ready" if all_ok else "not_ready", "databases": results},
        status_code=status_code,
    )


# ══════════════════════════════════════════════════════════════════════════════
# TOOLS
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool(
    annotations={
        "title": "Execute Oracle Query",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,   # re-running against a live table can return different rows
        "openWorldHint": True,
    },
)
async def execute_query(
    db_id: Annotated[str, Field(
        min_length=1, max_length=64,
        description="Database identifier from databases.yaml (e.g. 'fincore', 'riskdb')",
    )],
    sql: Annotated[str, Field(
        min_length=1, max_length=20000,
        description="Oracle SELECT statement. Use SCHEMA.TABLE_NAME format, "
                    "qualify all columns with aliases, use FETCH FIRST N ROWS ONLY.",
    )],
    max_rows: Annotated[int, Field(
        ge=1, le=5000,
        description="Maximum rows to return. Use smaller values for exploratory queries.",
    )] = 1000,
) -> str:
    """
    Execute a validated read-only SQL query against the specified Oracle database.

    Safety layers applied automatically:
      • Forbidden-keyword guard (INSERT / UPDATE / DELETE / DROP / …)
      • PII column detection and automatic SQL-level masking
      • FETCH FIRST {max_rows} ROWS ONLY injected when absent

    Returns:
        str: JSON string with keys: columns, rows, row_count, sql_executed, pii_warnings.
        On failure: {"error": "<message>"}.
    """
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(oracle_service.execute_sql, db_id, sql, max_rows),
            timeout=_TIMEOUT_ORACLE,
        )
        return json.dumps(result, default=str)
    except asyncio.TimeoutError:
        return json.dumps({"error": f"execute_query timed out after {_TIMEOUT_ORACLE}s"})
    except ValueError as exc:
        return json.dumps({"error": f"SQL safety check failed: {exc}"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@mcp.tool(
    annotations={
        "title": "Estimate Oracle Query Cost",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def explain_plan(
    db_id: Annotated[str, Field(min_length=1, max_length=64, description="Database identifier")],
    sql: Annotated[str, Field(min_length=1, max_length=20000, description="Oracle SQL to estimate cost for")],
) -> str:
    """
    Run Oracle EXPLAIN PLAN FOR <sql> and return cost metrics.
    Does NOT execute the query.

    Returns:
        str: JSON string {cost, has_full_scan, has_cartesian, plan_text}.
    """
    def _blocking_explain() -> dict:
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

        return {
            "cost":          cost,
            "has_full_scan": "TABLE ACCESS FULL" in plan_text.upper(),
            "has_cartesian": "CARTESIAN"          in plan_text.upper(),
            "plan_text":     plan_text,
        }

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_blocking_explain), timeout=_TIMEOUT_ORACLE,
        )
        return json.dumps(result)
    except asyncio.TimeoutError:
        return json.dumps({"error": f"explain_plan timed out after {_TIMEOUT_ORACLE}s",
                           "cost": None, "has_full_scan": False,
                           "has_cartesian": False, "plan_text": ""})
    except Exception as exc:
        return json.dumps({"error": str(exc), "cost": None,
                           "has_full_scan": False, "has_cartesian": False,
                           "plan_text": ""})


@mcp.tool(
    annotations={
        "title": "Get Oracle Schema Metadata",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_schema(
    db_id: Annotated[str, Field(min_length=1, max_length=64, description="Database identifier")],
    schema_name: Annotated[str, Field(
        max_length=128,
        description="Oracle schema name override (e.g. 'FINCORE'). "
                    "Leave blank to use the databases.yaml default.",
    )] = "",
    table_names: Annotated[list[str], Field(
        max_length=200,
        description="Optional: restrict results to these table names only. "
                    "Leave empty to page over the full schema instead.",
    )] = [],
    limit: Annotated[int, Field(
        ge=1, le=500,
        description="Maximum number of tables to return per call when table_names is empty.",
    )] = 100,
    offset: Annotated[int, Field(
        ge=0,
        description="Number of tables to skip, for paging through a large schema.",
    )] = 0,
) -> str:
    """
    Pull enriched data-dictionary metadata from Oracle ALL_* views.
    Never reads actual business data — only structural metadata.

    If table_names is provided, results are restricted to those tables
    (unbounded — this is expected to be a small, caller-chosen set).
    If table_names is empty, results page over the full schema's table
    list using limit/offset, to avoid returning an unbounded dictionary
    for schemas with very large numbers of tables.

    Returns:
        str: JSON string:
        {
          "columns":      [ {...}, … ]  (only columns belonging to the returned tables),
          "foreign_keys": [ {...}, … ],
          "indexes":      [ {...}, … ],
          "pk_map":       { "TABLE_NAME": ["COL1", …], … },
          "view_names":   ["VIEW1", …],
          "row_counts":   { "TABLE_NAME": <int>, … },
          "total_tables": <int>,
          "returned_tables": <int>,
          "has_more":     <bool>,
          "next_offset":  <int | null>
        }
    """
    try:
        full = await asyncio.wait_for(
            asyncio.to_thread(
                oracle_service.get_data_dictionary,
                db_id,
                schema=schema_name or None,
            ),
            timeout=_TIMEOUT_ORACLE,
        )
    except asyncio.TimeoutError:
        return json.dumps({"error": f"get_schema timed out after {_TIMEOUT_ORACLE}s"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})

    all_table_names = sorted({c["table_name"] for c in full["columns"]})

    if table_names:
        wanted = set(table_names)
        selected_tables = [t for t in all_table_names if t in wanted]
        has_more    = False
        next_offset = None
    else:
        total = len(all_table_names)
        selected_tables = all_table_names[offset: offset + limit]
        has_more    = (offset + len(selected_tables)) < total
        next_offset = (offset + len(selected_tables)) if has_more else None

    selected_set = set(selected_tables)
    result = {
        "columns":         [c for c in full["columns"] if c["table_name"] in selected_set],
        "foreign_keys":    [f for f in full["foreign_keys"]
                             if f["table_name"] in selected_set or f.get("ref_table") in selected_set],
        "indexes":         [i for i in full["indexes"] if i["table_name"] in selected_set],
        "pk_map":          {t: cols for t, cols in full["pk_map"].items() if t in selected_set},
        "view_names":      [v for v in full["view_names"] if v in selected_set],
        "row_counts":      {t: c for t, c in full["row_counts"].items() if t in selected_set},
        "total_tables":    len(all_table_names),
        "returned_tables": len(selected_tables),
        "has_more":        has_more,
        "next_offset":     next_offset,
    }
    return json.dumps(result, default=str)


@mcp.tool(
    annotations={
        "title": "List Registered Oracle Databases",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def list_databases() -> str:
    """
    List all Oracle databases registered in databases.yaml.

    Pure in-memory lookup, no I/O — safe to leave synchronous.

    Returns:
        str: JSON list: [{id, name, description, schema, configured}, …]
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


@mcp.tool(
    annotations={
        "title": "Check SQL Is Read-Only",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def check_read_only(
    sql: Annotated[str, Field(min_length=1, max_length=20000, description="Oracle SQL string to validate")],
) -> str:
    """
    Validate that SQL contains no DML / DDL keywords.
    No database connection required — pure string parsing, safe to leave
    synchronous (no blocking I/O).

    Returns:
        str: JSON {valid, forbidden_keywords}.
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

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.run(transport="streamable-http")
