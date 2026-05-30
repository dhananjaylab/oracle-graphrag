"""
backend/mcp_client/__init__.py

Module-level singletons for both MCP clients.
URLs come from settings so they are environment-overridable.

Connected during FastAPI lifespan startup, disconnected on shutdown.

Import from here in routes and services:
    from backend.mcp_client import oracle_mcp, neo4j_mcp
"""

from backend.config import settings
from backend.mcp_client.oracle_client import OracleMCPClient
from backend.mcp_client.neo4j_client  import Neo4jMCPClient

oracle_mcp = OracleMCPClient(server_url=settings.oracle_mcp_url)
neo4j_mcp  = Neo4jMCPClient(server_url=settings.neo4j_mcp_url)

def mcp_pool_stats() -> dict:
    return {
        "oracle": oracle_mcp.pool_stats,
        "neo4j": neo4j_mcp.pool_stats,
    }

__all__ = ["oracle_mcp", "neo4j_mcp", "mcp_pool_stats"]
