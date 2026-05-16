"""
backend/routes/query.py
------------------------
POST /api/query — the full Text-to-SQL pipeline:

  1. Embed question          → gemini text-embedding-004
  2. GraphRAG search         → neo4j vector search on enriched schema
  3. Get table details       → neo4j column metadata
  4. Find join paths         → neo4j FK graph traversal
  5. Build schema context    → metadata string (no raw data)
  6. Generate SQL            → gemini-1.5-flash
  7. Validate SQL            → sqlglot + read-only check
  8. Execute SQL             → oracle DB (on-prem)
  9. Build output            → DataFrame, chart type, summary stats
 10. Summarize results       → gemini (aggregate stats only, no raw rows)
"""

import asyncio
import time
from fastapi import APIRouter
from backend.models import QueryRequest, QueryResponse, QueryMeta
from backend.services import oracle_service, neo4j_service, gemini_service, output_service

router = APIRouter()


@router.post("/query", response_model=QueryResponse)
async def query(request: QueryRequest):
    question = request.question
    warnings: list[str] = []
    start_total = time.time()

    # ── Step 1: Embed the natural language question ───────────────────────────
    # get_embedding is sync — run in thread to avoid blocking the event loop
    query_embedding: list[float] = await asyncio.to_thread(
        gemini_service.get_embedding, question
    )

    # ── Step 2: GraphRAG — semantic search on enriched schema ────────────────
    search_results = await neo4j_service.semantic_schema_search(
        query_embedding, top_k=12
    )

    # Collect unique candidate table names from both table + column hits
    candidate_tables: list[str] = list(
        {r["table_name"] for r in search_results["tables"]}
        | {r["table_name"] for r in search_results["columns"]}
    )

    if not candidate_tables:
        return _error_response(
            question, "No relevant tables found in the schema graph. "
            "Have you run the ingestion script? (python -m ingestion.ingest_schema)"
        )

    # ── Step 3: Get full table/column metadata for selected tables ────────────
    table_details = await neo4j_service.get_table_details(candidate_tables)

    # ── Step 4: Find FK join paths between candidate table pairs ──────────────
    join_hints: list[str] = []
    if len(candidate_tables) > 1:
        for i in range(len(candidate_tables) - 1):
            path = await neo4j_service.get_join_path(
                candidate_tables[i], candidate_tables[i + 1]
            )
            if path:
                seq = " → ".join(path[0].get("table_sequence", []))
                conditions = path[0].get("join_conditions", [])
                hint = f"Join path: {seq}"
                if conditions:
                    joins = ", ".join(
                        f"{c['from_col']} = {c['to_col']}" for c in conditions if c
                    )
                    hint += f" ON ({joins})"
                join_hints.append(hint)

    # ── Step 5: Build schema context string (metadata only — no raw data) ─────
    schema_context = _build_schema_context(table_details, join_hints)

    # ── Step 6: Generate SQL with Gemini ─────────────────────────────────────
    sql_result = await gemini_service.generate_sql(
        question=question,
        schema_context=schema_context,
        conversation_history=[t.model_dump() for t in request.conversation_history],
    )
    sql = sql_result["sql"]

    # ── Step 7: Validate SQL (read-only check) ────────────────────────────────
    try:
        oracle_service.validate_read_only(sql)
    except ValueError as e:
        return _error_response(question, f"Generated SQL failed safety check: {e}", sql)

    # ── Step 8: Execute SQL on Oracle (preview mode skips execution) ──────────
    if not request.execute:
        return QueryResponse(
            question=question,
            sql=sql,
            columns=[],
            rows=[],
            summary="SQL generated — execution skipped (preview mode).",
            chart_type="none",
            warnings=warnings,
            meta=QueryMeta(
                tables_used=candidate_tables,
                row_count=0,
                execution_ms=0,
                chart_type="none",
            ),
        )

    exec_start = time.time()
    try:
        exec_result: dict = await asyncio.to_thread(
            oracle_service.execute_sql, sql, request.max_rows
        )
    except Exception as e:
        return _error_response(
            question, f"SQL execution failed: {e}", sql,
            tables_used=candidate_tables
        )
    exec_ms = int((time.time() - exec_start) * 1000)

    columns: list[str] = exec_result["columns"]
    rows: list[list] = exec_result["rows"]
    row_count: int = exec_result["row_count"]
    warnings.extend(exec_result.get("pii_warnings", []))

    # ── Step 9: Build output structures ──────────────────────────────────────
    df = output_service.build_dataframe(columns, rows)
    chart_type = output_service.detect_chart_type(df)
    summary_stats = output_service.compute_summary_stats(df)

    # ── Step 10: Summarize (aggregate stats to Gemini — no raw row data) ──────
    summary = await gemini_service.summarize_results(
        question=question,
        columns=columns,
        row_count=row_count,
        summary_stats=summary_stats,
    )

    return QueryResponse(
        question=question,
        sql=exec_result["sql_executed"],
        columns=columns,
        rows=rows,
        summary=summary,
        chart_type=chart_type,
        warnings=warnings,
        meta=QueryMeta(
            tables_used=candidate_tables,
            row_count=row_count,
            execution_ms=exec_ms,
            chart_type=chart_type,
        ),
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_schema_context(table_details: list[dict], join_hints: list[str]) -> str:
    """
    Assemble a compact schema context string for the Gemini SQL prompt.
    Contains ONLY metadata — table names, column names, types, and descriptions.
    No actual data values are included.
    """
    parts = []
    for t in table_details:
        col_lines = []
        for c in t.get("columns", []):
            pii_tag = " ⚠ PII" if c.get("is_pii") else ""
            col_lines.append(
                f"    - {c['name']} ({c['data_type']})"
                f" → [{c.get('label', c['name'])}]"
                f" {c.get('description', '')}{pii_tag}"
            )
        parts.append(
            f"Table: {t['schema_name']}.{t['table_name']}\n"
            f"Description: {t.get('table_description', 'N/A')}\n"
            f"Columns:\n" + "\n".join(col_lines)
        )

    context = "\n\n".join(parts)
    if join_hints:
        context += "\n\nJoin hints:\n" + "\n".join(f"  • {h}" for h in join_hints)
    return context


def _error_response(
    question: str,
    error_msg: str,
    sql: str = "",
    tables_used: list[str] | None = None,
) -> QueryResponse:
    return QueryResponse(
        question=question,
        sql=sql,
        columns=[],
        rows=[],
        summary="",
        chart_type="none",
        warnings=[],
        error=error_msg,
        meta=QueryMeta(
            tables_used=tables_used or [],
            row_count=0,
            execution_ms=0,
            chart_type="none",
        ),
    )
