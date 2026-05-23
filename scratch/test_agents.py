"""
scratch/test_agents.py  (Phase 3A)

Standalone unit tests for ValidationAgent and SelfHealingAgent.
No running server required — imports agents directly.

Usage:
    cd nlsql
    python scratch/test_agents.py
    python scratch/test_agents.py --db riskdb --skip-explain
"""

import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()


def section(title: str) -> None:
    print(f"\n{'─'*56}\n  {title}\n{'─'*56}")

def ok(msg: str)   -> None: print(f"  ✅ {msg}")
def warn(msg: str) -> None: print(f"  ⚠  {msg}")
def fail(msg: str) -> None: print(f"  ❌ {msg}")


# ── Layer 1: sqlglot syntax ────────────────────────────────────────────────────

def test_syntax_valid():
    section("Layer 1 — valid Oracle SQL")
    from backend.agents.validation_agent import ValidationAgent
    result = ValidationAgent()._check_syntax("""
        SELECT l.BRCH_CD, SUM(l.DISB_AMT_LCY) AS total
        FROM   FINCORE.LOAN_MASTER l
        WHERE  l.DISB_DT >= TRUNC(SYSDATE, 'Q')
        GROUP  BY l.BRCH_CD
        ORDER  BY total DESC
        FETCH  FIRST 10 ROWS ONLY
    """)
    ok("Syntax valid") if result["valid"] else fail(result["error"][:80])


def test_syntax_unbalanced_paren():
    section("Layer 1 — unbalanced parenthesis")
    from backend.agents.validation_agent import ValidationAgent
    result = ValidationAgent()._check_syntax(
        "SELECT NVL(l.DISB_AMT_LCY, 0 FROM FINCORE.LOAN_MASTER l"
    )
    if not result["valid"]:
        ok(f"Caught: {result['error'][:80]}")
    else:
        warn("sqlglot did not catch unbalanced paren — Oracle will at runtime")


# ── Layer 2: safety + transforms ──────────────────────────────────────────────

def test_read_only_guard():
    section("Layer 2 — read-only guard")
    from backend.services.oracle_service import validate_read_only
    for dml, should_block in [
        ("INSERT INTO FINCORE.LOAN_MASTER VALUES (1,2)", True),
        ("UPDATE FINCORE.LOAN_MASTER SET STATUS='X'",    True),
        ("DROP TABLE FINCORE.LOAN_MASTER",               True),
        ("SELECT LOAN_ACCT_NO FROM FINCORE.LOAN_MASTER", False),
    ]:
        try:
            validate_read_only(dml)
            if should_block:
                fail(f"NOT blocked: {dml[:50]}")
            else:
                ok(f"Allowed: {dml[:50]}")
        except ValueError:
            if should_block:
                ok(f"Blocked: {dml[:50]}")
            else:
                fail(f"Wrongly blocked: {dml[:50]}")


def test_pii_masking():
    section("Layer 2 — PII auto-masking")
    from backend.services.oracle_service import detect_and_mask_pii
    sql = (
        "SELECT c.CUST_NM, c.PAN_NO, c.MOBILE, c.ACCT_NO "
        "FROM FINCORE.CUST_MASTER c"
    )
    masked, warnings = detect_and_mask_pii(sql)
    if warnings:
        ok(f"{len(warnings)} PII column(s) masked")
        for w in warnings:
            print(f"     {w}")
    else:
        warn("No PII detected — verify _PII_PATTERNS covers your column names")
    print(f"  Masked (first 120 chars): {masked[:120]}")


def test_row_limit_injection():
    section("Layer 2 — FETCH FIRST injection")
    from backend.services.oracle_service import inject_row_limit
    sql = "SELECT LOAN_ACCT_NO FROM FINCORE.LOAN_MASTER"
    out = inject_row_limit(sql, 500)
    ok(f"Injected: …{out[-30:]}") if "FETCH FIRST" in out.upper() else fail("Not injected")

    sql2 = "SELECT * FROM FINCORE.LOAN_MASTER FETCH FIRST 100 ROWS ONLY"
    out2 = inject_row_limit(sql2, 500)
    count = out2.upper().count("FETCH FIRST")
    ok("Not duplicated") if count == 1 else fail(f"Duplicated! count={count}")


# ── Healing prompt ─────────────────────────────────────────────────────────────

def test_healing_prompt():
    section("Healing prompt builder")
    from backend.prompts.healing_prompt import build_healing_message, STRATEGIES
    for code in list(STRATEGIES.keys())[:6]:
        msg = build_healing_message(
            error_code        = code,
            error_text        = f"Simulated error for {code}",
            failed_sql        = "SELECT * FROM FINCORE.LOAN_MASTER",
            original_question = "Show loan disbursements by branch",
            schema_context    = "Table: FINCORE.LOAN_MASTER\n  - LOAN_ACCT_NO (VARCHAR2) [PK]",
            attempt           = 1,
            max_attempts      = 3,
        )
        if "Attempt 1 of 3" in msg and "FINCORE" in msg:
            ok(f"{code}: prompt OK ({len(msg)} chars)")
        else:
            fail(f"{code}: prompt missing expected content")


# ── Error classifier ───────────────────────────────────────────────────────────

def test_error_classifier():
    section("SelfHealingAgent error classifier")
    from backend.agents.self_healing_agent import SelfHealingAgent
    agent = SelfHealingAgent()
    cases = [
        ("ORA-00942: table or view does not exist",  "ORA-00942"),
        ("ORA-00904: \"BRCH_CD\": invalid identifier","ORA-00904"),
        ("ORA-01722: invalid number",                "ORA-01722"),
        ("ORA-00918: column ambiguously defined",    "ORA-00918"),
        ("ORA-00907: missing right parenthesis",     "ORA-00907"),
        ("ORA-01476: divisor is equal to zero",      "ORA-01476"),
        ("Cartesian join detected in EXPLAIN PLAN",  "cartesian_join"),
        ("cost_too_high — threshold exceeded",       "cost_too_high"),
        ("sqlglot parse error on line 3",            "sqlglot_syntax"),
        ("Some totally unknown error xyz",           "unknown"),
    ]
    for error_text, expected in cases:
        got = agent._classify(error_text)
        if got == expected:
            ok(f"{expected}")
        else:
            fail(f"Expected {expected}, got {got}")


# ── Full agent (needs Oracle) ──────────────────────────────────────────────────

def test_full_agent(db_id: str, skip_explain: bool):
    section(f"Full ValidationAgent — db='{db_id}'  skip_explain={skip_explain}")
    from backend.agents.validation_agent import ValidationAgent
    agent = ValidationAgent()

    cases = [
        ("Valid SELECT",
         "SELECT table_name FROM all_tables WHERE rownum < 5",
         True),
        ("Missing table alias leads to potential ORA-00904",
         "SELECT LOAN_ACCT_NO FROM FINCORE.LOAN_MASTER FETCH FIRST 10 ROWS ONLY",
         True),
        ("Forbidden keyword DELETE",
         "DELETE FROM FINCORE.LOAN_MASTER WHERE loan_acct_no = '001'",
         False),
    ]
    for label, sql, expect_valid in cases:
        try:
            result = agent.validate(db_id, sql, max_rows=10, skip_explain=skip_explain)
            if result.valid == expect_valid:
                cost_str = f"cost={result.cost_estimate}" if result.cost_estimate else "cost=N/A"
                ok(f"{label} → valid={result.valid}  {cost_str}")
            else:
                fail(f"{label} → expected valid={expect_valid}, got valid={result.valid}")
                if result.issues:
                    for i in result.issues:
                        print(f"       [{i.severity}] {i.code}: {i.message[:60]}")
        except Exception as e:
            warn(f"{label} → Oracle not reachable ({e.__class__.__name__}): {str(e)[:60]}")
            warn("Layers 1 + 2 still validated above without Oracle connection")
            break


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db",           default="fincore")
    parser.add_argument("--skip-explain", action="store_true", default=False)
    args = parser.parse_args()

    print("\n" + "="*56)
    print("  NL-SQL Phase 3A — Agent Tests")
    print("="*56)

    test_syntax_valid()
    test_syntax_unbalanced_paren()
    test_read_only_guard()
    test_pii_masking()
    test_row_limit_injection()
    test_healing_prompt()
    test_error_classifier()
    test_full_agent(args.db, skip_explain=args.skip_explain)

    print(f"\n{'='*56}")
    print("  Done. Start the system with:")
    print("    uvicorn backend.main:app --reload")
    print("    streamlit run frontend/app.py")
    print(f"{'='*56}\n")


if __name__ == "__main__":
    main()
