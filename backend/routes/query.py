"""
backend/routes/query.py  (v2 — multi-DB + QueryPattern storage)

Full pipeline per request:
  1.  Resolve db_id (default to first registered DB)
  2.  Embed question          → gemini-embedding-001
  3.  Search QueryPatterns    → reuse stored SQL + schema_cypher if similar (≥0.85)
  4.  GraphRAG schema search  → neo4j vector search (scoped to db_id)
  5.  Get table details       → neo4j column + domain metadata
  6.  Find FK join paths      → neo4j graph traversal (scoped to db_id)
  7.  Get cross-DB hints      → CROSS_DB_JOIN edges
  8.  Build schema context    → metadata string (no raw data)
  9.  Generate SQL            → gemini (+ matched patterns as few-shots)
 10.  Validate SQL            → read-only check
 11.  Execute SQL             → oracle (on-prem, scoped to db_id)
 12.  Build output            → DataFrame, chart type, summary stats
 13.  Summarize results       → gemini (aggregate stats only, no raw rows)
 14.  Store QueryPattern      → neo4j (background task, preserves schema_cypher)
"""

import asyncio
import time
from fastapi import APIRouter, BackgroundTasks

from backend.db_manager import db_manager
from backend.models import (
    QueryRequest, QueryResponse, QueryMeta, MatchedPattern,
)
from backend.services import (
    oracle_service, neo4j_service, gemini_service, output_service,
)

router = APIRouter()


@router.post("/query", response_model=QueryResponse)
async def query(request: QueryRequest, background_tasks: BackgroundTasks):

    # ── Step 1: Resolve database ────────────────────────────────────────────
    db_id = request.db_id or db_manager.get_default_id()
    try:
        db_cfg = db_manager.get_config(db_id)
    except ValueError as e:
        return _err(request.question, db_id, "", str(e))

    warnings:    list[str] = []
    schema_cypher_log: list[str] = []   # accumulate all Cypher used this request

    # ── Step 2: Embed the question ───────────────────────────────────────────
    query_embedding: list[float] = await asyncio.to_thread(
        gemini_service.get_embedding, request.question
    )

    # ── Step 3: Search for similar past QueryPatterns ────────────────────────
    matched_patterns = await neo4j_service.search_similar_patterns(
        query_embedding=query_embedding,
        database_id=db_id,
        top_k=3,
        min_similarity=0.85,
    )
    # Build the MatchedPattern response object (best match, if any)
    best_match: MatchedPattern | None = None
    if matched_patterns:
        mp = matched_patterns[0]
        best_match = MatchedPattern(
            nl_question  = mp["nl_question"],
            sql          = mp["sql"],
            schema_cypher= mp.get("schema_cypher", ""),
            similarity   = round(float(mp["score"]), 4),
            success_count= int(mp.get("success_count", 1)),
        )
        schema_cypher_log.append(
            "-- Pattern retrieval Cypher (from search_similar_patterns):\n"
            "CALL db.index.vector.queryNodes('pattern_embeddings', ...)"
        )

    # ── Step 4: GraphRAG schema search ──────────────────────────────────────
    search_results = await neo4j_service.semantic_schema_search(
        query_embedding=query_embedding,
        database_id=db_id,
        top_k=12,
    )
    schema_cypher_log.append(search_results["cypher_used"])

    # Unique candidate tables from table + column vector hits
    candidate_tables: list[str] = list(
        {r["table_name"] for r in search_results["tables"]}
        | {r["table_name"] for r in search_results["columns"]}
    )
    if not candidate_tables:
        return _err(
            request.question, db_id, db_cfg.name,
            "No relevant tables found. Run: python -m ingestion.ingest_schema",
        )

    # ── Step 5: Full table/column metadata ───────────────────────────────────
    table_details = await neo4j_service.get_table_details(candidate_tables, db_id)

    # ── Step 6: FK join paths ─────────────────────────────────────────────────
    join_hints: list[str] = []
    if len(candidate_tables) > 1:
        for i in range(len(candidate_tables) - 1):
            path = await neo4j_service.get_join_path(
                candidate_tables[i], candidate_tables[i + 1], db_id
            )
            if path:
                seq  = " → ".join(path[0].get("table_sequence", []))
                conds = path[0].get("join_conditions", [])
                hint  = f"Join path: {seq}"
                if conds:
                    hint += " ON (" + ", ".join(
                        f"{c['from_col']} = {c['to_col']}"
                        for c in conds if c
                    ) + ")"
                join_hints.append(hint)
        schema_cypher_log.append(
            "-- Join path Cypher (shortestPath via FK_TO relationships)"
        )

    # ── Step 7: Cross-DB hints ────────────────────────────────────────────────
    cross_hints = await neo4j_service.get_cross_db_hints(candidate_tables, db_id)
    cross_hint_strs: list[str] = []
    for lk in cross_hints:
        cross_hint_strs.append(
            f"Note: {lk['from_table']}.{lk['from_col']} in {lk['from_db']} "
            f"links to {lk['to_table']}.{lk['to_col']} in {lk['to_db']} "
            f"({lk['description']})"
        )

    # ── Step 8: Build schema context (metadata only — no raw data) ────────────
    schema_context = _build_schema_context(
        table_details, join_hints, cross_hint_strs, db_cfg.qualified_schema
    )

    # ── Step 9: Generate SQL ──────────────────────────────────────────────────
    sql_result = await gemini_service.generate_sql(
        question=request.question,
        schema_context=schema_context,
        db_name=db_cfg.name,
        conversation_history=[t.model_dump() for t in request.conversation_history],
        matched_patterns=matched_patterns,
    )
    sql = sql_result["sql"]

    # ── Step 10: Validate SQL ─────────────────────────────────────────────────
    try:
        oracle_service.validate_read_only(sql)
    except ValueError as e:
        return _err(request.question, db_id, db_cfg.name,
                    f"Generated SQL failed safety check: {e}", sql=sql,
                    tables_used=candidate_tables)

    # Consolidated Cypher log for this request
    full_schema_cypher = "\n\n".join(schema_cypher_log)

    # ── Preview mode (skip execution) ─────────────────────────────────────────
    if not request.execute:
        return QueryResponse(
            question=request.question, db_id=db_id, sql=sql,
            summary="SQL generated — execution skipped (preview mode).",
            chart_type="none", warnings=warnings,
            schema_cypher=full_schema_cypher,
            matched_pattern=best_match,
            meta=QueryMeta(
                db_id=db_id, db_name=db_cfg.name,
                tables_used=candidate_tables, row_count=0,
                execution_ms=0, chart_type="none",
                pattern_matched=bool(best_match),
            ),
        )

    # ── Step 11: Execute SQL on Oracle ────────────────────────────────────────
    exec_start = time.time()
    try:
        exec_result: dict = await asyncio.to_thread(
            oracle_service.execute_sql, db_id, sql, request.max_rows
        )
    except Exception as e:
        return _err(
            request.question, db_id, db_cfg.name,
            f"SQL execution failed: {e}", sql=sql,
            tables_used=candidate_tables,
        )
    exec_ms = int((time.time() - exec_start) * 1000)

    columns:   list[str]   = exec_result["columns"]
    rows:      list[list]  = exec_result["rows"]
    row_count: int         = exec_result["row_count"]
    warnings.extend(exec_result.get("pii_warnings", []))

    # ── Step 12: Build output ─────────────────────────────────────────────────
    df           = output_service.build_dataframe(columns, rows)
    chart_type   = output_service.detect_chart_type(df)
    summary_stats= output_service.compute_summary_stats(df)

    # ── Step 13: Summarize (aggregate stats only → Gemini) ────────────────────
    summary = await gemini_service.summarize_results(
        question=request.question,
        columns=columns,
        row_count=row_count,
        summary_stats=summary_stats,
        db_name=db_cfg.name,
    )

    # ── Step 14: Store QueryPattern in background (preserves schema_cypher) ───
    background_tasks.add_task(
        _store_pattern_bg,
        db_id=db_id,
        nl_question=request.question,
        sql=exec_result["sql_executed"],
        schema_cypher=full_schema_cypher,
        tables_used=candidate_tables,
        execution_ms=exec_ms,
        embedding=query_embedding,
    )

    return QueryResponse(
        question=request.question,
        db_id=db_id,
        sql=exec_result["sql_executed"],
        columns=columns,
        rows=rows,
        summary=summary,
        chart_type=chart_type,
        warnings=warnings,
        matched_pattern=best_match,
        schema_cypher=full_schema_cypher,
        meta=QueryMeta(
            db_id=db_id,
            db_name=db_cfg.name,
            tables_used=candidate_tables,
            row_count=row_count,
            execution_ms=exec_ms,
            chart_type=chart_type,
            pattern_matched=bool(best_match),
        ),
    )


# ── Background task: store pattern after response is sent ─────────────────────

async def _store_pattern_bg(
    db_id: str, nl_question: str, sql: str, schema_cypher: str,
    tables_used: list[str], execution_ms: int, embedding: list[float],
) -> None:
    """Fire-and-forget: persist successful NL→SQL as a QueryPattern node."""
    try:
        await neo4j_service.store_query_pattern(
            database_id=db_id,
            nl_question=nl_question,
            sql=sql,
            schema_cypher=schema_cypher,
            tables_used=tables_used,
            execution_ms=execution_ms,
            embedding=embedding,
        )
    except Exception:
        pass   # never block the response on pattern storage failures


# ── Helpers ────────────────────────────────────────────────────────────────────

def _build_schema_context(
    table_details: list[dict],
    join_hints:    list[str],
    cross_hints:   list[str],
    schema_name:   str,
) -> str:
    """
    Assemble compact schema context for Gemini SQL prompt.
    Contains ONLY metadata — qualified table names, column names,
    data types, enriched descriptions, PK/index flags, PII flags.
    No actual data values are ever included.
    """
    parts: list[str] = []
    for t in table_details:
        qualified = f"{schema_name}.{t['table_name']}"
        view_tag  = " [VIEW]" if t.get("is_view") else ""
        rc        = t.get("row_count_approx", 0)
        rc_hint   = f" (~{rc:,} rows)" if rc else ""
        dom       = f" | Domain: {t['domain_name']}" if t.get("domain_name") else ""
        pk_cols   = t.get("pk_columns") or []

        col_lines: list[str] = []
        for c in t.get("columns", []):
            tags: list[str] = []
            if c.get("is_pk"):        tags.append("PK")
            if c.get("is_unique"):    tags.append("UNIQUE")
            if c.get("is_indexed"):   tags.append("INDEXED")
            if c.get("is_pii"):       tags.append("⚠PII")
            card = c.get("cardinality_hint", "")
            if card not in ("", "unknown"):
                tags.append(f"cardinality:{card}")
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            col_lines.append(
                f"    - {c['name']} ({c['data_type']}){tag_str}"
                f" → [{c.get('label', c['name'])}] {c.get('description', '')}"
            )

        parts.append(
            f"Table: {qualified}{view_tag}{rc_hint}{dom}\n"
            f"Description: {t.get('table_description', 'N/A')}\n"
            + (f"Primary key: ({', '.join(pk_cols)})\n" if pk_cols else "")
            + f"Columns:\n" + "\n".join(col_lines)
        )

    context = "\n\n".join(parts)
    if join_hints:
        context += "\n\nJoin hints (FK paths):\n" + "\n".join(f"  • {h}" for h in join_hints)
    if cross_hints:
        context += "\n\nCross-database links:\n" + "\n".join(f"  ⬡ {h}" for h in cross_hints)
    return context


def _err(
    question: str,
    db_id:    str,
    db_name:  str,
    msg:      str,
    sql:      str = "",
    tables_used: list[str] | None = None,
) -> QueryResponse:
    return QueryResponse(
        question=question, db_id=db_id, sql=sql,
        summary="", chart_type="none", warnings=[], error=msg,
        schema_cypher="",
        meta=QueryMeta(
            db_id=db_id, db_name=db_name,
            tables_used=tables_used or [],
            row_count=0, execution_ms=0, chart_type="none",
        ),
    )
