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

    class Config:
        env_file = ".env"


settings = Settings()
