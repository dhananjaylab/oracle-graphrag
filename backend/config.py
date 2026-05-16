from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Gemini
    gemini_api_key: str

    # Oracle DB
    oracle_user: str
    oracle_password: str
    oracle_dsn: str
    oracle_schema: str = ""          # optional schema filter

    # Neo4j
    neo4j_uri: str
    neo4j_username: str
    neo4j_password: str

    class Config:
        env_file = ".env"


settings = Settings()
