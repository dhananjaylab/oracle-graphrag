"""
backend/agents/validation_agent.py

ValidationAgent — three-layer SQL safety gate:
  Layer 1  sqlglot syntax parse (Oracle dialect, zero DB calls, fast)
  Layer 2  Read-only keyword guard + PII column detection/masking
  Layer 3  Oracle EXPLAIN PLAN cost estimation (optional — skipped on failure)

Returns a ValidationResult with:
  valid        — True only if all hard-error layers pass
  sql          — potentially modified SQL (row limit injected, PII masked)
  issues       — list of ValidationIssue (severity: error | warning)
  warnings     — list of advisory strings
  cost_estimate— integer cost from EXPLAIN PLAN (None if unavailable)
  cost_blocked — True if cost exceeded hard threshold
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
import sqlglot.errors

from backend.db_manager import db_manager
from backend.services import oracle_service

# ── Thresholds ─────────────────────────────────────────────────────────────────
COST_WARN_THRESHOLD  = 10_000   # advisory warning
COST_BLOCK_THRESHOLD = 50_000   # hard block — too expensive to execute


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class ValidationIssue:
    severity: str    # "error" | "warning"
    code: str        # e.g. "sqlglot_syntax", "read_only_violation", "cost_too_high"
    message: str
    line: int | None = None


@dataclass
class ValidationResult:
    valid: bool
    sql: str
    issues: list[ValidationIssue] = field(default_factory=list)
    warnings: list[str]          = field(default_factory=list)
    cost_estimate: int | None    = None
    cost_blocked: bool           = False
    plan_text: str               = ""

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def error_summary(self) -> str:
        return "; ".join(i.message for i in self.errors)


# ── Agent ──────────────────────────────────────────────────────────────────────

class ValidationAgent:
    """
    Synchronous agent — call with asyncio.to_thread() from async routes.

    Usage:
        agent = ValidationAgent()
        result = agent.validate(db_id="fincore", sql=generated_sql)
        if not result.valid:
            # route to SelfHealingAgent
    """

    def validate(
        self,
        db_id: str,
        sql: str,
        max_rows: int = 1000,
        skip_explain: bool = False,
    ) -> ValidationResult:
        issues: list[ValidationIssue] = []
        warnings: list[str]           = []

        # ── Layer 1: sqlglot syntax (fast, no DB call) ────────────────────
        syntax = self._check_syntax(sql)
        if not syntax["valid"]:
            return ValidationResult(
                valid=False,
                sql=sql,
                issues=[ValidationIssue(
                    severity="error",
                    code="sqlglot_syntax",
                    message=syntax["error"],
                    line=syntax.get("line"),
                )],
            )

        # ── Layer 2a: read-only keyword guard ─────────────────────────────
        try:
            oracle_service.validate_read_only(sql)
        except ValueError as e:
            return ValidationResult(
                valid=False,
                sql=sql,
                issues=[ValidationIssue(
                    severity="error",
                    code="read_only_violation",
                    message=str(e),
                )],
            )

        # ── Layer 2b: inject row limit + PII masking ──────────────────────
        sql = oracle_service.inject_row_limit(sql, max_rows)
        sql, pii_warns = oracle_service.detect_and_mask_pii(sql)
        warnings.extend(pii_warns)

        # ── Layer 3: EXPLAIN PLAN cost estimation (optional) ──────────────
        cost_estimate: int | None = None
        cost_blocked              = False
        plan_text                 = ""

        if not skip_explain:
            try:
                explain = self._run_explain_plan(db_id, sql)
                plan_text     = explain["plan_text"]
                cost_estimate = explain["cost"]

                if explain["has_cartesian"]:
                    issues.append(ValidationIssue(
                        severity="error",
                        code="cartesian_join",
                        message=(
                            "Cartesian join detected — all rows multiplied together. "
                            "Add explicit JOIN ON conditions between all tables."
                        ),
                    ))

                if explain["has_full_scan"]:
                    warnings.append(
                        "⚠ Full table scan detected — consider WHERE on an indexed column"
                    )

                if cost_estimate is not None:
                    if cost_estimate > COST_BLOCK_THRESHOLD:
                        cost_blocked = True
                        issues.append(ValidationIssue(
                            severity="error",
                            code="cost_too_high",
                            message=(
                                f"Estimated query cost {cost_estimate:,} exceeds the "
                                f"allowed limit of {COST_BLOCK_THRESHOLD:,}. "
                                "Simplify with targeted WHERE clauses or fewer joins."
                            ),
                        ))
                    elif cost_estimate > COST_WARN_THRESHOLD:
                        warnings.append(
                            f"⚠ High estimated cost ({cost_estimate:,}) — query may run slowly"
                        )

            except Exception as exc:
                # EXPLAIN PLAN failures are non-fatal — log as warning only
                warnings.append(f"Cost estimation skipped: {exc}")

        has_errors = any(i.severity == "error" for i in issues)
        return ValidationResult(
            valid=not has_errors and not cost_blocked,
            sql=sql,
            issues=issues,
            warnings=warnings,
            cost_estimate=cost_estimate,
            cost_blocked=cost_blocked,
            plan_text=plan_text,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _check_syntax(self, sql: str) -> dict:
        """Parse Oracle SQL with sqlglot. Returns {valid, error, line}."""
        try:
            parsed = sqlglot.parse(sql, dialect="oracle", error_level=sqlglot.ErrorLevel.RAISE)
            if not parsed or parsed[0] is None:
                return {"valid": False, "error": "SQL parsed to empty AST"}
            
            # Oracle requires a FROM clause for every SELECT statement
            ast = parsed[0]
            for select in ast.find_all(sqlglot.exp.Select):
                if not select.args.get("from"):
                    return {"valid": False, "error": "Missing FROM clause in SELECT statement"}

            return {"valid": True}
        except sqlglot.errors.ParseError as exc:
            # Extract first error with optional line number
            errors = exc.errors
            if errors:
                first = errors[0]
                msg   = first.get("description", str(exc))
                line  = first.get("line")
            else:
                msg, line = str(exc), None
            return {"valid": False, "error": msg, "line": line}
        except Exception as exc:
            return {"valid": False, "error": str(exc)}

    def _run_explain_plan(self, db_id: str, sql: str) -> dict:
        """
        Execute EXPLAIN PLAN FOR <sql> against Oracle and parse the output.
        Only schema metadata (query plan) is returned — never actual data rows.
        """
        pool = db_manager.get_pool(db_id)
        with pool.acquire() as conn:
            with conn.cursor() as cur:
                # Write plan to PLAN_TABLE (Oracle built-in)
                cur.execute(f"EXPLAIN PLAN FOR {sql}")

                # Read formatted plan with cost info
                cur.execute("""
                    SELECT plan_table_output
                    FROM TABLE(
                        DBMS_XPLAN.DISPLAY('PLAN_TABLE', NULL, 'BASIC +COST +ROWS')
                    )
                """)
                rows      = cur.fetchall()
                plan_text = "\n".join(r[0] for r in rows if r[0])

                # Parse total cost from root row (id=0)
                cost: int | None = None
                cur.execute("""
                    SELECT NVL(cost, 0)
                    FROM   plan_table
                    WHERE  id = 0
                    ORDER BY timestamp DESC
                    FETCH FIRST 1 ROWS ONLY
                """)
                cost_row = cur.fetchone()
                if cost_row and cost_row[0] is not None:
                    try:
                        cost = int(cost_row[0])
                    except (ValueError, TypeError):
                        pass

                # Clean up plan_table to avoid stale rows
                cur.execute("DELETE FROM plan_table")
                conn.commit()

        return {
            "plan_text":    plan_text,
            "cost":         cost,
            "has_full_scan": "TABLE ACCESS FULL" in plan_text.upper(),
            "has_cartesian": "CARTESIAN"         in plan_text.upper(),
        }


# ── Module-level singleton ─────────────────────────────────────────────────────
validation_agent = ValidationAgent()
