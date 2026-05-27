"""
backend/config.py  (Phase 3C — cleaned)

Pydantic-Settings for Gemini, Neo4j, and MCP server URLs.
Oracle DB credentials are intentionally NOT here — db_manager.py
loads them dynamically from env vars via env_prefix in databases.yaml.
Removing the hardcoded fincore_* / riskdb_* fields that previously
caused startup failures when those env vars were absent.
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    gemini_api_key: str
    neo4j_uri:      str
    neo4j_username: str
    neo4j_password: str
    oracle_mcp_url: str = "http://localhost:8001"
    neo4j_mcp_url:  str = "http://localhost:8002"

    class Config:
        env_file = ".env"


settings = Settings()
