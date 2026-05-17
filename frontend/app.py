"""
frontend/app.py  (v2 — multi-DB + QueryPattern display)

New in v2:
  - Database selector dropdown (top of sidebar)
  - Matched pattern panel: shows reused SQL + the preserved schema Cypher
  - Schema Cypher expander: shows every Cypher query used for schema discovery
  - Domain filter in schema explorer
  - View / non-view badge on schema tables
"""

import httpx
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

BACKEND = "http://localhost:8000"

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="NL-SQL | Banking Analytics",
    page_icon="🏦",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
[data-testid="stSidebar"] { min-width: 290px; max-width: 330px; }
[data-testid="metric-container"] {
    background: #f8f9fa; border-radius: 8px; padding: 8px 12px; }
.pii-warn {
    background: #fff3cd; color: #856404; border-radius: 4px;
    padding: 4px 10px; font-size: 0.82rem; display: inline-block; margin-bottom: 4px; }
.pattern-badge {
    background: #d1ecf1; color: #0c5460; border-radius: 4px;
    padding: 3px 8px; font-size: 0.80rem; display: inline-block; }
.cross-db-hint {
    background: #e2e3f3; color: #383d8b; border-radius: 4px;
    padding: 4px 10px; font-size: 0.82rem; display: inline-block; margin-bottom: 4px; }
</style>
""", unsafe_allow_html=True)

_DATE_HINTS = {"DT", "DATE", "MONTH", "MON", "YEAR", "PERIOD", "QTR", "WEEK"}


# ── Backend helpers ────────────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def fetch_databases() -> list[dict]:
    try:
        return httpx.get(f"{BACKEND}/api/databases", timeout=5).json().get("databases", [])
    except Exception:
        return []


@st.cache_data(ttl=300)
def fetch_schema() -> list[dict]:
    try:
        return httpx.get(f"{BACKEND}/api/schema", timeout=8).json().get("databases", [])
    except Exception:
        return []


@st.cache_data(ttl=600)
def fetch_examples() -> list[str]:
    try:
        return httpx.get(f"{BACKEND}/api/examples", timeout=5).json().get("examples", [])
    except Exception:
        return []


def call_query_api(question: str, db_id: str, history: list[dict],
                   execute: bool = True) -> dict:
    try:
        r = httpx.post(
            f"{BACKEND}/api/query",
            json={
                "question": question,
                "db_id": db_id,
                "execute": execute,
                "max_rows": 1000,
                "conversation_history": history,
            },
            timeout=120,
        )
        return r.json()
    except httpx.TimeoutException:
        return {"error": "Request timed out (120 s). Try a simpler question."}
    except Exception as e:
        return {"error": f"Backend unreachable: {e}"}


def build_chart(df: pd.DataFrame, chart_type: str) -> go.Figure | None:
    num_cols = df.select_dtypes(include="number").columns.tolist()
    cat_cols = df.select_dtypes(exclude="number").columns.tolist()
    if not num_cols or chart_type == "none":
        return None
    if chart_type == "line":
        x = next((c for c in cat_cols if any(h in c.upper() for h in _DATE_HINTS)),
                  cat_cols[0] if cat_cols else df.columns[0])
        return px.line(df, x=x, y=num_cols[0], template="plotly_white",
                       title=f"{num_cols[0]} over {x}")
    if chart_type == "bar" and cat_cols:
        return px.bar(df, x=cat_cols[0], y=num_cols[0], template="plotly_white",
                      color=num_cols[0], color_continuous_scale="Blues",
                      title=f"{num_cols[0]} by {cat_cols[0]}")
    if chart_type == "scatter" and len(num_cols) >= 2:
        return px.scatter(df, x=num_cols[0], y=num_cols[1], template="plotly_white")
    if chart_type == "histogram" and num_cols:
        return px.histogram(df, x=num_cols[0], template="plotly_white")
    if cat_cols and num_cols:
        return px.bar(df, x=cat_cols[0], y=num_cols[0], template="plotly_white")
    return None


# ── Session state ──────────────────────────────────────────────────────────────
for key, default in [
    ("question", ""), ("result", None),
    ("history", []), ("run_query", False),
    ("preview_only", False), ("selected_db_id", ""),
]:
    if key not in st.session_state:
        st.session_state[key] = default


# ── Health check ───────────────────────────────────────────────────────────────
try:
    httpx.get(f"{BACKEND}/api/health", timeout=3).raise_for_status()
except Exception:
    st.error("⚠️ Backend not reachable.  Start: `uvicorn backend.main:app --reload`")
    st.stop()


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.title("🏦 NL-SQL")
    st.caption("Banking Analytics · Natural Language Interface")
    st.divider()

    # ── Database selector ──────────────────────────────────────────────────
    dbs = fetch_databases()
    if dbs:
        db_labels = {d["id"]: f"{d['name']} ({d['id']})" for d in dbs}
        db_ids    = [d["id"] for d in dbs]
        selected  = st.selectbox(
            "🗄 Active database",
            options=db_ids,
            format_func=lambda x: db_labels.get(x, x),
            key="db_selector",
        )
        st.session_state.selected_db_id = selected

        # Show description of selected DB
        sel_db = next((d for d in dbs if d["id"] == selected), None)
        if sel_db and sel_db.get("description"):
            st.caption(sel_db["description"][:120])
    else:
        st.warning("No databases found — run ingestion first.")
        st.session_state.selected_db_id = ""

    st.divider()

    # ── Example questions ──────────────────────────────────────────────────
    st.subheader("💡 Try a question")
    for ex in fetch_examples():
        if st.button(ex, key=f"ex_{ex[:28]}"):
            st.session_state.question  = ex
            st.session_state.run_query = True
            st.session_state.history   = []
            st.rerun()

    st.divider()

    # ── Schema explorer (scoped to selected DB) ────────────────────────────
    st.subheader("📂 Schema")
    schema_data = fetch_schema()
    sel_db_schema = next(
        (d for d in schema_data if d["id"] == st.session_state.selected_db_id), None
    )
    if sel_db_schema:
        tables  = sel_db_schema.get("tables", []) or []
        domains = list({t.get("domain") for t in tables if t.get("domain")})

        if domains:
            domain_filter = st.selectbox("Filter by domain", ["All"] + sorted(domains))
        else:
            domain_filter = "All"

        shown = [t for t in tables if domain_filter == "All"
                 or t.get("domain") == domain_filter]

        for tbl in shown[:60]:   # cap at 60 for sidebar performance
            view_badge = " 👁" if tbl.get("is_view") else ""
            rc = tbl.get("row_count_approx", 0)
            rc_str = f"  ~{rc:,} rows" if rc else ""
            with st.expander(f"🗄 {tbl['name']}{view_badge}{rc_str}"):
                if tbl.get("description"):
                    st.caption(tbl["description"][:100])
                if tbl.get("domain"):
                    st.caption(f"Domain: {tbl['domain']}")
    else:
        st.info("Run ingestion to populate the schema explorer.\n\n"
                "`python -m ingestion.ingest_schema`")

    st.divider()

    # ── Conversation history ───────────────────────────────────────────────
    if st.session_state.history:
        st.subheader("🗒 Conversation")
        for turn in st.session_state.history[-6:]:
            icon = "👤" if turn["role"] == "user" else "🤖"
            st.caption(f"{icon} {turn['content'][:72]}…")
        if st.button("🗑 Clear conversation"):
            st.session_state.history = []
            st.session_state.result  = None
            st.rerun()

    st.divider()
    st.caption("Data stays on-prem. Only schema metadata sent to Gemini.")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN AREA
# ══════════════════════════════════════════════════════════════════════════════

st.header("Ask a question about your data")

col_input, col_btn = st.columns([5, 1])
with col_input:
    question_input = st.text_input(
        "question", label_visibility="collapsed",
        value=st.session_state.question,
        placeholder="e.g.  Show total loan disbursements by branch for the current quarter",
        key="question_input",
    )
with col_btn:
    ask_clicked = st.button("Ask ↵", type="primary", use_container_width=True)

if ask_clicked and question_input.strip():
    st.session_state.question  = question_input.strip()
    st.session_state.run_query = True
    st.session_state.preview_only = False

# ── Refinement buttons ─────────────────────────────────────────────────────────
result = st.session_state.result
if result and not result.get("error") and result.get("rows"):
    st.markdown("**Refine:**")
    r_cols = st.columns(5)
    for i, (label, follow_up) in enumerate([
        ("📅 This month",   "Filter results to this month only"),
        ("📅 This quarter", "Filter results to the current quarter only"),
        ("🔝 Top 10",       "Show only the top 10 results by the main metric"),
        ("📈 Show chart",   "Visualise the same data as a chart"),
        ("🏢 By branch",    "Break down the results by branch"),
    ]):
        with r_cols[i]:
            if st.button(label, key=f"ref_{i}"):
                st.session_state.question  = follow_up
                st.session_state.run_query = True
                st.rerun()


# ── Execute query ──────────────────────────────────────────────────────────────
if st.session_state.run_query and st.session_state.question:
    st.session_state.run_query = False
    q = st.session_state.question

    with st.spinner(f"Analysing: *{q}*"):
        api_result = call_query_api(
            question=q,
            db_id=st.session_state.selected_db_id,
            history=st.session_state.history,
            execute=not st.session_state.preview_only,
        )

    st.session_state.result = api_result

    if not api_result.get("error"):
        st.session_state.history.append({"role": "user",  "content": q})
        if api_result.get("sql"):
            st.session_state.history.append(
                {"role": "model", "content": api_result["sql"]}
            )
        st.session_state.history = st.session_state.history[-10:]


# ══════════════════════════════════════════════════════════════════════════════
# RESULTS
# ══════════════════════════════════════════════════════════════════════════════

result = st.session_state.result

if result is None:
    st.info("Type a question above or pick an example from the sidebar.")
    st.stop()

if result.get("error"):
    st.error(f"❌ {result['error']}")
    if result.get("sql"):
        with st.expander("Generated SQL (debug)"):
            st.code(result["sql"], language="sql")
    st.stop()

# ── Matched QueryPattern banner ────────────────────────────────────────────────
mp = result.get("matched_pattern")
if mp:
    st.markdown(
        f'<span class="pattern-badge">♻ Reused stored pattern '
        f'(similarity {mp["similarity"]:.0%} · used {mp["success_count"]}×)</span>',
        unsafe_allow_html=True,
    )

# ── Metrics row ────────────────────────────────────────────────────────────────
meta = result.get("meta", {})
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Rows",          meta.get("row_count", 0))
m2.metric("Exec time",     f"{meta.get('execution_ms', 0)} ms")
m3.metric("Tables used",   len(meta.get("tables_used", [])))
m4.metric("Chart",         (meta.get("chart_type") or "none").capitalize())
m5.metric("DB",            meta.get("db_id", ""))

# ── PII warnings ───────────────────────────────────────────────────────────────
for warn in result.get("warnings", []):
    st.markdown(f'<span class="pii-warn">🔒 {warn}</span>', unsafe_allow_html=True)

# ── Result tabs ────────────────────────────────────────────────────────────────
columns = result.get("columns", [])
rows    = result.get("rows", [])

tab_table, tab_chart, tab_summary, tab_sql, tab_cypher = st.tabs(
    ["📊 Table", "📈 Chart", "💬 Summary", "🔍 SQL", "🔗 Cypher"]
)

# ── Tab 1: Table ───────────────────────────────────────────────────────────────
with tab_table:
    if not rows:
        st.info("Query returned no rows.")
    else:
        df = pd.DataFrame(rows, columns=columns)
        st.dataframe(df, use_container_width=True, height=420)
        st.caption(f"{len(df):,} rows · {len(columns)} columns")

        # Excel download
        try:
            import io, openpyxl
            buf = io.BytesIO()
            df.to_excel(buf, index=False, engine="openpyxl")
            st.download_button(
                "⬇ Download Excel", buf.getvalue(),
                file_name="query_results.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        except Exception:
            pass

# ── Tab 2: Chart ───────────────────────────────────────────────────────────────
with tab_chart:
    chart_type = result.get("chart_type", "none")
    if not rows:
        st.info("No data to chart.")
    elif chart_type == "none":
        st.info("No chart detected. Try: *'Show the same data grouped by month'*")
    else:
        df  = pd.DataFrame(rows, columns=columns)
        fig = build_chart(df, chart_type)
        if fig:
            st.plotly_chart(fig, use_container_width=True)
        override = st.selectbox(
            "Override chart type", ["auto", "bar", "line", "scatter", "histogram"],
            key="chart_override",
        )
        if override != "auto":
            fig2 = build_chart(df, override)
            if fig2:
                st.plotly_chart(fig2, use_container_width=True)

# ── Tab 3: Summary ─────────────────────────────────────────────────────────────
with tab_summary:
    summary = result.get("summary", "")
    if summary:
        st.markdown(f"### 💬 {summary}")
        tables_used = meta.get("tables_used", [])
        if tables_used:
            st.caption("Tables: `" + "` · `".join(tables_used) + "`")
    else:
        st.info("No summary available.")

# ── Tab 4: SQL ─────────────────────────────────────────────────────────────────
with tab_sql:
    sql = result.get("sql", "")
    if sql:
        st.code(sql, language="sql")
        st.caption("⚠️ Auto-generated SQL — review before using in critical reports.")

        # Show matched pattern SQL for comparison
        if mp and mp.get("sql"):
            with st.expander("📚 Matched pattern SQL (for reference)"):
                st.caption(
                    f"Question: *{mp['nl_question']}*  "
                    f"| Similarity: {mp['similarity']:.0%}  "
                    f"| Used {mp['success_count']}×"
                )
                st.code(mp["sql"], language="sql")
    else:
        st.info("No SQL to display.")

# ── Tab 5: Cypher — schema discovery queries (preserved) ──────────────────────
with tab_cypher:
    st.markdown("#### Schema discovery Cypher")
    st.caption(
        "These are the Neo4j Cypher queries executed to find relevant tables "
        "and join paths for this question. They are stored alongside each "
        "QueryPattern for future reuse and debugging."
    )

    schema_cypher = result.get("schema_cypher", "")
    if schema_cypher:
        st.code(schema_cypher, language="cypher")
    else:
        st.info("No Cypher queries recorded for this request.")

    # Also show matched pattern's stored Cypher if available
    if mp and mp.get("schema_cypher"):
        with st.expander("📚 Stored Cypher from matched pattern"):
            st.caption(
                "This is the Cypher that was used when this pattern was "
                "originally stored — reused to compare against current results."
            )
            st.code(mp["schema_cypher"], language="cypher")
