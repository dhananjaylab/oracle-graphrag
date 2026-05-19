"""
backend/routes/schema.py  (v2 + Phase 3A feedback endpoint)

New in Phase 3A:
  POST /api/feedback  — thumbs up/down → updates QueryPattern.success_count
                        optional corrected_sql → replaces stored SQL
"""

from fastapi import APIRouter

from backend.db_manager import db_manager
from backend.models import (
    SchemaResponse, DatabaseSummary, DomainSummary, TableSummary,
    FeedbackRequest,
)
from backend.services.neo4j_service import (
    get_schema_summary,
    increment_pattern_success,
    decrement_pattern_success,
    update_pattern_sql,
)

router = APIRouter()

EXAMPLE_QUESTIONS = [
    "Show total loan disbursements by branch for the current quarter",
    "List all transactions above ₹10 lakh in the last 30 days",
    "What is the NPA ratio by product segment as of this month end?",
    "Show month-over-month GL account balance movement this year",
    "Which customers have overdue EMI payments older than 90 days?",
    "Compare CASA balance across top 5 branches for the current year",
    "Show top 10 foreign currency transactions this week by amount",
]


# ── Health ─────────────────────────────────────────────────────────────────────

@router.get("/health")
async def health():
    dbs = [
        {"id": d.id, "name": d.name, "configured": d.is_configured}
        for d in db_manager.databases
    ]
    return {"status": "ok", "databases": dbs}


# ── Schema explorer ────────────────────────────────────────────────────────────

@router.get("/schema", response_model=SchemaResponse)
async def schema():
    """
    Return all databases with enriched tables and business domains.
    Reads from Neo4j — no Oracle queries at UI load time.
    """
    raw          = await get_schema_summary()
    db_summaries: list[DatabaseSummary] = []

    for db_raw in raw.get("databases", []):
        db_id   = db_raw.get("id", "")
        tables  = db_raw.get("tables", []) or []
        domains = db_raw.get("domains", []) or []

        # Deduplicate tables (Cypher collect may return nulls)
        seen: set[str] = set()
        table_summaries: list[TableSummary] = []
        for t in tables:
            if not t or not t.get("name") or t["name"] in seen:
                continue
            seen.add(t["name"])
            table_summaries.append(TableSummary(
                name             = t["name"],
                description      = t.get("description") or "",
                column_count     = 0,
                is_view          = bool(t.get("is_view", False)),
                row_count_approx = int(t.get("row_count") or 0),
                domain           = t.get("domain") or "",
            ))

        # Deduplicate domains
        seen_d: set[str] = set()
        domain_summaries: list[DomainSummary] = []
        for d in domains:
            if not d or not d.get("name") or d["name"] in seen_d:
                continue
            seen_d.add(d["name"])
            domain_summaries.append(DomainSummary(
                name=d["name"], hint=d.get("hint") or "",
            ))

        try:
            cfg  = db_manager.get_config(db_id)
            desc = cfg.description
        except Exception:
            desc = db_raw.get("description") or ""

        db_summaries.append(DatabaseSummary(
            id          = db_id,
            name        = db_raw.get("name") or db_id,
            description = desc,
            table_count = int(db_raw.get("table_count") or len(table_summaries)),
            domains     = domain_summaries,
            tables      = table_summaries,
        ))

    return SchemaResponse(databases=db_summaries, total_databases=len(db_summaries))


@router.get("/databases")
async def databases():
    """Quick list of registered databases for the UI DB-selector dropdown."""
    return {
        "databases": [
            {
                "id":          d.id,
                "name":        d.name,
                "description": d.description,
                "configured":  d.is_configured,
            }
            for d in db_manager.databases
        ]
    }


@router.get("/examples")
async def examples():
    return {"examples": EXAMPLE_QUESTIONS}


# ── Feedback (Phase 3A) ────────────────────────────────────────────────────────

@router.post("/feedback")
async def feedback(request: FeedbackRequest):
    """
    Record user feedback on a generated SQL result.

    rating ≥ 4  (thumbs up)   → increment pattern.success_count
    rating < 4  (thumbs down) → decrement pattern.success_count
    corrected_sql provided    → replace stored SQL (also bumps count by +2)

    Effect: patterns with higher success_count rank higher in future
    semantic searches and are injected as more-trusted few-shot examples.
    """
    updated = False

    if request.rating >= 4:
        updated = await increment_pattern_success(request.nl_question, request.db_id)
    else:
        updated = await decrement_pattern_success(request.nl_question, request.db_id)

    if request.corrected_sql and request.corrected_sql.strip():
        await update_pattern_sql(
            request.nl_question,
            request.db_id,
            request.corrected_sql.strip(),
        )

    return {
        "status":   "recorded" if updated else "pattern_not_found",
        "rating":   request.rating,
        "question": request.nl_question[:80],
        "has_correction": bool(request.corrected_sql),
    }
