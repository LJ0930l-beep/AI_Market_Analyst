"""Read-only V1.1 market-intelligence projections over durable evidence.

These views deliberately do not fetch providers, run analysis, or write domain
records.  A missing value remains unavailable instead of becoming demo data.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping

from .storage import SQLiteStore

MARKET_INTELLIGENCE_VERSION = "market_intelligence_v1"
PULSE_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "NVDA", "NASDAQ", "VIX", "US10Y")
SECTOR_BY_SYMBOL = {
    "NVDA": ("semiconductors", "equity"),
    "AAPL": ("ai_technology", "equity"),
    "MSFT": ("ai_technology", "equity"),
    "BTCUSDT": ("crypto_majors", "crypto"),
    "ETHUSDT": ("crypto_layer1", "crypto"),
    "SOLUSDT": ("crypto_layer1", "crypto"),
}


def _utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        return None
    return result.astimezone(timezone.utc)


def _context(prediction: Mapping[str, Any]) -> dict[str, Any]:
    raw = prediction.get("context_json")
    if not isinstance(raw, str) or len(raw) > 250_000:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _number(mapping: object, key: str) -> float | None:
    if not isinstance(mapping, Mapping):
        return None
    value = mapping.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _freshness(prediction: Mapping[str, Any], context: Mapping[str, Any], as_of: datetime) -> dict[str, object]:
    provider = context.get("provider_snapshot")
    timestamp = None
    stale_flag = False
    if isinstance(provider, Mapping):
        timestamp = _utc(provider.get("data_as_of"))
        stale_flag = provider.get("stale") is True
    timestamp = timestamp or _utc(prediction.get("data_as_of")) or _utc(prediction.get("generated_at"))
    if timestamp is None:
        return {"status": "unknown", "data_as_of": None, "age_seconds": None}
    if timestamp > as_of:
        return {"status": "invalid_future", "data_as_of": timestamp.isoformat(), "age_seconds": None}
    age = int((as_of - timestamp).total_seconds())
    return {
        "status": "stale" if stale_flag or age > 3_600 else "fresh",
        "data_as_of": timestamp.isoformat(),
        "age_seconds": age,
        "provider_stale": stale_flag,
    }


def _pulse_entry(symbol: str, record: Mapping[str, Any] | None, as_of: datetime) -> dict[str, object]:
    realtime = record.get("realtime") if isinstance(record, Mapping) else None
    if isinstance(realtime, Mapping):
        timestamp = _utc(realtime.get("data_as_of")) or _utc(realtime.get("last_trade_at"))
        if timestamp is None:
            freshness: dict[str, object] = {"status": "unknown", "data_as_of": None, "age_seconds": None}
        else:
            age = max(0, int((as_of - timestamp).total_seconds()))
            source_status = str(realtime.get("freshness_status") or "unknown")
            freshness = {"status": source_status, "data_as_of": timestamp.isoformat(), "age_seconds": age, "provider": realtime.get("provider")}
        price = _number(realtime, "price")
        change = _number(realtime, "change_pct")
        missing = [name for name, value in (("price_missing", price), ("change_missing", change)) if value is None]
        return {
            "symbol": symbol,
            "status": "available" if not missing and freshness.get("status") == "fresh" else "degraded",
            "price": price,
            "change_pct": change,
            "freshness": freshness,
            "provenance": ["sidecar_public_hydration", "durable_realtime_cache"],
            "missing_reasons": missing,
        }
    if record is None:
        return {
            "symbol": symbol,
            "status": "unavailable",
            "price": None,
            "change_pct": None,
            "freshness": {"status": "unavailable", "data_as_of": None},
            "provenance": ["durable_latest_live_prediction"],
            "missing_reasons": ["unsupported_or_no_saved_prediction"],
        }
    prediction = record.get("prediction")
    if not isinstance(prediction, Mapping):
        return _pulse_entry(symbol, None, as_of)
    context = _context(prediction)
    freshness = _freshness(prediction, context, as_of)
    if freshness["status"] == "invalid_future":
        return {
            "symbol": symbol,
            "status": "unavailable",
            "price": None,
            "change_pct": None,
            "freshness": freshness,
            "provenance": ["future_evidence_rejected"],
            "missing_reasons": ["future_evidence"],
        }
    price = context.get("price")
    quant = context.get("quant")
    last = _number(price, "last") or _number(quant, "price")
    change = _number(price, "change_pct")
    missing = [name for name, value in (("price_missing", last), ("change_missing", change)) if value is None]
    return {
        "symbol": symbol,
        "status": "available" if not missing and freshness["status"] == "fresh" else "degraded",
        "price": last,
        "change_pct": change,
        "freshness": freshness,
        "provenance": ["durable_latest_live_prediction", "saved_prediction_context_json"],
        "missing_reasons": missing,
    }


def _calendar(store: SQLiteStore, as_of: datetime) -> dict[str, object]:
    events = store.list_event_evidence(as_of=as_of.isoformat(), limit=100)
    rows: list[dict[str, object]] = []
    for event in events:
        event_at = _utc(event.get("event_at"))
        rows.append({
            "event_id": event.get("event_id"),
            "title": event.get("title"),
            "source": event.get("source", "unknown"),
            "category": event.get("category", "other"),
            "event_at": event_at.isoformat() if event_at else event.get("event_at"),
            "published_at": event.get("published_at"),
            "known_at": event.get("known_at"),
            "importance": event.get("importance", 0),
            "affected_symbols": event.get("affected_symbols", event.get("symbols", [])),
            "forecast": event.get("forecast"),
            "previous": event.get("previous"),
            "actual": event.get("actual"),
            "ai_view": event.get("ai_view"),
            "review": event.get("review"),
            "url": event.get("url"),
            "primary_source": bool(event.get("primary_source")),
            "capability": event.get("capability", {"status": "stored_evidence"}),
        })
    rows.sort(key=lambda row: (str(row.get("event_at") or ""), str(row.get("event_id") or "")))
    return {
        "status": "available" if rows else "unavailable",
        "events": rows,
        "missing_reasons": [] if rows else ["no_point_in_time_event_evidence"],
        "capabilities": {
            "macro_fields": "only_when_stored_by_provider",
            "crypto_derivatives_onchain": "unavailable_without_typed_provider_evidence",
            "ai_analysis": "explicit_only",
        },
    }


def _heatmap(records: Mapping[str, Mapping[str, Any]], as_of: datetime) -> dict[str, object]:
    cells: list[dict[str, object]] = []
    for symbol, (group, asset_type) in sorted(SECTOR_BY_SYMBOL.items()):
        record = records.get(symbol)
        pulse = _pulse_entry(symbol, record, as_of)
        cells.append({
            "symbol": symbol,
            "asset_type": asset_type,
            "group": group,
            "change_pct": pulse["change_pct"],
            "price": pulse["price"],
            "volume_strength": None,
            "relative_strength": None,
            "ai_explanation": None,
            "status": pulse["status"],
            "freshness": pulse["freshness"],
            "missing_reasons": [*pulse["missing_reasons"], "volume_strength_unavailable", "relative_strength_unavailable", "ai_explanation_not_generated"],
        })
    evidence_cells = [cell for cell in cells if cell["change_pct"] is not None]
    return {
        "status": "available" if evidence_cells else "unavailable",
        "cells": cells,
        "capability": "saved_prediction_context_only",
        "taxonomy_version": "market_taxonomy_v1",
    }


def build_market_intelligence(store: SQLiteStore, *, as_of: datetime | None = None) -> dict[str, object]:
    cutoff = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    all_symbols = {item.symbol for item in store.list_instruments()}
    all_symbols.update(entry["symbol"] for entry in store.list_watchlist_entries())
    records = store.list_latest_prediction_records(all_symbols | set(PULSE_SYMBOLS))
    realtime_records = {str(item.get("symbol")): item for item in store.list_realtime_states() if item.get("symbol")}
    for symbol, realtime in realtime_records.items():
        if symbol not in records:
            records[symbol] = {"realtime": realtime}
        else:
            records[symbol]["realtime"] = realtime
    pulse = [_pulse_entry(symbol, records.get(symbol), cutoff) for symbol in PULSE_SYMBOLS]
    watchlist: list[dict[str, object]] = []
    for entry in store.list_watchlist_entries():
        symbol = str(entry["symbol"])
        record = records.get(symbol)
        prediction = record.get("prediction") if isinstance(record, Mapping) else None
        context = _context(prediction) if isinstance(prediction, Mapping) else {}
        realtime = record.get("realtime") if isinstance(record, Mapping) else None
        watchlist.append({
            "symbol": symbol,
            "asset_type": next((item.asset_type.value for item in store.list_instruments() if item.symbol == symbol), None),
            "action": prediction.get("action") if isinstance(prediction, Mapping) else None,
            "prediction_id": prediction.get("prediction_id") if isinstance(prediction, Mapping) else None,
            "summary": prediction.get("summary") if isinstance(prediction, Mapping) else None,
            "raw_confidence": prediction.get("raw_confidence") if isinstance(prediction, Mapping) else None,
            "calibrated_confidence": prediction.get("calibrated_confidence") if isinstance(prediction, Mapping) else None,
            "market_regime": context.get("quant", {}).get("market_regime") if isinstance(context.get("quant"), Mapping) else None,
            "freshness": _freshness(prediction, context, cutoff) if isinstance(prediction, Mapping) else (_pulse_entry(symbol, {"realtime": realtime}, cutoff)["freshness"] if isinstance(realtime, Mapping) else {"status": "unavailable"}),
            "status": "monitoring" if isinstance(prediction, Mapping) else ("market_data_ready" if isinstance(realtime, Mapping) and _number(realtime, "price") is not None else "awaiting_analysis"),
        })
    predictions = [record["prediction"] for record in records.values() if isinstance(record.get("prediction"), Mapping)]
    predictions.sort(key=lambda value: (str(value.get("generated_at") or ""), str(value.get("prediction_id") or "")), reverse=True)
    latest_signal = predictions[0] if predictions else None
    calendar = _calendar(store, cutoff)
    heatmap = _heatmap(records, cutoff)
    news = [
        event for event in calendar["events"]
        if isinstance(event, dict) and str(event.get("category", "")).lower() in {"news", "earnings", "macro", "regulatory", "event"}
    ][:12]
    return {
        "contract_version": MARKET_INTELLIGENCE_VERSION,
        "as_of": cutoff.isoformat(),
        "read_only": True,
        "provider_calls": False,
        "domain_writes": False,
        "pulse": pulse,
        "calendar": calendar,
        "watchlist": watchlist,
        "latest_signal": latest_signal,
        "heatmap": heatmap,
        "news": {"status": "available" if news else "unavailable", "items": news, "missing_reasons": [] if news else ["no_stored_point_in_time_news"]},
        "daily_brief": store.latest_daily_brief(),
        "capabilities": {
            "market_pulse": "saved_prediction_context_only",
            "calendar": "stored_phase6_point_in_time_events",
            "watchlist_monitoring": "durable_watchlist_plus_latest_prediction",
            "heatmap": "typed_taxonomy_with_saved_change_only",
            "live_fetch_on_get": False,
            "public_cache": "sidecar_hydration_only",
        },
    }


def brief_source_evidence(view: Mapping[str, object]) -> tuple[str, list[str], list[str]]:
    bounded = {
        "as_of": view.get("as_of"),
        "pulse": view.get("pulse"),
        "calendar": view.get("calendar"),
        "watchlist": view.get("watchlist"),
        "heatmap": view.get("heatmap"),
        "news": view.get("news"),
    }
    serialized = json.dumps(bounded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sources = ["durable_watchlist", "latest_saved_live_predictions", "stored_phase6_events"]
    missing: list[str] = []
    if not any(item.get("price") is not None for item in view.get("pulse", []) if isinstance(item, Mapping)):
        missing.append("market_pulse_unavailable")
    calendar = view.get("calendar")
    if not isinstance(calendar, Mapping) or calendar.get("status") != "available":
        missing.append("calendar_unavailable")
    news = view.get("news")
    if not isinstance(news, Mapping) or news.get("status") != "available":
        missing.append("news_unavailable")
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest(), sources, missing
