"""
backend/routes/query.py  (v2 + Phase 3A agents + Phase 3B MCP clients)

All Neo4j and Oracle I/O now flows through MCP clients.
Each client falls back to its direct service on MCP failure,
so the pipeline degrades gracefully without hard errors.

Pipeline steps
──────────────
 1.  Resolve db_id
 2.  Embed question              gemini-embedding-001
 3.  Search QueryPatterns        neo4j_mcp.search_patterns()
 4.  GraphRAG semantic search    neo4j_mcp.semantic_search()
 5.  Get table details           neo4j_mcp.get_table_details()
 6.  FK join paths               neo4j_mcp.get_join_path()
 7.  Cross-DB hints              neo4j_mcp.get_cross_db_hints()
 8.  Build schema context        (local, no I/O)
 9.  Generate SQL                gemini_service.generate_sql()
[3A] 10. ValidationAgent         sqlglot · EXPLAIN PLAN · read-only guard
[3A] 10a.SelfHealingAgent        triggered on validation or Oracle failure
 11. Execute SQL                 oracle_mcp.execute_query()
 12. Build output                (local: DataFrame, chart, stats)
 13. Summarize results           gemini_service.summarize_results()
 14. Store QueryPattern          neo4j_mcp.store_pattern() [background]
"""

import asyncio
import time
from fastapi import APIRouter, BackgroundTasks

from backend.db_manager import db_manager
from backend.mcp_client import oracle_mcp, neo4j_mcp
from backend.models import (
    QueryRequest, QueryResponse, QueryMeta,
    MatchedPattern, AgentTrace,
    ValidationResult as ValidationResultModel,
    HealingAttemptModel,
)
from backend.agents.validation_agent import validation_agent, ValidationResult
from backend.agents.self_healing_agent import self_healing_agent
from backend.services import gemini_service, output_service

router = APIRouter()


# ══════════════════════════════════════════════════════════════════════════════
# POST /api/query
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/query", response_model=QueryResponse)
async def query(request: QueryRequest, background_tasks: BackgroundTasks):

    # ── Step 1: Resolve database ────────────────────────────────────────────
    db_id = request.db_id or db_manager.get_default_id()
    try:
        db_cfg = db_manager.get_config(db_id)
    except ValueError as exc:
        return _err(request.question, db_id, "", str(exc))

    warnings:    list[str] = []
    cypher_log:  list[str] = []

    # ── Step 2: Embed question ──────────────────────────────────────────────
    query_embedding: list[float] = await asyncio.to_thread(
        gemini_service.get_embedding, request.question
    )

    # ── Step 3: Search stored QueryPatterns (via Neo4j MCP) ────────────────
    matched_patterns: list[dict] = await neo4j_mcp.search_patterns(
        query_embedding = query_embedding,
        database_id     = db_id,
        top_k           = 3,
        min_similarity  = 0.85,
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
        cypher_log.append(
            "-- Pattern retrieval (Neo4j MCP: search_patterns):\n"
            "CALL db.index.vector.queryNodes('pattern_embeddings', $k, $embedding) …"
        )

    # ── Step 4: GraphRAG semantic search (via Neo4j MCP) ───────────────────
    search_results: dict = await neo4j_mcp.semantic_search(
        query_embedding = query_embedding,
        database_id     = db_id,
        top_k           = 12,
    )
    cypher_log.append(search_results.get("cypher_used", ""))

    candidate_tables: list[str] = list(
        {r["table_name"] for r in search_results.get("tables",  [])}
        | {r["table_name"] for r in search_results.get("columns", [])}
    )
    if not candidate_tables:
        return _err(
            request.question, db_id, db_cfg.name,
            "No relevant tables found. Run: python -m ingestion.ingest_schema",
        )

    # ── Step 5: Full table/column metadata (via Neo4j MCP) ─────────────────
    table_details: list[dict] = await neo4j_mcp.get_table_details(
        table_names = candidate_tables,
        database_id = db_id,
    )

    # ── Step 6: FK join paths (via Neo4j MCP) ──────────────────────────────
    join_hints: list[str] = []
    if len(candidate_tables) > 1:
        for i in range(len(candidate_tables) - 1):
            path = await neo4j_mcp.get_join_path(
                table1      = candidate_tables[i],
                table2      = candidate_tables[i + 1],
                database_id = db_id,
            )
            if path:
                seq   = " → ".join(path[0].get("table_sequence", []))
                conds = path[0].get("join_conditions", [])
                hint  = f"Join path: {seq}"
                if conds:
                    hint += " ON (" + ", ".join(
                        f"{c['from_col']} = {c['to_col']}"
                        for c in conds if c
                    ) + ")"
                join_hints.append(hint)
        cypher_log.append(
            "-- Join paths (Neo4j MCP: get_join_path):\n"
            "shortestPath via [:FK_TO*1..5] relationships"
        )

    # ── Step 7: Cross-DB hints (via Neo4j MCP) ─────────────────────────────
    cross_db_links: list[dict] = await neo4j_mcp.get_cross_db_hints(
        table_names = candidate_tables,
        database_id = db_id,
    )
    cross_hints: list[str] = [
        f"Note: {lk['from_table']}.{lk['from_col']} in {lk['from_db']} "
        f"links to {lk['to_table']}.{lk['to_col']} in {lk['to_db']} "
        f"({lk['description']})"
        for lk in cross_db_links
    ]

    # ── Step 8: Build schema context ────────────────────────────────────────
    schema_context = _build_schema_context(
        table_details, join_hints, cross_hints, db_cfg.qualified_schema
    )

    # ── Step 9: Generate SQL (Gemini) ───────────────────────────────────────
    sql_result = await gemini_service.generate_sql(
        question             = request.question,
        schema_context       = schema_context,
        db_name              = db_cfg.name,
        conversation_history = [t.model_dump() for t in request.conversation_history],
        matched_patterns     = matched_patterns,
    )
    sql = sql_result["sql"]

    # ── Step 10: ValidationAgent (Phase 3A) ─────────────────────────────────
    val_result: ValidationResult = await asyncio.to_thread(
        validation_agent.validate,
        db_id,
        sql,
        request.max_rows,
        request.skip_explain_plan,
    )
    warnings.extend(val_result.warnings)

    full_cypher = "\n\n".join(filter(None, cypher_log))

    agent_trace = AgentTrace(
        validation = ValidationResultModel(
            valid         = val_result.valid,
            sql           = val_result.sql,
            issues        = [
                {"severity": i.severity, "code": i.code,
                 "message":  i.message,  "line": i.line}
                for i in val_result.issues
            ],
            warnings      = val_result.warnings,
            cost_estimate = val_result.cost_estimate,
            cost_blocked  = val_result.cost_blocked,
        ),
        healed = False,
    )

    # ── Preview mode ────────────────────────────────────────────────────────
    if not request.execute:
        return QueryResponse(
            question        = request.question,
            db_id           = db_id,
            sql             = val_result.sql if val_result.valid else sql,
            summary         = "SQL generated — execution skipped (preview mode).",
            chart_type      = "none",
            warnings        = warnings,
            schema_cypher   = full_cypher,
            matched_pattern = best_match,
            agent_trace     = agent_trace,
            meta            = QueryMeta(
                db_id=db_id, db_name=db_cfg.name,
                tables_used=candidate_tables, row_count=0,
                execution_ms=0, chart_type="none",
                pattern_matched=bool(best_match), healed=False,
            ),
        )

    # ── Steps 10a + 11: Execute (with healing fallback) ─────────────────────
    exec_result: dict | None = None
    healed = False
    exec_start = time.time()

    if val_result.valid:
        sql = val_result.sql   # limit-injected + PII-masked version
        try:
            # Execute via Oracle MCP (Phase 3B)
            exec_result = await oracle_mcp.execute_query(db_id, sql, request.max_rows)
            if "error" in exec_result:
                raise RuntimeError(exec_result["error"])
        except Exception as exc:
            oracle_error = str(exc)
            warnings.append(f"Execution failed: {oracle_error}. Attempting recovery…")
            exec_result, sql, healed, heal_attempts = await _heal_and_execute(
                db_id=db_id, original_question=request.question,
                failed_sql=sql, error=oracle_error,
                schema_context=schema_context, db_name=db_cfg.name,
                max_rows=request.max_rows,
            )
            agent_trace.healing_attempts = heal_attempts
            agent_trace.healed = healed
            if exec_result is None:
                return _err(
                    request.question, db_id, db_cfg.name,
                    f"Query failed after {len(heal_attempts)} attempt(s): {oracle_error}",
                    sql=sql, tables_used=candidate_tables, agent_trace=agent_trace,
                )
    else:
        val_error = val_result.error_summary
        warnings.append(f"Validation failed: {val_error}. Attempting recovery…")
        exec_result, sql, healed, heal_attempts = await _heal_and_execute(
            db_id=db_id, original_question=request.question,
            failed_sql=sql, error=val_error,
            schema_context=schema_context, db_name=db_cfg.name,
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

    # ── Step 12: Build output ────────────────────────────────────────────────
    df            = output_service.build_dataframe(columns, rows)
    chart_type    = output_service.detect_chart_type(df)
    summary_stats = output_service.compute_summary_stats(df)

    # ── Step 13: Summarize (Gemini, aggregate stats only) ───────────────────
    summary = await gemini_service.summarize_results(
        question      = request.question,
        columns       = columns,
        row_count     = row_count,
        summary_stats = summary_stats,
        db_name       = db_cfg.name,
    )

    # ── Step 14: Store QueryPattern (via Neo4j MCP, background) ─────────────
    final_sql = exec_result.get("sql_executed", sql)
    background_tasks.add_task(
        _store_pattern_bg,
        db_id         = db_id,
        nl_question   = request.question,
        sql           = final_sql,
        schema_cypher = full_cypher,
        tables_used   = candidate_tables,
        execution_ms  = exec_ms,
        embedding     = query_embedding,
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
    """Delegate to SelfHealingAgent and return (exec_result, sql, healed, attempts)."""
    healing = await self_healing_agent.heal(
        db_id             = db_id,
        original_question = original_question,
        failed_sql        = failed_sql,
        error             = error,
        schema_context    = schema_context,
        db_name           = db_name,
        max_rows          = max_rows,
    )
    attempts = [
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
        return healing.exec_result, healing.sql, True, attempts
    return None, healing.sql, False, attempts


async def _store_pattern_bg(
    db_id: str, nl_question: str, sql: str, schema_cypher: str,
    tables_used: list[str], execution_ms: int, embedding: list[float],
) -> None:
    """Fire-and-forget via Neo4j MCP — never blocks the response."""
    try:
        await neo4j_mcp.store_pattern(
            database_id   = db_id,
            nl_question   = nl_question,
            sql           = sql,
            schema_cypher = schema_cypher,
            tables_used   = tables_used,
            execution_ms  = execution_ms,
            embedding     = embedding,
        )
    except Exception:
        pass


def _build_schema_context(
    table_details: list[dict],
    join_hints:    list[str],
    cross_hints:   list[str],
    schema_name:   str,
) -> str:
    """Assemble schema context for Gemini — metadata only, no raw data."""
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

    ctx = "\n\n".join(parts)
    if join_hints:
        ctx += "\n\nJoin hints:\n" + "\n".join(f"  • {h}" for h in join_hints)
    if cross_hints:
        ctx += "\n\nCross-database links:\n" + "\n".join(f"  ⬡ {h}" for h in cross_hints)
    return ctx


def _err(
    question: str, db_id: str, db_name: str, msg: str,
    sql: str = "", tables_used: list[str] | None = None,
    agent_trace: AgentTrace | None = None,
) -> QueryResponse:
    return QueryResponse(
        question    = question,
        db_id       = db_id,
        sql         = sql,
        summary     = "",
        chart_type  = "none",
        warnings    = [],
        error       = msg,
        schema_cypher = "",
        agent_trace = agent_trace,
        meta        = QueryMeta(
            db_id       = db_id,
            db_name     = db_name,
            tables_used = tables_used or [],
            row_count   = 0,
            execution_ms= 0,
            chart_type  = "none",
        ),
    )
