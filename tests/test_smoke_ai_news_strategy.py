"""Bounded diagnostics for the isolated public-data/model smoke."""
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke-ai-news-strategy.py"
SPEC = importlib.util.spec_from_file_location("smoke_ai_news_strategy", SCRIPT)
smoke = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(smoke)


def test_model_audit_records_market_facts_without_storing_full_prompt():
    calls = []

    class FakeModel:
        def generate_json(self, messages, **kwargs):
            return (
                {
                    "action": "WAIT",
                    "instrument_id": "UNKNOWN",
                    "reason": "无法确认市场标的",
                    "confidence": None,
                },
                "bounded raw response",
                {"model_id": smoke.DEFAULT_SMART_MODEL, "actual_model_id": "bonsai-test"},
            )

    model = FakeModel()
    smoke._install_model_audit(model, calls)
    schema = {
        "type": "object",
        "required": ["action", "instrument_id", "reason", "confidence"],
        "properties": {
            "action": {"type": "string"},
            "instrument_id": {"type": "string"},
            "reason": {"type": "string"},
            "confidence": {"type": ["number", "null"]},
        },
    }
    payload = {
        "allowed_instruments": ["BTCUSDT"],
        "market_snapshots": {"BTCUSDT": {"price": 64001.25}},
        "technical_context": {
            "BTCUSDT": {"status": "READY", "timeframes": {"15m": {"status": "READY", "candles": [[1, 2]]}}}
        },
        "news_revisions": [{"id": "news-1"}],
        "candidates": [{"id": "candidate-1"}],
        "active_strategy": {"template_id": "strategy-1", "name": "Test", "style": "aggressive", "profile": {"signal_timeframe": "15m"}},
        "account_truth": {"status": "AVAILABLE", "source": "LOCAL_PAPER_LEDGER", "equity": 10000, "available_margin": 9000},
    }
    messages = [
        {"role": "system", "content": "PRIVATE SYSTEM PROMPT SHOULD NOT BE SAVED"},
        {"role": "user", "content": json.dumps(payload)},
    ]

    model.generate_json(messages, schema=schema, prompt_version="test-v1")

    assert len(calls) == 1
    audit = calls[0]
    assert audit["allowed_instruments"] == ["BTCUSDT"]
    assert audit["market_prices"] == {"BTCUSDT": 64001.25}
    assert audit["technical"] == {"BTCUSDT": {"15m": {"status": "READY", "bars": 1}}}
    assert audit["news_revision_count"] == 1
    assert audit["candidate_count"] == 1
    assert audit["strategy"]["template_id"] == "strategy-1"
    assert audit["account"]["source"] == "LOCAL_PAPER_LEDGER"
    assert audit["response"]["instrument_id"] == "UNKNOWN"
    assert audit["schema_result"] == "PASS"
    assert audit["coordinator_prevalidation_error"] is None
    serialized = json.dumps(audit, ensure_ascii=False)
    assert "PRIVATE SYSTEM PROMPT" not in serialized
    assert json.dumps(payload) not in serialized


def test_smoke_rejects_unknown_instrument_even_for_wait():
    with pytest.raises(RuntimeError, match="SMOKE_INVALID_INSTRUMENT_ID"):
        smoke._require_smoke_instrument("UNKNOWN", ("BTCUSDT",))

    smoke._require_smoke_instrument("btcusdt", ("BTCUSDT",))


def test_fake_provider_valid_wait_without_strategy_plan_is_a_complete_smoke():
    class FakeModel:
        def generate_json(self, _messages, **_kwargs):
            return ({
                "action": "WAIT",
                "instrument_id": "BTCUSDT",
                "reason": "新闻与技术条件暂不共振，等待下一周期。",
                "confidence": None,
            }, "", {"model_id": smoke.DEFAULT_SMART_MODEL})

    model = FakeModel()
    result, *_ = model.generate_json([], schema={})
    smoke._require_smoke_instrument(result["instrument_id"], ("BTCUSDT",))

    assert smoke._smoke_completion_status("WAIT", None, "READY", 4) == "PASS"
    assert smoke._smoke_completion_status("HOLD", None, "READY", 4) == "PASS"
    assert smoke._smoke_completion_status("OPEN_LONG", None, "READY", 4) == "PARTIAL"
