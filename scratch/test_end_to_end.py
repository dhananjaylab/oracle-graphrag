"""
scratch/test_end_to_end.py  (Phase 3A)
Full pipeline smoke test via FastAPI backend.
Server must be running: uvicorn backend.main:app --reload

Usage:
    python scratch/test_end_to_end.py
    python scratch/test_end_to_end.py --db riskdb --execute
    python scratch/test_end_to_end.py --question "NPA ratio by product this month"
"""

import argparse
import asyncio
import httpx

BACKEND = "http://localhost:8000"

DEFAULTS = {
    "fincore": "Show total loan disbursements by branch this quarter",
    "riskdb":  "What is the NPA ratio by product segment this month?",
}


async def run(db_id: str, question: str, execute: bool) -> None:
    print(f"\n{'='*60}")
    print(f"  DB: {db_id}   execute={execute}")
    print(f"  Q:  {question}")
    print(f"{'='*60}\n")

    async with httpx.AsyncClient(timeout=120) as client:

        # 1. Health
        h = (await client.get(f"{BACKEND}/api/health")).json()
        print(f"Health: {h['status']}")
        for db in h.get("databases", []):
            print(f"  {'✅' if db['configured'] else '⚠'} {db['id']}: {db['name']}")
        print()

        # 2. Query
        r    = await client.post(f"{BACKEND}/api/query", json={
            "question": question, "db_id": db_id,
            "execute": execute, "max_rows": 50,
            "conversation_history": [], "skip_explain_plan": True,
        })
        resp = r.json()

        if resp.get("error"):
            print(f"❌ Error: {resp['error']}")
            trace = resp.get("agent_trace") or {}
            if trace.get("healing_attempts"):
                print(f"   Healing attempts: {len(trace['healing_attempts'])}")
                for a in trace["healing_attempts"]:
                    print(f"   [{a['attempt']}] {a['error_code']} → {a['outcome']}")
            return

        meta = resp.get("meta", {})
        print(f"SQL:\n{resp.get('sql','')}\n")
        print(f"Tables used:     {meta.get('tables_used', [])}")
        print(f"Pattern matched: {meta.get('pattern_matched', False)}")
        print(f"Healed:          {meta.get('healed', False)}")

        trace = resp.get("agent_trace") or {}
        val   = trace.get("validation") or {}
        if val:
            print(f"Validation:      valid={val.get('valid')}  cost={val.get('cost_estimate')}")

        heal = trace.get("healing_attempts", [])
        if heal:
            print(f"Healing:         {len(heal)} attempt(s)")
            for a in heal:
                print(f"  [{a['attempt']}] {a['error_code']} → {a['outcome']}")

        if execute and resp.get("rows"):
            print(f"\nRows: {meta.get('row_count', 0)}")
            print(f"Summary: {resp.get('summary', '')}")

        cypher = resp.get("schema_cypher", "")
        if cypher:
            print(f"\nCypher (first 200): {cypher[:200]}")

        # 3. Feedback smoke test (thumbs up)
        fb = (await client.post(f"{BACKEND}/api/feedback", json={
            "nl_question": question,
            "db_id":       db_id,
            "rating":      5,
        })).json()
        print(f"\nFeedback: {fb.get('status')}  rating=5")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db",       default="fincore")
    p.add_argument("--question", default="")
    p.add_argument("--execute",  action="store_true",
                   help="Actually run SQL on Oracle (default: preview only)")
    args = p.parse_args()
    q = args.question or DEFAULTS.get(args.db, DEFAULTS["fincore"])
    asyncio.run(run(args.db, q, execute=args.execute))


if __name__ == "__main__":
    main()
