"""
backend/services/output_service.py  (v2 — unchanged logic, compatible with multi-DB)
"""

import io
import pandas as pd

_DATE_HINTS = {
    "DT", "DATE", "MONTH", "MON", "YEAR", "YR", "PERIOD",
    "QTR", "QUARTER", "WEEK", "WK", "MTD", "YTD", "QTD",
}


def build_dataframe(columns: list[str], rows: list[list]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(rows, columns=columns)
    for col in df.select_dtypes(include="object").columns:
        try:
            df[col] = pd.to_numeric(df[col])
        except (ValueError, TypeError):
            pass
    return df


def compute_summary_stats(df: pd.DataFrame) -> dict:
    """
    Aggregate statistics per column — these are what get sent to Gemini
    for summarization. Individual row values are never included.
    """
    stats: dict = {"row_count": len(df)}
    for col in df.select_dtypes(include="number").columns:
        stats[col] = {
            "sum": round(float(df[col].sum()),  4),
            "avg": round(float(df[col].mean()), 4),
            "min": round(float(df[col].min()),  4),
            "max": round(float(df[col].max()),  4),
        }
    for col in df.select_dtypes(exclude="number").columns:
        stats[col] = {"unique_values": int(df[col].nunique())}
    return stats


def detect_chart_type(df: pd.DataFrame) -> str:
    """
    Infer the most appropriate chart type from DataFrame shape and column names.
    Priority: none → line (time-series) → bar → scatter → histogram → bar
    """
    if len(df) < 2:
        return "none"

    num_cols = df.select_dtypes(include="number").columns.tolist()
    cat_cols = df.select_dtypes(exclude="number").columns.tolist()

    if not num_cols:
        return "none"

    # Time-series: column name contains a date hint
    date_cols = [c for c in cat_cols if any(h in c.upper() for h in _DATE_HINTS)]
    if date_cols and num_cols:
        return "line"

    # Categorical bar: ≤25 distinct category values
    if cat_cols and num_cols and df[cat_cols[0]].nunique() <= 25:
        return "bar"

    if len(num_cols) >= 2:
        return "scatter"

    if len(num_cols) == 1 and not cat_cols:
        return "histogram"

    return "bar"


def to_excel_bytes(df: pd.DataFrame, sheet_name: str = "Query Results") -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name)
        ws = writer.sheets[sheet_name]
        for col_cells in ws.columns:
            max_len = max(
                (len(str(cell.value)) if cell.value is not None else 0)
                for cell in col_cells
            )
            ws.column_dimensions[col_cells[0].column_letter].width = min(max_len + 4, 50)
    return buf.getvalue()
