"""
backend/cache.py  (Phase 4B)

Three thread-safe caches covering the hot paths in the query pipeline.

EmbeddingCache   — avoids re-calling Gemini embedding API for repeated questions
                   Key: SHA-256 of question text  TTL: 1h   Size: 512
SchemaCache      — avoids re-fetching table/column metadata from Neo4j for the
                   same candidate table set within a short window
                   Key: (db_id, frozenset of table names)  TTL: 5min  Size: 256
ResultCache      — short-circuits identical SQL executions against Oracle
                   Key: (db_id, SHA-256 of normalised SQL)  TTL: 5min  Size: 1024

All three use threading.Lock internally so they are safe to call from both
the asyncio event loop and thread-pool workers (asyncio.to_thread).

Usage:
    from backend.cache import embedding_cache, schema_cache, result_cache
"""

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Internal TTL store ────────────────────────────────────────────────────────

@dataclass
class _Entry:
    value: Any
    expires_at: float


class _TTLCache:
    """
    Minimal thread-safe TTL cache backed by a plain dict.
    Uses a simple LRU eviction when max_size is exceeded (pop oldest).
    """

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
                # Evict the first (oldest) key
                oldest = next(iter(self._store))
                del self._store[oldest]
            self._store[key] = _Entry(
                value=value,
                expires_at=time.monotonic() + self._ttl,
            )

    def delete(self, key: str) -> bool:
        with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    def invalidate_prefix(self, prefix: str) -> int:
        """Delete all keys that start with prefix. Returns count deleted."""
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
                "size":     len(self._store),
                "maxsize":  self._maxsize,
                "ttl_s":    self._ttl,
                "hits":     self._hits,
                "misses":   self._misses,
                "hit_rate": round(self._hits / total, 3) if total else 0.0,
            }


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API — one object per cache type
# ══════════════════════════════════════════════════════════════════════════════

class EmbeddingCache:
    """
    Cache for 3072-dim Gemini embedding vectors.
    Key: SHA-256 of the raw question string.
    """

    def __init__(self, maxsize: int = 512, ttl: float = 3600.0) -> None:
        self._c = _TTLCache(maxsize=maxsize, ttl=ttl)

    @staticmethod
    def make_key(question: str) -> str:
        return hashlib.sha256(question.strip().lower().encode()).hexdigest()

    def get(self, question: str) -> Optional[list[float]]:
        return self._c.get(self.make_key(question))

    def set(self, question: str, embedding: list[float]) -> None:
        self._c.set(self.make_key(question), embedding)

    @property
    def stats(self) -> dict:
        return {"embedding_cache": self._c.stats}


class SchemaCache:
    """
    Cache for table_details + batch join_paths for a given (db_id, table_set).
    Invalidated automatically by TTL; manual invalidation available when
    new feedback corrections imply a schema recheck.
    """

    def __init__(self, maxsize: int = 256, ttl: float = 300.0) -> None:
        self._details   = _TTLCache(maxsize=maxsize, ttl=ttl)
        self._joinpaths = _TTLCache(maxsize=maxsize, ttl=ttl)

    @staticmethod
    def make_key(db_id: str, table_names: list[str]) -> str:
        frozen = "|".join(sorted(t.upper() for t in table_names))
        return f"{db_id}:{hashlib.sha256(frozen.encode()).hexdigest()[:16]}"

    def get_details(self, db_id: str, table_names: list[str]) -> Optional[list[dict]]:
        return self._details.get(self.make_key(db_id, table_names))

    def set_details(self, db_id: str, table_names: list[str],
                    details: list[dict]) -> None:
        self._details.set(self.make_key(db_id, table_names), details)

    def get_join_paths(self, db_id: str, table_names: list[str]) -> Optional[list[dict]]:
        return self._joinpaths.get(self.make_key(db_id, table_names))

    def set_join_paths(self, db_id: str, table_names: list[str],
                       paths: list[dict]) -> None:
        self._joinpaths.set(self.make_key(db_id, table_names), paths)

    def invalidate_db(self, db_id: str) -> int:
        n  = self._details.invalidate_prefix(db_id)
        n += self._joinpaths.invalidate_prefix(db_id)
        return n

    @property
    def stats(self) -> dict:
        return {
            "schema_details_cache":   self._details.stats,
            "schema_joinpaths_cache": self._joinpaths.stats,
        }


class ResultCache:
    """
    Cache for full Oracle query execution results.
    Key: (db_id, SHA-256 of normalised SQL).
    Invalidated when a user submits a corrected SQL for the same question
    (feedback action='correct'), signalling the prior result was wrong.
    """

    def __init__(self, maxsize: int = 1024, ttl: float = 300.0) -> None:
        self._c = _TTLCache(maxsize=maxsize, ttl=ttl)

    @staticmethod
    def make_key(db_id: str, sql: str) -> str:
        normalised = " ".join(sql.upper().split())
        return f"{db_id}:{hashlib.sha256(normalised.encode()).hexdigest()}"

    def get(self, db_id: str, sql: str) -> Optional[dict]:
        return self._c.get(self.make_key(db_id, sql))

    def set(self, db_id: str, sql: str, result: dict) -> None:
        self._c.set(self.make_key(db_id, sql), result)

    def invalidate_db(self, db_id: str) -> int:
        return self._c.invalidate_prefix(db_id)

    @property
    def stats(self) -> dict:
        return {"result_cache": self._c.stats}


# ── Module-level singletons (import these everywhere) ─────────────────────────
embedding_cache = EmbeddingCache()
schema_cache    = SchemaCache()
result_cache    = ResultCache()


def all_cache_stats() -> dict:
    """Aggregate stats for all caches — surfaced on /api/health."""
    return {
        **embedding_cache.stats,
        **schema_cache.stats,
        **result_cache.stats,
    }
