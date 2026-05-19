"""
frontend/app.py  (v2 + Phase 3A)

New in Phase 3A:
  - 👍 / 👎 feedback buttons below every result
  - Corrected SQL text area (optional, shown after thumbs-down)
  - 🛠 Agent trace tab: validation issues, healing attempts, cost info
  - Healing indicator badge when SelfHealingAgent recovered the query
  - skip_explain_plan toggle in sidebar (faster for dev/testing)
"""

import httpx
import io
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

BACKEND     = "http://localhost:8000"
_DATE_HINTS = {"DT", "DATE", "MONTH", "MON", "YEAR", "PERIOD", "QTR", "WEEK"}

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
    background:#f8f9fa; border-radius:8px; padding:8px 12px; }
.pii-warn   { background:#fff3cd; color:#856404; border-radius:4px;
              padding:4px 10px; font-size:0.82rem; display:inline-block; margin-bottom:4px; }
.heal-badge { background:#d4edda; color:#155724; border-radius:4px;
              padding:3px 8px; font-size:0.80rem; display:inline-block; }
.fail-badge { background:#f8d7da; color:#721c24; border-radius:4px;
              padding:3px 8px; font-size:0.80rem; display:inline-block; }
.pat-badge  { background:#d1ecf1; color:#0c5460; border-radius:4px;
              padding:3px 8px; font-size:0.80rem; display:inline-block; }
</style>
""", unsafe_allow_html=True)


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
                   execute: bool = True, skip_explain: bool = False) -> dict:
    try:
        r = httpx.post(
            f"{BACKEND}/api/query",
            json={
                "question":             question,
                "db_id":                db_id,
                "execute":              execute,
                "max_rows":             1000,
                "conversation_history": history,
                "skip_explain_plan":    skip_explain,
            },
            timeout=120,
        )
        return r.json()
    except httpx.TimeoutException:
        return {"error": "Request timed out (120 s). Try a simpler question."}
    except Exception as e:
        return {"error": f"Backend unreachable: {e}"}


def call_feedback_api(nl_question: str, db_id: str,
                      rating: int, corrected_sql: str | None = None) -> dict:
    try:
        r = httpx.post(
            f"{BACKEND}/api/feedback",
            json={
                "nl_question":   nl_question,
                "db_id":         db_id,
                "rating":        rating,
                "corrected_sql": corrected_sql or None,
            },
            timeout=10,
        )
        return r.json()
    except Exception as e:
        return {"status": "error", "detail": str(e)}


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
    ("question", ""), ("result", None), ("history", []),
    ("run_query", False), ("preview_only", False),
    ("selected_db_id", ""), ("feedback_given", False),
    ("show_correction", False),
]:
    if key not in st.session_state:
        st.session_state[key] = default


# ── Health check ───────────────────────────────────────────────────────────────
try:
    httpx.get(f"{BACKEND}/api/health", timeout=3).raise_for_status()
except Exception:
    st.error("⚠️ Backend not reachable.  `uvicorn backend.main:app --reload`")
    st.stop()


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.title("🏦 NL-SQL")
    st.caption("Banking Analytics · Natural Language Interface")
    st.divider()

    # ── DB selector ────────────────────────────────────────────────────────
    dbs = fetch_databases()
    if dbs:
        db_ids    = [d["id"] for d in dbs]
        db_labels = {d["id"]: f"{d['name']} ({d['id']})" for d in dbs}
        selected  = st.selectbox(
            "🗄 Active database", options=db_ids,
            format_func=lambda x: db_labels.get(x, x), key="db_selector",
        )
        st.session_state.selected_db_id = selected
        sel_db = next((d for d in dbs if d["id"] == selected), None)
        if sel_db and sel_db.get("description"):
            st.caption(sel_db["description"][:120])
    else:
        st.warning("No databases — run ingestion first.")
        st.session_state.selected_db_id = ""

    st.divider()

    # ── Dev options ────────────────────────────────────────────────────────
    with st.expander("⚙️ Options"):
        skip_explain = st.toggle(
            "Skip EXPLAIN PLAN", value=False,
            help="Faster in dev — skips Oracle cost estimation."
        )
        st.session_state.skip_explain = skip_explain

    st.divider()

    # ── Example questions ──────────────────────────────────────────────────
    st.subheader("💡 Try a question")
    for ex in fetch_examples():
        if st.button(ex, key=f"ex_{ex[:28]}"):
            st.session_state.question      = ex
            st.session_state.run_query     = True
            st.session_state.history       = []
            st.session_state.feedback_given= False
            st.rerun()

    st.divider()

    # ── Schema explorer ────────────────────────────────────────────────────
    st.subheader("📂 Schema")
    schema_data   = fetch_schema()
    sel_db_schema = next(
        (d for d in schema_data if d["id"] == st.session_state.selected_db_id), None
    )
    if sel_db_schema:
        tables  = sel_db_schema.get("tables", []) or []
        domains = sorted({t.get("domain") for t in tables if t.get("domain")})
        domain_filter = st.selectbox("Domain", ["All"] + domains) if domains else "All"
        shown = [t for t in tables
                 if domain_filter == "All" or t.get("domain") == domain_filter]
        for tbl in shown[:60]:
            view_badge = " 👁" if tbl.get("is_view") else ""
            rc         = tbl.get("row_count_approx", 0)
            rc_str     = f"  ~{rc:,}" if rc else ""
            with st.expander(f"🗄 {tbl['name']}{view_badge}{rc_str}"):
                if tbl.get("description"):
                    st.caption(tbl["description"][:100])
                if tbl.get("domain"):
                    st.caption(f"Domain: {tbl['domain']}")
    else:
        st.info("Run ingestion:\n`python -m ingestion.ingest_schema`")

    st.divider()

    # ── Conversation ───────────────────────────────────────────────────────
    if st.session_state.history:
        st.subheader("🗒 Conversation")
        for turn in st.session_state.history[-6:]:
            icon = "👤" if turn["role"] == "user" else "🤖"
            st.caption(f"{icon} {turn['content'][:72]}…")
        if st.button("🗑 Clear"):
            st.session_state.history       = []
            st.session_state.result        = None
            st.session_state.feedback_given= False
            st.rerun()

    st.divider()
    st.caption("Data stays on-prem · Only schema metadata → Gemini")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN AREA
# ══════════════════════════════════════════════════════════════════════════════

st.header("Ask a question about your data")

col_input, col_btn = st.columns([5, 1])
with col_input:
    question_input = st.text_input(
        "question", label_visibility="collapsed",
        value=st.session_state.question,
        placeholder="e.g. Show total loan disbursements by branch for the current quarter",
        key="question_input",
    )
with col_btn:
    ask_clicked = st.button("Ask ↵", type="primary", use_container_width=True)

if ask_clicked and question_input.strip():
    st.session_state.question       = question_input.strip()
    st.session_state.run_query      = True
    st.session_state.preview_only   = False
    st.session_state.feedback_given = False
    st.session_state.show_correction= False

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
                st.session_state.question       = follow_up
                st.session_state.run_query      = True
                st.session_state.feedback_given = False
                st.rerun()


# ── Execute query ──────────────────────────────────────────────────────────────
if st.session_state.run_query and st.session_state.question:
    st.session_state.run_query = False
    q = st.session_state.question
    with st.spinner(f"Analysing: *{q}*"):
        api_result = call_query_api(
            question     = q,
            db_id        = st.session_state.selected_db_id,
            history      = st.session_state.history,
            execute      = not st.session_state.preview_only,
            skip_explain = st.session_state.get("skip_explain", False),
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
    st.info("Type a question above or choose an example from the sidebar.")
    st.stop()

if result.get("error"):
    st.error(f"❌ {result['error']}")
    if result.get("sql"):
        with st.expander("Last attempted SQL"):
            st.code(result["sql"], language="sql")
    # Show agent trace even on errors for debugging
    trace = result.get("agent_trace")
    if trace and (trace.get("healing_attempts") or trace.get("validation")):
        with st.expander("🛠 Agent trace (debug)"):
            st.json(trace)
    st.stop()

# ── Status badges ──────────────────────────────────────────────────────────────
meta = result.get("meta", {})

badge_cols = st.columns([2, 2, 6])
with badge_cols[0]:
    if result.get("matched_pattern"):
        mp = result["matched_pattern"]
        st.markdown(
            f'<span class="pat-badge">♻ Pattern reused '
            f'({mp["similarity"]:.0%} · {mp["success_count"]}×)</span>',
            unsafe_allow_html=True,
        )
with badge_cols[1]:
    if meta.get("healed"):
        st.markdown(
            '<span class="heal-badge">🔧 Auto-recovered by SelfHealingAgent</span>',
            unsafe_allow_html=True,
        )

# ── Metrics ────────────────────────────────────────────────────────────────────
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Rows",        meta.get("row_count", 0))
m2.metric("Exec time",   f"{meta.get('execution_ms', 0)} ms")
m3.metric("Tables",      len(meta.get("tables_used", [])))
m4.metric("Chart",       (meta.get("chart_type") or "none").capitalize())
m5.metric("DB",          meta.get("db_id", ""))

for warn in result.get("warnings", []):
    st.markdown(f'<span class="pii-warn">🔒 {warn}</span>', unsafe_allow_html=True)

# ── Result tabs ────────────────────────────────────────────────────────────────
columns = result.get("columns", [])
rows    = result.get("rows",    [])

tab_table, tab_chart, tab_summary, tab_sql, tab_cypher, tab_agent = st.tabs(
    ["📊 Table", "📈 Chart", "💬 Summary", "🔍 SQL", "🔗 Cypher", "🛠 Agent"]
)

# ── Tab 1: Table ───────────────────────────────────────────────────────────────
with tab_table:
    if not rows:
        st.info("Query returned no rows.")
    else:
        df = pd.DataFrame(rows, columns=columns)
        st.dataframe(df, use_container_width=True, height=420)
        st.caption(f"{len(df):,} rows · {len(columns)} columns")
        try:
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
        st.info("No chart auto-detected. Try: *'Show the same data grouped by month'*")
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
        st.caption("⚠️ Auto-generated — review before using in critical reports.")
        mp = result.get("matched_pattern")
        if mp and mp.get("sql"):
            with st.expander("📚 Matched pattern SQL (reference)"):
                st.caption(
                    f"*{mp['nl_question']}*  |  "
                    f"Similarity: {mp['similarity']:.0%}  |  "
                    f"Used: {mp['success_count']}×"
                )
                st.code(mp["sql"], language="sql")
    else:
        st.info("No SQL to display.")

# ── Tab 5: Cypher ──────────────────────────────────────────────────────────────
with tab_cypher:
    st.markdown("#### Schema discovery Cypher")
    st.caption(
        "Neo4j Cypher queries that discovered the relevant tables and join paths. "
        "Stored in every QueryPattern for future reuse and audit."
    )
    schema_cypher = result.get("schema_cypher", "")
    if schema_cypher:
        st.code(schema_cypher, language="cypher")
    else:
        st.info("No Cypher recorded for this request.")
    mp = result.get("matched_pattern")
    if mp and mp.get("schema_cypher"):
        with st.expander("📚 Stored Cypher from matched pattern"):
            st.code(mp["schema_cypher"], language="cypher")

# ── Tab 6: Agent trace (Phase 3A) ─────────────────────────────────────────────
with tab_agent:
    st.markdown("#### Agent decisions")
    trace = result.get("agent_trace") or {}

    # ── Validation result ──────────────────────────────────────────────────
    val = trace.get("validation") or {}
    if val:
        st.markdown("**ValidationAgent**")
        vcols = st.columns(3)
        vcols[0].metric("Valid",         "✅ Yes" if val.get("valid") else "❌ No")
        vcols[1].metric("Cost estimate",  str(val.get("cost_estimate") or "N/A"))
        vcols[2].metric("Cost blocked",   "Yes" if val.get("cost_blocked") else "No")

        issues = val.get("issues", [])
        if issues:
            for issue in issues:
                severity = issue.get("severity", "")
                icon     = "🔴" if severity == "error" else "🟡"
                st.markdown(
                    f"{icon} **{issue.get('code', '')}** — {issue.get('message', '')}"
                )
        else:
            st.success("No validation issues.")

        vwarnings = val.get("warnings", [])
        if vwarnings:
            for w in vwarnings:
                st.warning(w)

    st.divider()

    # ── Healing attempts ───────────────────────────────────────────────────
    heal_attempts = trace.get("healing_attempts", [])
    if heal_attempts:
        st.markdown(f"**SelfHealingAgent** — {len(heal_attempts)} attempt(s)")
        for attempt in heal_attempts:
            outcome = attempt.get("outcome", "")
            icon    = "✅" if outcome == "success" else "❌"
            label   = f"{icon} Attempt {attempt.get('attempt')} · {attempt.get('error_code')} · {outcome}"
            with st.expander(label):
                if attempt.get("error_msg"):
                    st.error(attempt["error_msg"])
                if attempt.get("sql_tried"):
                    st.code(attempt["sql_tried"], language="sql")
    elif meta.get("healed"):
        st.success("Query was healed successfully.")
    else:
        st.info("No healing was needed — SQL passed validation on first attempt.")

    st.divider()
    st.caption(
        f"Total attempts: {trace.get('total_attempts', 1)}  |  "
        f"Healed: {'Yes' if trace.get('healed') else 'No'}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# FEEDBACK  (Phase 3A)
# ══════════════════════════════════════════════════════════════════════════════

if result and not result.get("error") and result.get("sql"):
    st.divider()
    st.markdown("**Was this result correct?**")
    fb_cols = st.columns([1, 1, 8])

    with fb_cols[0]:
        if st.button("👍 Yes", key="thumbs_up",
                     disabled=st.session_state.feedback_given):
            fb = call_feedback_api(
                nl_question  = st.session_state.question,
                db_id        = st.session_state.selected_db_id,
                rating       = 5,
            )
            st.session_state.feedback_given = True
            st.success("Feedback recorded — pattern weight increased.")
            st.rerun()

    with fb_cols[1]:
        if st.button("👎 No", key="thumbs_down",
                     disabled=st.session_state.feedback_given):
            st.session_state.show_correction = True
            st.session_state.feedback_given  = False   # allow submit after correction

    if st.session_state.get("show_correction") and not st.session_state.feedback_given:
        corrected = st.text_area(
            "Optional: paste the correct SQL (helps future queries)",
            height=100, key="corrected_sql_input",
        )
        if st.button("Submit feedback", type="primary", key="submit_feedback"):
            fb = call_feedback_api(
                nl_question  = st.session_state.question,
                db_id        = st.session_state.selected_db_id,
                rating       = 1,
                corrected_sql= corrected.strip() if corrected.strip() else None,
            )
            st.session_state.feedback_given  = True
            st.session_state.show_correction = False
            if corrected.strip():
                st.warning("Feedback recorded — pattern de-weighted and SQL corrected.")
            else:
                st.warning("Feedback recorded — pattern de-weighted.")
            st.rerun()

    if st.session_state.feedback_given:
        st.caption("✓ Feedback submitted")
