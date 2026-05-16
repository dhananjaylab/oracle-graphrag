from neo4j import GraphDatabase
import os
import sys
from dotenv import load_dotenv

# Set encoding for Windows terminal
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

load_dotenv()

uri = os.getenv("NEO4J_URI")
# Try with 'neo4j' as username instead of the instance ID
user = "neo4j"
password = os.getenv("NEO4J_PASSWORD")

print(f"Connecting to Neo4j at {uri} as {user}...")

try:
    print("\n--- Test 1: neo4j+s with user 'neo4j' ---")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()
    print("SUCCESS: Connected to Neo4j!")
    driver.close()
except Exception as e:
    print(f"FAILED: {e}")
