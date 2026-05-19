"""
backend/prompts/healing_prompt.py

Error-aware re-prompt templates for SelfHealingAgent.
Each Oracle error code maps to a targeted fix strategy so Gemini
can correct the specific problem rather than rewriting the whole query.
"""

HEALING_SYSTEM_PROMPT = """You are an Oracle SQL repair expert.
You receive a broken Oracle SQL query, the exact error it produced,
a targeted fix strategy, and the schema context.

Your ONLY output is the corrected Oracle SQL.
No explanation. No markdown. No code fences. No preamble.

Rules:
- Fix ONLY what the error requires — preserve the original structure where possible
- ALWAYS qualify table names with schema prefix  (SCHEMA.TABLE_NAME)
- ALWAYS qualify columns with table alias  (t.COLUMN_NAME)
- Use ONLY tables and columns listed in the schema context
- NEVER generate INSERT / UPDATE / DELETE / DROP / CREATE / ALTER
"""

# Per-error-code targeted strategies sent as part of the user message
STRATEGIES: dict[str, str] = {
    "ORA-00942": (
        "The table or view does not exist in this schema. "
        "Check the schema qualification — use SCHEMA.TABLE_NAME exactly as listed "
        "in the schema context. Do not invent table names."
    ),
    "ORA-00904": (
        "An invalid column identifier was referenced. "
        "Use ONLY the exact column names (not the business labels) listed "
        "in the schema context. Qualify every column with its table alias."
    ),
    "ORA-00918": (
        "A column is ambiguously defined — it exists in more than one table. "
        "Prefix EVERY column reference with its table alias "
        "(e.g. l.LOAN_ACCT_NO not just LOAN_ACCT_NO)."
    ),
    "ORA-00907": (
        "A right parenthesis is missing. "
        "Check CASE WHEN...END blocks, subqueries, and function calls "
        "for balanced parentheses."
    ),
    "ORA-01789": (
        "UNION / INTERSECT / MINUS requires the same number of columns in each "
        "SELECT clause. Align the column counts across all parts of the set operation."
    ),
    "ORA-01722": (
        "Invalid number — a string value is being compared to a numeric column "
        "or vice versa. Ensure string literals are single-quoted, numeric literals "
        "are unquoted, and date comparisons use TO_DATE() or TRUNC()."
    ),
    "ORA-00936": (
        "Missing expression. A clause is incomplete — check GROUP BY, ORDER BY, "
        "WHERE, and SELECT lists for missing column names or expressions."
    ),
    "ORA-00933": (
        "SQL command not properly ended. "
        "Remove any trailing semicolons inside subqueries. "
        "Ensure FETCH FIRST ... ROWS ONLY is the last clause (not inside a subquery)."
    ),
    "ORA-01830": (
        "Date format picture does not match. "
        "Use TO_DATE('value', 'format') explicitly. "
        "Common formats: 'DD-MON-YYYY', 'YYYY-MM-DD', 'DD/MM/YYYY'."
    ),
    "ORA-01476": (
        "Divisor is zero. Wrap division in NULLIF(denominator, 0) "
        "to prevent division by zero: value / NULLIF(denominator, 0)."
    ),
    "cartesian_join": (
        "A Cartesian join (cross join of all rows) was detected — extremely expensive. "
        "Add explicit JOIN ... ON conditions connecting ALL tables. "
        "Every table must be joined to at least one other table via a column condition."
    ),
    "cost_too_high": (
        "The query cost exceeds the allowed threshold. Reduce cost by: "
        "(1) adding WHERE clauses that filter on indexed columns early, "
        "(2) removing unnecessary table joins, "
        "(3) using FETCH FIRST N ROWS ONLY to limit the result set."
    ),
    "sqlglot_syntax": (
        "The SQL has a syntax error that was caught before reaching Oracle. "
        "Rewrite the query following Oracle SQL syntax rules strictly: "
        "use FETCH FIRST N ROWS ONLY (not LIMIT), use SYSDATE (not NOW()), "
        "use NVL() (not COALESCE() unless it works in Oracle context)."
    ),
    "unknown": (
        "Fix the error shown below and return valid Oracle SQL. "
        "If the error is unclear, simplify the query by removing complex clauses "
        "and rebuild step by step."
    ),
}


def build_healing_message(
    error_code: str,
    error_text: str,
    failed_sql: str,
    original_question: str,
    schema_context: str,
    attempt: int,
    max_attempts: int,
) -> str:
    """
    Build the full healing user message for one retry attempt.
    Includes the failed SQL, exact error, targeted strategy, and schema context.
    """
    strategy = STRATEGIES.get(error_code, STRATEGIES["unknown"])

    return (
        f"Attempt {attempt} of {max_attempts} — fix this Oracle SQL failure.\n\n"
        f"=== Failed SQL ===\n{failed_sql}\n\n"
        f"=== Oracle error ===\n{error_text}\n\n"
        f"=== Fix strategy for {error_code} ===\n{strategy}\n\n"
        f"=== Schema context (use only these tables/columns) ===\n{schema_context}\n\n"
        f"=== Original user question ===\n{original_question}\n\n"
        "Return ONLY the corrected Oracle SQL."
    )
