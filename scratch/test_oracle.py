"""
scratch/test_oracle.py
Test Oracle connectivity for every database registered in databases.yaml.

Usage:
    cd nlsql
    python scratch/test_oracle.py
"""
import os, sys
from dotenv import load_dotenv
load_dotenv()

import oracledb
import yaml


def main():
    if not os.path.exists("databases.yaml"):
        print("❌ databases.yaml not found. Run from project root.")
        sys.exit(1)

    with open("databases.yaml") as f:
        data = yaml.safe_load(f)

    dbs = data.get("databases", [])
    print(f"\nTesting {len(dbs)} Oracle connection(s)...\n")

    for db in dbs:
        prefix = db["env_prefix"]
        user   = os.getenv(f"{prefix}_USER", "")
        pwd    = os.getenv(f"{prefix}_PASSWORD", "")
        dsn    = os.getenv(f"{prefix}_DSN", "")

        print(f"── {db['name']} ({db['id']}) ──")
        print(f"   DSN: {dsn}   User: {user or '(not set)'}")

        if not user or not dsn:
            print(f"   ⚠  Missing {prefix}_USER or {prefix}_DSN in .env — skipped\n")
            continue

        # Attempt 1: default thin mode
        try:
            conn = oracledb.connect(user=user, password=pwd, dsn=dsn)
            print(f"   ✅ Connected — Oracle {conn.version}\n")
            conn.close()
            continue
        except Exception as e:
            print(f"   ⚠  Default connect failed: {e}")

        # Attempt 2: TCPS
        tcps = f"tcps://{dsn}" if not dsn.startswith("tcps://") else dsn
        try:
            conn = oracledb.connect(user=user, password=pwd, dsn=tcps,
                                    ssl_server_dn_match=False)
            print(f"   ✅ Connected via TCPS\n")
            conn.close()
        except Exception as e2:
            print(f"   ❌ TCPS also failed: {e2}\n")


if __name__ == "__main__":
    main()
