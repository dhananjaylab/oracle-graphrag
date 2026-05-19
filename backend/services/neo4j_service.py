"""
backend/services/neo4j_service.py  (v2 + Phase 3A feedback)

New in Phase 3A:
  increment_pattern_success()  — thumbs up → raise pattern weight
  decrement_pattern_success()  — thumbs down → lower pattern weight
  update_pattern_sql()         — user supplies corrected SQL → replace stored SQL

All other functions unchanged from v2.
"""

import uuid
from datetime import datetime, timezone

from neo4j import AsyncGraphDatabase

from backend.config import settings

_driver      = None
EMBED_DIMS   = 3072   # gemini-embedding-001


def get_driver():
    global _driver
    if _driver is None:
        _driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_username, settings.neo4j_password),
        )
    return _driver


async def close_driver():
    global _driver
    if _driver:
        await _driver.close()
        _driver = None


# ══════════════════════════════════════════════════════════════════════════════
# QUERY-TIME — pattern retrieval
# ══════════════════════════════════════════════════════════════════════════════

async def search_similar_patterns(
    query_embedding: list[float],
    database_id:     str,
    top_k:           int   = 3,
    min_similarity:  float = 0.85,
) -> list[dict]:
    cypher = """
        CALL db.index.vector.queryNodes('pattern_embeddings', $k, $embedding)
        YIELD node, score
        WHERE node.database_id = $db_id AND score >= $min_sim
        RETURN node.nl_question   AS nl_question,
               node.sql           AS sql,
               node.schema_cypher AS schema_cypher,
               node.tables_used   AS tables_used,
               node.success_count AS success_count,
               score
        ORDER BY score DESC
        LIMIT $k
    """
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            cypher, k=top_k, embedding=query_embedding,
            db_id=database_id, min_sim=min_similarity,
        )
        return await result.data()


# ══════════════════════════════════════════════════════════════════════════════
# QUERY-TIME — schema discovery
# ══════════════════════════════════════════════════════════════════════════════

async def semantic_schema_search(
    query_embedding: list[float],
    database_id:     str,
    top_k:           int = 12,
) -> dict:
    table_cypher = """CALL db.index.vector.queryNodes('table_embeddings', $k, $embedding)
YIELD node, score
WHERE node.database_id = $db_id
RETURN node.name AS table_name, node.enriched_description AS description,
       node.is_view AS is_view, node.row_count_approx AS row_count_approx, score
ORDER BY score DESC"""

    col_cypher = """CALL db.index.vector.queryNodes('column_embeddings', $k, $embedding)
YIELD node, score
WHERE node.database_id = $db_id
RETURN node.table_name AS table_name, node.name AS column_name,
       node.enriched_description AS description, node.is_pk AS is_pk,
       node.cardinality_hint AS cardinality_hint, score
ORDER BY score DESC"""

    driver = get_driver()
    async with driver.session() as session:
        t_res   = await session.run(table_cypher, k=top_k, embedding=query_embedding, db_id=database_id)
        tables  = await t_res.data()
        c_res   = await session.run(col_cypher,   k=top_k, embedding=query_embedding, db_id=database_id)
        columns = await c_res.data()

    return {
        "tables":      tables,
        "columns":     columns,
        "cypher_used": f"-- Table search:\n{table_cypher}\n\n-- Column search:\n{col_cypher}",
    }


async def get_table_details(table_names: list[str], database_id: str) -> list[dict]:
    if not table_names:
        return []
    cypher = """
        MATCH (t:Table)-[:HAS_COLUMN]->(c:Column)
        WHERE t.name IN $tables AND t.database_id = $db_id
        OPTIONAL MATCH (t)-[:IN_DOMAIN]->(d:BusinessDomain)
        RETURN t.name                 AS table_name,
               t.schema_name          AS schema_name,
               t.enriched_description AS table_description,
               t.is_view              AS is_view,
               t.row_count_approx     AS row_count_approx,
               t.pk_columns           AS pk_columns,
               d.name                 AS domain_name,
               collect({
                   name:             c.name,
                   data_type:        c.data_type,
                   nullable:         c.nullable,
                   label:            c.label,
                   description:      c.enriched_description,
                   is_pii:           c.is_pii,
                   is_pk:            c.is_pk,
                   is_unique:        c.is_unique,
                   is_indexed:       c.is_indexed,
                   cardinality_hint: c.cardinality_hint
               }) AS columns
        ORDER BY t.name
    """
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(cypher, tables=table_names, db_id=database_id)
        return await result.data()


async def get_join_path(table1: str, table2: str, database_id: str) -> list[dict]:
    cypher = """
        MATCH path = shortestPath(
            (t1:Table {name: $t1, database_id: $db_id})
            -[:FK_TO*1..5]-
            (t2:Table {name: $t2, database_id: $db_id})
        )
        RETURN [n IN nodes(path) | n.name] AS table_sequence,
               [r IN relationships(path) | {from_col: r.from_col, to_col: r.to_col}]
               AS join_conditions
        LIMIT 1
    """
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(cypher, t1=table1, t2=table2, db_id=database_id)
        return await result.data()


async def get_cross_db_hints(table_names: list[str], database_id: str) -> list[dict]:
    cypher = """
        MATCH (t1:Table)-[r:CROSS_DB_JOIN]->(t2:Table)
        WHERE t1.name IN $tables AND t1.database_id = $db_id
        RETURN t1.name AS from_table, t1.database_id AS from_db, r.from_col AS from_col,
               t2.name AS to_table,  t2.database_id AS to_db,   r.to_col   AS to_col,
               r.description AS description
    """
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(cypher, tables=table_names, db_id=database_id)
        return await result.data()


async def get_schema_summary() -> dict:
    cypher = """
        MATCH (db:Database)
        OPTIONAL MATCH (t:Table {database_id: db.id})
        OPTIONAL MATCH (t)-[:IN_DOMAIN]->(dom:BusinessDomain)
        WITH db,
            collect(DISTINCT {
                name:        t.name,
                description: t.enriched_description,
                is_view:     t.is_view,
                row_count:   t.row_count_approx,
                domain:      dom.name
            }) AS tables,
            collect(DISTINCT {name: dom.name, hint: dom.hint}) AS domains
        RETURN db.id AS id, db.name AS name, db.description AS description,
               size(tables) AS table_count, tables, domains
        ORDER BY db.name
    """
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(cypher)
        return {"databases": await result.data()}


# ══════════════════════════════════════════════════════════════════════════════
# QUERY PATTERN STORAGE — preserve SQL + Cypher after successful execution
# ══════════════════════════════════════════════════════════════════════════════

async def store_query_pattern(
    database_id:  str,
    nl_question:  str,
    sql:          str,
    schema_cypher: str,
    tables_used:  list[str],
    execution_ms: int,
    embedding:    list[float],
) -> None:
    now    = datetime.now(timezone.utc).isoformat()
    cypher = """
        MERGE (qp:QueryPattern {nl_question: $nl, database_id: $db_id})
        ON CREATE SET
            qp.id               = $pid,
            qp.sql              = $sql,
            qp.schema_cypher    = $schema_cypher,
            qp.tables_used      = $tables,
            qp.success_count    = 1,
            qp.avg_execution_ms = $exec_ms,
            qp.embedding        = $embedding,
            qp.created_at       = $now,
            qp.last_used        = $now
        ON MATCH SET
            qp.sql              = $sql,
            qp.schema_cypher    = $schema_cypher,
            qp.tables_used      = $tables,
            qp.success_count    = qp.success_count + 1,
            qp.avg_execution_ms = (qp.avg_execution_ms * qp.success_count + $exec_ms)
                                  / (qp.success_count + 1),
            qp.last_used        = $now
        WITH qp
        MATCH (db:Database {id: $db_id})
        MERGE (qp)-[:FOR_DB]->(db)
        WITH qp
        UNWIND $tables AS tname
            MATCH (t:Table {name: tname, database_id: $db_id})
            MERGE (qp)-[:QUERIES]->(t)
    """
    driver = get_driver()
    async with driver.session() as session:
        await session.run(
            cypher,
            nl=nl_question, db_id=database_id, pid=str(uuid.uuid4()),
            sql=sql, schema_cypher=schema_cypher, tables=tables_used,
            exec_ms=execution_ms, embedding=embedding, now=now,
        )


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3A — FEEDBACK FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def increment_pattern_success(nl_question: str, database_id: str) -> bool:
    """
    Thumbs-up feedback: raise the pattern's success_count by 1.
    Higher success_count → pattern ranks higher in future similarity searches.
    Returns True if the pattern was found and updated.
    """
    now    = datetime.now(timezone.utc).isoformat()
    cypher = """
        MATCH (qp:QueryPattern {nl_question: $nl, database_id: $db_id})
        SET qp.success_count = qp.success_count + 1,
            qp.last_used     = $now
        RETURN count(qp) AS updated
    """
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(cypher, nl=nl_question, db_id=database_id, now=now)
        data   = await result.single()
        return bool(data and data["updated"] > 0)


async def decrement_pattern_success(nl_question: str, database_id: str) -> bool:
    """
    Thumbs-down feedback: lower the pattern's success_count.
    Count never goes below 0 — pattern is not deleted, just de-weighted.
    Returns True if the pattern was found and updated.
    """
    now    = datetime.now(timezone.utc).isoformat()
    cypher = """
        MATCH (qp:QueryPattern {nl_question: $nl, database_id: $db_id})
        SET qp.success_count = CASE
                WHEN qp.success_count > 1 THEN qp.success_count - 1
                ELSE 0
            END,
            qp.last_used = $now
        RETURN count(qp) AS updated
    """
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(cypher, nl=nl_question, db_id=database_id, now=now)
        data   = await result.single()
        return bool(data and data["updated"] > 0)


async def update_pattern_sql(
    nl_question:   str,
    database_id:   str,
    corrected_sql: str,
) -> bool:
    """
    User-supplied corrected SQL: replace the stored SQL in the QueryPattern.
    Also bumps success_count so the corrected version ranks higher.
    Returns True if the pattern was found and updated.
    """
    now    = datetime.now(timezone.utc).isoformat()
    cypher = """
        MATCH (qp:QueryPattern {nl_question: $nl, database_id: $db_id})
        SET qp.sql           = $corrected_sql,
            qp.success_count = qp.success_count + 2,   /* reward user-corrected patterns */
            qp.last_used     = $now
        RETURN count(qp) AS updated
    """
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            cypher, nl=nl_question, db_id=database_id,
            corrected_sql=corrected_sql, now=now,
        )
        data = await result.single()
        return bool(data and data["updated"] > 0)


# ══════════════════════════════════════════════════════════════════════════════
# INGESTION-TIME FUNCTIONS (unchanged from v2)
# ══════════════════════════════════════════════════════════════════════════════

async def create_indexes(session) -> None:
    for idx in ("table_embeddings", "column_embeddings", "pattern_embeddings"):
        await session.run(f"DROP INDEX {idx} IF EXISTS")
    for label, idx in [
        ("Table",        "table_embeddings"),
        ("Column",       "column_embeddings"),
        ("QueryPattern", "pattern_embeddings"),
    ]:
        await session.run(f"""
            CREATE VECTOR INDEX {idx} IF NOT EXISTS
            FOR (n:{label}) ON (n.embedding)
            OPTIONS {{indexConfig: {{
                `vector.dimensions`: {EMBED_DIMS},
                `vector.similarity_function`: 'cosine'
            }}}}
        """)
    await session.run(
        "CREATE CONSTRAINT table_db_unique IF NOT EXISTS "
        "FOR (t:Table) REQUIRE (t.name, t.database_id) IS UNIQUE"
    )
    await session.run(
        "CREATE CONSTRAINT db_id_unique IF NOT EXISTS "
        "FOR (db:Database) REQUIRE db.id IS UNIQUE"
    )


async def upsert_database(session, db_id, name, schema, description, table_count):
    await session.run("""
        MERGE (db:Database {id: $id})
        SET db.name=$name, db.schema=$schema, db.description=$description,
            db.table_count=$table_count, db.last_ingested=datetime()
    """, id=db_id, name=name, schema=schema, description=description, table_count=table_count)


async def upsert_domain(session, db_id, name, hint):
    await session.run("""
        MERGE (d:BusinessDomain {name: $name, database_id: $db_id})
        SET d.hint=$hint
        WITH d MATCH (db:Database {id: $db_id}) MERGE (d)-[:BELONGS_TO]->(db)
    """, name=name, db_id=db_id, hint=hint)


async def upsert_table(session, name, database_id, schema_name, description,
                        enriched_description, embedding, is_view, row_count_approx, pk_columns):
    await session.run("""
        MERGE (t:Table {name: $name, database_id: $db_id})
        SET t.schema_name=$schema_name, t.description=$desc,
            t.enriched_description=$edesc, t.embedding=$emb,
            t.is_view=$is_view, t.row_count_approx=$rc,
            t.pk_columns=$pk, t.updated_at=datetime()
        WITH t MATCH (db:Database {id: $db_id}) MERGE (db)-[:HAS_TABLE]->(t)
    """, name=name, db_id=database_id, schema_name=schema_name, desc=description,
         edesc=enriched_description, emb=embedding, is_view=is_view,
         rc=row_count_approx, pk=pk_columns)


async def upsert_column(session, table_name, database_id, col_name, data_type, nullable,
                         label, enriched_description, is_pii, is_pk, is_unique,
                         is_indexed, cardinality_hint, embedding):
    await session.run("""
        MATCH (t:Table {name: $tname, database_id: $db_id})
        MERGE (c:Column {name: $cname, table_name: $tname, database_id: $db_id})
        SET c.data_type=$dtype, c.nullable=$nullable, c.label=$label,
            c.enriched_description=$edesc, c.is_pii=$is_pii, c.is_pk=$is_pk,
            c.is_unique=$is_unique, c.is_indexed=$is_indexed,
            c.cardinality_hint=$cardinality_hint, c.embedding=$emb
        MERGE (t)-[:HAS_COLUMN]->(c)
    """, tname=table_name, db_id=database_id, cname=col_name, dtype=data_type,
         nullable=nullable, label=label, edesc=enriched_description, is_pii=is_pii,
         is_pk=is_pk, is_unique=is_unique, is_indexed=is_indexed,
         cardinality_hint=cardinality_hint, emb=embedding)


async def upsert_index(session, table_name, database_id, index_name, columns, is_unique, index_type):
    await session.run("""
        MATCH (t:Table {name: $tname, database_id: $db_id})
        MERGE (idx:Index {name: $iname, database_id: $db_id})
        SET idx.table_name=$tname, idx.columns=$cols,
            idx.is_unique=$is_unique, idx.index_type=$itype
        MERGE (t)-[:HAS_INDEX]->(idx)
    """, tname=table_name, db_id=database_id, iname=index_name,
         cols=columns, is_unique=is_unique, itype=index_type)


async def upsert_fk(session, from_table, to_table, from_col, to_col, database_id):
    await session.run("""
        MATCH (t1:Table {name: $ft, database_id: $db_id})
        MATCH (t2:Table {name: $tt, database_id: $db_id})
        MERGE (t1)-[r:FK_TO {from_col: $fc, to_col: $tc}]->(t2)
    """, ft=from_table, tt=to_table, fc=from_col, tc=to_col, db_id=database_id)


async def link_table_to_domain(session, table_name, database_id, domain_name):
    await session.run("""
        MATCH (t:Table {name: $tname, database_id: $db_id})
        MATCH (d:BusinessDomain {name: $dname, database_id: $db_id})
        MERGE (t)-[:IN_DOMAIN]->(d)
    """, tname=table_name, db_id=database_id, dname=domain_name)


async def upsert_cross_db_link(session, from_table, from_db, from_col,
                                to_table, to_db, to_col, description):
    await session.run("""
        MATCH (t1:Table {name: $ft, database_id: $fdb})
        MATCH (t2:Table {name: $tt, database_id: $tdb})
        MERGE (t1)-[r:CROSS_DB_JOIN {from_col: $fc, to_col: $tc}]->(t2)
        SET r.description=$desc
    """, ft=from_table, fdb=from_db, fc=from_col,
         tt=to_table, tdb=to_db, tc=to_col, desc=description)
