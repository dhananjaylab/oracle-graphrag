"""
backend/agents/self_healing_agent.py  (Phase 4D)

Phase 4D fix: step-5 execution now routes through oracle_mcp.execute_query.
This ensures:
  • The same safety pipeline (PII masking, row limit injection) applies to
    healed SQL as to the main pipeline's SQL.
  • The MCP connection pool is used — no new direct Oracle connections.
  • Fallback behaviour is handled by oracle_mcp's existing fallback layer.

Everything else (retry loop, error classifier, healing prompt logic)
is unchanged from Phase 3A.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from backend.agents.validation_agent import ValidationAgent, ValidationResult
from backend.prompts.healing_prompt import HEALING_SYSTEM_PROMPT, build_healing_message

MAX_RETRIES = 3

# Oracle error codes that have targeted fix strategies
_ORACLE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"ORA-00942"), "ORA-00942"),
    (re.compile(r"ORA-00904"), "ORA-00904"),
    (re.compile(r"ORA-00918"), "ORA-00918"),
    (re.compile(r"ORA-00907"), "ORA-00907"),
    (re.compile(r"ORA-01789"), "ORA-01789"),
    (re.compile(r"ORA-01722"), "ORA-01722"),
    (re.compile(r"ORA-00936"), "ORA-00936"),
    (re.compile(r"ORA-00933"), "ORA-00933"),
    (re.compile(r"ORA-01830"), "ORA-01830"),
    (re.compile(r"ORA-01476"), "ORA-01476"),
    (re.compile(r"cartesian",  re.I), "cartesian_join"),
    (re.compile(r"cost_too_high",     re.I), "cost_too_high"),
    (re.compile(r"sqlglot",           re.I), "sqlglot_syntax"),
]


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class HealingAttempt:
    attempt:    int
    error_code: str
    sql_tried:  str
    outcome:    str    # "validation_failed" | "execution_failed" | "success" | "gemini_failed"
    error_msg:  str = ""


@dataclass
class HealingResult:
    success:          bool
    sql:              str
    attempts:         int
    strategy:         str
    last_error:       str = ""
    exec_result:      dict | None = None
    healing_attempts: list[HealingAttempt] = field(default_factory=list)


# ── Agent ──────────────────────────────────────────────────────────────────────

class SelfHealingAgent:
    """
    Async agent — call from async routes via await self_healing_agent.heal(...).

    Imports gemini_service and oracle_mcp lazily to avoid circular imports.
    """

    def __init__(self) -> None:
        self._validator = ValidationAgent()

    async def heal(
        self,
        db_id:            str,
        original_question: str,
        failed_sql:        str,
        error:             str,
        schema_context:    str,
        db_name:           str,
        max_rows:          int = 1000,
    ) -> HealingResult:
        """
        Attempt to heal the failed SQL. Returns HealingResult with
        success=True and exec_result populated if any retry succeeds.
        """
        # Lazy imports avoid circular dependency at module level
        from backend.services import gemini_service     # noqa: PLC0415
        from backend.mcp_client import oracle_mcp       # noqa: PLC0415

        last_sql   = failed_sql
        last_error = error
        attempts:  list[HealingAttempt] = []

        for attempt_num in range(1, MAX_RETRIES + 1):

            # ── 1. Classify error ──────────────────────────────────────────
            error_code = self._classify(last_error)

            # ── 2. Build targeted healing message ─────────────────────────
            healing_msg = build_healing_message(
                error_code        = error_code,
                error_text        = last_error,
                failed_sql        = last_sql,
                original_question = original_question,
                schema_context    = schema_context,
                attempt           = attempt_num,
                max_attempts      = MAX_RETRIES,
            )

            # ── 3. Ask Gemini to fix ───────────────────────────────────────
            try:
                fixed_result = await gemini_service.heal_sql(
                    healing_message = healing_msg,
                    db_name         = db_name,
                )
                fixed_sql = fixed_result["sql"]
            except Exception as exc:
                last_error = f"Gemini healing call failed: {exc}"
                attempts.append(HealingAttempt(
                    attempt=attempt_num, error_code=error_code,
                    sql_tried=last_sql, outcome="gemini_failed",
                    error_msg=last_error,
                ))
                continue

            # ── 4. Re-validate ─────────────────────────────────────────────
            val: ValidationResult = await asyncio.to_thread(
                self._validator.validate, db_id, fixed_sql, max_rows
            )

            if not val.valid:
                last_sql   = fixed_sql
                last_error = val.error_summary
                attempts.append(HealingAttempt(
                    attempt=attempt_num, error_code=error_code,
                    sql_tried=fixed_sql, outcome="validation_failed",
                    error_msg=last_error,
                ))
                continue

            # Use potentially-modified SQL (row limit injected, PII masked)
            fixed_sql = val.sql

            # ── 5. Execute via oracle_mcp (Phase 4D fix) ───────────────────
            # Phase 4D: routes through oracle_mcp (not direct oracle_service call)
            # so PII masking, row-limit injection, and connection pool apply.
            try:
                exec_result = await oracle_mcp.execute_query(
                    db_id    = db_id,
                    sql      = fixed_sql,
                    max_rows = max_rows,
                )
                # oracle_mcp returns {"error": ...} on Oracle-level failures
                if "error" in exec_result:
                    raise RuntimeError(exec_result["error"])

                attempts.append(HealingAttempt(
                    attempt    = attempt_num,
                    error_code = error_code,
                    sql_tried  = fixed_sql,
                    outcome    = "success",
                ))
                return HealingResult(
                    success          = True,
                    sql              = fixed_sql,
                    attempts         = attempt_num,
                    strategy         = error_code,
                    exec_result      = exec_result,
                    healing_attempts = attempts,
                )

            except Exception as exc:
                last_sql   = fixed_sql
                last_error = str(exc)
                attempts.append(HealingAttempt(
                    attempt    = attempt_num,
                    error_code = error_code,
                    sql_tried  = fixed_sql,
                    outcome    = "execution_failed",
                    error_msg  = last_error,
                ))

        # All retries exhausted
        return HealingResult(
            success          = False,
            sql              = last_sql,
            attempts         = MAX_RETRIES,
            strategy         = self._classify(last_error),
            last_error       = last_error,
            healing_attempts = attempts,
        )

    # ── Error classifier ───────────────────────────────────────────────────────

    @staticmethod
    def _classify(error: str) -> str:
        """Map an error string to a known ORA- code or category."""
        for pattern, code in _ORACLE_PATTERNS:
            if pattern.search(error):
                return code
        return "unknown"


# ── Module-level singleton ─────────────────────────────────────────────────────
self_healing_agent = SelfHealingAgent()
