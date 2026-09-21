"""Explicit public-news cache refresh for the read-only intelligence UI.

The market-intelligence GET endpoint intentionally does not touch the network.
This service is the corresponding write boundary: it fetches public RSS once,
normalizes point-in-time event evidence, and persists only what the provider
actually returned.  A failed provider leaves prior evidence intact.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from core.events import EventIntelligenceService
from core.instruments import instrument_for
from core.news_engine import RSSNewsProvider


DEFAULT_NEWS_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def _utc(value: datetime | None = None) -> datetime:
    point = value or datetime.now(timezone.utc)
    if point.tzinfo is None:
        point = point.replace(tzinfo=timezone.utc)
    return point.astimezone(timezone.utc)


def refresh_public_news(
    store: Any,
    *,
    symbols: Iterable[str] = DEFAULT_NEWS_SYMBOLS,
    provider: Any | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Fetch and persist public RSS evidence, without model or order calls."""

    fetched_at = _utc(now)
    active_provider = provider or RSSNewsProvider(timeout=8.0, retries=0)
    collector = EventIntelligenceService(active_provider)
    attempted: list[str] = []
    results: list[dict[str, object]] = []
    event_count = 0

    for raw_symbol in symbols:
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol or symbol in attempted:
            continue
        attempted.append(symbol)
        try:
            instrument = store.resolve_instrument(symbol) if hasattr(store, 'resolve_instrument') else instrument_for(symbol)
        except ValueError:
            results.append({"symbol": symbol, "status": "INVALID_SYMBOL", "event_count": 0})
            continue

        result = collector.collect(instrument, as_of=fetched_at, limit=12)
        if result.available:
            store.save_event_context(result.to_dict())
            count = len(result.events)
            event_count += count
            results.append(
                {
                    "symbol": instrument.symbol,
                    "status": "AVAILABLE",
                    "event_count": count,
                    "provider": result.provider,
                }
            )
        else:
            # Do not erase a previous successful cache because a public HTTP
            # source was temporarily unavailable.
            results.append(
                {
                    "symbol": instrument.symbol,
                    "status": "UNAVAILABLE",
                    "event_count": 0,
                    "provider": result.provider,
                    "error_code": result.error_code or "NEWS_PROVIDER_UNAVAILABLE",
                }
            )

    available = sum(1 for item in results if item["status"] == "AVAILABLE")
    status = "AVAILABLE" if available else "UNAVAILABLE"
    payload: dict[str, object] = {
        "status": status,
        "provider": str(getattr(active_provider, "provider_name", active_provider.__class__.__name__.lower())),
        "fetched_at": fetched_at.isoformat(),
        "requested_symbols": attempted,
        "available_symbols": available,
        "event_count": event_count,
        "results": results,
        "model_called": False,
        "order_created": False,
    }
    store.set_scheduler_state("public_news.status", payload, updated_at=fetched_at.isoformat())
    return payload
