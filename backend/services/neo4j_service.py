"""
backend/services/neo4j_service.py
-----------------------------------
Handles all Neo4j interactions for GraphRAG:
  - Semantic vector search on enriched schema descriptions
  - Graph traversal to find FK join paths between tables
  - Table and column detail retrieval for SQL generation context
  - Schema summary for the UI explorer

Graph schema stored:
  (:Table {name, schema, description, enriched_description, embedding})
  (:Column {name, table_name, data_type, nullable, label,
            enriched_description, is_pii, embedding})
  (:Table)-[:HAS_COLUMN]->(:Column)
  (:Table)-[:FK_TO {from_col, to_col}]->(:Table)
"""

from neo4j import AsyncGraphDatabase
from backend.config import settings

_driver = None


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


# ── Query-time functions ──────────────────────────────────────────────────────

async def semantic_schema_search(
    query_embedding: list[float], top_k: int = 12
) -> dict:
    """
    Vector similarity search on both Table and Column nodes.
    Returns the most semantically relevant schema elements for a NL query.
    """
    driver = get_driver()
    async with driver.session() as session:
        # Search Table nodes
        table_result = await session.run(
            """
            CALL db.index.vector.queryNodes('table_embeddings', $k, $embedding)
            YIELD node, score
            RETURN node.name              AS table_name,
                   node.enriched_description AS description,
                   score
            ORDER BY score DESC
            """,
            k=top_k,
            embedding=query_embedding,
        )
        tables = await table_result.data()

        # Search Column nodes — surface tables that own relevant columns
        col_result = await session.run(
            """
            CALL db.index.vector.queryNodes('column_embeddings', $k, $embedding)
            YIELD node, score
            RETURN node.table_name        AS table_name,
                   node.name              AS column_name,
                   node.enriched_description AS description,
                   score
            ORDER BY score DESC
            """,
            k=top_k,
            embedding=query_embedding,
        )
        columns = await col_result.data()

    return {"tables": tables, "columns": columns}


async def get_table_details(table_names: list[str]) -> list[dict]:
    """
    Fetch full column details for a set of tables.
    This is the schema context sent to Gemini for SQL generation.
    """
    if not table_names:
        return []
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (t:Table)-[:HAS_COLUMN]->(c:Column)
            WHERE t.name IN $tables
            RETURN
                t.name                   AS table_name,
                t.schema                 AS schema_name,
                t.enriched_description   AS table_description,
                collect({
                    name:        c.name,
                    data_type:   c.data_type,
                    nullable:    c.nullable,
                    label:       c.label,
                    description: c.enriched_description,
                    is_pii:      c.is_pii
                }) AS columns
            ORDER BY t.name
            """,
            tables=table_names,
        )
        return await result.data()


async def get_join_path(table1: str, table2: str) -> list[dict]:
    """
    Find the shortest FK relationship path between two tables.
    Used to generate accurate JOIN conditions.
    """
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH path = shortestPath(
                (t1:Table {name: $t1})-[:FK_TO*1..5]-(t2:Table {name: $t2})
            )
            RETURN
                [n IN nodes(path) | n.name]          AS table_sequence,
                [r IN relationships(path) | {
                    from_col: r.from_col,
                    to_col:   r.to_col
                }]                                    AS join_conditions
            LIMIT 1
            """,
            t1=table1,
            t2=table2,
        )
        return await result.data()


async def get_schema_summary() -> dict:
    """Return table list with descriptions and column counts — for the UI explorer."""
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (t:Table)
            OPTIONAL MATCH (t)-[:HAS_COLUMN]->(c:Column)
            RETURN
                t.name                  AS name,
                t.enriched_description  AS description,
                count(c)                AS column_count
            ORDER BY t.name
            """
        )
        return {"tables": await result.data()}


# ── Ingestion-time functions (called from ingestion/ingest_schema.py) ─────────

async def create_indexes(session) -> None:
    """Create vector indexes for Table and Column nodes. Recreates them to ensure dimensions match."""
    # Drop old indexes if they exist to handle dimensionality changes
    await session.run("DROP INDEX table_embeddings IF EXISTS")
    await session.run("DROP INDEX column_embeddings IF EXISTS")
    
    await session.run(
        """
        CREATE VECTOR INDEX table_embeddings IF NOT EXISTS
        FOR (t:Table) ON (t.embedding)
        OPTIONS {indexConfig: {
            `vector.dimensions`: 3072,
            `vector.similarity_function`: 'cosine'
        }}
        """
    )
    await session.run(
        """
        CREATE VECTOR INDEX column_embeddings IF NOT EXISTS
        FOR (c:Column) ON (c.embedding)
        OPTIONS {indexConfig: {
            `vector.dimensions`: 3072,
            `vector.similarity_function`: 'cosine'
        }}
        """
    )
    # Uniqueness constraints
    await session.run(
        "CREATE CONSTRAINT table_name_unique IF NOT EXISTS "
        "FOR (t:Table) REQUIRE t.name IS UNIQUE"
    )


async def upsert_table(
    session,
    name: str,
    schema: str,
    description: str,
    enriched_description: str,
    embedding: list[float],
) -> None:
    await session.run(
        """
        MERGE (t:Table {name: $name})
        SET t.schema               = $schema,
            t.description          = $description,
            t.enriched_description = $enriched_description,
            t.embedding            = $embedding,
            t.updated_at           = datetime()
        """,
        name=name,
        schema=schema,
        description=description,
        enriched_description=enriched_description,
        embedding=embedding,
    )


async def upsert_column(
    session,
    table_name: str,
    col_name: str,
    data_type: str,
    nullable: str,
    label: str,
    enriched_description: str,
    is_pii: bool,
    embedding: list[float],
) -> None:
    await session.run(
        """
        MATCH (t:Table {name: $table_name})
        MERGE (c:Column {name: $col_name, table_name: $table_name})
        SET c.data_type           = $data_type,
            c.nullable            = $nullable,
            c.label               = $label,
            c.enriched_description = $enriched_description,
            c.is_pii              = $is_pii,
            c.embedding           = $embedding
        MERGE (t)-[:HAS_COLUMN]->(c)
        """,
        table_name=table_name,
        col_name=col_name,
        data_type=data_type,
        nullable=nullable,
        label=label,
        enriched_description=enriched_description,
        is_pii=is_pii,
        embedding=embedding,
    )


async def upsert_fk(
    session, from_table: str, to_table: str, from_col: str, to_col: str
) -> None:
    await session.run(
        """
        MATCH (t1:Table {name: $from_table})
        MATCH (t2:Table {name: $to_table})
        MERGE (t1)-[r:FK_TO {from_col: $from_col, to_col: $to_col}]->(t2)
        """,
        from_table=from_table,
        to_table=to_table,
        from_col=from_col,
        to_col=to_col,
    )
