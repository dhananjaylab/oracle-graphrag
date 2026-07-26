"""
Oracle Database Setup and Population Script - OPTIMIZED VERSION

Executes SQL files efficiently:
- PL/SQL files: Intelligent splitting for proper execution
- INSERT files: Execute as single script for better performance
"""

import os
import sys
import re
from pathlib import Path
from decouple import config
import oracledb

# Load environment variables

# Configuration
DB_DSN = config('ORACLE_DSN')
USERNAME = config('ORACLE_USER')
PASSWORD = config('ORACLE_PASSWORD')

# SQL files in current directory
SCRIPT_DIR = Path(__file__).parent
SQL_FILES = {
    "tables": SCRIPT_DIR / "script.sql",
    "functions": SCRIPT_DIR / "similarity_search.sql",
    "products": SCRIPT_DIR / "inserts_products_books.sql",
    "invoices": SCRIPT_DIR / "invoice_data_insert.sql"
}

class DatabaseSetup:
    """Handles Oracle database setup and population"""
    
    def __init__(self, dsn, user, password):
        self.dsn = dsn
        self.user = user
        self.password = password
        self.connection = None
        self.cursor = None
    
    def connect(self):
        """Connect to Oracle Database"""
        try:
            print("🔌 Connecting to Oracle Database...")
            oracledb.init_oracle_client(
                lib_dir=r"C:\oracle\instantclient_23_0"
            )

            self.connection = oracledb.connect(
                user=self.user,
                password=self.password,
                dsn=self.dsn
            )
            self.cursor = self.connection.cursor()
            print(f"✅ Connected to {self.dsn}")
            return True
        except Exception as e:
            print(f"❌ Connection failed: {e}")
            return False
    
    def disconnect(self):
        """Close database connection"""
        if self.cursor:
            self.cursor.close()
        if self.connection:
            self.connection.close()
        print("✅ Disconnected from database")
    
    def drop_existing_objects(self):
        """Drop existing database objects"""
        print("\n🗑️  Dropping Existing Objects (if any)")
        print("=" * 60)
        
        drop_statements = [
            "DROP TABLE ITEM_INVOICE CASCADE CONSTRAINTS",
            "DROP TABLE INVOICE CASCADE CONSTRAINTS",
            "DROP TABLE PRODUCTS CASCADE CONSTRAINTS",
            "DROP FUNCTION FN_ADVANCED_SEARCH",
            "DROP TYPE PRODUCT_RESULT_TAB FORCE",
            "DROP TYPE PRODUCT_RESULT FORCE"
        ]
        
        for statement in drop_statements:
            try:
                self.cursor.execute(statement)
                self.connection.commit()
                obj_name = statement.split()[2]
                print(f"   ✓ Dropped {obj_name}")
            except oracledb.Error as e:
                # Ignore errors if object doesn't exist
                if "does not exist" in str(e).lower() or "ORA-04043" in str(e) or "ORA-00942" in str(e):
                    obj_name = statement.split()[2]
                    print(f"   - {obj_name} (doesn't exist, skipped)")
                else:
                    print(f"   ⚠ Warning: {str(e)[:80]}...")
        
        print("✅ Cleanup completed")
        return True
    
    def read_sql_file(self, file_path):
        """Read SQL file and return content"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return content
        except FileNotFoundError:
            print(f"❌ File not found: {file_path}")
            return None
        except Exception as e:
            print(f"❌ Error reading file {file_path}: {e}")
            return None
    
    def execute_script_bulk(self, file_path, description):
        """
        Execute entire SQL file at once - splits internally but runs as fast as possible.
        No progress updates - just like running a script file in SQL*Plus or SQL Developer.
        """
        print(f"\n📋 {description}")
        print("=" * 60)
        
        sql_content = self.read_sql_file(file_path)
        if not sql_content:
            return False
        
        try:
            # Count statements for info only
            statement_count = sql_content.count(';')
            print(f"   📊 Executing SQL file with {statement_count:,} statements...")
            
            # Split and execute all at once - no progress updates, maximum speed
            statements = [s.strip() for s in sql_content.split(';') if s.strip() and not s.strip().startswith('--')]
            
            for stmt in statements:
                if stmt:
                    self.cursor.execute(stmt)
            
            # Single commit at the end
            self.connection.commit()
            
            print(f"   ✓ Successfully executed all {statement_count:,} statements")
            print(f"✅ {description} completed")
            return True
            
        except oracledb.Error as e:
            print(f"❌ Error executing file: {e}")
            return False
    
    def split_sql_statements(self, sql_content):
        """
        Split SQL content into executable statements.
        Handles:
        - Anonymous PL/SQL blocks (BEGIN...END/)
        - CREATE OR REPLACE blocks (CREATE.../)
        - Regular SQL (statements ending with ;)
        """
        statements = []
        current_statement = []
        in_plsql_block = False
        begin_count = 0
        
        lines = sql_content.split('\n')
        
        i = 0
        while i < len(lines):
            line = lines[i]
            line_stripped = line.strip()
            line_upper = line_stripped.upper()
            
            # Skip standalone comments and empty lines at the start
            if not current_statement and (not line_stripped or line_stripped.startswith('--')):
                i += 1
                continue
            
            # Detect start of PL/SQL block
            if not in_plsql_block:
                # Anonymous block
                if line_upper.startswith('BEGIN'):
                    in_plsql_block = True
                    begin_count = 1
                # Named block
                elif any(kw in line_upper for kw in [
                    'CREATE OR REPLACE FUNCTION',
                    'CREATE OR REPLACE PROCEDURE',
                    'CREATE OR REPLACE TYPE',
                    'CREATE FUNCTION',
                    'CREATE PROCEDURE',
                    'CREATE TYPE'
                ]):
                    in_plsql_block = True
                    begin_count = 0
            
            # Track BEGIN/END nesting in PL/SQL blocks
            if in_plsql_block:
                # Count BEGINs
                if re.search(r'\bBEGIN\b', line_upper):
                    begin_count += line_upper.count('BEGIN')
                # Count ENDs
                if re.search(r'\bEND\b', line_upper):
                    # Check if it's END; or END <n>;
                    if re.search(r'\bEND\s*;', line_upper) or re.search(r'\bEND\s+\w+\s*;', line_upper):
                        begin_count -= 1
            
            current_statement.append(line)
            
            # Check for end of statement
            if in_plsql_block:
                # End of PL/SQL block is marked by / on its own line
                if line_stripped == '/' or (line_stripped.endswith('/') and len(line_stripped) == 1):
                    stmt = '\n'.join(current_statement[:-1]).strip()  # Exclude the /
                    if stmt:
                        statements.append(stmt)
                    current_statement = []
                    in_plsql_block = False
                    begin_count = 0
                # Also check if BEGIN/END balanced and next line is /
                elif begin_count == 0 and i + 1 < len(lines) and lines[i + 1].strip() == '/':
                    current_statement.append(lines[i + 1])
                    stmt = '\n'.join(current_statement[:-1]).strip()  # Exclude the /
                    if stmt:
                        statements.append(stmt)
                    current_statement = []
                    in_plsql_block = False
                    i += 1  # Skip the / line
            else:
                # Regular SQL statement ends with ;
                if line_stripped.endswith(';'):
                    stmt = '\n'.join(current_statement).strip()
                    if stmt:
                        statements.append(stmt)
                    current_statement = []
            
            i += 1
        
        # Add any remaining statement
        if current_statement:
            stmt = '\n'.join(current_statement).strip()
            if stmt and stmt != '/':
                statements.append(stmt)
        
        return statements
    
    def execute_sql_file_split(self, file_path, description):
        """Execute SQL file by splitting into individual statements"""
        print(f"\n📋 {description}")
        print("=" * 60)
        
        sql_content = self.read_sql_file(file_path)
        if not sql_content:
            return False
        
        try:
            # Simple split for regular SQL (DDL/DML)
            statements = [
                stmt.strip() 
                for stmt in sql_content.split(';') 
                if stmt.strip() and not stmt.strip().startswith('--')
            ]
            
            total = len(statements)
            print(f"   📊 Executing {total:,} statements individually...")
            
            # Use progress indicators for large files
            show_progress = total > 50
            progress_interval = max(1, total // 20)  # Show 20 progress updates max
            
            for i, statement in enumerate(statements, 1):
                try:
                    statement = statement.strip()
                    if not statement:
                        continue
                    
                    self.cursor.execute(statement)
                    
                    # Show progress for large files
                    if show_progress and (i % progress_interval == 0 or i == total):
                        pct = (i / total) * 100
                        print(f"   ⏳ Progress: {i:,}/{total:,} ({pct:.1f}%)")
                    elif not show_progress:
                        preview = statement[:50].replace('\n', ' ')
                        print(f"   ✓ Statement {i}/{total}: {preview}...")
                    
                except oracledb.Error as e:
                    error_msg = str(e)
                    if "does not exist" in error_msg.lower() or "ORA-04043" in error_msg:
                        if not show_progress:
                            print(f"   ⚠ Statement {i}: Object doesn't exist (ignored)")
                    else:
                        print(f"   ❌ Statement {i} failed!")
                        print(f"   Error: {error_msg}")
                        print(f"   Statement: {statement[:200]}...")
                        raise
            
            self.connection.commit()
            print(f"✅ {description} completed ({total:,} statements)")
            return True
        
        except Exception as e:
            print(f"❌ Error: {e}")
            return False
    
    def execute_plsql_file(self, file_path, description):
        """Execute PL/SQL file with intelligent statement splitting"""
        print(f"\n📋 {description}")
        print("=" * 60)
        
        sql_content = self.read_sql_file(file_path)
        if not sql_content:
            return False
        
        try:
            statements = self.split_sql_statements(sql_content)
            
            for i, statement in enumerate(statements, 1):
                try:
                    statement = statement.strip()
                    if not statement:
                        continue
                    
                    preview = statement[:50].replace('\n', ' ')
                    print(f"   → Statement {i}/{len(statements)}: {preview}...")
                    
                    self.cursor.execute(statement)
                    self.connection.commit()
                    print(f"   ✓ Statement {i}/{len(statements)} executed")
                except oracledb.Error as e:
                    error_msg = str(e)
                    if "does not exist" in error_msg.lower() or "ORA-04043" in error_msg:
                        print(f"   ⚠ Statement {i}: Object doesn't exist (ignored)")
                    else:
                        print(f"   ❌ Statement {i} failed!")
                        print(f"   Error: {error_msg}")
                        print(f"   Statement: {statement[:300]}...")
                        raise
            
            print(f"✅ {description} completed")
            return True
        
        except Exception as e:
            print(f"❌ Error: {e}")
            return False
    
    def verify_tables(self):
        """Verify tables were created"""
        print("\n📊 Verifying Tables")
        print("=" * 60)
        
        tables = ["PRODUCTS", "INVOICE", "ITEM_INVOICE"]
        
        try:
            for table in tables:
                self.cursor.execute(
                    "SELECT COUNT(*) FROM user_tables WHERE table_name = :1",
                    [table]
                )
                result = self.cursor.fetchone()
                if result[0] > 0:
                    # Get row count
                    self.cursor.execute(f"SELECT COUNT(*) FROM {table}")
                    count = self.cursor.fetchone()[0]
                    print(f"   ✓ {table}: {count:,} rows")
                else:
                    print(f"   ✗ {table}: NOT FOUND")
            
            print("✅ Table verification completed")
            return True
        
        except Exception as e:
            print(f"❌ Verification failed: {e}")
            return False
    
    def verify_function(self):
        """Verify function was created"""
        print("\n🔧 Verifying Functions")
        print("=" * 60)
        
        try:
            self.cursor.execute(
                "SELECT COUNT(*) FROM user_objects WHERE object_type = 'FUNCTION' AND object_name = 'FN_ADVANCED_SEARCH'"
            )
            result = self.cursor.fetchone()
            if result[0] > 0:
                print("   ✓ fn_advanced_search: EXISTS")
                print("✅ Function verification completed")
                return True
            else:
                print("   ✗ fn_advanced_search: NOT FOUND")
                return False
        
        except Exception as e:
            print(f"❌ Verification failed: {e}")
            return False
    
    def test_search_function(self):
        """Test the search function"""
        print("\n🧪 Testing Search Function")
        print("=" * 60)
        
        try:
            # Test with a sample search
            self.cursor.execute("""
                SELECT * FROM TABLE(fn_advanced_search('harry potter'))
                ORDER BY similarity DESC
                FETCH FIRST 3 ROWS ONLY
            """)
            
            results = self.cursor.fetchall()
            if results:
                print(f"   ✓ Found {len(results)} results for 'harry potter':")
                for code, desc, score in results:
                    print(f"      - {code}: {desc[:50]}... (score: {score})")
                print("✅ Search function test passed")
                return True
            else:
                print("   ⚠ No results found (function working, but no data matches)")
                return True
        
        except Exception as e:
            print(f"❌ Test failed: {e}")
            return False
    
    def generate_summary(self):
        """Generate data summary report"""
        print("\n📈 Database Summary")
        print("=" * 60)
        
        try:
            # Products count
            self.cursor.execute("SELECT COUNT(*) FROM PRODUCTS")
            products_count = self.cursor.fetchone()[0]
            print(f"   📦 Products: {products_count:,}")
            
            # Invoices count
            self.cursor.execute("SELECT COUNT(*) FROM INVOICE")
            invoices_count = self.cursor.fetchone()[0]
            print(f"   📄 Invoices: {invoices_count:,}")
            
            # Invoice items count
            self.cursor.execute("SELECT COUNT(*) FROM ITEM_INVOICE")
            items_count = self.cursor.fetchone()[0]
            print(f"   🛍️  Invoice Items: {items_count:,}")
            
            # Total invoice value
            self.cursor.execute("SELECT SUM(VALUE_TOTAL) FROM INVOICE")
            total_value = self.cursor.fetchone()[0]
            if total_value:
                print(f"   💰 Total Invoice Value: ${total_value:,.2f}")
            
            # Average invoice value
            self.cursor.execute("SELECT AVG(VALUE_TOTAL) FROM INVOICE")
            avg_value = self.cursor.fetchone()[0]
            if avg_value:
                print(f"   📊 Average Invoice Value: ${avg_value:,.2f}")
            
            # States represented
            self.cursor.execute("SELECT COUNT(DISTINCT STATE) FROM INVOICE")
            states_count = self.cursor.fetchone()[0]
            print(f"   🗺️  States in Database: {states_count}")
            
            self.cursor.execute("SELECT DISTINCT STATE FROM INVOICE ORDER BY STATE")
            states = [row[0] for row in self.cursor.fetchall()]
            if states:
                print(f"      States: {', '.join(states)}")
            
            print("✅ Summary report completed")
            return True
        
        except Exception as e:
            print(f"❌ Summary failed: {e}")
            return False
    
    def run_full_setup(self):
        """Execute full setup workflow"""
        print("\n" + "="*60)
        print("🚀 ORACLE DATABASE SETUP AND POPULATION")
        print("="*60)
        print(f"Database: {self.dsn}")
        print(f"User: {self.user}")
        print("="*60 + "\n")
        
        # Step 1: Connect
        if not self.connect():
            return False
        
        # Step 2: Verify SQL files exist
        print("\n✓ Checking SQL files...")
        for name, path in SQL_FILES.items():
            if path.exists():
                print(f"   ✓ {name}: {path}")
            else:
                print(f"   ✗ {name}: NOT FOUND at {path}")
                self.disconnect()
                return False
        
        # Step 3: Drop existing objects
        if not self.drop_existing_objects():
            self.disconnect()
            return False
        
        # Step 4: Create tables (split into statements)
        if not self.execute_sql_file_split(SQL_FILES["tables"], "Creating Tables"):
            self.disconnect()
            return False
        
        # Step 5: Create search function (PL/SQL with intelligent splitting)
        if not self.execute_plsql_file(SQL_FILES["functions"], "Creating Search Function"):
            self.disconnect()
            return False
        
        # Step 6: Insert products (bulk execution for performance)
        if not self.execute_script_bulk(SQL_FILES["products"], "Inserting Products"):
            self.disconnect()
            return False
        
        # Step 7: Insert invoices (bulk execution for performance)
        if not self.execute_script_bulk(SQL_FILES["invoices"], "Inserting Invoices and Items"):
            self.disconnect()
            return False
        
        # Step 8: Verify tables
        if not self.verify_tables():
            self.disconnect()
            return False
        
        # Step 9: Verify function
        if not self.verify_function():
            self.disconnect()
            return False
        
        # Step 10: Test search function
        if not self.test_search_function():
            self.disconnect()
            return False
        
        # Step 11: Generate summary
        if not self.generate_summary():
            self.disconnect()
            return False
        
        # Cleanup
        self.disconnect()
        
        print("\n" + "="*60)
        print("✅ DATABASE SETUP COMPLETED SUCCESSFULLY!")
        print("="*60)
        print("\nNext steps:")
        print("  1. Run: python process_vector_products.py")
        print("     (to generate and store embeddings)")
        print("  2. Run: python main.py")
        print("     (to start the invoice resolution agent)")
        print("="*60 + "\n")
        
        return True


def main():
    """Main entry point"""
    # Pre-flight checks
    if not os.path.exists(".env"):
        print("❌ .env file not found!")
        print("\nCreate a .env file with:")
        print("  GEMINI_API_KEY=your_key_here")
        print("  ORACLE_DSN=localhost:1521/xe")
        print("  ORACLE_USER=admin")
        print("  ORACLE_PASSWORD=your_password")
        sys.exit(1)
    
    # Initialize setup
    setup = DatabaseSetup(DB_DSN, USERNAME, PASSWORD)
    
    # Run full setup
    success = setup.run_full_setup()
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()