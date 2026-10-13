from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

import pytest

from core.replay.pa_decision_quality_v37.partitioning import (
    load_frozen_partition_policy,
    partition_for_time,
    validate_anchor_partitions,
    validate_research_plan,
)
from scripts.reconstruct_historical_evidence_v37 import CALENDAR_ANCHORS

FROZEN_PARTITIONS = (
    ("optimization", datetime(2025, 10, 1, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC)),
    ("validation", datetime(2026, 4, 1, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC)),
    ("untouched_test", datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)),
)


def _partition_for(decision_time: str) -> str | None:
    point = datetime.fromisoformat(decision_time).astimezone(UTC)
    matches = [name for name, start, end in FROZEN_PARTITIONS if start <= point < end]
    return matches[0] if len(matches) == 1 else None


def test_all_v37_pilot_anchors_belong_to_optimization_partition():
    actual = [(decision_time, declared, _partition_for(decision_time))
              for _, decision_time, declared in CALENDAR_ANCHORS]

    old_v37_dates = {"2025-10-15T12:00:00Z", "2025-12-14T12:00:00Z", "2026-02-12T12:00:00Z"}
    assert all(declared == resolved for _, declared, resolved in actual)
    assert [(when, declared) for when, declared, _ in actual[:3]] == [
        ("2025-10-15T12:00:00Z", "optimization"),
        ("2025-12-14T12:00:00Z", "optimization"),
        ("2026-02-12T12:00:00Z", "optimization"),
    ]
    assert old_v37_dates == {when for when, declared, _ in actual[:3] if declared == "optimization"}
    assert Counter(declared for _, declared, _ in actual) == {
        "optimization": 3, "validation": 3, "untouched_test": 3,
    }


def test_frozen_boundaries_are_half_open_and_cover_the_full_year():
    policy, _ = load_frozen_partition_policy()
    partitions = policy["partitions"]

    assert partition_for_time("2025-10-01T00:00:00Z", partitions) == "optimization"
    assert partition_for_time("2026-04-01T00:00:00Z", partitions) == "validation"
    assert partition_for_time("2026-07-01T00:00:00Z", partitions) == "untouched_test"
    with pytest.raises(ValueError, match="OUTSIDE_EXACTLY_ONE_PARTITION"):
        partition_for_time("2026-10-01T00:00:00Z", partitions)


def test_wrong_anchor_partition_label_is_rejected():
    policy, _ = load_frozen_partition_policy()

    with pytest.raises(ValueError, match="V37_PARTITION_LABEL_MISMATCH"):
        validate_anchor_partitions(
            [("mislabelled-pilot-2", "2025-12-14T12:00:00Z", "validation")],
            policy["partitions"],
        )


def test_research_plan_must_match_frozen_source_window_and_all_boundaries():
    policy, _ = load_frozen_partition_policy()
    plan = {
        "source_sha256": {policy["source"]["path"]: policy["source"]["sha256"]},
        "window_start": policy["archive_window"]["start"],
        "window_end": policy["archive_window"]["end"],
        "partitions": policy["partitions"],
        "pilot_windows": policy["pilot_windows"],
        "heldout_protocol": policy["heldout_protocol"],
    }
    verified, _ = validate_research_plan(plan)
    assert verified["partitions"] == policy["partitions"]

    uncovered = dict(plan)
    uncovered["partitions"] = [dict(item) for item in policy["partitions"]]
    uncovered["partitions"][-1]["end"] = "2026-09-30T00:00:00+00:00"
    with pytest.raises(ValueError, match="V37_PARTITIONS_DO_NOT_COVER_ARCHIVE_WINDOW"):
        validate_research_plan(uncovered)
