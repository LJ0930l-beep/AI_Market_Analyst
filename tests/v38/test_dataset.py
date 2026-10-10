from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from itertools import pairwise

from core.replay.pa_decision_quality_v38.dataset import (
    EXPECTED_PARTITIONS,
    _annotate_overlap,
    load_v38_sample_plan,
    select_v38_calendar_points,
)


def test_v38_sample_plan_hash_and_frozen_boundaries_are_valid():
    plan, digest = load_v38_sample_plan()

    assert len(digest) == 64
    assert plan["labels_and_calls"]["future_outcome_labels_created"] is False
    assert plan["labels_and_calls"]["gemini_calls_authorized"] is False
    assert plan["untouched_test_handling"]["persist_market_input_payloads"] is False
    declared = {
        item["id"]: (item["start_utc_inclusive"], item["end_utc_exclusive"])
        for item in plan["partitions"]
    }
    assert declared == EXPECTED_PARTITIONS


def test_v38_calendar_selection_is_deterministic_stratified_and_outcome_blind():
    first = select_v38_calendar_points()
    second = select_v38_calendar_points()

    assert first == second
    assert len(first) == 180
    counts = Counter((item["symbol"], item["partition"]) for item in first)
    assert counts == Counter({(symbol, partition): 30 for symbol in ("BTCUSDT", "ETHUSDT")
                              for partition in EXPECTED_PARTITIONS})
    assert all(item["decision_time"].endswith("T12:00:00Z") for item in first)
    assert all(item["selection_key_version"] == "sha256(seed|symbol|partition|YYYY-MM-DD)" for item in first)


def test_v38_month_quotas_and_minimum_gap_are_enforced():
    points = select_v38_calendar_points()
    by_group = defaultdict(list)
    by_month = Counter()
    for item in points:
        timestamp = datetime.fromisoformat(item["decision_time"])
        by_group[(item["symbol"], item["partition"])].append(timestamp)
        by_month[(item["symbol"], item["partition"], timestamp.strftime("%Y-%m"))] += 1
    for symbol, partition in ((s, p) for s in ("BTCUSDT", "ETHUSDT") for p in EXPECTED_PARTITIONS):
        times = sorted(by_group[(symbol, partition)])
        assert all((right - left).total_seconds() >= 48 * 3600 for left, right in pairwise(times))
        per_month = 5 if partition == "optimization" else 10
        assert all(value == per_month for (sym, part, _), value in by_month.items()
                   if sym == symbol and part == partition)


def test_v38_overlap_metadata_uses_causal_eight_day_context_windows():
    points = _annotate_overlap(select_v38_calendar_points())

    assert len(points) == 180
    assert all(item["input_window_end_exclusive"] == item["decision_time"] for item in points)
    assert all(item["overlap_seconds_with_previous"] >= 0 for item in points)
    assert all(item["overlap_cluster_id"] for item in points)
