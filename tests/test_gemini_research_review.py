"""Offline negative gates: held-out or incomplete data cannot tune strategies."""
import json
from pathlib import Path
import runpy

import pytest

from core.replay.ai_history import digest


def register(tmp_path, partition):
    plan = {"pilot_windows": [{"id": "fixture", "partition": partition}]}
    (tmp_path / "research-plan.json").write_text(json.dumps({"plan": plan, "plan_sha256": digest(plan)}), encoding="utf-8")
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/analyze_gemini_research.py"))["optimization_evidence"]


@pytest.mark.parametrize("partition", ["validation", "untouched_test"])
def test_review_refuses_held_out_results_before_reading_them(tmp_path, partition):
    review = register(tmp_path, partition)
    with pytest.raises(ValueError, match="REFUSES_VALIDATION_OR_TEST"):
        review(tmp_path)


def test_review_refuses_partial_pilot(tmp_path):
    review = register(tmp_path, "optimization")
    target = tmp_path / "fixture"
    target.mkdir()
    (target / "results.json").write_text(json.dumps({"status": "PAUSED"}), encoding="utf-8")
    (target / "ledger-audit.json").write_text(json.dumps({"status": "PASS"}), encoding="utf-8")
    with pytest.raises(ValueError, match="COMPLETE_AUDITED_WINDOWS"):
        review(tmp_path)
