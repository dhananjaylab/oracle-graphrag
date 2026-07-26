import os
import sys
import oracledb
import numpy as np
import difflib
from rapidfuzz import fuzz
from decouple import config
from google import genai
from google.genai import types

class SearchSimilarProduct:
    def __init__(self, top_k=5, minimal_distance=1.0, embedding_model="gemini-embedding-001"):
        api_key = config("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("❌ GEMINI_API_KEY is missing")
        
        self.client = genai.Client(api_key=api_key)
        self.db_dsn = config("ORACLE_DSN")
        self.username = config("ORACLE_USER")
        self.password = config("ORACLE_PASSWORD")
        self.top_k = top_k
        self.minimal_distance = minimal_distance
        self.embedding_model = embedding_model

        try:
            self.conn = oracledb.connect(user=self.username, password=self.password, dsn=self.db_dsn)
            with self.conn.cursor() as cursor:
                cursor.execute("ALTER SESSION SET CURRENT_SCHEMA = BOOKSTORE")
            # Redirecting to stderr ensures MCP JSON-RPC on stdout isn't corrupted
            print(f"DEBUG: Connected to Oracle (BOOKSTORE)", file=sys.stderr)
        except Exception as e:
            print(f"DEBUG: Oracle Error: {e}", file=sys.stderr)
            raise

        self._load_embeddings()

    def _load_embeddings(self):
        try:
            with self.conn.cursor() as cursor:
                cursor.execute("SELECT id, code, description, vector FROM embeddings_products")
                self.vectors = []
                self.products = []
                for row in cursor.fetchall():
                    if row[3]:
                        vector = np.frombuffer(row[3].read(), dtype=np.float32)
                        self.vectors.append(vector)
                        self.products.append({"id": row[0], "code": row[1], "description": row[2]})
                self.vectors = np.array(self.vectors)
                print(f"DEBUG: Loaded {len(self.products)} vectors", file=sys.stderr)
        except Exception as e:
            print(f"DEBUG: Load Error: {e}", file=sys.stderr)

    def _embed_text(self, text):
        try:
            response = self.client.models.embed_content(
                model=self.embedding_model,
                contents=text,
                config=types.EmbedContentConfig(task_type="SEMANTIC_SIMILARITY")
            )
            return np.array(response.embeddings[0].values)
        except Exception as e:
            print(f"DEBUG: Embedding Error: {e}", file=sys.stderr)
            return None

    def search_similar_products(self, description_input):
        description_input = description_input.strip()
        # Simple fuzzy correction
        descriptions = [p["description"] for p in self.products]
        matches = difflib.get_close_matches(description_input, descriptions, n=1, cutoff=0.6)
        corrected = matches[0] if matches else description_input

        results = {"consult_original": description_input, "consult_used": corrected, "semantics": [], "fallback_fuzzy": []}

        if len(self.vectors) == 0:
            return results

        emb = self._embed_text(corrected)
        if emb is None: return results

        dists = np.linalg.norm(self.vectors - emb, axis=1)
        top_indices = np.argsort(dists)[:self.top_k]

        for idx in top_indices:
            dist = dists[idx]
            if dist < self.minimal_distance:
                match = self.products[idx]
                results["semantics"].append({
                    "id": match["id"], "code": match["code"], "description": match["description"],
                    "similarity": round((1/(1+dist)) * 100, 2)
                })
        return results
    
    def close(self):
        """Explicitly close the Oracle connection."""
        if hasattr(self, 'conn') and self.conn:
            self.conn.close()