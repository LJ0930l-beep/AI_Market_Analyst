from datetime import UTC, datetime

from core.model_routing import DEFAULT_SMART_MODEL
from core.storage import SQLiteStore
from core.trading import ai_calibration
from core.trading.ai_calibration import AICalibrationService


class _NoDigestProvider:
    model_id = DEFAULT_SMART_MODEL

    def __init__(self):
        self.generate_calls = 0

    def generate_json(self, *_args, **_kwargs):
        self.generate_calls += 1
        raise AssertionError("calibration must reject before model generation")


def test_missing_weight_digest_keeps_calibration_not_ready_without_model_call(
    tmp_path, monkeypatch
):
    now = datetime(2026, 10, 12, 21, 10, tzinfo=UTC)
    monkeypatch.setattr(
        ai_calibration,
        "_closed_bars",
        lambda *_args, **_kwargs: [
            {
                "symbol": "BTCUSDT",
                "bar_end": now.isoformat(),
                "close": 100.0,
            }
        ],
    )
    store = SQLiteStore(tmp_path / "calibration.sqlite3")
    store.initialize()
    service = AICalibrationService(store)
    provider = _NoDigestProvider()

    result = service.run(
        account_id="gate_testnet",
        provider="gate",
        environment="testnet",
        session_id="missing-weight-digest",
        symbols=("BTCUSDT",),
        model_provider=provider,
        now=now,
        min_bars=1,
    )

    assert result["status"] == "NOT_READY"
    assert result["error_code"] == "MODEL_DIGEST_UNAVAILABLE"
    assert result["digest_status"] == "UNKNOWN_NOT_PROVIDED"
    assert provider.generate_calls == 0
    assert service.active_profile(
        "gate_testnet", environment="testnet", now=now
    ) is None
