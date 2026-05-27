"""
backend/tools/function_definitions.py  (Phase 3C)

All 13 MCP tools + the supervisor finish() tool defined as
Gemini FunctionDeclarations for use with Gemini function calling.

Tool categories:
  ORACLE  — execute_query, explain_plan, get_schema, list_databases, check_read_only
  NEO4J   — semantic_search, get_table_details, get_join_path, get_cross_db_hints,
             search_patterns, store_pattern, get_schema_summary
  CONTROL — finish (signals supervisor completion)

Each declaration includes a rich description so Gemini understands
when and how to invoke each tool.
"""

import google.generativeai as genai
from google.generativeai.types import FunctionDeclaration, Tool

# ── Helpers ────────────────────────────────────────────────────────────────────

def _str(description: str) -> dict:
    return {"type": "string", "description": description}

def _int(description: str) -> dict:
    return {"type": "integer", "description": description}

def _float(description: str) -> dict:
    return {"type": "number", "description": description}

def _bool(description: str) -> dict:
    return {"type": "boolean", "description": description}

def _fn(name: str, description: str, properties: dict, required: list) -> FunctionDeclaration:
    return FunctionDeclaration(
        name        = name,
        description = description,
        parameters  = {
            "type":       "object",
            "properties": properties,
            "required":   required,
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# ORACLE MCP TOOLS
# ══════════════════════════════════════════════════════════════════════════════

EXECUTE_QUERY = _fn(
    name = "execute_query",
    description = (
        "Execute a validated read-only Oracle SQL query against a specified database. "
        "Safety layers are applied automatically: forbidden-keyword guard, PII masking, "
        "and row-limit injection. Returns columns, rows, row_count, and pii_warnings. "
        "Always call check_read_only() first if you wrote the SQL yourself."
    ),
    properties = {
        "db_id":    _str("Database identifier from databases.yaml (e.g. 'fincore', 'riskdb')"),
        "sql":      _str("Oracle SELECT statement. Use SCHEMA.TABLE_NAME format, qualify all columns with aliases, use FETCH FIRST N ROWS ONLY."),
        "max_rows": _int("Maximum rows to return (default 1000). Use smaller values for exploratory queries."),
    },
    required = ["db_id", "sql"],
)

EXPLAIN_PLAN = _fn(
    name = "explain_plan",
    description = (
        "Run Oracle EXPLAIN PLAN FOR <sql> without executing the query. "
        "Returns estimated cost, full-table-scan flag, and Cartesian-join flag. "
        "Call this before execute_query() when querying large tables (row_count_approx > 1M) "
        "or when the SQL involves multiple JOIN conditions."
    ),
    properties = {
        "db_id": _str("Database identifier"),
        "sql":   _str("Oracle SQL to estimate cost for"),
    },
    required = ["db_id", "sql"],
)

GET_SCHEMA = _fn(
    name = "get_schema",
    description = (
        "Pull enriched data-dictionary metadata from Oracle ALL_* views. "
        "Returns table names, column names, data types, PKs, FKs, indexes, and row counts. "
        "Never returns actual business data — only structural metadata. "
        "Use when you need to explore an unfamiliar database before generating SQL."
    ),
    properties = {
        "db_id":       _str("Database identifier"),
        "schema_name": _str("Oracle schema name override (e.g. 'FINCORE'). Leave blank to use databases.yaml default."),
    },
    required = ["db_id"],
)

LIST_DATABASES = _fn(
    name = "list_databases",
    description = (
        "List all registered Oracle databases with their IDs, names, schema names, "
        "and configuration status. Call this when the question does not clearly indicate "
        "which database to use, or when a cross-DB question requires knowing all options."
    ),
    properties = {},
    required   = [],
)

CHECK_READ_ONLY = _fn(
    name = "check_read_only",
    description = (
        "Fast pre-flight check that SQL contains no DML or DDL keywords. "
        "No database connection required — runs locally. "
        "Always call this before execute_query() for SQL you generated yourself."
    ),
    properties = {
        "sql": _str("Oracle SQL string to validate"),
    },
    required = ["sql"],
)


# ══════════════════════════════════════════════════════════════════════════════
# NEO4J MCP TOOLS
# ══════════════════════════════════════════════════════════════════════════════

SEMANTIC_SEARCH = _fn(
    name = "semantic_search",
    description = (
        "Vector cosine-similarity search on (:Table) and (:Column) nodes in the "
        "Neo4j schema graph, scoped to one database. "
        "Use the pre-computed question embedding provided in the session context. "
        "Returns ranked tables and columns with similarity scores. "
        "Always call this first on the most likely database before building SQL."
    ),
    properties = {
        "embedding_json":  _str("JSON-serialized list[float] — the pre-computed 3072-dim question embedding provided in context."),
        "database_id":     _str("Database identifier to search within (e.g. 'fincore')"),
        "top_k":           _int("Number of nearest neighbours to return (default 12)"),
    },
    required = ["embedding_json", "database_id"],
)

GET_TABLE_DETAILS = _fn(
    name = "get_table_details",
    description = (
        "Retrieve full column metadata for a list of tables from the Neo4j graph. "
        "Returns column names, data types, enriched business labels, PK/FK/index flags, "
        "PII flags, and cardinality hints. "
        "Call this after semantic_search to build the schema context for SQL generation."
    ),
    properties = {
        "table_names_json": _str("JSON-serialized list[str] of table names to retrieve details for"),
        "database_id":      _str("Database identifier"),
    },
    required = ["table_names_json", "database_id"],
)

GET_JOIN_PATH = _fn(
    name = "get_join_path",
    description = (
        "Find the shortest FK-based join path between two tables in the Neo4j graph. "
        "Uses shortestPath over [:FK_TO*1..5] relationships. "
        "Returns the table sequence and JOIN ON conditions. "
        "Call when you need to join two tables that may not be directly connected."
    ),
    properties = {
        "table1":      _str("Source table name"),
        "table2":      _str("Target table name"),
        "database_id": _str("Database identifier"),
    },
    required = ["table1", "table2", "database_id"],
)

GET_CROSS_DB_HINTS = _fn(
    name = "get_cross_db_hints",
    description = (
        "Return cross-database CROSS_DB_JOIN edges for candidate tables. "
        "These are known logical relationships between tables in DIFFERENT Oracle databases "
        "(e.g. LOAN_MASTER in fincore is linked to NPA_MASTER in riskdb via LOAN_ACCT_NO). "
        "Call this after semantic_search to discover if the question requires multi-DB queries."
    ),
    properties = {
        "table_names_json": _str("JSON-serialized list[str] of candidate table names from the primary database"),
        "database_id":      _str("Source database identifier"),
    },
    required = ["table_names_json", "database_id"],
)

SEARCH_PATTERNS = _fn(
    name = "search_patterns",
    description = (
        "Find past successful QueryPattern nodes in Neo4j that are semantically similar "
        "to the current question. Returns stored NL questions, SQL, schema Cypher, "
        "and success counts. "
        "If a pattern with score ≥ 0.85 matches, prefer its SQL as a starting point — "
        "it has already been validated and executed successfully."
    ),
    properties = {
        "embedding_json":  _str("JSON-serialized list[float] — the pre-computed question embedding"),
        "database_id":     _str("Database identifier"),
        "top_k":           _int("Max patterns to return (default 3)"),
        "min_similarity":  _float("Minimum cosine similarity threshold 0.0–1.0 (default 0.85)"),
    },
    required = ["embedding_json", "database_id"],
)

STORE_PATTERN = _fn(
    name = "store_pattern",
    description = (
        "Persist a successful NL→SQL exchange as a QueryPattern node in Neo4j. "
        "Call this after every successful execute_query() before calling finish(). "
        "Stores the NL question, SQL, schema discovery Cypher, tables used, and execution time. "
        "Patterns are reused as few-shot examples in future similar queries."
    ),
    properties = {
        "database_id":      _str("Database identifier"),
        "nl_question":      _str("Original natural language question"),
        "sql":              _str("Executed Oracle SQL"),
        "schema_cypher":    _str("Cypher queries used for schema discovery"),
        "tables_used_json": _str("JSON-serialized list[str] of table names used"),
        "execution_ms":     _int("Execution time in milliseconds"),
        "embedding_json":   _str("JSON-serialized list[float] — question embedding"),
    },
    required = ["database_id", "nl_question", "sql",
                "schema_cypher", "tables_used_json", "execution_ms", "embedding_json"],
)

GET_SCHEMA_SUMMARY = _fn(
    name = "get_schema_summary",
    description = (
        "Return all databases with their enriched tables and business domains from Neo4j. "
        "Useful for getting a high-level overview of available data before routing. "
        "Call when the user asks a broad question about available data or database structure."
    ),
    properties = {},
    required   = [],
)


# ══════════════════════════════════════════════════════════════════════════════
# CONTROL TOOL — finish signals supervisor loop completion
# ══════════════════════════════════════════════════════════════════════════════

FINISH = _fn(
    name = "finish",
    description = (
        "Signal that you have completed the analysis and have a final answer. "
        "ALWAYS call this tool as the last action — never stop without calling finish(). "
        "Populate sql_results with each database query result (columns + aggregate stats only, no raw rows). "
        "If the answer is partial (some data unavailable), set partial=true and explain in missing_info."
    ),
    properties = {
        "summary": _str(
            "Concise business-friendly answer in 2-3 sentences. "
            "Use ₹ for monetary amounts, crore/lakh denomination. "
            "Never mention SQL, database, or technical terms."
        ),
        "sql_results": _str(
            "JSON-serialized list of per-database results: "
            "[{db_id, sql, columns, row_count, summary_stats, tables_used}, ...]. "
            "Include aggregate stats (sum/avg/min/max) only — never include raw row data."
        ),
        "dbs_queried": _str("JSON-serialized list[str] of database IDs that were successfully queried"),
        "tables_used": _str("JSON-serialized list[str] of all table names used across all databases"),
        "partial":     _bool("True if the answer is incomplete because some data was unavailable"),
        "missing_info": _str("Description of what data was missing or could not be retrieved (empty string if not partial)"),
        "merge_strategy": _str("The merge strategy used: 'sequential', 'parallel', or 'single_db'"),
    },
    required = ["summary", "sql_results", "dbs_queried", "tables_used", "partial"],
)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL REGISTRY — single Tool object for the Gemini model
# ══════════════════════════════════════════════════════════════════════════════

SUPERVISOR_TOOLS = Tool(function_declarations=[
    # Oracle MCP
    EXECUTE_QUERY,
    EXPLAIN_PLAN,
    GET_SCHEMA,
    LIST_DATABASES,
    CHECK_READ_ONLY,
    # Neo4j MCP
    SEMANTIC_SEARCH,
    GET_TABLE_DETAILS,
    GET_JOIN_PATH,
    GET_CROSS_DB_HINTS,
    SEARCH_PATTERNS,
    STORE_PATTERN,
    GET_SCHEMA_SUMMARY,
    # Control
    FINISH,
])

# Set of tool names the supervisor can call
ORACLE_TOOL_NAMES  = {"execute_query", "explain_plan", "get_schema", "list_databases", "check_read_only"}
NEO4J_TOOL_NAMES   = {"semantic_search", "get_table_details", "get_join_path", "get_cross_db_hints",
                       "search_patterns", "store_pattern", "get_schema_summary"}
CONTROL_TOOL_NAMES = {"finish"}
ALL_TOOL_NAMES     = ORACLE_TOOL_NAMES | NEO4J_TOOL_NAMES | CONTROL_TOOL_NAMES
