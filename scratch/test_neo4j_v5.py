from neo4j import GraphDatabase
import os
import sys
from dotenv import load_dotenv

# Set encoding for Windows terminal
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

load_dotenv()

uri = os.getenv("NEO4J_URI").replace("neo4j+s://", "neo4j+ssc://")
user = os.getenv("NEO4J_USERNAME")
password = os.getenv("NEO4J_PASSWORD")

print(f"Connecting to Neo4j at {uri} as {user}...")

try:
    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()
    print("SUCCESS: Connected to Neo4j with neo4j+ssc!")
    driver.close()
except Exception as e:
    print(f"FAILED: {e}")
