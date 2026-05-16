from neo4j import GraphDatabase, TRUST_ALL_CERTIFICATES
import os
import sys
from dotenv import load_dotenv

# Set encoding for Windows terminal
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

load_dotenv()

uri = os.getenv("NEO4J_URI").replace("neo4j+s://", "neo4j://")
user = "neo4j"
password = os.getenv("NEO4J_PASSWORD")

print(f"Connecting to Neo4j at {uri} as {user} with TRUST_ALL_CERTIFICATES...")

try:
    # Use TRUST_ALL_CERTIFICATES to bypass SSL verification
    driver = GraphDatabase.driver(uri, auth=(user, password), encrypted=True, trust=TRUST_ALL_CERTIFICATES)
    driver.verify_connectivity()
    print("SUCCESS: Connected to Neo4j!")
    driver.close()
except Exception as e:
    print(f"FAILED: {e}")
