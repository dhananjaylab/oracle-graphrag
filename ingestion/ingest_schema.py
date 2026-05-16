"""
ingestion/ingest_schema.py
---------------------------
One-time script that builds the Neo4j schema graph from Oracle.

Pipeline:
  1. Connect to Oracle → pull tables, columns, FK relationships
  2. Group columns by table
  3. For each table: send column names to Gemini → get business labels + descriptions
  4. Generate embeddings for each enriched description
  5. Load Table and Column nodes into Neo4j with embeddings
  6. Load FK_TO relationships between tables
  7. Create vector indexes for semantic search

Run BEFORE starting the FastAPI backend for the first time.
Re-run whenever the Oracle schema changes significantly.

Usage:
    cd nlsql
    python -m ingestion.ingest_schema
    python -m ingestion.ingest_schema --schema FINCORE   # target a specific schema
"""

import argparse
import asyncio
import sys
import time
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv()

# Imports after dotenv so Settings can read .env
import google.generativeai as genai
from neo4j import AsyncGraphDatabase

from backend.config import settings
from backend.services import oracle_service
from backend.services import neo4j_service
from backend.services.gemini_service import get_embedding, enrich_columns

genai.configure(api_key=settings.gemini_api_key)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _group_columns_by_table(col_rows: list[dict]) -> dict[str, list[dict]]:
    """Group flat column rows into a dict keyed by table_name."""
    tables: dict[str, list[dict]] = defaultdict(list)
    for row in col_rows:
        tables[row["table_name"]].append(row)
    return dict(tables)


def _build_table_description(table_name: str, cols: list[dict]) -> str:
    """Build a plain-text description of a table for embedding."""
    col_names = ", ".join(c["column_name"] for c in cols[:20])
    comment = cols[0].get("table_comment", "") if cols else ""
    if comment:
        return f"{table_name}: {comment}. Columns: {col_names}"
    return f"Table {table_name} with columns: {col_names}"


def _rate_limited_sleep(delay: float = 0.5):
    """Small sleep between Gemini API calls to avoid rate limiting."""
    time.sleep(delay)


# ── Main ingestion ────────────────────────────────────────────────────────────

async def ingest(schema_override: str | None = None) -> None:
    schema_name = schema_override or settings.oracle_schema or None
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    print("\n" + "="*46)
    print("  NL-SQL Schema Ingestion")
    print("="*46)
    print(f"  Oracle DSN   : {settings.oracle_dsn}")
    print(f"  Oracle Schema: {schema_name or '(all accessible)'}")
    print(f"  Neo4j URI    : {settings.neo4j_uri}")
    print("="*46 + "\n")

    # ── Step 1: Pull Oracle data dictionary ───────────────────────────────────
    print("📥 Fetching Oracle data dictionary…")
    try:
        data_dict = oracle_service.get_data_dictionary(schema=schema_name)
    except Exception as e:
        print(f"❌ Oracle connection failed: {e}")
        print("   Check ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN in .env")
        sys.exit(1)

    col_rows = data_dict["columns"]
    fk_rows = data_dict["foreign_keys"]

    if not col_rows:
        print("❌ No tables found. Check ORACLE_SCHEMA in .env or schema access.")
        sys.exit(1)

    tables = _group_columns_by_table(col_rows)
    print(f"✅ Found {len(tables)} tables, {len(col_rows)} columns, {len(fk_rows)} FKs\n")

    # ── Step 2: Connect to Neo4j ──────────────────────────────────────────────
    print("🔌 Connecting to Neo4j…")
    try:
        driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_username, settings.neo4j_password),
        )
        await driver.verify_connectivity()
        print("✅ Connected!\n")
    except Exception as e:
        print(f"❌ Neo4j connection failed: {e}")
        print("   Check NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD in .env")
        sys.exit(1)

    async with driver.session() as session:
        # ── Step 3: Create indexes ────────────────────────────────────────────
        print("🗂️  Creating Neo4j vector indexes and constraints…")
        await neo4j_service.create_indexes(session)
        print("✅ Indexes ready\n")

        # ── Step 4: Process each table ────────────────────────────────────────
        total = len(tables)
        for idx, (table_name, cols) in enumerate(tables.items(), 1):
            schema_owner = cols[0].get("owner", schema_name or "")
            table_comment = cols[0].get("table_comment", "")

            print(f"[{idx:>3}/{total}] {table_name}", end="  ", flush=True)

            # ── Step 4a: Enrich column names with Gemini ──────────────────────
            try:
                enriched_cols = enrich_columns(table_name, table_comment, cols)
                _rate_limited_sleep(0.4)   # stay within Gemini free-tier rate limits
            except Exception as e:
                print(f"⚠ enrichment failed ({e}), using raw names")
                enriched_cols = [
                    {
                        "column": c["column_name"],
                        "label": c["column_name"].replace("_", " ").title(),
                        "description": c.get("col_comment") or c["column_name"],
                        "is_pii": False,
                    }
                    for c in cols
                ]

            # Build enrichment lookup: column_name → enriched info
            enrichment_map = {e["column"]: e for e in enriched_cols}

            # ── Step 4b: Build and embed table-level description ──────────────
            table_desc_raw = _build_table_description(table_name, cols)
            # Use enriched labels for a richer embedding
            enriched_labels = " ".join(
                e.get("label", "") for e in enriched_cols if e.get("label")
            )
            table_desc_enriched = (
                f"{table_name}: {table_comment or ''} | "
                f"Columns: {enriched_labels}"
            )
            table_embedding = get_embedding(table_desc_enriched)
            _rate_limited_sleep(0.2)

            # ── Step 4c: Upsert Table node ────────────────────────────────────
            await neo4j_service.upsert_table(
                session=session,
                name=table_name,
                schema=schema_owner,
                description=table_desc_raw,
                enriched_description=table_desc_enriched,
                embedding=table_embedding,
            )

            # ── Step 4d: Upsert Column nodes ──────────────────────────────────
            for col in cols:
                col_name = col["column_name"]
                enriched = enrichment_map.get(col_name, {})
                label = enriched.get("label", col_name.replace("_", " ").title())
                description = enriched.get("description", col.get("col_comment") or col_name)
                is_pii = enriched.get("is_pii", False)

                col_embed_text = (
                    f"{table_name}.{col_name} ({col['data_type']}): "
                    f"{label} — {description}"
                )
                col_embedding = get_embedding(col_embed_text)
                _rate_limited_sleep(0.15)

                await neo4j_service.upsert_column(
                    session=session,
                    table_name=table_name,
                    col_name=col_name,
                    data_type=col["data_type"],
                    nullable=col.get("nullable", "Y"),
                    label=label,
                    enriched_description=f"{label}: {description}",
                    is_pii=is_pii,
                    embedding=col_embedding,
                )

            print("✓")

        # ── Step 5: Load FK relationships ─────────────────────────────────────
        print(f"\n🔗 Loading {len(fk_rows)} foreign key relationships…")
        fk_loaded = 0
        for fk in fk_rows:
            try:
                await neo4j_service.upsert_fk(
                    session=session,
                    from_table=fk["table_name"],
                    to_table=fk["ref_table"],
                    from_col=fk["column_name"],
                    to_col=fk["ref_column"],
                )
                fk_loaded += 1
            except Exception as e:
                print(f"  ⚠ FK skipped ({fk['table_name']} → {fk['ref_table']}): {e}")

        print(f"✅ {fk_loaded} FK relationships loaded")

    await driver.close()

    print("\n" + "="*46)
    print("  Ingestion complete!")
    print(f"  {total} tables · {len(col_rows)} columns · {fk_loaded} FKs")
    print("="*46)
    print("\nNext steps:")
    print("  Terminal 1: uvicorn backend.main:app --reload")
    print("  Terminal 2: streamlit run frontend/app.py\n")


def main():
    parser = argparse.ArgumentParser(description="Ingest Oracle schema into Neo4j")
    parser.add_argument(
        "--schema", type=str, default=None,
        help="Oracle schema name to ingest (overrides ORACLE_SCHEMA in .env)"
    )
    args = parser.parse_args()
    asyncio.run(ingest(schema_override=args.schema))


if __name__ == "__main__":
    main()
