from pydantic_settings import BaseSettings
class Settings(BaseSettings):
    gemini_api_key: str
    neo4j_uri: str
    neo4j_username: str
    neo4j_password: str
    fincore_user: str
    fincore_password: str
    fincore_dsn: str
    riskdb_user: str
    riskdb_password: str
    riskdb_dsn: str
    class Config:
        env_file = ".env"
settings = Settings()
