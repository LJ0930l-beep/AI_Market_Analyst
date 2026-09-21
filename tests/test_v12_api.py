from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from core.instruments import instrument_for
from core.model_routing import DEFAULT_MODEL
from core.providers import Bar, ProviderError, Quote
from core.storage import SQLiteStore


POINT = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)


def make_bars() -> list[Bar]:
    bars: list[Bar] = []
    start = POINT - timedelta(minutes=15 * 64)
    for index in range(64):
        close = 100.0 + index * 0.5
        bars.append(Bar(start + timedelta(minutes=15 * index), close - 0.2, close + 0.4, close - 0.4, close, 100.0))
    last = bars[-1]
    bars[-1] = Bar(last.timestamp, last.open, last.close + 3.0, last.low, last.close + 2.0, 300.0)
    return bars


class FakePublicProvider:
    provider_name = "test_public_crypto"
    stale = False

    def __init__(self) -> None:
        self.bars = make_bars()

    def get_quote(self, instrument):
        return Quote(instrument, POINT, self.bars[-1].close, 300.0, 2.0, self.bars[-1].high, self.bars[-1].low)

    def get_bars(self, _instrument, _timeframe, limit=200):
        return self.bars[-limit:]


class FakeSmartProvider:
    def health(self):
        return {
            "available": True,
            "model_available": True,
            "model_id": DEFAULT_MODEL,
            "actual_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
            "model_identity_source": "verified_manifest",
        }

    def generate_json(self, _messages, **_kwargs):
        return {
            "bias": "LONG_WATCH",
            "confidence": 0.84,
            "regime": "bull_trend",
            "watch_zone": {"low": 131.0, "high": 133.0},
            "invalidation": {"price": 127.0, "conditions": ["closed below EMA20"]},
            "targets": [140.0, 146.0],
            "holding_horizon": "next_4h",
            "re_evaluate_at": (POINT + timedelta(minutes=30)).isoformat(),
            "evidence": ["closed-bar breakout"],
            "news_context": [],
            "event_risk": False,
            "missing_evidence": ["news unavailable in API test"],
        }, "{}", {
            "latency_ms": 1.0,
            "model_id": DEFAULT_MODEL,
            "actual_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
            "verified_manifest_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
            "model_identity_source": "request_bound_to_verified_manifest",
        }


class FailingProvider:
    provider_name = "unavailable_public_crypto"

    def get_quote(self, _instrument):
        raise ProviderError("provider unavailable", code="network_error", provider=self.provider_name)

    def get_bars(self, _instrument, _timeframe, _limit=200):
        raise AssertionError("quote failure should stop the provider request")


def test_v12_crypto_api_is_explicit_and_uses_persisted_cache(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "api.sqlite3")
    store.initialize()
    provider = FakePublicProvider()
    selected = {"provider": provider}
    app = create_app(
        store=store,
        llm_provider=FakeSmartProvider(),
        monitoring_provider_factory=lambda _instrument: selected["provider"],
        monitoring_clock=lambda: POINT,
    )
    client = TestClient(app)

    monitoring = client.get("/monitoring")
    assert monitoring.status_code == 200
    assert monitoring.json()["defaults"] == {
        "enabled": False,
        "primary_timeframe": "15m",
        "context_timeframe": "1h",
        "auto_start": False,
        "resume": False,
    }
    policy = monitoring.json()["policies"][0]
    assert policy["instrument_id"] == "BTCUSDT"
    assert policy["enabled"] is False

    updated = client.put(
        "/monitoring",
        json={
            "instrument_id": "BTCUSDT",
            "enabled": True,
            "primary_timeframe": "15m",
            "context_timeframe": "1h",
            "trigger_types": ["breakout", "regime", "volume"],
            "min_trigger_score": 0.65,
            "ai_min_confidence": 0.6,
            "cooldown_minutes": 60,
            "quiet_hours": {},
            "notify": {"desktop": True, "sound": False},
        },
    )
    assert updated.status_code == 200
    assert updated.json()["enabled"] is True

    run = client.post("/monitoring/run", json={"symbols": ["BTCUSDT"]})
    assert run.status_code == 200
    assert run.json()["items"][0]["status"] == "TRIGGERED"
    assert any(event["status"] == "ANALYZED" for event in client.get("/triggers").json()["events"])
    opportunities = client.get("/monitoring/opportunities", params={"symbol": "BTCUSDT"})
    assert opportunities.status_code == 200
    assert opportunities.json()["model_tier"] == "bonsai_27b_only"
    assert opportunities.json()["analyses"][0]["model_id"] == "Bonsai-2-27B-PTQ1_0"

    chart = client.get("/chart/BTCUSDT/bars", params={"timeframe": "15m", "limit": 60})
    assert chart.status_code == 200
    assert len(chart.json()["bars"]) == 60
    assert chart.json()["tradingview"]["backend_api"] is False
    realtime = client.get("/market/realtime/BTCUSDT")
    assert realtime.status_code == 200
    assert realtime.json()["capabilities"] == {
        "public_rest": True,
        "public_websocket": True,
        "accounts": False,
        "secrets": False,
        "orders": False,
    }

    selected["provider"] = FailingProvider()
    cached = client.get("/chart/BTCUSDT/bars", params={"timeframe": "15m", "limit": 10})
    assert cached.status_code == 200
    assert cached.json()["provider"]["provider"] == "cache"
    assert cached.json()["provider"]["stale"] is True
    assert cached.json()["provider"]["error_code"].startswith("live_unavailable:")


def test_v12_crypto_routes_reject_equities_and_invalid_policy_values(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "validation.sqlite3")
    store.initialize()
    app = create_app(store=store, llm_provider=None)
    client = TestClient(app)
    assert client.get("/chart/NVDA/bars").json()["error"]["code"] == "CRYPTO_SYMBOL_REQUIRED"
    invalid = client.put(
        "/monitoring",
        json={
            "instrument_id": "BTCUSDT",
            "enabled": "false",
            "primary_timeframe": "15m",
            "context_timeframe": "1h",
            "trigger_types": ["breakout"],
            "min_trigger_score": 0.65,
            "ai_min_confidence": 0.6,
            "cooldown_minutes": 60,
            "quiet_hours": {},
            "notify": {},
        },
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "INVALID_MONITORING_POLICY"
