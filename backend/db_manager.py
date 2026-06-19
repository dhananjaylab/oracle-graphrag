"""
backend/db_manager.py
----------------------
Singleton that owns all database connections.

Loads structural config from databases.yaml (non-sensitive).
Resolves credentials from environment variables via env_prefix convention.

  databases.yaml entry:  { id: fincore, env_prefix: FINCORE, ... }
  .env variables:        FINCORE_USER, FINCORE_PASSWORD, FINCORE_DSN

One oracledb connection pool is created lazily per database on first use.

CHANGED — configurable + coordinated pool sizing:
  The oracledb pool min/max used to be hardcoded (min=1, max=5) and
  invisible to the rest of the system. That's a problem because the
  Oracle MCP server's own MCPConnectionPool (backend/mcp_client/pool.py)
  can admit up to MCP_POOL_MAX concurrent execute_query calls — if that
  number is larger than the actual oracledb pool size, the real
  contention happens inside this process, underneath the MCP pool's
  stats, where /api/health can't see it.

  Pool size is now:
    1. Per-database override via databases.yaml (`pool_min` / `pool_max`
       keys on a database entry), if present, else
    2. Global defaults from ORACLE_POOL_MIN / ORACLE_POOL_MAX env vars.

  At pool-creation time we also log a warning if the resulting Oracle
  pool max is smaller than MCP_POOL_MAX, so the mismatch is visible
  instead of silent.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
import oracledb

logger = logging.getLogger(__name__)

# ── Global pool-sizing defaults (env-configurable) ──────────────────────────
# These should be coordinated with MCP_POOL_MAX (backend/mcp_client/pool.py):
# the Oracle pool is the real resource ceiling, so it should generally be
# >= the MCP session pool's max_size, not smaller.
_GLOBAL_POOL_MIN       = int(os.getenv("ORACLE_POOL_MIN", "2"))
_GLOBAL_POOL_MAX       = int(os.getenv("ORACLE_POOL_MAX", "8"))
_GLOBAL_POOL_INCREMENT = int(os.getenv("ORACLE_POOL_INCREMENT", "1"))
_MCP_POOL_MAX_HINT     = int(os.getenv("MCP_POOL_MAX", "8"))


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
    # optional per-database pool size override from databases.yaml
    # (0 means "use the global ORACLE_POOL_MIN / ORACLE_POOL_MAX default")
    pool_min: int = 0
    pool_max: int = 0

    @property
    def qualified_schema(self) -> str:
        return self.schema.upper()

    @property
    def is_configured(self) -> bool:
        return bool(self.user and self.password and self.dsn)

    @property
    def effective_pool_min(self) -> int:
        return self.pool_min or _GLOBAL_POOL_MIN

    @property
    def effective_pool_max(self) -> int:
        return self.pool_max or _GLOBAL_POOL_MAX


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
                pool_min=int(entry.get("pool_min", 0) or 0),
                pool_max=int(entry.get("pool_max", 0) or 0),
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

            pool_min = cfg.effective_pool_min
            pool_max = cfg.effective_pool_max

            if pool_max < _MCP_POOL_MAX_HINT:
                logger.warning(
                    "[db_manager] Oracle pool max (%d) for '%s' is smaller than "
                    "MCP_POOL_MAX (%d). The MCP layer can admit more concurrent "
                    "execute_query calls than this database has connections for "
                    "— contention will happen invisibly inside the Oracle MCP "
                    "server process instead of showing up in pool.stats. Raise "
                    "ORACLE_POOL_MAX (or this database's pool_max in "
                    "databases.yaml), or lower MCP_POOL_MAX, so they agree.",
                    pool_max, db_id, _MCP_POOL_MAX_HINT,
                )

            self._pools[db_id] = oracledb.create_pool(
                user=cfg.user,
                password=cfg.password,
                dsn=cfg.dsn,
                min=pool_min,
                max=pool_max,
                increment=_GLOBAL_POOL_INCREMENT,
            )
            logger.info(
                "[db_manager] Oracle pool created for '%s': min=%d max=%d increment=%d",
                db_id, pool_min, pool_max, _GLOBAL_POOL_INCREMENT,
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
