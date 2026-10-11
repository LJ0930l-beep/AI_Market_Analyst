from __future__ import annotations

import json

import pytest

from core.ai.transport_diagnostics import CompletionTransportTrace
from core.model_routing import DEFAULT_MODEL
from core.replay.pa_decision_quality_v38.format_pilot import (
    FormatPilotSpec,
    _append_pilot_event,
    _write_report_exclusive,
    build_format_pilot_intent,
    run_format_pilot,
)
from core.replay.pa_decision_quality_v38.gemini_runner import ProviderCallResult
from core.replay.pa_decision_quality_v38.json_response import RAW_OR_SINGLE_JSON_FENCE_V1
from core.replay.pa_decision_quality_v38.market_only import build_market_only_input
from scripts import run_v38_json_format_pilot as format_pilot_cli


def _spec(market_input: dict) -> FormatPilotSpec:
    return FormatPilotSpec(
        authorization_id="TEST_FORMAT_PILOT_V1",
        dataset_id="TEST_DATASET_V1",
        dataset_manifest_sha256="1" * 64,
        visible_inputs_sha256="2" * 64,
        decision_id=market_input["decision_id"],
        market_input_sha256=market_input["market_input_sha256"],
        arm_id="A1_MARKET_ONLY",
        prompt_version="FORMAT_PILOT_TEST_V1",
        prompt_file_sha256="3" * 64,
        parser_version=RAW_OR_SINGLE_JSON_FENCE_V1,
    )


def _valid_analysis(evidence_refs: list[str]) -> dict:
    return {
        "schema_version": "pa-market-only-v38/analysis-1",
        "action": "WAIT",
        "market_view": {
            "market_regime": "UNCERTAIN",
            "higher_timeframe_bias": "UNCERTAIN",
            "location": "UNKNOWN",
            "signal": {"setup": "UNKNOWN", "quality": "UNKNOWN"},
            "indicative_bias": "UNKNOWN",
            "candidate_setup": {
                "kind": "UNASSESSED",
                "description": "The evidence does not establish a setup.",
                "evidence_refs": [],
            },
            "target_structure": {
                "kind": "UNKNOWN",
                "rationale": "No target structure is established.",
                "evidence_refs": [],
            },
            "counter_evidence": ["The supplied context is uncertain."],
            "decision_rationale": "Wait because the evidence is insufficient.",
            "evidence_refs": evidence_refs[:1],
            "wait_reason": "UNCERTAIN_CONTEXT",
        },
    }


def _call_result(messages: list[dict[str, str]], request_id: str, analysis: dict) -> ProviderCallResult:
    raw = "```json\n" + json.dumps(analysis, separators=(",", ":")) + "\n```"
    trace = CompletionTransportTrace(messages, 1, 30, request_id=request_id)
    trace.data["transport_mode"] = "JSON"
    trace.request_written(512, 0.01)
    trace.awaiting_headers()
    trace.headers_received(200)
    trace.body_received(len(raw))
    trace.completed()
    return ProviderCallResult(
        payload={
            "model": DEFAULT_MODEL,
            "choices": [{"message": {"content": raw}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        },
        response_model_id=DEFAULT_MODEL,
        transport_trace=trace.snapshot(),
    )


def _setup(market_point_factory, tmp_path):
    market_input = build_market_only_input(market_point_factory())
    spec = _spec(market_input)
    prompt = {"prompt_version": spec.prompt_version, "research_arm": spec.arm_id}
    messages = [
        {"role": "system", "content": "frozen format-pilot test prompt"},
        {"role": "user", "content": "fixture-only market input"},
    ]
    reports_root = tmp_path / "reports" / "v38+" / "runs"
    return market_input, prompt, messages, spec, reports_root


def test_dry_run_creates_no_intent_and_never_calls_model(market_point_factory, tmp_path):
    market_input, prompt, messages, spec, root = _setup(market_point_factory, tmp_path)
    calls = []
    report = run_format_pilot(
        market_input=market_input,
        prompt=prompt,
        messages=messages,
        ledger_path=root / "pilot.jsonl",
        report_path=root / "dry-run.json",
        repository_root=tmp_path,
        execute=False,
        call_model=lambda *_: calls.append(True),
        spec=spec,
    )
    assert report["status"] == "DRY_RUN"
    assert report["dispatch_intent_count"] == 0
    assert report["completion_intents_created"] == 0
    assert calls == []
    assert not (root / "pilot.jsonl").exists()


def test_shared_ledger_root_uses_git_common_directory(monkeypatch, tmp_path):
    common_dir = tmp_path / ".git"
    common_dir.mkdir()

    class Result:
        stdout = str(common_dir)

    monkeypatch.setattr(format_pilot_cli.subprocess, "run", lambda *args, **kwargs: Result())

    assert format_pilot_cli._shared_repository_root() == tmp_path


def test_cli_rejects_alternate_ledger_path():
    with pytest.raises(SystemExit) as error:
        format_pilot_cli.main(["--ledger", "reports/v38+/runs/alternate.jsonl"])

    assert error.value.code == 2


def test_valid_fenced_response_is_retained_raw_but_excluded_from_quality_metrics(
    market_point_factory, tmp_path,
):
    market_input, prompt, messages, spec, root = _setup(market_point_factory, tmp_path)
    calls = []

    def caller(sent_messages, request_id):
        calls.append(request_id)
        analysis = _valid_analysis(market_input["evidence_refs"])
        return _call_result(sent_messages, request_id, analysis)

    report = run_format_pilot(
        market_input=market_input,
        prompt=prompt,
        messages=messages,
        ledger_path=root / "pilot.jsonl",
        report_path=root / "result.json",
        repository_root=tmp_path,
        execute=True,
        call_model=caller,
        spec=spec,
    )
    assert report["status"] == "FORMAT_PILOT_ANALYSIS_VALID"
    assert report["dispatch_intent_count"] == report["terminal_result_count"] == 1
    assert report["quality_sample_eligible"] is False
    assert report["market_quality_claim_permitted"] is False
    assert report["profitability_claim_permitted"] is False
    assert report["orders_created"] == report["proposals_created"] == 0
    assert report["old_campaign_records_modified"] == report["old_campaign_records_reclassified"] == 0
    assert report["result"]["raw_response_text"].startswith("```json\n")
    assert report["result"]["analysis"] == _valid_analysis(market_input["evidence_refs"])
    assert len(calls) == 1


def test_existing_terminal_pilot_result_is_reviewed_without_a_second_call(
    market_point_factory, tmp_path,
):
    market_input, prompt, messages, spec, root = _setup(market_point_factory, tmp_path)
    calls = []

    def caller(sent_messages, request_id):
        calls.append(request_id)
        return _call_result(sent_messages, request_id, _valid_analysis(market_input["evidence_refs"]))

    ledger = root / "pilot.jsonl"
    first = run_format_pilot(
        market_input=market_input, prompt=prompt, messages=messages,
        ledger_path=ledger, report_path=root / "first.json", repository_root=tmp_path,
        execute=True, call_model=caller, spec=spec,
    )
    second = run_format_pilot(
        market_input=market_input, prompt=prompt, messages=messages,
        ledger_path=ledger, report_path=root / "second.json", repository_root=tmp_path,
        execute=True, call_model=lambda *_: pytest.fail("pilot must not be reissued"), spec=spec,
    )
    assert first["status"] == second["status"] == "FORMAT_PILOT_ANALYSIS_VALID"
    assert len(calls) == 1


def test_unmatched_intent_is_ambiguous_and_never_reissued(market_point_factory, tmp_path):
    market_input, prompt, messages, spec, root = _setup(market_point_factory, tmp_path)
    ledger = root / "pilot.jsonl"
    intent = build_format_pilot_intent(
        market_input, prompt, messages, run_id="a" * 32, request_id="b" * 32, spec=spec,
    )
    _append_pilot_event(ledger, intent, spec)
    report = run_format_pilot(
        market_input=market_input, prompt=prompt, messages=messages,
        ledger_path=ledger, report_path=root / "ambiguous.json", repository_root=tmp_path,
        execute=True, call_model=lambda *_: pytest.fail("ambiguous intent must never be retried"), spec=spec,
    )
    assert report["status"] == "AMBIGUOUS_NO_RESULT_NEVER_RETRY"
    assert report["dispatch_intent_count"] == 1
    assert report["terminal_result_count"] == 0


def test_mismatched_model_identity_is_not_marked_as_valid(market_point_factory, tmp_path):
    market_input, prompt, messages, spec, root = _setup(market_point_factory, tmp_path)

    def caller(sent_messages, request_id):
        result = _call_result(sent_messages, request_id, _valid_analysis(market_input["evidence_refs"]))
        result.payload["model"] = "different-model"
        return result

    report = run_format_pilot(
        market_input=market_input, prompt=prompt, messages=messages,
        ledger_path=root / "identity.jsonl", report_path=root / "identity.json",
        repository_root=tmp_path, execute=True, call_model=caller, spec=spec,
    )
    assert report["status"] == "FORMAT_PILOT_MODEL_IDENTITY_UNVERIFIED"
    assert report["result"]["analysis"] is None


def test_report_write_is_exclusive(market_point_factory, tmp_path):
    market_input, prompt, messages, spec, root = _setup(market_point_factory, tmp_path)
    report = run_format_pilot(
        market_input=market_input, prompt=prompt, messages=messages,
        ledger_path=root / "dry.jsonl", report_path=root / "dry.json",
        repository_root=tmp_path, execute=False, spec=spec,
    )
    path = root / "dry.json"
    _write_report_exclusive(path, report, repository_root=tmp_path)
    with pytest.raises(ValueError, match="FORMAT_PILOT_OUTPUT_EXISTS_REFUSE_OVERWRITE"):
        _write_report_exclusive(path, report, repository_root=tmp_path)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["report_sha256"]
