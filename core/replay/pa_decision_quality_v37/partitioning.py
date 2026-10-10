"""Frozen calendar partitions copied from the V37-base research plan."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

FROZEN_PARTITION_POLICY_PATH = (
    Path(__file__).resolve().parents[3]
    / "configs" / "research" / "partitions" / "gemini-year-research-v1.json"
)
FROZEN_PARTITION_POLICY_SHA256 = "5169daa812c643cb35b40e3c5327bfbadf30b0c6b9c7db78b172de5c561f7ac7"
RESEARCH_PLAN_SOURCE_PATH = "core/replay/gemini_research.py"


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_time(value: Any) -> datetime:
    try:
        point = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("V37_PARTITION_TIME_INVALID") from exc
    if point.tzinfo is None or point.utcoffset() is None:
        raise ValueError("V37_PARTITION_TIMEZONE_REQUIRED")
    return point.astimezone(UTC)


def _partition_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("V37_PARTITION_LIST_INVALID")
    records = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise TypeError("V37_PARTITION_RECORD_INVALID")
        partition_id = item.get("id")
        if not isinstance(partition_id, str) or not partition_id or partition_id in seen:
            raise ValueError("V37_PARTITION_ID_INVALID")
        start, end = _parse_time(item.get("start")), _parse_time(item.get("end"))
        if start >= end:
            raise ValueError("V37_PARTITION_RANGE_INVALID")
        seen.add(partition_id)
        records.append({"id": partition_id, "start": start, "end": end})
    records.sort(key=lambda item: item["start"])
    for previous, current in pairwise(records):
        if previous["end"] != current["start"]:
            raise ValueError("V37_PARTITIONS_MUST_BE_CONTIGUOUS")
    return records


def _comparable_partitions(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"id": item["id"], "start": item["start"], "end": item["end"]}
        for item in records
    ]


def load_frozen_partition_policy(path: Path | None = None) -> tuple[dict[str, Any], str]:
    source_path = path or FROZEN_PARTITION_POLICY_PATH
    try:
        policy = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("V37_FROZEN_PARTITION_POLICY_UNAVAILABLE") from exc
    if not isinstance(policy, dict):
        raise TypeError("V37_FROZEN_PARTITION_POLICY_INVALID")
    digest = _canonical_sha256(policy)
    if digest != FROZEN_PARTITION_POLICY_SHA256:
        raise ValueError("V37_FROZEN_PARTITION_POLICY_HASH_MISMATCH")
    source = policy.get("source")
    if not isinstance(source, dict) or (
        source.get("commit") != "608ee0083b9e7912c02fd3bf53d05087378f944f"
        or source.get("path") != RESEARCH_PLAN_SOURCE_PATH
        or source.get("symbol") != "research_plan"
        or not isinstance(source.get("sha256"), str)
    ):
        raise ValueError("V37_FROZEN_PARTITION_SOURCE_INVALID")
    _partition_records(policy.get("partitions"))
    return policy, digest


def validate_research_plan(plan: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Require the executed original plan to match the version-pinned schedule."""
    policy, policy_sha256 = load_frozen_partition_policy()
    source_hashes = plan.get("source_sha256") if isinstance(plan, dict) else None
    if not isinstance(source_hashes, dict) or source_hashes.get(RESEARCH_PLAN_SOURCE_PATH) != policy["source"]["sha256"]:
        raise ValueError("V37_RESEARCH_PLAN_SOURCE_HASH_MISMATCH")
    archive_window = policy.get("archive_window")
    if not isinstance(archive_window, dict) or (
        _parse_time(plan.get("window_start")) != _parse_time(archive_window.get("start"))
        or _parse_time(plan.get("window_end")) != _parse_time(archive_window.get("end"))
    ):
        raise ValueError("V37_RESEARCH_PLAN_ARCHIVE_WINDOW_MISMATCH")

    actual_partitions = _partition_records(plan.get("partitions"))
    frozen_partitions = _partition_records(policy.get("partitions"))
    if (
        actual_partitions[0]["start"] != _parse_time(archive_window["start"])
        or actual_partitions[-1]["end"] != _parse_time(archive_window["end"])
    ):
        raise ValueError("V37_PARTITIONS_DO_NOT_COVER_ARCHIVE_WINDOW")
    if _comparable_partitions(actual_partitions) != _comparable_partitions(frozen_partitions):
        raise ValueError("V37_RESEARCH_PLAN_PARTITION_MISMATCH")
    if plan.get("pilot_windows") != policy.get("pilot_windows"):
        # The original plan returns +00:00 timestamps; this comparison intentionally
        # binds their exact dates, durations, and optimization labels.
        raise ValueError("V37_RESEARCH_PLAN_PILOT_WINDOWS_MISMATCH")
    if plan.get("heldout_protocol") != policy.get("heldout_protocol"):
        raise ValueError("V37_RESEARCH_PLAN_HELDOUT_PROTOCOL_MISMATCH")
    return policy, policy_sha256


def partition_for_time(decision_time: Any, partitions: list[dict[str, Any]]) -> str:
    point = _parse_time(decision_time)
    records = _partition_records(partitions)
    matches = [item["id"] for item in records if item["start"] <= point < item["end"]]
    if len(matches) != 1:
        raise ValueError("V37_DECISION_TIME_OUTSIDE_EXACTLY_ONE_PARTITION")
    return matches[0]


def validate_anchor_partitions(
    anchors: Iterable[tuple[str, str, str]], partitions: list[dict[str, Any]],
) -> list[dict[str, str]]:
    validated: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for decision_id, decision_time, declared_partition in anchors:
        if not isinstance(decision_id, str) or not decision_id or decision_id in seen_ids:
            raise ValueError("V37_ANCHOR_ID_INVALID_OR_DUPLICATE")
        resolved = partition_for_time(decision_time, partitions)
        if declared_partition != resolved:
            raise ValueError("V37_PARTITION_LABEL_MISMATCH")
        seen_ids.add(decision_id)
        validated.append({
            "decision_id": decision_id,
            "decision_time": _parse_time(decision_time).isoformat().replace("+00:00", "Z"),
            "partition": resolved,
        })
    return validated
