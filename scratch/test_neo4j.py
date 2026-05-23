"""
scratch/test_neo4j.py
Test Neo4j connectivity and verify vector index state.

Usage:
    cd nlsql
    python scratch/test_neo4j.py
"""
import os, asyncio, sys
from dotenv import load_dotenv
load_dotenv()

from neo4j import AsyncGraphDatabase

URI  = os.getenv("NEO4J_URI",      "bolt://localhost:7687")
USER = os.getenv("NEO4J_USERNAME", "neo4j")
PWD  = os.getenv("NEO4J_PASSWORD", "")

EXPECTED = ["table_embeddings", "column_embeddings", "pattern_embeddings"]


async def main():
    print(f"\nConnecting to Neo4j at {URI} as {USER}...\n")

    uris = [URI]
    if URI.startswith("neo4j+s://"):
        uris.append(URI.replace("neo4j+s://", "bolt+s://"))
    if URI.startswith("bolt://"):
        uris.append(URI.replace("bolt://", "neo4j://"))

    driver = None
    for uri in uris:
        try:
            d = AsyncGraphDatabase.driver(uri, auth=(USER, PWD))
            await d.verify_connectivity()
            driver = d
            print(f"✅ Connected via: {uri}\n")
            break
        except Exception as e:
            print(f"   ⚠  {uri}: {e}")

    if not driver:
        print("❌ Could not connect. Check NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD.")
        return

    async with driver.session() as session:
        # Node counts
        for label in ("Database","Table","Column","Index","BusinessDomain","QueryPattern"):
            res = await session.run(f"MATCH (n:{label}) RETURN count(n) AS c")
            row = await res.single()
            print(f"  {label:20s}: {(row['c'] if row else 0):>6,} nodes")

        print()

        # Vector indexes
        res     = await session.run("""
            SHOW INDEXES YIELD name, type, state, labelsOrTypes, properties
            WHERE type = 'VECTOR'
            RETURN name, state, labelsOrTypes, properties
        """)
        indexes = await res.data()
        if not indexes:
            print("⚠  No vector indexes found. Run: python -m ingestion.ingest_schema")
        else:
            found = set()
            for idx in indexes:
                tick = "✅" if idx["state"] == "ONLINE" else "⚠ "
                print(f"  {tick} {idx['name']:30s} [{idx['state']}]")
                found.add(idx["name"])
            missing = [n for n in EXPECTED if n not in found]
            if missing:
                print(f"\n⚠  Missing: {missing}")
                print("   Run: python -m ingestion.ingest_schema")
            else:
                print("\n✅ All expected vector indexes present")

    await driver.close()


if __name__ == "__main__":
    asyncio.run(main())
