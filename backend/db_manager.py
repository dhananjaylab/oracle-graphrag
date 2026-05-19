"""
backend/db_manager.py
----------------------
Singleton that owns all database connections.

Loads structural config from databases.yaml (non-sensitive).
Resolves credentials from environment variables via env_prefix convention.

  databases.yaml entry:  { id: fincore, env_prefix: FINCORE, ... }
  .env variables:        FINCORE_USER, FINCORE_PASSWORD, FINCORE_DSN

One oracledb connection pool is created lazily per database on first use.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

# Ensure environment variables are loaded before config parsing
load_dotenv()

import yaml
import oracledb


@dataclass
class DomainConfig:
    name: str
    hint: str = ""


@dataclass
class CrossDBLink:
    from_db: str
    from_table: str
    from_col: str
    to_db: str
    to_table: str
    to_col: str
    description: str = ""


@dataclass
class DBConfig:
    id: str
    name: str
    env_prefix: str
    schema: str
    description: str
    domains: list[DomainConfig] = field(default_factory=list)
    # resolved from env at load time
    user: str = ""
    password: str = ""
    dsn: str = ""

    @property
    def qualified_schema(self) -> str:
        return self.schema.upper()

    @property
    def is_configured(self) -> bool:
        return bool(self.user and self.password and self.dsn)


class DBManager:
    """Singleton database manager — one instance for the entire application."""

    _instance: "DBManager | None" = None

    def __new__(cls) -> "DBManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._ready = False
        return cls._instance

    def __init__(self) -> None:
        if self._ready:
            return
        self._configs: dict[str, DBConfig] = {}
        self._cross_links: list[CrossDBLink] = []
        self._pools: dict[str, oracledb.ConnectionPool] = {}
        self._load()
        self._ready = True

    # ── Loading ───────────────────────────────────────────────────────────────

    def _load(self) -> None:
        yaml_path = Path("databases.yaml")
        if not yaml_path.exists():
            # Fallback: single DB from legacy env vars (backward compat)
            self._load_legacy_env()
            return

        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        for entry in data.get("databases", []):
            prefix = entry["env_prefix"]
            cfg = DBConfig(
                id=entry["id"],
                name=entry["name"],
                env_prefix=prefix,
                schema=entry.get("schema", ""),
                description=entry.get("description", ""),
                domains=[
                    DomainConfig(name=d["name"], hint=d.get("hint", ""))
                    for d in entry.get("domains", [])
                ],
                user=os.getenv(f"{prefix}_USER", ""),
                password=os.getenv(f"{prefix}_PASSWORD", ""),
                dsn=os.getenv(f"{prefix}_DSN", ""),
            )
            self._configs[cfg.id] = cfg

        for link in data.get("cross_db_links", []):
            self._cross_links.append(CrossDBLink(**link))

    def _load_legacy_env(self) -> None:
        """Single-DB backward compatibility when no databases.yaml exists."""
        user = os.getenv("ORACLE_USER", "")
        if user:
            cfg = DBConfig(
                id="default",
                name="Default Database",
                env_prefix="ORACLE",
                schema=os.getenv("ORACLE_SCHEMA", ""),
                description="Legacy single-database configuration",
                user=user,
                password=os.getenv("ORACLE_PASSWORD", ""),
                dsn=os.getenv("ORACLE_DSN", ""),
            )
            self._configs["default"] = cfg

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def databases(self) -> list[DBConfig]:
        return list(self._configs.values())

    @property
    def cross_links(self) -> list[CrossDBLink]:
        return self._cross_links

    def get_config(self, db_id: str) -> DBConfig:
        if db_id not in self._configs:
            available = list(self._configs.keys())
            raise ValueError(
                f"Unknown database '{db_id}'. "
                f"Registered databases: {available}"
            )
        return self._configs[db_id]

    def get_default_id(self) -> str:
        if not self._configs:
            raise RuntimeError(
                "No databases configured. "
                "Add entries to databases.yaml and credentials to .env"
            )
        return next(iter(self._configs))

    def get_pool(self, db_id: str) -> oracledb.ConnectionPool:
        if db_id not in self._pools:
            cfg = self.get_config(db_id)
            if not cfg.is_configured:
                raise RuntimeError(
                    f"Missing credentials for '{db_id}'. "
                    f"Set {cfg.env_prefix}_USER, {cfg.env_prefix}_PASSWORD, "
                    f"{cfg.env_prefix}_DSN in .env"
                )
            self._pools[db_id] = oracledb.create_pool(
                user=cfg.user,
                password=cfg.password,
                dsn=cfg.dsn,
                min=1,
                max=5,
                increment=1,
            )
        return self._pools[db_id]

    def cross_links_for_table(
        self, db_id: str, table_name: str
    ) -> list[CrossDBLink]:
        """Return cross-DB links where this table is the source."""
        return [
            lk for lk in self._cross_links
            if lk.from_db == db_id and lk.from_table.upper() == table_name.upper()
        ]


# ── Module-level singleton ─────────────────────────────────────────────────────
db_manager = DBManager()
