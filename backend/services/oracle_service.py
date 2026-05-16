"""
backend/services/oracle_service.py
------------------------------------
Handles all Oracle DB interactions:
  - Connection pool management
  - Read-only SQL execution with safety guards
  - Data dictionary extraction for schema ingestion

Uses python-oracledb in thin mode — no Oracle Instant Client install needed.
If your org requires thick mode (e.g. for wallets / advanced auth), call
oracledb.init_oracle_client(lib_dir="/path/to/instantclient") before importing this module.
"""

import re
import oracledb
from backend.config import settings

_pool: oracledb.ConnectionPool | None = None

# Keywords that should never appear in a SELECT-only context
_FORBIDDEN = {
    "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
    "TRUNCATE", "MERGE", "GRANT", "REVOKE", "EXECUTE", "EXEC",
    "CALL", "COMMIT", "ROLLBACK", "SAVEPOINT", "BEGIN", "END",
}

_PII_PATTERNS = [
    (re.compile(r"\b(CUST_NM|CUST_NAME|CUSTOMER_NAME|FULL_NAME|FIRST_NM|LAST_NM)\b", re.I),
     lambda col: f"REGEXP_REPLACE({col}, '(\\S)\\S+', '\\1***')"),
    (re.compile(r"\b(EMAIL|EMAIL_ID|EMAIL_ADDR)\b", re.I),
     lambda col: f"REGEXP_REPLACE({col}, '(.).*(@)', '\\1***\\2')"),
    (re.compile(r"\b(MOBILE|PHONE|PHONE_NO|CONTACT_NO|MOB_NO)\b", re.I),
     lambda col: f"'XX-XXXX-' || SUBSTR({col}, -4)"),
    (re.compile(r"\b(PAN_NO|PAN|PAN_CD)\b", re.I),
     lambda col: f"'***MASKED***'"),
    (re.compile(r"\b(AADHAR|AADHAAR|UID_NO)\b", re.I),
     lambda col: f"'***MASKED***'"),
    (re.compile(r"\b(ACCT_NO|ACCOUNT_NO|ACCT_NUM)\b", re.I),
     lambda col: f"'XXXX' || SUBSTR({col}, -4)"),
]


def get_pool() -> oracledb.ConnectionPool:
    global _pool
    if _pool is None:
        _pool = oracledb.create_pool(
            user=settings.oracle_user,
            password=settings.oracle_password,
            dsn=settings.oracle_dsn,
            min=1,
            max=5,
            increment=1,
        )
    return _pool


def validate_read_only(sql: str) -> None:
    """Raise ValueError if SQL contains any DML/DDL keyword."""
    # Strip string literals to avoid false positives on values like 'INSERT'
    cleaned = re.sub(r"'[^']*'", "''", sql)
    tokens = set(re.split(r"\W+", cleaned.upper()))
    hits = _FORBIDDEN & tokens
    if hits:
        raise ValueError(f"Operation not permitted — forbidden keywords: {', '.join(sorted(hits))}")


def detect_and_mask_pii(sql: str) -> tuple[str, list[str]]:
    """
    Scan SELECT clause for known PII column patterns and auto-mask them.
    Returns (modified_sql, list_of_warnings).
    """
    warnings = []
    for pattern, mask_fn in _PII_PATTERNS:
        for match in pattern.finditer(sql):
            col = match.group(0)
            masked_expr = mask_fn(col)
            sql = sql[:match.start()] + masked_expr + sql[match.end():]
            warnings.append(f"PII column '{col}' automatically masked in output")
            break  # re-scan after replacement to handle offset shifts
    return sql, warnings


def inject_row_limit(sql: str, max_rows: int) -> str:
    """Add FETCH FIRST ... ROWS ONLY if no row limit is already present."""
    upper = sql.upper()
    if "FETCH FIRST" not in upper and "ROWNUM" not in upper:
        return f"{sql.rstrip(';')} FETCH FIRST {max_rows} ROWS ONLY"
    return sql


def execute_sql(sql: str, max_rows: int = 1000) -> dict:
    """
    Execute a validated, read-only SQL query against Oracle.
    Returns columns, rows, row_count, and the final SQL executed.
    """
    validate_read_only(sql)
    sql, pii_warnings = detect_and_mask_pii(sql)
    sql = inject_row_limit(sql, max_rows)

    pool = get_pool()
    with pool.acquire() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            columns = [col[0] for col in cursor.description]
            rows = cursor.fetchmany(max_rows)
            return {
                "columns": columns,
                "rows": [list(row) for row in rows],
                "row_count": len(rows),
                "sql_executed": sql,
                "pii_warnings": pii_warnings,
            }


def get_data_dictionary(schema: str | None = None) -> dict:
    """
    Pull table + column metadata and FK relationships from Oracle data dictionary.
    Sends ONLY schema metadata — never row data.
    """
    schema_filter = (schema or settings.oracle_schema or "").upper() or None

    columns_sql = """
        SELECT
            t.owner,
            t.table_name,
            c.column_name,
            c.data_type,
            c.data_length,
            c.nullable,
            c.column_id,
            NVL(tc.comments, '')  AS col_comment,
            NVL(tbc.comments, '') AS table_comment
        FROM all_tables t
        JOIN all_tab_columns c
          ON t.owner = c.owner AND t.table_name = c.table_name
        LEFT JOIN all_col_comments tc
          ON  c.owner = tc.owner
          AND c.table_name = tc.table_name
          AND c.column_name = tc.column_name
        LEFT JOIN all_tab_comments tbc
          ON  t.owner = tbc.owner
          AND t.table_name = tbc.table_name
        WHERE t.table_name NOT LIKE 'BIN$%'  -- exclude recyclebin tables
    """
    fk_sql = """
        SELECT
            a.owner,
            a.table_name,
            a.column_name,
            c_pk.owner        AS ref_owner,
            c_pk.table_name   AS ref_table,
            cc_pk.column_name AS ref_column
        FROM all_cons_columns a
        JOIN all_constraints c
          ON  a.owner = c.owner AND a.constraint_name = c.constraint_name
         AND c.constraint_type = 'R'
        JOIN all_constraints c_pk
          ON  c.r_owner = c_pk.owner AND c.r_constraint_name = c_pk.constraint_name
        JOIN all_cons_columns cc_pk
          ON  c_pk.owner = cc_pk.owner AND c_pk.constraint_name = cc_pk.constraint_name
    """

    params_col, params_fk = [], []
    if schema_filter:
        columns_sql += " AND t.owner = :1"
        fk_sql += " WHERE a.owner = :1"
        params_col = [schema_filter]
        params_fk = [schema_filter]

    columns_sql += " ORDER BY t.table_name, c.column_id"

    pool = get_pool()
    with pool.acquire() as conn:
        with conn.cursor() as cur:
            cur.execute(columns_sql, params_col)
            col_headers = [d[0].lower() for d in cur.description]
            col_rows = [dict(zip(col_headers, row)) for row in cur.fetchall()]

            cur.execute(fk_sql, params_fk)
            fk_headers = [d[0].lower() for d in cur.description]
            fk_rows = [dict(zip(fk_headers, row)) for row in cur.fetchall()]

    return {"columns": col_rows, "foreign_keys": fk_rows}
