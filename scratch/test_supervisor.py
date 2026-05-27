"""
scratch/test_supervisor.py  (Phase 3C)

Tests for SupervisorAgent, tool executor, and SSE streaming endpoint.
Requires MCP servers running for full integration tests.
Unit tests run without any server.

Usage:
    cd nlsql
    python scratch/test_supervisor.py                  # unit tests only
    python scratch/test_supervisor.py --integration    # full integration (needs servers)
    python scratch/test_supervisor.py --db fincore     # specify DB for integration
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()


def section(title: str) -> None:
    print(f"\n{'─'*60}\n  {title}\n{'─'*60}")

def ok(msg: str)   -> None: print(f"  ✅ {msg}")
def warn(msg: str) -> None: print(f"  ⚠  {msg}")
def fail(msg: str) -> None: print(f"  ❌ {msg}")


# ══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS — no server needed
# ══════════════════════════════════════════════════════════════════════════════

def test_tool_definitions():
    section("Tool definitions — all 14 declared")
    from backend.tools.function_definitions import (
        SUPERVISOR_TOOLS, ALL_TOOL_NAMES,
        ORACLE_TOOL_NAMES, NEO4J_TOOL_NAMES, CONTROL_TOOL_NAMES,
    )
    fns = {fd.name for fd in SUPERVISOR_TOOLS.function_declarations}

    expected = {"execute_query","explain_plan","get_schema","list_databases","check_read_only",
                "semantic_search","get_table_details","get_join_path","get_cross_db_hints",
                "search_patterns","store_pattern","get_schema_summary","finish"}

    for name in expected:
        if name in fns:
            ok(f"Tool declared: {name}")
        else:
            fail(f"MISSING tool: {name}")

    extra = fns - expected
    if extra:
        warn(f"Unexpected extra tools: {extra}")

    ok(f"Oracle tools: {ORACLE_TOOL_NAMES}")
    ok(f"Neo4j tools:  {NEO4J_TOOL_NAMES}")
    ok(f"Control tools: {CONTROL_TOOL_NAMES}")
    ok(f"ALL_TOOL_NAMES has {len(ALL_TOOL_NAMES)} entries")


def test_supervisor_prompt():
    section("Supervisor prompt builder")
    from backend.prompts.supervisor_prompt import (
        SUPERVISOR_SYSTEM_PROMPT,
        build_conversation_context,
        build_supervisor_user_message,
    )

    # Test prompt content
    required_sections = [
        "TOOL CATEGORIES", "ROUTING STRATEGY",
        "MERGE STRATEGY", "PARTIAL RESULTS POLICY",
        "SAFETY RULES", "finish",
    ]
    for sec in required_sections:
        if sec in SUPERVISOR_SYSTEM_PROMPT:
            ok(f"Section present: {sec}")
        else:
            fail(f"Section missing: {sec}")

    # Test context builder (empty history)
    ctx_empty = build_conversation_context([])
    ok(f"Empty context: {ctx_empty[:50]}")

    # Test context builder (with history)
    history = [
        {
            "question": "Show NPA ratio by product",
            "dbs_queried": ["riskdb"],
            "tables_used": ["NPA_MASTER"],
            "row_count": 12,
            "key_metrics": {"NPA_RATIO.sum": 4.2},
            "partial": False,
            "missing_info": "",
        }
    ]
    ctx = build_conversation_context(history)
    if "Show NPA ratio" in ctx and "riskdb" in ctx:
        ok("Context correctly includes prior turn")
    else:
        fail(f"Context missing expected content: {ctx[:200]}")

    # Test user message builder
    msg = build_supervisor_user_message(
        question             = "Compare loans vs NPA by branch",
        databases            = [{"id": "fincore", "name": "Core Banking",
                                  "description": "Loans", "configured": True}],
        embedding_note       = "[3072-dim vector]",
        conversation_context = ctx,
    )
    if "Compare loans" in msg and "fincore" in msg:
        ok(f"User message built correctly ({len(msg)} chars)")
    else:
        fail(f"User message missing content: {msg[:200]}")


def test_tool_executor_routing():
    section("Tool executor routing logic")
    from backend.tools.function_definitions import ORACLE_TOOL_NAMES, NEO4J_TOOL_NAMES

    oracle_expected = {"execute_query","explain_plan","get_schema","list_databases","check_read_only"}
    neo4j_expected  = {"semantic_search","get_table_details","get_join_path","get_cross_db_hints",
                        "search_patterns","store_pattern","get_schema_summary"}

    for name in oracle_expected:
        if name in ORACLE_TOOL_NAMES:
            ok(f"Oracle: {name}")
        else:
            fail(f"Missing from ORACLE_TOOL_NAMES: {name}")

    for name in neo4j_expected:
        if name in NEO4J_TOOL_NAMES:
            ok(f"Neo4j: {name}")
        else:
            fail(f"Missing from NEO4J_TOOL_NAMES: {name}")


def test_supervisor_agent_init():
    section("SupervisorAgent initialisation")
    try:
        from backend.agents.supervisor_agent import supervisor_agent
        ok(f"supervisor_agent singleton created: {type(supervisor_agent).__name__}")
    except Exception as e:
        fail(f"SupervisorAgent failed to initialise: {e}")


def test_models():
    section("Phase 3C Pydantic models")
    from backend.models import (
        SupervisorRequest, SupervisorResult, DBResult,
        ToolCallRecord, ConversationContextEntry,
    )

    req = SupervisorRequest(question="Show NPA ratio")
    ok(f"SupervisorRequest: question='{req.question}', max_rows={req.max_rows}")

    entry = ConversationContextEntry(
        question="Test", dbs_queried=["fincore"], row_count=10,
        key_metrics={"loan.sum": 100.0}
    )
    ok(f"ConversationContextEntry: {entry.dbs_queried}")

    dbr = DBResult(db_id="fincore", sql="SELECT 1 FROM DUAL", row_count=1)
    ok(f"DBResult: db_id={dbr.db_id}, row_count={dbr.row_count}")

    tc = ToolCallRecord(
        tool_name="semantic_search", args={"database_id": "fincore"},
        result={"ok": True}, elapsed_ms=42, iteration=1
    )
    ok(f"ToolCallRecord: {tc.tool_name} in {tc.elapsed_ms}ms")

    res = SupervisorResult(summary="NPA ratio is 3.2%", dbs_queried=["riskdb"])
    ok(f"SupervisorResult: summary='{res.summary[:30]}...'")


def test_conversation_context_compression():
    section("Conversation context compression")
    from backend.prompts.supervisor_prompt import build_conversation_context

    # 10 turns — should only keep last 5
    history = [
        {
            "question":     f"Query number {i}",
            "dbs_queried":  ["fincore"],
            "tables_used":  ["LOAN_MASTER"],
            "row_count":    100 * i,
            "key_metrics":  {"amount.sum": float(i * 1_000_000)},
            "partial":      (i % 3 == 0),
            "missing_info": "some data" if i % 3 == 0 else "",
        }
        for i in range(1, 11)
    ]
    ctx = build_conversation_context(history)

    # Last 5 turns should be present
    for i in range(6, 11):
        if f"Query number {i}" in ctx:
            ok(f"Turn {i} present in context")
        else:
            fail(f"Turn {i} missing from context")

    # Early turns should not be present
    if "Query number 1" not in ctx and "Query number 5" not in ctx:
        ok("Old turns correctly truncated (last 5 only)")
    else:
        warn("Old turns may still be in context — check truncation logic")


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS — require MCP servers + FastAPI running
# ══════════════════════════════════════════════════════════════════════════════

async def test_supervisor_stream_single_db(db_id: str, question: str) -> None:
    section(f"SupervisorAgent stream — single DB ({db_id})")
    from backend.agents.supervisor_agent import supervisor_agent
    from backend.services.gemini_service import get_embedding

    try:
        embedding = await asyncio.to_thread(get_embedding, question)
        ok(f"Embedding computed: {len(embedding)} dims")
    except Exception as e:
        fail(f"Embedding failed: {e}")
        return

    databases = [{"id": db_id, "name": "Test DB",
                  "description": "Test", "configured": True}]

    events  = []
    finish  = None
    tool_ct = 0

    async for event in supervisor_agent.run_stream(
        question             = question,
        query_embedding      = embedding,
        databases            = databases,
        conversation_history = [],
    ):
        events.append(event)
        if event.event_type == "tool_call":
            tool_ct += 1
            print(f"     🔧 {event.data.get('tool_name','?')} — {event.data.get('message','')}")
        elif event.event_type == "tool_result":
            status = "✅" if event.data.get("ok", True) else "❌"
            print(f"     {status}  {event.data.get('tool_name','?')} → {event.data.get('summary','')}")
        elif event.event_type == "finish":
            finish = event.data

    if finish:
        ok(f"Supervisor finished: {finish.get('total_iterations',0)} iterations")
        ok(f"Summary: {finish.get('summary','')[:80]}…")
        ok(f"DBs queried: {finish.get('dbs_queried', [])}")
        ok(f"Tool calls: {tool_ct}")
        if finish.get("partial"):
            warn(f"Partial result: {finish.get('missing_info','')}")
    else:
        fail(f"Supervisor did not call finish(). Events: {[e.event_type for e in events]}")


async def test_supervisor_stream_multi_db() -> None:
    section("SupervisorAgent stream — cross-DB question")
    from backend.agents.supervisor_agent import supervisor_agent
    from backend.services.gemini_service import get_embedding

    question = "Show loans from fincore that are classified as NPA in riskdb"

    try:
        embedding = await asyncio.to_thread(get_embedding, question)
    except Exception as e:
        fail(f"Embedding failed: {e}")
        return

    databases = [
        {"id": "fincore", "name": "Core Banking",    "description": "Loans, CASA, transactions", "configured": True},
        {"id": "riskdb",  "name": "Risk Management", "description": "NPA, credit ratings",       "configured": True},
    ]

    finish = None
    async for event in supervisor_agent.run_stream(
        question             = question,
        query_embedding      = embedding,
        databases            = databases,
        conversation_history = [],
    ):
        if event.event_type == "tool_call":
            print(f"     🔧 {event.data.get('tool_name','?')} — {event.data.get('message','')}")
        elif event.event_type == "finish":
            finish = event.data

    if finish:
        dbs = finish.get("dbs_queried", [])
        ok(f"Finished — DBs queried: {dbs}")
        ok(f"Merge strategy: {finish.get('merge_strategy','?')}")
        if len(dbs) > 1:
            ok("Multi-DB query confirmed!")
        else:
            warn("Only one DB queried — supervisor may not have detected cross-DB need")
    else:
        fail("Supervisor did not finish")


async def test_sse_endpoint(base_url: str, question: str) -> None:
    section(f"SSE endpoint — POST {base_url}/api/supervisor")
    import httpx

    events = []
    finish = None

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", f"{base_url}/api/supervisor", json={
                "question": question, "max_rows": 100, "conversation_history": [],
            }) as resp:
                resp.raise_for_status()
                current_type = ""
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        current_type = line[6:].strip()
                    elif line.startswith("data:"):
                        try:
                            data = json.loads(line[5:].strip())
                            events.append({"type": current_type, "data": data})
                            if current_type == "tool_call":
                                print(f"     🔧 {data.get('tool_name','?')}")
                            elif current_type == "finish":
                                finish = data
                        except json.JSONDecodeError:
                            pass

        if finish:
            ok(f"SSE stream complete: {len(events)} events")
            ok(f"Summary: {finish.get('summary','')[:80]}")
            ok(f"DBs: {finish.get('dbs_queried', [])}")
        else:
            fail(f"No finish event received. Got {len(events)} events: {[e['type'] for e in events]}")

    except httpx.ConnectError:
        warn(f"Backend not reachable at {base_url} — start with: uvicorn backend.main:app --reload")
    except Exception as e:
        fail(f"SSE test failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Phase 3C supervisor tests")
    parser.add_argument("--integration", action="store_true",
                        help="Run integration tests (needs MCP servers + FastAPI)")
    parser.add_argument("--db",       default="fincore",
                        help="Primary database ID for integration tests")
    parser.add_argument("--question", default="Show total loan disbursements by branch this quarter",
                        help="Question for integration tests")
    parser.add_argument("--backend",  default="http://localhost:8000",
                        help="Backend URL for SSE endpoint test")
    args = parser.parse_args()

    print("\n" + "="*60)
    print("  NL-SQL Phase 3C — Supervisor Tests")
    print("="*60)

    # ── Unit tests (always run) ──────────────────────────────────────────
    test_tool_definitions()
    test_supervisor_prompt()
    test_tool_executor_routing()
    test_supervisor_agent_init()
    test_models()
    test_conversation_context_compression()

    # ── Integration tests (opt-in) ───────────────────────────────────────
    if args.integration:
        print(f"\n{'='*60}")
        print(f"  Integration tests  (db={args.db})")
        print(f"{'='*60}")
        asyncio.run(test_supervisor_stream_single_db(args.db, args.question))
        asyncio.run(test_supervisor_stream_multi_db())
        asyncio.run(test_sse_endpoint(args.backend, args.question))
    else:
        print("\n  (Skipping integration tests — pass --integration to run them)")

    print(f"\n{'='*60}")
    print("  Done.")
    print("  Full startup:")
    print("    ./start_servers.sh")
    print("    python scratch/test_supervisor.py --integration")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
