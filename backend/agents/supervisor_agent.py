"""
backend/agents/supervisor_agent.py  (Phase 3C)

SupervisorAgent — orchestrates multi-DB analytics queries using
Gemini function calling. Each tool call is dispatched to the appropriate
MCP server (Oracle or Neo4j) via tool_executor.

Design decisions encoded:
  • Dynamic finish: loop continues until supervisor calls finish(), no hard cap
  • Hybrid routing: semantic search + cross-DB hints (encoded in system prompt)
  • Supervisor decides merge: sequential vs parallel — supervisor chooses per query
  • Partial results: supervisor calls finish(partial=True) rather than failing
  • Summarised context: prior turns compressed, passed in initial message
  • SSE streaming: yields SupervisorEvent objects for the route to stream

The agent is a pure async generator — it never writes to HTTP directly.
The route layer (supervisor.py) serialises events as SSE frames.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncGenerator, Any

import google.generativeai as genai
import google.ai.generativelanguage as glm

from backend.config import settings
from backend.prompts.supervisor_prompt import (
    SUPERVISOR_SYSTEM_PROMPT,
    build_conversation_context,
    build_supervisor_user_message,
)
from backend.tools.function_definitions import SUPERVISOR_TOOLS
from backend.tools.tool_executor import execute_tool
from backend.services.output_service import compute_summary_stats, build_dataframe

logger = logging.getLogger(__name__)

genai.configure(api_key=settings.gemini_api_key)

# Safety: absolute maximum iterations to prevent infinite loops
_HARD_CAP = 20


# ── Event types streamed to the UI ─────────────────────────────────────────────

@dataclass
class SupervisorEvent:
    """Single SSE event yielded by the supervisor run loop."""
    event_type: str    # "thinking" | "tool_call" | "tool_result" | "sql" | "finish" | "error"
    data:       dict


@dataclass
class ToolCallRecord:
    """Full record of one tool call for the supervisor trace tab."""
    tool_name:  str
    args:       dict
    result:     dict
    elapsed_ms: int
    iteration:  int


@dataclass
class DBResult:
    """Result from one database in a multi-DB query."""
    db_id:         str
    sql:           str
    columns:       list[str]       = field(default_factory=list)
    rows:          list[list[Any]] = field(default_factory=list)
    row_count:     int             = 0
    summary_stats: dict            = field(default_factory=dict)
    tables_used:   list[str]       = field(default_factory=list)
    execution_ms:  int             = 0


@dataclass
class SupervisorResult:
    """Final result returned by the supervisor after finish() is called."""
    summary:        str
    db_results:     list[DBResult]        = field(default_factory=list)
    dbs_queried:    list[str]             = field(default_factory=list)
    tables_used:    list[str]             = field(default_factory=list)
    tool_calls:     list[ToolCallRecord]  = field(default_factory=list)
    partial:        bool                  = False
    missing_info:   str                   = ""
    merge_strategy: str                   = "single_db"
    total_iterations: int                 = 0
    total_ms:       int                   = 0
    error:          str                   = ""


# ── Agent ──────────────────────────────────────────────────────────────────────

class SupervisorAgent:
    """
    Gemini function-calling supervisor. Call run_stream() to get an
    async generator of SupervisorEvent objects.

    Usage:
        async for event in supervisor_agent.run_stream(
            question=..., query_embedding=..., databases=...,
            conversation_history=...,
        ):
            # serialise event as SSE frame
    """

    def __init__(self) -> None:
        self._model = genai.GenerativeModel(
            model_name         = "gemini-flash-latest",
            system_instruction = SUPERVISOR_SYSTEM_PROMPT,
            tools              = [SUPERVISOR_TOOLS],
            generation_config  = genai.types.GenerationConfig(
                temperature      = 0.1,
                candidate_count  = 1,
            ),
        )

    async def run_stream(
        self,
        question:             str,
        query_embedding:      list[float],
        databases:            list[dict],
        conversation_history: list[dict],
    ) -> AsyncGenerator[SupervisorEvent, None]:
        """
        Run the supervisor loop, yielding SupervisorEvents as each step completes.
        Always ends with a "finish" or "error" event.
        """
        t_start       = time.monotonic()
        tool_calls:   list[ToolCallRecord] = []
        db_results:   list[DBResult]       = []
        iteration     = 0

        # ── Initial context ────────────────────────────────────────────────
        embedding_note = (
            f"Pre-computed 3072-dim embedding available. "
            f"Pass this JSON as embedding_json to semantic_search / search_patterns:\n"
            f"{json.dumps(query_embedding)}"
        )
        context_str = build_conversation_context(conversation_history)
        user_message = build_supervisor_user_message(
            question             = question,
            databases            = databases,
            embedding_note       = embedding_note,
            conversation_context = context_str,
        )

        yield SupervisorEvent("thinking", {
            "message":    "Supervisor starting — analysing question and routing to tools",
            "databases":  [d["id"] for d in databases],
            "iteration":  0,
        })

        # ── Gemini chat session ────────────────────────────────────────────
        chat = self._model.start_chat(history=[])

        try:
            response = await chat.send_message_async(user_message)
        except Exception as exc:
            yield SupervisorEvent("error", {"message": f"Gemini initialisation failed: {exc}"})
            return

        # ── Main loop ──────────────────────────────────────────────────────
        while iteration < _HARD_CAP:
            iteration += 1

            # Extract function calls from response
            fn_calls = self._extract_function_calls(response)

            if not fn_calls:
                # Gemini returned text without a tool call — treat as finish
                text = self._extract_text(response)
                yield SupervisorEvent("finish", {
                    "summary":        text or "Analysis complete.",
                    "db_results":     [self._db_result_to_dict(r) for r in db_results],
                    "dbs_queried":    list({r.db_id for r in db_results}),
                    "tables_used":    list({t for r in db_results for t in r.tables_used}),
                    "tool_calls":     [self._tc_to_dict(tc) for tc in tool_calls],
                    "partial":        False,
                    "missing_info":   "",
                    "merge_strategy": "single_db",
                    "total_iterations": iteration,
                    "total_ms":       int((time.monotonic() - t_start) * 1000),
                })
                return

            # Process every tool call Gemini requested
            function_responses = []

            for fn in fn_calls:
                fn_name = fn.name
                fn_args = dict(fn.args) if fn.args else {}

                # ── finish() signals completion ────────────────────────────
                if fn_name == "finish":
                    result = await self._handle_finish(
                        fn_args, db_results, tool_calls, iteration,
                        int((time.monotonic() - t_start) * 1000),
                    )
                    yield SupervisorEvent("finish", result)
                    return

                # ── Stream tool_call event before executing ────────────────
                yield SupervisorEvent("tool_call", {
                    "tool_name": fn_name,
                    "args":      self._safe_args_preview(fn_name, fn_args),
                    "iteration": iteration,
                    "message":   self._tool_status_message(fn_name, fn_args),
                })

                # ── Execute the tool via MCP ───────────────────────────────
                tc_start = time.monotonic()
                result   = await execute_tool(fn_name, fn_args)
                elapsed  = int((time.monotonic() - tc_start) * 1000)

                # Record for trace tab
                tool_calls.append(ToolCallRecord(
                    tool_name  = fn_name,
                    args       = self._safe_args_preview(fn_name, fn_args),
                    result     = self._safe_result_preview(fn_name, result),
                    elapsed_ms = elapsed,
                    iteration  = iteration,
                ))

                # If this was a successful execute_query, capture DB result
                if fn_name == "execute_query" and result.get("ok") and "columns" in result:
                    db_result = self._capture_db_result(fn_args, result)
                    db_results.append(db_result)
                    yield SupervisorEvent("sql", {
                        "db_id":     fn_args.get("db_id", ""),
                        "sql":       fn_args.get("sql", ""),
                        "row_count": result.get("row_count", 0),
                        "columns":   result.get("columns", []),
                        "elapsed_ms": elapsed,
                    })

                # ── Stream tool_result event ───────────────────────────────
                yield SupervisorEvent("tool_result", {
                    "tool_name":  fn_name,
                    "ok":         result.get("ok", False),
                    "elapsed_ms": elapsed,
                    "iteration":  iteration,
                    "summary":    self._result_summary(fn_name, result),
                })

                # Build function response for Gemini
                function_responses.append(
                    glm.Part(function_response=glm.FunctionResponse(
                        name     = fn_name,
                        response = {"result": json.dumps(
                            self._safe_result_for_gemini(fn_name, result),
                            default=str,
                        )},
                    ))
                )

            # ── Feed all results back to Gemini ───────────────────────────
            if function_responses:
                try:
                    response = await chat.send_message_async(
                        glm.Content(role="function", parts=function_responses)
                    )
                except Exception as exc:
                    yield SupervisorEvent("error", {
                        "message":    f"Gemini error on iteration {iteration}: {exc}",
                        "tool_calls": [self._tc_to_dict(tc) for tc in tool_calls],
                    })
                    return

        # ── Hard cap reached ───────────────────────────────────────────────
        yield SupervisorEvent("finish", {
            "summary":        "Analysis reached the iteration limit. Returning partial results.",
            "db_results":     [self._db_result_to_dict(r) for r in db_results],
            "dbs_queried":    list({r.db_id for r in db_results}),
            "tables_used":    list({t for r in db_results for t in r.tables_used}),
            "tool_calls":     [self._tc_to_dict(tc) for tc in tool_calls],
            "partial":        True,
            "missing_info":   f"Supervisor hit the {_HARD_CAP}-iteration safety limit.",
            "merge_strategy": "unknown",
            "total_iterations": iteration,
            "total_ms":       int((time.monotonic() - t_start) * 1000),
        })

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _extract_function_calls(self, response) -> list:
        """Extract all function_call parts from a Gemini response."""
        calls = []
        try:
            for candidate in response.candidates:
                for part in candidate.content.parts:
                    if hasattr(part, "function_call") and part.function_call.name:
                        calls.append(part.function_call)
        except Exception:
            pass
        return calls

    def _extract_text(self, response) -> str:
        """Extract plain text from a Gemini response."""
        try:
            for candidate in response.candidates:
                for part in candidate.content.parts:
                    if hasattr(part, "text") and part.text:
                        return part.text.strip()
        except Exception:
            pass
        return ""

    def _capture_db_result(self, fn_args: dict, result: dict) -> DBResult:
        """Build a DBResult from a successful execute_query call."""
        columns   = result.get("columns", [])
        rows      = result.get("rows", [])
        df        = build_dataframe(columns, rows)
        stats     = compute_summary_stats(df)
        return DBResult(
            db_id         = fn_args.get("db_id", ""),
            sql           = result.get("sql_executed", fn_args.get("sql", "")),
            columns       = columns,
            rows          = rows,
            row_count     = result.get("row_count", len(rows)),
            summary_stats = stats,
            tables_used   = [],     # populated by supervisor's store_pattern args
            execution_ms  = result.get("elapsed_ms", 0),
        )

    async def _handle_finish(
        self,
        args:        dict,
        db_results:  list[DBResult],
        tool_calls:  list[ToolCallRecord],
        iteration:   int,
        total_ms:    int,
    ) -> dict:
        """Parse the finish() tool call arguments into a final event dict."""
        # Parse sql_results JSON that supervisor may have constructed
        sql_results_raw = args.get("sql_results", "[]")
        try:
            sql_results_parsed = json.loads(sql_results_raw) if isinstance(sql_results_raw, str) else sql_results_raw
        except Exception:
            sql_results_parsed = []

        # Merge supervisor-reported results with captured DBResult objects
        final_db_results = db_results if db_results else [
            {
                "db_id":         r.get("db_id", ""),
                "sql":           r.get("sql", ""),
                "columns":       r.get("columns", []),
                "row_count":     r.get("row_count", 0),
                "summary_stats": r.get("summary_stats", {}),
                "tables_used":   r.get("tables_used", []),
            }
            for r in sql_results_parsed
        ]

        dbs_queried = json.loads(args.get("dbs_queried", "[]")) if isinstance(
            args.get("dbs_queried"), str) else args.get("dbs_queried", [])
        tables_used = json.loads(args.get("tables_used", "[]")) if isinstance(
            args.get("tables_used"), str) else args.get("tables_used", [])

        return {
            "summary":          args.get("summary", "Analysis complete."),
            "db_results":       [self._db_result_to_dict(r) for r in db_results] or final_db_results,
            "dbs_queried":      dbs_queried or list({r.db_id for r in db_results}),
            "tables_used":      tables_used or list({t for r in db_results for t in r.tables_used}),
            "tool_calls":       [self._tc_to_dict(tc) for tc in tool_calls],
            "partial":          bool(args.get("partial", False)),
            "missing_info":     args.get("missing_info", ""),
            "merge_strategy":   args.get("merge_strategy", "single_db"),
            "total_iterations": iteration,
            "total_ms":         total_ms,
        }

    def _db_result_to_dict(self, r: DBResult) -> dict:
        return {
            "db_id":         r.db_id,
            "sql":           r.sql,
            "columns":       r.columns,
            "rows":          r.rows,
            "row_count":     r.row_count,
            "summary_stats": r.summary_stats,
            "tables_used":   r.tables_used,
            "execution_ms":  r.execution_ms,
        }

    def _tc_to_dict(self, tc: ToolCallRecord) -> dict:
        return {
            "tool_name":  tc.tool_name,
            "args":       tc.args,
            "result":     tc.result,
            "elapsed_ms": tc.elapsed_ms,
            "iteration":  tc.iteration,
        }

    def _safe_args_preview(self, tool_name: str, args: dict) -> dict:
        """Return args with embedding vectors truncated for display."""
        preview = {}
        for k, v in args.items():
            if "embedding" in k.lower() and isinstance(v, str):
                preview[k] = f"[3072-dim vector — truncated for display]"
            elif isinstance(v, str) and len(v) > 300:
                preview[k] = v[:300] + "…"
            else:
                preview[k] = v
        return preview

    def _safe_result_preview(self, tool_name: str, result: dict) -> dict:
        """Trim large result payloads for the trace tab."""
        preview = {}
        for k, v in result.items():
            if k == "rows" and isinstance(v, list) and len(v) > 5:
                preview[k] = v[:5] + [f"… {len(v)-5} more rows"]
            elif isinstance(v, str) and len(v) > 500:
                preview[k] = v[:500] + "…"
            else:
                preview[k] = v
        return preview

    def _safe_result_for_gemini(self, tool_name: str, result: dict) -> dict:
        """
        Build the result dict sent back to Gemini as a FunctionResponse.
        For execute_query: send columns + aggregate stats ONLY (never raw rows).
        For other tools: send the full result.
        """
        if tool_name == "execute_query" and result.get("ok"):
            cols  = result.get("columns", [])
            rows  = result.get("rows", [])
            df    = build_dataframe(cols, rows)
            stats = compute_summary_stats(df)
            return {
                "ok":           True,
                "columns":      cols,
                "row_count":    result.get("row_count", 0),
                "sql_executed": result.get("sql_executed", ""),
                "summary_stats": stats,    # aggregate only — no raw rows to Gemini
                "pii_warnings": result.get("pii_warnings", []),
            }
        return result

    def _tool_status_message(self, tool_name: str, args: dict) -> str:
        messages = {
            "list_databases":    "Listing available databases…",
            "semantic_search":   f"Searching schema graph in {args.get('database_id', '?')}…",
            "get_table_details": "Loading table and column details…",
            "get_join_path":     f"Finding join path: {args.get('table1','?')} → {args.get('table2','?')}…",
            "get_cross_db_hints":"Checking cross-database links…",
            "search_patterns":   "Looking up past similar queries…",
            "check_read_only":   "Validating SQL safety…",
            "explain_plan":      f"Estimating query cost on {args.get('db_id','?')}…",
            "execute_query":     f"Executing SQL on {args.get('db_id','?')}…",
            "store_pattern":     "Storing successful query for future reuse…",
            "get_schema":        f"Fetching schema from {args.get('db_id','?')}…",
            "get_schema_summary":"Loading full schema overview…",
        }
        return messages.get(tool_name, f"Calling {tool_name}…")

    def _result_summary(self, tool_name: str, result: dict) -> str:
        if not result.get("ok", True):
            return f"Failed: {result.get('error', 'unknown error')}"
        if tool_name == "execute_query":
            return f"{result.get('row_count', 0)} rows returned in {result.get('elapsed_ms', 0)}ms"
        if tool_name == "semantic_search":
            t = len(result.get("tables", []))
            c = len(result.get("columns", []))
            return f"{t} tables · {c} columns matched"
        if tool_name == "search_patterns":
            p = len(result.get("patterns", []))
            return f"{p} past pattern(s) found"
        if tool_name == "list_databases":
            n = len(result.get("databases", []))
            return f"{n} database(s) registered"
        if tool_name == "check_read_only":
            valid = result.get("valid", False)
            return "SQL is safe" if valid else f"BLOCKED: {result.get('forbidden_keywords', [])}"
        if tool_name == "explain_plan":
            cost = result.get("cost")
            return f"Cost={cost}" if cost else "Cost estimate unavailable"
        return "OK"


# ── Module-level singleton ─────────────────────────────────────────────────────
supervisor_agent = SupervisorAgent()
