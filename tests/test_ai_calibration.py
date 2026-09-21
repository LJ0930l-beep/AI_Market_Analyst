from datetime import UTC, datetime, timedelta

from core.storage import SQLiteStore
from core.trading.ai_calibration import CALIBRATION_PROFILE_VERSION, AICalibrationService


def _bar(symbol: str, end: datetime, close: float) -> dict[str, object]:
    return {"symbol": symbol, "bar_end": end.isoformat(), "close": close}


def test_calibration_replay_returns_are_grouped_by_symbol_and_contiguous_bar():
    start = datetime(2026, 9, 21, 0, 15, tzinfo=UTC)
    bars = [
        _bar("BTCUSDT", start, 100.0),
        _bar("ALTUSDT", start, 0.01),
        _bar("BTCUSDT", start + timedelta(minutes=15), 101.0),
        _bar("ALTUSDT", start + timedelta(minutes=15), 0.0102),
        # A missing candle must not be treated as a single 15m return.
        _bar("ALTUSDT", start + timedelta(minutes=45), 0.0104),
    ]

    summary = AICalibrationService._replay_summary(bars)

    assert summary["sample_size"] == 5
    assert summary["symbol_count"] == 2
    assert summary["return_observations"] == 2
    assert summary["positive_fraction"] == 1.0
    assert summary["mean_return"] == 0.015


def test_calibration_does_not_reuse_profile_from_previous_replay_semantics(tmp_path):
    store = SQLiteStore(tmp_path / "calibration.sqlite3")
    store.initialize()
    service = AICalibrationService(store)
    with store._connect() as db:
        db.execute(
            """INSERT INTO ai_calibration_profiles(
                profile_id, account_id, provider, environment, profile_version,
                model_id, digest_status, input_hash, sample_size, calibrated_at,
                expires_at, active, profile_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "old-profile", "gate_testnet", "gate", "testnet", "ai_operation_profile_v1",
                "Bonsai-2-27B-PTQ1_0", "OBSERVED_LOCAL_ARTIFACT_SHA256", "old-hash", 500,
                "2026-09-20T00:00:00+00:00", "2099-09-20T00:00:00+00:00", 1, "{}",
            ),
        )

    active = service.active_profile(
        "gate_testnet", environment="testnet", now=datetime(2026, 9, 21, tzinfo=UTC)
    )

    assert CALIBRATION_PROFILE_VERSION == "ai_operation_profile_v2"
    assert active is None
