"""RAG Retriever module for AI Market Analyst V2."""

from __future__ import annotations

from typing import Any, Dict, List
from .vector_store import VectorStore


class KnowledgeRetriever:
    """Retrieves relevant trading wisdom, strategy setups, and risk rules."""

    def __init__(self, store: VectorStore | None = None) -> None:
        self.store = store or VectorStore()
        self._seed_default_knowledge()

    def _seed_default_knowledge(self) -> None:
        # Seed fundamental Price Action and Risk Rules if store is empty
        results = self.store.search("Price Action", top_k=1)
        if not results:
            self.store.add_document(
                doc_id="pa_001",
                title="Price Action Market Structure Principles",
                category="technical",
                content=(
                    "A sustained bullish trend requires confirmed Higher Highs and Higher Lows (HH_HL).\n\n"
                    "When price breaches a previous swing low in a bull trend, market structure shifts to neutral or potential LH_LL.\n\n"
                    "Do not enter breakout trades at the exact extreme of a multi-leg expansion without a retest of the broken level."
                )
            )
            self.store.add_document(
                doc_id="risk_001",
                title="Institutional Risk Management & Sizing Guidelines",
                category="risk",
                content=(
                    "Never risk more than 5% of total portfolio equity on any single trading setup.\n\n"
                    "Ensure the stop-loss distance is grounded in technical market structure rather than arbitrary dollar targets.\n\n"
                    "If the market displays erratic chop with contracting volume and indeterminate trend, stay in cash."
                )
            )

    def retrieve(self, query: str, top_k: int = 3) -> List[str]:
        hits = self.store.search(query, top_k=top_k)
        return [h["text"] for h in hits]
