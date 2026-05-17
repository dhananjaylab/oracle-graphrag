"""
ingestion/ingest_schema.py  (v2 — multi-DB enriched ingestion)

Pipeline (per database):
  1.  Pull Oracle data dictionary → tables, columns, PKs, indexes, FKs, views, row counts
  2.  Group columns by table
  3.  Batch-send column names to Gemini → enriched labels + descriptions (no data)
  4.  Generate embeddings for table and column descriptions
  5.  Upsert (:Database), (:BusinessDomain) nodes
  6.  Upsert (:Table) nodes with is_view, row_count_approx, pk_columns, embedding
  7.  Upsert (:Column) nodes with is_pk, is_unique, is_indexed, cardinality_hint, embedding
  8.  Upsert (:Index) nodes
  9.  Upsert (:Table)-[:FK_TO]->(:Table) FK relationships
 10.  Link tables to domains based on databases.yaml domain config
 11.  After all DBs: upsert CROSS_DB_JOIN edges from databases.yaml cross_db_links
 12.  Create all Neo4j vector indexes

Usage:
    cd nlsql
    python -m ingestion.ingest_schema              # ingest all databases.yaml entries
    python -m ingestion.ingest_schema --db fincore # ingest one database by id
    python -m ingestion.ingest_schema --db fincore --schema FINCORE
"""

import argparse
import asyncio
import sys
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import google.generativeai as genai
from neo4j import AsyncGraphDatabase

from backend.config import settings
from backend.db_manager import db_manager, DBConfig
from backend.services import oracle_service, neo4j_service
from backend.services.gemini_service import get_embedding, enrich_columns

genai.configure(api_key=settings.gemini_api_key)

_RATE_DELAY_ENRICH  = 0.4    # seconds between Gemini enrichment calls
_RATE_DELAY_EMBED   = 0.15   # seconds between embedding calls


# ── Helpers ────────────────────────────────────────────────────────────────────

def _group_by_table(col_rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in col_rows:
        groups[row["table_name"]].append(row)
    return dict(groups)


def _table_embed_text(table_name: str, table_comment: str,
                       enriched_labels: str) -> str:
    base = table_comment.strip() if table_comment else ""
    return f"{table_name}: {base} | Columns: {enriched_labels}".strip(" |")


def _col_embed_text(table_name: str, col_name: str, data_type: str,
                     label: str, description: str) -> str:
    return f"{table_name}.{col_name} ({data_type}): {label} — {description}"


def _domain_for_table(table_name: str, cfg: DBConfig) -> str | None:
    """Return the first domain whose table list contains this table name."""
    for domain in cfg.domains:
        # Domain-level table lists aren't in yaml — match by name hints
        # Fallback: all tables belong to no domain unless explicitly mapped
        pass
    return None


def _infer_domain(table_name: str, cfg: DBConfig) -> str | None:
    """
    Heuristic domain assignment based on table name keywords.
    Can be overridden by adding explicit table lists to databases.yaml.
    """
    t = table_name.upper()
    for domain in cfg.domains:
        kw = domain.name.upper()
        # Simple keyword match — extend this per your naming conventions
        if kw in t:
            return domain.name
        # Specific hints per domain name
        hints: dict[str, list[str]] = {
            "LENDING":        ["LOAN", "EMI", "DISB", "MORATORIUM"],
            "CASA":           ["CASA", "SAVING", "CURRENT", "SB", "CA"],
            "TRANSACTIONS":   ["TXN", "TRANSACTION", "TRNX", "XFER"],
            "GENERAL LEDGER": ["GL", "LEDGER", "COA", "JOURNAL"],
            "CUSTOMERS":      ["CUST", "CLIENT", "KYC"],
            "BRANCH":         ["BRCH", "BRANCH", "REGION", "ZONE"],
            "NPA":            ["NPA", "OVERDUE", "PROVISION", "WRITTEN"],
            "CREDIT RISK":    ["CREDIT", "RATING", "EXPOSURE", "LIMIT"],
            "MARKET RISK":    ["FX", "FOREX", "RATE", "VAR", "MARKET"],
            "PROVISIONING":   ["PROV", "ECL", "IFRS", "STAGE"],
        }
        for kw2, keywords in hints.items():
            if domain.name.upper() == kw2:
                if any(k in t for k in keywords):
                    return domain.name
    return None


# ── Per-DB ingestion ───────────────────────────────────────────────────────────

async def ingest_db(cfg: DBConfig, driver, schema_override: str | None = None) -> dict:
    """Ingest one database. Returns stats dict."""
    print(f"\n{'='*56}")
    print(f"  Database: {cfg.name}  [{cfg.id}]")
    print(f"  Oracle DSN:  {cfg.dsn}")
    print(f"  Schema:      {schema_override or cfg.schema or '(all)'}")
    print(f"{'='*56}\n")

    # ── Pull Oracle data dictionary ────────────────────────────────────────
    print("  📥 Fetching Oracle data dictionary …")
    try:
        dd = oracle_service.get_data_dictionary(cfg.id, schema=schema_override)
    except Exception as e:
        print(f"  ❌ Oracle connection failed: {e}")
        return {"tables": 0, "columns": 0, "fks": 0, "indexes": 0}

    col_rows   = dd["columns"]
    fk_rows    = dd["foreign_keys"]
    idx_rows   = dd["indexes"]
    pk_map     = dd["pk_map"]
    row_counts = dd["row_counts"]

    if not col_rows:
        print("  ❌ No tables found. Check schema name and Oracle user grants.")
        return {"tables": 0, "columns": 0, "fks": 0, "indexes": 0}

    tables = _group_by_table(col_rows)
    print(f"  ✅ {len(tables)} tables · {len(col_rows)} columns · "
          f"{len(fk_rows)} FKs · {len(idx_rows)} indexes\n")

    async with driver.session() as session:

        # ── Upsert Database node ───────────────────────────────────────────
        await neo4j_service.upsert_database(
            session, db_id=cfg.id, name=cfg.name,
            schema=cfg.qualified_schema,
            description=cfg.description,
            table_count=len(tables),
        )

        # ── Upsert BusinessDomain nodes ────────────────────────────────────
        for domain in cfg.domains:
            await neo4j_service.upsert_domain(
                session, db_id=cfg.id,
                name=domain.name, hint=domain.hint,
            )

        # ── Process each table ─────────────────────────────────────────────
        total = len(tables)
        cols_done = 0
        for idx, (table_name, cols) in enumerate(tables.items(), 1):
            schema_owner    = cols[0].get("owner", cfg.qualified_schema)
            table_comment   = (cols[0].get("table_comment") or "").strip()
            is_view         = cols[0].get("is_view", False)
            row_count_approx= row_counts.get(table_name, 0)
            pk_columns      = pk_map.get(table_name, [])

            print(f"  [{idx:>3}/{total}] {table_name}", end="  ", flush=True)

            # ── Enrich column names via Gemini ─────────────────────────────
            inferred_domain = _infer_domain(table_name, cfg)
            domain_hint_str = next(
                (d.hint for d in cfg.domains if d.name == inferred_domain), ""
            ) if inferred_domain else ""
            try:
                enriched = enrich_columns(
                    table_name, table_comment, cols,
                    db_name=cfg.name,
                    domain_hint=domain_hint_str,
                )
                time.sleep(_RATE_DELAY_ENRICH)
            except Exception as e:
                print(f"⚠ enrichment failed ({e}), using raw names  ", end="")
                enriched = [
                    {
                        "column":      c["column_name"],
                        "label":       c["column_name"].replace("_", " ").title(),
                        "description": c.get("col_comment") or c["column_name"],
                        "is_pii":      False,
                    }
                    for c in cols
                ]

            enrichment_map = {e["column"]: e for e in enriched}

            # ── Table embedding ────────────────────────────────────────────
            enriched_labels = " ".join(
                e.get("label", "") for e in enriched if e.get("label")
            )
            table_edesc = _table_embed_text(table_name, table_comment, enriched_labels)
            table_emb   = get_embedding(table_edesc)
            time.sleep(_RATE_DELAY_EMBED)

            # ── Upsert Table node ──────────────────────────────────────────
            await neo4j_service.upsert_table(
                session,
                name=table_name,
                database_id=cfg.id,
                schema_name=schema_owner,
                description=f"Table {table_name} with {len(cols)} columns",
                enriched_description=table_edesc,
                embedding=table_emb,
                is_view=is_view,
                row_count_approx=row_count_approx,
                pk_columns=pk_columns,
            )

            # ── Upsert Column nodes ────────────────────────────────────────
            for col in cols:
                cname    = col["column_name"]
                enriched_col = enrichment_map.get(cname, {})
                label    = enriched_col.get("label", cname.replace("_", " ").title())
                desc     = enriched_col.get("description", col.get("col_comment") or cname)
                is_pii   = bool(enriched_col.get("is_pii", False))

                col_edesc = _col_embed_text(table_name, cname, col["data_type"], label, desc)
                col_emb   = get_embedding(col_edesc)
                time.sleep(_RATE_DELAY_EMBED)

                await neo4j_service.upsert_column(
                    session,
                    table_name=table_name,
                    database_id=cfg.id,
                    col_name=cname,
                    data_type=col["data_type"],
                    nullable=col.get("nullable", "Y"),
                    label=label,
                    enriched_description=col_edesc,
                    is_pii=is_pii,
                    is_pk=bool(col.get("is_pk", False)),
                    is_unique=bool(col.get("is_unique", False)),
                    is_indexed=bool(col.get("is_indexed", False)),
                    cardinality_hint=col.get("cardinality_hint", "unknown"),
                    embedding=col_emb,
                )
                cols_done += 1

            # ── Link table to domain ───────────────────────────────────────
            domain_name = _infer_domain(table_name, cfg)
            if domain_name:
                await neo4j_service.link_table_to_domain(
                    session, table_name, cfg.id, domain_name
                )

            print("✓")

        # ── Upsert indexes ─────────────────────────────────────────────────
        print(f"\n  🗂  Loading {len(idx_rows)} indexes …")
        idx_done = 0
        for idx in idx_rows:
            try:
                cols_list = [c.strip() for c in (idx.get("idx_cols") or "").split(",") if c.strip()]
                await neo4j_service.upsert_index(
                    session,
                    table_name=idx["table_name"],
                    database_id=cfg.id,
                    index_name=idx["index_name"],
                    columns=cols_list,
                    is_unique=(idx.get("uniqueness") == "UNIQUE"),
                    index_type=idx.get("index_type", "NORMAL"),
                )
                idx_done += 1
            except Exception as e:
                print(f"  ⚠ Index {idx.get('index_name')} skipped: {e}")
        print(f"  ✅ {idx_done} indexes loaded")

        # ── Upsert FK relationships ────────────────────────────────────────
        print(f"  🔗 Loading {len(fk_rows)} FK relationships …")
        fk_done = 0
        for fk in fk_rows:
            try:
                await neo4j_service.upsert_fk(
                    session,
                    from_table=fk["table_name"],
                    to_table=fk["ref_table"],
                    from_col=fk["column_name"],
                    to_col=fk["ref_column"],
                    database_id=cfg.id,
                )
                fk_done += 1
            except Exception as e:
                print(f"  ⚠ FK skipped ({fk['table_name']} → {fk['ref_table']}): {e}")
        print(f"  ✅ {fk_done} FK relationships loaded")

    return {
        "tables":  total,
        "columns": cols_done,
        "fks":     fk_done,
        "indexes": idx_done,
    }


# ── Main entry point ───────────────────────────────────────────────────────────

async def ingest_all(db_id_filter: str | None = None,
                      schema_override: str | None = None) -> None:
    if sys.platform == "win32":
        import io as _io
        sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    print("\n" + "="*56)
    print("  NL-SQL Schema Ingestion  (v2 — multi-DB)")
    print("="*56)

    # Select which DBs to process
    all_cfgs = db_manager.databases
    if db_id_filter:
        target_cfgs = [c for c in all_cfgs if c.id == db_id_filter]
        if not target_cfgs:
            print(f"❌ No database with id='{db_id_filter}'. "
                  f"Available: {[c.id for c in all_cfgs]}")
            sys.exit(1)
    else:
        target_cfgs = all_cfgs

    if not target_cfgs:
        print("❌ No databases configured. Add entries to databases.yaml.")
        sys.exit(1)

    # Connect Neo4j
    print("\n🔌 Connecting to Neo4j …")
    try:
        driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_username, settings.neo4j_password),
        )
        await driver.verify_connectivity()
        print("✅ Connected!\n")
    except Exception as e:
        print(f"❌ Neo4j connection failed: {e}")
        sys.exit(1)

    # Create vector indexes ONCE before ingesting any DB
    async with driver.session() as session:
        print("🗂  Creating / refreshing Neo4j vector indexes …")
        await neo4j_service.create_indexes(session)
        print("✅ Indexes ready\n")

    # Ingest each database
    total_stats: dict[str, int] = {"tables": 0, "columns": 0, "fks": 0, "indexes": 0}
    for cfg in target_cfgs:
        if not cfg.is_configured:
            print(f"⚠  Skipping '{cfg.id}' — credentials not set "
                  f"({cfg.env_prefix}_USER / _PASSWORD / _DSN missing in .env)")
            continue
        stats = await ingest_db(cfg, driver, schema_override=schema_override)
        for k in total_stats:
            total_stats[k] += stats.get(k, 0)

    # Upsert cross-DB links (requires both DB nodes to exist)
    cross_links = db_manager.cross_links
    if cross_links:
        print(f"\n🌐 Loading {len(cross_links)} cross-database links …")
        async with driver.session() as session:
            cl_done = 0
            for lk in cross_links:
                try:
                    await neo4j_service.upsert_cross_db_link(
                        session,
                        from_table=lk.from_table, from_db=lk.from_db, from_col=lk.from_col,
                        to_table=lk.to_table,   to_db=lk.to_db,   to_col=lk.to_col,
                        description=lk.description,
                    )
                    cl_done += 1
                    print(f"  ✓ {lk.from_db}.{lk.from_table} → {lk.to_db}.{lk.to_table}")
                except Exception as e:
                    print(f"  ⚠ Cross-link skipped: {e}")
        print(f"✅ {cl_done} cross-DB links loaded")

    await driver.close()

    print(f"\n{'='*56}")
    print(f"  Ingestion complete  ({len(target_cfgs)} database(s))")
    print(f"  Tables:  {total_stats['tables']}")
    print(f"  Columns: {total_stats['columns']}")
    print(f"  FKs:     {total_stats['fks']}")
    print(f"  Indexes: {total_stats['indexes']}")
    print(f"{'='*56}")
    print("\nNext steps:")
    print("  Terminal 1: uvicorn backend.main:app --reload")
    print("  Terminal 2: streamlit run frontend/app.py\n")


def main():
    parser = argparse.ArgumentParser(description="Ingest Oracle schemas into Neo4j")
    parser.add_argument("--db",     type=str, default=None,
                        help="Database id to ingest (default: all from databases.yaml)")
    parser.add_argument("--schema", type=str, default=None,
                        help="Override Oracle schema name for targeted ingestion")
    args = parser.parse_args()
    asyncio.run(ingest_all(db_id_filter=args.db, schema_override=args.schema))


if __name__ == "__main__":
    main()
