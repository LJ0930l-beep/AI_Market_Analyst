from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scripts.v36_test_evidence import Evidence, compare, outcome_sets


def _report(*, collected=("test_a",), phases=(), collection=(), environment=None,
            exit_code=0, finished=True, internal_errors=(), interruptions=()):
    data = {
        "schema_version": 1,
        "environment": environment or {"python": "3.x", "packages": {"pytest": "x"}},
        "collected": list(collected),
        "deselected": [],
        "phases": list(phases),
        "collection": list(collection),
        "exit_code": exit_code,
        "session_finished": finished,
        "internal_errors": list(internal_errors),
        "interruptions": list(interruptions),
    }
    data["sets"] = outcome_sets(data)
    return data


def _phase(node, phase, outcome, *, wasxfail=False):
    return {"nodeid": node, "phase": phase, "outcome": outcome,
            "wasxfail": wasxfail, "exception_type": None}


def test_outcome_sets_separates_call_errors_and_collection_outcomes():
    report = _report(
        collected=("call_fail", "setup_error", "xfail", "skip"),
        phases=(
            _phase("call_fail", "call", "failed"),
            _phase("setup_error", "setup", "failed"),
            _phase("xfail", "call", "skipped", wasxfail=True),
            _phase("skip", "setup", "skipped"),
        ),
        collection=(
            {"nodeid": "collection_fail", "outcome": "failed"},
            {"nodeid": "collection_skip", "outcome": "skipped"},
        ),
    )
    sets = outcome_sets(report)
    assert sets["failed"] == ["call_fail"]
    assert sets["errors"] == ["setup_error"]
    assert sets["xfail"] == ["xfail"]
    assert sets["skipped"] == ["skip"]
    assert sets["collection_errors"] == ["collection_fail"]
    assert sets["collection_skipped"] == ["collection_skip"]


def test_compare_accepts_identical_complete_runs():
    baseline = _report(phases=(_phase("test_a", "call", "passed"),))
    assert compare(baseline, baseline)["status"] == "PASS"


@pytest.mark.parametrize(
    "after,field,expected",
    [
        (_report(collected=("test_a", "test_new")), "extra_nodeids", True),
        (_report(collected=(), phases=()), "missing_nodeids", True),
        (_report(phases=(_phase("test_a", "call", "failed"),), exit_code=1), "new_phase_failures", True),
        (_report(environment={"python": "different", "packages": {}}), "same_environment", False),
        (_report(finished=False), "complete_runs", False),
        (_report(internal_errors=({"exception_type": "RuntimeError"},)), "complete_runs", False),
    ],
)
def test_compare_fails_closed_on_incomplete_or_changed_runs(after, field, expected):
    baseline = _report(phases=(_phase("test_a", "call", "passed"),))
    result = compare(baseline, after)
    assert result["status"] == "FAIL"
    assert bool(result[field]) is expected


def test_recorder_stores_phase_errors_without_exporting_diagnostics(tmp_path):
    destination = tmp_path / "evidence.json"
    recorder = Evidence(destination, SimpleNamespace(invocation_params=SimpleNamespace(args=[])))
    recorder.pytest_collection_finish(SimpleNamespace(items=[SimpleNamespace(nodeid="test_secret")]))
    recorder.pytest_collectreport(SimpleNamespace(
        nodeid="test_collection", outcome="failed", longrepr="private diagnostic",
    ))
    recorder.pytest_runtest_logreport(SimpleNamespace(
        nodeid="test_secret", when="teardown", outcome="failed", duration=0.1,
        wasxfail=False, longrepr="private teardown diagnostic",
    ))
    recorder.pytest_sessionfinish(SimpleNamespace(exitstatus=1), 1)

    text = destination.read_text(encoding="utf-8")
    data = json.loads(text)
    assert data["session_finished"] is True
    assert data["exit_code"] == 1
    assert data["sets"]["errors"] == ["test_secret"]
    assert data["sets"]["collection_errors"] == ["test_collection"]
    assert "private diagnostic" not in text
    assert "private teardown diagnostic" not in text
    with pytest.raises(FileExistsError):
        recorder.pytest_sessionfinish(SimpleNamespace(exitstatus=1), 1)
