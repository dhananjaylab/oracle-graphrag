import oracledb

# Port 1522 (verified by lsnrctl) 
# Service xepdb1 (verified by lsnrctl)
DSN = "localhost:1522/xepdb1" 

print("🔌 Connecting to Local Oracle 21c XE...")

try:
    connection = oracledb.connect(
        user="system",
        password="oracle",
        dsn=DSN
    )
    print("✅ Connected successfully to XEPDB1!")
    
    # Optional: Check if the products table is there
    cursor = connection.cursor()
    cursor.execute("SELECT table_name FROM user_tables WHERE table_name = 'PRODUCTS'")
    cursor.execute("SELECT * FROM TABLE(fn_advanced_search('harry poter'))")
    for row in cursor.fetchall():
        print(f"Match Found: {row}")
    row = cursor.fetchone()
    if row:
        print("📦 Table 'PRODUCTS' found!")
    else:
        print("⚠️ 'PRODUCTS' table not found in this schema.")
        
except oracledb.Error as e:
    print(f"❌ Oracle Error: {e}")