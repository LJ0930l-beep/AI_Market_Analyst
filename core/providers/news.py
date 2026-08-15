"""News provider boundary; sentiment remains data, not an LLM side effect."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from ..instruments import Instrument


class NewsCategory(StrEnum):
    EARNINGS = "earnings"
    REGULATION = "regulation"
    PRODUCT = "product"
    MACRO = "macro"
    ANALYST = "analyst"
    MERGER = "merger"
    LEGAL = "legal"
    OTHER = "other"


class ImpactHorizon(StrEnum):
    INTRADAY = "intraday"
    DAYS_1_TO_3 = "1-3d"
    WEEKS_1_TO_4 = "1-4w"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class NewsEvent:
    event_id: str
    source: str
    published_at: datetime
    title: str
    symbols: tuple[str, ...]
    category: str = NewsCategory.OTHER.value
    sentiment: float = 0.0
    importance: int = 0
    summary_raw: str | None = None
    url: str | None = None
    credibility: int = 50
    impact_horizon: str = ImpactHorizon.UNKNOWN.value
    dedupe_hash: str | None = None

    def __post_init__(self) -> None:
        if not self.event_id or not self.source or not self.title:
            raise ValueError("news event requires id, source, and title")
        if self.published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")
        object.__setattr__(self, "symbols", tuple(symbol.strip().upper() for symbol in self.symbols if symbol.strip()))
        object.__setattr__(self, "category", str(self.category).lower())
        object.__setattr__(self, "impact_horizon", str(self.impact_horizon).lower())
        if self.category not in {item.value for item in NewsCategory}:
            raise ValueError(f"unsupported news category: {self.category!r}")
        if self.impact_horizon not in {item.value for item in ImpactHorizon}:
            raise ValueError(f"unsupported impact horizon: {self.impact_horizon!r}")
        if not -1.0 <= self.sentiment <= 1.0:
            raise ValueError("sentiment must be in [-1, 1]")
        importance = self.importance
        if isinstance(importance, float) and 0.0 <= importance <= 1.0:
            importance = round(importance * 100)
        if not 0 <= int(importance) <= 100:
            raise ValueError("importance must be in [0, 100]")
        object.__setattr__(self, "importance", int(importance))
        if not 0 <= int(self.credibility) <= 100:
            raise ValueError("credibility must be in [0, 100]")
        if self.dedupe_hash is None:
            raw = f"{self.title.strip().lower()}|{(self.url or '').strip().lower()}".encode("utf-8")
            object.__setattr__(self, "dedupe_hash", hashlib.sha256(raw).hexdigest())

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.event_id,
            "source": self.source,
            "published_at": self.published_at.isoformat(),
            "symbols": list(self.symbols),
            "title": self.title,
            "summary_raw": self.summary_raw,
            "url": self.url,
            "category": self.category,
            "sentiment": self.sentiment,
            "importance": self.importance,
            "credibility": self.credibility,
            "impact_horizon": self.impact_horizon,
            "dedupe_hash": self.dedupe_hash,
        }


class NewsProvider(Protocol):
    def get_events(self, instrument: Instrument, limit: int = 20) -> list[NewsEvent]:
        ...
