from datetime import datetime, timedelta, timezone

from core.instruments import instrument_for
from core.providers.base import Bar, Quote
from core.storage import SQLiteStore
from core.trading.qwen_market_scanner import QwenMarketScanner


class _Market:
    provider_name = "gate_public_fixture"
    environment = "LIVE_PUBLIC"

    def get_quote(self, instrument):
        return Quote(instrument, datetime.now(timezone.utc), 100.0)

    def get_bars(self, instrument, timeframe, limit=24):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        return [
            Bar(now - timedelta(minutes=5), 99.0, 101.0, 98.0, 100.0, 10.0, bar_end=now, is_closed=True)
        ]


class _News:
    provider_name = "news_fixture"

    def get_events(self, instrument, limit=8):
        return []


class _Model:
    def health(self, *, model_name=None):
        return {"available": True, "model_available": True, "model_id": model_name, "weight_digest": "sha256:test"}

    def generate_json(self, messages, *, model_name, prompt_version, input_hash, temperature, schema):
        assert model_name == "qwen3.5:9b"
        assert "strategy" not in messages[1]["content"].lower()
        return (
            {
                "market_summary": "public market scan",
                "overall_bias": "NEUTRAL",
                "confidence": 50,
                "risk_flags": ["news unavailable"],
                "symbol_analysis": [{"symbol": "BTCUSDT", "bias": "NEUTRAL", "summary": "range", "key_levels": [99, 101]}],
            },
            "{}",
            {"model_id": model_name, "model_version": model_name, "latency_ms": 1.0, "schema_enforcement": "fixture", "parse_status": "valid"},
        )


def test_qwen_market_scanner_persists_model_analysis_without_order_or_strategy(tmp_path):
    store = SQLiteStore(tmp_path / "qwen-scanner.db")
    store.initialize()
    scanner = QwenMarketScanner(store, _Model(), market_provider=_Market(), news_provider=_News())

    result = scanner.run_once(["BTCUSDT"])

    assert result["status"] == "COMPLETED"
    assert result["payload"]["decision_origin"] == "MODEL_ANALYSIS"
    assert result["payload"]["execution"] == "NOT_REQUESTED"
    assert result["payload"]["order_created"] is False
    assert result["payload"]["analysis"]["overall_bias"] == "NEUTRAL"
    assert scanner.history() == [result]
    with store._connect() as db:
        row = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_intents'").fetchone()
        assert row is None
