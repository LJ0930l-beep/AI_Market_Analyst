"""Opt-in pytest evidence recorder; never changes test outcomes or exit status.

Record with ``-p scripts.v36_test_evidence --v36-report=NEW_PATH``.
Compare with ``python scripts/v36_test_evidence.py BEFORE AFTER``.
Reports deliberately omit captured output, exception messages, and locals.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _now():
    return datetime.now(UTC).isoformat()


def _diagnostic(value):
    """Identify an exception without exporting possibly sensitive assertion data."""
    return hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()


def environment():
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": dict(sorted(
            (dist.metadata["Name"], dist.version)
            for dist in importlib.metadata.distributions() if dist.metadata["Name"]
        )),
    }


class Evidence:
    def __init__(self, path, config):
        self.path = path
        self.data = {
            "schema_version": 1, "started_at": _now(), "finished_at": None,
            "environment": environment(), "pytest_version": pytest.__version__,
            "arguments": list(config.invocation_params.args),
            "collected": [], "deselected": [], "collection": [], "phases": [],
            "internal_errors": [], "interruptions": [], "exit_code": None,
            "session_finished": False,
        }

    def pytest_collection_finish(self, session):
        self.data["collected"] = [item.nodeid for item in session.items]

    def pytest_deselected(self, items):
        self.data["deselected"].extend(item.nodeid for item in items)

    def pytest_collectreport(self, report):
        item = {"nodeid": report.nodeid, "outcome": report.outcome}
        if report.longrepr:
            item["diagnostic_sha256"] = _diagnostic(report.longrepr)
        self.data["collection"].append(item)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        result = yield
        report = result.get_result()
        report.v36_exception_type = call.excinfo.typename if call.excinfo else None

    def pytest_runtest_logreport(self, report):
        item = {
            "nodeid": report.nodeid, "phase": report.when, "outcome": report.outcome,
            "duration_seconds": report.duration,
            "wasxfail": hasattr(report, "wasxfail"),
            "exception_type": getattr(report, "v36_exception_type", None),
        }
        if report.longrepr:
            item["diagnostic_sha256"] = _diagnostic(report.longrepr)
        self.data["phases"].append(item)

    def pytest_internalerror(self, excrepr, excinfo):
        self.data["internal_errors"].append({
            "exception_type": excinfo.typename, "diagnostic_sha256": _diagnostic(excrepr),
        })

    def pytest_keyboard_interrupt(self, excinfo):
        self.data["interruptions"].append({"exception_type": excinfo.typename})

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        self.data.update(finished_at=_now(), exit_code=int(session.exitstatus), session_finished=True)
        self.data["sets"] = outcome_sets(self.data)
        self.data["counts"] = {key: len(value) for key, value in self.data["sets"].items()}
        # Exclusive creation protects an immutable baseline from accidental reruns.
        with self.path.open("x", encoding="utf-8") as stream:
            json.dump(self.data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")


def pytest_addoption(parser):
    parser.addoption("--v36-report", metavar="NEW_PATH", help="Write immutable V36 test evidence")


def pytest_configure(config):
    destination = config.getoption("--v36-report")
    if destination:
        path = Path(destination)
        if path.exists():
            raise pytest.UsageError(f"V36 evidence already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        config.pluginmanager.register(Evidence(path, config), "v36-evidence-recorder")


def outcome_sets(data):
    groups = {key: set() for key in (
        "passed", "failed", "errors", "skipped", "xfail", "xpass",
        "collection_errors", "collection_skipped",
    )}
    for row in data["collection"]:
        key = {"failed": "collection_errors", "skipped": "collection_skipped"}.get(row["outcome"])
        if key:
            groups[key].add(row["nodeid"])
    for row in data["phases"]:
        node, outcome = row["nodeid"], row["outcome"]
        if outcome == "failed":
            groups["failed" if row["phase"] == "call" else "errors"].add(node)
        elif row["wasxfail"]:
            groups["xfail" if outcome == "skipped" else "xpass"].add(node)
        elif outcome == "skipped":
            groups["skipped"].add(node)
        elif row["phase"] == "call":
            groups["passed"].add(node)
    groups["passed"] -= groups["errors"]
    return {key: sorted(value) for key, value in groups.items()}


def compare(before, after):
    """Fail closed on hidden tests, phase errors, changed environments or incomplete runs."""
    left, right = outcome_sets(before), outcome_sets(after)
    added = {key: sorted(set(right[key]) - set(left[key])) for key in left}
    removed = {key: sorted(set(left[key]) - set(right[key])) for key in left}
    missing = list((Counter(before["collected"]) - Counter(after["collected"])).elements())
    extra = list((Counter(after["collected"]) - Counter(before["collected"])).elements())
    lost_passes = removed["passed"]
    before_phases = Counter((r["nodeid"], r["phase"]) for r in before["phases"])
    after_phases = Counter((r["nodeid"], r["phase"]) for r in after["phases"])
    missing_phases = sorted((before_phases - after_phases).elements())
    # A teardown error moving to setup on the same node is still a new error.
    left_errors = {(r["nodeid"], r["phase"]) for r in before["phases"] if r["outcome"] == "failed"}
    right_errors = {(r["nodeid"], r["phase"]) for r in after["phases"] if r["outcome"] == "failed"}
    new_phase_failures = sorted(right_errors - left_errors)
    complete = all(d.get("session_finished") and d.get("exit_code") in (0, 1)
                   and not d["internal_errors"] and not d["interruptions"] for d in (before, after))
    same_environment = before["environment"] == after["environment"]
    ok = (complete and same_environment and not missing and not extra and not lost_passes
          and not missing_phases and not new_phase_failures
          and before["deselected"] == after["deselected"]
          and not any(added[key] for key in ("failed", "errors", "skipped", "xfail", "xpass",
                                            "collection_errors", "collection_skipped")))
    return {
        "status": "PASS" if ok else "FAIL", "complete_runs": complete,
        "same_environment": same_environment, "missing_nodeids": sorted(missing),
        "extra_nodeids": sorted(extra), "lost_passes": lost_passes,
        "missing_phases": missing_phases, "new_phase_failures": new_phase_failures,
        "added": added, "removed": removed,
        "exit_codes": [before["exit_code"], after["exit_code"]],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args(argv)
    try:
        reports = [json.loads(path.read_text(encoding="utf-8")) for path in (args.before, args.after)]
        if any(report.get("schema_version") != 1 for report in reports):
            raise ValueError("Unsupported evidence schema")
        result = compare(*reports)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "INVALID_EVIDENCE", "error_type": type(exc).__name__}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
