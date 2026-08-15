"""Structured context passed between Provider, Quant, and future AI layers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timezone

from .instruments import Instrument
from .providers import Bar, NewsEvent, Quote
from .providers.runtime import ProviderSnapshot
from .quant.engine import QuantSnapshot
from .time_rules import TimePolicy


@dataclass(frozen=True, slots=True)
class MarketContext:
    instrument: Instrument
    quote: Quote
    bars: tuple[Bar, ...]
    quant: QuantSnapshot
    news: tuple[NewsEvent, ...] = ()
    time_policy: TimePolicy | None = None
    provider_snapshot: ProviderSnapshot | None = None
    market_context: dict[str, object] | None = None
    risk_events: tuple[dict[str, object], ...] = ()
    history: dict[str, object] | None = None

    def to_prompt_payload(self, max_news: int = 8) -> dict[str, object]:
        """Return only compressed structured fields allowed into the LLM prompt."""

        payload: dict[str, object] = {
            "instrument": {
                "symbol": self.instrument.symbol,
                "asset_type": self.instrument.asset_type.value,
                "exchange": self.instrument.exchange,
                "quote_currency": self.instrument.quote,
                "trading_hours": self.instrument.trading_hours.value,
            },
            "analysis_time": {
                "timestamp": self.quote.timestamp.astimezone(timezone.utc).isoformat(),
                "timeframe": self.quant.timeframe,
            },
            "price": {
                "last": self.quote.price,
                "change_pct": self.quote.change_pct,
                "high": self.quote.high,
                "low": self.quote.low,
            },
            "quant": self.quant.to_dict(),
            "news": [event.to_dict() for event in self.news[:max_news]],
            "market_context": self.market_context or {},
            "risk_events": list(self.risk_events),
            "history": self.history or {},
        }
        if self.time_policy is not None:
            payload["time_policy"] = self.time_policy.to_dict()
        if self.provider_snapshot is not None:
            payload["provider_snapshot"] = self.provider_snapshot.to_dict()
        return payload

    def input_hash(self) -> str:
        serialized = json.dumps(self.to_prompt_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return self.to_prompt_payload(max_news=50) | {"input_hash": self.input_hash()}
