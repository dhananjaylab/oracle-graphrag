# 🏦 NL-SQL — Banking Natural Language Analytics

> Ask questions in plain English. Get Oracle SQL, live results, charts, and a business summary — powered by GraphRAG + Gemini.

---

## 🧠 How it works

1. **Schema ingestion (one-time):** Oracle data dictionary → Gemini enriches cryptic column names → Neo4j stores schema graph with vector embeddings
2. **At query time:** Your question is embedded → Neo4j finds relevant tables via semantic search + FK graph traversal → Gemini generates Oracle SQL using only schema metadata → SQL executes on-prem → results returned
3. **Output:** Table · Auto-chart · Plain English summary · Excel download

**Data privacy:** Only schema metadata (table/column names + descriptions) goes to Gemini. Raw data never leaves your network.

---

## 🛠 Tech Stack

| Layer | Technology |
|---|---|
| 🖥️ Frontend | Streamlit |
| 🔙 Backend | FastAPI + Python 3.11+ |
| 🤖 LLM | Gemini 1.5 Flash (Google AI Studio) |
| 📐 Embeddings | text-embedding-004 (768 dims) |
| 🗄️ Graph DB | Neo4j 5.x (on-prem) |
| 🏛️ Data DB | Oracle DB (on-prem) |
| 📊 Charts | Plotly |
| 📋 Export | pandas + openpyxl |

---

## 📋 Prerequisites

| Requirement | Notes |
|---|---|
| 🐍 Python 3.11+ | `python3 --version` |
| 🏛️ Oracle DB | Accessible on-prem; user must have SELECT on data dict views |
| 🗄️ Neo4j 5.11+ | Install: https://neo4j.com/deployment-center/ — Community edition sufficient |
| 🔑 Gemini API key | Free at https://aistudio.google.com/app/apikey |

---

## 🚀 Getting Started

### Step 1 — Install dependencies

```bash
git clone <your-repo>
cd nlsql
pip install -r requirements.txt
```

### Step 2 — Configure credentials

```bash
cp .env.example .env
```

Edit `.env`:

```bash
GEMINI_API_KEY=AIza...

ORACLE_USER=your_user
ORACLE_PASSWORD=your_password
ORACLE_DSN=host:1521/service_name
ORACLE_SCHEMA=YOUR_SCHEMA_NAME   # e.g. FINCORE

NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=your-password
```

### Step 3 — Run schema ingestion (one-time)

```bash
python -m ingestion.ingest_schema
# Or target a specific schema:
python -m ingestion.ingest_schema --schema FINCORE
```

This pulls your Oracle schema, enriches cryptic column names with Gemini, generates vector embeddings, and loads the schema graph into Neo4j. Takes 2–10 minutes depending on schema size.

### Step 4 — Start the backend

```bash
# Terminal 1
uvicorn backend.main:app --reload
```

Backend runs at **http://localhost:8000**

### Step 5 — Start the frontend

```bash
# Terminal 2
streamlit run frontend/app.py
```

Open **http://localhost:8501** in your browser. 🎉

---

## 🗂 Project Structure

```
nlsql/
│
├── 🌱 ingestion/
│   └── ingest_schema.py        # One-time: Oracle → Gemini labels → Neo4j
│
├── 🔙 backend/
│   ├── main.py                 # FastAPI app · CORS · routes
│   ├── config.py               # Loads .env · validates on startup
│   ├── models.py               # Pydantic request + response models
│   │
│   ├── prompts/
│   │   ├── sql_prompt.py       # Oracle SQL system prompt + banking few-shots
│   │   └── enrichment_prompt.py # Column name enrichment prompt
│   │
│   ├── routes/
│   │   ├── query.py            # POST /api/query — full NL-to-SQL pipeline
│   │   └── schema.py           # GET /api/schema · /api/examples · /health
│   │
│   └── services/
│       ├── oracle_service.py   # Connection pool · execute_sql · data dict
│       ├── neo4j_service.py    # Vector search · join paths · schema graph
│       ├── gemini_service.py   # Embeddings · SQL gen · summarization
│       └── output_service.py   # DataFrame · chart detection · Excel export
│
├── 🖥️ frontend/
│   └── app.py                  # Full Streamlit UI
│
├── .env.example
├── requirements.txt
└── README.md
```

---

## 📡 API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Liveness check |
| `GET` | `/api/schema` | All tables with enriched descriptions and column counts |
| `GET` | `/api/examples` | Pre-built example questions |
| `POST` | `/api/query` | Full pipeline: NL → SQL → execute → results |

### POST /api/query payload

```json
{
  "question": "Show total loan disbursements by branch this quarter",
  "execute": true,
  "max_rows": 1000,
  "conversation_history": []
}
```

---

## 🔒 Security Notes

- Oracle user should have `SELECT` privileges only — no DML grants needed
- The backend enforces read-only SQL at the application layer (forbidden keyword check)
- PII columns (name, email, mobile, PAN, Aadhaar, account number) are auto-masked in output
- Only schema metadata sent to Gemini — raw Oracle data stays on-prem

---

## 🔄 Re-running Ingestion

Run ingestion again whenever:
- New tables are added to Oracle
- Column names or comments are updated
- Business meaning of tables changes significantly

```bash
python -m ingestion.ingest_schema --schema YOUR_SCHEMA
```

The script uses `MERGE` — it updates existing nodes rather than duplicating them.

---

## 🗺️ Roadmap

- [ ] Phase 2: Query history stored in Neo4j as patterns (improves accuracy over time)
- [ ] Phase 2: User feedback loop (thumbs up/down refines future queries)
- [ ] Phase 3: Validation agent with sqlglot + EXPLAIN PLAN cost check
- [ ] Phase 3: Self-healing agent for SQL error recovery
- [ ] Phase 3: MCP server wrappers for Oracle and Neo4j
