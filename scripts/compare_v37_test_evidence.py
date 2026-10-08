"""Compare complete failing node IDs and pytest phases for V37 acceptance."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("PYTEST_EVIDENCE_SCHEMA_INVALID")
    return value


def _environment_sha(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _failed_phase_pairs(report: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (str(item.get("nodeid")), str(item.get("phase")))
        for item in report.get("phases", [])
        if isinstance(item, dict) and item.get("outcome") == "failed"
    }


def _phase_counts(pairs: set[tuple[str, str]]) -> dict[str, int]:
    return dict(sorted(Counter(phase for _, phase in pairs).items()))


def compare(
    baseline: dict[str, Any], current: dict[str, Any], *,
    baseline_commit: str = "23ca359082193033d9c40ee282c41a074eee53a2",
) -> dict[str, Any]:
    before = _failed_phase_pairs(baseline)
    after = _failed_phase_pairs(current)
    new = sorted(after - before)
    resolved = sorted(before - after)
    before_nodes = set(baseline.get("collected", []))
    after_nodes = set(current.get("collected", []))
    missing_tests = sorted(before_nodes - after_nodes)
    new_tests = sorted(after_nodes - before_nodes)
    before_by_node: dict[str, set[str]] = defaultdict(set)
    after_by_node: dict[str, set[str]] = defaultdict(set)
    for node, phase in before:
        before_by_node[node].add(phase)
    for node, phase in after:
        after_by_node[node].add(phase)
    phase_changes = [
        {"nodeid": node, "baseline_phases": sorted(before_by_node[node]),
         "current_phases": sorted(after_by_node[node])}
        for node in sorted(set(before_by_node) & set(after_by_node))
        if before_by_node[node] != after_by_node[node]
    ]
    baseline_complete = (
        baseline.get("session_finished") is True
        and baseline.get("exit_code") in (0, 1)
        and not baseline.get("internal_errors")
        and not baseline.get("interruptions")
    )
    current_complete = (
        current.get("session_finished") is True
        and current.get("exit_code") in (0, 1)
        and not current.get("internal_errors")
        and not current.get("interruptions")
    )
    same_environment = baseline.get("environment") == current.get("environment")
    no_new_failure = not new and not phase_changes
    status = "PASS_NO_NEW_FAILURES" if (
        baseline_complete and current_complete and same_environment and not missing_tests and no_new_failure
    ) else "FAIL"
    current_passed = set(current.get("sets", {}).get("passed", []))
    new_test_results = Counter(
        "PASSED" if node in current_passed else "NOT_PASSED"
        for node in new_tests
    )
    return {
        "schema_version": "pa-decision-quality-v37/full-suite-failure-comparison-1",
        "status": status,
        "runs_complete": baseline_complete and current_complete,
        "same_environment": same_environment,
        "environment_sha256": _environment_sha(current.get("environment")),
        "baseline": {
            "commit": baseline_commit,
            "collected_nodeid_count": len(before_nodes),
            "failed_node_phase_count": len(before),
            "failed_node_count": len({node for node, _ in before}),
            "failure_phase_counts": _phase_counts(before),
            "exit_code": baseline.get("exit_code"),
            "evidence_started_at": baseline.get("started_at"),
            "evidence_sha256": _environment_sha({
                "collected": baseline.get("collected"),
                "failed_node_phases": sorted(before),
                "environment": baseline.get("environment"),
            }),
        },
        "current": {
            "collected_nodeid_count": len(after_nodes),
            "failed_node_phase_count": len(after),
            "failed_node_count": len({node for node, _ in after}),
            "failure_phase_counts": _phase_counts(after),
            "exit_code": current.get("exit_code"),
            "evidence_started_at": current.get("started_at"),
            "evidence_sha256": _environment_sha({
                "collected": current.get("collected"),
                "failed_node_phases": sorted(after),
                "environment": current.get("environment"),
            }),
        },
        "comparison": {
            "missing_baseline_test_nodeids": missing_tests,
            "new_test_nodeids": new_tests,
            "new_test_results": dict(sorted(new_test_results.items())),
            "new_failure_node_phases": [
                {"nodeid": node, "phase": phase} for node, phase in new
            ],
            "resolved_baseline_failure_node_phases": [
                {"nodeid": node, "phase": phase} for node, phase in resolved
            ],
            "failure_phase_changes": phase_changes,
            "all_failure_nodeids_and_phases_match": before == after,
            "no_new_failure_nodeids_or_phases": no_new_failure,
        },
        "baseline_failure_nodeids_and_phases": [
            {"nodeid": node, "phase": phase} for node, phase in sorted(before)
        ],
        "current_failure_nodeids_and_phases": [
            {"nodeid": node, "phase": phase} for node, phase in sorted(after)
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("current", type=Path)
    parser.add_argument("--output", type=Path, required=True,
                        help="New local JSON report path; existing files are never overwritten")
    parser.add_argument("--baseline-commit", default="23ca359082193033d9c40ee282c41a074eee53a2",
                        help="Commit represented by the baseline evidence")
    args = parser.parse_args(argv)
    try:
        result = compare(
            _load(args.baseline), _load(args.current), baseline_commit=args.baseline_commit,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "INVALID_EVIDENCE", "error_type": type(exc).__name__}))
        return 2
    print(json.dumps({
        "status": result["status"],
        "baseline_failures": result["baseline"]["failed_node_count"],
        "current_failures": result["current"]["failed_node_count"],
        "new_failure_node_phases": len(result["comparison"]["new_failure_node_phases"]),
        "resolved_failure_node_phases": len(result["comparison"]["resolved_baseline_failure_node_phases"]),
        "phase_changes": len(result["comparison"]["failure_phase_changes"]),
        "new_test_count": len(result["comparison"]["new_test_nodeids"]),
        "output": str(args.output.resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS_NO_NEW_FAILURES" else 1


if __name__ == "__main__":
    raise SystemExit(main())
