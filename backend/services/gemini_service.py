"""
backend/services/gemini_service.py
------------------------------------
All Gemini API interactions:
  - get_embedding()     → text-embedding-004 (768 dims) for GraphRAG
  - enrich_columns()    → cryptic column names → business labels (ingestion time)
  - generate_sql()      → NL question + schema context → Oracle SQL
  - summarize_results() → aggregated stats → plain English summary

DATA PRIVACY GUARANTEE:
  - enrich_columns: sends only column names + data types (schema metadata)
  - generate_sql:   sends only table/column names + enriched descriptions
  - summarize_results: sends only aggregate statistics (sum/avg/min/max/count)
  - RAW ROW DATA NEVER LEAVES ON-PREM
"""

import json
import re
import google.generativeai as genai
from backend.config import settings
from backend.prompts.sql_prompt import SQL_SYSTEM_PROMPT
from backend.prompts.enrichment_prompt import build_enrichment_prompt

genai.configure(api_key=settings.gemini_api_key)

_flash = genai.GenerativeModel("gemini-flash-latest")


# ── Embeddings ────────────────────────────────────────────────────────────────

def get_embedding(text: str) -> list[float]:
    """
    Generate a 768-dimensional embedding using text-embedding-004.
    Used for both ingestion (schema nodes) and query-time (NL question).
    """
    result = genai.embed_content(
        model="models/gemini-embedding-001",
        content=text,
        task_type="retrieval_document",
    )
    return result["embedding"]


# ── Schema enrichment (ingestion time) ───────────────────────────────────────

def enrich_columns(
    table_name: str, table_comment: str, columns: list[dict]
) -> list[dict]:
    """
    Send a batch of cryptic column names to Gemini and receive
    human-readable labels and descriptions in return.

    Only schema metadata is sent — no actual data values.
    Called once per table during ingestion, not at query time.
    """
    prompt = build_enrichment_prompt(table_name, table_comment, columns)
    response = _flash.generate_content(
        prompt,
        generation_config=genai.types.GenerationConfig(
            temperature=0.1,
            response_mime_type="application/json",
        ),
    )
    try:
        # Strip accidental markdown fences if present
        raw = response.text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except (json.JSONDecodeError, Exception):
        # Fallback: return columns with minimal enrichment
        return [
            {
                "column": c["column_name"],
                "label": c["column_name"].replace("_", " ").title(),
                "description": c.get("col_comment") or c["column_name"],
                "is_pii": False,
            }
            for c in columns
        ]


# ── SQL generation (query time) ───────────────────────────────────────────────

async def generate_sql(
    question: str,
    schema_context: str,
    conversation_history: list[dict] | None = None,
) -> dict:
    """
    Generate Oracle SQL from a natural language question and schema context.

    conversation_history: list of {"role": "user"|"model", "content": "..."}
    Enables multi-turn refinement (e.g. "now filter by branch X").

    ONLY schema metadata is sent to Gemini — never raw Oracle data.
    """
    model = genai.GenerativeModel(
        "gemini-flash-latest",
        system_instruction=SQL_SYSTEM_PROMPT,
    )

    # Build Gemini chat history from conversation turns
    history = []
    for turn in (conversation_history or []):
        history.append({
            "role": turn["role"],
            "parts": [turn["content"]],
        })

    chat = model.start_chat(history=history)

    user_message = (
        f"Schema context (table/column metadata only — no actual data):\n"
        f"{schema_context}\n\n"
        f"Question: {question}\n\n"
        f"Generate Oracle SQL only. No explanation, no markdown, no code fences."
    )

    response = await chat.send_message_async(
        user_message,
        generation_config=genai.types.GenerationConfig(temperature=0.1),
    )

    sql = response.text.strip()
    # Clean up any accidental markdown the model might add
    sql = re.sub(r"^```(?:sql|oracle|plsql)?\s*", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\s*```$", "", sql)
    sql = sql.strip()

    return {"sql": sql, "model": "gemini-flash-latest"}


# ── Result summarization (query time) ────────────────────────────────────────

async def summarize_results(
    question: str,
    columns: list[str],
    row_count: int,
    summary_stats: dict,
) -> str:
    """
    Generate a plain English summary of query results.

    PRIVACY: Only aggregate statistics (sum, avg, min, max, unique counts)
    are sent to Gemini — never actual row values.
    """
    # Format stats readably
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

    prompt = f"""A business user in a bank asked: "{question}"

The query executed successfully. Here are the AGGREGATED results (no individual records):
Columns returned: {', '.join(columns)}
{chr(10).join(stats_lines)}

Write a concise, business-friendly answer in 2-3 sentences:
- Lead with the key finding or headline number
- Use ₹ for monetary amounts and crore/lakh denomination where appropriate
- Mention any notable pattern (e.g. top value, trend direction) if inferable from stats
- Do NOT mention SQL, database, or technical terms
- Do NOT say "based on the data" or "the results show" — just state the finding directly"""

    response = await _flash.generate_content_async(prompt)
    return response.text.strip()
