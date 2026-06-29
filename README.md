# 🏦 NL-SQL v3 — Banking Natural Language Analytics

> Ask questions in plain English across multiple Oracle databases. Powered by GraphRAG, Gemini, self-healing agents, a full MCP server layer, and a horizontally-scalable production stack.

---

## 🧠 Architecture

```
User question
     │
     ▼
Embed (gemini-embedding-001)  ← EmbeddingCache (L1 local + L2 Redis)
     │
     ├──► Neo4j MCP Server ──► search_patterns()      ← reuse past SQL
     │
     ├──► Neo4j MCP Server ──► semantic_search()      ← GraphRAG schema
     │                         get_table_details()    ← SchemaCache
     │                         get_join_paths_batch()
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
     ├── valid ──► Oracle MCP Server ──► execute_query()   ← ResultCache
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
                      (increment / decrement / correct → invalidates caches)
```

**Privacy guarantee:** Only schema metadata (table/column names + enriched descriptions) reaches Gemini. Raw Oracle row data never leaves on-prem.

**Transport:** Both MCP servers run on **Streamable HTTP** (stateless), not the legacy SSE transport — see [Scaling to production](#-scaling-to-production) below.

---

## 🛠 Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Streamlit 1.35 (port 8501) |
| Backend | FastAPI + Python 3.11+ (port 8000) |
| Oracle MCP Server | Official `mcp` SDK · Streamable HTTP, stateless (port 8001) |
| Neo4j MCP Server | Official `mcp` SDK · Streamable HTTP, stateless (port 8002) |
| LLM | `gemini-flash-latest` (Google AI Studio) |
| Embeddings | `gemini-embedding-001` · 3072 dims |
| Graph DB | Neo4j 5.11+ (on-prem) |
| Data DB | Oracle DB — multi-instance via databases.yaml |
| SQL Validation | sqlglot 25.x (Oracle dialect) |
| MCP SDK | `mcp[cli]>=1.28.0,<2.0.0` (official SDK only — no third-party `fastmcp`) |
| Cache | EmbeddingCache / SchemaCache / ResultCache — local L1 + optional Redis L2 |
| Load balancing | HAProxy (primary) or nginx — fronts multi-replica MCP tiers |
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
├── docker-compose.yml          # Full production stack: redis + MCP replicas + haproxy + backend replicas
├── haproxy.cfg                 # Bare-metal HAProxy config
├── haproxy-compose.cfg         # Docker-Compose HAProxy config (service-name DNS)
├── nginx-mcp.conf              # nginx alternative to HAProxy
├── Dockerfile.mcp-oracle / Dockerfile.mcp-neo4j / Dockerfile.backend / Dockerfile.frontend
├── Dockerfile                  # Single-container variant (e.g. Hugging Face Spaces) — uses start.sh
├── start_servers.sh            # Single-host dev startup (Unix/Mac)
├── start_servers.bat           # Single-host dev startup (Windows)
│
├── mcp_servers/                ← MCP server layer (Streamable HTTP, stateless)
│   ├── oracle_mcp/
│   │   └── server.py           # Oracle MCP server (port 8001) + /health + /ready
│   │                           # Tools: execute_query, explain_plan,
│   │                           #        get_schema, list_databases,
│   │                           #        check_read_only
│   └── neo4j_mcp/
│       └── server.py           # Neo4j MCP server (port 8002) + /health + /ready
│                               # Tools: semantic_search, get_table_details,
│                               #        get_join_path, get_join_paths_batch,
│                               #        get_cross_db_hints, search_patterns,
│                               #        store_pattern, get_schema_summary,
│                               #        record_feedback
│
├── backend/
│   ├── main.py                 # FastAPI app (lifespan connects MCP pools)
│   ├── config.py                # Settings: Gemini, Neo4j, MCP URLs, pool/cache tuning
│   ├── db_manager.py            # Multi-DB Oracle pool manager (env + per-DB configurable sizing)
│   ├── cache.py                 # EmbeddingCache / SchemaCache / ResultCache (L1 local + L2 Redis)
│   ├── models.py                 # Pydantic models incl. AgentTrace, FeedbackRequest
│   │
│   ├── mcp_client/              ← Typed MCP client wrappers
│   │   ├── base.py              # MCPClientSession (Streamable HTTP, backoff reconnect)
│   │   ├── pool.py               # MCPConnectionPool (connection pool + circuit breaker)
│   │   ├── oracle_client.py     # OracleMCPClient (fallback to direct service)
│   │   └── neo4j_client.py      # Neo4jMCPClient  (fallback to direct service)
│   │
│   ├── agents/                 ← validation + healing + supervisor
│   │   ├── validation_agent.py  # sqlglot · EXPLAIN PLAN · read-only guard
│   │   ├── self_healing_agent.py # error-aware retry loop (max 3)
│   │   └── supervisor_agent.py   # Gemini function-calling multi-DB orchestrator
│   │
│   ├── prompts/
│   │   ├── sql_prompt.py        # Oracle SQL system prompt + static few-shots
│   │   ├── enrichment_prompt.py  # Domain-aware column enrichment
│   │   ├── healing_prompt.py     # Error-code-specific healing strategies
│   │   └── supervisor_prompt.py  # Supervisor system prompt + context builders
│   │
│   ├── routes/
│   │   ├── query.py             # POST /api/query — linear pipeline
│   │   ├── supervisor.py         # POST /api/supervisor — SSE multi-DB agent
│   │   └── schema.py             # GET /api/schema, /health · POST /api/feedback
│   │
│   ├── tools/                   # Gemini function-calling tool definitions + dispatcher
│   │   ├── function_definitions.py
│   │   └── tool_executor.py
│   │
│   └── services/                # Direct service implementations (MCP fallback)
│       ├── oracle_service.py
│       ├── neo4j_service.py
│       ├── gemini_service.py
│       └── output_service.py
│
├── ingestion/
│   └── ingest_schema.py         # Multi-DB: Oracle → Gemini → Neo4j
│
└── frontend/
    └── app.py                   # Streamlit UI: linear + supervisor modes, feedback, trace tabs
```

---

## 📋 Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | `python3 --version` |
| Oracle DB | User needs `SELECT` on `ALL_*` data dictionary views |
| Neo4j 5.11+ | Community edition — [neo4j.com/deployment-center](https://neo4j.com/deployment-center/) |
| Gemini API key | Free at [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) |
| Redis (optional) | Only needed if running multiple FastAPI replicas — falls back to in-process cache otherwise |

---

## 🚀 Getting Started (single host, dev)

### Step 1 — Install

```bash
git clone <your-repo> && cd nlsql
pip install -r requirements.txt
```

### Step 2 — Configure

```bash
cp .env.example .env
```

Fill `.env` — see `.env.example` for the full list, including the production-scaling knobs (`ORACLE_POOL_MIN/MAX`, `MCP_POOL_MIN/MAX`, `MCP_BREAKER_FAILURE_THRESHOLD`, `MCP_BREAKER_COOLDOWN_S`, `REDIS_URL`, `ORACLE_MCP_TOKEN`/`NEO4J_MCP_TOKEN`). Minimal required:

```env
GEMINI_API_KEY=AIza...
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=your-password

FINCORE_USER=fincore_user
FINCORE_PASSWORD=secret
FINCORE_DSN=host1:1521/FINCORE
```

Edit `databases.yaml` to register your Oracle databases.

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
python -m mcp_servers.oracle_mcp.server   # :8001  (Streamable HTTP, /mcp /health /ready)
python -m mcp_servers.neo4j_mcp.server    # :8002  (Streamable HTTP, /mcp /health /ready)
uvicorn backend.main:app --reload         # :8000
streamlit run frontend/app.py             # :8501
```

Open **http://localhost:8501** 🎉

---

## 🏭 Scaling to production

The codebase is designed to run as a single host for development, but every layer can scale horizontally without protocol-level changes:

**MCP servers are stateless.** Both run Streamable HTTP with `stateless_http=True` — no in-memory session is pinned to a process, so any replica can serve any request. This is what makes plain round-robin load balancing correct (no sticky sessions needed).

**Connection pooling has a circuit breaker.** `MCPConnectionPool` trips to an OPEN state after `MCP_BREAKER_FAILURE_THRESHOLD` consecutive downstream failures and fails fast for `MCP_BREAKER_COOLDOWN_S` instead of letting every request pay the full checkout timeout during an outage. Pool capacity exhaustion (busy-but-healthy) does *not* trip the breaker — only actual call failures do.

**Caching is shared across replicas via Redis**, with a transparent local-dict fallback if `REDIS_URL` is unset. Three caches: `EmbeddingCache` (skip Gemini re-embedding), `SchemaCache` (skip repeat Neo4j lookups for the same table set), `ResultCache` (skip repeat Oracle execution for identical SQL within TTL).

**Bring up the full stack:**

```bash
docker compose up --build
```

This starts Redis, 2 Oracle MCP replicas, 2 Neo4j MCP replicas, HAProxy fronting both MCP tiers, 2 FastAPI backend replicas, and the Streamlit frontend. Scale further by adding more `oracle-mcp-N` / `neo4j-mcp-N` service blocks and corresponding `server` lines in `haproxy-compose.cfg` — no MCP server code changes needed.

**Pool sizing must be coordinated across layers.** `ORACLE_POOL_MAX` (the real `oracledb` connection ceiling per database) should be ≥ `MCP_POOL_MAX` (how many concurrent `execute_query` calls the MCP layer admits) — `db_manager.py` logs a warning at startup if they disagree, since otherwise contention happens invisibly inside the Oracle MCP server process where `/api/health` can't see it.

**Not yet implemented — tracked for a future phase:** OAuth 2.1 on the MCP servers themselves (currently only an optional static bearer token via `ORACLE_MCP_TOKEN`/`NEO4J_MCP_TOKEN`); an end-to-end per-request deadline budget coordinating frontend/backend/MCP timeouts.

---

## 📡 API Reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Liveness + DB config + MCP pool stats + cache stats |
| `GET` | `/api/databases` | Registered databases |
| `GET` | `/api/schema` | Full enriched schema (via Neo4j MCP) |
| `GET` | `/api/examples` | Example banking questions |
| `POST` | `/api/query` | Linear pipeline: NL → SQL → execute → results |
| `POST` | `/api/supervisor` | SSE: Gemini supervisor, dynamic multi-DB tool calling |
| `POST` | `/api/feedback` | Thumbs up/down + optional corrected SQL (invalidates caches) |

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
| `meta.cache_hit` / `meta.cache_source` | Which cache(s) served this request |

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
`corrected_sql` → replace stored SQL + `success_count + 2` + invalidate result/schema caches (local + Redis)

---

## 🔌 MCP Server Reference

Both servers run Streamable HTTP, mounted at `/mcp`. `stateless_http=True` — no client/server session affinity required.

### Oracle MCP — port 8001

| Tool | Arguments | Returns |
|---|---|---|
| `execute_query` | `db_id, sql, max_rows` | `{columns, rows, row_count, sql_executed, pii_warnings}` |
| `explain_plan` | `db_id, sql` | `{cost, has_full_scan, has_cartesian, plan_text}` |
| `get_schema` | `db_id, schema_name` | Full data dictionary JSON |
| `list_databases` | — | `[{id, name, schema, configured}]` |
| `check_read_only` | `sql` | `{valid, forbidden_keywords}` |

Operational routes: `GET /health` (liveness, fast), `GET /ready` (readiness — acquires a real Oracle connection per configured DB, 503 if any unreachable).

### Neo4j MCP — port 8002

| Tool | Arguments | Returns |
|---|---|---|
| `semantic_search` | `embedding_json, database_id, top_k` | `{tables, columns, cypher_used}` |
| `get_table_details` | `table_names_json, database_id` | List of table objects with columns |
| `get_join_path` | `table1, table2, database_id` | FK join path (single pair) |
| `get_join_paths_batch` | `table_names_json, database_id` | FK join paths for all candidate pairs in one query |
| `get_cross_db_hints` | `table_names_json, database_id` | Cross-DB link objects |
| `search_patterns` | `embedding_json, database_id, top_k, min_similarity` | Matched QueryPatterns |
| `store_pattern` | `database_id, nl_question, sql, schema_cypher, tables_used_json, execution_ms, embedding_json` | `{stored: bool}` |
| `get_schema_summary` | — | All databases + tables + domains |
| `record_feedback` | `nl_question, database_id, action, corrected_sql` | `{updated: bool}` |

Operational routes: `GET /health` (liveness), `GET /ready` (readiness — runs `RETURN 1` against Neo4j, 503 if unreachable).

---

## 🔒 Security

- Oracle user: `SELECT` privileges only — no DML ever granted
- Read-only enforced at application layer (ValidationAgent) AND Oracle MCP server
- PII columns auto-masked in SQL at execution time
- Gemini receives: enriched metadata only — never actual row values
- MCP servers bind to `0.0.0.0` by default — firewall ports 8001/8002 (or 7001/7002 if fronted by HAProxy) in production
- Optional static bearer token auth between FastAPI and the MCP servers via `ORACLE_MCP_TOKEN`/`NEO4J_MCP_TOKEN` — this is service-to-service plumbing, not a substitute for the OAuth 2.1 flow the MCP spec specifies for remote servers (not yet implemented — see [Scaling to production](#-scaling-to-production))
- `.env` is gitignored — never commit credentials

---

## ♻ QueryPattern Learning Loop

Every successful query stores a `(:QueryPattern)` node containing NL question, SQL, and the schema discovery Cypher. Future similar questions (cosine ≥ 0.85) inject the stored SQL as a dynamic few-shot example. User feedback (👍/👎 + corrected SQL) adjusts pattern weights and invalidates the relevant caches. The system improves automatically over time.

---

## 🗺 Roadmap

- [x] Phase 3A: ValidationAgent + SelfHealingAgent + feedback loop
- [x] Phase 3B: Oracle MCP server + Neo4j MCP server + typed client wrappers
- [x] Phase 3C: Supervisor agent (Gemini function calling) + multi-DB routing + SSE streaming
- [x] Phase 4 (scalability): Streamable HTTP migration (stateless, off legacy SSE) · connection-pool circuit breaker · Redis-backed shared caching · HAProxy/nginx load-balanced MCP tiers · coordinated pool sizing
- [ ] Phase 5: OAuth 2.1 on the MCP servers (replacing the static-token placeholder)
- [ ] Phase 5: End-to-end per-request deadline budget across frontend/backend/MCP timeouts
- [ ] Phase 5: `backend/config.py` consolidation — pool/cache/breaker settings currently read via raw `os.getenv()` in `cache.py`/`pool.py`/`db_manager.py` rather than through the central `Settings` object; unify on one source of truth
- [ ] Phase 5: Claude Desktop / external MCP client integration
