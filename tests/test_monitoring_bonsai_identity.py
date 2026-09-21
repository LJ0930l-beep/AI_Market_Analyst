from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.model_routing import DEFAULT_MODEL
from core.monitoring import MonitoringError, SmartModelUnavailable, SmartOpportunityAnalyzer
from core.providers.base import Bar


class _HealthProvider:
    def __init__(self, health: dict[str, object], receipt: dict[str, object] | None = None) -> None:
        self._health = health
        self._receipt = receipt

    def health(self):
        return self._health

    def generate_json(self, *_args, **_kwargs):
        now = datetime.now(timezone.utc)
        return (
            {
                "bias": "WAIT",
                "confidence": 0.0,
                "regime": "range",
                "watch_zone": None,
                "invalidation": [],
                "targets": [],
                "re_evaluate_at": (now + timedelta(minutes=15)).isoformat(),
            },
            "{}",
            self._receipt or {},
        )


def _healthy_manifest() -> dict[str, object]:
    return {
        "available": True,
        "model_available": True,
        "model_id": DEFAULT_MODEL,
        "actual_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
        "model_identity_source": "verified_manifest",
    }


def test_monitoring_rejects_non_bonsai_models_and_alias_only_health() -> None:
    with pytest.raises(ValueError, match="Bonsai 2 27B"):
        SmartOpportunityAnalyzer(None, smart_model="qwen3.5:9b")

    provider = _HealthProvider({"available": True, "models": [DEFAULT_MODEL], "model_id": DEFAULT_MODEL})
    analyzer = SmartOpportunityAnalyzer(provider)
    with pytest.raises(SmartModelUnavailable):
        analyzer._check_available()


def test_monitoring_requires_manifest_bound_completion_receipt() -> None:
    now = datetime.now(timezone.utc)
    bar = Bar(now, 100.0, 101.0, 99.0, 100.5, 10.0)
    analyzer = SmartOpportunityAnalyzer(_HealthProvider(_healthy_manifest()))
    with pytest.raises(MonitoringError) as caught:
        analyzer.analyze(
            {"trigger_event_id": "trigger-1"},
            [bar],
            symbol="BTCUSDT",
            timeframe="15m",
            data_as_of=now,
        )
    assert caught.value.code == "SMART_MODEL_IDENTITY_UNVERIFIED"

    receipt = {
        "model_id": DEFAULT_MODEL,
        "actual_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
        "verified_manifest_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
        "model_identity_source": "request_bound_to_verified_manifest",
    }
    analyzer = SmartOpportunityAnalyzer(_HealthProvider(_healthy_manifest(), receipt))
    analysis, _metadata = analyzer.analyze(
        {"trigger_event_id": "trigger-1"},
        [bar],
        symbol="BTCUSDT",
        timeframe="15m",
        data_as_of=now,
    )
    assert analysis.model_id == DEFAULT_MODEL
