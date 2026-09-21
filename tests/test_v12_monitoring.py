from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.instruments import instrument_for
from core.monitoring import (
    MonitoringPolicy,
    MonitoringService,
    OpportunityAnalysis,
    RealtimeBarCache,
    trigger_candidates,
)
from core.providers import Bar, Quote
from core.storage import SQLiteStore

POINT = datetime(2030, 1, 2, 12, 0, tzinfo=UTC)


def make_bars() -> tuple[list[Bar], list[Bar]]:
    primary: list[Bar] = []
    start = POINT - timedelta(minutes=15 * 64)
    for index in range(64):
        close = 100.0 + index * 0.5
        primary.append(Bar(start + timedelta(minutes=15 * index), close - 0.2, close + 0.4, close - 0.4, close, 100.0))
    last = primary[-1]
    primary[-1] = Bar(last.timestamp, last.open, last.close + 3.0, last.low, last.close + 2.0, 300.0)
    context: list[Bar] = []
    context_start = POINT - timedelta(hours=30)
    for index in range(30):
        close = 100.0 + index * 1.2
        context.append(Bar(context_start + timedelta(hours=index), close - 0.4, close + 0.6, close - 0.6, close, 500.0))
    return primary, context


class FakeMarketProvider:
    provider_name = "test_public_crypto"
    stale = False

    def __init__(self) -> None:
        self.primary, self.context = make_bars()

    def get_quote(self, instrument):
        return Quote(instrument, POINT, self.primary[-1].close)

    def get_bars(self, instrument, timeframe, limit=200):
        return (self.primary if timeframe == "15m" else self.context)[-limit:]


class FakeSmartProvider:
    def __init__(self) -> None:
        self.calls = 0

    def health(self):
        return {
            "available": True,
            "model_available": True,
            "model_id": "Bonsai-2-27B-PTQ1_0",
            "actual_model_id": "Ternary-Bonsai-2-27B-PTQ1_0.gguf",
            "model_identity_source": "verified_manifest",
            "models": ["Ternary-Bonsai-2-27B-PTQ1_0.gguf"],
        }

    def generate_json(self, messages, **kwargs):
        self.calls += 1
        price = 132.0
        payload = {
            "bias": "LONG_WATCH",
            "confidence": 0.84,
            "regime": "bull_trend",
            "watch_zone": {"low": price * 0.995, "high": price * 1.005},
            "invalidation": {"price": price * 0.97, "conditions": ["closed below EMA20"]},
            "targets": [price * 1.06, price * 1.10],
            "holding_horizon": "next_4h",
            "re_evaluate_at": (POINT + timedelta(minutes=30)).isoformat(),
            "evidence": ["synthetic closed-bar breakout"],
            "news_context": [],
            "event_risk": False,
            "missing_evidence": ["news unavailable in deterministic test"],
        }
        return payload, json.dumps(payload, separators=(",", ":")), {
            "latency_ms": 1.0,
            "model_id": "Bonsai-2-27B-PTQ1_0",
            "actual_model_id": "Ternary-Bonsai-2-27B-PTQ1_0.gguf",
            "verified_manifest_model_id": "Ternary-Bonsai-2-27B-PTQ1_0.gguf",
            "model_identity_source": "completion_response",
            "prompt_version": kwargs.get("prompt_version"),
            "input_hash": kwargs.get("input_hash"),
            "parse_status": "valid",
        }


class InvalidLevelsSmartProvider(FakeSmartProvider):
    def generate_json(self, messages, **kwargs):
        payload, raw, metadata = super().generate_json(messages, **kwargs)
        payload["targets"] = [132.0 * 1.01, 132.0 * 1.02]
        return payload, raw, metadata


def seeded_store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    store.save_instrument(instrument_for("BTCUSDT"))
    return store


def enabled_policy() -> MonitoringPolicy:
    return replace(
        MonitoringPolicy.defaults("BTCUSDT"),
        enabled=True,
        trigger_types=("breakout", "regime", "volume"),
    )


def test_monitoring_runs_smart_only_after_closed_trigger_and_claims_once(tmp_path: Path) -> None:
    store = seeded_store(tmp_path / "monitoring.sqlite3")
    smart = FakeSmartProvider()
    provider = FakeMarketProvider()
    service = MonitoringService(
        store=store,
        provider_factory=lambda _instrument: provider,
        llm_provider=smart,
        clock=lambda: POINT,
    )
    service.upsert_policy(enabled_policy())

    first = service.run(now=POINT)
    assert first.status == "COMPLETED"
    assert first.items[0]["status"] == "TRIGGERED"
    assert smart.calls >= 1
    assert store.counts()["predictions"] >= 1
    assert store.counts()["opportunity_analyses"] == smart.calls
    assert all(item["status"] == "ANALYZED" for item in store.list_trigger_events(symbol="BTCUSDT"))

    second = service.run(now=POINT)
    assert second.items[0]["status"] == "NO_NEW_CLOSED_BAR"
    assert smart.calls == len(store.list_opportunity_analyses(symbol="BTCUSDT"))
    assert store.counts()["predictions"] == len(store.list_opportunity_analyses(symbol="BTCUSDT"))


def test_monitoring_never_substitutes_fast_model_when_smart_is_missing(tmp_path: Path) -> None:
    store = seeded_store(tmp_path / "no-smart.sqlite3")
    service = MonitoringService(
        store=store,
        provider_factory=lambda _instrument: FakeMarketProvider(),
        llm_provider=None,
        clock=lambda: POINT,
    )
    service.upsert_policy(enabled_policy())
    result = service.run(now=POINT)
    assert result.items[0]["status"] == "TRIGGERED"
    assert result.items[0]["events"]
    assert all(item["status"] == "DEGRADED" for item in result.items[0]["events"])
    assert all(item["smart_fallback"] is False for item in result.items[0]["events"])
    assert store.counts()["predictions"] == 0


def test_invalid_smart_levels_are_audited_as_quarantined(tmp_path: Path) -> None:
    store = seeded_store(tmp_path / "invalid-smart.sqlite3")
    service = MonitoringService(
        store=store,
        provider_factory=lambda _instrument: FakeMarketProvider(),
        llm_provider=InvalidLevelsSmartProvider(),
        clock=lambda: POINT,
    )
    service.upsert_policy(enabled_policy())

    result = service.run(now=POINT)

    assert result.items[0]["events"]
    assert all(item["status"] == "QUARANTINED" for item in result.items[0]["events"])
    analyses = store.list_opportunity_analyses(symbol="BTCUSDT")
    assert analyses
    assert all(item["validator_status"] == "QUARANTINED" for item in analyses)
    assert store.counts()["predictions"] == 0


def test_trigger_policy_and_cache_are_deterministic_and_bounded() -> None:
    primary, _ = make_bars()
    first = trigger_candidates(primary, symbol="BTCUSDT", trigger_types=("breakout", "regime"))
    second = trigger_candidates(primary, symbol="BTCUSDT", trigger_types=("breakout", "regime"))
    assert first == second
    assert {item["policy_version"] for item in first} == {"trigger_policy_v2"}

    bounded_cache = RealtimeBarCache(max_symbols=20, max_bars_per_symbol=100)
    for index in range(21):
        bounded_cache.put(f"COIN{index}USDT", "15m", primary)
    bounded_resource = bounded_cache.resource()
    assert bounded_resource["max_symbols"] == 20
    assert bounded_resource["active_symbols"] == 20
    assert bounded_resource["cached_bars"] <= 20 * 100

    cache = RealtimeBarCache(max_symbols=50, max_bars_per_symbol=100)
    for index in range(51):
        cache.put(f"COIN{index}USDT", "15m", primary)
    resource = cache.resource()
    assert resource["max_symbols"] == 50
    assert resource["active_symbols"] == 50
    assert resource["cached_bars"] <= 50 * 100


def test_opportunity_accepts_list_invalidation_without_optional_price() -> None:
    analysis = OpportunityAnalysis.from_payload(
        {
            "bias": "WAIT",
            "confidence": 0.42,
            "regime": "unclear",
            "watch_zone": None,
            "invalidation": [],
            "targets": [],
            "re_evaluate_at": (POINT + timedelta(minutes=15)).isoformat(),
            "evidence": [],
            "news_context": [],
            "event_risk": False,
            "missing_evidence": ["news"],
        },
        symbol="BTCUSDT",
        timeframe="15m",
        trigger_event_id="trigger-list-invalidation",
        model_id="Bonsai-2-27B-PTQ1_0",
        data_as_of=POINT,
    )
    assert analysis.invalidation_price is None
