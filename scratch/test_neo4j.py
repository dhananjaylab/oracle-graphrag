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
user = os.getenv("NEO4J_USERNAME")
password = os.getenv("NEO4J_PASSWORD")

print(f"Connecting to Neo4j at {uri} as {user}...")

# Test 1: neo4j+s
try:
    print("\n--- Test 1: neo4j+s ---")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()
    print("SUCCESS: Connected to Neo4j!")
    driver.close()
except Exception as e:
    print(f"FAILED: neo4j+s: {e}")

# Test 2: bolt+s
bolt_uri = uri.replace("neo4j+s://", "bolt+s://")
try:
    print(f"\n--- Test 2: bolt+s ({bolt_uri}) ---")
    driver = GraphDatabase.driver(bolt_uri, auth=(user, password))
    driver.verify_connectivity()
    print("SUCCESS: Connected to Neo4j with bolt+s!")
    driver.close()
except Exception as e:
    print(f"FAILED: bolt+s: {e}")
