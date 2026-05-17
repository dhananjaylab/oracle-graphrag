from pydantic import BaseModel
from typing import Any, Optional


class ConversationTurn(BaseModel):
    role: str
    content: str


class QueryRequest(BaseModel):
    question: str
    db_id: str = ""
    execute: bool = True
    max_rows: int = 1000
    conversation_history: list[ConversationTurn] = []


class MatchedPattern(BaseModel):
    nl_question: str
    sql: str
    schema_cypher: str    # preserved Cypher used for schema discovery
    similarity: float
    success_count: int


class QueryMeta(BaseModel):
    db_id: str
    db_name: str
    tables_used: list[str]
    row_count: int
    execution_ms: int
    chart_type: str
    pattern_matched: bool = False


class QueryResponse(BaseModel):
    question: str
    db_id: str
    sql: str
    columns: list[str] = []
    rows: list[list[Any]] = []
    summary: str = ""
    chart_type: str = "none"
    warnings: list[str] = []
    matched_pattern: Optional[MatchedPattern] = None
    schema_cypher: str = ""   # Cypher used for schema discovery — always returned
    meta: QueryMeta
    error: Optional[str] = None


class DomainSummary(BaseModel):
    name: str
    hint: str


class TableSummary(BaseModel):
    name: str
    description: str
    column_count: int
    is_view: bool = False
    row_count_approx: int = 0
    domain: str = ""


class DatabaseSummary(BaseModel):
    id: str
    name: str
    description: str
    table_count: int
    domains: list[DomainSummary]
    tables: list[TableSummary]


class SchemaResponse(BaseModel):
    databases: list[DatabaseSummary]
    total_databases: int
