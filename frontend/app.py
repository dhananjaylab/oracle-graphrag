"""
frontend/app.py  (v2 + Phase 3A + 3B + Phase 3C)

Phase 3C additions:
  - Feature-flag toggle: "Linear pipeline" vs "Supervisor (multi-DB)"
  - SSE streaming: real-time tool-call status updates during supervisor run
  - 🤖 Supervisor trace tab: every tool call, args, result, elapsed time
  - 📊 Multi-DB results panel: per-database sub-results when supervisor queries 2+ DBs
  - Summarised conversation context: compressed prior turns sent to supervisor
"""

import httpx
import io
import json
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
[data-testid="stSidebar"]      { min-width:290px; max-width:330px; }
[data-testid="metric-container"]{ background:#f8f9fa; border-radius:8px; padding:8px 12px; }
.pii-warn   { background:#fff3cd; color:#856404; border-radius:4px;
              padding:4px 10px; font-size:.82rem; display:inline-block; margin-bottom:4px; }
.heal-badge { background:#d4edda; color:#155724; border-radius:4px;
              padding:3px 8px; font-size:.80rem; display:inline-block; }
.pat-badge  { background:#d1ecf1; color:#0c5460; border-radius:4px;
              padding:3px 8px; font-size:.80rem; display:inline-block; }
.sup-badge  { background:#e8d5f5; color:#5b1a8e; border-radius:4px;
              padding:3px 8px; font-size:.80rem; display:inline-block; }
.partial-badge { background:#fff3cd; color:#856404; border-radius:4px;
              padding:3px 8px; font-size:.80rem; display:inline-block; }
.tool-row   { border-left:3px solid #dee2e6; padding:4px 0 4px 10px;
              margin:4px 0; font-size:.82rem; }
.tool-ok    { border-color:#28a745; }
.tool-err   { border-color:#dc3545; }
.tool-run   { border-color:#ffc107; }
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

def call_linear_api(question: str, db_id: str, history: list[dict],
                    execute: bool = True, skip_explain: bool = False) -> dict:
    try:
        r = httpx.post(f"{BACKEND}/api/query", json={
            "question": question, "db_id": db_id, "execute": execute,
            "max_rows": 1000, "conversation_history": history,
            "skip_explain_plan": skip_explain,
        }, timeout=120)
        return r.json()
    except httpx.TimeoutException:
        return {"error": "Request timed out (120s). Try a simpler question."}
    except Exception as e:
        return {"error": f"Backend unreachable: {e}"}

def call_feedback_api(nl_question: str, db_id: str,
                      rating: int, corrected_sql: str | None = None) -> dict:
    try:
        r = httpx.post(f"{BACKEND}/api/feedback", json={
            "nl_question": nl_question, "db_id": db_id,
            "rating": rating, "corrected_sql": corrected_sql or None,
        }, timeout=10)
        return r.json()
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ── SSE streaming for supervisor mode ─────────────────────────────────────────

def run_supervisor_streaming(question: str, conv_history: list[dict]) -> dict:
    """
    Connect to /api/supervisor SSE stream.
    Displays live status updates in st.status() and returns the
    final 'finish' payload once the supervisor completes.
    """
    payload = {
        "question":             question,
        "max_rows":             1000,
        "conversation_history": conv_history,
    }

    events_log:  list[dict] = []
    finish_data: dict | None = None

    _TOOL_ICONS = {
        "list_databases":    "🗄",
        "semantic_search":   "🔍",
        "get_table_details": "📋",
        "get_join_path":     "🔗",
        "get_cross_db_hints":"🌐",
        "search_patterns":   "♻",
        "check_read_only":   "🛡",
        "explain_plan":      "📐",
        "execute_query":     "⚡",
        "store_pattern":     "💾",
        "get_schema":        "🏛",
        "get_schema_summary":"📂",
        "finish":            "✅",
    }

    with st.status("🤖 Supervisor running…", expanded=True) as status_box:
        live_log = st.empty()
        log_lines: list[str] = []

        try:
            with httpx.Client(timeout=180) as client:
                with client.stream("POST", f"{BACKEND}/api/supervisor",
                                   json=payload) as resp:
                    resp.raise_for_status()

                    for raw_line in resp.iter_lines():
                        if not raw_line:
                            continue

                        # SSE: lines starting with "event:" or "data:"
                        if raw_line.startswith("event:"):
                            current_event_type = raw_line[6:].strip()
                            continue
                        if not raw_line.startswith("data:"):
                            continue

                        try:
                            data = json.loads(raw_line[5:].strip())
                        except json.JSONDecodeError:
                            continue

                        event_type = data.get("_type") or locals().get("current_event_type", "")
                        events_log.append({"type": event_type, "data": data})

                        # ── Render live status update ──────────────────────
                        if event_type == "thinking":
                            dbs = ", ".join(data.get("databases", []))
                            log_lines.append(f"🧠 Analysing… databases: **{dbs}**")

                        elif event_type == "tool_call":
                            icon = _TOOL_ICONS.get(data.get("tool_name", ""), "🔧")
                            msg  = data.get("message", data.get("tool_name", ""))
                            log_lines.append(f"{icon} `{data.get('tool_name')}` — {msg}")
                            status_box.update(label=f"🤖 {msg}")

                        elif event_type == "tool_result":
                            ok  = data.get("ok", True)
                            sym = "✅" if ok else "❌"
                            log_lines.append(
                                f"  {sym} {data.get('tool_name')} → "
                                f"{data.get('summary', '')} "
                                f"({data.get('elapsed_ms', 0)}ms)"
                            )

                        elif event_type == "sql":
                            db      = data.get("db_id", "")
                            rows    = data.get("row_count", 0)
                            cols    = ", ".join(data.get("columns", [])[:4])
                            log_lines.append(f"  📊 **{db}**: {rows:,} rows — [{cols}…]")

                        elif event_type == "finish":
                            finish_data = data
                            partial     = data.get("partial", False)
                            iters       = data.get("total_iterations", 0)
                            ms          = data.get("total_ms", 0)
                            dbs         = ", ".join(data.get("dbs_queried", []))
                            if partial:
                                log_lines.append(f"⚠ Partial result — {data.get('missing_info', '')}")
                                status_box.update(label="🤖 Supervisor — partial result", state="error")
                            else:
                                log_lines.append(f"✅ Complete — {iters} iterations · {ms}ms · DBs: {dbs}")
                                status_box.update(label="🤖 Supervisor complete", state="complete")

                        elif event_type == "error":
                            log_lines.append(f"❌ Error: {data.get('message', '')}")
                            status_box.update(label="🤖 Supervisor — error", state="error")

                        # Refresh live log (last 12 lines)
                        live_log.markdown("\n\n".join(log_lines[-12:]))

        except httpx.TimeoutException:
            return {"error": "Supervisor timed out (180s). Try a simpler question."}
        except Exception as exc:
            return {"error": f"Supervisor stream failed: {exc}"}

    if finish_data is None:
        return {"error": "Supervisor ended without a finish event."}

    # Attach raw event log for trace tab
    finish_data["_events_log"] = events_log
    return finish_data


# ── Chart builder ──────────────────────────────────────────────────────────────

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
                      color=num_cols[0], color_continuous_scale="Blues")
    if chart_type == "scatter" and len(num_cols) >= 2:
        return px.scatter(df, x=num_cols[0], y=num_cols[1], template="plotly_white")
    if chart_type == "histogram" and num_cols:
        return px.histogram(df, x=num_cols[0], template="plotly_white")
    if cat_cols and num_cols:
        return px.bar(df, x=cat_cols[0], y=num_cols[0], template="plotly_white")
    return None


# ── Session state ──────────────────────────────────────────────────────────────

for key, default in [
    ("question", ""), ("result", None), ("linear_history", []),
    ("supervisor_conv_history", []), ("run_query", False),
    ("supervisor_mode", False), ("selected_db_id", ""),
    ("feedback_given", False), ("show_correction", False),
]:
    if key not in st.session_state:
        st.session_state[key] = default


# ── Health check ───────────────────────────────────────────────────────────────
try:
    hc = httpx.get(f"{BACKEND}/api/health", timeout=3).json()
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

    # ── Mode toggle (Phase 3C feature flag) ───────────────────────────────
    supervisor_mode = st.toggle(
        "🤖 Supervisor mode (multi-DB)",
        value=st.session_state.supervisor_mode,
        help=(
            "OFF → Linear pipeline (single DB, fast, deterministic)\n"
            "ON  → Gemini supervisor (multi-DB, dynamic tool calling, SSE streaming)"
        ),
    )
    if supervisor_mode != st.session_state.supervisor_mode:
        st.session_state.supervisor_mode = supervisor_mode
        st.session_state.result          = None
        st.session_state.feedback_given  = False
        st.rerun()

    if supervisor_mode:
        st.caption("🤖 Supervisor: Gemini selects tools dynamically")
        mcp_oracle = hc.get("mcp_servers", {}).get("oracle", "unknown")
        mcp_neo4j  = hc.get("mcp_servers", {}).get("neo4j",  "unknown")
        st.caption(f"Oracle MCP: {mcp_oracle}  |  Neo4j MCP: {mcp_neo4j}")
    else:
        st.caption("⚡ Linear: fixed 14-step pipeline")

    st.divider()

    # ── DB selector (linear mode only) ────────────────────────────────────
    if not supervisor_mode:
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
    else:
        st.info("Supervisor queries all relevant databases automatically.")

    st.divider()

    # ── Dev options ────────────────────────────────────────────────────────
    with st.expander("⚙️ Options"):
        skip_explain = st.toggle("Skip EXPLAIN PLAN", value=False,
                                 help="Faster in dev — skips Oracle cost check.")
        st.session_state.skip_explain = skip_explain

    st.divider()

    # ── Example questions ──────────────────────────────────────────────────
    st.subheader("💡 Try a question")
    examples = fetch_examples()
    if supervisor_mode:
        examples = [
            "Show loans from fincore that are classified as NPA in riskdb",
            "Compare credit ratings vs loan outstanding by customer",
            "What is total NPA exposure across all product segments?",
        ] + examples[:4]
    for ex in examples[:7]:
        if st.button(ex, key=f"ex_{ex[:28]}"):
            st.session_state.question       = ex
            st.session_state.run_query      = True
            st.session_state.feedback_given = False
            st.rerun()

    st.divider()

    # ── Schema explorer ────────────────────────────────────────────────────
    st.subheader("📂 Schema")
    schema_data   = fetch_schema()
    sel_db_schema = next(
        (d for d in schema_data if d["id"] == st.session_state.selected_db_id), None
    ) if not supervisor_mode else None

    if sel_db_schema:
        tables  = sel_db_schema.get("tables", []) or []
        domains = sorted({t.get("domain") for t in tables if t.get("domain")})
        dfilt   = st.selectbox("Domain", ["All"] + domains) if domains else "All"
        shown   = [t for t in tables if dfilt == "All" or t.get("domain") == dfilt]
        for tbl in shown[:60]:
            badge = " 👁" if tbl.get("is_view") else ""
            rc    = f"  ~{tbl.get('row_count_approx',0):,}" if tbl.get("row_count_approx") else ""
            with st.expander(f"🗄 {tbl['name']}{badge}{rc}"):
                if tbl.get("description"):
                    st.caption(tbl["description"][:100])
    elif supervisor_mode:
        for db in schema_data[:3]:
            st.caption(f"**{db['name']}** — {db.get('table_count',0)} tables")
    else:
        st.info("Run: `python -m ingestion.ingest_schema`")

    st.divider()

    # ── Conversation history ───────────────────────────────────────────────
    history_key = "supervisor_conv_history" if supervisor_mode else "linear_history"
    if st.session_state[history_key]:
        st.subheader("🗒 Session")
        hist = st.session_state[history_key]
        for turn in (hist[-3:] if supervisor_mode else hist[-6:]):
            q   = turn.get("question", turn.get("content", ""))[:72] if supervisor_mode else turn.get("content", "")[:72]
            dbs = turn.get("dbs_queried", [])
            db_str = f" [{','.join(dbs)}]" if dbs else ""
            st.caption(f"👤 {q}…{db_str}")
        if st.button("🗑 Clear session"):
            st.session_state[history_key] = []
            st.session_state.result       = None
            st.session_state.feedback_given = False
            st.rerun()

    st.divider()
    st.caption("Data stays on-prem · Schema metadata only → Gemini")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN AREA
# ══════════════════════════════════════════════════════════════════════════════

mode_label = "🤖 Supervisor (multi-DB)" if supervisor_mode else "⚡ Linear pipeline"
st.header(f"Ask a question about your data  —  {mode_label}")

col_input, col_btn = st.columns([5, 1])
with col_input:
    question_input = st.text_input(
        "question", label_visibility="collapsed",
        value=st.session_state.question,
        placeholder="e.g. Show NPA loans from fincore that are overdue 90+ days in riskdb",
        key="question_input",
    )
with col_btn:
    ask_clicked = st.button("Ask ↵", type="primary", use_container_width=True)

if ask_clicked and question_input.strip():
    st.session_state.question       = question_input.strip()
    st.session_state.run_query      = True
    st.session_state.feedback_given = False
    st.session_state.show_correction= False

# ── Refinement buttons (linear mode) ──────────────────────────────────────────
result = st.session_state.result
if result and not result.get("error") and not supervisor_mode:
    rows_key = "rows" if "rows" in result else None
    if rows_key and result.get(rows_key):
        st.markdown("**Refine:**")
        r_cols = st.columns(5)
        for i, (label, fu) in enumerate([
            ("📅 This month",   "Filter results to this month only"),
            ("📅 This quarter", "Filter results to the current quarter only"),
            ("🔝 Top 10",       "Show only the top 10 results by the main metric"),
            ("📈 Chart",        "Visualise as a chart"),
            ("🏢 By branch",    "Break down by branch"),
        ]):
            with r_cols[i]:
                if st.button(label, key=f"ref_{i}"):
                    st.session_state.question       = fu
                    st.session_state.run_query      = True
                    st.session_state.feedback_given = False
                    st.rerun()


# ── Execute query ──────────────────────────────────────────────────────────────
if st.session_state.run_query and st.session_state.question:
    st.session_state.run_query = False
    q = st.session_state.question

    if st.session_state.supervisor_mode:
        # ── Supervisor path: SSE streaming ────────────────────────────────
        api_result = run_supervisor_streaming(
            question     = q,
            conv_history = st.session_state.supervisor_conv_history,
        )
        st.session_state.result = {"_mode": "supervisor", **api_result}

        if not api_result.get("error"):
            # Build compressed context entry for next turn
            db_results = api_result.get("db_results", [])
            key_metrics: dict = {}
            for dbr in db_results[:2]:
                for col, stat in dbr.get("summary_stats", {}).items():
                    if col == "row_count":
                        continue
                    if "sum" in stat:
                        key_metrics[f"{dbr['db_id']}.{col}.sum"] = stat["sum"]
                    if len(key_metrics) >= 4:
                        break

            st.session_state.supervisor_conv_history.append({
                "question":    q,
                "dbs_queried": api_result.get("dbs_queried", []),
                "tables_used": api_result.get("tables_used", []),
                "row_count":   sum(r.get("row_count", 0) for r in db_results),
                "key_metrics": key_metrics,
                "partial":     api_result.get("partial", False),
                "missing_info":api_result.get("missing_info", ""),
            })
            st.session_state.supervisor_conv_history = st.session_state.supervisor_conv_history[-5:]

    else:
        # ── Linear path: synchronous ──────────────────────────────────────
        with st.spinner(f"Analysing: *{q}*"):
            api_result = call_linear_api(
                question     = q,
                db_id        = st.session_state.selected_db_id,
                history      = st.session_state.linear_history,
                execute      = True,
                skip_explain = st.session_state.get("skip_explain", False),
            )
        st.session_state.result = {"_mode": "linear", **api_result}

        if not api_result.get("error"):
            st.session_state.linear_history.append({"role": "user",  "content": q})
            if api_result.get("sql"):
                st.session_state.linear_history.append(
                    {"role": "model", "content": api_result["sql"]}
                )
            st.session_state.linear_history = st.session_state.linear_history[-10:]

    st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# RESULTS
# ══════════════════════════════════════════════════════════════════════════════

result = st.session_state.result
if result is None:
    st.info("Type a question above or choose an example from the sidebar.")
    st.stop()

mode = result.get("_mode", "linear")

if result.get("error"):
    st.error(f"❌ {result['error']}")
    if result.get("sql"):
        with st.expander("Last attempted SQL"):
            st.code(result["sql"], language="sql")
    trace = result.get("agent_trace")
    if trace:
        with st.expander("🛠 Agent trace"):
            st.json(trace)
    st.stop()


# ══════════════════════════════════════════════════════════════════════════════
# SUPERVISOR MODE RESULTS
# ══════════════════════════════════════════════════════════════════════════════

if mode == "supervisor":
    partial      = result.get("partial", False)
    dbs_queried  = result.get("dbs_queried", [])
    db_results   = result.get("db_results", [])
    tool_calls   = result.get("tool_calls", [])
    summary      = result.get("summary", "")
    merge_strat  = result.get("merge_strategy", "single_db")
    total_iters  = result.get("total_iterations", 0)
    total_ms     = result.get("total_ms", 0)

    # ── Status badges ──────────────────────────────────────────────────────
    badge_row = st.columns([2, 2, 2, 4])
    with badge_row[0]:
        st.markdown(
            f'<span class="sup-badge">🤖 Supervisor · {total_iters} iterations · {total_ms}ms</span>',
            unsafe_allow_html=True,
        )
    with badge_row[1]:
        if partial:
            st.markdown(
                f'<span class="partial-badge">⚠ Partial — {result.get("missing_info","")[:60]}</span>',
                unsafe_allow_html=True,
            )
        elif len(dbs_queried) > 1:
            st.markdown(
                f'<span class="sup-badge">🌐 Multi-DB: {" + ".join(dbs_queried)} ({merge_strat})</span>',
                unsafe_allow_html=True,
            )
    with badge_row[2]:
        if len(dbs_queried) == 1:
            st.markdown(
                f'<span class="pat-badge">🗄 {dbs_queried[0]}</span>',
                unsafe_allow_html=True,
            )

    # ── Metrics ────────────────────────────────────────────────────────────
    total_rows = sum(r.get("row_count", 0) for r in db_results)
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Total rows",    total_rows)
    m2.metric("DBs queried",   len(dbs_queried))
    m3.metric("Tool calls",    len(tool_calls))
    m4.metric("Iterations",    total_iters)
    m5.metric("Time (ms)",     total_ms)

    # ── Summary ────────────────────────────────────────────────────────────
    if summary:
        st.markdown(f"### 💬 {summary}")
        if partial and result.get("missing_info"):
            st.warning(f"⚠ Partial result: {result['missing_info']}")

    st.divider()

    # ── Result tabs ────────────────────────────────────────────────────────
    has_multi = len(db_results) > 1
    tab_labels = ["📊 Results"]
    if has_multi:
        tab_labels.append("🌐 Multi-DB")
    tab_labels += ["🤖 Supervisor", "🔍 SQL"]
    tabs = st.tabs(tab_labels)
    tab_idx = 0

    # ── Tab: Results ───────────────────────────────────────────────────────
    with tabs[tab_idx]:
        tab_idx += 1
        if not db_results:
            st.info("No query results returned.")
        else:
            primary = db_results[0]
            cols  = primary.get("columns", [])
            rows  = primary.get("rows", [])
            if rows:
                df = pd.DataFrame(rows, columns=cols)
                st.dataframe(df, use_container_width=True, height=380)
                st.caption(f"{len(df):,} rows · {len(cols)} columns · {primary.get('db_id','')} ")
                try:
                    buf = io.BytesIO()
                    df.to_excel(buf, index=False, engine="openpyxl")
                    st.download_button(
                        "⬇ Download Excel", buf.getvalue(),
                        file_name="supervisor_results.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                except Exception:
                    pass
            else:
                st.info("Query returned no rows.")

    # ── Tab: Multi-DB results ──────────────────────────────────────────────
    if has_multi:
        with tabs[tab_idx]:
            tab_idx += 1
            st.markdown(f"#### Results from {len(db_results)} databases")
            for dbr in db_results:
                db_id   = dbr.get("db_id", "?")
                cols_db = dbr.get("columns", [])
                rows_db = dbr.get("rows", [])
                rc      = dbr.get("row_count", 0)
                xms     = dbr.get("execution_ms", 0)
                with st.expander(
                    f"🗄 {db_id} — {rc:,} rows · {xms}ms · "
                    f"tables: {', '.join(dbr.get('tables_used', [])[:3])}",
                    expanded=True,
                ):
                    if rows_db:
                        st.dataframe(
                            pd.DataFrame(rows_db, columns=cols_db),
                            use_container_width=True, height=260,
                        )
                    else:
                        st.info("No rows returned from this database.")
                    if dbr.get("sql"):
                        with st.expander("SQL used"):
                            st.code(dbr["sql"], language="sql")
                    stats = dbr.get("summary_stats", {})
                    if stats:
                        stat_items = [
                            f"**{k}**: sum={v['sum']:,.2f} avg={v['avg']:,.2f}"
                            for k, v in stats.items()
                            if k != "row_count" and "sum" in v
                        ]
                        if stat_items:
                            st.caption(" · ".join(stat_items[:3]))

    # ── Tab: Supervisor trace ──────────────────────────────────────────────
    with tabs[tab_idx]:
        tab_idx += 1
        st.markdown("#### Supervisor tool call trace")
        st.caption(
            f"Total: {len(tool_calls)} tool call(s) across {total_iters} iteration(s) "
            f"in {total_ms}ms · Merge strategy: **{merge_strat}**"
        )
        if not tool_calls:
            st.info("No tool calls recorded.")
        else:
            for tc in tool_calls:
                ok     = tc.get("result", {}).get("ok", True)
                icon   = "✅" if ok else "❌"
                elapsed= tc.get("elapsed_ms", 0)
                label  = (
                    f"{icon} [{tc.get('iteration',0)}] "
                    f"`{tc.get('tool_name','?')}` — {elapsed}ms"
                )
                with st.expander(label):
                    c1, c2 = st.columns(2)
                    with c1:
                        st.markdown("**Arguments**")
                        st.json(tc.get("args", {}))
                    with c2:
                        st.markdown("**Result**")
                        res = tc.get("result", {})
                        # Show safe preview — truncate large fields
                        safe_res = {
                            k: (v[:200] + "…" if isinstance(v, str) and len(v) > 200 else v)
                            for k, v in res.items()
                            if k not in ("rows",)
                        }
                        st.json(safe_res)

    # ── Tab: SQL ──────────────────────────────────────────────────────────
    with tabs[tab_idx]:
        for dbr in db_results:
            if dbr.get("sql"):
                st.markdown(f"**{dbr.get('db_id', '?')}**")
                st.code(dbr["sql"], language="sql")

    st.stop()


# ══════════════════════════════════════════════════════════════════════════════
# LINEAR MODE RESULTS
# ══════════════════════════════════════════════════════════════════════════════

meta = result.get("meta", {})

# ── Status badges ──────────────────────────────────────────────────────────
badge_cols = st.columns([2, 2, 6])
with badge_cols[0]:
    if result.get("matched_pattern"):
        mp = result["matched_pattern"]
        st.markdown(
            f'<span class="pat-badge">♻ Pattern reused ({mp["similarity"]:.0%} · {mp["success_count"]}×)</span>',
            unsafe_allow_html=True,
        )
with badge_cols[1]:
    if meta.get("healed"):
        st.markdown(
            '<span class="heal-badge">🔧 Auto-recovered</span>',
            unsafe_allow_html=True,
        )

# ── Metrics ────────────────────────────────────────────────────────────────
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Rows",       meta.get("row_count", 0))
m2.metric("Exec time",  f"{meta.get('execution_ms', 0)} ms")
m3.metric("Tables",     len(meta.get("tables_used", [])))
m4.metric("Chart",      (meta.get("chart_type") or "none").capitalize())
m5.metric("DB",         meta.get("db_id", ""))

for warn in result.get("warnings", []):
    st.markdown(f'<span class="pii-warn">🔒 {warn}</span>', unsafe_allow_html=True)

# ── Result tabs ────────────────────────────────────────────────────────────
columns = result.get("columns", [])
rows    = result.get("rows",    [])

tab_table, tab_chart, tab_summary, tab_sql, tab_cypher, tab_agent = st.tabs(
    ["📊 Table", "📈 Chart", "💬 Summary", "🔍 SQL", "🔗 Cypher", "🛠 Agent"]
)

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
            st.download_button("⬇ Download Excel", buf.getvalue(),
                               file_name="query_results.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        except Exception:
            pass

with tab_chart:
    chart_type = result.get("chart_type", "none")
    if not rows:
        st.info("No data to chart.")
    elif chart_type == "none":
        st.info("No chart detected. Try: *'Show grouped by month'*")
    else:
        df  = pd.DataFrame(rows, columns=columns)
        fig = build_chart(df, chart_type)
        if fig:
            st.plotly_chart(fig, use_container_width=True)
        override = st.selectbox("Override chart type",
                                ["auto","bar","line","scatter","histogram"],
                                key="chart_override")
        if override != "auto":
            fig2 = build_chart(df, override)
            if fig2:
                st.plotly_chart(fig2, use_container_width=True)

with tab_summary:
    summary = result.get("summary", "")
    if summary:
        st.markdown(f"### 💬 {summary}")
        tables_used = meta.get("tables_used", [])
        if tables_used:
            st.caption("Tables: `" + "` · `".join(tables_used) + "`")
    else:
        st.info("No summary available.")

with tab_sql:
    sql = result.get("sql", "")
    if sql:
        st.code(sql, language="sql")
        st.caption("⚠️ Auto-generated — review before using in critical reports.")
        mp = result.get("matched_pattern")
        if mp and mp.get("sql"):
            with st.expander("📚 Matched pattern SQL"):
                st.caption(f"*{mp['nl_question']}*  |  Sim: {mp['similarity']:.0%}  |  Used: {mp['success_count']}×")
                st.code(mp["sql"], language="sql")
    else:
        st.info("No SQL to display.")

with tab_cypher:
    st.markdown("#### Schema discovery Cypher")
    schema_cypher = result.get("schema_cypher", "")
    if schema_cypher:
        st.code(schema_cypher, language="cypher")
    else:
        st.info("No Cypher recorded.")
    mp = result.get("matched_pattern")
    if mp and mp.get("schema_cypher"):
        with st.expander("📚 Stored Cypher from matched pattern"):
            st.code(mp["schema_cypher"], language="cypher")

with tab_agent:
    st.markdown("#### Agent decisions")
    trace = result.get("agent_trace") or {}
    val   = trace.get("validation") or {}
    if val:
        st.markdown("**ValidationAgent**")
        vc = st.columns(3)
        vc[0].metric("Valid",        "✅ Yes" if val.get("valid") else "❌ No")
        vc[1].metric("Cost estimate", str(val.get("cost_estimate") or "N/A"))
        vc[2].metric("Cost blocked",  "Yes" if val.get("cost_blocked") else "No")
        for issue in val.get("issues", []):
            icon = "🔴" if issue.get("severity") == "error" else "🟡"
            st.markdown(f"{icon} **{issue.get('code','')}** — {issue.get('message','')}")
        for w in val.get("warnings", []):
            st.warning(w)
    st.divider()
    heal = trace.get("healing_attempts", [])
    if heal:
        st.markdown(f"**SelfHealingAgent** — {len(heal)} attempt(s)")
        for a in heal:
            outcome = a.get("outcome", "")
            icon    = "✅" if outcome == "success" else "❌"
            with st.expander(f"{icon} Attempt {a.get('attempt')} · {a.get('error_code')} · {outcome}"):
                if a.get("error_msg"):
                    st.error(a["error_msg"])
                if a.get("sql_tried"):
                    st.code(a["sql_tried"], language="sql")
    elif not val:
        st.info("No agent decisions recorded.")
    st.divider()
    st.caption(f"Total attempts: {trace.get('total_attempts',1)}  |  Healed: {'Yes' if trace.get('healed') else 'No'}")


# ══════════════════════════════════════════════════════════════════════════════
# FEEDBACK (linear mode)
# ══════════════════════════════════════════════════════════════════════════════

if mode == "linear" and result.get("sql") and not result.get("error"):
    st.divider()
    st.markdown("**Was this result correct?**")
    fb_cols = st.columns([1, 1, 8])
    with fb_cols[0]:
        if st.button("👍 Yes", key="thumbs_up",
                     disabled=st.session_state.feedback_given):
            call_feedback_api(st.session_state.question,
                              st.session_state.selected_db_id, 5)
            st.session_state.feedback_given = True
            st.success("Feedback recorded — pattern weight increased.")
            st.rerun()
    with fb_cols[1]:
        if st.button("👎 No", key="thumbs_down",
                     disabled=st.session_state.feedback_given):
            st.session_state.show_correction = True
            st.session_state.feedback_given  = False

    if st.session_state.get("show_correction") and not st.session_state.feedback_given:
        corrected = st.text_area("Optional: paste the correct SQL",
                                 height=100, key="corrected_sql_input")
        if st.button("Submit feedback", type="primary", key="submit_feedback"):
            call_feedback_api(st.session_state.question,
                              st.session_state.selected_db_id, 1,
                              corrected.strip() or None)
            st.session_state.feedback_given  = True
            st.session_state.show_correction = False
            st.warning("Feedback recorded — pattern de-weighted.")
            st.rerun()
    if st.session_state.feedback_given:
        st.caption("✓ Feedback submitted")
