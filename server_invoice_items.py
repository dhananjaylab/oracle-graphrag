# -*- coding: utf-8 -*-
import os
import sys
import oracledb
from mcp.server.fastmcp import FastMCP
from product_search import SearchSimilarProduct
from decouple import config
from contextlib import contextmanager


# === DB CONFIGURATION ===
DB_DSN = config('ORACLE_DSN')
USERNAME = config('ORACLE_USER')
PASSWORD = config('ORACLE_PASSWORD')

@contextmanager
def get_db_connection():
    conn = None
    try:
        conn = oracledb.connect(user=USERNAME, password=PASSWORD, dsn=DB_DSN)
        with conn.cursor() as cursor:
            # FIX: Use standard execute() without keyword arguments
            cursor.execute("ALTER SESSION SET CURRENT_SCHEMA = BOOKSTORE")
        yield conn
    except Exception as e:
        print(f"❌ Database error in context manager: {e}", file=sys.stderr)
        raise
    finally:
        if conn:
            conn.close()

# === INITIALIZATION ===
mcp = FastMCP("InvoiceItemResolver")

try:
    # Initialize Searcher (triggers the fixed __init__ in product_search.py)
    searcher = SearchSimilarProduct()
    print("✅ Product search service initialized", file=sys.stderr)
except Exception as e:
    print(f"⚠️  Product search service failed: {e}", file=sys.stderr)
    searcher = None

# === HELPER FUNCTIONS ===
def execute_query(query: str, params: dict = None):
    if params is None: params = {}
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(query, params)
                return cursor.fetchall()
    except Exception as e:
        print(f"❌ Query execution failed: {e}", file=sys.stderr)
        return []

# === MCP TOOLS ===
@mcp.tool()
def get_system_status() -> dict:
    """Returns the status of the Oracle connection and row counts."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT (SELECT COUNT(*) FROM products), (SELECT COUNT(*) FROM invoice) FROM DUAL")
                res = cursor.fetchone()
                return {"status": "online", "products": res[0], "invoices": res[1]}
    except Exception as e:
        return {"status": "offline", "error": str(e)}

@mcp.tool()
def search_vectorized_product(description: str) -> dict:
    """Searches for a product using semantic embeddings."""
    if searcher is None:
        return {"error": "Product search service not available"}
    try:
        return searcher.search_similar_products(description)
    except Exception as e:
        return {"error": f"Search failed: {str(e)}"}

@mcp.tool()
def resolve_ean(description: str) -> dict:
    """Resolves EAN via advanced scoring."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                query = "SELECT code, description, similarity FROM TABLE(fn_advanced_search(:1)) ORDER BY similarity DESC"
                cursor.execute(query, [description])
                row = cursor.fetchone()
                if row:
                    return {"code": row[0], "description": row[1], "similarity": row[2]}
                return {"error": "No matching EAN found"}
    except Exception as e:
        return {"error": f"EAN resolution failed: {str(e)}"}

@mcp.tool()
def search_invoices_by_criteria(customer: str = None, state: str = None, price: float = None, ean: str = None, margin: float = 0.05) -> list:
    """Searches for A/R invoices based on multiple criteria."""
    try:
        query = """
            SELECT nf.no_invoice, nf.name_customer, nf.state, nf.date_print,
                   inf.no_item, inf.code_ean, inf.description_product, inf.value_unitary
            FROM invoice nf
            JOIN item_invoice inf ON nf.no_invoice = inf.no_invoice
            WHERE 1=1
        """
        params = {}
        if customer:
            query += " AND LOWER(nf.name_customer) LIKE LOWER(:customer)"
            params["customer"] = f"%{customer}%"
        if state:
            query += " AND LOWER(nf.state) = LOWER(:state)"
            params["state"] = state
        if ean:
            query += " AND inf.code_ean = :ean"
            params["ean"] = ean
        if price is not None and price > 0:
            query += " AND inf.value_unitary BETWEEN :price_min AND :price_max"
            params["price_min"] = price * (1 - margin)
            params["price_max"] = price * (1 + margin)

        results = execute_query(query, params)
        cols = ["no_invoice", "name_customer", "state", "date_print", "no_item", "code_ean", "description_product", "value_unitary"]
        return [dict(zip(cols, row)) for row in results]
    except Exception as e:
        return [{"error": f"Invoice search failed: {str(e)}"}]

if __name__ == "__main__":
    try:
        print(f"🔌 MCP Server starting at {DB_DSN}...", file=sys.stderr)
        mcp.run(transport="stdio")
    except (KeyboardInterrupt, EOFError):
        # EOFError happens when the pipe is closed by the client
        print("\n🛑 MCP Server received shutdown signal.", file=sys.stderr)
    finally:
        # CLEANUP: Ensure any persistent searcher connections are closed
        if 'searcher' in locals() and searcher and hasattr(searcher, 'conn'):
            try:
                searcher.conn.close()
                print("📦 Oracle connection closed safely.", file=sys.stderr)
            except:
                pass
        print("👋 MCP Server offline.", file=sys.stderr)