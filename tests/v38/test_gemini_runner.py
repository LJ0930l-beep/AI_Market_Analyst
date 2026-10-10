from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.ai.transport_diagnostics import CompletionTransportTrace
from core.model_routing import DEFAULT_MODEL
from core.replay.pa_decision_quality_v38.gemini_runner import (
    ANALYSIS_JSON_SCHEMA,
    AUTHORIZATION_ID,
    PROMPT_HASHES,
    ROUTE_READINESS_SCHEMA_VERSION,
    ProviderCallResult,
    _call_key,
    _read_ledger,
    _result_event,
    build_model_messages,
    canonical_sha256,
    execute_authorized_optimization,
    load_authorization,
    load_prompt_templates,
    review_gemini_ledger,
)
from core.replay.pa_decision_quality_v38.market_only import (
    build_market_only_input,
    validate_stored_market_only_input,
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
                "description": "The supplied evidence does not establish a confirmed setup.",
                "evidence_refs": [],
            },
            "target_structure": {
                "kind": "UNKNOWN",
                "rationale": "No supported target structure can be identified from this evidence.",
                "evidence_refs": [],
            },
            "counter_evidence": ["The evidence is insufficient to establish directional follow-through."],
            "decision_rationale": "Wait because the supplied causal context is uncertain.",
            "evidence_refs": evidence_refs[:1],
            "wait_reason": "UNCERTAIN_CONTEXT",
        },
    }


def _fixture_call(messages: list[dict[str, str]], request_id: str) -> ProviderCallResult:
    payload_in = json.loads(messages[1]["content"])
    refs = payload_in["market_input"]["evidence_refs"]
    analysis = _valid_analysis(refs)
    trace = CompletionTransportTrace(messages, 1, 30, request_id=request_id)
    trace.data["transport_mode"] = "JSON"
    trace.request_written(512, 0.01)
    trace.awaiting_headers()
    trace.headers_received(200)
    trace.body_received(256)
    trace.completed()
    return ProviderCallResult(
        payload={
            "model": DEFAULT_MODEL,
            "choices": [{"message": {"content": json.dumps(analysis)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 300, "completion_tokens": 120, "total_tokens": 420},
        },
        response_model_id=DEFAULT_MODEL,
        transport_trace=trace.snapshot(),
    )


def _route_reauthorization_evidence() -> dict[str, object]:
    catalog_bytes = json.dumps([DEFAULT_MODEL], separators=(",", ":")).encode("utf-8")
    return {
        "schema_version": ROUTE_READINESS_SCHEMA_VERSION,
        "endpoint": "http://127.0.0.1:8045/v1/models",
        "http_status": 200,
        "model_id": DEFAULT_MODEL,
        "model_listed": True,
        "model_catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "checked_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "operator_confirmed_route_change": True,
    }


def _setup(root: Path, market_point_factory):
    auth = load_authorization(
        root / "configs" / "research" / "authorizations" / "v38-gemini-optimization-run-20261010-v1.json",
    )
    prompts = load_prompt_templates(root)
    market_input = build_market_only_input(market_point_factory())
    return auth, prompts, market_input


@pytest.mark.parametrize("invalid_refs", [[""], [f"bar:15m:{'0' * 16}"]])
def test_executor_rejects_evidence_refs_not_backed_by_market_context_before_provider_call(
    tmp_path, market_point_factory, invalid_refs,
):
    root = Path(__file__).resolve().parents[2]
    auth, prompts, original_input = _setup(root, market_point_factory)
    market_input = deepcopy(original_input)
    market_input["evidence_refs"] = invalid_refs
    market_input["market_input_sha256"] = canonical_sha256({
        key: value for key, value in market_input.items() if key != "market_input_sha256"
    })

    assert not validate_stored_market_only_input(market_input)

    calls: list[str] = []
    ledger = tmp_path / "model-call-ledger.jsonl"
    with pytest.raises(ValueError, match="GEMINI_RUN_OPTIMIZATION_INPUT_INVALID"):
        execute_authorized_optimization(
            [market_input], prompts, authorization=auth, ledger_path=ledger,
            call_model=lambda messages, request_id: (
                calls.append(request_id) or _fixture_call(messages, request_id)
            ), max_contexts=1,
        )

    assert calls == []
    assert not ledger.exists()


def test_frozen_authorization_and_prompt_templates_pin_the_expected_study(tmp_path_factory, market_point_factory):
    root = Path(__file__).resolve().parents[2]
    auth, prompts, _ = _setup(root, market_point_factory)

    assert auth["authorization_id"] == AUTHORIZATION_ID
    assert auth["model_id"] == DEFAULT_MODEL
    assert auth["research_scope"]["allowed_partition"] == "optimization"
    assert auth["research_scope"]["maximum_model_request_intents"] == 72
    assert auth["research_scope"]["allow_validation"] is False
    assert auth["research_scope"]["allow_untouched_test"] is False
    assert auth["duplicate_cost_policy"]["provider_internal_deduplication"] == "UNVERIFIED"
    assert set(prompts) == {"A1_MARKET_ONLY", "A2_MARKET_ONLY"}
    assert PROMPT_HASHES["A1_MARKET_ONLY"] != PROMPT_HASHES["A2_MARKET_ONLY"]


def test_executor_rejects_invalid_authorization_before_creating_intent_or_calling_model(
    tmp_path, market_point_factory,
):
    root = Path(__file__).resolve().parents[2]
    auth, prompts, market_input = _setup(root, market_point_factory)
    auth["model_id"] = "unapproved-model"
    calls: list[str] = []
    ledger = tmp_path / "model-call-ledger.jsonl"

    with pytest.raises(ValueError, match="GEMINI_RUN_AUTHORIZATION_CONTRACT_INVALID"):
        execute_authorized_optimization(
            [market_input], prompts, authorization=auth, ledger_path=ledger,
            call_model=lambda _messages, request_id: calls.append(request_id), max_contexts=1,
        )

    assert calls == []
    assert not ledger.exists()


def test_executor_rejects_substituted_prompt_mapping(market_point_factory):
    root = Path(__file__).resolve().parents[2]
    _, prompts, market_input = _setup(root, market_point_factory)
    prompts["A1_MARKET_ONLY"]["task"] += " altered after loading"

    with pytest.raises(ValueError, match="GEMINI_RUN_PROMPT_MAPPING_NOT_FROZEN"):
        review_gemini_ledger([market_input], prompts, [])


def test_model_messages_redact_absolute_dates_keep_causal_evidence_and_prohibit_execution(
    market_point_factory,
):
    root = Path(__file__).resolve().parents[2]
    _, prompts, market_input = _setup(root, market_point_factory)
    messages = build_model_messages(market_input, prompts["A1_MARKET_ONLY"])
    wire = json.dumps(messages, ensure_ascii=False)

    assert "2025-10-15" not in wire
    assert "decision_id" not in wire
    assert "evidence_refs" in wire
    assert market_input["market_input_sha256"] in wire
    assert "orders" in wire.lower()
    assert "output_schema" in wire
    assert ANALYSIS_JSON_SCHEMA["properties"]["action"]["enum"] == ["OBSERVE", "WAIT"]


def test_authorized_runner_is_single_attempt_append_only_and_does_not_change_trade_lifecycle(
    tmp_path, market_point_factory,
):
    root = Path(__file__).resolve().parents[2]
    auth, prompts, market_input = _setup(root, market_point_factory)
    ledger = tmp_path / "model-call-ledger.jsonl"
    calls: list[str] = []

    def call(messages, request_id):
        calls.append(request_id)
        return _fixture_call(messages, request_id)

    first = execute_authorized_optimization(
        [market_input], prompts, authorization=auth, ledger_path=ledger, call_model=call, max_contexts=1,
    )
    assert len(calls) == 2
    assert len(set(calls)) == 2
    assert first["planned_attempt_denominator"] == 2
    assert first["dispatch_intent_count"] == 2
    assert first["terminal_result_count"] == 2
    assert first["valid_analysis_records"] == 2
    assert first["provider_usage_tokens_reported"]["total_tokens"] == 840
    assert first["provider_cost_usdt"] is None
    assert first["orders_created"] == 0
    assert first["proposals_created"] == 0
    assert first["profitability_claim_permitted"] is False
    assert all(row["trade_lifecycle"]["proposal"] == "NOT_CREATED" for row in first["records"])

    second = execute_authorized_optimization(
        [market_input], prompts, authorization=auth, ledger_path=ledger, call_model=call, max_contexts=1,
    )
    assert len(calls) == 2
    assert second["status_counts"] == {"VALID_ANALYSIS": 2}
    events = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [event["event_type"] for event in events] == [
        "DISPATCH_INTENT", "RESULT", "DISPATCH_INTENT", "RESULT",
    ]
    assert all(event["request_id"] in calls for event in events)
    assert [event["ledger_sequence"] for event in events] == [1, 2, 3, 4]
    assert all(len(event["event_sha256"]) == 64 for event in events)

    tampered_chain = deepcopy(events)
    tampered_chain[0]["symbol"] = "ETHUSDT"
    with pytest.raises(ValueError, match="GEMINI_RUN_LEDGER_HASH_CHAIN_INVALID"):
        review_gemini_ledger([market_input], prompts, tampered_chain)

    events[-1]["transport_trace"]["request_bytes_written"] = 0
    unsigned_last_event = {key: value for key, value in events[-1].items() if key != "event_sha256"}
    events[-1]["event_sha256"] = canonical_sha256(unsigned_last_event)
    with pytest.raises(ValueError, match="GEMINI_RUN_VALID_ANALYSIS_PROVENANCE_INVALID"):
        review_gemini_ledger([market_input], prompts, events)


def test_crash_after_intent_is_ambiguous_and_never_reissues_same_context_arm(
    tmp_path, market_point_factory,
):
    root = Path(__file__).resolve().parents[2]
    auth, prompts, market_input = _setup(root, market_point_factory)
    ledger = tmp_path / "model-call-ledger.jsonl"
    dispatched: list[str] = []

    def crash_after_dispatch_intent(_messages, request_id):
        dispatched.append(request_id)
        raise KeyboardInterrupt("simulated process interruption")

    with pytest.raises(KeyboardInterrupt, match="simulated process interruption"):
        execute_authorized_optimization(
            [market_input], prompts, authorization=auth, ledger_path=ledger,
            call_model=crash_after_dispatch_intent, max_contexts=1,
        )
    assert len(dispatched) == 1

    resumed_calls: list[str] = []
    report = execute_authorized_optimization(
        [market_input], prompts, authorization=auth, ledger_path=ledger,
        call_model=lambda messages, request_id: (resumed_calls.append(request_id)
            or _fixture_call(messages, request_id)),
        max_contexts=1,
    )
    assert len(resumed_calls) == 1
    assert resumed_calls[0] not in dispatched
    assert report["status_counts"] == {
        "AMBIGUOUS_NO_RESULT_NEVER_RETRY": 1,
        "VALID_ANALYSIS": 1,
    }
    assert report["dispatch_intent_count"] == 2


@pytest.mark.parametrize("response_model", [None, "gemini-3.8-flash-control"])
def test_nonmatching_or_missing_response_model_is_not_counted_as_valid(
    tmp_path, market_point_factory, response_model,
):
    root = Path(__file__).resolve().parents[2]
    auth, prompts, market_input = _setup(root, market_point_factory)
    ledger = tmp_path / "model-call-ledger.jsonl"

    def mismatched(messages, request_id):
        fixture = _fixture_call(messages, request_id)
        payload = dict(fixture.payload)
        payload["model"] = response_model
        return ProviderCallResult(payload, response_model, fixture.transport_trace)

    report = execute_authorized_optimization(
        [market_input], prompts, authorization=auth, ledger_path=ledger,
        call_model=mismatched, max_contexts=1,
    )
    expected = "MODEL_IDENTITY_UNVERIFIED" if response_model is None else "MODEL_IDENTITY_MISMATCH"
    assert report["status_counts"] == {expected: 2}
    assert report["valid_analysis_records"] == 0


def test_analysis_requires_a_completed_bound_json_transport_trace(tmp_path, market_point_factory):
    root = Path(__file__).resolve().parents[2]
    auth, prompts, market_input = _setup(root, market_point_factory)
    ledger = tmp_path / "model-call-ledger.jsonl"

    def unproven_transport(messages, request_id):
        fixture = _fixture_call(messages, request_id)
        trace = dict(fixture.transport_trace)
        trace["request_bytes_written"] = 0
        return ProviderCallResult(fixture.payload, DEFAULT_MODEL, trace)

    report = execute_authorized_optimization(
        [market_input], prompts, authorization=auth, ledger_path=ledger,
        call_model=unproven_transport, max_contexts=1,
    )

    assert report["status_counts"] == {"INVALID_TRANSPORT_EVIDENCE": 2}
    assert report["valid_analysis_records"] == 0


@pytest.mark.parametrize("request_bytes_written", [1.0, 1.5])
def test_runner_and_review_reject_noninteger_transport_byte_counts(
    tmp_path,
    market_point_factory,
    request_bytes_written,
):
    root = Path(__file__).resolve().parents[2]
    auth, prompts, market_input = _setup(root, market_point_factory)
    ledger = tmp_path / "model-call-ledger.jsonl"

    def noninteger_transport(messages, request_id):
        fixture = _fixture_call(messages, request_id)
        trace = dict(fixture.transport_trace)
        trace["request_bytes_written"] = request_bytes_written
        return ProviderCallResult(fixture.payload, fixture.response_model_id, trace)

    report = execute_authorized_optimization(
        [market_input],
        prompts,
        authorization=auth,
        ledger_path=ledger,
        call_model=noninteger_transport,
        max_contexts=1,
    )

    assert report["status_counts"] == {"INVALID_TRANSPORT_EVIDENCE": 2}
    events = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    reviewed = review_gemini_ledger([market_input], prompts, events)
    assert reviewed["status_counts"] == {"INVALID_TRANSPORT_EVIDENCE": 2}
    assert reviewed["transport_request_bytes_written_count"] == 0


def test_ledger_review_rejects_rehashed_valid_analysis_with_fractional_byte_count(
    tmp_path,
    market_point_factory,
):
    root = Path(__file__).resolve().parents[2]
    auth, prompts, market_input = _setup(root, market_point_factory)
    ledger = tmp_path / "model-call-ledger.jsonl"
    execute_authorized_optimization(
        [market_input],
        prompts,
        authorization=auth,
        ledger_path=ledger,
        call_model=_fixture_call,
        max_contexts=1,
    )
    events = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    for event in events:
        if event["event_type"] == "RESULT":
            event["transport_trace"]["request_bytes_written"] = 1.5

    previous_digest = "0" * 64
    for event in events:
        event["previous_event_sha256"] = previous_digest
        unsigned = {key: value for key, value in event.items() if key != "event_sha256"}
        event["event_sha256"] = canonical_sha256(unsigned)
        previous_digest = event["event_sha256"]

    with pytest.raises(ValueError, match="GEMINI_RUN_VALID_ANALYSIS_PROVENANCE_INVALID"):
        review_gemini_ledger([market_input], prompts, events)


def test_model_call_error_keeps_only_safe_model_code_and_current_trace(tmp_path, market_point_factory):
    from core.model_client import ModelClientError

    root = Path(__file__).resolve().parents[2]
    auth, prompts, market_input = _setup(root, market_point_factory)
    ledger = tmp_path / "model-call-ledger.jsonl"

    def refused(messages, request_id):
        trace = CompletionTransportTrace(messages, 1, 30, request_id=request_id)
        trace.data["transport_mode"] = "JSON"
        trace.request_written(128, 0.01)
        trace.awaiting_headers()
        trace.headers_received(401)
        trace.body_received(8)
        trace.failed(ModelClientError("MODEL_UPSTREAM_HTTP_401"))
        failure = ModelClientError("MODEL_UPSTREAM_HTTP_401")
        failure.transport_trace = trace.snapshot()
        raise failure

    report = execute_authorized_optimization(
        [market_input], prompts, authorization=auth, ledger_path=ledger,
        call_model=refused, max_contexts=1,
    )

    assert report["status_counts"] == {
        "CALL_ERROR_NO_RETRY": 1,
        "NOT_ATTEMPTED": 1,
    }
    assert report["valid_analysis_records"] == 0
    assert all(
        row["result"]["error_code"] == "MODEL_UPSTREAM_HTTP_401"
        and row["result"]["transport_trace"]["http_status"] == 401
        and row["result"]["transport_trace"]["request_id"] == row["intent"]["request_id"]
        for row in report["records"] if row["status"] == "CALL_ERROR_NO_RETRY"
    )


def test_model_call_error_discards_arbitrary_exception_message():
    failure = RuntimeError("private token must not be recorded")
    result = _result_event({"call_key": "key", "run_id": "run", "request_id": "request"}, None, failure=failure)

    assert result["error_code"] == "MODEL_CALL_FAILED"
    assert result["error_type"] == "RuntimeError"
    assert "private token" not in json.dumps(result)


@pytest.mark.parametrize("trace_request_id", ["a" * 32, "b" * 32])
def test_cli_model_caller_attaches_transport_trace_only_for_current_request(
    monkeypatch, trace_request_id,
):
    import scripts.run_market_only_gemini_v38 as cli
    from core.config import config
    from core.model_client import ModelClientError, model_client

    monkeypatch.setattr(config, "api_key", "fixture-relay-key")

    def refused(messages, **kwargs):
        trace = CompletionTransportTrace(
            messages, 1, kwargs["timeout_sec"], request_id=trace_request_id,
        )
        trace.data["transport_mode"] = "JSON"
        trace.request_written(128, 0.01)
        trace.awaiting_headers()
        trace.headers_received(401)
        trace.body_received(8)
        trace.failed(ModelClientError("MODEL_UPSTREAM_HTTP_401"))
        model_client._response_state.transport_trace = trace
        raise ModelClientError("MODEL_UPSTREAM_HTTP_401")

    monkeypatch.setattr(model_client, "chat_completion", refused)
    _, call_model = cli._model_caller()
    request_id = "a" * 32

    with pytest.raises(ModelClientError) as failure:
        call_model([{"role": "user", "content": "fixture"}], request_id)

    attached_trace = getattr(failure.value, "transport_trace", None)
    if trace_request_id == request_id:
        assert attached_trace["request_id"] == request_id
        assert attached_trace["http_status"] == 401
    else:
        assert attached_trace is None


def test_invalid_analysis_is_preserved_and_not_silently_repaired_or_resized(
    tmp_path, market_point_factory,
):
    root = Path(__file__).resolve().parents[2]
    auth, prompts, market_input = _setup(root, market_point_factory)
    ledger = tmp_path / "model-call-ledger.jsonl"

    def forbidden_action(messages, request_id):
        result = _fixture_call(messages, request_id)
        payload = dict(result.payload)
        content = json.loads(payload["choices"][0]["message"]["content"])
        content["action"] = "OPEN_LONG"
        payload["choices"] = [{"message": {"content": json.dumps(content)}, "finish_reason": "stop"}]
        return ProviderCallResult(payload, DEFAULT_MODEL, result.transport_trace)

    report = execute_authorized_optimization(
        [market_input], prompts, authorization=auth, ledger_path=ledger,
        call_model=forbidden_action, max_contexts=1,
    )
    assert report["status_counts"] == {"INVALID_ANALYSIS": 2}
    assert report["valid_analysis_records"] == 0
    assert report["orders_created"] == 0
    events = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    results = [event for event in events if event["event_type"] == "RESULT"]
    assert all("MARKET_ONLY_ACTION_INVALID" in event["validation_errors"] for event in results)


def test_ledger_corruption_and_concurrent_lock_fail_closed_without_model_call(
    tmp_path, market_point_factory,
):
    root = Path(__file__).resolve().parents[2]
    auth, prompts, market_input = _setup(root, market_point_factory)
    ledger = tmp_path / "model-call-ledger.jsonl"
    ledger.write_text("{invalid\n", encoding="utf-8")
    calls = []
    with pytest.raises(ValueError, match="GEMINI_RUN_LEDGER"):
        execute_authorized_optimization(
            [market_input], prompts, authorization=auth, ledger_path=ledger,
            call_model=lambda *_: calls.append(True), max_contexts=1,
        )
    assert calls == []

    ledger.unlink()
    lock = ledger.with_name(ledger.name + ".lock")
    lock.write_text("held", encoding="utf-8")
    with pytest.raises(ValueError, match="GEMINI_RUN_LEDGER_LOCK_EXISTS_FAIL_CLOSED"):
        execute_authorized_optimization(
            [market_input], prompts, authorization=auth, ledger_path=ledger,
            call_model=lambda *_: calls.append(True), max_contexts=1,
        )
    assert calls == []
    assert _call_key(market_input["decision_id"], "A1_MARKET_ONLY") != _call_key(
        market_input["decision_id"], "A2_MARKET_ONLY",
    )


def test_existing_provider_quota_rejection_latches_across_invocations(
    tmp_path, market_point_factory,
):
    root = Path(__file__).resolve().parents[2]
    auth, prompts, market_input = _setup(root, market_point_factory)
    ledger = tmp_path / "model-call-ledger.jsonl"
    calls: list[str] = []

    def quota_rejection(messages, request_id):
        calls.append(request_id)
        trace = CompletionTransportTrace(messages, 1, 30, request_id=request_id)
        trace.data["transport_mode"] = "JSON"
        trace.request_written(128, 0.01)
        trace.awaiting_headers()
        trace.headers_received(429)
        trace.body_received(64)
        trace.completed()
        return ProviderCallResult(
            payload={"model": DEFAULT_MODEL, "choices": []},
            response_model_id=DEFAULT_MODEL,
            transport_trace=trace.snapshot(),
        )

    first = execute_authorized_optimization(
        [market_input], prompts, authorization=auth, ledger_path=ledger,
        call_model=quota_rejection, max_contexts=1,
    )
    second = execute_authorized_optimization(
        [market_input], prompts, authorization=auth, ledger_path=ledger,
        call_model=quota_rejection, max_contexts=1,
    )

    assert len(calls) == 1
    assert first["provider_rate_or_quota_stop_latched"] is True
    assert second["provider_rate_or_quota_stop_latched"] is True
    assert second["status_counts"] == {"INVALID_PROVIDER_RESPONSE": 1, "NOT_ATTEMPTED": 1}
    with pytest.raises(ValueError, match="GEMINI_RUN_PROVIDER_QUOTA_STOP_REMAINS_LATCHED"):
        execute_authorized_optimization(
            [market_input], prompts, authorization=auth, ledger_path=ledger,
            call_model=quota_rejection, max_contexts=1,
            provider_route_reauthorization=_route_reauthorization_evidence(),
        )
    assert len(calls) == 1


def test_region_rejection_stops_arm_pair_and_latches_across_invocations(
    tmp_path, market_point_factory,
):
    from core.model_client import ModelClientError

    root = Path(__file__).resolve().parents[2]
    auth, prompts, market_input = _setup(root, market_point_factory)
    second_market_input = build_market_only_input(market_point_factory(
        symbol="ETHUSDT", decision_id="v38-fixture-eth-20251015-1200z",
    ))
    ledger = tmp_path / "model-call-ledger.jsonl"
    calls: list[str] = []

    def region_rejection(messages, request_id):
        calls.append(request_id)
        trace = CompletionTransportTrace(messages, 1, 30, request_id=request_id)
        trace.data["transport_mode"] = "JSON"
        trace.request_written(128, 0.01)
        trace.awaiting_headers()
        trace.headers_received(400)
        trace.body_received(64)
        trace.failed(ModelClientError("MODEL_UPSTREAM_REGION_UNSUPPORTED"))
        failure = ModelClientError("MODEL_UPSTREAM_REGION_UNSUPPORTED")
        failure.transport_trace = trace.snapshot()
        raise failure

    first = execute_authorized_optimization(
        [market_input, second_market_input], prompts, authorization=auth, ledger_path=ledger,
        call_model=region_rejection, max_contexts=2,
    )
    second = execute_authorized_optimization(
        [market_input, second_market_input], prompts, authorization=auth, ledger_path=ledger,
        call_model=region_rejection, max_contexts=2,
    )

    assert len(calls) == 1
    assert first["dispatch_intent_count"] == second["dispatch_intent_count"] == 1
    assert first["status_counts"] == second["status_counts"] == {
        "CALL_ERROR_NO_RETRY": 1,
        "NOT_ATTEMPTED": 3,
    }
    assert first["provider_stop_latched"] is True
    assert first["provider_rate_or_quota_stop_latched"] is False
    assert second["provider_stop_latched"] is True


def test_route_reauthorization_is_append_only_and_never_retries_prior_pair(
    tmp_path, monkeypatch, market_point_factory,
):
    root = Path(__file__).resolve().parents[2]
    auth, prompts, market_input = _setup(root, market_point_factory)
    second_market_input = build_market_only_input(market_point_factory(
        symbol="ETHUSDT", decision_id="v38-fixture-eth-20251015-1200z",
    ))
    ledger = tmp_path / "model-call-ledger.jsonl"
    calls: list[str] = []

    def region_rejection(messages, request_id):
        calls.append(request_id)
        trace = CompletionTransportTrace(messages, 1, 30, request_id=request_id)
        trace.data["transport_mode"] = "JSON"
        trace.request_written(128, 0.01)
        trace.awaiting_headers()
        trace.headers_received(400)
        trace.body_received(64)
        error = RuntimeError("MODEL_UPSTREAM_REGION_UNSUPPORTED")
        error.transport_trace = trace.snapshot()
        raise error

    first = execute_authorized_optimization(
        [market_input, second_market_input], prompts, authorization=auth,
        ledger_path=ledger, call_model=region_rejection, max_contexts=2,
    )
    original_lines = ledger.read_text(encoding="utf-8").splitlines()
    original_events = [json.loads(line) for line in original_lines]
    original_result = next(event for event in original_events if event["event_type"] == "RESULT")
    assert first["provider_stop_latched"] is True
    assert len(calls) == 1

    invalid_evidence = _route_reauthorization_evidence()
    invalid_evidence["model_listed"] = False
    with pytest.raises(ValueError, match="GEMINI_PROVIDER_REARM_EVIDENCE_INVALID"):
        execute_authorized_optimization(
            [market_input, second_market_input], prompts, authorization=auth,
            ledger_path=ledger, call_model=_fixture_call, max_contexts=1,
            provider_route_reauthorization=invalid_evidence,
        )
    assert ledger.read_text(encoding="utf-8").splitlines() == original_lines

    stale_evidence = _route_reauthorization_evidence()
    stale_evidence["checked_at_utc"] = (
        datetime.now(UTC) - timedelta(minutes=6)
    ).isoformat().replace("+00:00", "Z")
    with pytest.raises(ValueError, match="GEMINI_PROVIDER_REARM_EVIDENCE_STALE"):
        execute_authorized_optimization(
            [market_input, second_market_input], prompts, authorization=auth,
            ledger_path=ledger, call_model=_fixture_call, max_contexts=1,
            provider_route_reauthorization=stale_evidence,
        )
    assert ledger.read_text(encoding="utf-8").splitlines() == original_lines

    recovered = execute_authorized_optimization(
        [market_input, second_market_input], prompts, authorization=auth,
        ledger_path=ledger, call_model=lambda messages, request_id: (
            calls.append(request_id) or _fixture_call(messages, request_id)
        ), max_contexts=1,
        provider_route_reauthorization=_route_reauthorization_evidence(),
    )
    events_after_rearm = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    rearm = next(event for event in events_after_rearm if event["event_type"] == "PROVIDER_STOP_REARM")
    assert len(calls) == 2
    assert events_after_rearm[:2] == original_events
    assert rearm["supersedes_result_event_sha256s"] == [original_result["event_sha256"]]
    assert recovered["provider_stop_rearm_count"] == 1
    assert recovered["provider_stop_latched"] is False
    assert recovered["status_counts"] == {
        "CALL_ERROR_NO_RETRY": 1,
        "NOT_ATTEMPTED": 2,
        "VALID_ANALYSIS": 1,
    }

    from core.replay.pa_decision_quality_v38 import gemini_runner

    class LaterDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return super().now(tz) + timedelta(minutes=10)

    with monkeypatch.context() as future_clock:
        future_clock.setattr(gemini_runner, "datetime", LaterDateTime)
        aged_events = _read_ledger(ledger)
        aged_review = gemini_runner.review_gemini_ledger(
            [market_input, second_market_input], prompts, aged_events,
        )
    assert aged_review["provider_stop_rearm_count"] == 1
    assert aged_review["provider_stop_latched"] is False

    continued = execute_authorized_optimization(
        [market_input, second_market_input], prompts, authorization=auth,
        ledger_path=ledger, call_model=lambda messages, request_id: (
            calls.append(request_id) or _fixture_call(messages, request_id)
        ), max_contexts=1,
    )
    assert len(calls) == 4
    assert continued["provider_stop_latched"] is False
    assert continued["valid_analysis_records"] == 3
    intents = [event for event in events_after_rearm if event["event_type"] == "DISPATCH_INTENT"]
    assert len({event["call_key"] for event in intents}) == len(intents) == 2


def test_route_refusal_after_reauthorization_latches_again_immediately(
    tmp_path, market_point_factory,
):
    root = Path(__file__).resolve().parents[2]
    auth, prompts, market_input = _setup(root, market_point_factory)
    ledger = tmp_path / "model-call-ledger.jsonl"
    calls: list[str] = []

    def region_rejection(messages, request_id):
        calls.append(request_id)
        trace = CompletionTransportTrace(messages, 1, 30, request_id=request_id)
        trace.data["transport_mode"] = "JSON"
        trace.request_written(128, 0.01)
        trace.awaiting_headers()
        trace.headers_received(400)
        trace.body_received(64)
        error = RuntimeError("MODEL_UPSTREAM_REGION_UNSUPPORTED")
        error.transport_trace = trace.snapshot()
        raise error

    first = execute_authorized_optimization(
        [market_input], prompts, authorization=auth, ledger_path=ledger,
        call_model=region_rejection, max_contexts=1,
    )
    assert first["provider_stop_latched"] is True

    resumed = execute_authorized_optimization(
        [market_input], prompts, authorization=auth, ledger_path=ledger,
        call_model=region_rejection, max_contexts=1,
        provider_route_reauthorization=_route_reauthorization_evidence(),
    )
    blocked = execute_authorized_optimization(
        [market_input], prompts, authorization=auth, ledger_path=ledger,
        call_model=region_rejection, max_contexts=1,
    )

    assert len(calls) == 2
    assert resumed["provider_stop_rearm_count"] == 1
    assert resumed["provider_stop_latched"] is True
    assert blocked["provider_stop_latched"] is True
    assert blocked["dispatch_intent_count"] == 2
    assert blocked["status_counts"] == {"CALL_ERROR_NO_RETRY": 2}


def test_cli_is_dry_run_by_default_and_does_not_initialize_model_client(
    tmp_path, monkeypatch, capsys, market_point_factory,
):
    import scripts.run_market_only_gemini_v38 as cli

    root = tmp_path
    report_root = root / "reports" / "v38+"
    report_root.mkdir(parents=True)
    market_input = build_market_only_input(market_point_factory())
    monkeypatch.setattr(cli, "ROOT", root)
    monkeypatch.setattr(cli, "load_authorization", lambda _path: {
        "authorization_id": AUTHORIZATION_ID, "model_id": DEFAULT_MODEL,
    })
    monkeypatch.setattr(cli, "load_prompt_templates", lambda _root: {"A1_MARKET_ONLY": {}, "A2_MARKET_ONLY": {}})
    monkeypatch.setattr(cli, "load_authorized_optimization_inputs", lambda *_: [market_input])
    monkeypatch.setattr(cli, "review_gemini_ledger", lambda *_: {
        "dataset_id": "frozen-fixture-dataset",
        "dataset_manifest_sha256": "a" * 64,
        "planned_attempt_denominator": 2,
    })
    monkeypatch.setattr(cli, "_model_caller", lambda: pytest.fail("DRY_RUN_INITIALIZED_MODEL_CLIENT"))

    status = cli.main([
        "--input", "frozen-input.json",
        "--manifest", "frozen-manifest.json",
        "--ledger", str(report_root / "runs" / "intent.jsonl"),
    ])
    output = capsys.readouterr().out

    assert status == 0
    assert "DRY_RUN_READY_NO_MODEL_CALL" in output
    assert '"model_calls_used": 0' in output
    assert not (report_root / "runs" / "intent.jsonl").exists()


def test_route_reauthorization_preflight_is_local_authenticated_get_without_completion(monkeypatch):
    import scripts.run_market_only_gemini_v38 as cli

    class FakeConfiguration:
        base_url = "http://127.0.0.1:8045/v1"
        model_name = DEFAULT_MODEL
        api_key = "test-local-token"

    class FakeClient:
        model_name = DEFAULT_MODEL

        @staticmethod
        def _configuration_error(_model):
            return None

        @staticmethod
        def chat_completion(*_args, **_kwargs):
            pytest.fail("ROUTE_READINESS_MUST_NOT_SEND_COMPLETION")

    class FakeResponse:
        status = 200

        @staticmethod
        def read(_limit):
            return json.dumps({"data": [{"id": DEFAULT_MODEL}]}).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    observed: list[tuple[str, str, int]] = []

    def fake_open(request, *, timeout):
        observed.append((request.full_url, request.get_method(), timeout))
        assert request.get_header("Authorization") == "Bearer test-local-token"
        return FakeResponse()

    evidence = cli._provider_route_reauthorization_evidence(
        configuration=FakeConfiguration(), client=FakeClient(), opener=fake_open,
    )

    assert observed == [("http://127.0.0.1:8045/v1/models", "GET", 8)]
    assert evidence["model_listed"] is True
    assert evidence["http_status"] == 200
    assert evidence["operator_confirmed_route_change"] is True
