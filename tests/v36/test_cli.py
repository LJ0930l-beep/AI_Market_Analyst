from __future__ import annotations

import json
from pathlib import Path

from scripts.review_pa_decision_quality_v36 import _load, build_v25_coverage_report
from scripts.run_pa_decision_quality_v36 import main as run_main

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_v25_pilot_is_coverage_only_and_reconciles_source_aggregates():
    source = REPO_ROOT / "docs/research/v25-price-action-sample-20261008.json"
    sample, source_hash = _load(source)
    report = build_v25_coverage_report(sample, source_hash)
    assert report["status"] == "PILOT_NOT_RUN_INPUT_COVERAGE_ONLY"
    assert report["historical_descriptive_counts"]["completed_scans"] == 100
    assert report["historical_descriptive_counts"]["accepted_simulated_open_events"] == 13
    assert report["historical_descriptive_counts"]["complete_closed_trade_records"] == 10
    assert report["prior_v35_reference"]["net_reward_risk_below_2_0_under_v35_proxy_assumptions"] == 9
    assert report["experiment_status"]["A0"]["status"] == "NOT_RUN"
    assert report["experiment_status"]["A3"]["status"] == "NOT_RUN"
    assert report["counterfactual_feasibility"]["FIXED_NOTIONAL"]["eligible_count"] is None
    assert report["counterfactual_feasibility"]["RISK_BUDGETED_NOTIONAL"]["eligible_count"] is None


def test_offline_cli_writes_new_file_and_never_overwrites(tmp_path):
    source = tmp_path / "input.json"
    output = tmp_path / "report.json"
    source.write_text(json.dumps({"decision_points": []}), encoding="utf-8")
    assert run_main(["--input", str(source), "--output", str(output)]) == 0
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["run_manifest"]["mode"] == "OFFLINE_DEFAULT"
    assert saved["run_manifest"]["model_calls_used"] == 0
    assert run_main(["--input", str(source), "--output", str(output)]) == 2
    assert json.loads(output.read_text(encoding="utf-8")) == saved


def test_model_cli_requires_explicit_run_budget_and_injected_caller(tmp_path, capsys):
    source = tmp_path / "input.json"
    source.write_text(json.dumps({"decision_points": []}), encoding="utf-8")
    no_budget = run_main([
        "--input", str(source), "--output", str(tmp_path / "bad-budget.json"),
        "--run", "--model-id", "gemini-high",
    ])
    assert no_budget == 2
    assert "MODEL_CALL_REQUIRES_POSITIVE_MAX_DECISIONS" in capsys.readouterr().out

    no_caller = run_main([
        "--input", str(source), "--output", str(tmp_path / "no-caller.json"),
        "--run", "--max-decisions", "1", "--model-id", "gemini-high",
    ])
    assert no_caller == 2
    assert "MODEL_CALLER_NOT_CONFIGURED" in capsys.readouterr().out
