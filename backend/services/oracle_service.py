"""
backend/services/oracle_service.py  (v2 — multi-DB)

All functions accept db_id. Connection pools are managed by DBManager.
get_data_dictionary() now fetches PKs, unique constraints, indexes,
approximate row counts, views, and cardinality hints — all metadata only.
"""

import re
import oracledb
from backend.db_manager import db_manager

_FORBIDDEN = {
    "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE",
    "MERGE", "GRANT", "REVOKE", "EXECUTE", "EXEC", "CALL",
    "COMMIT", "ROLLBACK", "SAVEPOINT", "BEGIN", "END",
}

_PII_PATTERNS = [
    (re.compile(r"\b(CUST_NM|CUST_NAME|CUSTOMER_NAME|FULL_NAME|FIRST_NM|LAST_NM)\b", re.I),
     lambda col: f"REGEXP_REPLACE({col}, '(\\S)\\S+', '\\1***')"),
    (re.compile(r"\b(EMAIL|EMAIL_ID|EMAIL_ADDR)\b", re.I),
     lambda col: f"REGEXP_REPLACE({col}, '(.).*(@)', '\\1***\\2')"),
    (re.compile(r"\b(MOBILE|PHONE|PHONE_NO|CONTACT_NO|MOB_NO)\b", re.I),
     lambda col: f"'XX-XXXX-' || SUBSTR({col}, -4)"),
    (re.compile(r"\b(PAN_NO|PAN|PAN_CD)\b", re.I),
     lambda _: "'***MASKED***'"),
    (re.compile(r"\b(AADHAR|AADHAAR|UID_NO)\b", re.I),
     lambda _: "'***MASKED***'"),
    (re.compile(r"\b(ACCT_NO|ACCOUNT_NO|ACCT_NUM)\b", re.I),
     lambda col: f"'XXXX' || SUBSTR({col}, -4)"),
]


# ── Safety ─────────────────────────────────────────────────────────────────────

def validate_read_only(sql: str) -> None:
    cleaned = re.sub(r"'[^']*'", "''", sql)
    tokens  = set(re.split(r"\W+", cleaned.upper()))
    hits    = _FORBIDDEN & tokens
    if hits:
        raise ValueError(f"Forbidden keywords detected: {', '.join(sorted(hits))}")


def detect_and_mask_pii(sql: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    for pattern, mask_fn in _PII_PATTERNS:
        for match in pattern.finditer(sql):
            col = match.group(0)
            sql = sql[: match.start()] + mask_fn(col) + sql[match.end():]
            warnings.append(f"PII column '{col}' automatically masked in output")
            break   # rescan offsets shift after replacement
    return sql, warnings


def inject_row_limit(sql: str, max_rows: int) -> str:
    upper = sql.upper()
    if "FETCH FIRST" not in upper and "ROWNUM" not in upper:
        return f"{sql.rstrip(';')} FETCH FIRST {max_rows} ROWS ONLY"
    return sql


# ── SQL execution ───────────────────────────────────────────────────────────────

def execute_sql(db_id: str, sql: str, max_rows: int = 1000) -> dict:
    """Execute a validated read-only SQL query against the named database."""
    validate_read_only(sql)
    sql, pii_warnings = detect_and_mask_pii(sql)
    sql = inject_row_limit(sql, max_rows)

    pool = db_manager.get_pool(db_id)
    with pool.acquire() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            columns = [c[0] for c in cur.description]
            rows    = cur.fetchmany(max_rows)
            return {
                "columns":     columns,
                "rows":        [list(r) for r in rows],
                "row_count":   len(rows),
                "sql_executed": sql,
                "pii_warnings": pii_warnings,
            }


# ── Cardinality inference (metadata only, never touches data) ──────────────────

def _cardinality_hint(col: dict) -> str:
    if col.get("is_pk") or col.get("is_unique"):
        return "unique"
    name  = col["column_name"].upper()
    dtype = (col.get("data_type") or "").upper()
    if any(name.endswith(s) for s in ("_FLG", "_IND", "_TYP", "_CD", "_STATUS", "_TYPE")):
        return "low"
    if dtype == "NUMBER" and col.get("data_precision") == 1:
        return "low"
    if col.get("is_indexed") and name.endswith("_ID"):
        return "medium"
    if col.get("is_indexed"):
        return "medium"
    return "unknown"


# ── Enriched data dictionary ────────────────────────────────────────────────────

def get_data_dictionary(db_id: str, schema: str | None = None) -> dict:
    """
    Pull enriched schema metadata from Oracle data dictionary.
    Never reads actual data — only metadata from ALL_* views.

    Returns:
        columns      — list of column dicts annotated with is_pk, is_unique,
                       is_indexed, is_view, cardinality_hint, row_count
        foreign_keys — FK relationships
        indexes      — index definitions
        pk_map       — table → [pk_column_names]
        view_names   — set of view names (treated as tables)
        row_counts   — approx row counts from optimizer stats
    """
    cfg = db_manager.get_config(db_id)
    sf  = (schema or cfg.schema or "").upper() or None
    pool = db_manager.get_pool(db_id)

    with pool.acquire() as conn:
        with conn.cursor() as cur:

            # ── Columns ────────────────────────────────────────────────────
            col_sql = """
                SELECT t.owner, t.table_name, c.column_name,
                       c.data_type, c.data_length, c.data_precision,
                       c.data_scale, c.nullable, c.column_id,
                       NVL(tc.comments,  '') AS col_comment,
                       NVL(tbc.comments, '') AS table_comment
                FROM   all_tables t
                JOIN   all_tab_columns c
                       ON  t.owner = c.owner AND t.table_name = c.table_name
                LEFT JOIN all_col_comments tc
                       ON  c.owner = tc.owner
                       AND c.table_name = tc.table_name
                       AND c.column_name = tc.column_name
                LEFT JOIN all_tab_comments tbc
                       ON  t.owner = tbc.owner AND t.table_name = tbc.table_name
                WHERE  t.table_name NOT LIKE 'BIN$%'
            """
            p = []
            if sf:
                col_sql += " AND t.owner = :1"
                p.append(sf)
            col_sql += " ORDER BY t.table_name, c.column_id"
            cur.execute(col_sql, p)
            hdrs     = [d[0].lower() for d in cur.description]
            col_rows = [dict(zip(hdrs, r)) for r in cur.fetchall()]

            # ── Views ───────────────────────────────────────────────────────
            view_sql = "SELECT owner, view_name FROM all_views"
            vp = []
            if sf:
                view_sql += " WHERE owner = :1"
                vp.append(sf)
            cur.execute(view_sql, vp)
            view_names: set[str] = {r[1] for r in cur.fetchall()}

            # ── Approx row counts (optimizer stats — metadata, not data) ────
            stats_sql = """
                SELECT owner, table_name, NVL(num_rows, 0)
                FROM   all_tables
                WHERE  table_name NOT LIKE 'BIN$%'
            """
            sp = []
            if sf:
                stats_sql += " AND owner = :1"
                sp.append(sf)
            cur.execute(stats_sql, sp)
            row_counts: dict[str, int] = {r[1]: int(r[2] or 0) for r in cur.fetchall()}

            # ── Primary keys ────────────────────────────────────────────────
            pk_sql = """
                SELECT acc.owner, acc.table_name, acc.column_name
                FROM   all_cons_columns acc
                JOIN   all_constraints ac
                       ON  acc.owner = ac.owner
                       AND acc.constraint_name = ac.constraint_name
                WHERE  ac.constraint_type = 'P'
            """
            pkp = []
            if sf:
                pk_sql += " AND acc.owner = :1"
                pkp.append(sf)
            cur.execute(pk_sql, pkp)
            pk_map: dict[str, set[str]] = {}
            for r in cur.fetchall():
                pk_map.setdefault(r[1], set()).add(r[2])

            # ── Unique constraints ───────────────────────────────────────────
            uq_sql = """
                SELECT acc.owner, acc.table_name, acc.column_name
                FROM   all_cons_columns acc
                JOIN   all_constraints ac
                       ON  acc.owner = ac.owner
                       AND acc.constraint_name = ac.constraint_name
                WHERE  ac.constraint_type = 'U'
            """
            uqp = []
            if sf:
                uq_sql += " AND acc.owner = :1"
                uqp.append(sf)
            cur.execute(uq_sql, uqp)
            uq_map: dict[str, set[str]] = {}
            for r in cur.fetchall():
                uq_map.setdefault(r[1], set()).add(r[2])

            # ── Indexes ─────────────────────────────────────────────────────
            idx_sql = """
                SELECT ai.owner, ai.table_name, ai.index_name,
                       ai.uniqueness, ai.index_type,
                       LISTAGG(aic.column_name, ',')
                           WITHIN GROUP (ORDER BY aic.column_position) AS idx_cols
                FROM   all_indexes ai
                JOIN   all_ind_columns aic
                       ON  ai.owner = aic.index_owner
                       AND ai.index_name = aic.index_name
                WHERE  ai.generated = 'N'
                  AND  ai.index_type IN (
                       'NORMAL', 'BITMAP', 'FUNCTION-BASED NORMAL')
            """
            ip = []
            if sf:
                idx_sql += " AND ai.owner = :1"
                ip.append(sf)
            idx_sql += " GROUP BY ai.owner, ai.table_name, ai.index_name, ai.uniqueness, ai.index_type"
            cur.execute(idx_sql, ip)
            idx_hdrs = [d[0].lower() for d in cur.description]
            idx_rows = [dict(zip(idx_hdrs, r)) for r in cur.fetchall()]

            indexed_map: dict[str, set[str]] = {}
            for idx in idx_rows:
                for col in (idx.get("idx_cols") or "").split(","):
                    indexed_map.setdefault(idx["table_name"], set()).add(col.strip())

            # ── Foreign keys ────────────────────────────────────────────────
            fk_sql = """
                SELECT a.owner, a.table_name, a.column_name,
                       c_pk.owner AS ref_owner, c_pk.table_name AS ref_table,
                       cc_pk.column_name AS ref_column
                FROM   all_cons_columns a
                JOIN   all_constraints c
                       ON  a.owner = c.owner
                       AND a.constraint_name = c.constraint_name
                       AND c.constraint_type = 'R'
                JOIN   all_constraints c_pk
                       ON  c.r_owner = c_pk.owner
                       AND c.r_constraint_name = c_pk.constraint_name
                JOIN   all_cons_columns cc_pk
                       ON  c_pk.owner = cc_pk.owner
                       AND c_pk.constraint_name = cc_pk.constraint_name
            """
            fp = []
            if sf:
                fk_sql += " WHERE a.owner = :1"
                fp.append(sf)
            cur.execute(fk_sql, fp)
            fk_hdrs = [d[0].lower() for d in cur.description]
            fk_rows = [dict(zip(fk_hdrs, r)) for r in cur.fetchall()]

    # ── Annotate columns with enriched structural metadata ──────────────────
    for col in col_rows:
        tname = col["table_name"]
        cname = col["column_name"]
        col["is_view"]        = tname in view_names
        col["row_count"]      = row_counts.get(tname, 0)
        col["is_pk"]          = cname in pk_map.get(tname, set())
        col["is_unique"]      = cname in uq_map.get(tname, set())
        col["is_indexed"]     = cname in indexed_map.get(tname, set())
        col["cardinality_hint"] = _cardinality_hint(col)

    return {
        "columns":      col_rows,
        "foreign_keys": fk_rows,
        "indexes":      idx_rows,
        "pk_map":       {t: list(cols) for t, cols in pk_map.items()},
        "view_names":   list(view_names),
        "row_counts":   row_counts,
    }
