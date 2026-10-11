from __future__ import annotations

import json
from datetime import timedelta

import pytest

from core.replay.pa_decision_quality_v36.context import build_context
from core.replay.pa_decision_quality_v36.experiments import experiment_prompt


def test_all_four_timeframes_are_causal_and_swing_confirmation_is_later(research_fixture):
    context = build_context(research_fixture["frames"](inject_pivot=True), research_fixture["decision_time"])
    assert context.status == "READY"
    assert set(context.frames) == {"15m", "5m", "1h", "4h"}
    assert all(frame["status"] == "READY" for frame in context.frames.values())
    swings = context.frames["15m"]["objective_facts"]["confirmed_swings"]
    assert swings
    for swing in swings:
        assert swing["pivot_at"] < swing["confirmed_at"]
        assert swing["known_at"] < research_fixture["decision_time"].isoformat().replace("+00:00", "Z")
        assert set(swing["confirmation_evidence_refs"]).issubset(context.evidence_refs)


def test_future_rows_and_same_timestamp_rows_cannot_change_frozen_context(research_fixture):
    point = research_fixture["point"]()
    before = build_context(point["bars_by_timeframe"], point["decision_time"])
    altered = {key: list(value) for key, value in point["bars_by_timeframe"].items()}
    end = research_fixture["decision_time"]
    future = {
        "timeframe": "15m", "bar_start": end.isoformat(), "bar_end": (end + timedelta(minutes=15)).isoformat(),
        "available_at": (end + timedelta(minutes=15)).isoformat(), "is_closed": True,
        "quality_status": "FROZEN_RESEARCH", "source": "future-mutated",
        "open": 1e99, "high": 1e99, "low": 1e99, "close": 1e99, "volume": 1e99,
    }
    same_time = dict(future, bar_start=(end - timedelta(minutes=15)).isoformat(),
                     bar_end=end.isoformat(), available_at=end.isoformat(), high=-1)
    altered["15m"].extend((future, same_time))
    after = build_context(altered, point["decision_time"])
    assert after.input_sha256 == before.input_sha256
    assert after.prompt_payload() == before.prompt_payload()


def test_prompt_keeps_compact_causal_candles_and_stays_within_size_budget(research_fixture):
    point = research_fixture["point"]()
    context = build_context(point["bars_by_timeframe"], point["decision_time"])
    payload = experiment_prompt(
        "A2", context, point["state_snapshot"], research_fixture["risk_inputs"](),
    )
    limits = {"15m": 8, "5m": 6, "1h": 4, "4h": 4}
    for timeframe, limit in limits.items():
        candles = payload["causal_context"]["frames"][timeframe]["recent_candles"]
        assert len(candles) == limit
        assert all(candle["bar_end"] < payload["causal_context"]["decision_time"] for candle in candles)
    encoded_size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert encoded_size <= 32_000


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("duplicate", "DUPLICATE_BAR_END"),
        ("gap", "BAR_GAP_OR_DUPLICATE"),
        ("unsorted", "BAR_ORDER_INVALID"),
        ("bad_price", "BAR_OHLCV_GEOMETRY_INVALID"),
        ("missing_available", "BAR_TIME_UNKNOWN"),
    ],
)
def test_invalid_historical_rows_fail_closed(research_fixture, mutation, reason):
    frames = research_fixture["frames"]()
    rows = list(frames["15m"])
    if mutation == "duplicate":
        rows.insert(4, dict(rows[3]))
    elif mutation == "gap":
        rows.pop(10)
    elif mutation == "unsorted":
        rows[4], rows[5] = rows[5], rows[4]
    elif mutation == "bad_price":
        rows[5]["low"] = rows[5]["high"] + 1
    elif mutation == "missing_available":
        rows[5].pop("available_at")
    frames["15m"] = rows
    result = build_context(frames, research_fixture["decision_time"])
    assert result.status == "INCOMPLETE"
    assert result.frames["15m"]["reason"] == reason


def test_missing_background_frame_is_explicit(research_fixture):
    frames = research_fixture["frames"]()
    del frames["4h"]
    context = build_context(frames, research_fixture["decision_time"])
    assert context.status == "INCOMPLETE"
    assert context.frames["4h"]["reason"] == "BARS_MISSING"


def test_naive_decision_time_is_rejected(research_fixture):
    with pytest.raises(ValueError, match="DECISION_TIME_INVALID"):
        build_context(research_fixture["frames"](), "2025-01-05T11:55:00")
