"""
backend/config.py

Pydantic-Settings config — reads from .env file.
Only Neo4j + Gemini credentials here.
Oracle DB credentials are loaded per-database by DBManager via databases.yaml.

Phase 3B: adds oracle_mcp_url and neo4j_mcp_url with localhost defaults
so MCP client URLs are environment-overridable without code changes.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Gemini (Google AI Studio) ──────────────────────────────────────────
    gemini_api_key: str

    # ── Neo4j ─────────────────────────────────────────────────────────────
    neo4j_uri:      str
    neo4j_username: str
    neo4j_password: str

    # ── Core Banking DB ───────────────────────────────────────────────────
    fincore_user: str
    fincore_password: str
    fincore_dsn: str

    # ── Risk Management DB ────────────────────────────────────────────────
    riskdb_user: str
    riskdb_password: str
    riskdb_dsn: str

    # ── Phase 3B: MCP server URLs ─────────────────────────────────────────
    # Override via env: ORACLE_MCP_URL=http://host:8001
    oracle_mcp_url: str = "http://localhost:8001"
    neo4j_mcp_url:  str = "http://localhost:8002"

        # ── Redis (optional — falls back to in-process cache if unset) ────────────────
    redis_url: str
    redis_password: str
    redis_max_connections: int
    redis_socket_timeout: float

    # ── MCP circuit breaker tuning ─────────────────────────────────────────────────
    mcp_breaker_failure_threshold: int = 5
    mcp_breaker_cooldown_s: int = 30

    # ── MCP connection pool sizing ─────────────────────────────────────────────────
    mcp_pool_min: int = 2
    mcp_pool_max: int = 8
    mcp_checkout_timeout_s: int = 10
    mcp_tool_timeout_s: int = 60

    # ── Oracle connection pool sizing (must be >= MCP_POOL_MAX) ───────────────────
    oracle_pool_min: int = 2
    oracle_pool_max: int = 8
    oracle_pool_increment: int = 1

    # ── Optional static bearer tokens for MCP inter-service auth ─────────────────
    oracle_mcp_token: str = ""
    neo4j_mcp_token: str = ""

    class Config:
        env_file = ".env"


settings = Settings()