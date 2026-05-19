"""
backend/services/gemini_service.py  (v2 + Phase 3A)

New: heal_sql() — called by SelfHealingAgent to fix broken Oracle SQL.
     Uses temperature=0 for fully deterministic repair output.

Data privacy guarantee (unchanged):
  enrich_columns  → column names + data types only
  generate_sql    → schema metadata + enriched descriptions only
  heal_sql        → failed SQL + error message + schema metadata only
  summarize_results → aggregate statistics only (sum/avg/min/max/count)
  Raw Oracle row data NEVER leaves on-prem.
"""

import json
import re
import google.generativeai as genai

from backend.config import settings
from backend.prompts.sql_prompt import SQL_SYSTEM_PROMPT
from backend.prompts.enrichment_prompt import build_enrichment_prompt
from backend.prompts.healing_prompt import HEALING_SYSTEM_PROMPT

genai.configure(api_key=settings.gemini_api_key)

_flash = genai.GenerativeModel("gemini-flash-latest")


# ── Embeddings ────────────────────────────────────────────────────────────────

def get_embedding(text: str) -> list[float]:
    """3072-dim embedding via gemini-embedding-001."""
    result = genai.embed_content(
        model="models/gemini-embedding-001",
        content=text,
        task_type="retrieval_document",
    )
    return result["embedding"]


# ── Column enrichment (ingestion time) ────────────────────────────────────────

def enrich_columns(
    table_name:   str,
    table_comment: str,
    columns:      list[dict],
    db_name:      str = "",
    domain_hint:  str = "",
) -> list[dict]:
    """
    Batch-enrich cryptic column names with business labels + descriptions.
    Sends only column names and data types — no actual data values.
    """
    prompt = build_enrichment_prompt(
        table_name, table_comment, columns,
        db_name=db_name, domain_hint=domain_hint,
    )
    response = _flash.generate_content(
        prompt,
        generation_config=genai.types.GenerationConfig(
            temperature=0.1,
            response_mime_type="application/json",
        ),
    )
    try:
        raw = re.sub(r"^```(?:json)?\s*", "", response.text.strip())
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except Exception:
        return [
            {
                "column":      c["column_name"],
                "label":       c["column_name"].replace("_", " ").title(),
                "description": c.get("col_comment") or c["column_name"],
                "is_pii":      False,
            }
            for c in columns
        ]


# ── SQL generation (query time) ───────────────────────────────────────────────

async def generate_sql(
    question:             str,
    schema_context:       str,
    db_name:              str,
    conversation_history: list[dict] | None = None,
    matched_patterns:     list[dict] | None = None,
) -> dict:
    """
    Generate Oracle SQL from NL question + schema context.
    Injects matched QueryPatterns as dynamic few-shot examples.
    Only schema metadata is sent to Gemini — never raw Oracle data.
    """
    model = genai.GenerativeModel(
        "gemini-flash-latest",
        system_instruction=SQL_SYSTEM_PROMPT,
    )

    # Build dynamic few-shot block from matched QueryPatterns
    few_shot_block = ""
    if matched_patterns:
        examples = [
            f"Q: {p['nl_question']}\nSQL:\n{p['sql']}"
            for p in matched_patterns[:3]
        ]
        few_shot_block = (
            "\n\n── Matched patterns from query history (use as few-shot examples) ──\n"
            + "\n\n".join(examples)
            + "\n── End of matched patterns ──\n"
        )

    history = [
        {"role": t["role"], "parts": [t["content"]]}
        for t in (conversation_history or [])
    ]
    chat = model.start_chat(history=history)

    user_message = (
        f"Database: {db_name}\n\n"
        f"Schema context (metadata only — no actual data):\n{schema_context}"
        f"{few_shot_block}\n\n"
        f"Question: {question}\n\n"
        "Generate Oracle SQL only. No explanation, no markdown, no code fences."
    )

    response = await chat.send_message_async(
        user_message,
        generation_config=genai.types.GenerationConfig(temperature=0.1),
    )

    sql = _clean_sql(response.text)
    return {"sql": sql, "model": "gemini-flash-latest"}


# ── SQL healing (SelfHealingAgent) ────────────────────────────────────────────

async def heal_sql(healing_message: str, db_name: str) -> dict:
    """
    Ask Gemini to fix a broken SQL query with error-specific guidance.

    Called by SelfHealingAgent on each retry attempt.
    Uses temperature=0 for fully deterministic, reproducible fixes.

    healing_message is built by prompts/healing_prompt.py and contains:
      - The failed SQL
      - The exact Oracle error (ORA-XXXXX or category)
      - A targeted fix strategy for that error type
      - The schema context (table/column metadata only)
      - The original user question

    Only schema metadata + the failed SQL reach Gemini — no actual data rows.
    """
    model = genai.GenerativeModel(
        "gemini-flash-latest",
        system_instruction=HEALING_SYSTEM_PROMPT,
    )
    response = await model.generate_content_async(
        f"Database: {db_name}\n\n{healing_message}",
        generation_config=genai.types.GenerationConfig(
            temperature=0.0,    # deterministic for repair tasks
        ),
    )
    sql = _clean_sql(response.text)
    return {"sql": sql, "model": "gemini-flash-latest"}


# ── Result summarization (query time) ─────────────────────────────────────────

async def summarize_results(
    question:      str,
    columns:       list[str],
    row_count:     int,
    summary_stats: dict,
    db_name:       str,
) -> str:
    """
    Plain-English summary from aggregated statistics only.
    Individual row values are never sent to Gemini.
    """
    stats_lines = [f"Total rows returned: {row_count}"]
    for col, stat in summary_stats.items():
        if col == "row_count":
            continue
        if "sum" in stat:
            stats_lines.append(
                f"{col}: sum={stat['sum']:,.2f}, avg={stat['avg']:,.2f}, "
                f"min={stat['min']:,.2f}, max={stat['max']:,.2f}"
            )
        elif "unique_values" in stat:
            stats_lines.append(f"{col}: {stat['unique_values']} unique values")

    prompt = (
        f'A business user querying the "{db_name}" database asked: "{question}"\n\n'
        f"Query executed. Aggregated statistics (no individual records):\n"
        f"Columns: {', '.join(columns)}\n"
        + "\n".join(stats_lines)
        + "\n\nWrite a concise, business-friendly answer in 2-3 sentences:\n"
        "- Lead with the key headline number or finding\n"
        "- Use ₹ for monetary amounts; use crore/lakh denomination\n"
        "- Mention any notable trend if inferable from min/max/avg\n"
        "- Never mention SQL, database, or technical terms\n"
        "- Never start with 'Based on the data' or 'The results show'"
    )
    response = await _flash.generate_content_async(prompt)
    return response.text.strip()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _clean_sql(raw: str) -> str:
    """Strip any markdown fences the model might accidentally add."""
    sql = raw.strip()
    sql = re.sub(r"^```(?:sql|oracle|plsql)?\s*", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\s*```$", "", sql)
    return sql.strip()
