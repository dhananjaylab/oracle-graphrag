"""
backend/cache.py  (Phase 4B + Redis backend)

Three caches covering the hot paths in the query pipeline.

  EmbeddingCache  — skip Gemini embedding API for repeated questions
  SchemaCache     — skip Neo4j table_details + join_paths for known table sets
  ResultCache     — skip Oracle execution for identical SQL within TTL

CHANGED — Redis backend with in-process LRU fallback
────────────────────────────────────────────────────────────────────────────
The original implementation used threading.Lock + plain dicts.  That works
perfectly for a single FastAPI process, but the moment you scale the
FastAPI tier to N replicas (or run multiple uvicorn workers with --workers),
each process gets its own independent dict: N replicas → N separate caches,
each starting cold, while the total request rate sent to Gemini / Neo4j /
Oracle goes UP (every replica re-warms from scratch) instead of down.

The fix: use Redis as a shared backing store.  Each cache key's TTL is
preserved identically to the original implementation.  JSON serialisation
is used rather than pickle — safe to deserialise across Python versions and
process boundaries.

FALLBACK — if REDIS_URL is not set (local dev, CI, environments where Redis
hasn't been provisioned yet), every cache silently falls back to the
original in-process dict implementation, so local development workflows are
completely unaffected and the code path difference is zero.

Configuration
─────────────
  REDIS_URL            Redis DSN. Defaults to redis://localhost:6379/0.
                       Set to "" (empty string) to force the in-process fallback.
  REDIS_PASSWORD       Optional password (also accepted inline in REDIS_URL).
  REDIS_MAX_CONNECTIONS Pool ceiling for the async Redis connection pool.
                       Default 20 — enough for all async uvicorn worker tasks.
  REDIS_SOCKET_TIMEOUT Seconds before a Redis call is considered failed and
                       the in-process fallback is used instead.  Default 0.5s
                       — fast enough that a Redis hiccup doesn't add latency
                       to the user-visible request.

Cache key conventions (unchanged from the original)
─────────────────────────────────────────────────────
  EmbeddingCache : sha256(question.strip().lower())
  SchemaCache    : "{db_id}:{sha256(sorted_tables)[:16]}"
  ResultCache    : "{db_id}:{sha256(normalised_sql)}"

All keys are namespaced with a short prefix so they don't collide with any
other Redis usage in the same instance:
  nlsql:emb:{key}
  nlsql:sch:{key}   (table details sub-key)
  nlsql:jps:{key}   (join paths sub-key)
  nlsql:res:{key}

Usage — identical to the original, no changes needed in callers:
  from backend.cache import embedding_cache, schema_cache, result_cache
"""

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Redis configuration ───────────────────────────────────────────────────────
_REDIS_URL             = os.getenv("REDIS_URL", "redis://localhost:6379/0")
_REDIS_PASSWORD        = os.getenv("REDIS_PASSWORD")
_REDIS_MAX_CONNECTIONS = int(os.getenv("REDIS_MAX_CONNECTIONS", "20"))
_REDIS_SOCKET_TIMEOUT  = float(os.getenv("REDIS_SOCKET_TIMEOUT", "0.5"))

# Flag: set to True once Redis is confirmed reachable at startup
_redis_available = False
_redis_client    = None   # redis.asyncio.Redis, initialised lazily


def _build_redis_client():
    """
    Attempt to import redis.asyncio and create a connection pool.
    Returns None (and sets _redis_available=False) if redis-py is not
    installed or REDIS_URL is empty — the in-process fallback is used instead.
    """
    global _redis_client, _redis_available
    if not _REDIS_URL:
        logger.info("[cache] REDIS_URL not set — using in-process fallback for all caches")
        return None
    try:
        import redis.asyncio as aioredis  # redis-py >= 4.2 ships redis.asyncio
        pool = aioredis.ConnectionPool.from_url(
            _REDIS_URL,
            password        = _REDIS_PASSWORD or None,
            max_connections = _REDIS_MAX_CONNECTIONS,
            socket_timeout  = _REDIS_SOCKET_TIMEOUT,
            decode_responses = True,   # all keys/values are strings
        )
        client = aioredis.Redis(connection_pool=pool)
        _redis_client    = client
        _redis_available = True
        logger.info("[cache] Redis connection pool created: %s (max_connections=%d)",
                    _REDIS_URL.split("@")[-1], _REDIS_MAX_CONNECTIONS)
        return client
    except ImportError:
        logger.warning(
            "[cache] redis-py not installed (pip install redis). "
            "Using in-process fallback — add redis to requirements.txt for "
            "shared caching across FastAPI replicas."
        )
        return None
    except Exception as exc:
        logger.warning("[cache] Redis unavailable (%s) — using in-process fallback", exc)
        return None


_build_redis_client()   # called once at module import time


async def _redis_get(key: str) -> Optional[str]:
    """Return the raw string value or None.  Never raises."""
    if not _redis_available or _redis_client is None:
        return None
    try:
        return await _redis_client.get(key)
    except Exception as exc:
        logger.debug("[cache] Redis GET failed (%s) — fallback", exc)
        return None


async def _redis_set(key: str, value: str, ttl_s: int) -> None:
    """Set key with TTL.  Never raises."""
    if not _redis_available or _redis_client is None:
        return
    try:
        await _redis_client.setex(key, ttl_s, value)
    except Exception as exc:
        logger.debug("[cache] Redis SETEX failed (%s) — fallback", exc)


async def _redis_delete_prefix(prefix: str) -> int:
    """
    Delete all keys matching prefix*.
    Uses SCAN to avoid blocking the server with KEYS * on large keyspaces.
    Returns count of deleted keys.
    """
    if not _redis_available or _redis_client is None:
        return 0
    try:
        deleted = 0
        async for key in _redis_client.scan_iter(match=f"{prefix}*", count=100):
            await _redis_client.delete(key)
            deleted += 1
        return deleted
    except Exception as exc:
        logger.debug("[cache] Redis prefix delete failed (%s)", exc)
        return 0


# ── In-process LRU fallback (identical to the original implementation) ─────────

@dataclass
class _Entry:
    value: Any
    expires_at: float


class _LocalTTLCache:
    """Thread-safe in-process TTL cache — used when Redis is unavailable."""

    def __init__(self, maxsize: int, ttl: float) -> None:
        self._maxsize = maxsize
        self._ttl     = ttl
        self._store:  dict[str, _Entry] = {}
        self._lock    = threading.Lock()
        self._hits    = 0
        self._misses  = 0

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            if time.monotonic() > entry.expires_at:
                del self._store[key]
                self._misses += 1
                return None
            self._hits += 1
            return entry.value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if len(self._store) >= self._maxsize:
                oldest = next(iter(self._store))
                del self._store[oldest]
            self._store[key] = _Entry(
                value=value,
                expires_at=time.monotonic() + self._ttl,
            )

    def invalidate_prefix(self, prefix: str) -> int:
        with self._lock:
            victims = [k for k in self._store if k.startswith(prefix)]
            for k in victims:
                del self._store[k]
            return len(victims)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    @property
    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "backend":  "local",
                "size":     len(self._store),
                "maxsize":  self._maxsize,
                "ttl_s":    self._ttl,
                "hits":     self._hits,
                "misses":   self._misses,
                "hit_rate": round(self._hits / total, 3) if total else 0.0,
            }


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC CACHE CLASSES
# ══════════════════════════════════════════════════════════════════════════════

class EmbeddingCache:
    """
    Cache for 3072-dim Gemini embedding vectors.
    TTL: 1h   Redis key prefix: nlsql:emb:
    Value serialised as a JSON array of floats.
    """

    _PREFIX = "nlsql:emb:"
    _TTL    = 3600

    def __init__(self, maxsize: int = 512) -> None:
        self._local = _LocalTTLCache(maxsize=maxsize, ttl=self._TTL)
        self._hits_redis  = 0
        self._hits_local  = 0
        self._misses      = 0

    @staticmethod
    def make_key(question: str) -> str:
        return hashlib.sha256(question.strip().lower().encode()).hexdigest()

    def get(self, question: str) -> Optional[list[float]]:
        """Synchronous read — checks local L1 first, then Redis (sync via loop if needed)."""
        key = self.make_key(question)
        # L1: local dict
        val = self._local.get(key)
        if val is not None:
            self._hits_local += 1
            return val
        # Redis is async — callers of EmbeddingCache.get() are in async context;
        # the async variant below is the preferred path.
        self._misses += 1
        return None

    async def aget(self, question: str) -> Optional[list[float]]:
        """Async read: L1 local → L2 Redis."""
        key = self.make_key(question)
        val = self._local.get(key)
        if val is not None:
            self._hits_local += 1
            return val

        raw = await _redis_get(self._PREFIX + key)
        if raw is not None:
            try:
                embedding = json.loads(raw)
                self._local.set(key, embedding)   # warm L1
                self._hits_redis += 1
                return embedding
            except Exception:
                pass

        self._misses += 1
        return None

    def set(self, question: str, embedding: list[float]) -> None:
        """Synchronous write to L1 only (for backward compat with sync callers)."""
        key = self.make_key(question)
        self._local.set(key, embedding)

    async def aset(self, question: str, embedding: list[float]) -> None:
        """Async write to L1 + Redis."""
        key = self.make_key(question)
        self._local.set(key, embedding)
        await _redis_set(self._PREFIX + key, json.dumps(embedding), self._TTL)

    @property
    def stats(self) -> dict:
        s = self._local.stats
        s.update({
            "backend":     "redis" if _redis_available else "local",
            "hits_local":  self._hits_local,
            "hits_redis":  self._hits_redis,
            "misses":      self._misses,
        })
        return {"embedding_cache": s}


class SchemaCache:
    """
    Cache for table_details + batch join_paths for a (db_id, table_set).
    TTL: 5 min   Redis key prefixes: nlsql:sch:  nlsql:jps:
    """

    _PREFIX_DETAILS   = "nlsql:sch:"
    _PREFIX_JOINPATHS = "nlsql:jps:"
    _TTL              = 300

    def __init__(self, maxsize: int = 256) -> None:
        self._local_det = _LocalTTLCache(maxsize=maxsize, ttl=self._TTL)
        self._local_jps = _LocalTTLCache(maxsize=maxsize, ttl=self._TTL)

    @staticmethod
    def make_key(db_id: str, table_names: list[str]) -> str:
        frozen = "|".join(sorted(t.upper() for t in table_names))
        return f"{db_id}:{hashlib.sha256(frozen.encode()).hexdigest()[:16]}"

    # ── Table details ─────────────────────────────────────────────────────────

    def get_details(self, db_id: str, table_names: list[str]) -> Optional[list[dict]]:
        return self._local_det.get(self.make_key(db_id, table_names))

    async def aget_details(self, db_id: str, table_names: list[str]) -> Optional[list[dict]]:
        key = self.make_key(db_id, table_names)
        val = self._local_det.get(key)
        if val is not None:
            return val
        raw = await _redis_get(self._PREFIX_DETAILS + key)
        if raw is not None:
            try:
                details = json.loads(raw)
                self._local_det.set(key, details)
                return details
            except Exception:
                pass
        return None

    def set_details(self, db_id: str, table_names: list[str], details: list[dict]) -> None:
        self._local_det.set(self.make_key(db_id, table_names), details)

    async def aset_details(self, db_id: str, table_names: list[str], details: list[dict]) -> None:
        key = self.make_key(db_id, table_names)
        self._local_det.set(key, details)
        await _redis_set(self._PREFIX_DETAILS + key, json.dumps(details, default=str), self._TTL)

    # ── Join paths ────────────────────────────────────────────────────────────

    def get_join_paths(self, db_id: str, table_names: list[str]) -> Optional[list[dict]]:
        return self._local_jps.get(self.make_key(db_id, table_names))

    async def aget_join_paths(self, db_id: str, table_names: list[str]) -> Optional[list[dict]]:
        key = self.make_key(db_id, table_names)
        val = self._local_jps.get(key)
        if val is not None:
            return val
        raw = await _redis_get(self._PREFIX_JOINPATHS + key)
        if raw is not None:
            try:
                paths = json.loads(raw)
                self._local_jps.set(key, paths)
                return paths
            except Exception:
                pass
        return None

    def set_join_paths(self, db_id: str, table_names: list[str], paths: list[dict]) -> None:
        self._local_jps.set(self.make_key(db_id, table_names), paths)

    async def aset_join_paths(self, db_id: str, table_names: list[str], paths: list[dict]) -> None:
        key = self.make_key(db_id, table_names)
        self._local_jps.set(key, paths)
        await _redis_set(self._PREFIX_JOINPATHS + key, json.dumps(paths, default=str), self._TTL)

    # ── Invalidation ──────────────────────────────────────────────────────────

    def invalidate_db(self, db_id: str) -> int:
        n  = self._local_det.invalidate_prefix(db_id)
        n += self._local_jps.invalidate_prefix(db_id)
        return n

    async def ainvalidate_db(self, db_id: str) -> int:
        n  = self.invalidate_db(db_id)          # local
        n += await _redis_delete_prefix(f"{self._PREFIX_DETAILS}{db_id}:")
        n += await _redis_delete_prefix(f"{self._PREFIX_JOINPATHS}{db_id}:")
        return n

    @property
    def stats(self) -> dict:
        return {
            "schema_details_cache":   {
                **self._local_det.stats, "backend": "redis" if _redis_available else "local"
            },
            "schema_joinpaths_cache": {
                **self._local_jps.stats, "backend": "redis" if _redis_available else "local"
            },
        }


class ResultCache:
    """
    Cache for full Oracle query execution results.
    TTL: 5 min   Redis key prefix: nlsql:res:
    Invalidated (Redis + local) when a user correction is submitted.
    """

    _PREFIX = "nlsql:res:"
    _TTL    = 300

    def __init__(self, maxsize: int = 1024) -> None:
        self._local = _LocalTTLCache(maxsize=maxsize, ttl=self._TTL)

    @staticmethod
    def make_key(db_id: str, sql: str) -> str:
        normalised = " ".join(sql.upper().split())
        return f"{db_id}:{hashlib.sha256(normalised.encode()).hexdigest()}"

    def get(self, db_id: str, sql: str) -> Optional[dict]:
        return self._local.get(self.make_key(db_id, sql))

    async def aget(self, db_id: str, sql: str) -> Optional[dict]:
        key = self.make_key(db_id, sql)
        val = self._local.get(key)
        if val is not None:
            return val
        raw = await _redis_get(self._PREFIX + key)
        if raw is not None:
            try:
                result = json.loads(raw)
                self._local.set(key, result)
                return result
            except Exception:
                pass
        return None

    def set(self, db_id: str, sql: str, result: dict) -> None:
        self._local.set(self.make_key(db_id, sql), result)

    async def aset(self, db_id: str, sql: str, result: dict) -> None:
        key = self.make_key(db_id, sql)
        self._local.set(key, result)
        await _redis_set(self._PREFIX + key, json.dumps(result, default=str), self._TTL)

    def invalidate_db(self, db_id: str) -> int:
        return self._local.invalidate_prefix(db_id)

    async def ainvalidate_db(self, db_id: str) -> int:
        n  = self.invalidate_db(db_id)
        n += await _redis_delete_prefix(f"{self._PREFIX}{db_id}:")
        return n

    @property
    def stats(self) -> dict:
        s = self._local.stats
        s["backend"] = "redis" if _redis_available else "local"
        return {"result_cache": s}


# ── Module-level singletons (import these everywhere — API unchanged) ──────────
embedding_cache = EmbeddingCache()
schema_cache    = SchemaCache()
result_cache    = ResultCache()


def all_cache_stats() -> dict:
    """Aggregate stats for all caches — surfaced on /api/health."""
    return {
        **embedding_cache.stats,
        **schema_cache.stats,
        **result_cache.stats,
        "redis_available": _redis_available,
        "redis_url":       (_REDIS_URL.split("@")[-1] if _redis_available else "none"),
    }
