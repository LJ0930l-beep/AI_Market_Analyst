"""Lightweight embedded vector knowledge store for AI Market Analyst V2.

Stores institutional trading textbooks, Price Action principles, and risk guidelines.
Strictly separates persistent educational knowledge from volatile live market prices.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple


class VectorStore:
    """SQLite-backed persistent vector and text repository."""

    def __init__(self, db_path: str = "data/rag_knowledge.sqlite3") -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_docs (
                    doc_id TEXT PRIMARY KEY,
                    category TEXT,
                    title TEXT,
                    content TEXT,
                    metadata_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    doc_id TEXT,
                    chunk_index INTEGER,
                    text TEXT,
                    embedding_json TEXT,
                    FOREIGN KEY (doc_id) REFERENCES knowledge_docs(doc_id)
                )
            """)
            conn.commit()

    def add_document(self, doc_id: str, title: str, category: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO knowledge_docs (doc_id, category, title, content, metadata_json) VALUES (?, ?, ?, ?, ?)",
                (doc_id, category, title, content, json.dumps(metadata or {}, ensure_ascii=False))
            )
            # Simple chunking by paragraph/sections
            paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
            for idx, p in enumerate(paragraphs):
                chunk_id = f"{doc_id}_c{idx}"
                # Lightweight deterministic pseudo-embedding for local matching without external downloads
                tokens = [w.lower() for w in p.split()]
                conn.execute(
                    "INSERT OR REPLACE INTO knowledge_chunks (chunk_id, doc_id, chunk_index, text, embedding_json) VALUES (?, ?, ?, ?, ?)",
                    (chunk_id, doc_id, idx, p, json.dumps(tokens[:40]))
                )
            conn.commit()

    def search(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """Search relevant chunks matching query terms."""
        query_words = set(query.lower().split())
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT chunk_id, doc_id, text FROM knowledge_chunks").fetchall()
            
            scored = []
            for r in rows:
                text = r["text"]
                text_lower = text.lower()
                # BM25-like overlap score
                overlap = sum(1 for w in query_words if w in text_lower)
                if overlap > 0:
                    scored.append((overlap, dict(r)))
                    
            scored.sort(key=lambda x: x[0], reverse=True)
            return [item[1] for item in scored[:top_k]]
