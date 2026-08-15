"""Deterministic providers used for local smoke tests and replay."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from ..instruments import Instrument
from .base import Bar, MarketProvider, Quote
from .news import NewsEvent


class FixtureProvider(MarketProvider):
    """Generate stable OHLCV without network calls or credentials."""

    provider_name = "fixture"
    stale = True

    _BASE = {
        "AAPL": 190.0,
        "NVDA": 180.0,
        "TSLA": 250.0,
        "AMD": 150.0,
        "BTCUSDT": 65000.0,
        "ETHUSDT": 3200.0,
    }

    def get_bars(self, instrument: Instrument, timeframe: str, limit: int = 200) -> list[Bar]:
        if limit < 60:
            # Quote callers may request only a couple of bars; keep the
            # provider usable while the Quant layer still enforces its own
            # minimum history.
            limit = 60
        base = self._BASE.get(instrument.symbol, 100.0)
        step = {"5m": timedelta(minutes=5), "15m": timedelta(minutes=15), "1h": timedelta(hours=1), "4h": timedelta(hours=4), "1d": timedelta(days=1)}.get(timeframe.lower())
        if step is None:
            raise ValueError(f"unsupported timeframe: {timeframe!r}")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc) - step * (limit - 1)
        bars: list[Bar] = []
        bias = {"AAPL": 0.00045, "NVDA": 0.00070, "TSLA": -0.00015, "AMD": 0.00030, "BTCUSDT": 0.00055, "ETHUSDT": 0.00035}.get(instrument.symbol, 0.0002)
        for i in range(limit):
            close = base * (1.0 + bias * i + 0.012 * math.sin(i / 7.0) + 0.004 * math.sin(i / 2.7))
            open_ = close * (1.0 - 0.002 * math.sin(i / 3.0))
            high = max(open_, close) * (1.0 + 0.004 + 0.001 * abs(math.sin(i)))
            low = min(open_, close) * (1.0 - 0.004 - 0.001 * abs(math.cos(i)))
            volume = 1_000_000.0 * (1.0 + 0.25 * math.sin(i / 5.0) + (0.4 if i % 13 == 0 else 0.0))
            bars.append(Bar(start + i * step, open_, high, low, close, max(0.0, volume)))
        return bars

    def get_quote(self, instrument: Instrument) -> Quote:
        bars = self.get_bars(instrument, "1h", limit=120)
        last = bars[-1]
        previous = bars[-2].close
        return Quote(
            instrument=instrument,
            timestamp=last.timestamp,
            price=last.close,
            volume=last.volume,
            change_pct=(last.close / previous - 1.0) * 100.0 if previous else None,
            high=last.high,
            low=last.low,
        )


class FixtureNewsProvider:
    provider_name = "fixture_news"

    def get_events(self, instrument: Instrument, limit: int = 20) -> list[NewsEvent]:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return [
            NewsEvent(
                event_id=f"fixture-{instrument.symbol}-001",
                source="fixture",
                published_at=now,
                title=f"{instrument.symbol} market context fixture",
                symbols=(instrument.symbol,),
                category="other",
                sentiment=0.1,
                importance=20,
                summary_raw=f"Deterministic news context for {instrument.symbol}.",
                url=f"https://example.invalid/fixture/{instrument.symbol}",
                credibility=100,
            )
        ][:limit]
