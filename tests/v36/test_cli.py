from __future__ import annotations

import hashlib
import json
from pathlib import Path

from core.replay.pa_decision_quality_v36.context import build_context
from core.replay.pa_decision_quality_v36.experiments import (
    EXPERIMENTS,
    exact_cache_identity,
    prompt_sha256,
    safe_state_snapshot,
)
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


def test_offline_cli_uses_model_id_only_for_a1_a2_exact_cache_matching(
    tmp_path, research_fixture, capsys,
):
    point = research_fixture["point"]()
    context = build_context(point["bars_by_timeframe"], point["decision_time"])
    state = safe_state_snapshot(point["state_snapshot"])
    state_sha = hashlib.sha256(json.dumps(
        state, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    decision_time = point["decision_time"].replace("+00:00", "Z")
    evidence_ref = min(context.evidence_refs)
    analysis = {
        "context": {"market_regime": "UNCERTAIN", "higher_timeframe_bias": "NEUTRAL"},
        "location": {"trade_location": "UNKNOWN"},
        "signal": {"setup": "NONE", "signal_quality": "NO_SIGNAL"},
        "action": "WAIT", "decision_rationale": "No confirmed entry trigger.",
        "counter_evidence": ["No structure has been confirmed."],
        "evidence_refs": [evidence_ref], "future_hypotheses": [],
    }
    cache_rows = []
    for experiment_id in ("A1", "A2"):
        spec = EXPERIMENTS[experiment_id]
        cache_rows.append({
            "identity": exact_cache_identity(
                experiment_id=experiment_id, model_id="cache-model-v1",
                prompt_sha=prompt_sha256(experiment_id, context, state, None, "FIXED_NOTIONAL"),
                data_sha=context.input_sha256, decision_time=decision_time,
                state_sha=state_sha, prompt_version=spec["prompt_version"],
                analysis_schema_version=spec["analysis_schema_version"],
            ),
            "status": "COMPLETED", "analysis": analysis,
            "analysis_schema_version": spec["analysis_schema_version"],
            "requested_model_id": "cache-model-v1", "actual_model_id": "cache-model-v1",
        })
    source = tmp_path / "frozen-input.json"
    output = tmp_path / "offline-cache-report.json"
    source.write_text(json.dumps({"decision_points": [point], "cache_rows": cache_rows}), encoding="utf-8")

    assert run_main([
        "--input", str(source), "--output", str(output),
        "--model-id", "cache-model-v1",
    ]) == 0
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["run_manifest"]["mode"] == "OFFLINE_DEFAULT"
    assert saved["run_manifest"]["model_calls_used"] == 0
    assert saved["run_manifest"]["model_id_role"] == "CACHE_MATCH_METADATA"
    for experiment_id in ("A1", "A2"):
        item = saved["decision_records"][0]["experiments"][experiment_id]
        assert item["status"] == "CACHE_MATCH"
        assert item["analysis_validation"]["status"] == "VALID"
        assert item["model_identity_status"] == "MATCHED"

    caller_output = tmp_path / "offline-caller-report.json"
    assert run_main([
        "--input", str(source), "--output", str(caller_output),
        "--model-id", "cache-model-v1", "--caller", "missing_provider:call",
    ]) == 2
    assert "MODEL_CALLER_REQUIRES_RUN_FLAG" in capsys.readouterr().out
    assert not caller_output.exists()


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
