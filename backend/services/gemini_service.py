"""
backend/services/gemini_service.py  (Phase 4D)

Phase 4D additions:
  _is_retryable()      — detects 429 / 503 / timeout errors from Gemini
  _gemini_with_retry() — exponential backoff wrapper (max 3 attempts,
                         2s → 4s → 8s + jitter)

Applied to:
  generate_sql()       — SQL generation at query time
  heal_sql()           — SelfHealingAgent repair calls
  summarize_results()  — Result summarisation at query time
  enrich_columns()     — Ingestion-time column enrichment (sync → wrapped)

Data privacy guarantee (unchanged):
  Only schema metadata + aggregate stats reach Gemini.
  Raw Oracle row data NEVER leaves on-prem.
"""

import asyncio
import json
import logging
import random
import re

import google.generativeai as genai

from backend.config import settings
from backend.prompts.sql_prompt import SQL_SYSTEM_PROMPT
from backend.prompts.enrichment_prompt import build_enrichment_prompt
from backend.prompts.healing_prompt import HEALING_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

genai.configure(api_key=settings.gemini_api_key)

_flash = genai.GenerativeModel("gemini-flash-latest")

# Retry configuration
_MAX_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_S = 2.0
_RETRY_MAX_DELAY_S  = 30.0


# ── Retry helpers ─────────────────────────────────────────────────────────────

def _is_retryable(exc: Exception) -> bool:
    """
    Return True for transient Gemini API errors that are safe to retry:
    rate limits (429), service unavailability (503), and timeouts.
    Does NOT retry on authentication errors, invalid arguments, or
    context-length violations.
    """
    msg = str(exc).lower()
    return any(kw in msg for kw in (
        "429",
        "quota",
        "resource exhausted",
        "rate limit",
        "503",
        "service unavailable",
        "timeout",
        "deadline exceeded",
        "connection reset",
        "connection error",
    ))


async def _gemini_with_retry(coro_factory, label: str = "gemini"):
    """
    Execute an async Gemini coroutine with exponential backoff + jitter.

    coro_factory: zero-argument callable that returns a fresh coroutine
                  (must be a factory, not an already-started coroutine,
                  so we can retry without reusing a consumed coroutine).

    Raises the final exception after all attempts are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRY_ATTEMPTS + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            if _is_retryable(exc) and attempt < _MAX_RETRY_ATTEMPTS:
                delay = min(
                    _RETRY_BASE_DELAY_S * (2 ** (attempt - 1)) + random.uniform(0, 1),
                    _RETRY_MAX_DELAY_S,
                )
                logger.warning(
                    "[%s] Retryable error (attempt %d/%d): %s — retrying in %.1fs",
                    label, attempt, _MAX_RETRY_ATTEMPTS, exc, delay,
                )
                await asyncio.sleep(delay)
                last_exc = exc
            else:
                raise
    # Should not reach here but satisfies type checker
    if last_exc:
        raise last_exc


# ── Embeddings ────────────────────────────────────────────────────────────────

def get_embedding(text: str) -> list[float]:
    """
    3072-dim embedding via gemini-embedding-001.
    Called via asyncio.to_thread — sync call is intentional here.
    Caller (query.py) wraps this with the EmbeddingCache, so the
    Gemini API is only called on cache misses.
    """
    result = genai.embed_content(
        model     = "models/gemini-embedding-001",
        content   = text,
        task_type = "retrieval_document",
    )
    return result["embedding"]


# ── Column enrichment (ingestion time) ────────────────────────────────────────

def enrich_columns(
    table_name:    str,
    table_comment: str,
    columns:       list[dict],
    db_name:       str = "",
    domain_hint:   str = "",
) -> list[dict]:
    """
    Batch-enrich cryptic column names with business labels + descriptions.
    Sync function called from ingestion pipeline.
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

    Retries up to 3 times on 429/503/timeout errors.
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

    generation_config = genai.types.GenerationConfig(temperature=0.1)

    async def _call():
        response = await chat.send_message_async(
            user_message,
            generation_config=generation_config,
        )
        return {"sql": _clean_sql(response.text), "model": "gemini-flash-latest"}

    return await _gemini_with_retry(_call, label="generate_sql")


# ── SQL healing (SelfHealingAgent) ────────────────────────────────────────────

async def heal_sql(healing_message: str, db_name: str) -> dict:
    """
    Ask Gemini to fix a broken SQL query with error-specific guidance.

    Uses temperature=0 for fully deterministic, reproducible fixes.
    Retries up to 3 times on transient Gemini errors.

    Only schema metadata + the failed SQL reach Gemini — no data rows.
    """
    model = genai.GenerativeModel(
        "gemini-flash-latest",
        system_instruction=HEALING_SYSTEM_PROMPT,
    )

    async def _call():
        response = await model.generate_content_async(
            f"Database: {db_name}\n\n{healing_message}",
            generation_config=genai.types.GenerationConfig(temperature=0.0),
        )
        return {"sql": _clean_sql(response.text), "model": "gemini-flash-latest"}

    return await _gemini_with_retry(_call, label="heal_sql")


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
    Retries up to 3 times on transient errors.
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

    async def _call():
        response = await _flash.generate_content_async(prompt)
        return response.text.strip()

    return await _gemini_with_retry(_call, label="summarize_results")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _clean_sql(raw: str) -> str:
    """Strip any markdown fences the model might accidentally add."""
    sql = raw.strip()
    sql = re.sub(r"^```(?:sql|oracle|plsql)?\s*", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\s*```$", "", sql)
    return sql.strip()
