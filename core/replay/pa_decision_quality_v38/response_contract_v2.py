"""Offline-only V2 response contract for V38 parseability experiments.

This module validates fixed provider-envelope fixtures. It has no network client
and never promotes provider capabilities from response shape alone.
"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from enum import Enum
from typing import Any

from .json_response import RAW_OR_SINGLE_JSON_FENCE_V2, parse_json_completion
from .market_only import MarketOnlyError

RESPONSE_CONTRACT_VERSION = "pa-market-only-v38/response-contract-2"
MINIMAL_SCHEMA_VERSION = "pa-market-only-v38/analysis-minimal-2"
FULL_SCHEMA_VERSION = "pa-market-only-v38/analysis-full-2"
MAX_RAW_RESPONSE_CHARS = 12_000


class OutputTier(str, Enum):
    MINIMAL = "MINIMAL"
    FULL = "FULL"


class ResponseFormatCapability(str, Enum):
    """Vocabulary for independently evidenced route capability; never inferred here."""

    NATIVE_SCHEMA_VERIFIED = "native_schema_verified"
    JSON_OBJECT_ONLY = "json_object_only"
    PROMPT_ONLY_UNVERIFIED = "prompt_only_unverified"
    UNKNOWN_UNVERIFIED = "unknown_unverified"


SCHEMA_MINIMAL_V2: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "V38 market-only minimal analysis V2",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "action", "market_regime", "bias", "evidence_refs"],
    "properties": {
        "schema_version": {"const": MINIMAL_SCHEMA_VERSION},
        "action": {"type": "string", "enum": ["OBSERVE", "WAIT"]},
        "market_regime": {
            "type": "string",
            "enum": ["BULL_TREND", "BEAR_TREND", "TRADING_RANGE", "BREAKOUT_TRANSITION", "UNCERTAIN"],
        },
        "bias": {"type": "string", "enum": ["BULLISH", "BEARISH", "NEUTRAL", "UNCERTAIN"]},
        "evidence_refs": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string"},
        },
    },
}

SCHEMA_FULL_V2: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "V38 market-only full analysis V2",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "action",
        "market_regime",
        "bias",
        "location",
        "signal",
        "candidate_setup",
        "target_structure",
        "counter_evidence",
        "decision_rationale",
        "evidence_refs",
        "wait_reason",
    ],
    "properties": {
        "schema_version": {"const": FULL_SCHEMA_VERSION},
        "action": {"type": "string", "enum": ["OBSERVE", "WAIT"]},
        "market_regime": {
            "type": "string",
            "enum": ["BULL_TREND", "BEAR_TREND", "TRADING_RANGE", "BREAKOUT_TRANSITION", "UNCERTAIN"],
        },
        "bias": {"type": "string", "enum": ["BULLISH", "BEARISH", "NEUTRAL", "UNCERTAIN"]},
        "location": {
            "type": "string",
            "enum": [
                "TREND_PULLBACK",
                "RANGE_UPPER",
                "RANGE_LOWER",
                "RANGE_MIDDLE",
                "STRUCTURE_LEVEL",
                "BREAKOUT_RETEST",
                "EXTENDED",
                "OTHER",
                "UNKNOWN",
            ],
        },
        "signal": {
            "type": "object",
            "additionalProperties": False,
            "required": ["setup", "quality"],
            "properties": {
                "setup": {
                    "type": "string",
                    "enum": [
                        "H1",
                        "H2",
                        "L1",
                        "L2",
                        "BREAKOUT",
                        "BREAKOUT_PULLBACK",
                        "FAILED_BREAKOUT",
                        "REVERSAL",
                        "WEDGE",
                        "TRADING_RANGE_REVERSAL",
                        "NONE",
                        "OTHER",
                        "UNKNOWN",
                    ],
                },
                "quality": {
                    "type": "string",
                    "enum": ["STRONG", "CONDITIONAL", "WEAK", "NO_SIGNAL", "UNKNOWN"],
                },
            },
        },
        "candidate_setup": {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "description", "evidence_refs"],
            "properties": {
                "kind": {"type": "string", "enum": ["NONE", "CONDITIONAL", "UNASSESSED"]},
                "description": {"type": "string", "minLength": 1},
                "evidence_refs": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string"},
                },
            },
        },
        "target_structure": {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "rationale", "evidence_refs"],
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["PRIOR_SWING", "RANGE_EXTREME", "TREND_MEASURED_MOVE", "NONE", "UNKNOWN"],
                },
                "rationale": {"type": "string", "minLength": 1},
                "evidence_refs": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string"},
                },
            },
        },
        "counter_evidence": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "decision_rationale": {"type": "string", "minLength": 1},
        "evidence_refs": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string"},
        },
        "wait_reason": {
            "type": ["string", "null"],
            "enum": [
                "UNCERTAIN_CONTEXT",
                "NO_CONFIRMED_SETUP",
                "COUNTER_EVIDENCE_DOMINATES",
                "DATA_INSUFFICIENT",
                "NOT_ASSESSED_BY_STUB",
                None,
            ],
        },
    },
}


def schema_for_tier(tier: OutputTier | str) -> dict[str, Any]:
    """Return the versioned V2 schema for one explicit response tier."""
    try:
        resolved = OutputTier(tier)
    except (TypeError, ValueError) as exc:
        raise MarketOnlyError("V38_RESPONSE_TIER_UNSUPPORTED") from exc
    return deepcopy(SCHEMA_MINIMAL_V2 if resolved is OutputTier.MINIMAL else SCHEMA_FULL_V2)


def _enum_value(value: Any, allowed: set[str]) -> bool:
    return isinstance(value, str) and value in allowed


def _known_refs(value: Any, valid_evidence_refs: set[str], *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(isinstance(ref, str) and ref in valid_evidence_refs for ref in value)
        and len(value) == len(set(value))
    )


def _validate_v2_analysis(
    analysis: Any,
    *,
    tier: OutputTier,
    valid_evidence_refs: set[str],
) -> tuple[list[str], str | None]:
    if not isinstance(analysis, dict):
        return ["ANALYSIS_NOT_OBJECT"], "INVALID_SCHEMA"

    schema = schema_for_tier(tier)
    required = set(schema["required"])
    if set(analysis) != required:
        return ["ANALYSIS_FIELDS_INVALID"], "INVALID_SCHEMA"
    if analysis.get("schema_version") != schema["properties"]["schema_version"]["const"]:
        return ["ANALYSIS_SCHEMA_VERSION_INVALID"], "INVALID_SCHEMA"
    if not _enum_value(analysis.get("action"), {"OBSERVE", "WAIT"}):
        return ["ACTION_INVALID"], "INVALID_SCHEMA"

    enum_fields = {
        "market_regime": set(schema["properties"]["market_regime"]["enum"]),
        "bias": set(schema["properties"]["bias"]["enum"]),
    }
    for field, allowed in enum_fields.items():
        if not _enum_value(analysis.get(field), allowed):
            return [f"{field.upper()}_INVALID"], "INVALID_SCHEMA"

    if tier is OutputTier.FULL:
        if not _enum_value(analysis.get("location"), set(schema["properties"]["location"]["enum"])):
            return ["LOCATION_INVALID"], "INVALID_SCHEMA"
        signal = analysis.get("signal")
        if (
            not isinstance(signal, dict)
            or set(signal) != {"setup", "quality"}
            or not _enum_value(signal.get("setup"), set(schema["properties"]["signal"]["properties"]["setup"]["enum"]))
            or not _enum_value(
                signal.get("quality"), set(schema["properties"]["signal"]["properties"]["quality"]["enum"])
            )
        ):
            return ["SIGNAL_INVALID"], "INVALID_SCHEMA"
        candidate = analysis.get("candidate_setup")
        if (
            not isinstance(candidate, dict)
            or set(candidate) != {"kind", "description", "evidence_refs"}
            or not _enum_value(candidate.get("kind"), {"NONE", "CONDITIONAL", "UNASSESSED"})
            or not isinstance(candidate.get("description"), str)
            or not candidate["description"].strip()
        ):
            return ["CANDIDATE_SETUP_INVALID"], "INVALID_SCHEMA"
        target = analysis.get("target_structure")
        if (
            not isinstance(target, dict)
            or set(target) != {"kind", "rationale", "evidence_refs"}
            or not _enum_value(
                target.get("kind"),
                {
                    "PRIOR_SWING",
                    "RANGE_EXTREME",
                    "TREND_MEASURED_MOVE",
                    "NONE",
                    "UNKNOWN",
                },
            )
            or not isinstance(target.get("rationale"), str)
            or not target["rationale"].strip()
        ):
            return ["TARGET_STRUCTURE_INVALID"], "INVALID_SCHEMA"
        counter_evidence = analysis.get("counter_evidence")
        if (
            not isinstance(counter_evidence, list)
            or not counter_evidence
            or any(not isinstance(item, str) or not item.strip() for item in counter_evidence)
        ):
            return ["COUNTER_EVIDENCE_INVALID"], "INVALID_SCHEMA"
        if not isinstance(analysis.get("decision_rationale"), str) or not analysis["decision_rationale"].strip():
            return ["DECISION_RATIONALE_INVALID"], "INVALID_SCHEMA"
        wait_reason = analysis.get("wait_reason")
        allowed_wait_reasons = {
            "UNCERTAIN_CONTEXT",
            "NO_CONFIRMED_SETUP",
            "COUNTER_EVIDENCE_DOMINATES",
            "DATA_INSUFFICIENT",
            "NOT_ASSESSED_BY_STUB",
        }
        if (analysis["action"] == "WAIT" and not _enum_value(wait_reason, allowed_wait_reasons)) or (
            analysis["action"] == "OBSERVE" and wait_reason is not None
        ):
            return ["WAIT_REASON_INVALID"], "INVALID_SCHEMA"

    ref_lists = [analysis.get("evidence_refs")]
    if tier is OutputTier.FULL:
        ref_lists.extend(
            [
                analysis["candidate_setup"]["evidence_refs"],
                analysis["target_structure"]["evidence_refs"],
            ]
        )
    if any(not _known_refs(refs, valid_evidence_refs, allow_empty=(index > 0)) for index, refs in enumerate(ref_lists)):
        return ["EVIDENCE_REFERENCE_INVALID_OR_NOT_CAUSAL"], "INVALID_EVIDENCE"
    try:
        json.dumps(analysis, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return ["ANALYSIS_NOT_JSON_SERIALIZABLE"], "INVALID_SCHEMA"
    return [], None


def _usage_status(usage: Any) -> str:
    if not isinstance(usage, dict):
        return "UNVERIFIED_MISSING"
    values = [usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")]
    if any(type(value) is not int or value < 0 for value in values):
        return "UNVERIFIED_INVALID"
    prompt_tokens, completion_tokens, total_tokens = values
    if prompt_tokens + completion_tokens != total_tokens:
        return "UNVERIFIED_ARITHMETIC_MISMATCH"
    return "REPORTED_ARITHMETICALLY_CONSISTENT_UNVERIFIED_BILLING"


def evaluate_response_v2(
    envelope: Any,
    *,
    tier: OutputTier | str,
    requested_model_id: str,
    expected_request_id: str,
    valid_evidence_refs: set[str],
    max_raw_response_chars: int = MAX_RAW_RESPONSE_CHARS,
) -> dict[str, Any]:
    """Evaluate one fixed response fixture without making any network call.

    A syntactically valid response never upgrades provider capability. This
    function only reports local contract validity and always denies dispatch.
    """
    try:
        resolved_tier = OutputTier(tier)
    except (TypeError, ValueError) as exc:
        raise MarketOnlyError("V38_RESPONSE_TIER_UNSUPPORTED") from exc
    schema_version = schema_for_tier(resolved_tier)["properties"]["schema_version"]["const"]
    result: dict[str, Any] = {
        "contract_version": RESPONSE_CONTRACT_VERSION,
        "tier": resolved_tier.value,
        "schema_version": schema_version,
        "parser_version": RAW_OR_SINGLE_JSON_FENCE_V2,
        "status": "INVALID_PROVIDER_RESPONSE",
        "error_code": None,
        "requested_model_id": requested_model_id,
        "response_model_id": None,
        "payload_model_id": None,
        "response_model_id_source": "UNAVAILABLE",
        "model_identity_status": "UNVERIFIED_NO_RESPONSE_MODEL_ID",
        "model_identity_evidence_level": "UNVERIFIED_NO_RESPONSE_MODEL_METADATA",
        "response_format_capability": ResponseFormatCapability.UNKNOWN_UNVERIFIED.value,
        "capability_evidence_status": "NOT_PRESENT_NO_REMOTE_CAPABILITY_PROBE",
        "remote_dispatch_allowed": False,
        "raw_response_text": None,
        "raw_response_sha256": None,
        "raw_response_chars": None,
        "normalized_analysis": None,
        "validation_errors": [],
        "finish_reason": None,
        "transport_receipt_status": "UNVERIFIED",
        "usage_reported_by_provider": None,
        "usage_receipt_status": "UNVERIFIED_MISSING",
        "provider_cost_status": "UNKNOWN_NO_VERIFIED_BILLING_RECEIPT",
    }

    def reject(status: str, code: str) -> dict[str, Any]:
        result["status"] = status
        result["error_code"] = code
        return result

    if type(max_raw_response_chars) is not int or max_raw_response_chars < 1:
        return reject("INVALID_PROVIDER_RESPONSE", "RESPONSE_SIZE_LIMIT_INVALID")

    if not isinstance(envelope, dict):
        return reject("INVALID_PROVIDER_RESPONSE", "RESPONSE_ENVELOPE_NOT_OBJECT")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return reject("INVALID_PROVIDER_RESPONSE", "RESPONSE_PAYLOAD_NOT_OBJECT")

    result["usage_reported_by_provider"] = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
    result["usage_receipt_status"] = _usage_status(payload.get("usage"))

    choices = payload.get("choices")
    first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    finish_reason = first.get("finish_reason") if isinstance(first, dict) else None
    result["finish_reason"] = finish_reason if isinstance(finish_reason, str) else None
    if isinstance(content, str):
        raw = content.encode("utf-8", errors="replace")
        result["raw_response_sha256"] = hashlib.sha256(raw).hexdigest()
        result["raw_response_chars"] = len(content)
        if len(content) <= max_raw_response_chars:
            result["raw_response_text"] = content

    payload_model = payload.get("model")
    response_model = envelope.get("response_model_id")
    result["response_model_id"] = response_model if isinstance(response_model, str) else None
    result["payload_model_id"] = payload_model if isinstance(payload_model, str) else None
    if not isinstance(requested_model_id, str) or not requested_model_id.strip():
        result["model_identity_status"] = "UNVERIFIED_INVALID_REQUESTED_MODEL_ID"
        result["response_model_id_source"] = "REQUESTED_MODEL_ID_INVALID"
        return reject("MODEL_IDENTITY_UNVERIFIED", "REQUESTED_MODEL_ID_INVALID")
    if (
        not isinstance(payload_model, str)
        or not payload_model.strip()
        or not isinstance(response_model, str)
        or not response_model.strip()
    ):
        if isinstance(response_model, str) and response_model.strip():
            result["response_model_id_source"] = "ADAPTER_RECEIPT_ONLY"
        elif isinstance(payload_model, str) and payload_model.strip():
            result["response_model_id_source"] = "PAYLOAD_FIELD_ONLY"
        else:
            result["response_model_id_source"] = "NO_RESPONSE_MODEL_FIELD"
        result["model_identity_status"] = "UNVERIFIED_NO_RESPONSE_MODEL_ID"
        return reject("MODEL_IDENTITY_UNVERIFIED", "RESPONSE_MODEL_ID_MISSING")
    if payload_model != response_model:
        result["response_model_id_source"] = "PAYLOAD_AND_ADAPTER_RECEIPT_DISAGREE"
        result["model_identity_evidence_level"] = "REPORTED_RESPONSE_FIELDS_DISAGREE"
        result["model_identity_status"] = "RESPONSE_MODEL_FIELDS_DISAGREE"
        return reject("MODEL_IDENTITY_MISMATCH", "RESPONSE_MODEL_FIELDS_DISAGREE")
    result["response_model_id_source"] = "PAYLOAD_AND_ADAPTER_RECEIPT_MATCH"
    if response_model != requested_model_id:
        result["model_identity_evidence_level"] = "REPORTED_FIELDS_DO_NOT_MATCH_REQUESTED_MODEL"
        result["model_identity_status"] = "RESPONSE_MODEL_ID_MISMATCH"
        return reject("MODEL_IDENTITY_MISMATCH", "RESPONSE_MODEL_ID_NOT_REQUESTED_MODEL")
    result["model_identity_status"] = "RESPONSE_MATCHED_REQUESTED_MODEL"
    result["model_identity_evidence_level"] = "REPORTED_FIELDS_MATCH_ONLY_NO_WEIGHT_ATTESTATION"

    trace = envelope.get("transport_trace")
    request_bytes_written = trace.get("request_bytes_written") if isinstance(trace, dict) else None
    valid_request_bytes = (type(request_bytes_written) is int and request_bytes_written > 0) or (
        type(request_bytes_written) is float and math.isfinite(request_bytes_written) and request_bytes_written > 0
    )
    if (
        not isinstance(expected_request_id, str)
        or not expected_request_id
        or not isinstance(trace, dict)
        or trace.get("request_id") != expected_request_id
        or trace.get("phase") != "COMPLETED"
        or trace.get("http_status") != 200
        or trace.get("transport_mode") != "JSON"
        or not valid_request_bytes
    ):
        return reject("INVALID_TRANSPORT", "COMPLETED_JSON_TRANSPORT_UNVERIFIED")
    result["transport_receipt_status"] = "MATCHED_COMPLETED_JSON_HTTP_200"

    if not isinstance(content, str):
        if finish_reason == "length":
            return reject("TRUNCATED_RESPONSE", "COMPLETION_FINISH_REASON_LENGTH")
        return reject("INVALID_PROVIDER_RESPONSE", "COMPLETION_CONTENT_MISSING")
    if finish_reason == "length":
        return reject("TRUNCATED_RESPONSE", "COMPLETION_FINISH_REASON_LENGTH")
    if finish_reason != "stop":
        return reject(
            "INVALID_FINISH_REASON",
            "COMPLETION_FINISH_REASON_MISSING" if finish_reason is None else "COMPLETION_DID_NOT_FINISH_WITH_STOP",
        )
    if len(content) > max_raw_response_chars:
        return reject("RESPONSE_TOO_LARGE", "COMPLETION_CONTENT_TOO_LARGE")
    if not content.strip():
        return reject("EMPTY_RESPONSE", "COMPLETION_CONTENT_EMPTY")

    try:
        analysis = parse_json_completion(content, parser_version=RAW_OR_SINGLE_JSON_FENCE_V2)
    except MarketOnlyError as exc:
        return reject("INVALID_JSON", exc.code)

    errors, category = _validate_v2_analysis(
        analysis,
        tier=resolved_tier,
        valid_evidence_refs=valid_evidence_refs,
    )
    result["validation_errors"] = errors
    if errors:
        return reject(category or "INVALID_SCHEMA", "RESPONSE_SCHEMA_OR_EVIDENCE_INVALID")
    result["normalized_analysis"] = analysis
    result["status"] = "VALID_LOCAL_CONTRACT"
    return result
