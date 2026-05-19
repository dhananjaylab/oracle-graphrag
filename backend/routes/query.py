"""
backend/routes/query.py  (v2 + Phase 3A agents)

Full pipeline — 14 steps + agent gates:

  1.  Resolve db_id
  2.  Embed question              → gemini-embedding-001
  3.  Search QueryPatterns        → reuse stored SQL/Cypher if similar ≥ 0.85
  4.  GraphRAG semantic search    → neo4j vector search (db-scoped)
  5.  Get table details           → column + domain metadata
  6.  Find FK join paths          → neo4j graph traversal
  7.  Get cross-DB hints          → CROSS_DB_JOIN edges
  8.  Build schema context        → metadata string only (no raw data)
  9.  Generate SQL                → gemini (+ matched patterns as few-shots)
  [NEW] 10. ValidationAgent       → sqlglot · EXPLAIN PLAN · read-only guard
  [NEW] 10a. SelfHealingAgent     → triggered if validation fails or ORA-* raised
  11. Execute SQL on Oracle       → on-prem (may be inside healing loop)
  12. Build output                → DataFrame, chart type, summary stats
  13. Summarize results           → gemini (aggregate stats only)
  14. Store QueryPattern          → neo4j background task (Cypher preserved)
"""

import asyncio
import time
from fastapi import APIRouter, BackgroundTasks

from backend.db_manager import db_manager
from backend.models import (
    QueryRequest, QueryResponse, QueryMeta,
    MatchedPattern, AgentTrace,
    ValidationResult as ValidationResultModel,
    HealingAttemptModel,
)
from backend.agents.validation_agent import validation_agent, ValidationResult
from backend.agents.self_healing_agent import self_healing_agent
from backend.services import oracle_service, neo4j_service, gemini_service, output_service

router = APIRouter()


@router.post("/query", response_model=QueryResponse)
async def query(request: QueryRequest, background_tasks: BackgroundTasks):

    # ── Step 1: Resolve database ────────────────────────────────────────────
    db_id = request.db_id or db_manager.get_default_id()
    try:
        db_cfg = db_manager.get_config(db_id)
    except ValueError as e:
        return _err(request.question, db_id, "", str(e))

    warnings: list[str] = []
    cypher_log: list[str] = []

    # ── Step 2: Embed question ──────────────────────────────────────────────
    query_embedding: list[float] = await asyncio.to_thread(
        gemini_service.get_embedding, request.question
    )

    # ── Step 3: Search stored QueryPatterns ────────────────────────────────
    matched_patterns = await neo4j_service.search_similar_patterns(
        query_embedding=query_embedding,
        database_id=db_id,
        top_k=3,
        min_similarity=0.85,
    )
    best_match: MatchedPattern | None = None
    if matched_patterns:
        mp = matched_patterns[0]
        best_match = MatchedPattern(
            nl_question   = mp["nl_question"],
            sql           = mp["sql"],
            schema_cypher = mp.get("schema_cypher", ""),
            similarity    = round(float(mp["score"]), 4),
            success_count = int(mp.get("success_count", 1)),
        )
        cypher_log.append("-- Pattern retrieval:\nCALL db.index.vector.queryNodes('pattern_embeddings', ...)")

    # ── Step 4: GraphRAG semantic search ────────────────────────────────────
    search_results = await neo4j_service.semantic_schema_search(
        query_embedding=query_embedding, database_id=db_id, top_k=12,
    )
    cypher_log.append(search_results["cypher_used"])

    candidate_tables: list[str] = list(
        {r["table_name"] for r in search_results["tables"]}
        | {r["table_name"] for r in search_results["columns"]}
    )
    if not candidate_tables:
        return _err(
            request.question, db_id, db_cfg.name,
            "No relevant tables found in schema graph. "
            "Run: python -m ingestion.ingest_schema",
        )

    # ── Step 5: Full table/column metadata ─────────────────────────────────
    table_details = await neo4j_service.get_table_details(candidate_tables, db_id)

    # ── Step 6: FK join paths ───────────────────────────────────────────────
    join_hints: list[str] = []
    if len(candidate_tables) > 1:
        for i in range(len(candidate_tables) - 1):
            path = await neo4j_service.get_join_path(
                candidate_tables[i], candidate_tables[i + 1], db_id
            )
            if path:
                seq   = " → ".join(path[0].get("table_sequence", []))
                conds = path[0].get("join_conditions", [])
                hint  = f"Join path: {seq}"
                if conds:
                    hint += " ON (" + ", ".join(
                        f"{c['from_col']} = {c['to_col']}" for c in conds if c
                    ) + ")"
                join_hints.append(hint)
        cypher_log.append("-- Join paths: shortestPath via FK_TO relationships")

    # ── Step 7: Cross-DB hints ──────────────────────────────────────────────
    cross_hints = await neo4j_service.get_cross_db_hints(candidate_tables, db_id)
    cross_hint_strs: list[str] = [
        f"Note: {lk['from_table']}.{lk['from_col']} in {lk['from_db']} "
        f"links to {lk['to_table']}.{lk['to_col']} in {lk['to_db']} "
        f"({lk['description']})"
        for lk in cross_hints
    ]

    # ── Step 8: Build schema context (metadata only — no raw data) ──────────
    schema_context = _build_schema_context(
        table_details, join_hints, cross_hint_strs, db_cfg.qualified_schema
    )

    # ── Step 9: Generate SQL ────────────────────────────────────────────────
    sql_result = await gemini_service.generate_sql(
        question             = request.question,
        schema_context       = schema_context,
        db_name              = db_cfg.name,
        conversation_history = [t.model_dump() for t in request.conversation_history],
        matched_patterns     = matched_patterns,
    )
    sql = sql_result["sql"]

    # ── Step 10: ValidationAgent ────────────────────────────────────────────
    val_result: ValidationResult = await asyncio.to_thread(
        validation_agent.validate,
        db_id,
        sql,
        request.max_rows,
        request.skip_explain_plan,
    )
    warnings.extend(val_result.warnings)

    full_cypher = "\n\n".join(cypher_log)

    # Agent trace — populated throughout
    agent_trace = AgentTrace(
        validation=ValidationResultModel(
            valid         = val_result.valid,
            sql           = val_result.sql,
            issues        = [
                {"severity": i.severity, "code": i.code,
                 "message": i.message, "line": i.line}
                for i in val_result.issues
            ],
            warnings      = val_result.warnings,
            cost_estimate = val_result.cost_estimate,
            cost_blocked  = val_result.cost_blocked,
        ),
        healed=False,
    )

    # ── Preview mode (skip execution) ───────────────────────────────────────
    if not request.execute:
        return QueryResponse(
            question       = request.question,
            db_id          = db_id,
            sql            = val_result.sql if val_result.valid else sql,
            summary        = "SQL generated — execution skipped (preview mode).",
            chart_type     = "none",
            warnings       = warnings,
            schema_cypher  = full_cypher,
            matched_pattern= best_match,
            agent_trace    = agent_trace,
            meta           = QueryMeta(
                db_id=db_id, db_name=db_cfg.name,
                tables_used=candidate_tables, row_count=0,
                execution_ms=0, chart_type="none",
                pattern_matched=bool(best_match), healed=False,
            ),
        )

    # ── Step 10a + 11: Execute (with healing fallback) ──────────────────────
    if not db_cfg.is_configured:
        return _err(
            request.question, db_id, db_cfg.name,
            f"Missing credentials for '{db_id}'. Set {db_cfg.env_prefix}_USER, {db_cfg.env_prefix}_PASSWORD, {db_cfg.env_prefix}_DSN in .env",
            sql=val_result.sql if val_result.valid else sql,
            tables_used=candidate_tables,
            agent_trace=agent_trace,
        )

    exec_result: dict | None = None
    healed = False
    exec_start = time.time()

    if val_result.valid:
        # Happy path — execute directly
        sql = val_result.sql   # use limit-injected / PII-masked version
        try:
            exec_result = await asyncio.to_thread(
                oracle_service.execute_sql, db_id, sql, request.max_rows
            )
        except Exception as exc:
            # Oracle raised at runtime — hand off to SelfHealingAgent
            oracle_error = str(exc)
            warnings.append(f"SQL execution failed: {oracle_error}. Attempting auto-recovery…")
            exec_result, sql, healed, heal_attempts = await _heal_and_execute(
                db_id=db_id,
                original_question=request.question,
                failed_sql=sql,
                error=oracle_error,
                schema_context=schema_context,
                db_name=db_cfg.name,
                max_rows=request.max_rows,
            )
            agent_trace.healing_attempts = heal_attempts
            agent_trace.healed = healed
            if exec_result is None:
                return _err(
                    request.question, db_id, db_cfg.name,
                    f"Query failed after {len(heal_attempts)} recovery attempt(s): {oracle_error}",
                    sql=sql, tables_used=candidate_tables, agent_trace=agent_trace,
                )
    else:
        # Validation rejected — hand off to SelfHealingAgent immediately
        val_error = val_result.error_summary
        warnings.append(f"Validation failed: {val_error}. Attempting auto-recovery…")
        exec_result, sql, healed, heal_attempts = await _heal_and_execute(
            db_id=db_id,
            original_question=request.question,
            failed_sql=sql,
            error=val_error,
            schema_context=schema_context,
            db_name=db_cfg.name,
            max_rows=request.max_rows,
        )
        agent_trace.healing_attempts = heal_attempts
        agent_trace.healed = healed
        if exec_result is None:
            return _err(
                request.question, db_id, db_cfg.name,
                f"Could not generate valid SQL after {len(heal_attempts)} attempt(s).",
                sql=sql, tables_used=candidate_tables, agent_trace=agent_trace,
            )

    exec_ms = int((time.time() - exec_start) * 1000)

    columns:   list[str]  = exec_result["columns"]
    rows:      list[list] = exec_result["rows"]
    row_count: int        = exec_result["row_count"]
    warnings.extend(exec_result.get("pii_warnings", []))
    agent_trace.total_attempts = len(agent_trace.healing_attempts) + 1

    # ── Step 12: Build output ───────────────────────────────────────────────
    df            = output_service.build_dataframe(columns, rows)
    chart_type    = output_service.detect_chart_type(df)
    summary_stats = output_service.compute_summary_stats(df)

    # ── Step 13: Summarize (aggregate stats → Gemini, no raw rows) ──────────
    summary = await gemini_service.summarize_results(
        question=request.question, columns=columns,
        row_count=row_count, summary_stats=summary_stats, db_name=db_cfg.name,
    )

    # ── Step 14: Store QueryPattern in background ───────────────────────────
    final_sql = exec_result["sql_executed"]
    background_tasks.add_task(
        _store_pattern_bg,
        db_id=db_id, nl_question=request.question,
        sql=final_sql, schema_cypher=full_cypher,
        tables_used=candidate_tables,
        execution_ms=exec_ms, embedding=query_embedding,
    )

    return QueryResponse(
        question        = request.question,
        db_id           = db_id,
        sql             = final_sql,
        columns         = columns,
        rows            = rows,
        summary         = summary,
        chart_type      = chart_type,
        warnings        = warnings,
        matched_pattern = best_match,
        schema_cypher   = full_cypher,
        agent_trace     = agent_trace,
        meta            = QueryMeta(
            db_id           = db_id,
            db_name         = db_cfg.name,
            tables_used     = candidate_tables,
            row_count       = row_count,
            execution_ms    = exec_ms,
            chart_type      = chart_type,
            pattern_matched = bool(best_match),
            healed          = healed,
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def _heal_and_execute(
    db_id: str, original_question: str, failed_sql: str,
    error: str, schema_context: str, db_name: str, max_rows: int,
) -> tuple[dict | None, str, bool, list[HealingAttemptModel]]:
    """
    Delegate to SelfHealingAgent. Returns (exec_result, sql, healed, attempts).
    exec_result is None if all retries were exhausted.
    """
    healing = await self_healing_agent.heal(
        db_id=db_id, original_question=original_question,
        failed_sql=failed_sql, error=error,
        schema_context=schema_context, db_name=db_name, max_rows=max_rows,
    )

    heal_attempts = [
        HealingAttemptModel(
            attempt    = a.attempt,
            error_code = a.error_code,
            sql_tried  = a.sql_tried,
            outcome    = a.outcome,
            error_msg  = a.error_msg,
        )
        for a in healing.healing_attempts
    ]

    if healing.success and healing.exec_result:
        return healing.exec_result, healing.sql, True, heal_attempts

    return None, healing.sql, False, heal_attempts


async def _store_pattern_bg(
    db_id: str, nl_question: str, sql: str, schema_cypher: str,
    tables_used: list[str], execution_ms: int, embedding: list[float],
) -> None:
    try:
        await neo4j_service.store_query_pattern(
            database_id=db_id, nl_question=nl_question,
            sql=sql, schema_cypher=schema_cypher,
            tables_used=tables_used, execution_ms=execution_ms,
            embedding=embedding,
        )
    except Exception:
        pass   # never block the response on pattern storage


def _build_schema_context(
    table_details: list[dict],
    join_hints:    list[str],
    cross_hints:   list[str],
    schema_name:   str,
) -> str:
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
            if c.get("is_pk"):      tags.append("PK")
            if c.get("is_unique"):  tags.append("UNIQUE")
            if c.get("is_indexed"): tags.append("INDEXED")
            if c.get("is_pii"):     tags.append("⚠PII")
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
            + "Columns:\n" + "\n".join(col_lines)
        )

    context = "\n\n".join(parts)
    if join_hints:
        context += "\n\nJoin hints (FK paths):\n" + "\n".join(f"  • {h}" for h in join_hints)
    if cross_hints:
        context += "\n\nCross-database links:\n" + "\n".join(f"  ⬡ {h}" for h in cross_hints)
    return context


def _err(
    question: str, db_id: str, db_name: str, msg: str,
    sql: str = "", tables_used: list[str] | None = None,
    agent_trace: AgentTrace | None = None,
) -> QueryResponse:
    return QueryResponse(
        question=question, db_id=db_id, sql=sql,
        summary="", chart_type="none", warnings=[], error=msg,
        schema_cypher="", agent_trace=agent_trace,
        meta=QueryMeta(
            db_id=db_id, db_name=db_name,
            tables_used=tables_used or [], row_count=0,
            execution_ms=0, chart_type="none",
        ),
    )
