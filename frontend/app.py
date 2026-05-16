"""
frontend/app.py
----------------
Streamlit UI for the NL-SQL Banking platform.

Layout:
  Sidebar  — schema explorer + example questions
  Main     — query input + refinement buttons + result tabs

Result tabs:
  📊 Table    — st.dataframe with row count
  📈 Chart    — auto-detected plotly chart
  💬 Summary  — plain English answer from Gemini
  🔍 SQL      — generated Oracle SQL with copy support

Conversation: session_state stores last 10 turns for multi-turn refinement.
"""

import httpx
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

BACKEND = "http://localhost:8000"

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="NL-SQL | Banking Analytics",
    page_icon="🏦",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    /* Slightly tighten sidebar */
    [data-testid="stSidebar"] { min-width: 280px; max-width: 320px; }
    /* Metric card styling */
    [data-testid="metric-container"] {
        background: #f8f9fa; border-radius: 8px; padding: 8px 12px;
    }
    /* Make example buttons full-width */
    div[data-testid="stVerticalBlock"] button { width: 100%; text-align: left; }
    /* Warning pill */
    .pii-warn {
        background: #fff3cd; color: #856404; border-radius: 4px;
        padding: 4px 10px; font-size: 0.82rem; margin-bottom: 4px;
        display: inline-block;
    }
</style>
""", unsafe_allow_html=True)


# ── Backend helpers ───────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def fetch_schema() -> list[dict]:
    try:
        r = httpx.get(f"{BACKEND}/api/schema", timeout=5)
        return r.json().get("tables", [])
    except Exception:
        return []


@st.cache_data(ttl=600)
def fetch_examples() -> list[str]:
    try:
        r = httpx.get(f"{BACKEND}/api/examples", timeout=5)
        return r.json().get("examples", [])
    except Exception:
        return []


def call_query_api(question: str, history: list[dict], execute: bool = True) -> dict:
    try:
        r = httpx.post(
            f"{BACKEND}/api/query",
            json={
                "question": question,
                "execute": execute,
                "max_rows": 1000,
                "conversation_history": history,
            },
            timeout=120,
        )
        return r.json()
    except httpx.TimeoutException:
        return {"error": "Request timed out after 120s. Try a simpler question."}
    except Exception as e:
        return {"error": f"Backend unreachable: {e}"}


# ── Chart builder (local — no data leaves on-prem) ───────────────────────────

def build_chart(df: pd.DataFrame, chart_type: str) -> go.Figure | None:
    num_cols = df.select_dtypes(include="number").columns.tolist()
    cat_cols = df.select_dtypes(exclude="number").columns.tolist()

    if not num_cols or chart_type == "none":
        return None

    _DATE_HINTS = {"DT", "DATE", "MONTH", "MON", "YEAR", "PERIOD", "QTR", "WEEK"}

    if chart_type == "line":
        x_col = next(
            (c for c in cat_cols if any(h in c.upper() for h in _DATE_HINTS)),
            cat_cols[0] if cat_cols else df.columns[0],
        )
        return px.line(
            df, x=x_col, y=num_cols[0],
            title=f"{num_cols[0]} over {x_col}",
            template="plotly_white",
        )
    elif chart_type == "bar" and cat_cols:
        return px.bar(
            df, x=cat_cols[0], y=num_cols[0],
            title=f"{num_cols[0]} by {cat_cols[0]}",
            template="plotly_white",
            color=num_cols[0],
            color_continuous_scale="Blues",
        )
    elif chart_type == "scatter" and len(num_cols) >= 2:
        return px.scatter(
            df, x=num_cols[0], y=num_cols[1],
            title=f"{num_cols[1]} vs {num_cols[0]}",
            template="plotly_white",
        )
    elif chart_type == "histogram" and num_cols:
        return px.histogram(
            df, x=num_cols[0],
            title=f"Distribution of {num_cols[0]}",
            template="plotly_white",
        )
    # Fallback
    if cat_cols and num_cols:
        return px.bar(df, x=cat_cols[0], y=num_cols[0], template="plotly_white")
    return None


# ── Session state init ────────────────────────────────────────────────────────

if "question" not in st.session_state:
    st.session_state.question = ""
if "result" not in st.session_state:
    st.session_state.result = None
if "history" not in st.session_state:
    st.session_state.history: list[dict] = []     # [{role, content}]
if "run_query" not in st.session_state:
    st.session_state.run_query = False
if "preview_only" not in st.session_state:
    st.session_state.preview_only = False


# ── Health check ──────────────────────────────────────────────────────────────
try:
    httpx.get(f"{BACKEND}/api/health", timeout=3).raise_for_status()
    backend_ok = True
except Exception:
    backend_ok = False

if not backend_ok:
    st.error(
        "⚠️ Backend is not reachable. "
        "Start it with:  `uvicorn backend.main:app --reload`"
    )
    st.stop()


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.title("🏦 NL-SQL")
    st.caption("Banking Analytics · Natural Language Interface")
    st.divider()

    # ── Example questions ─────────────────────────────────────────────────────
    st.subheader("💡 Try a question")
    examples = fetch_examples()
    for ex in examples:
        if st.button(ex, key=f"ex_{ex[:30]}"):
            st.session_state.question = ex
            st.session_state.run_query = True
            st.session_state.history = []    # fresh conversation
            st.rerun()

    st.divider()

    # ── Schema explorer ───────────────────────────────────────────────────────
    st.subheader("📂 Schema")
    tables = fetch_schema()
    if tables:
        for tbl in tables:
            with st.expander(f"🗄 {tbl['name']}  ({tbl['column_count']} cols)"):
                desc = tbl.get("description", "")
                if desc:
                    st.caption(desc)
    else:
        st.info(
            "No schema loaded yet.\n\n"
            "Run:  `python -m ingestion.ingest_schema`"
        )

    st.divider()

    # ── Conversation history ──────────────────────────────────────────────────
    if st.session_state.history:
        st.subheader("🗒 Conversation")
        for turn in st.session_state.history[-6:]:   # show last 3 exchanges
            role_icon = "👤" if turn["role"] == "user" else "🤖"
            st.caption(f"{role_icon} {turn['content'][:80]}…")
        if st.button("🗑 Clear conversation"):
            st.session_state.history = []
            st.session_state.result = None
            st.rerun()

    st.divider()
    st.caption("Data stays on-prem. Only schema metadata is sent to Gemini.")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN AREA
# ══════════════════════════════════════════════════════════════════════════════

st.header("Ask a question about your data")

# ── Query input ───────────────────────────────────────────────────────────────
col_input, col_btn = st.columns([5, 1])

with col_input:
    question_input = st.text_input(
        label="question",
        label_visibility="collapsed",
        value=st.session_state.question,
        placeholder="e.g.  Show total loan disbursements by branch for the current quarter",
        key="question_input",
    )

with col_btn:
    ask_clicked = st.button("Ask ↵", type="primary", use_container_width=True)

if ask_clicked and question_input.strip():
    st.session_state.question = question_input.strip()
    st.session_state.run_query = True
    st.session_state.preview_only = False

# ── Refinement buttons (shown only after a successful result) ─────────────────
result = st.session_state.result
if result and not result.get("error") and result.get("rows"):
    st.markdown("**Refine:**")
    ref_cols = st.columns(5)
    refinements = [
        ("📅 This month",      "Filter results to this month only"),
        ("📅 This quarter",    "Filter results to the current quarter only"),
        ("🔝 Top 10",          "Show only the top 10 results by the main metric"),
        ("📈 Show as chart",   "Visualise the same data as a chart"),
        ("🏢 By branch",       "Break down the results by branch"),
    ]
    for i, (label, follow_up) in enumerate(refinements):
        with ref_cols[i]:
            if st.button(label, key=f"refine_{i}"):
                st.session_state.question = follow_up
                st.session_state.run_query = True
                st.session_state.preview_only = False
                st.rerun()


# ── Run query ─────────────────────────────────────────────────────────────────
if st.session_state.run_query and st.session_state.question:
    st.session_state.run_query = False
    question = st.session_state.question

    with st.spinner(f"Analysing: *{question}*"):
        api_result = call_query_api(
            question=question,
            history=st.session_state.history,
            execute=not st.session_state.preview_only,
        )

    st.session_state.result = api_result

    # Update conversation history (keep last 10 turns = 5 exchanges)
    if not api_result.get("error"):
        st.session_state.history.append({"role": "user", "content": question})
        if api_result.get("sql"):
            st.session_state.history.append({
                "role": "model",
                "content": api_result["sql"],
            })
        st.session_state.history = st.session_state.history[-10:]


# ══════════════════════════════════════════════════════════════════════════════
# RESULTS
# ══════════════════════════════════════════════════════════════════════════════

result = st.session_state.result

if result is None:
    st.info("Type a question above or choose an example from the sidebar to get started.")
    st.stop()

if result.get("error"):
    st.error(f"❌ {result['error']}")
    if result.get("sql"):
        with st.expander("Generated SQL (for debugging)"):
            st.code(result["sql"], language="sql")
    st.stop()

# ── Metrics row ───────────────────────────────────────────────────────────────
meta = result.get("meta", {})
m1, m2, m3, m4 = st.columns(4)
m1.metric("Rows returned",   meta.get("row_count", 0))
m2.metric("Execution time",  f"{meta.get('execution_ms', 0)} ms")
m3.metric("Tables used",     len(meta.get("tables_used", [])))
m4.metric("Chart detected",  meta.get("chart_type", "none").capitalize())

# ── PII warnings ──────────────────────────────────────────────────────────────
for warn in result.get("warnings", []):
    st.markdown(f'<span class="pii-warn">🔒 {warn}</span>', unsafe_allow_html=True)

# ── Result tabs ───────────────────────────────────────────────────────────────
columns = result.get("columns", [])
rows    = result.get("rows", [])

tab_table, tab_chart, tab_summary, tab_sql = st.tabs(
    ["📊 Table", "📈 Chart", "💬 Summary", "🔍 SQL"]
)

# ── Tab 1: Table ──────────────────────────────────────────────────────────────
with tab_table:
    if not rows:
        st.info("Query returned no rows.")
    else:
        df = pd.DataFrame(rows, columns=columns)
        st.dataframe(df, use_container_width=True, height=420)
        st.caption(f"{len(df)} rows · {len(columns)} columns")

        # Excel download
        try:
            from backend.services.output_service import to_excel_bytes
            excel_bytes = to_excel_bytes(df)
        except ImportError:
            # Fallback if running frontend standalone
            import io, openpyxl
            buf = io.BytesIO()
            df.to_excel(buf, index=False, engine="openpyxl")
            excel_bytes = buf.getvalue()

        st.download_button(
            label="⬇ Download Excel",
            data=excel_bytes,
            file_name="query_results.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

# ── Tab 2: Chart ──────────────────────────────────────────────────────────────
with tab_chart:
    chart_type = result.get("chart_type", "none")
    if not rows:
        st.info("No data to chart.")
    elif chart_type == "none":
        st.info(
            "No suitable chart detected for this result shape.\n\n"
            "Try asking: *'Show the same data grouped by month'* or "
            "*'Show as a bar chart'*"
        )
    else:
        df = pd.DataFrame(rows, columns=columns)
        fig = build_chart(df, chart_type)
        if fig:
            st.plotly_chart(fig, use_container_width=True)
            # Chart type switcher
            override = st.selectbox(
                "Override chart type",
                ["auto", "bar", "line", "scatter", "histogram"],
                key="chart_override",
            )
            if override != "auto":
                fig2 = build_chart(df, override)
                if fig2:
                    st.plotly_chart(fig2, use_container_width=True)
        else:
            st.info("Could not render chart for this data shape.")

# ── Tab 3: Summary ────────────────────────────────────────────────────────────
with tab_summary:
    summary = result.get("summary", "")
    if summary:
        st.markdown(f"### 💬 {summary}")
        tables_used = meta.get("tables_used", [])
        if tables_used:
            st.caption(f"Tables queried: `{'` · `'.join(tables_used)}`")
    else:
        st.info("No summary available.")

# ── Tab 4: SQL ────────────────────────────────────────────────────────────────
with tab_sql:
    sql = result.get("sql", "")
    if sql:
        st.code(sql, language="sql")
        st.caption(
            "⚠️ This SQL was auto-generated. Review before using in critical reports."
        )
    else:
        st.info("No SQL to display.")
