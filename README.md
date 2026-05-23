# 🏦 NL-SQL v3 — Banking Natural Language Analytics

> Ask questions in plain English across multiple Oracle databases. Powered by GraphRAG, Gemini, self-healing agents, and a full MCP server layer.

---

## 🧠 Architecture

```
User question
     │
     ▼
Embed (gemini-embedding-001)
     │
     ├──► Neo4j MCP Server ──► search_patterns()      ← reuse past SQL
     │
     ├──► Neo4j MCP Server ──► semantic_search()      ← GraphRAG schema
     │                         get_table_details()
     │                         get_join_path()
     │                         get_cross_db_hints()
     ▼
Build schema context (metadata only — no raw data)
     │
     ▼
Gemini SQL generation  ← schema context + matched patterns as few-shots
     │
     ▼
[Phase 3A] ValidationAgent
     │  sqlglot parse · EXPLAIN PLAN cost · read-only guard
     │
     ├── valid ──► Oracle MCP Server ──► execute_query()
     │                    │
     │              success ─────────────────────────────► Output
     │              ORA-* error                              │
     │                    │                                  │
     └── invalid          ▼                                  │
               [Phase 3A] SelfHealingAgent                   │
                    classify → re-prompt Gemini → retry      │
                    max 3 attempts                            │
                         │                                    │
                    healed ──► Oracle MCP ──► execute() ─────┘
                    failed ──► Error response
     │
     ▼
[Background] Neo4j MCP Server ──► store_pattern()
                                   (NL + SQL + schema Cypher)
     │
     ▼
User feedback: POST /api/feedback
Neo4j MCP Server ──► record_feedback()
                      (increment / decrement / correct)
```

**Privacy guarantee:** Only schema metadata (table/column names + enriched descriptions) reaches Gemini. Raw Oracle row data never leaves on-prem.

---

## 🛠 Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Streamlit 1.35 (port 8501) |
| Backend | FastAPI + Python 3.11+ (port 8000) |
| Oracle MCP Server | FastMCP + SSE (port 8001) |
| Neo4j MCP Server | FastMCP + SSE (port 8002) |
| LLM | `gemini-flash-latest` (Google AI Studio) |
| Embeddings | `gemini-embedding-001` · 3072 dims |
| Graph DB | Neo4j 5.11+ (on-prem) |
| Data DB | Oracle DB — multi-instance via databases.yaml |
| SQL Validation | sqlglot 25.x (Oracle dialect) |
| MCP SDK | `mcp[cli]>=1.0.0,<2.0.0` |
| Charts | Plotly |
| Export | pandas + openpyxl |

---

## 🗂 Project Structure

```
nlsql/
│
├── databases.yaml              # DB list, domains, cross-DB links (non-sensitive)
├── .env                        # Credentials: API keys, DB passwords (gitignored)
├── .env.example
├── requirements.txt
├── start_servers.sh            # Phase 3B: start all servers (Unix/Mac)
├── start_servers.bat           # Phase 3B: start all servers (Windows)
│
├── mcp_servers/                ← Phase 3B: MCP server layer
│   ├── oracle_mcp/
│   │   └── server.py           # Oracle MCP server (port 8001)
│   │                           # Tools: execute_query, explain_plan,
│   │                           #        get_schema, list_databases,
│   │                           #        check_read_only
│   └── neo4j_mcp/
│       └── server.py           # Neo4j MCP server (port 8002)
│                               # Tools: semantic_search, get_table_details,
│                               #        get_join_path, get_cross_db_hints,
│                               #        search_patterns, store_pattern,
│                               #        get_schema_summary, record_feedback
│
├── backend/
│   ├── main.py                 # FastAPI app (lifespan connects MCP clients)
│   ├── config.py               # Settings: Gemini, Neo4j, MCP URLs
│   ├── db_manager.py           # Multi-DB Oracle pool manager
│   ├── models.py               # Pydantic models incl. AgentTrace, FeedbackRequest
│   │
│   ├── mcp_client/             ← Phase 3B: typed MCP client wrappers
│   │   ├── base.py             # MCPClientSession (persistent SSE + auto-reconnect)
│   │   ├── oracle_client.py    # OracleMCPClient (fallback to direct service)
│   │   └── neo4j_client.py     # Neo4jMCPClient  (fallback to direct service)
│   │
│   ├── agents/                 ← Phase 3A: validation + healing
│   │   ├── validation_agent.py # sqlglot · EXPLAIN PLAN · read-only guard
│   │   └── self_healing_agent.py # error-aware retry loop (max 3)
│   │
│   ├── prompts/
│   │   ├── sql_prompt.py       # Oracle SQL system prompt + static few-shots
│   │   ├── enrichment_prompt.py # Domain-aware column enrichment
│   │   └── healing_prompt.py   # 14 error-code-specific healing strategies
│   │
│   ├── routes/
│   │   ├── query.py            # POST /api/query — full 14-step MCP pipeline
│   │   └── schema.py           # GET /api/schema, /health · POST /api/feedback
│   │
│   └── services/               # Direct service implementations (MCP fallback)
│       ├── oracle_service.py
│       ├── neo4j_service.py
│       ├── gemini_service.py
│       └── output_service.py
│
├── ingestion/
│   └── ingest_schema.py        # Multi-DB: Oracle → Gemini → Neo4j
│
├── frontend/
│   └── app.py                  # Streamlit UI: DB selector, 6 tabs, feedback
│
└── scratch/
    ├── test_agents.py          # Phase 3A: standalone agent unit tests
    ├── test_mcp_servers.py     # Phase 3B: MCP server integration tests
    ├── test_end_to_end.py      # Full pipeline smoke test
    ├── test_oracle.py          # Oracle connectivity check
    └── test_neo4j.py           # Neo4j connectivity + vector index check
```

---

## 📋 Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | `python3 --version` |
| Oracle DB | User needs `SELECT` on `ALL_*` data dictionary views |
| Neo4j 5.11+ | Community edition — [neo4j.com/deployment-center](https://neo4j.com/deployment-center/) |
| Gemini API key | Free at [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) |

---

## 🚀 Getting Started

### Step 1 — Install

```bash
git clone <your-repo> && cd nlsql
pip install -r requirements.txt
```

### Step 2 — Configure

```bash
cp .env.example .env
```

Fill `.env`:
```env
GEMINI_API_KEY=AIza...
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=your-password

# Optional: override MCP server URLs (default localhost)
ORACLE_MCP_URL=http://localhost:8001
NEO4J_MCP_URL=http://localhost:8002

# Oracle DB credentials (one block per databases.yaml entry)
FINCORE_USER=fincore_user
FINCORE_PASSWORD=secret
FINCORE_DSN=host1:1521/FINCORE
```

Edit `databases.yaml` to register your Oracle databases (see template).

### Step 3 — Ingest schema

```bash
python -m ingestion.ingest_schema          # all databases
python -m ingestion.ingest_schema --db fincore   # one database
```

### Step 4 — Start all servers

```bash
# Unix / Mac
chmod +x start_servers.sh && ./start_servers.sh

# Windows
start_servers.bat

# Or manually (four terminals)
python -m mcp_servers.oracle_mcp.server   # :8001
python -m mcp_servers.neo4j_mcp.server    # :8002
uvicorn backend.main:app --reload         # :8000
streamlit run frontend/app.py             # :8501
```

Open **http://localhost:8501** 🎉

---

## 📡 API Reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Liveness + DB config + MCP server status |
| `GET` | `/api/databases` | Registered databases |
| `GET` | `/api/schema` | Full enriched schema (via Neo4j MCP) |
| `GET` | `/api/examples` | Example banking questions |
| `POST` | `/api/query` | Full NL → SQL → execute → results |
| `POST` | `/api/feedback` | Thumbs up/down + optional corrected SQL |

### POST /api/query

```json
{
  "question":          "Show NPA ratio by product segment this month",
  "db_id":             "riskdb",
  "execute":           true,
  "max_rows":          1000,
  "skip_explain_plan": false,
  "conversation_history": []
}
```

Key response fields:

| Field | Description |
|---|---|
| `sql` | Final executed Oracle SQL |
| `schema_cypher` | Neo4j Cypher used for schema discovery |
| `matched_pattern` | Reused QueryPattern (similarity ≥ 0.85) + its stored Cypher |
| `agent_trace.validation` | ValidationAgent result: valid, cost, issues |
| `agent_trace.healing_attempts` | SelfHealingAgent retry log |
| `meta.healed` | `true` if SelfHealingAgent recovered the query |
| `meta.pattern_matched` | `true` if a stored pattern drove SQL generation |

### POST /api/feedback

```json
{
  "nl_question":  "Show NPA ratio by product segment this month",
  "db_id":        "riskdb",
  "rating":       5,
  "corrected_sql": null
}
```

`rating ≥ 4` → thumbs up → `success_count + 1`
`rating < 4` → thumbs down → `success_count - 1`
`corrected_sql` → replace stored SQL + `success_count + 2`

---

## 🔌 MCP Server Reference

### Oracle MCP — port 8001

| Tool | Arguments | Returns |
|---|---|---|
| `execute_query` | `db_id, sql, max_rows` | `{columns, rows, row_count, sql_executed, pii_warnings}` |
| `explain_plan` | `db_id, sql` | `{cost, has_full_scan, has_cartesian, plan_text}` |
| `get_schema` | `db_id, schema_name` | Full data dictionary JSON |
| `list_databases` | — | `[{id, name, schema, configured}]` |
| `check_read_only` | `sql` | `{valid, forbidden_keywords}` |

### Neo4j MCP — port 8002

| Tool | Arguments | Returns |
|---|---|---|
| `semantic_search` | `embedding_json, database_id, top_k` | `{tables, columns, cypher_used}` |
| `get_table_details` | `table_names_json, database_id` | List of table objects with columns |
| `get_join_path` | `table1, table2, database_id` | FK join path |
| `get_cross_db_hints` | `table_names_json, database_id` | Cross-DB link objects |
| `search_patterns` | `embedding_json, database_id, top_k, min_similarity` | Matched QueryPatterns |
| `store_pattern` | `database_id, nl_question, sql, schema_cypher, tables_used_json, execution_ms, embedding_json` | `{stored: bool}` |
| `get_schema_summary` | — | All databases + tables + domains |
| `record_feedback` | `nl_question, database_id, action, corrected_sql` | `{updated: bool}` |

---

## 🔒 Security

- Oracle user: `SELECT` privileges only — no DML ever granted
- Read-only enforced at application layer (ValidationAgent) AND Oracle MCP server
- PII columns auto-masked in SQL at execution time
- Gemini receives: enriched metadata only — never actual row values
- MCP servers bind to `0.0.0.0` by default — firewall ports 8001/8002 in production
- `.env` is gitignored — never commit credentials

---

## ♻ QueryPattern Learning Loop

Every successful query stores a `(:QueryPattern)` node containing NL question, SQL, and the schema discovery Cypher. Future similar questions (cosine ≥ 0.85) inject the stored SQL as a dynamic few-shot example. User feedback (👍/👎 + corrected SQL) adjusts pattern weights. The system improves automatically over time.

---

## 🗺 Roadmap

- [x] Phase 3A: ValidationAgent + SelfHealingAgent + feedback loop
- [x] Phase 3B: Oracle MCP server + Neo4j MCP server + typed client wrappers
- [ ] Phase 3C: Supervisor agent (Google ADK / Gemini function calling)
- [ ] Phase 3C: Multi-DB routing — supervisor splits cross-DB questions
- [ ] Phase 3C: Claude Desktop integration via MCP
