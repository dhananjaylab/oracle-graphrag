import oracledb
import os
import sys
from dotenv import load_dotenv

# Set encoding for Windows terminal
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

load_dotenv()

user = os.getenv("ORACLE_USER")
password = os.getenv("ORACLE_PASSWORD")
dsn = os.getenv("ORACLE_DSN")

print(f"Connecting to {dsn} as {user}...")

# Test 1: Original DSN
try:
    print("\n--- Test 1: Original DSN ---")
    conn = oracledb.connect(user=user, password=password, dsn=dsn)
    print("SUCCESS: Original DSN worked!")
    conn.close()
except Exception as e:
    print(f"FAILED: Original DSN: {e}")

# Test 2: TCPS prefix
tcps_dsn = f"tcps://{dsn}" if not dsn.startswith("tcps://") else dsn
try:
    print(f"\n--- Test 2: TCPS DSN ({tcps_dsn}) ---")
    conn = oracledb.connect(user=user, password=password, dsn=tcps_dsn)
    print("SUCCESS: TCPS DSN worked!")
    conn.close()
except Exception as e:
    print(f"FAILED: TCPS DSN: {e}")

# Test 3: TCPS with dn match false
try:
    print("\n--- Test 3: TCPS with ssl_server_dn_match=False ---")
    conn = oracledb.connect(user=user, password=password, dsn=tcps_dsn, ssl_server_dn_match=False)
    print("SUCCESS: TCPS with dn match false worked!")
    conn.close()
except Exception as e:
    print(f"FAILED: TCPS with dn match false: {e}")

# Test 4: Custom DSN string with protocol
try:
    print("\n--- Test 4: Custom DSN string ---")
    # Example: (DESCRIPTION=(ADDRESS=(PROTOCOL=TCPS)(HOST=db.freesql.com)(PORT=2484))(CONNECT_DATA=(SERVICE_NAME=23ai_34ui2)))
    host, port_service = dsn.split(':')
    port, service = port_service.split('/')
    custom_dsn = f"(DESCRIPTION=(ADDRESS=(PROTOCOL=TCPS)(HOST={host})(PORT={port}))(CONNECT_DATA=(SERVICE_NAME={service})))"
    conn = oracledb.connect(user=user, password=password, dsn=custom_dsn, ssl_server_dn_match=False)
    print("SUCCESS: Custom DSN string worked!")
    conn.close()
except Exception as e:
    print(f"FAILED: Custom DSN string: {e}")
