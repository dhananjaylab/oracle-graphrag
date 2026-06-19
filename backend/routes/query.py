"""
backend/routes/query.py  (Phase 4A + 4B)

Phase 4A — parallel pipeline:
  Steps 3 + 4  search_patterns + semantic_search run with asyncio.gather
  Steps 6 + 7  get_join_paths_batch + get_cross_db_hints run with asyncio.gather
               (batch join replaces the previous O(N) sequential loop)

Phase 4B — three caches:
  EmbeddingCache  — skip Gemini embed API for repeated questions
  SchemaCache     — skip Neo4j table_details + join_paths for known table sets
  ResultCache     — skip Oracle execution for identical SQL within TTL

Schema context token budget:
  _trim_schema_context() drops low-priority tables when the assembled
  context would exceed MAX_SCHEMA_TOKENS, always preserving PKs and
  indexed columns on tables that remain.

All Phase 3A/3B agent behaviour (ValidationAgent, SelfHealingAgent,
MCP fallbacks) is preserved unchanged.

Pipeline steps
──────────────
 1.  Resolve db_id
 2.  Embed question              embedding_cache → gemini_service.get_embedding
[P]  3+4. search_patterns + semantic_search     asyncio.gather (parallel)
 5.  Get table details           schema_cache → neo4j_mcp.get_table_details
[P]  6+7. get_join_paths_batch + get_cross_db_hints  asyncio.gather
 8.  Build schema context        (local, token-budget trimmed)
 9.  Generate SQL                gemini_service.generate_sql
[3A] 10. ValidationAgent
[3A] 10a. SelfHealingAgent       triggered on validation / Oracle failure
[4B] 11. result_cache check      skip Oracle if identical SQL seen recently
 11. Execute SQL                 oracle_mcp.execute_query
 12. Build output
 13. Summarize results           gemini_service.summarize_results
 14. Store QueryPattern          neo4j_mcp.store_pattern [background]
"""

import asyncio
import logging
import os
import time
from fastapi import APIRouter, BackgroundTasks

logger = logging.getLogger(__name__)

from backend.db_manager import db_manager
from backend.mcp_client import oracle_mcp, neo4j_mcp
from backend.cache import embedding_cache, schema_cache, result_cache
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

# Schema context token budget (rough: 1 token ≈ 4 chars)
MAX_SCHEMA_TOKENS = int(os.getenv("MAX_SCHEMA_TOKENS", "8000"))
_CHARS_PER_TOKEN  = 4


# ══════════════════════════════════════════════════════════════════════════════
# POST /api/query
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/query", response_model=QueryResponse)
async def query(request: QueryRequest, background_tasks: BackgroundTasks):

    t_pipeline_start = time.monotonic()

    # ── Step 1: Resolve database ─────────────────────────────────────────────
    db_id = request.db_id or db_manager.get_default_id()
    try:
        db_cfg = db_manager.get_config(db_id)
    except ValueError as exc:
        return _err(request.question, db_id, "", str(exc))

    warnings:    list[str] = []
    cypher_log:  list[str] = []
    cache_flags: dict[str, bool] = {
        "embedding": False,
        "schema":    False,
        "result":    False,
    }

    # ── Step 2: Embed question (with EmbeddingCache) ─────────────────────────
    query_embedding: list[float] | None = embedding_cache.get(request.question)
    if query_embedding is None:
        query_embedding = await asyncio.to_thread(
            gemini_service.get_embedding, request.question
        )
        embedding_cache.set(request.question, query_embedding)
    else:
        cache_flags["embedding"] = True

    # ── Steps 3 + 4: PARALLEL — search_patterns + semantic_search ───────────
    matched_patterns_raw, search_results = await asyncio.gather(
        neo4j_mcp.search_patterns(
            query_embedding = query_embedding,
            database_id     = db_id,
            top_k           = 3,
            min_similarity  = 0.85,
        ),
        neo4j_mcp.semantic_search(
            query_embedding = query_embedding,
            database_id     = db_id,
            top_k           = 12,
        ),
    )

    # Resolve best matched pattern
    best_match: MatchedPattern | None = None
    if matched_patterns_raw:
        mp = matched_patterns_raw[0]
        best_match = MatchedPattern(
            nl_question   = mp["nl_question"],
            sql           = mp["sql"],
            schema_cypher = mp.get("schema_cypher", ""),
            similarity    = round(float(mp["score"]), 4),
            success_count = int(mp.get("success_count", 1)),
        )
        cypher_log.append(
            "-- Pattern retrieval (parallel, Neo4j MCP: search_patterns):\n"
            "CALL db.index.vector.queryNodes('pattern_embeddings', $k, $embedding) …"
        )

    # Normalise search_results — MCP may return a raw string on transient errors;
    # treat anything that isn't a dict as an empty result so we fail gracefully.
    if not isinstance(search_results, dict):
        logger.warning(
            "[query] semantic_search returned unexpected type %s — treating as empty",
            type(search_results).__name__,
        )
        search_results = {}

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

    # ── Step 5: Table details (SchemaCache) ──────────────────────────────────
    table_details: list[dict] | None = schema_cache.get_details(db_id, candidate_tables)
    if table_details is None:
        table_details = await neo4j_mcp.get_table_details(
            table_names = candidate_tables,
            database_id = db_id,
        )
        schema_cache.set_details(db_id, candidate_tables, table_details)
    else:
        cache_flags["schema"] = True

    # ── Steps 6 + 7: PARALLEL — get_join_paths_batch + get_cross_db_hints ───
    join_paths_cached = schema_cache.get_join_paths(db_id, candidate_tables)

    if len(candidate_tables) > 1:
        if join_paths_cached is not None:
            # Join paths from cache — only need cross-db hints live
            join_paths_raw = join_paths_cached
            cross_db_links = await neo4j_mcp.get_cross_db_hints(
                table_names = candidate_tables,
                database_id = db_id,
            )
        else:
            # Both calls live — run in parallel
            join_paths_raw, cross_db_links = await asyncio.gather(
                neo4j_mcp.get_join_paths_batch(
                    table_names = candidate_tables,
                    database_id = db_id,
                ),
                neo4j_mcp.get_cross_db_hints(
                    table_names = candidate_tables,
                    database_id = db_id,
                ),
            )
            schema_cache.set_join_paths(db_id, candidate_tables, join_paths_raw)
            cypher_log.append(
                "-- Join paths batch (parallel, Neo4j MCP: get_join_paths_batch):\n"
                "shortestPath via [:FK_TO*1..5] — all table pairs in one query"
            )
    else:
        join_paths_raw = []
        cross_db_links = await neo4j_mcp.get_cross_db_hints(
            table_names = candidate_tables,
            database_id = db_id,
        )

    # Build join hints from batch results
    join_hints: list[str] = _build_join_hints(join_paths_raw)

    # Build cross-DB hint strings
    cross_hints: list[str] = [
        f"Note: {lk['from_table']}.{lk['from_col']} in {lk['from_db']} "
        f"links to {lk['to_table']}.{lk['to_col']} in {lk['to_db']} "
        f"({lk['description']})"
        for lk in cross_db_links
    ]

    # ── Step 8: Build schema context (token-budget trimmed) ──────────────────
    schema_context, trimmed = _build_schema_context(
        table_details  = table_details,
        join_hints     = join_hints,
        cross_hints    = cross_hints,
        schema_name    = db_cfg.qualified_schema,
        max_tokens     = MAX_SCHEMA_TOKENS,
        search_results = search_results,
    )
    if trimmed:
        warnings.append(
            f"⚠ Schema context trimmed to ~{MAX_SCHEMA_TOKENS:,} tokens "
            f"({len(table_details)} tables, lower-ranked ones truncated)"
        )

    # ── Step 9: Generate SQL ─────────────────────────────────────────────────
    sql_result = await gemini_service.generate_sql(
        question             = request.question,
        schema_context       = schema_context,
        db_name              = db_cfg.name,
        conversation_history = [t.model_dump() for t in request.conversation_history],
        matched_patterns     = matched_patterns_raw,
    )
    sql = sql_result["sql"]

    # ── Step 10: ValidationAgent ─────────────────────────────────────────────
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

    # ── Preview mode ─────────────────────────────────────────────────────────
    if not request.execute:
        cache_hit = any(cache_flags.values())
        cache_source = _cache_source_label(cache_flags)
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
                db_id           = db_id,
                db_name         = db_cfg.name,
                tables_used     = candidate_tables,
                row_count       = 0,
                execution_ms    = 0,
                chart_type      = "none",
                pattern_matched = bool(best_match),
                healed          = False,
                cache_hit       = cache_hit,
                cache_source    = cache_source,
            ),
        )

    # ── Steps 10a + 11: Execute (with result cache + healing fallback) ────────
    exec_result: dict | None = None
    healed    = False
    exec_ms   = 0
    from_result_cache = False
    exec_start = time.monotonic()

    if val_result.valid:
        sql = val_result.sql   # limit-injected + PII-masked

        # ── Result cache check ────────────────────────────────────────────
        cached_result = result_cache.get(db_id, sql)
        if cached_result is not None:
            exec_result       = cached_result
            from_result_cache = True
            cache_flags["result"] = True
            exec_ms = 0
            warnings.append("⚡ Result served from cache (identical SQL within TTL)")
        else:
            try:
                exec_result = await oracle_mcp.execute_query(db_id, sql, request.max_rows)
                if "error" in exec_result:
                    raise RuntimeError(exec_result["error"])
                # Populate result cache on success
                result_cache.set(db_id, sql, exec_result)
            except Exception as exc:
                oracle_error = str(exc)
                warnings.append(
                    f"Execution failed: {oracle_error}. Attempting recovery…"
                )
                exec_result, sql, healed, heal_attempts = await _heal_and_execute(
                    db_id             = db_id,
                    original_question = request.question,
                    failed_sql        = sql,
                    error             = oracle_error,
                    schema_context    = schema_context,
                    db_name           = db_cfg.name,
                    max_rows          = request.max_rows,
                )
                agent_trace.healing_attempts = heal_attempts
                agent_trace.healed           = healed
                if exec_result is None:
                    return _err(
                        request.question, db_id, db_cfg.name,
                        f"Query failed after {len(heal_attempts)} attempt(s): {oracle_error}",
                        sql=sql, tables_used=candidate_tables, agent_trace=agent_trace,
                    )
                # Cache the healed result too
                result_cache.set(db_id, sql, exec_result)
    else:
        val_error = val_result.error_summary
        warnings.append(f"Validation failed: {val_error}. Attempting recovery…")
        exec_result, sql, healed, heal_attempts = await _heal_and_execute(
            db_id             = db_id,
            original_question = request.question,
            failed_sql        = sql,
            error             = val_error,
            schema_context    = schema_context,
            db_name           = db_cfg.name,
            max_rows          = request.max_rows,
        )
        agent_trace.healing_attempts = heal_attempts
        agent_trace.healed           = healed
        if exec_result is None:
            return _err(
                request.question, db_id, db_cfg.name,
                f"Could not generate valid SQL after {len(heal_attempts)} attempt(s).",
                sql=sql, tables_used=candidate_tables, agent_trace=agent_trace,
            )
        result_cache.set(db_id, sql, exec_result)

    if not from_result_cache:
        exec_ms = int((time.monotonic() - exec_start) * 1000)

    columns:   list[str]  = exec_result["columns"]
    rows:      list[list] = exec_result["rows"]
    row_count: int        = exec_result["row_count"]
    warnings.extend(exec_result.get("pii_warnings", []))
    agent_trace.total_attempts = len(agent_trace.healing_attempts) + 1

    # ── Step 12: Build output ─────────────────────────────────────────────────
    df            = output_service.build_dataframe(columns, rows)
    chart_type    = output_service.detect_chart_type(df)
    summary_stats = output_service.compute_summary_stats(df)

    # ── Step 13: Summarize ────────────────────────────────────────────────────
    summary = await gemini_service.summarize_results(
        question      = request.question,
        columns       = columns,
        row_count     = row_count,
        summary_stats = summary_stats,
        db_name       = db_cfg.name,
    )

    # ── Step 14: Store QueryPattern (background, only if not a cache hit) ─────
    final_sql = exec_result.get("sql_executed", sql)
    if not from_result_cache:
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

    cache_hit    = any(cache_flags.values())
    cache_source = _cache_source_label(cache_flags)

    total_pipeline_ms = int((time.monotonic() - t_pipeline_start) * 1000)

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
            cache_hit       = cache_hit,
            cache_source    = cache_source,
            pipeline_ms     = total_pipeline_ms,
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def _heal_and_execute(
    db_id: str, original_question: str, failed_sql: str,
    error: str, schema_context: str, db_name: str, max_rows: int,
) -> tuple[dict | None, str, bool, list[HealingAttemptModel]]:
    """Delegate to SelfHealingAgent."""
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
    """Fire-and-forget — never blocks the response."""
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


def _build_join_hints(join_paths_raw: list[dict]) -> list[str]:
    """
    Convert batch join path results into human-readable hint strings
    for the schema context.
    """
    hints: list[str] = []
    for path in join_paths_raw:
        seq   = path.get("table_sequence") or []
        conds = path.get("join_conditions") or []
        if not seq:
            continue
        hint = "Join path: " + " → ".join(seq)
        valid_conds = [
            c for c in conds
            if c and c.get("from_col") and c.get("to_col")
        ]
        if valid_conds:
            hint += " ON (" + ", ".join(
                f"{c['from_col']} = {c['to_col']}" for c in valid_conds
            ) + ")"
        hints.append(hint)
    return hints


def _build_schema_context(
    table_details:  list[dict],
    join_hints:     list[str],
    cross_hints:    list[str],
    schema_name:    str,
    max_tokens:     int,
    search_results: dict,
) -> tuple[str, bool]:
    """
    Assemble schema context for Gemini — metadata only, no raw data.
    Returns (context_string, was_trimmed).

    Tables are already ordered by semantic relevance from the search
    results. When the assembled context exceeds max_tokens, we drop
    lower-ranked tables until we're within budget, always keeping at
    least the top 2 tables regardless of size.
    """
    # Build score map: table_name → max cosine score from search
    score_map: dict[str, float] = {}
    for r in search_results.get("tables", []):
        name  = r.get("table_name", "")
        score = float(r.get("score", 0.0))
        if name not in score_map or score > score_map[name]:
            score_map[name] = score

    # Sort table_details by descending relevance score
    sorted_details = sorted(
        table_details,
        key=lambda t: score_map.get(t.get("table_name", ""), 0.0),
        reverse=True,
    )

    parts: list[str] = []
    for t in sorted_details:
        parts.append(_format_table_block(t, schema_name))

    # Token budget trimming — always keep at least first 2 tables
    trimmed = False
    if max_tokens > 0:
        kept: list[str] = []
        budget = max_tokens * _CHARS_PER_TOKEN
        for i, part in enumerate(parts):
            if i < 2:  # always keep top 2
                kept.append(part)
                budget -= len(part)
            elif budget - len(part) > 0:
                kept.append(part)
                budget -= len(part)
            else:
                trimmed = True
        parts = kept

    ctx = "\n\n".join(parts)
    if join_hints:
        ctx += "\n\nJoin hints:\n" + "\n".join(f"  • {h}" for h in join_hints)
    if cross_hints:
        ctx += "\n\nCross-database links:\n" + "\n".join(f"  ⬡ {h}" for h in cross_hints)
    return ctx, trimmed


def _format_table_block(t: dict, schema_name: str) -> str:
    """Format a single table's metadata block for the schema context."""
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

    return (
        f"Table: {qualified}{view_tag}{rc_hint}{dom}\n"
        f"Description: {t.get('table_description', 'N/A')}\n"
        + (f"Primary key: ({', '.join(pk_cols)})\n" if pk_cols else "")
        + "Columns:\n" + "\n".join(col_lines)
    )


def _cache_source_label(flags: dict[str, bool]) -> str:
    """Human-readable label showing which caches were hit."""
    hits = [k for k, v in flags.items() if v]
    return "+".join(hits) if hits else ""


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
