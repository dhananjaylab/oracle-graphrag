from pydantic import BaseModel
from typing import Any, Optional


class ConversationTurn(BaseModel):
    role: str        # "user" | "model"
    content: str


class QueryRequest(BaseModel):
    question: str
    execute: bool = True
    max_rows: int = 1000
    conversation_history: list[ConversationTurn] = []


class QueryMeta(BaseModel):
    tables_used: list[str]
    row_count: int
    execution_ms: int
    chart_type: str


class QueryResponse(BaseModel):
    question: str
    sql: str
    columns: list[str] = []
    rows: list[list[Any]] = []
    summary: str = ""
    chart_type: str = "none"
    warnings: list[str] = []
    meta: QueryMeta
    error: Optional[str] = None


class TableSummary(BaseModel):
    name: str
    description: str
    column_count: int


class SchemaResponse(BaseModel):
    tables: list[TableSummary]
    total_tables: int
