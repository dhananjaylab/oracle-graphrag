"""backend/models.py  (v2 + Phase 3A agents)"""

from pydantic import BaseModel
from typing import Any, Optional


# ── Conversation ───────────────────────────────────────────────────────────────

class ConversationTurn(BaseModel):
    role:    str    # "user" | "model"
    content: str


# ── Agent trace models ─────────────────────────────────────────────────────────

class ValidationIssue(BaseModel):
    severity: str          # "error" | "warning"
    code:     str          # "ORA-00942", "sqlglot_syntax", "cartesian_join", …
    message:  str
    line:     Optional[int] = None


class ValidationResult(BaseModel):
    valid:          bool
    sql:            str
    issues:         list[ValidationIssue] = []
    warnings:       list[str]             = []
    cost_estimate:  Optional[int]         = None
    cost_blocked:   bool                  = False


class HealingAttemptModel(BaseModel):
    attempt:    int
    error_code: str
    sql_tried:  str
    outcome:    str    # "validation_failed" | "execution_failed" | "success"
    error_msg:  str    = ""


class AgentTrace(BaseModel):
    validation:      Optional[ValidationResult]      = None
    healing_attempts: list[HealingAttemptModel]      = []
    healed:          bool                            = False
    total_attempts:  int                             = 0


# ── Request / Response ─────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question:             str
    db_id:                str                    = ""
    execute:              bool                   = True
    max_rows:             int                    = 1000
    conversation_history: list[ConversationTurn] = []
    skip_explain_plan:    bool                   = False  # skip EXPLAIN PLAN (faster in dev)


class MatchedPattern(BaseModel):
    nl_question:  str
    sql:          str
    schema_cypher: str
    similarity:   float
    success_count: int


class QueryMeta(BaseModel):
    db_id:           str
    db_name:         str
    tables_used:     list[str]
    row_count:       int
    execution_ms:    int
    chart_type:      str
    pattern_matched: bool = False
    healed:          bool = False   # True if SelfHealingAgent recovered the query


class QueryResponse(BaseModel):
    question:       str
    db_id:          str
    sql:            str
    columns:        list[str]         = []
    rows:           list[list[Any]]   = []
    summary:        str               = ""
    chart_type:     str               = "none"
    warnings:       list[str]         = []
    matched_pattern: Optional[MatchedPattern]  = None
    schema_cypher:  str               = ""
    agent_trace:    Optional[AgentTrace]       = None   # full agent decision log
    meta:           QueryMeta
    error:          Optional[str]     = None


# ── Schema / DB summary ────────────────────────────────────────────────────────

class DomainSummary(BaseModel):
    name: str
    hint: str


class TableSummary(BaseModel):
    name:            str
    description:     str
    column_count:    int
    is_view:         bool = False
    row_count_approx: int = 0
    domain:          str  = ""


class DatabaseSummary(BaseModel):
    id:          str
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
    rating:        int            # 1 = thumbs down, 5 = thumbs up
    corrected_sql: Optional[str] = None   # user-provided correct SQL (optional)
