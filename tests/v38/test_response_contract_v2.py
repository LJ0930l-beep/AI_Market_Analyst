from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.model_routing import DEFAULT_MODEL
from core.replay.pa_decision_quality_v38.json_response import (
    RAW_OR_SINGLE_JSON_FENCE_V1,
    RAW_OR_SINGLE_JSON_FENCE_V2,
    parse_json_completion,
)
from core.replay.pa_decision_quality_v38.market_only import MarketOnlyError
from core.replay.pa_decision_quality_v38.response_contract_v2 import (
    FULL_SCHEMA_VERSION,
    MINIMAL_SCHEMA_VERSION,
    SCHEMA_FULL_V2,
    SCHEMA_MINIMAL_V2,
    OutputTier,
    ResponseFormatCapability,
    evaluate_response_v2,
    schema_for_tier,
)

_REQUEST_ID = "a" * 32
_VALID_REFS = {"bar:15m:2025-10-10T12:00:00Z", "bar:1h:2025-10-10T12:00:00Z"}


def _minimal() -> dict:
    return {
        "schema_version": MINIMAL_SCHEMA_VERSION,
        "action": "WAIT",
        "market_regime": "UNCERTAIN",
        "bias": "UNCERTAIN",
        "evidence_refs": ["bar:15m:2025-10-10T12:00:00Z"],
    }


def _full() -> dict:
    return {
        "schema_version": FULL_SCHEMA_VERSION,
        "action": "WAIT",
        "market_regime": "UNCERTAIN",
        "bias": "UNCERTAIN",
        "location": "UNKNOWN",
        "signal": {"setup": "UNKNOWN", "quality": "UNKNOWN"},
        "candidate_setup": {
            "kind": "UNASSESSED",
            "description": "The supplied evidence does not confirm a setup.",
            "evidence_refs": [],
        },
        "target_structure": {
            "kind": "UNKNOWN",
            "rationale": "The supplied evidence does not establish a target.",
            "evidence_refs": [],
        },
        "counter_evidence": ["The evidence is insufficient for directional follow-through."],
        "decision_rationale": "Wait because the supplied context is uncertain.",
        "evidence_refs": ["bar:15m:2025-10-10T12:00:00Z"],
        "wait_reason": "UNCERTAIN_CONTEXT",
    }


def _envelope(
    analysis: dict | None = None,
    *,
    content: str | None = None,
    finish_reason: str | None = "stop",
    payload_model: str | None = DEFAULT_MODEL,
    response_model_id: str | None = DEFAULT_MODEL,
    request_id: str = _REQUEST_ID,
    usage: dict | None = None,
) -> dict:
    if content is None and analysis is not None:
        content = json.dumps(analysis, separators=(",", ":"), ensure_ascii=False)
    payload = {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": usage
        if usage is not None
        else {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
        },
    }
    if payload_model is not None:
        payload["model"] = payload_model
    return {
        "payload": payload,
        "response_model_id": response_model_id,
        "transport_trace": {
            "request_id": request_id,
            "phase": "COMPLETED",
            "http_status": 200,
            "transport_mode": "JSON",
            "request_bytes_written": 256,
        },
    }


def _evaluate(envelope: dict, *, tier: OutputTier = OutputTier.MINIMAL, **kwargs) -> dict:
    return evaluate_response_v2(
        envelope,
        tier=tier,
        requested_model_id=DEFAULT_MODEL,
        expected_request_id=_REQUEST_ID,
        valid_evidence_refs=_VALID_REFS,
        **kwargs,
    )


@pytest.mark.parametrize(
    ("tier", "analysis"),
    [(OutputTier.MINIMAL, _minimal()), (OutputTier.FULL, _full())],
)
def test_v2_minimal_and_full_contracts_accept_causal_offline_fixtures(tier, analysis):
    result = _evaluate(_envelope(analysis), tier=tier)

    assert result["status"] == "VALID_LOCAL_CONTRACT"
    assert result["normalized_analysis"] == analysis
    assert result["model_identity_status"] == "RESPONSE_MATCHED_REQUESTED_MODEL"
    assert result["transport_receipt_status"] == "MATCHED_COMPLETED_JSON_HTTP_200"
    assert result["provider_cost_status"] == "UNKNOWN_NO_VERIFIED_BILLING_RECEIPT"
    assert result["remote_dispatch_allowed"] is False


def test_v2_fence_compatibility_keeps_raw_and_normalized_response_separate():
    fence = chr(96) * 3
    raw = fence + "json\n" + json.dumps(_minimal(), separators=(",", ":")) + "\n" + fence
    result = _evaluate(_envelope(content=raw))

    assert result["status"] == "VALID_LOCAL_CONTRACT"
    assert result["raw_response_text"] == raw
    assert result["raw_response_sha256"]
    assert result["normalized_analysis"] == _minimal()
    assert result["parser_version"] == RAW_OR_SINGLE_JSON_FENCE_V2


@pytest.mark.parametrize(
    ("content", "finish_reason", "status", "code"),
    [
        ("   ", "stop", "EMPTY_RESPONSE", "COMPLETION_CONTENT_EMPTY"),
        ('{"schema_version":', "stop", "INVALID_JSON", "GEMINI_JSON_COMPLETION_INVALID"),
        ('{"ok":true}', None, "INVALID_FINISH_REASON", "COMPLETION_FINISH_REASON_MISSING"),
        ('{"ok":true}', "error", "INVALID_FINISH_REASON", "COMPLETION_DID_NOT_FINISH_WITH_STOP"),
        ('{"partial":', "length", "TRUNCATED_RESPONSE", "COMPLETION_FINISH_REASON_LENGTH"),
    ],
)
def test_v2_classifies_empty_partial_and_interrupted_responses(content, finish_reason, status, code):
    result = _evaluate(_envelope(content=content, finish_reason=finish_reason))

    assert result["status"] == status
    assert result["error_code"] == code
    assert result["normalized_analysis"] is None


def test_v2_rejects_missing_actual_response_model_without_using_requested_model_as_identity():
    envelope = _envelope(_minimal(), payload_model=None, response_model_id=None)
    result = _evaluate(envelope)

    assert result["status"] == "MODEL_IDENTITY_UNVERIFIED"
    assert result["model_identity_status"] == "UNVERIFIED_NO_RESPONSE_MODEL_ID"
    assert result["requested_model_id"] == DEFAULT_MODEL
    assert result["response_model_id"] is None
    assert result["remote_dispatch_allowed"] is False


def test_payload_model_alone_does_not_verify_missing_adapter_identity():
    result = _evaluate(_envelope(_minimal(), response_model_id=None))

    assert result["status"] == "MODEL_IDENTITY_UNVERIFIED"
    assert result["response_model_id"] == DEFAULT_MODEL
    assert result["model_identity_status"] == "UNVERIFIED_NO_RESPONSE_MODEL_ID"
    assert result["raw_response_sha256"]
    assert result["normalized_analysis"] is None


@pytest.mark.parametrize(
    ("payload_model", "response_model", "status"),
    [
        ("different-model", "different-model", "MODEL_IDENTITY_MISMATCH"),
        ("different-model", DEFAULT_MODEL, "MODEL_IDENTITY_MISMATCH"),
    ],
)
def test_v2_rejects_mismatched_actual_model_identity(payload_model, response_model, status):
    result = _evaluate(
        _envelope(
            _minimal(),
            payload_model=payload_model,
            response_model_id=response_model,
        )
    )

    assert result["status"] == status
    assert result["normalized_analysis"] is None


@pytest.mark.parametrize("bad_ref", ["bar:future:2099-01-01T00:00:00Z", "invented:price"])
def test_v2_rejects_unknown_or_future_evidence_references(bad_ref):
    analysis = _minimal()
    analysis["evidence_refs"] = [bad_ref]
    result = _evaluate(_envelope(analysis))

    assert result["status"] == "INVALID_EVIDENCE"
    assert result["error_code"] == "RESPONSE_SCHEMA_OR_EVIDENCE_INVALID"


def test_v2_rejects_unknown_enum_and_execution_fields():
    analysis = _minimal()
    analysis["market_regime"] = "PROFIT_GUARANTEED"
    analysis["order"] = {"side": "BUY"}
    result = _evaluate(_envelope(analysis))

    assert result["status"] == "INVALID_SCHEMA"
    assert result["normalized_analysis"] is None


def test_v2_rejects_request_response_transport_mismatch():
    result = _evaluate(_envelope(_minimal(), request_id="b" * 32))

    assert result["status"] == "INVALID_TRANSPORT"
    assert result["transport_receipt_status"] == "UNVERIFIED"


def test_v2_oversized_body_is_hashed_but_not_retained():
    raw = "x" * 64
    result = _evaluate(_envelope(content=raw), max_raw_response_chars=32)

    assert result["status"] == "RESPONSE_TOO_LARGE"
    assert result["raw_response_chars"] == 64
    assert result["raw_response_sha256"]
    assert result["raw_response_text"] is None


def test_fake_provider_ignoring_schema_request_does_not_prove_route_capability():
    fence = chr(96) * 3
    raw = fence + "json\n" + json.dumps(_minimal(), separators=(",", ":")) + "\n" + fence
    result = _evaluate(_envelope(content=raw))

    assert result["status"] == "VALID_LOCAL_CONTRACT"
    assert result["response_format_capability"] == "unknown_unverified"
    assert result["remote_dispatch_allowed"] is False


def test_inconsistent_usage_remains_unverified_and_never_becomes_cost_credit():
    result = _evaluate(
        _envelope(
            _minimal(),
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 13131},
        )
    )

    assert result["status"] == "VALID_LOCAL_CONTRACT"
    assert result["usage_receipt_status"] == "UNVERIFIED_ARITHMETIC_MISMATCH"
    assert result["provider_cost_status"] == "UNKNOWN_NO_VERIFIED_BILLING_RECEIPT"


def test_prompt_templates_pin_v2_contract_without_authorizing_remote_calls():
    root = Path(__file__).resolve().parents[2]
    templates = [
        json.loads((root / "configs/research/prompts/v38-market-only-minimal-v2.json").read_text()),
        json.loads((root / "configs/research/prompts/v38-market-only-full-v2.json").read_text()),
    ]

    assert [template["output_tier"] for template in templates] == ["MINIMAL", "FULL"]
    assert [template["output_schema_version"] for template in templates] == [
        MINIMAL_SCHEMA_VERSION,
        FULL_SCHEMA_VERSION,
    ]
    assert [template["response_parser_version"] for template in templates] == [
        RAW_OR_SINGLE_JSON_FENCE_V2,
        RAW_OR_SINGLE_JSON_FENCE_V2,
    ]
    assert all(template["response_format_capability"] == "unknown_unverified" for template in templates)
    assert all(template["remote_dispatch_enabled"] is False for template in templates)
    assert all(
        template["response_format_contract"]["preferred_request"]["type"] == "json_schema" for template in templates
    )
    assert all(
        template["response_format_contract"]["capability_status"] == "unknown_unverified" for template in templates
    )
    assert all(template["response_format_contract"]["remote_dispatch_allowed"] is False for template in templates)
    assert all(
        template["call_authorization"]
        == {
            "authorized": False,
            "maximum_calls": 0,
            "study_scope": "OFFLINE_CONTRACT_FIXTURES_ONLY",
        }
        for template in templates
    )
    assert templates[1]["same_research_opinion_chain"] is True
    assert templates[1]["requires_minimal_stage_status"] == "VALID_LOCAL_CONTRACT"
    assert schema_for_tier(OutputTier.MINIMAL) == SCHEMA_MINIMAL_V2
    assert schema_for_tier(OutputTier.FULL) == SCHEMA_FULL_V2
    copied_schema = schema_for_tier(OutputTier.MINIMAL)
    copied_schema["properties"].clear()
    assert schema_for_tier(OutputTier.MINIMAL)["properties"]


def test_v2_parser_keeps_v1_policy_available_and_versioned():
    assert RAW_OR_SINGLE_JSON_FENCE_V1 != RAW_OR_SINGLE_JSON_FENCE_V2
    assert parse_json_completion(
        '{"x":1}',
        parser_version=RAW_OR_SINGLE_JSON_FENCE_V1,
    ) == {"x": 1}
    assert parse_json_completion(
        '{"x":1}',
        parser_version=RAW_OR_SINGLE_JSON_FENCE_V2,
    ) == {"x": 1}
    fence = chr(96) * 3
    invalid = fence + 'json\n{"x":1}\n' + fence + " trailing"
    with pytest.raises(MarketOnlyError, match="GEMINI_JSON_ENVELOPE_INVALID"):
        parse_json_completion(invalid, parser_version=RAW_OR_SINGLE_JSON_FENCE_V2)


def test_v2_parser_rejects_multiple_json_fences():
    fence = chr(96) * 3
    raw = fence + "json\n{}\n" + fence + "\n" + fence + "json\n{}\n" + fence
    with pytest.raises(MarketOnlyError, match="GEMINI_JSON_ENVELOPE_INVALID"):
        parse_json_completion(raw, parser_version=RAW_OR_SINGLE_JSON_FENCE_V2)


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', '{"x":NaN}'])
def test_v2_parser_rejects_duplicate_keys_and_non_json_constants(raw):
    with pytest.raises(MarketOnlyError, match="GEMINI_JSON_COMPLETION_INVALID"):
        parse_json_completion(raw, parser_version=RAW_OR_SINGLE_JSON_FENCE_V2)


def test_non_string_enum_payloads_fail_closed_without_raising():
    assert {value.value for value in ResponseFormatCapability} == {
        "native_schema_verified",
        "json_object_only",
        "prompt_only_unverified",
        "unknown_unverified",
    }
