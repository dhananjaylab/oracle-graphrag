"""
backend/routes/schema.py  (v2 + Phase 3A feedback + Phase 3B MCP routing)

Phase 3B changes:
  /api/schema    → reads from neo4j_mcp.get_schema_summary()
  /api/feedback  → writes through neo4j_mcp.record_feedback()

Both fall back to direct neo4j_service calls when the MCP server
is unavailable, ensuring the API stays live during MCP restarts.
"""

from fastapi import APIRouter

from backend.db_manager import db_manager
from backend.mcp_client import neo4j_mcp
from backend.models import (
    SchemaResponse, DatabaseSummary, DomainSummary, TableSummary,
    FeedbackRequest,
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
    """
    Liveness check. Reports Oracle DB config status and MCP server reachability.
    """
    dbs = [
        {"id": d.id, "name": d.name, "configured": d.is_configured}
        for d in db_manager.databases
    ]

    # Probe both MCP servers (non-blocking — failures don't break health)
    from backend.mcp_client import oracle_mcp
    oracle_mcp_ok = await oracle_mcp.ping()
    neo4j_mcp_ok  = await neo4j_mcp.ping()

    return {
        "status":     "ok",
        "databases":  dbs,
        "mcp_servers": {
            "oracle": "up" if oracle_mcp_ok else "down (fallback active)",
            "neo4j":  "up" if neo4j_mcp_ok  else "down (fallback active)",
        },
    }


# ── Schema ─────────────────────────────────────────────────────────────────────

@router.get("/schema", response_model=SchemaResponse)
async def schema():
    """
    Return all databases with enriched tables and business domains.
    Reads from Neo4j via MCP — no Oracle queries at UI load time.
    Falls back to direct neo4j_service on MCP failure.
    """
    raw          = await neo4j_mcp.get_schema_summary()
    db_summaries: list[DatabaseSummary] = []

    for db_raw in raw.get("databases", []):
        db_id   = db_raw.get("id", "")
        tables  = db_raw.get("tables",  []) or []
        domains = db_raw.get("domains", []) or []

        # Deduplicate tables (Cypher collect() may produce nulls)
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


# ── Databases list ─────────────────────────────────────────────────────────────

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


# ── Examples ───────────────────────────────────────────────────────────────────

@router.get("/examples")
async def examples():
    return {"examples": EXAMPLE_QUESTIONS}


# ── Feedback (Phase 3A + 3B MCP routing) ──────────────────────────────────────

@router.post("/feedback")
async def feedback(request: FeedbackRequest):
    """
    Record user feedback on a generated SQL result.

    Phase 3B: routed through Neo4j MCP server (neo4j_mcp.record_feedback).
    Falls back to direct neo4j_service calls on MCP failure.

    rating ≥ 4  (thumbs up)   → action="increment"  success_count + 1
    rating < 4  (thumbs down) → action="decrement"  success_count - 1
    corrected_sql provided    → action="correct"     replace stored SQL + 2
    """
    updated      = False
    corrected    = (request.corrected_sql or "").strip()

    if request.rating >= 4:
        updated = await neo4j_mcp.record_feedback(
            nl_question = request.nl_question,
            database_id = request.db_id,
            action      = "increment",
        )
    else:
        updated = await neo4j_mcp.record_feedback(
            nl_question = request.nl_question,
            database_id = request.db_id,
            action      = "decrement",
        )

    # Correction is additive — apply on top of any rating
    if corrected:
        await neo4j_mcp.record_feedback(
            nl_question   = request.nl_question,
            database_id   = request.db_id,
            action        = "correct",
            corrected_sql = corrected,
        )

    return {
        "status":          "recorded" if updated else "pattern_not_found",
        "rating":          request.rating,
        "question":        request.nl_question[:80],
        "has_correction":  bool(corrected),
    }
