"""Contract tests for pure NOFX indicator snapshot calculations."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from core.trading.nofx_indicators import (
    MAX_BARS,
    build_indicator_snapshot,
    normalize_nofx_indicator_config,
)


def _bars(closes, volumes=None, *, start=None):
    start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    volumes = volumes or [10.0] * len(closes)
    rows = []
    for index, (close, volume) in enumerate(zip(closes, volumes)):
        rows.append({
            "timestamp": start + timedelta(minutes=5 * index),
            "open": float(close),
            "high": float(close) + 1.0,
            "low": float(close) - 1.0,
            "close": float(close),
            "volume": float(volume),
        })
    return rows


def _snapshot(bars, config, **kwargs):
    return build_indicator_snapshot(bars, config, source="gate_public_swap", timeframe="5m", **kwargs)


def test_normalizes_nofx_defaults_and_wrapped_indicator_config():
    normalized = normalize_nofx_indicator_config({"indicators": {"enable_ema": True}})

    assert normalized["enable_ema"] is True
    assert normalized["ema_periods"] == [20, 50]
    assert normalized["rsi_periods"] == [7, 14]
    assert normalized["atr_periods"] == [14]
    assert normalized["boll_periods"] == [20]
    assert normalized["volume_lookback"] == 20
    assert normalized["enable_oi"] is False


@pytest.mark.parametrize(
    "configuration",
    [
        None,
        {"enable_ema": 1},
        {"ema_periods": [True]},
        {"ema_periods": [1]},
        {"ema_periods": [501]},
        {"ema_periods": [2] * 9},
        {"ema_periods": []},
        {"volume_lookback": True},
    ],
)
def test_rejects_invalid_or_unbounded_configuration(configuration):
    with pytest.raises(ValueError):
        normalize_nofx_indicator_config(configuration)


def test_ema_rsi_atr_and_bollinger_return_latest_values():
    bars = _bars([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29])
    snapshot = _snapshot(bars, {
        "enable_ema": True, "ema_periods": [3],
        "enable_rsi": True, "rsi_periods": [3],
        "enable_atr": True, "atr_periods": [3],
        "enable_boll": True, "boll_periods": [3],
    })
    indicators = snapshot["indicators"]

    assert indicators["ema"]["periods"]["3"]["value"] == 28.0
    assert indicators["rsi"]["periods"]["3"]["value"] == 100.0
    assert indicators["atr"]["periods"]["3"]["value"] == 2.0
    assert indicators["bollinger"]["periods"]["3"] == {
        "status": "AVAILABLE", "upper": 29.6329931619,
        "middle": 28.0, "lower": 26.3670068381, "width_pct": 11.6642368704,
    }


def test_macd_uses_fixed_nofx_12_26_9_periods_and_reports_history_requirement():
    insufficient = _snapshot(_bars(list(range(100, 133))), {"enable_macd": True})
    available = _snapshot(_bars(list(range(100, 150))), {"enable_macd": True})

    assert insufficient["indicators"]["macd"] == {"status": "INSUFFICIENT_DATA", "required_bars": 34}
    result = available["indicators"]["macd"]
    assert result["status"] == "AVAILABLE"
    assert (result["fast_period"], result["slow_period"], result["signal_period"]) == (12, 26, 9)
    assert set(result["value"]) == {"line", "signal", "histogram"}
    assert result["value"]["line"] > 0


def test_volume_ratio_compares_current_bar_to_previous_baseline_only():
    snapshot = _snapshot(_bars([10, 11, 12], [100, 10, 20]), {"enable_volume": True, "volume_lookback": 2})

    assert snapshot["indicators"]["volume_ratio"] == {
        "status": "AVAILABLE", "current_volume": 20.0,
        "average_volume": 55.0, "lookback": 2, "ratio": 0.363636363636,
    }


def test_flat_rsi_has_defined_midpoint_and_zero_volume_baseline_is_unavailable():
    flat = _snapshot(_bars([10, 10, 10, 10]), {"enable_rsi": True, "rsi_periods": [2]})
    zero_volume = _snapshot(_bars([10, 11, 12], [0, 0, 0]), {"enable_volume": True, "volume_lookback": 2})

    assert flat["indicators"]["rsi"]["periods"]["2"]["value"] == 50.0
    assert zero_volume["indicators"]["volume_ratio"] == {
        "status": "UNAVAILABLE", "current_volume": 0.0,
        "average_volume": 0.0, "lookback": 2, "reason": "ZERO_BASELINE_VOLUME",
    }


def test_short_history_has_explicit_per_indicator_unavailable_status():
    snapshot = _snapshot(_bars([10, 11]), {
        "enable_ema": True, "ema_periods": [4],
        "enable_rsi": True, "rsi_periods": [4],
        "enable_atr": True, "atr_periods": [4],
        "enable_boll": True, "boll_periods": [4],
        "enable_volume": True, "volume_lookback": 4,
    })
    indicators = snapshot["indicators"]

    assert indicators["ema"]["periods"]["4"] == {"status": "INSUFFICIENT_DATA", "required_bars": 4}
    assert indicators["rsi"]["periods"]["4"] == {"status": "INSUFFICIENT_DATA", "required_bars": 5}
    assert indicators["atr"]["periods"]["4"] == {"status": "INSUFFICIENT_DATA", "required_bars": 5}
    assert indicators["bollinger"]["periods"]["4"] == {"status": "INSUFFICIENT_DATA", "required_bars": 4}
    assert indicators["volume_ratio"] == {"status": "INSUFFICIENT_DATA", "required_bars": 5}


def test_oi_and_funding_only_pass_through_verified_source_backed_values():
    config = {"enable_oi": True, "enable_funding_rate": True}
    snapshot = _snapshot(_bars([10, 11]), config, verified_derivatives={
        "open_interest": {
            "status": "AVAILABLE", "verified": True, "value": 1234.5,
            "source": "gate_public_derivatives", "as_of": "2026-01-01T00:05:00Z", "unit": "contracts",
        },
        "funding_rate": {
            "status": "AVAILABLE", "verified": True, "value": -0.0003,
            "source": "gate_public_derivatives", "as_of": "2026-01-01T00:05:00Z",
        },
    })

    assert snapshot["derivatives"]["open_interest"] == {
        "status": "AVAILABLE", "value": 1234.5,
        "source": "gate_public_derivatives", "as_of": "2026-01-01T00:05:00Z", "unit": "contracts",
    }
    assert snapshot["derivatives"]["funding_rate"]["value"] == -0.0003
    assert snapshot["derivatives"]["funding_rate"]["status"] == "AVAILABLE"


@pytest.mark.parametrize(
    ("input_record", "reason"),
    [
        (None, "NOT_SUPPLIED"),
        ({"status": "UNAVAILABLE", "value": 4, "verified": True}, "SOURCE_UNAVAILABLE"),
        ({"status": "AVAILABLE", "value": 4, "source": "test", "as_of": "2026-01-01T00:00:00Z"}, "UNVERIFIED_INPUT"),
        ({"status": "AVAILABLE", "verified": True, "value": 4, "source": "test"}, "PROVENANCE_REQUIRED"),
        ({"status": "AVAILABLE", "verified": True, "value": float("nan"), "source": "test", "as_of": "2026-01-01T00:00:00Z"}, "INVALID_VALUE"),
    ],
)
def test_missing_or_untrusted_derivatives_are_explicitly_unavailable(input_record, reason):
    snapshot = _snapshot(_bars([10, 11]), {"enable_oi": True}, verified_derivatives={"open_interest": input_record})

    assert snapshot["derivatives"]["open_interest"] == {"status": "UNAVAILABLE", "reason": reason}
    assert snapshot["derivatives"]["funding_rate"] == {"status": "DISABLED"}


@pytest.mark.parametrize(
    "bad_bar",
    [
        {"open": 1, "high": 1, "low": 1, "close": 1, "volume": float("nan")},
        {"open": 1, "high": float("inf"), "low": 1, "close": 1, "volume": 1},
        {"open": 1, "high": 0.9, "low": 0.8, "close": 1, "volume": 1},
        {"open": 1, "high": 1, "low": 1.1, "close": 1, "volume": 1},
        {"open": 0, "high": 1, "low": 0, "close": 1, "volume": 1},
        {"open": 1, "high": 1, "low": 1, "close": 1, "volume": -1},
        {"open": True, "high": 1, "low": 1, "close": 1, "volume": 1},
    ],
)
def test_rejects_non_finite_or_invalid_ohlcv(bad_bar):
    with pytest.raises(ValueError):
        _snapshot([bad_bar], {})


def test_rejects_duplicate_or_out_of_order_bar_timestamps():
    bars = _bars([10, 11, 12])
    bars[2]["timestamp"] = bars[1]["timestamp"]

    with pytest.raises(ValueError, match="OHLCV_BARS_NOT_OLDEST_FIRST"):
        _snapshot(bars, {})


def test_snapshot_has_provenance_is_json_safe_deterministic_and_bounded():
    bars = _bars(list(range(100, 160)))
    config = {"enable_ema": True, "enable_macd": True, "enable_rsi": True, "enable_atr": True, "enable_boll": True, "enable_volume": True}
    first = _snapshot(bars, config)
    second = _snapshot(bars, config)

    assert first == second
    assert first["source"] == "gate_public_swap"
    assert first["timeframe"] == "5m"
    assert first["as_of"] == "2026-01-01T04:55:00Z"
    assert first["bar_count"] == 60
    encoded = json.dumps(first, allow_nan=False, sort_keys=True)
    assert len(encoded) < 5000
    assert len(first["indicators"]["ema"]["periods"]) == 2
    assert all(len(group["periods"]) <= 8 for group in (
        first["indicators"]["ema"], first["indicators"]["rsi"],
        first["indicators"]["atr"], first["indicators"]["bollinger"],
    ))


def test_rejects_input_series_over_hard_bar_limit():
    with pytest.raises(ValueError, match="OHLCV_BARS_EXCEED_MAX"):
        _snapshot(_bars([10] * (MAX_BARS + 1)), {})


def test_snapshot_keeps_as_of_key_even_when_bar_time_is_missing():
    bar = {"open": 10, "high": 11, "low": 9, "close": 10, "volume": 0}
    snapshot = _snapshot([bar], {})

    assert "as_of" in snapshot
    assert snapshot["as_of"] is None
