# 🏦 NL-SQL — Banking Natural Language Analytics
## Phase 3A: ValidationAgent + SelfHealingAgent + Feedback Loop

> Ask questions in plain English across multiple Oracle databases.
> SQL failures auto-recover. Results improve over time from user feedback.
> Powered by GraphRAG + Gemini + Neo4j QueryPatterns.

---

## 🔁 Phase 3A additions

| What | How |
|---|---|
| **ValidationAgent** | sqlglot syntax parse → EXPLAIN PLAN cost check → read-only guard → PII masking |
| **SelfHealingAgent** | Classifies ORA-* errors → targeted Gemini re-prompt → re-validate → retry up to 3× |
| **Feedback endpoint** | `POST /api/feedback` — 👍/👎 updates `QueryPattern.success_count` in Neo4j |
| **Agent trace** | Every response includes `agent_trace` — full validation + healing log |
| **`🛠 Agent` tab** | Streamlit shows validation issues, cost estimate, healing attempts |

---

## 🧠 Full pipeline (14 steps + agent gates)

```
Question
  │
  ▼
Embed (gemini-embedding-001, 3072 dims)
  ├──► Search QueryPatterns → inject matched SQL as few-shot if similarity ≥ 0.85
  ▼
GraphRAG (Neo4j) → table vector search + column vector search + FK join paths
  ▼
Schema context (metadata only — no raw data)
  ▼
Gemini SQL generation (+ dynamic few-shots from QueryPatterns)
  ▼
┌─────────────────────────────────────┐
│  ValidationAgent                    │
│  1. sqlglot Oracle syntax parse     │
│  2. Read-only keyword guard         │
│  3. PII column detection + masking  │
│  4. EXPLAIN PLAN cost estimation    │
└──────────────┬──────────────────────┘
          valid│  invalid or cost exceeded
               │         │
               │         ▼
               │   SelfHealingAgent (max 3 retries)
               │   1. classify ORA-* error type
               │   2. targeted Gemini re-prompt
               │   3. re-validate
               │   4. re-execute
               │         │
               └────┬────┘
                    ▼
             Execute on Oracle (on-prem)
                    ▼
         Output: table · chart · summary · Excel
                    ▼
         [background] Store QueryPattern in Neo4j
                       preserves SQL + schema Cypher
```

---

## 🛠 Tech stack

| Layer | Technology |
|---|---|
| 🖥️ Frontend | Streamlit (port 8501) · 6 tabs incl. `🛠 Agent` |
| 🔙 Backend | FastAPI + Python 3.11+ (port 8000) |
| 🤖 LLM | `gemini-flash-latest` (generation + healing + summarization) |
| 📐 Embeddings | `gemini-embedding-001` · 3072 dims |
| 🗄️ Graph DB | Neo4j 5.11+ · enriched schema graph + QueryPattern store |
| 🏛️ Data DB | Oracle DB · multiple instances · thin mode |
| 🔍 SQL validation | sqlglot (Oracle dialect) |

---

## 🗂 Enriched Neo4j graph

```
(:Database)  (:Table)  (:Column)  (:Index)  (:BusinessDomain)
(:QueryPattern {nl_question, sql, schema_cypher, embedding,
                success_count, avg_execution_ms, tables_used})

(:Table)-[:FK_TO]->(:Table)
(:Table)-[:CROSS_DB_JOIN]->(:Table)   ← cross-database links
(:QueryPattern)-[:QUERIES]->(:Table)
```

---

## 🚀 Getting started

### 1. Install
```bash
pip install -r requirements.txt
```

### 2. Configure
```bash
cp .env.example .env   # fill in credentials
```

`databases.yaml` — register Oracle databases:
```yaml
databases:
  - id: fincore
    name: "Core Banking"
    env_prefix: FINCORE      # → FINCORE_USER / _PASSWORD / _DSN in .env
    schema: FINCORE
    domains:
      - { name: Lending, hint: "Loans, EMI, disbursement" }
      - { name: CASA,    hint: "Savings and current accounts" }
```

### 3. Ingest schema (one-time)
```bash
python -m ingestion.ingest_schema          # all databases
python -m ingestion.ingest_schema --db fincore  # one database
```

### 4. Test agents (no server needed)
```bash
python scratch/test_agents.py
```

### 5. Start
```bash
# Terminal 1
uvicorn backend.main:app --reload

# Terminal 2
streamlit run frontend/app.py
```

---

## 🗂 Project structure

```
nlsql/
│
├── databases.yaml              # DB list, domains, cross-DB links
├── .env                        # Keys + DB credentials (gitignored)
│
├── ingestion/
│   └── ingest_schema.py        # Oracle → Gemini enrichment → Neo4j
│
├── backend/
│   ├── main.py
│   ├── config.py               # Gemini + Neo4j settings
│   ├── db_manager.py           # Multi-DB Oracle pool manager
│   ├── models.py               # Pydantic models incl. AgentTrace, FeedbackRequest
│   │
│   ├── agents/                 ← Phase 3A
│   │   ├── validation_agent.py # sqlglot · EXPLAIN PLAN · PII masking
│   │   └── self_healing_agent.py # ORA-* classification · retry loop
│   │
│   ├── prompts/
│   │   ├── sql_prompt.py       # Oracle SQL system prompt + static few-shots
│   │   ├── enrichment_prompt.py
│   │   └── healing_prompt.py   # Error-type strategies for SelfHealingAgent
│   │
│   ├── routes/
│   │   ├── query.py            # 14-step pipeline + agent integration
│   │   └── schema.py           # /schema · /databases · /feedback · /health
│   │
│   └── services/
│       ├── oracle_service.py   # execute_sql · get_data_dictionary
│       ├── neo4j_service.py    # GraphRAG · QueryPattern · feedback functions
│       ├── gemini_service.py   # embed · generate_sql · heal_sql · summarize
│       └── output_service.py   # DataFrame · chart · Excel
│
├── frontend/
│   └── app.py                  # 6 tabs: Table·Chart·Summary·SQL·Cypher·Agent
│                                # + 👍/👎 feedback + corrected SQL input
│
└── scratch/
    ├── test_agents.py          # Phase 3A: ValidationAgent + SelfHealingAgent tests
    ├── test_end_to_end.py      # Full pipeline smoke test (server must run)
    ├── test_oracle.py          # Oracle connectivity check
    ├── test_neo4j.py           # Neo4j connectivity + index check
    ├── check_dims.py           # Gemini embedding dimensions
    └── list_gemini_models.py   # List available Gemini models
```

---

## 📡 API reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Liveness + DB config status |
| `GET` | `/api/databases` | Registered databases |
| `GET` | `/api/schema` | Enriched schema (all DBs) |
| `GET` | `/api/examples` | Example banking questions |
| `POST` | `/api/query` | Full pipeline → results + `agent_trace` |
| `POST` | `/api/feedback` | 👍/👎 + optional corrected SQL |

### POST /api/query

```json
{
  "question":          "Show NPA ratio by product segment",
  "db_id":             "riskdb",
  "execute":           true,
  "max_rows":          1000,
  "skip_explain_plan": false,
  "conversation_history": []
}
```

### POST /api/feedback

```json
{
  "nl_question":   "Show NPA ratio by product segment",
  "db_id":         "riskdb",
  "rating":        5,
  "corrected_sql": null
}
```

`rating` ≥ 4 → thumbs up → `success_count + 1`
`rating` < 4 → thumbs down → `success_count - 1`
`corrected_sql` provided → replaces stored SQL + `success_count + 2`

### agent_trace in QueryResponse

```json
{
  "agent_trace": {
    "validation": {
      "valid": true,
      "sql": "SELECT ...",
      "issues": [],
      "warnings": ["⚠ Full table scan detected"],
      "cost_estimate": 842,
      "cost_blocked": false
    },
    "healing_attempts": [],
    "healed": false,
    "total_attempts": 1
  }
}
```

---

## ♻️ QueryPattern learning loop

Every successful query stores:
- NL question + Oracle SQL + schema discovery Cypher
- Execution time, tables used, success_count, embedding

Next similar question (cosine ≥ 0.85):
- Stored SQL injected as dynamic few-shot → better accuracy
- Stored Cypher shown in `🔗 Cypher` tab for audit

Feedback updates success_count → higher-count patterns rank higher.

---

## 🔒 Security

- Oracle: `SELECT` privileges only — no DML
- Read-only enforced before every execution
- PII columns (name, mobile, PAN, Aadhaar, account no) auto-masked
- Gemini receives: enriched column descriptions + aggregate stats only
- Raw Oracle row data never leaves on-prem

---

## 🗺️ Roadmap

```
Phase 3A ✅  ValidationAgent · SelfHealingAgent · Feedback endpoint
Phase 3B     Oracle MCP Server · Neo4j MCP Server · agent uses MCP tools
Phase 3C     Supervisor agent (Gemini function calling) · multi-DB routing · Claude Desktop
```
