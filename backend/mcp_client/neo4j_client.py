"""
backend/mcp_client/neo4j_client.py

Neo4jMCPClient — typed wrapper around MCPConnectionPool for Neo4j tools.

All methods serialize complex arguments (embeddings, list[str]) to JSON
before sending to the MCP server, matching the FastMCP tool signatures.
Falls back to direct backend.services.neo4j_service calls on MCP failure.

CHANGED: accepts an optional bearer token (from NEO4J_MCP_TOKEN) that's
forwarded to every pooled session as an Authorization header — see the
matching note in oracle_client.py for scope/limitations.
"""

from __future__ import annotations

import json
import logging
import os

from backend.mcp_client.pool import MCPConnectionPool

logger = logging.getLogger(__name__)

NEO4J_MCP_URL   = os.getenv("NEO4J_MCP_URL", "http://localhost:8002")
NEO4J_MCP_TOKEN = os.getenv("NEO4J_MCP_TOKEN")  # optional static bearer token


class Neo4jMCPClient:
    """
    Typed MCP client for the Neo4j MCP server.

    Instantiated as a module-level singleton in backend/mcp_client/__init__.py
    and connected during FastAPI startup.
    """

    def __init__(self, server_url: str = NEO4J_MCP_URL) -> None:
        self._session = MCPConnectionPool(
            server_url, name="neo4j", auth_token=NEO4J_MCP_TOKEN,
        )

    @property
    def pool_stats(self) -> dict:
        return self._session.stats

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        await self._session.connect()

    async def disconnect(self) -> None:
        await self._session.disconnect()

    async def ping(self) -> bool:
        return await self._session.ping()

    # ══════════════════════════════════════════════════════════════════════════
    # semantic_search
    # ══════════════════════════════════════════════════════════════════════════

    async def semantic_search(
        self,
        query_embedding: list[float],
        database_id:     str,
        top_k:           int = 12,
    ) -> dict:
        """
        Vector similarity search on (:Table) and (:Column) nodes.

        Returns {tables, columns, cypher_used}.
        Falls back to direct neo4j_service call on MCP failure.
        """
        try:
            result = await self._session.call_tool("semantic_search", {
                "embedding_json": json.dumps(query_embedding),
                "database_id":    database_id,
                "top_k":          top_k,
            })
            return result
        except Exception as exc:
            logger.warning("[neo4j-mcp] semantic_search fallback: %s", exc)
            from backend.services import neo4j_service
            return await neo4j_service.semantic_schema_search(
                query_embedding=query_embedding,
                database_id=database_id,
                top_k=top_k,
            )

    # ══════════════════════════════════════════════════════════════════════════
    # get_table_details
    # ══════════════════════════════════════════════════════════════════════════

    async def get_table_details(
        self,
        table_names: list[str],
        database_id: str,
    ) -> list[dict]:
        """
        Full column + domain metadata for named tables.

        Returns list of table detail objects.
        """
        try:
            result = await self._session.call_tool("get_table_details", {
                "table_names_json": json.dumps(table_names),
                "database_id":      database_id,
            })
            return result if isinstance(result, list) else []
        except Exception as exc:
            logger.warning("[neo4j-mcp] get_table_details fallback: %s", exc)
            from backend.services import neo4j_service
            return await neo4j_service.get_table_details(table_names, database_id)

    # ══════════════════════════════════════════════════════════════════════════
    # get_join_path
    # ══════════════════════════════════════════════════════════════════════════

    async def get_join_path(
        self,
        table1:      str,
        table2:      str,
        database_id: str,
    ) -> list[dict]:
        """
        Shortest FK join path between two tables.

        Returns list of path objects (empty = no path).
        """
        try:
            result = await self._session.call_tool("get_join_path", {
                "table1":      table1,
                "table2":      table2,
                "database_id": database_id,
            })
            return result if isinstance(result, list) else []
        except Exception as exc:
            logger.warning("[neo4j-mcp] get_join_path fallback: %s", exc)
            from backend.services import neo4j_service
            return await neo4j_service.get_join_path(table1, table2, database_id)

    # ══════════════════════════════════════════════════════════════════════════
    # get_join_paths_batch
    # ══════════════════════════════════════════════════════════════════════════

    async def get_join_paths_batch(
        self,
        table_names: list[str],
        database_id: str,
    ) -> list[dict]:
        """
        Shortest FK join paths between all pairs of candidate tables.

        Returns list of path objects.
        """
        try:
            result = await self._session.call_tool("get_join_paths_batch", {
                "table_names_json": json.dumps(table_names),
                "database_id":      database_id,
            })
            return result if isinstance(result, list) else []
        except Exception as exc:
            logger.warning("[neo4j-mcp] get_join_paths_batch fallback: %s", exc)
            from backend.services import neo4j_service
            return await neo4j_service.get_join_paths_batch(table_names, database_id)

    # ══════════════════════════════════════════════════════════════════════════
    # get_cross_db_hints
    # ══════════════════════════════════════════════════════════════════════════

    async def get_cross_db_hints(
        self,
        table_names: list[str],
        database_id: str,
    ) -> list[dict]:
        """
        CROSS_DB_JOIN edges for candidate tables.

        Returns list of cross-DB link objects.
        """
        try:
            result = await self._session.call_tool("get_cross_db_hints", {
                "table_names_json": json.dumps(table_names),
                "database_id":      database_id,
            })
            return result if isinstance(result, list) else []
        except Exception as exc:
            logger.warning("[neo4j-mcp] get_cross_db_hints fallback: %s", exc)
            from backend.services import neo4j_service
            return await neo4j_service.get_cross_db_hints(table_names, database_id)

    # ══════════════════════════════════════════════════════════════════════════
    # search_patterns
    # ══════════════════════════════════════════════════════════════════════════

    async def search_patterns(
        self,
        query_embedding: list[float],
        database_id:     str,
        top_k:           int   = 3,
        min_similarity:  float = 0.85,
    ) -> list[dict]:
        """
        Find past QueryPatterns similar to the current question embedding.

        Returns list of matched pattern objects with nl_question, sql,
        schema_cypher, success_count, score.
        """
        try:
            result = await self._session.call_tool("search_patterns", {
                "embedding_json": json.dumps(query_embedding),
                "database_id":    database_id,
                "top_k":          top_k,
                "min_similarity": min_similarity,
            })
            return result if isinstance(result, list) else []
        except Exception as exc:
            logger.warning("[neo4j-mcp] search_patterns fallback: %s", exc)
            from backend.services import neo4j_service
            return await neo4j_service.search_similar_patterns(
                query_embedding=query_embedding,
                database_id=database_id,
                top_k=top_k,
                min_similarity=min_similarity,
            )

    # ══════════════════════════════════════════════════════════════════════════
    # store_pattern
    # ══════════════════════════════════════════════════════════════════════════

    async def store_pattern(
        self,
        database_id:   str,
        nl_question:   str,
        sql:           str,
        schema_cypher: str,
        tables_used:   list[str],
        execution_ms:  int,
        embedding:     list[float],
    ) -> bool:
        """
        Persist a successful NL→SQL exchange as a QueryPattern node.

        Returns True if stored successfully.
        """
        try:
            result = await self._session.call_tool("store_pattern", {
                "database_id":      database_id,
                "nl_question":      nl_question,
                "sql":              sql,
                "schema_cypher":    schema_cypher,
                "tables_used_json": json.dumps(tables_used),
                "execution_ms":     execution_ms,
                "embedding_json":   json.dumps(embedding),
            })
            return bool(result.get("stored", False)) if isinstance(result, dict) else False
        except Exception as exc:
            logger.warning("[neo4j-mcp] store_pattern fallback: %s", exc)
            try:
                from backend.services import neo4j_service
                await neo4j_service.store_query_pattern(
                    database_id=database_id, nl_question=nl_question,
                    sql=sql, schema_cypher=schema_cypher,
                    tables_used=tables_used, execution_ms=execution_ms,
                    embedding=embedding,
                )
                return True
            except Exception:
                return False

    # ══════════════════════════════════════════════════════════════════════════
    # get_schema_summary
    # ══════════════════════════════════════════════════════════════════════════

    async def get_schema_summary(self) -> dict:
        """
        All databases with their tables and domains — for the UI explorer.

        Returns {databases: [...]}
        """
        try:
            result = await self._session.call_tool("get_schema_summary", {})
            return result if isinstance(result, dict) else {"databases": []}
        except Exception as exc:
            logger.warning("[neo4j-mcp] get_schema_summary fallback: %s", exc)
            from backend.services import neo4j_service
            return await neo4j_service.get_schema_summary()

    # ══════════════════════════════════════════════════════════════════════════
    # record_feedback
    # ══════════════════════════════════════════════════════════════════════════

    async def record_feedback(
        self,
        nl_question:   str,
        database_id:   str,
        action:        str,              # "increment" | "decrement" | "correct"
        corrected_sql: str = "",
    ) -> bool:
        """
        Update QueryPattern weight based on user feedback.

        Returns True if the pattern was found and updated.
        """
        try:
            result = await self._session.call_tool("record_feedback", {
                "nl_question":   nl_question,
                "database_id":   database_id,
                "action":        action,
                "corrected_sql": corrected_sql,
            })
            return bool(result.get("updated", False)) if isinstance(result, dict) else False
        except Exception as exc:
            logger.warning("[neo4j-mcp] record_feedback fallback: %s", exc)
            from backend.services import neo4j_service
            try:
                if action == "increment":
                    return await neo4j_service.increment_pattern_success(nl_question, database_id)
                elif action == "decrement":
                    return await neo4j_service.decrement_pattern_success(nl_question, database_id)
                elif action == "correct" and corrected_sql.strip():
                    return await neo4j_service.update_pattern_sql(
                        nl_question, database_id, corrected_sql.strip()
                    )
            except Exception:
                pass
            return False
