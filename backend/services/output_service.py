"""
backend/services/output_service.py
------------------------------------
Transforms raw Oracle query results into structured output:
  - build_dataframe()       → pandas DataFrame from columns + rows
  - compute_summary_stats() → aggregate stats (never raw rows) for Gemini summarization
  - detect_chart_type()     → infer best chart from DataFrame shape and column names
  - to_excel_bytes()        → Excel file as bytes for download
"""

import io
import pandas as pd


# ── DataFrame ─────────────────────────────────────────────────────────────────

def build_dataframe(columns: list[str], rows: list[list]) -> pd.DataFrame:
    """Build a pandas DataFrame from Oracle query results."""
    if not rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(rows, columns=columns)
    # Attempt to coerce obvious numeric-looking object columns
    for col in df.select_dtypes(include="object").columns:
        try:
            df[col] = pd.to_numeric(df[col])
        except (ValueError, TypeError):
            pass
    return df


# ── Summary stats (sent to Gemini — no raw values) ───────────────────────────

def compute_summary_stats(df: pd.DataFrame) -> dict:
    """
    Compute aggregated statistics per column.
    These stats (not raw rows) are what gets sent to Gemini for summarization.
    """
    stats: dict = {"row_count": len(df)}
    for col in df.select_dtypes(include="number").columns:
        stats[col] = {
            "sum":   round(float(df[col].sum()), 4),
            "avg":   round(float(df[col].mean()), 4),
            "min":   round(float(df[col].min()), 4),
            "max":   round(float(df[col].max()), 4),
        }
    for col in df.select_dtypes(exclude="number").columns:
        stats[col] = {"unique_values": int(df[col].nunique())}
    return stats


# ── Chart type detection ──────────────────────────────────────────────────────

# Date/time keyword hints in column names
_DATE_HINTS = {"DT", "DATE", "MONTH", "MON", "YEAR", "YR", "PERIOD",
               "QTR", "QUARTER", "WEEK", "WK", "MTD", "YTD", "QTD"}


def detect_chart_type(df: pd.DataFrame) -> str:
    """
    Infer the most appropriate chart type for a query result DataFrame.

    Rules (in priority order):
      1. < 2 rows or no numeric cols → none
      2. Date/time column + numeric  → line (time series)
      3. Categorical + numeric, ≤25 categories → bar
      4. Two numeric cols            → scatter
      5. One numeric, no categories  → histogram
      6. Default fallback            → bar
    """
    if len(df) < 2:
        return "none"

    num_cols = df.select_dtypes(include="number").columns.tolist()
    cat_cols = df.select_dtypes(exclude="number").columns.tolist()

    if not num_cols:
        return "none"

    # Rule 2: time series detection
    date_cols = [
        c for c in cat_cols
        if any(hint in c.upper() for hint in _DATE_HINTS)
    ]
    if date_cols and num_cols:
        return "line"

    # Rule 3: categorical bar chart
    if cat_cols and num_cols and df[cat_cols[0]].nunique() <= 25:
        return "bar"

    # Rule 4: scatter
    if len(num_cols) >= 2:
        return "scatter"

    # Rule 5: histogram
    if len(num_cols) == 1 and not cat_cols:
        return "histogram"

    return "bar"


# ── Excel export ──────────────────────────────────────────────────────────────

def to_excel_bytes(df: pd.DataFrame, sheet_name: str = "Query Results") -> bytes:
    """Serialize DataFrame to an Excel file and return as bytes."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name)
        # Auto-fit column widths
        ws = writer.sheets[sheet_name]
        for col_cells in ws.columns:
            max_len = max(
                len(str(cell.value)) if cell.value is not None else 0
                for cell in col_cells
            )
            ws.column_dimensions[col_cells[0].column_letter].width = min(max_len + 4, 50)
    return buf.getvalue()
