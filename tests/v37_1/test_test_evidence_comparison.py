from __future__ import annotations

from scripts.compare_v37_test_evidence import compare


def test_test_evidence_comparison_records_the_requested_baseline_commit():
    evidence = {
        "schema_version": 1,
        "session_finished": True,
        "exit_code": 0,
        "internal_errors": [],
        "interruptions": [],
        "environment": {"python": "same"},
        "collected": ["sample::test"],
        "phases": [{"nodeid": "sample::test", "phase": "call", "outcome": "passed"}],
        "sets": {"passed": ["sample::test"]},
    }

    result = compare(evidence, evidence, baseline_commit="608ee0083b9e7912c02fd3bf53d05087378f944f")

    assert result["status"] == "PASS_NO_NEW_FAILURES"
    assert result["baseline"]["commit"] == "608ee0083b9e7912c02fd3bf53d05087378f944f"
