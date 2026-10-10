"""One-request, format-only V38 Gemini pilot with durable at-most-once execution."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.ai.transport_diagnostics import safe_transport_trace
from core.model_routing import DEFAULT_MODEL

from .gemini_runner import (
    MAX_RECORDED_RESPONSE_CHARS,
    ProviderCallResult,
    _safe_usage,
    _trace_matches_intent,
    _transport_wire_messages_sha256,
    canonical_json_bytes,
    canonical_sha256,
)
from .json_response import RAW_OR_SINGLE_JSON_FENCE_V1, parse_json_completion
from .market_only import ANALYSIS_SCHEMA_VERSION, INPUT_SCHEMA_VERSION, MarketOnlyError, validate_market_only_analysis

FORMAT_PILOT_SCHEMA_VERSION = "pa-market-only-v38/json-format-pilot-1"
FORMAT_PILOT_LEDGER_SCHEMA_VERSION = "pa-market-only-v38/json-format-pilot-ledger-1"
FORMAT_PILOT_AUTHORIZATION_ID = "V38_GEMINI_JSON_FORMAT_PILOT_20261010_V1"
FORMAT_PILOT_DATASET_ID = "V38_BINANCE_BTC_ETH_STRATIFIED_PURGED_CONTEXTS_20261009_V1"
FORMAT_PILOT_DECISION_ID = "v38sp-optimization-20251010-s1-1200z-btcusdt"
FORMAT_PILOT_MARKET_INPUT_SHA256 = "7b70153bc27d4051af7821cf9f3518610d7487687f4f9b7edddf9f6dd5bf176a"
FORMAT_PILOT_PROMPT_VERSION = "pa_decision_quality_v38-A1.2-json-envelope-pilot"
FORMAT_PILOT_PROMPT_SHA256 = "a6828a5c4fb33d62f3256dec2b09c09b36057aca7d08f8ac1fc9afd666c1f37d"
FORMAT_PILOT_PARSER_VERSION = RAW_OR_SINGLE_JSON_FENCE_V1
FORMAT_PILOT_MAX_REQUESTS = 1
FORMAT_PILOT_MAX_OUTPUT_TOKENS = 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class FormatPilotSpec:
    authorization_id: str
    dataset_id: str
    dataset_manifest_sha256: str
    visible_inputs_sha256: str
    decision_id: str
    market_input_sha256: str
    arm_id: str
    prompt_version: str
    prompt_file_sha256: str
    parser_version: str
    max_requests: int = 1
    max_output_tokens: int = 1024
    max_wire_prompt_bytes: int = 32768


FORMAT_PILOT_SPEC = FormatPilotSpec(
    authorization_id=FORMAT_PILOT_AUTHORIZATION_ID,
    dataset_id=FORMAT_PILOT_DATASET_ID,
    dataset_manifest_sha256="1f9e157bad2df4c32f863c2ae7a779b7d9ed573ad09d37f49ca44590eca4352a",
    visible_inputs_sha256="4fd074eafb73ecd113c8734c5417c05878b4ee6ad6afbcb75f736b74c173399d",
    decision_id=FORMAT_PILOT_DECISION_ID,
    market_input_sha256=FORMAT_PILOT_MARKET_INPUT_SHA256,
    arm_id="A1_MARKET_ONLY",
    prompt_version=FORMAT_PILOT_PROMPT_VERSION,
    prompt_file_sha256=FORMAT_PILOT_PROMPT_SHA256,
    parser_version=FORMAT_PILOT_PARSER_VERSION,
)


def validate_format_pilot_authorization(
    authorization: Any,
    prompt: Any,
    *,
    prompt_bytes: bytes,
    market_input: dict[str, Any],
    spec: FormatPilotSpec = FORMAT_PILOT_SPEC,
) -> None:
    """Fail closed unless the one-intent optimization-only contract is exact."""
    if (not isinstance(authorization, dict)
            or authorization.get("schema_version") != "pa-market-only-v38/format-pilot-authorization-1"
            or authorization.get("authorization_id") != spec.authorization_id
            or authorization.get("authorized_by") != "user_in_this_conversation"
            or authorization.get("provider") != "Antigravity Tools reverse proxy"
            or authorization.get("model_id") != DEFAULT_MODEL):
        raise MarketOnlyError("FORMAT_PILOT_AUTHORIZATION_HEADER_INVALID")
    scope = authorization.get("scope")
    expected_scope = {
        "dataset_id": spec.dataset_id,
        "dataset_manifest_sha256": spec.dataset_manifest_sha256,
        "visible_inputs_sha256": spec.visible_inputs_sha256,
        "allowed_partition": "optimization",
        "selection_policy": "FIRST_SORTED_OPTIMIZATION_CONTEXT_V1",
        "selected_decision_id": spec.decision_id,
        "selected_market_input_sha256": spec.market_input_sha256,
        "arm_id": spec.arm_id,
        "prompt_version": spec.prompt_version,
        "prompt_file": "configs/research/prompts/v38-market-only-a1-json-format-pilot-v1.json",
        "prompt_file_sha256": spec.prompt_file_sha256,
        "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
        "response_parser_version": spec.parser_version,
        "maximum_contexts": 1,
        "maximum_model_request_intents": spec.max_requests,
        "maximum_output_tokens_per_request": spec.max_output_tokens,
        "maximum_wire_prompt_bytes": spec.max_wire_prompt_bytes,
        "maximum_attempts_per_context_arm": 1,
        "maximum_transport_attempts": 1,
        "allow_automatic_retry": False,
        "allow_syntax_repair_call": False,
        "allow_validation": False,
        "allow_untouched_test": False,
        "allow_orders_or_proposals": False,
        "allow_account_or_risk_data": False,
        "allow_production_or_livemode": False,
        "quality_sample_eligible": False,
        "reused_context_from_prior_optimization_run": True,
    }
    if scope != expected_scope:
        raise MarketOnlyError("FORMAT_PILOT_AUTHORIZATION_SCOPE_INVALID")
    duplicate_policy = authorization.get("duplicate_cost_policy")
    if (not isinstance(duplicate_policy, dict)
            or duplicate_policy.get("client_retries") != 0
            or duplicate_policy.get("same_context_arm_reexecution") != "NEVER_REEXECUTE_THIS_PILOT_INTENT"
            or duplicate_policy.get("dedupe_ledger_scope") != "ONE_PER_PILOT_AUTHORIZATION"
            or duplicate_policy.get("provider_internal_deduplication") != "UNVERIFIED"
            or duplicate_policy.get("provider_billing_per_request") != "UNVERIFIED"):
        raise MarketOnlyError("FORMAT_PILOT_DUPLICATE_COST_POLICY_INVALID")
    if (hashlib.sha256(prompt_bytes).hexdigest() != spec.prompt_file_sha256
            or not isinstance(prompt, dict)
            or prompt.get("schema_version") != "pa-market-only-v38/prompt-template-2"
            or prompt.get("prompt_id") != "V38_A1_MARKET_ONLY_JSON_FORMAT_PILOT_V1"
            or prompt.get("prompt_version") != spec.prompt_version
            or prompt.get("research_arm") != spec.arm_id
            or prompt.get("input_schema_version") != INPUT_SCHEMA_VERSION
            or prompt.get("output_schema_version") != ANALYSIS_SCHEMA_VERSION
            or prompt.get("response_parser_version") != spec.parser_version
            or prompt.get("model_selection", {}).get("model_id") != DEFAULT_MODEL
            or prompt.get("call_authorization", {}).get("authorized") is not True
            or prompt.get("call_authorization", {}).get("maximum_calls") != spec.max_requests
            or prompt.get("call_authorization", {}).get("study_scope")
                != "FORMAT_PARSEABILITY_ONLY_NOT_DECISION_QUALITY"):
        raise MarketOnlyError("FORMAT_PILOT_PROMPT_HASH_OR_CONTRACT_INVALID")
    if (market_input.get("partition") != "optimization"
            or market_input.get("decision_id") != spec.decision_id
            or market_input.get("market_input_sha256") != spec.market_input_sha256):
        raise MarketOnlyError("FORMAT_PILOT_SELECTED_INPUT_INVALID")


def build_format_pilot_intent(
    market_input: dict[str, Any],
    prompt: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    run_id: str | None = None,
    request_id: str | None = None,
    spec: FormatPilotSpec = FORMAT_PILOT_SPEC,
) -> dict[str, Any]:
    if (market_input.get("partition") != "optimization"
            or market_input.get("decision_id") != spec.decision_id
            or market_input.get("market_input_sha256") != spec.market_input_sha256
            or prompt.get("prompt_version") != spec.prompt_version):
        raise MarketOnlyError("FORMAT_PILOT_INTENT_SCOPE_INVALID")
    request_id = request_id or uuid.uuid4().hex
    run_id = run_id or uuid.uuid4().hex
    if not _REQUEST_ID.fullmatch(request_id) or not _REQUEST_ID.fullmatch(run_id):
        raise MarketOnlyError("FORMAT_PILOT_REQUEST_ID_INVALID")
    return {
        "schema_version": FORMAT_PILOT_LEDGER_SCHEMA_VERSION,
        "event_type": "DISPATCH_INTENT",
        "authorization_id": spec.authorization_id,
        "dataset_id": spec.dataset_id,
        "call_key": canonical_sha256({
            "authorization_id": spec.authorization_id,
            "dataset_id": spec.dataset_id,
            "decision_id": spec.decision_id,
            "arm_id": spec.arm_id,
            "prompt_file_sha256": spec.prompt_file_sha256,
        }),
        "run_id": run_id,
        "request_id": request_id,
        "arm_id": spec.arm_id,
        "decision_id": spec.decision_id,
        "partition": "optimization",
        "symbol": market_input["symbol"],
        "market_input_sha256": spec.market_input_sha256,
        "dataset_manifest_sha256": spec.dataset_manifest_sha256,
        "visible_inputs_sha256": spec.visible_inputs_sha256,
        "prompt_version": spec.prompt_version,
        "prompt_file_sha256": spec.prompt_file_sha256,
        "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
        "response_parser_version": spec.parser_version,
        "messages_sha256": canonical_sha256(messages),
        "wire_messages_sha256": _transport_wire_messages_sha256(messages),
        "model_id_requested": DEFAULT_MODEL,
        "maximum_output_tokens": spec.max_output_tokens,
        "transport_attempt_limit": 1,
        "valid_evidence_refs": list(market_input["evidence_refs"]),
        "quality_sample_eligible": False,
        "created_at_utc": _utc_now(),
    }


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _result_event(
    intent: dict[str, Any],
    call_result: ProviderCallResult | None,
    market_input: dict[str, Any],
    *,
    failure: Exception | None = None,
    spec: FormatPilotSpec = FORMAT_PILOT_SPEC,
) -> dict[str, Any]:
    trace_source = call_result.transport_trace if call_result else getattr(failure, "transport_trace", None)
    trace = safe_transport_trace(trace_source)
    event: dict[str, Any] = {
        "schema_version": FORMAT_PILOT_LEDGER_SCHEMA_VERSION,
        "event_type": "RESULT",
        "authorization_id": spec.authorization_id,
        "dataset_id": spec.dataset_id,
        "call_key": intent["call_key"],
        "run_id": intent["run_id"],
        "request_id": intent["request_id"],
        "completed_at_utc": _utc_now(),
        "response_parser_version": spec.parser_version,
        "response_model_id": None,
        "model_identity_status": "UNVERIFIED_NO_RESPONSE",
        "transport_trace": trace,
        "usage_reported_by_provider": None,
        "provider_cost_usdt": None,
        "provider_cost_status": "UNVERIFIED_NO_PRICE_OR_BILLING_RECEIPT",
        "finish_reason": None,
        "raw_response_text": None,
        "raw_response_sha256": None,
        "raw_response_chars": None,
        "analysis": None,
        "validation_errors": [],
        "quality_sample_eligible": False,
    }
    if failure is not None:
        code = getattr(failure, "code", None)
        if not isinstance(code, str) or re.fullmatch(r"MODEL_[A-Z0-9_]{1,96}", code) is None:
            message = str(failure)
            code = message if re.fullmatch(r"MODEL_[A-Z0-9_]{1,96}", message) else "MODEL_CALL_FAILED"
        event.update(status="FORMAT_PILOT_CALL_ERROR_NO_RETRY", error_code=code)
        return event
    if call_result is None or not isinstance(call_result.payload, dict):
        event.update(status="FORMAT_PILOT_INVALID_PROVIDER_RESPONSE", error_code="PROVIDER_RESPONSE_NOT_OBJECT")
        return event

    response = call_result.payload
    response_model = call_result.response_model_id
    payload_model = response.get("model")
    event["response_model_id"] = payload_model if isinstance(payload_model, str) else None
    if payload_model != response_model:
        event.update(
            status="FORMAT_PILOT_MODEL_IDENTITY_UNVERIFIED",
            error_code="RESPONSE_MODEL_FIELDS_DISAGREE",
            model_identity_status="RESPONSE_MODEL_FIELDS_DISAGREE",
        )
        return event
    if response_model != DEFAULT_MODEL:
        event.update(
            status="FORMAT_PILOT_MODEL_IDENTITY_UNVERIFIED",
            error_code="RESPONSE_MODEL_ID_NOT_EXACT_GEMINI_3_8_FLASH_HIGH",
            model_identity_status=("UNVERIFIED_NO_RESPONSE_MODEL_ID" if response_model is None
                                   else "RESPONSE_MODEL_ID_MISMATCH"),
        )
        return event
    event["model_identity_status"] = "RESPONSE_MATCHED_REQUESTED_MODEL"

    choices = response.get("choices")
    first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    finish_reason = first.get("finish_reason") if isinstance(first, dict) else None
    event["usage_reported_by_provider"] = _safe_usage(response.get("usage"))
    event["finish_reason"] = finish_reason if isinstance(finish_reason, str) else None
    if not isinstance(content, str):
        event.update(status="FORMAT_PILOT_INVALID_PROVIDER_RESPONSE", error_code="COMPLETION_CONTENT_MISSING")
        return event
    content_bytes = content.encode("utf-8", errors="replace")
    event["raw_response_sha256"] = hashlib.sha256(content_bytes).hexdigest()
    event["raw_response_chars"] = len(content)
    if len(content) <= MAX_RECORDED_RESPONSE_CHARS:
        event["raw_response_text"] = content
    if finish_reason != "stop":
        event.update(status="FORMAT_PILOT_INVALID_FINISH", error_code="COMPLETION_DID_NOT_FINISH_WITH_STOP")
        return event
    if len(content) > MAX_RECORDED_RESPONSE_CHARS:
        event.update(status="FORMAT_PILOT_INVALID_PROVIDER_RESPONSE", error_code="COMPLETION_CONTENT_TOO_LARGE")
        return event
    if (not _trace_matches_intent(trace, intent)
            or trace.get("phase") != "COMPLETED"
            or trace.get("http_status") != 200
            or trace.get("transport_mode") != "JSON"
            or type(trace.get("request_bytes_written")) not in (int, float)
            or trace["request_bytes_written"] <= 0):
        event.update(status="FORMAT_PILOT_INVALID_TRANSPORT", error_code="COMPLETED_JSON_TRANSPORT_UNVERIFIED")
        return event

    try:
        analysis = parse_json_completion(content, parser_version=spec.parser_version)
    except MarketOnlyError as exc:
        event.update(status="FORMAT_PILOT_INVALID_JSON", error_code=exc.code)
        return event
    errors = validate_market_only_analysis(
        analysis, valid_evidence_refs=set(market_input["evidence_refs"]),
    )
    event["analysis"] = analysis
    event["validation_errors"] = errors
    if errors:
        event.update(status="FORMAT_PILOT_INVALID_ANALYSIS", error_code="MARKET_ONLY_SCHEMA_OR_EVIDENCE_INVALID")
        return event
    event.update(status="FORMAT_PILOT_ANALYSIS_VALID", error_code=None)
    return event


def _read_pilot_ledger(path: Path, spec: FormatPilotSpec) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise MarketOnlyError("FORMAT_PILOT_LEDGER_UNAVAILABLE") from exc
    events: list[dict[str, Any]] = []
    previous = "0" * 64
    for sequence, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MarketOnlyError("FORMAT_PILOT_LEDGER_INVALID_JSON") from exc
        if (not isinstance(event, dict)
                or event.get("schema_version") != FORMAT_PILOT_LEDGER_SCHEMA_VERSION
                or event.get("authorization_id") != spec.authorization_id
                or event.get("dataset_id") != spec.dataset_id
                or event.get("event_type") not in {"DISPATCH_INTENT", "RESULT"}
                or event.get("ledger_sequence") != sequence
                or event.get("previous_event_sha256") != previous):
            raise MarketOnlyError("FORMAT_PILOT_LEDGER_EVENT_INVALID")
        digest = event.get("event_sha256")
        unsigned = {key: value for key, value in event.items() if key != "event_sha256"}
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest) or canonical_sha256(unsigned) != digest:
            raise MarketOnlyError("FORMAT_PILOT_LEDGER_HASH_CHAIN_INVALID")
        previous = digest
        events.append(event)
    if (len(events) > FORMAT_PILOT_MAX_REQUESTS + 1
            or any(event.get("event_type") != expected for event, expected in zip(
                events, ("DISPATCH_INTENT", "RESULT"), strict=False,
            ))
            or (len(events) == 2 and any(
                events[0].get(key) != events[1].get(key)
                for key in ("call_key", "run_id", "request_id")
            ))):
        raise MarketOnlyError("FORMAT_PILOT_LEDGER_AT_MOST_ONCE_VIOLATION")
    if events:
        intent = events[0]
        if (intent.get("response_parser_version") != spec.parser_version
                or intent.get("decision_id") != spec.decision_id
                or intent.get("market_input_sha256") != spec.market_input_sha256
                or intent.get("prompt_file_sha256") != spec.prompt_file_sha256
                or intent.get("model_id_requested") != DEFAULT_MODEL
                or intent.get("quality_sample_eligible") is not False
                or intent.get("transport_attempt_limit") != 1):
            raise MarketOnlyError("FORMAT_PILOT_LEDGER_INTENT_BINDING_INVALID")
        expected_key = canonical_sha256({
            "authorization_id": spec.authorization_id,
            "dataset_id": spec.dataset_id,
            "decision_id": spec.decision_id,
            "arm_id": spec.arm_id,
            "prompt_file_sha256": spec.prompt_file_sha256,
        })
        if (intent.get("call_key") != expected_key
                or not isinstance(intent.get("request_id"), str)
                or not _REQUEST_ID.fullmatch(intent["request_id"])
                or not isinstance(intent.get("run_id"), str)
                or not _REQUEST_ID.fullmatch(intent["run_id"])):
            raise MarketOnlyError("FORMAT_PILOT_LEDGER_INTENT_IDENTITY_INVALID")
        if len(events) == 2:
            result = events[1]
            if (result.get("response_parser_version") != spec.parser_version
                    or result.get("request_id") != intent.get("request_id")
                    or result.get("run_id") != intent.get("run_id")
                    or result.get("call_key") != intent.get("call_key")):
                raise MarketOnlyError("FORMAT_PILOT_LEDGER_RESULT_BINDING_INVALID")
    return events


def _validate_pilot_intent(
    intent: dict[str, Any], market_input: dict[str, Any], messages: list[dict[str, str]],
    spec: FormatPilotSpec,
) -> None:
    if (intent.get("decision_id") != market_input.get("decision_id")
            or intent.get("market_input_sha256") != market_input.get("market_input_sha256")
            or intent.get("symbol") != market_input.get("symbol")
            or intent.get("messages_sha256") != canonical_sha256(messages)
            or intent.get("wire_messages_sha256") != _transport_wire_messages_sha256(messages)
            or intent.get("valid_evidence_refs") != market_input.get("evidence_refs")):
        raise MarketOnlyError("FORMAT_PILOT_LEDGER_INTENT_CONTENT_MISMATCH")


def _append_pilot_event(path: Path, event: dict[str, Any], spec: FormatPilotSpec) -> dict[str, Any]:
    events = _read_pilot_ledger(path, spec)
    if len(events) >= FORMAT_PILOT_MAX_REQUESTS + 1:
        raise MarketOnlyError("FORMAT_PILOT_REQUEST_LIMIT_EXHAUSTED")
    stamped = dict(event)
    stamped["ledger_sequence"] = len(events) + 1
    stamped["previous_event_sha256"] = events[-1]["event_sha256"] if events else "0" * 64
    stamped["event_sha256"] = canonical_sha256(stamped)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical_json_bytes(stamped).decode("utf-8") + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise MarketOnlyError("FORMAT_PILOT_LEDGER_WRITE_FAILED") from exc
    return stamped


def _validate_result_event(result: dict[str, Any], intent: dict[str, Any], market_input: dict[str, Any], spec: FormatPilotSpec) -> None:
    raw = result.get("raw_response_text")
    if result.get("status") != "FORMAT_PILOT_ANALYSIS_VALID":
        return
    trace = result.get("transport_trace")
    if (not isinstance(raw, str)
            or result.get("raw_response_chars") != len(raw)
            or result.get("raw_response_sha256") != hashlib.sha256(raw.encode("utf-8")).hexdigest()
            or result.get("response_model_id") != DEFAULT_MODEL
            or result.get("model_identity_status") != "RESPONSE_MATCHED_REQUESTED_MODEL"
            or result.get("response_parser_version") != spec.parser_version
            or result.get("finish_reason") != "stop"
            or result.get("error_code") is not None
            or result.get("quality_sample_eligible") is not False
            or not _trace_matches_intent(trace, intent)
            or trace.get("phase") != "COMPLETED"
            or trace.get("http_status") != 200
            or trace.get("transport_mode") != "JSON"
            or type(trace.get("request_bytes_written")) not in (int, float)
            or trace["request_bytes_written"] <= 0):
        raise MarketOnlyError("FORMAT_PILOT_VALID_RESULT_PROVENANCE_INVALID")
    parsed = parse_json_completion(raw, parser_version=spec.parser_version)
    analysis = result.get("analysis")
    if (parsed != analysis
            or validate_market_only_analysis(analysis, valid_evidence_refs=set(market_input["evidence_refs"]))
            or result.get("validation_errors") != []):
        raise MarketOnlyError("FORMAT_PILOT_VALID_RESULT_ANALYSIS_INVALID")


def _pilot_report(
    events: list[dict[str, Any]], *, spec: FormatPilotSpec, market_input: dict[str, Any],
    dry_run: bool = False, route_readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intent = events[0] if events else None
    result = events[1] if len(events) > 1 else None
    status = (
        "DRY_RUN" if dry_run else "NOT_DISPATCHED" if intent is None
        else "AMBIGUOUS_NO_RESULT_NEVER_RETRY" if result is None
        else str(result.get("status") or "INVALID_LEDGER_RESULT")
    )
    if result is not None:
        _validate_result_event(result, intent, market_input, spec)
    return {
        "schema_version": FORMAT_PILOT_SCHEMA_VERSION,
        "pilot_kind": "JSON_FORMAT_AND_SCHEMA_PARSEABILITY_ONLY",
        "authorization_id": spec.authorization_id,
        "provider": "Antigravity Tools reverse proxy",
        "requested_model_id": DEFAULT_MODEL,
        "dataset_id": spec.dataset_id,
        "dataset_manifest_sha256": spec.dataset_manifest_sha256,
        "visible_inputs_sha256": spec.visible_inputs_sha256,
        "partition": "optimization",
        "decision_id": spec.decision_id,
        "symbol": market_input["symbol"],
        "market_input_sha256": spec.market_input_sha256,
        "prompt_version": spec.prompt_version,
        "prompt_file_sha256": spec.prompt_file_sha256,
        "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
        "response_parser_version": spec.parser_version,
        "context_reused_from_prior_run": True,
        "quality_sample_eligible": False,
        "planned_attempt_denominator": spec.max_requests,
        "dispatch_intent_count": int(intent is not None),
        "terminal_result_count": int(result is not None),
        "completion_intents_created": int(intent is not None),
        "status": status,
        "provider_usage_reported": result.get("usage_reported_by_provider") if result else None,
        "provider_cost_usdt": None,
        "provider_cost_status": result.get("provider_cost_status") if result else "NOT_RUN",
        "provider_route_readiness": route_readiness,
        "orders_created": 0,
        "proposals_created": 0,
        "gateway_acceptances_observed": 0,
        "venue_fills_observed": 0,
        "complete_closes_observed": 0,
        "market_quality_claim_permitted": False,
        "profitability_claim_permitted": False,
        "old_campaign_records_modified": 0,
        "old_campaign_records_reclassified": 0,
        "intent": intent,
        "result": result,
    }


def _write_report_exclusive(path: Path, report: dict[str, Any], *, repository_root: Path) -> None:
    resolved = path.resolve()
    allowed_root = (repository_root / "reports" / "v38+").resolve()
    try:
        resolved.relative_to(allowed_root)
    except ValueError as exc:
        raise MarketOnlyError("FORMAT_PILOT_OUTPUT_MUST_BE_IGNORED") from exc
    if resolved.exists():
        raise MarketOnlyError("FORMAT_PILOT_OUTPUT_EXISTS_REFUSE_OVERWRITE")
    report_with_digest = dict(report)
    report_with_digest["report_sha256"] = canonical_sha256(report_with_digest)
    encoded = canonical_json_bytes(report_with_digest)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    try:
        with resolved.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise MarketOnlyError("FORMAT_PILOT_OUTPUT_EXISTS_REFUSE_OVERWRITE") from exc
    except OSError as exc:
        raise MarketOnlyError("FORMAT_PILOT_OUTPUT_WRITE_FAILED") from exc


def run_format_pilot(
    *,
    market_input: dict[str, Any],
    prompt: dict[str, Any],
    messages: list[dict[str, str]],
    ledger_path: Path,
    report_path: Path,
    repository_root: Path,
    execute: bool,
    call_model: Callable[[list[dict[str, str]], str], ProviderCallResult] | None = None,
    route_readiness: dict[str, Any] | None = None,
    ledger_root: Path | None = None,
    spec: FormatPilotSpec = FORMAT_PILOT_SPEC,
) -> dict[str, Any]:
    if not isinstance(execute, bool):
        raise MarketOnlyError("FORMAT_PILOT_EXECUTE_FLAG_INVALID")
    if execute and not callable(call_model):
        raise MarketOnlyError("FORMAT_PILOT_MODEL_CALLER_REQUIRED")
    if (len(canonical_json_bytes({"messages": messages})) > spec.max_wire_prompt_bytes
            or market_input.get("decision_id") != spec.decision_id
            or market_input.get("market_input_sha256") != spec.market_input_sha256
            or market_input.get("partition") != "optimization"
            or prompt.get("prompt_version") != spec.prompt_version):
        raise MarketOnlyError("FORMAT_PILOT_RUN_INPUT_INVALID")
    ledger_resolved = ledger_path.resolve()
    allowed_root = ((ledger_root or repository_root) / "reports" / "v38+" / "runs").resolve()
    try:
        ledger_resolved.relative_to(allowed_root)
    except ValueError as exc:
        raise MarketOnlyError("FORMAT_PILOT_LEDGER_MUST_BE_IGNORED") from exc
    report_resolved = report_path.resolve()
    report_root = (repository_root / "reports" / "v38+").resolve()
    try:
        report_resolved.relative_to(report_root)
    except ValueError as exc:
        raise MarketOnlyError("FORMAT_PILOT_OUTPUT_MUST_BE_IGNORED") from exc
    if report_resolved.exists():
        raise MarketOnlyError("FORMAT_PILOT_OUTPUT_EXISTS_REFUSE_OVERWRITE")

    events = _read_pilot_ledger(ledger_resolved, spec)
    if events:
        _validate_pilot_intent(events[0], market_input, messages, spec)
    if not execute:
        return _pilot_report(
            events, spec=spec, market_input=market_input, dry_run=True,
            route_readiness=route_readiness,
        )
    if events:
        # Existing dispatches are immutable, including intents without a result.
        return _pilot_report(
            events, spec=spec, market_input=market_input, route_readiness=route_readiness,
        )

    ledger_resolved.parent.mkdir(parents=True, exist_ok=True)
    request_id = uuid.uuid4().hex
    intent = build_format_pilot_intent(
        market_input, prompt, messages, request_id=request_id, spec=spec,
    )
    lock_path = ledger_resolved.with_name(ledger_resolved.name + ".lock")
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise MarketOnlyError("FORMAT_PILOT_LEDGER_LOCK_EXISTS_FAIL_CLOSED") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
            lock_file.write(f"pid={os.getpid()} created={_utc_now()}\n")
            lock_file.flush()
            os.fsync(lock_file.fileno())
        events = _read_pilot_ledger(ledger_resolved, spec)
        if events:
            _validate_pilot_intent(events[0], market_input, messages, spec)
            return _pilot_report(
                events, spec=spec, market_input=market_input, route_readiness=route_readiness,
            )
        intent = _append_pilot_event(ledger_resolved, intent, spec)
        try:
            call_result = call_model(messages, request_id)  # type: ignore[misc]
            result = _result_event(intent, call_result, market_input, spec=spec)
        except Exception as exc:  # noqa: BLE001 - persist ordinary provider errors; no retries
            result = _result_event(intent, None, market_input, failure=exc, spec=spec)
        _append_pilot_event(ledger_resolved, result, spec)
        events = _read_pilot_ledger(ledger_resolved, spec)
        return _pilot_report(
            events, spec=spec, market_input=market_input, route_readiness=route_readiness,
        )
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
