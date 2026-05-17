# 🏦 NL-SQL  v2 — Banking Natural Language Analytics

> Ask questions in plain English across **multiple Oracle databases**. Get Oracle SQL, live results, charts, and a plain-English summary — powered by GraphRAG + Gemini + self-improving QueryPatterns.

---

## 🧠 How it works

```
User question
     │
     ▼
Embed (gemini-embedding-001, 3072 dims)
     │
     ├──► Search QueryPattern graph → find past similar queries
     │    └── If match (≥85% similarity): inject stored SQL as few-shot
     │
     ▼
GraphRAG semantic search (Neo4j — scoped to selected DB)
     ├── Table vector search on enriched descriptions
     ├── Column vector search on business labels
     └── FK join-path traversal (graph traversal)
     ↓
Build schema context (metadata only — no raw data ever)
     │
     ▼
Gemini SQL generation  ← schema context + dynamic few-shots from QueryPatterns
     │
     ▼
Validate SQL (read-only guard + PII auto-masking)
     │
     ▼
Execute on Oracle DB (on-prem)
     │
     ▼
Output: Table · Auto-chart · Plain English summary · Excel export
     │
     ▼
[Background] Store as QueryPattern in Neo4j
             Preserves: NL question + SQL + schema discovery Cypher
```

**Data privacy guarantee:** Only schema metadata goes to Gemini.
Raw Oracle row data **never** leaves your on-prem network.

---

## 🛠 Tech Stack

| Layer | Technology |
|---|---|
| 🖥️ Frontend | Streamlit (port 8501) |
| 🔙 Backend | FastAPI + Python 3.11+ (port 8000) |
| 🤖 LLM | `gemini-flash-latest` (Google AI Studio) |
| 📐 Embeddings | `gemini-embedding-001` · 3072 dims |
| 🗄️ Graph DB | Neo4j 5.11+ (on-prem) |
| 🏛️ Data DB | Oracle DB — multiple instances supported |
| 📊 Charts | Plotly |
| 📋 Export | pandas + openpyxl |
| ⚙️ Config | `databases.yaml` + `.env` |

---

## 🗂 Enriched Neo4j Graph Schema

```
(:Database  {id, name, schema, description, last_ingested, table_count})
(:Table     {name, database_id, schema_name, enriched_description, embedding,
             is_view, row_count_approx, pk_columns})
(:Column    {name, table_name, database_id, data_type, label, enriched_description,
             embedding, is_pk, is_unique, is_indexed, is_pii, cardinality_hint})
(:Index     {name, table_name, database_id, columns, is_unique, index_type})
(:BusinessDomain {name, database_id, hint})
(:QueryPattern   {id, database_id, nl_question, sql, schema_cypher,
                  tables_used, success_count, avg_execution_ms, embedding})

(:Database)-[:HAS_TABLE]->(:Table)
(:Table)-[:HAS_COLUMN]->(:Column)
(:Table)-[:FK_TO {from_col, to_col}]->(:Table)
(:Table)-[:HAS_INDEX]->(:Index)
(:Table)-[:IN_DOMAIN]->(:BusinessDomain)
(:BusinessDomain)-[:BELONGS_TO]->(:Database)
(:QueryPattern)-[:QUERIES]->(:Table)
(:QueryPattern)-[:FOR_DB]->(:Database)
(:Table)-[:CROSS_DB_JOIN {from_col, to_col, description}]->(:Table)
```

---

## 📋 Prerequisites

| Requirement | Notes |
|---|---|
| 🐍 Python 3.11+ | `python3 --version` |
| 🏛️ Oracle DB | User needs `SELECT` on `ALL_*` data dictionary views |
| 🗄️ Neo4j 5.11+ | Community edition sufficient — [neo4j.com/deployment-center](https://neo4j.com/deployment-center/) |
| 🔑 Gemini API key | Free at [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) |

---

## 🚀 Getting Started

### Step 1 — Install

```bash
git clone <your-repo> && cd nlsql
pip install -r requirements.txt
```

### Step 2 — Configure

```bash
cp .env.example .env   # fill in credentials
```

Edit `databases.yaml` to register your Oracle databases:

```yaml
databases:
  - id: fincore
    name: "Core Banking"
    env_prefix: FINCORE       # looks for FINCORE_USER/PASSWORD/DSN in .env
    schema: FINCORE
    description: "Core banking — loans, CASA, transactions, GL"
    domains:
      - { name: Lending,      hint: "Loan origination, disbursement, EMI" }
      - { name: CASA,         hint: "Current and savings accounts" }
      - { name: Transactions, hint: "Financial transactions, GL entries" }

  - id: riskdb
    name: "Risk Management"
    env_prefix: RISKDB
    schema: RISK
    description: "Risk — NPA, credit ratings, exposure"
    domains:
      - { name: NPA,         hint: "Non-performing assets, provision" }
      - { name: Credit Risk, hint: "Credit ratings, borrower limits" }

cross_db_links:
  - from_db: fincore
    from_table: LOAN_MASTER
    from_col: LOAN_ACCT_NO
    to_db: riskdb
    to_table: NPA_MASTER
    to_col: LOAN_ACCT_NO
    description: "Loans classified as NPA in risk system"
```

### Step 3 — Ingest schema

```bash
python -m ingestion.ingest_schema          # all DBs in databases.yaml
python -m ingestion.ingest_schema --db fincore           # one DB
python -m ingestion.ingest_schema --db fincore --schema FINCORE  # explicit schema
```

Takes 2–15 min depending on schema size. Re-run when schema changes.

### Step 4 — Start

```bash
# Terminal 1
uvicorn backend.main:app --reload

# Terminal 2
streamlit run frontend/app.py
```

Open **http://localhost:8501** 🎉

---

## 🗂 Project Structure

```
nlsql/
│
├── databases.yaml              # Non-sensitive: DB list, domains, cross-DB links
├── .env                        # Sensitive: keys + DB credentials (gitignored)
├── .env.example
├── requirements.txt
│
├── ingestion/
│   └── ingest_schema.py        # Multi-DB: Oracle → Gemini → Neo4j enriched graph
│
├── backend/
│   ├── main.py                 # FastAPI app
│   ├── config.py               # Neo4j + Gemini env settings
│   ├── db_manager.py           # Multi-DB pool manager (reads databases.yaml)
│   ├── models.py               # Pydantic request/response models
│   ├── prompts/
│   │   ├── sql_prompt.py       # Oracle SQL system prompt + static few-shots
│   │   └── enrichment_prompt.py# Domain-aware column enrichment prompt
│   ├── routes/
│   │   ├── query.py            # POST /api/query — 14-step pipeline
│   │   └── schema.py           # GET /api/schema · /databases · /health
│   └── services/
│       ├── oracle_service.py   # Multi-DB execution + enriched data dictionary
│       ├── neo4j_service.py    # GraphRAG + QueryPattern store/retrieve
│       ├── gemini_service.py   # Embeddings, SQL gen + dynamic few-shots
│       └── output_service.py   # DataFrame, chart detection, Excel export
│
├── frontend/
│   └── app.py                  # Streamlit: DB selector, 5 tabs incl. Cypher viewer
│
└── scratch/                    # Connectivity test scripts
    ├── check_dims.py
    ├── list_gemini_models.py
    ├── test_oracle.py
    └── test_neo4j.py
```

---

## 📡 API Reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Liveness + DB config status |
| `GET` | `/api/databases` | All registered databases |
| `GET` | `/api/schema` | Full enriched schema with domains and tables |
| `GET` | `/api/examples` | Example banking questions |
| `POST` | `/api/query` | Full NL → SQL → execute → results pipeline |

**POST /api/query payload:**

```json
{
  "question": "Show total loan disbursements by branch this quarter",
  "db_id": "fincore",
  "execute": true,
  "max_rows": 1000,
  "conversation_history": []
}
```

**Key response fields:**

| Field | Description |
|---|---|
| `sql` | Generated Oracle SQL |
| `schema_cypher` | Neo4j Cypher used for schema discovery — always returned and stored |
| `matched_pattern` | Reused QueryPattern (if similarity ≥ 0.85): includes its stored Cypher |
| `meta.pattern_matched` | `true` when a stored pattern drove SQL generation |
| `meta.tables_used` | Tables the GraphRAG pipeline selected |

---

## ♻ QueryPattern Learning Loop

Every successful query is persisted as a `(:QueryPattern)` node:
- NL question + generated SQL
- **Schema discovery Cypher** (the Neo4j queries that found the relevant tables)
- Tables used, execution time, success count

On similar future questions (cosine similarity ≥ 0.85):
- Stored SQL injected as a dynamic few-shot example → better accuracy
- Stored Cypher shown in the **🔗 Cypher tab** for audit/debugging
- `success_count` increments → higher-success patterns rank higher over time

The system improves automatically as it handles more queries.

---

## 🔒 Security Notes

- Oracle user: `SELECT` privileges only — no DML ever granted
- Read-only enforced at application layer before every execution
- PII columns (name, mobile, PAN, Aadhaar, account no) auto-masked in SQL output
- Gemini receives: enriched column descriptions only — never actual row values
- Summarization: only aggregate stats (sum/avg/min/max) sent to Gemini

---

## 🔄 Re-ingesting

```bash
python -m ingestion.ingest_schema              # all DBs, uses MERGE (safe to repeat)
python -m ingestion.ingest_schema --db riskdb  # single DB re-ingest
```

Add a new database: register in `databases.yaml`, add creds to `.env`, then ingest.

---

## 🗺 Roadmap

- [ ] Phase 3: Validation agent — sqlglot parse + EXPLAIN PLAN cost estimation
- [ ] Phase 3: Self-healing agent — automatic SQL error recovery
- [ ] Phase 3: MCP server wrappers (Oracle MCP + Neo4j MCP)
- [ ] Phase 3: User feedback loop (thumbs up/down → updates pattern weight)
- [ ] Phase 4: Cross-DB query routing with Oracle DB Link awareness
