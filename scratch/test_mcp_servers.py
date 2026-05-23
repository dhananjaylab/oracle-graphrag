"""
scratch/test_mcp_servers.py  (Phase 3B)

Integration tests for Oracle MCP and Neo4j MCP servers.
Both MCP servers must be running before executing this script.

Usage:
    # Terminal 1 — start Oracle MCP
    python -m mcp_servers.oracle_mcp.server

    # Terminal 2 — start Neo4j MCP
    python -m mcp_servers.neo4j_mcp.server

    # Terminal 3 — run tests
    python scratch/test_mcp_servers.py
    python scratch/test_mcp_servers.py --oracle-url http://localhost:8001
"""

import argparse
import asyncio
import json
import sys
from dotenv import load_dotenv
load_dotenv()

from backend.mcp_client.base import MCPClientSession


# ── Helpers ────────────────────────────────────────────────────────────────────

def section(title: str) -> None:
    print(f"\n{'─'*60}\n  {title}\n{'─'*60}")

def ok(msg: str)   -> None: print(f"  ✅ {msg}")
def warn(msg: str) -> None: print(f"  ⚠  {msg}")
def fail(msg: str) -> None: print(f"  ❌ {msg}")


# ══════════════════════════════════════════════════════════════════════════════
# ORACLE MCP TESTS
# ══════════════════════════════════════════════════════════════════════════════

async def test_oracle_list_databases(oracle_url: str) -> None:
    section("Oracle MCP — list_databases")
    async with MCPClientSession(oracle_url, "oracle") as client:
        result = await client.call_tool("list_databases", {})
        if isinstance(result, list) and len(result) > 0:
            ok(f"{len(result)} database(s) registered")
            for db in result:
                tick = "✅" if db.get("configured") else "⚠ "
                print(f"     {tick} {db['id']}: {db['name']} (schema={db['schema']})")
        else:
            fail(f"Unexpected result: {result}")


async def test_oracle_check_read_only(oracle_url: str) -> None:
    section("Oracle MCP — check_read_only")
    async with MCPClientSession(oracle_url, "oracle") as client:
        for sql, expect_valid in [
            ("SELECT * FROM FINCORE.LOAN_MASTER FETCH FIRST 5 ROWS ONLY", True),
            ("DELETE FROM FINCORE.LOAN_MASTER WHERE 1=1",                  False),
            ("DROP TABLE FINCORE.LOAN_MASTER",                             False),
        ]:
            result = await client.call_tool("check_read_only", {"sql": sql})
            got_valid = result.get("valid", None)
            if got_valid == expect_valid:
                ok(f"valid={got_valid} | {sql[:55]}…")
            else:
                fail(f"Expected valid={expect_valid}, got {result} | {sql[:55]}…")


async def test_oracle_execute_query(oracle_url: str, db_id: str) -> None:
    section(f"Oracle MCP — execute_query (db={db_id})")
    async with MCPClientSession(oracle_url, "oracle") as client:
        sql    = "SELECT table_name FROM all_tables WHERE rownum <= 5"
        result = await client.call_tool("execute_query", {
            "db_id": db_id, "sql": sql, "max_rows": 5
        })
        if "error" in result:
            warn(f"Oracle not reachable for '{db_id}': {result['error']}")
        elif "columns" in result:
            ok(f"{result['row_count']} rows · cols={result['columns']}")
        else:
            fail(f"Unexpected result: {result}")


async def test_oracle_explain_plan(oracle_url: str, db_id: str) -> None:
    section(f"Oracle MCP — explain_plan (db={db_id})")
    async with MCPClientSession(oracle_url, "oracle") as client:
        sql    = "SELECT table_name FROM all_tables WHERE rownum <= 10"
        result = await client.call_tool("explain_plan", {
            "db_id": db_id, "sql": sql
        })
        if "error" in result and result.get("cost") is None:
            warn(f"EXPLAIN PLAN not available: {result.get('error','')}")
        else:
            ok(f"cost={result.get('cost')}  "
               f"full_scan={result.get('has_full_scan')}  "
               f"cartesian={result.get('has_cartesian')}")


# ══════════════════════════════════════════════════════════════════════════════
# NEO4J MCP TESTS
# ══════════════════════════════════════════════════════════════════════════════

async def test_neo4j_schema_summary(neo4j_url: str) -> None:
    section("Neo4j MCP — get_schema_summary")
    async with MCPClientSession(neo4j_url, "neo4j") as client:
        result = await client.call_tool("get_schema_summary", {})
        dbs = result.get("databases", [])
        if dbs:
            ok(f"{len(dbs)} database(s) in graph")
            for db in dbs:
                print(f"     {db.get('id')}: {db.get('table_count',0)} tables")
        else:
            warn("No databases in Neo4j — run ingestion first")


async def test_neo4j_semantic_search(neo4j_url: str, db_id: str) -> None:
    section(f"Neo4j MCP — semantic_search (db={db_id})")
    async with MCPClientSession(neo4j_url, "neo4j") as client:
        # Use a dummy 3072-dim zero embedding — just testing the tool
        zero_emb = [0.0] * 3072
        result   = await client.call_tool("semantic_search", {
            "embedding_json": json.dumps(zero_emb),
            "database_id":    db_id,
            "top_k":          5,
        })
        tables  = result.get("tables",  [])
        columns = result.get("columns", [])
        cypher  = result.get("cypher_used", "")
        if tables or columns:
            ok(f"{len(tables)} tables · {len(columns)} columns returned")
            ok(f"Cypher preserved: {len(cypher)} chars")
        else:
            warn("No results — embeddings may not be ingested yet")


async def test_neo4j_store_and_search(neo4j_url: str, db_id: str) -> None:
    section(f"Neo4j MCP — store_pattern then search_patterns (db={db_id})")
    test_emb = [0.001 * i for i in range(3072)]
    async with MCPClientSession(neo4j_url, "neo4j") as client:
        # Store a test pattern
        store_result = await client.call_tool("store_pattern", {
            "database_id":      db_id,
            "nl_question":      "__test_mcp_pattern__",
            "sql":              "SELECT 1 FROM DUAL",
            "schema_cypher":    "-- test cypher",
            "tables_used_json": json.dumps(["DUAL"]),
            "execution_ms":     42,
            "embedding_json":   json.dumps(test_emb),
        })
        if store_result.get("stored"):
            ok("Pattern stored")
        else:
            warn(f"Store result: {store_result}")

        # Search for it (similarity=0.0 threshold so it comes back)
        search_result = await client.call_tool("search_patterns", {
            "embedding_json": json.dumps(test_emb),
            "database_id":    db_id,
            "top_k":          3,
            "min_similarity": 0.0,
        })
        found = [
            p for p in (search_result if isinstance(search_result, list) else [])
            if p.get("nl_question") == "__test_mcp_pattern__"
        ]
        if found:
            ok(f"Pattern retrieved: score={found[0].get('score'):.4f}")
        else:
            warn("Test pattern not found in search results")

        # Feedback: increment
        fb_result = await client.call_tool("record_feedback", {
            "nl_question": "__test_mcp_pattern__",
            "database_id": db_id,
            "action":      "increment",
        })
        ok(f"Feedback recorded: {fb_result}")


async def test_neo4j_join_path(neo4j_url: str, db_id: str) -> None:
    section(f"Neo4j MCP — get_join_path (db={db_id})")
    async with MCPClientSession(neo4j_url, "neo4j") as client:
        result = await client.call_tool("get_join_path", {
            "table1":      "LOAN_MASTER",
            "table2":      "BRANCH_MASTER",
            "database_id": db_id,
        })
        paths = result if isinstance(result, list) else []
        if paths:
            seq = " → ".join(paths[0].get("table_sequence", []))
            ok(f"Path found: {seq}")
        else:
            warn("No join path found (tables may not be in graph yet)")


# ══════════════════════════════════════════════════════════════════════════════
# MCP CLIENT TYPED WRAPPER TEST
# ══════════════════════════════════════════════════════════════════════════════

async def test_typed_clients(oracle_url: str, neo4j_url: str) -> None:
    section("Typed client wrappers (OracleMCPClient + Neo4jMCPClient)")
    from backend.mcp_client.oracle_client import OracleMCPClient
    from backend.mcp_client.neo4j_client  import Neo4jMCPClient

    oracle = OracleMCPClient(oracle_url)
    neo4j  = Neo4jMCPClient(neo4j_url)

    await oracle.connect()
    await neo4j.connect()

    try:
        dbs = await oracle.list_databases()
        ok(f"OracleMCPClient.list_databases() → {len(dbs)} DB(s)")

        safety = await oracle.check_read_only("SELECT 1 FROM DUAL")
        ok(f"OracleMCPClient.check_read_only()  → valid={safety.get('valid')}")

        summary = await neo4j.get_schema_summary()
        ok(f"Neo4jMCPClient.get_schema_summary() → "
           f"{len(summary.get('databases',[]))} DB(s)")

    finally:
        await oracle.disconnect()
        await neo4j.disconnect()


# ── Entry point ────────────────────────────────────────────────────────────────

async def run_all(oracle_url: str, neo4j_url: str, db_id: str) -> None:
    print("\n" + "="*60)
    print("  NL-SQL Phase 3B — MCP Server Integration Tests")
    print("="*60)

    # Oracle MCP tests
    try:
        await test_oracle_list_databases(oracle_url)
        await test_oracle_check_read_only(oracle_url)
        await test_oracle_execute_query(oracle_url, db_id)
        await test_oracle_explain_plan(oracle_url, db_id)
    except Exception as exc:
        fail(f"Oracle MCP not reachable at {oracle_url}: {exc}")
        print("  Start with: python -m mcp_servers.oracle_mcp.server")

    # Neo4j MCP tests
    try:
        await test_neo4j_schema_summary(neo4j_url)
        await test_neo4j_semantic_search(neo4j_url, db_id)
        await test_neo4j_join_path(neo4j_url, db_id)
        await test_neo4j_store_and_search(neo4j_url, db_id)
    except Exception as exc:
        fail(f"Neo4j MCP not reachable at {neo4j_url}: {exc}")
        print("  Start with: python -m mcp_servers.neo4j_mcp.server")

    # Typed client wrappers
    try:
        await test_typed_clients(oracle_url, neo4j_url)
    except Exception as exc:
        fail(f"Typed client test failed: {exc}")

    print(f"\n{'='*60}")
    print("  Done. Full system startup:")
    print("    python -m mcp_servers.oracle_mcp.server  &")
    print("    python -m mcp_servers.neo4j_mcp.server   &")
    print("    uvicorn backend.main:app --reload")
    print("    streamlit run frontend/app.py")
    print(f"{'='*60}\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--oracle-url", default="http://localhost:8001")
    parser.add_argument("--neo4j-url",  default="http://localhost:8002")
    parser.add_argument("--db",         default="fincore")
    args = parser.parse_args()
    asyncio.run(run_all(args.oracle_url, args.neo4j_url, args.db))


if __name__ == "__main__":
    main()
