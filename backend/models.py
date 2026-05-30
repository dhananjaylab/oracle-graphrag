"""backend/models.py  (v2 + Phase 3A + 3B + 3C + Phase 4B)

Phase 4B additions to QueryMeta:
  cache_hit     — True if any cache (embedding / schema / result) was used
  cache_source  — which cache(s) were hit: "embedding", "schema", "result",
                  "embedding+schema", "result", etc.
  pipeline_ms   — total wall-clock time of the full pipeline in ms
"""

from pydantic import BaseModel
from typing import Any, Optional


# ── Conversation ───────────────────────────────────────────────────────────────

class ConversationTurn(BaseModel):
    role:    str
    content: str


# ── Conversation context for supervisor (Phase 3C) ────────────────────────────

class ConversationContextEntry(BaseModel):
    question:     str
    dbs_queried:  list[str]       = []
    tables_used:  list[str]       = []
    row_count:    int             = 0
    key_metrics:  dict[str, Any]  = {}
    partial:      bool            = False
    missing_info: str             = ""


# ── Agent trace (Phase 3A) ─────────────────────────────────────────────────────

class ValidationIssue(BaseModel):
    severity: str
    code:     str
    message:  str
    line:     Optional[int] = None


class ValidationResult(BaseModel):
    valid:         bool
    sql:           str
    issues:        list[ValidationIssue] = []
    warnings:      list[str]             = []
    cost_estimate: Optional[int]         = None
    cost_blocked:  bool                  = False


class HealingAttemptModel(BaseModel):
    attempt:    int
    error_code: str
    sql_tried:  str
    outcome:    str
    error_msg:  str = ""


class AgentTrace(BaseModel):
    validation:       Optional[ValidationResult]  = None
    healing_attempts: list[HealingAttemptModel]   = []
    healed:           bool                        = False
    total_attempts:   int                         = 0


# ── Linear pipeline request / response (Phase 3A/3B + Phase 4B) ───────────────

class QueryRequest(BaseModel):
    question:             str
    db_id:                str                    = ""
    execute:              bool                   = True
    max_rows:             int                    = 1000
    conversation_history: list[ConversationTurn] = []
    skip_explain_plan:    bool                   = False


class MatchedPattern(BaseModel):
    nl_question:   str
    sql:           str
    schema_cypher: str
    similarity:    float
    success_count: int


class QueryMeta(BaseModel):
    db_id:           str
    db_name:         str
    tables_used:     list[str]
    row_count:       int
    execution_ms:    int
    chart_type:      str
    pattern_matched: bool = False
    healed:          bool = False
    # Phase 4B — cache observability
    cache_hit:       bool = False
    cache_source:    str  = ""   # e.g. "embedding+schema", "result", ""
    pipeline_ms:     int  = 0    # total wall-clock time for the full pipeline


class QueryResponse(BaseModel):
    question:        str
    db_id:           str
    sql:             str
    columns:         list[str]         = []
    rows:            list[list[Any]]   = []
    summary:         str               = ""
    chart_type:      str               = "none"
    warnings:        list[str]         = []
    matched_pattern: Optional[MatchedPattern] = None
    schema_cypher:   str               = ""
    agent_trace:     Optional[AgentTrace]     = None
    meta:            QueryMeta
    error:           Optional[str]     = None


# ── Supervisor request / response (Phase 3C) ───────────────────────────────────

class SupervisorRequest(BaseModel):
    question:             str
    max_rows:             int                              = 1000
    conversation_history: list[ConversationContextEntry]  = []


class DBResult(BaseModel):
    db_id:         str
    sql:           str
    columns:       list[str]         = []
    rows:          list[list[Any]]   = []
    row_count:     int               = 0
    summary_stats: dict[str, Any]    = {}
    tables_used:   list[str]         = []
    execution_ms:  int               = 0


class ToolCallRecord(BaseModel):
    tool_name:  str
    args:       dict[str, Any]
    result:     dict[str, Any]
    elapsed_ms: int
    iteration:  int


class SupervisorResult(BaseModel):
    summary:          str
    db_results:       list[DBResult]       = []
    dbs_queried:      list[str]            = []
    tables_used:      list[str]            = []
    tool_calls:       list[ToolCallRecord] = []
    partial:          bool                 = False
    missing_info:     str                  = ""
    merge_strategy:   str                  = "single_db"
    total_iterations: int                  = 0
    total_ms:         int                  = 0
    error:            str                  = ""


# ── Schema / DB summary ────────────────────────────────────────────────────────

class DomainSummary(BaseModel):
    name: str
    hint: str


class TableSummary(BaseModel):
    name:             str
    description:      str
    column_count:     int
    is_view:          bool = False
    row_count_approx: int  = 0
    domain:           str  = ""


class DatabaseSummary(BaseModel):
    id:          Optional[str] = None
    name:        str
    description: str
    table_count: int
    domains:     list[DomainSummary]
    tables:      list[TableSummary]


class SchemaResponse(BaseModel):
    databases:       list[DatabaseSummary]
    total_databases: int


# ── Feedback ───────────────────────────────────────────────────────────────────

class FeedbackRequest(BaseModel):
    nl_question:   str
    db_id:         str
    rating:        int
    corrected_sql: Optional[str] = None
