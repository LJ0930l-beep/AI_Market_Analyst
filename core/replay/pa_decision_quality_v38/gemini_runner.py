"""At-most-once, market-only Gemini research runner for the frozen V38 study.

This module has no account, risk, proposal, or order integration. Importing it
does not initialize a model client or make a network request. The live client
is injected only by the explicit CLI execution path.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.ai.transport_diagnostics import safe_transport_trace
from core.model_routing import DEFAULT_MODEL
from core.replay.pa_decision_quality_v38.market_only import (
    ANALYSIS_SCHEMA_VERSION,
    INPUT_SCHEMA_VERSION,
    MarketOnlyError,
    build_market_only_input,
    validate_market_only_analysis,
    validate_stored_market_only_input,
)

AUTHORIZATION_SCHEMA_VERSION = "pa-market-only-v38/model-call-authorization-1"
RUN_SCHEMA_VERSION = "pa-market-only-v38/gemini-run-1"
LEDGER_SCHEMA_VERSION = "pa-market-only-v38/gemini-call-ledger-2"
DATASET_SCHEMA_VERSION = "pa-market-only-v38/stratified-purged-market-input-dataset-1"
DATASET_ID = "V38_BINANCE_BTC_ETH_STRATIFIED_PURGED_CONTEXTS_20261009_V1"
PLAN_SHA256 = "094ee37ff932b3f237c4eef77a42dc94cf496928a46afd5a5c30efa35cfa873f"
DATASET_MANIFEST_SHA256 = "1f9e157bad2df4c32f863c2ae7a779b7d9ed573ad09d37f49ca44590eca4352a"
VISIBLE_INPUTS_SHA256 = "4fd074eafb73ecd113c8734c5417c05878b4ee6ad6afbcb75f736b74c173399d"
PARTITION_POLICY_SHA256 = "5169daa812c643cb35b40e3c5327bfbadf30b0c6b9c7db78b172de5c561f7ac7"
AUTHORIZATION_ID = "V38_GEMINI_OPTIMIZATION_A1_A2_20261010_V1"
PROMPT_HASHES = {
    "A1_MARKET_ONLY": "b09bfbd1a9c58ef4e4d87feb3d35c2f49e24b1e7b5dd4d080d40273c760c32b4",
    "A2_MARKET_ONLY": "040f3182f7ee36d483a3fe842a95acbea56d67cf1396816c766eee6239270b19",
}
PROMPT_PATHS = {
    "A1_MARKET_ONLY": Path("configs/research/prompts/v38-market-only-a1-v1.json"),
    "A2_MARKET_ONLY": Path("configs/research/prompts/v38-market-only-a2-v1.json"),
}
MAX_CONTEXTS = 36
MAX_CALLS = 72
MAX_OUTPUT_TOKENS = 1024
MAX_WIRE_PROMPT_BYTES = 32768
MAX_RECORDED_RESPONSE_CHARS = 12000
TIME_REDACTION_POLICY = "V38_TIME_REDACTION_ORDER_PRESERVED_V1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
_ISO_TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})\b"
)
_TIME_KEYS = {
    "decision_time", "data_as_of", "bar_start", "bar_end", "available_at",
    "pivot_at", "confirmed_at", "known_at", "timestamp", "time_utc",
}

ANALYSIS_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "action", "market_view"],
    "properties": {
        "schema_version": {"const": ANALYSIS_SCHEMA_VERSION},
        "action": {"enum": ["OBSERVE", "WAIT"]},
        "market_view": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "market_regime", "higher_timeframe_bias", "location", "signal",
                "indicative_bias", "candidate_setup", "target_structure",
                "counter_evidence", "decision_rationale", "evidence_refs", "wait_reason",
            ],
            "properties": {
                "market_regime": {"enum": ["BULL_TREND", "BEAR_TREND", "TRADING_RANGE", "BREAKOUT_TRANSITION", "UNCERTAIN"]},
                "higher_timeframe_bias": {"enum": ["BULLISH", "BEARISH", "NEUTRAL", "UNCERTAIN"]},
                "location": {"enum": ["TREND_PULLBACK", "RANGE_UPPER", "RANGE_LOWER", "RANGE_MIDDLE", "STRUCTURE_LEVEL", "BREAKOUT_RETEST", "EXTENDED", "OTHER", "UNKNOWN"]},
                "signal": {
                    "type": "object", "additionalProperties": False,
                    "required": ["setup", "quality"],
                    "properties": {
                        "setup": {"enum": ["H1", "H2", "L1", "L2", "BREAKOUT", "BREAKOUT_PULLBACK", "FAILED_BREAKOUT", "REVERSAL", "WEDGE", "TRADING_RANGE_REVERSAL", "NONE", "OTHER", "UNKNOWN"]},
                        "quality": {"enum": ["STRONG", "CONDITIONAL", "WEAK", "NO_SIGNAL", "UNKNOWN"]},
                    },
                },
                "indicative_bias": {"enum": ["LONG", "SHORT", "NEUTRAL", "UNKNOWN"]},
                "candidate_setup": {
                    "type": "object", "additionalProperties": False,
                    "required": ["kind", "description", "evidence_refs"],
                    "properties": {
                        "kind": {"enum": ["NONE", "CONDITIONAL", "UNASSESSED"]},
                        "description": {"type": "string", "minLength": 1},
                        "evidence_refs": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                    },
                },
                "target_structure": {
                    "type": "object", "additionalProperties": False,
                    "required": ["kind", "rationale", "evidence_refs"],
                    "properties": {
                        "kind": {"enum": ["PRIOR_SWING", "RANGE_EXTREME", "TREND_MEASURED_MOVE", "NONE", "UNKNOWN"]},
                        "rationale": {"type": "string", "minLength": 1},
                        "evidence_refs": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                    },
                },
                "counter_evidence": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
                "decision_rationale": {"type": "string", "minLength": 1},
                "evidence_refs": {"type": "array", "minItems": 1, "items": {"type": "string"}, "uniqueItems": True},
                "wait_reason": {"enum": ["UNCERTAIN_CONTEXT", "NO_CONFIRMED_SETUP", "COUNTER_EVIDENCE_DOMINATES", "DATA_INSUFFICIENT", None]},
            },
        },
    },
}


@dataclass(frozen=True, slots=True)
class ProviderCallResult:
    """One HTTP completion response and response-sourced evidence only."""

    payload: dict[str, Any]
    response_model_id: str | None
    transport_trace: dict[str, Any] | None


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MarketOnlyError("GEMINI_RUN_NON_CANONICAL_JSON") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _transport_wire_messages_sha256(messages: list[dict[str, str]]) -> str:
    """Match CompletionTransportTrace's exact JSON encoding of the wire messages."""
    wire = json.dumps({"messages": messages}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _load_json(path: Path, error_code: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MarketOnlyError(error_code) from exc


def _validate_authorization(auth: Any) -> dict[str, Any]:
    scope = auth.get("research_scope") if isinstance(auth, dict) else None
    expected_arms = [
        {"arm_id": arm, "prompt_version": version, "prompt_file_sha256": digest}
        for arm, version, digest in (
            ("A1_MARKET_ONLY", "pa_decision_quality_v38-A1.1", PROMPT_HASHES["A1_MARKET_ONLY"]),
            ("A2_MARKET_ONLY", "pa_decision_quality_v38-A2.1", PROMPT_HASHES["A2_MARKET_ONLY"]),
        )
    ]
    expected_scope = {
        "dataset_id": DATASET_ID,
        "dataset_manifest_sha256": DATASET_MANIFEST_SHA256,
        "allowed_partition": "optimization",
        "maximum_contexts": MAX_CONTEXTS,
        "arms": expected_arms,
        "maximum_model_request_intents": MAX_CALLS,
        "maximum_output_tokens_per_request": MAX_OUTPUT_TOKENS,
        "maximum_wire_prompt_bytes": MAX_WIRE_PROMPT_BYTES,
        "maximum_attempts_per_context_arm": 1,
        "maximum_transport_attempts": 1,
        "allow_automatic_retry": False,
        "allow_syntax_repair_call": False,
        "allow_health_completion_probe": False,
        "allow_validation": False,
        "allow_untouched_test": False,
        "allow_orders_or_proposals": False,
        "allow_account_or_risk_data": False,
        "allow_production_or_livemode": False,
    }
    if (not isinstance(auth, dict)
            or auth.get("schema_version") != AUTHORIZATION_SCHEMA_VERSION
            or auth.get("authorization_id") != AUTHORIZATION_ID
            or auth.get("authorized_by") != "user_in_this_conversation"
            or auth.get("provider") != "Antigravity Tools reverse proxy"
            or auth.get("model_id") != DEFAULT_MODEL
            or scope != expected_scope):
        raise MarketOnlyError("GEMINI_RUN_AUTHORIZATION_CONTRACT_INVALID")
    duplicate_policy = auth.get("duplicate_cost_policy")
    reexecution_policy = duplicate_policy.get("same_context_arm_reexecution") if isinstance(duplicate_policy, dict) else None
    if (not isinstance(duplicate_policy, dict)
            or duplicate_policy.get("client_retries") != 0
            or not isinstance(reexecution_policy, str)
            or not reexecution_policy.startswith("NEVER")
            or duplicate_policy.get("dedupe_ledger_scope") != "ONE_PER_LINKED_WORKTREE_CLONE"
            or duplicate_policy.get("provider_internal_deduplication") != "UNVERIFIED"
            or duplicate_policy.get("provider_billing_per_request") != "UNVERIFIED"):
        raise MarketOnlyError("GEMINI_RUN_DUPLICATE_COST_POLICY_INVALID")
    return auth


def load_authorization(path: Path) -> dict[str, Any]:
    return _validate_authorization(
        _load_json(path, "GEMINI_RUN_AUTHORIZATION_UNAVAILABLE"),
    )


def load_prompt_templates(repository_root: Path) -> dict[str, dict[str, Any]]:
    prompts: dict[str, dict[str, Any]] = {}
    for arm_id, relative_path in PROMPT_PATHS.items():
        path = repository_root / relative_path
        try:
            raw = path.read_bytes()
            prompt = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MarketOnlyError("GEMINI_RUN_PROMPT_UNAVAILABLE") from exc
        if (hashlib.sha256(raw).hexdigest() != PROMPT_HASHES[arm_id]
                or not isinstance(prompt, dict)
                or prompt.get("research_arm") != arm_id
                or prompt.get("output_schema_version") != ANALYSIS_SCHEMA_VERSION
                or prompt.get("input_schema_version") != INPUT_SCHEMA_VERSION
                or not isinstance(prompt.get("task"), str)
                or not isinstance(prompt.get("constraints"), list)
                or not all(isinstance(item, str) and item.strip() for item in prompt["constraints"])):
            raise MarketOnlyError("GEMINI_RUN_PROMPT_HASH_OR_CONTRACT_MISMATCH")
        prompts[arm_id] = prompt
    return prompts


def _manifest_without_digest(manifest: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in manifest.items() if key != "manifest_sha256"}


def load_authorized_optimization_inputs(input_path: Path, manifest_path: Path) -> list[dict[str, Any]]:
    """Load only the frozen primary dataset and return rebuilt optimization inputs.

    The 18 untouched-test payloads are not included in either input file. This
    loader never opens the separate sealed hash/coverage file.
    """
    try:
        input_bytes = input_path.read_bytes()
    except OSError as exc:
        raise MarketOnlyError("V38_OPTIMIZATION_INPUT_FILE_UNAVAILABLE") from exc
    if hashlib.sha256(input_bytes).hexdigest() != VISIBLE_INPUTS_SHA256:
        raise MarketOnlyError("V38_OPTIMIZATION_INPUT_FILE_HASH_MISMATCH")
    try:
        document = json.loads(input_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MarketOnlyError("V38_OPTIMIZATION_INPUT_FILE_INVALID") from exc
    manifest = _load_json(manifest_path, "V38_DATASET_MANIFEST_UNAVAILABLE")
    if (not isinstance(manifest, dict)
            or manifest.get("manifest_sha256") != DATASET_MANIFEST_SHA256
            or canonical_sha256(_manifest_without_digest(manifest)) != DATASET_MANIFEST_SHA256
            or manifest.get("dataset_id") != DATASET_ID
            or manifest.get("plan_sha256") != PLAN_SHA256
            or manifest.get("files", {}).get("optimization-validation-inputs.json") != VISIBLE_INPUTS_SHA256):
        raise MarketOnlyError("V38_DATASET_MANIFEST_HASH_OR_CONTRACT_MISMATCH")
    if (not isinstance(document, dict)
            or set(document) != {
                "dataset_kind", "decision_points", "model_outputs_included", "outcome_labels_included",
                "partition_policy_sha256", "partition_scope", "plan_sha256", "schema_version",
                "source_database_sha256", "source_manifest_sha256",
            }
            or document.get("schema_version") != DATASET_SCHEMA_VERSION
            or document.get("dataset_kind") != "MARKET_ONLY_CAUSAL_STRATIFIED_PURGED_PAIRED_CONTEXTS_NO_LABELS"
            or document.get("model_outputs_included") is not False
            or document.get("outcome_labels_included") is not False
            or document.get("partition_policy_sha256") != PARTITION_POLICY_SHA256
            or document.get("plan_sha256") != PLAN_SHA256):
        raise MarketOnlyError("V38_OPTIMIZATION_INPUT_CONTRACT_INVALID")
    if (manifest.get("untouched_test_payloads_included") is not False
            or manifest.get("untouched_test_hash_only_count") != 18
            or manifest.get("optimization_validation_input_count") != 54
            or manifest.get("same_symbol_input_window_overlap_count") != 0
            or manifest.get("cross_partition_input_window_overlap_count") != 0):
        raise MarketOnlyError("V38_DATASET_SCOPE_OR_OVERLAP_INVALID")

    points = document.get("decision_points")
    manifest_points = manifest.get("points")
    if not isinstance(points, list) or len(points) != 54 or not isinstance(manifest_points, list):
        raise MarketOnlyError("V38_VISIBLE_POINT_COUNT_INVALID")
    expected_visible = {
        row.get("decision_id"): row
        for row in manifest_points
        if isinstance(row, dict) and row.get("partition") in {"optimization", "validation"}
    }
    if len(expected_visible) != 54:
        raise MarketOnlyError("V38_MANIFEST_VISIBLE_POINT_COUNT_INVALID")
    seen_ids: set[str] = set()
    partition_counts: Counter[str] = Counter()
    optimization_inputs: list[dict[str, Any]] = []
    for point in points:
        if (not isinstance(point, dict)
                or set(point) != {"bars_by_timeframe", "decision_id", "decision_time", "partition", "symbol"}
                or point.get("partition") not in {"optimization", "validation"}
                or not isinstance(point.get("decision_id"), str)
                or point["decision_id"] in seen_ids):
            raise MarketOnlyError("V38_VISIBLE_POINT_CONTRACT_INVALID")
        seen_ids.add(point["decision_id"])
        partition_counts[point["partition"]] += 1
        frozen = expected_visible.get(point["decision_id"])
        if (not isinstance(frozen, dict)
                or frozen.get("symbol") != point.get("symbol")
                or frozen.get("partition") != point.get("partition")
                or frozen.get("decision_time") != point.get("decision_time")):
            raise MarketOnlyError("V38_VISIBLE_POINT_MANIFEST_BINDING_INVALID")
        if point["partition"] != "optimization":
            continue
        try:
            request = build_market_only_input(point)
        except MarketOnlyError as exc:
            raise MarketOnlyError("V38_OPTIMIZATION_POINT_REBUILD_FAILED") from exc
        if (request.get("market_input_sha256") != frozen.get("market_input_sha256")
                or len(request.get("evidence_refs", [])) != frozen.get("evidence_ref_count")):
            raise MarketOnlyError("V38_OPTIMIZATION_POINT_DERIVED_INPUT_MISMATCH")
        optimization_inputs.append(request)
    if partition_counts != Counter({"optimization": 36, "validation": 18}) or len(seen_ids) != 54:
        raise MarketOnlyError("V38_VISIBLE_PARTITION_COUNTS_INVALID")
    if len(optimization_inputs) != MAX_CONTEXTS:
        raise MarketOnlyError("V38_OPTIMIZATION_CONTEXT_COUNT_INVALID")
    return sorted(optimization_inputs, key=lambda item: (item["decision_time"], item["symbol"], item["decision_id"]))


def _redact_prompt_times(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact_prompt_times(child)
            for key, child in value.items()
            if str(key).strip().lower() not in _TIME_KEYS and str(key).strip().lower() not in {"decision_id", "partition"}
        }
    if isinstance(value, list):
        return [_redact_prompt_times(item) for item in value]
    if isinstance(value, str):
        return _ISO_TIMESTAMP.sub("[TIME_REDACTED]", value)
    return value


def build_model_messages(market_input: dict[str, Any], prompt: dict[str, Any]) -> list[dict[str, str]]:
    if not validate_stored_market_only_input(market_input):
        raise MarketOnlyError("GEMINI_RUN_STORED_MARKET_INPUT_INVALID")
    arm_id = prompt.get("research_arm")
    if arm_id not in PROMPT_HASHES:
        raise MarketOnlyError("GEMINI_RUN_ARM_INVALID")
    model_input = _redact_prompt_times({
        key: value for key, value in market_input.items()
        if key not in {"decision_id", "decision_time", "partition"}
    })
    contract = {
        "research_arm": arm_id,
        "prompt_id": prompt["prompt_id"],
        "prompt_version": prompt["prompt_version"],
        "task": prompt["task"],
        "constraints": prompt["constraints"],
        "time_redaction_policy": TIME_REDACTION_POLICY,
        "rules": [
            "Return exactly one JSON object matching output_schema_version and output_schema.",
            "Do not include absolute dates, future outcomes, orders, proposals, prices for execution, account facts, or risk decisions.",
            "Use only the supplied relative-order market context and evidence references.",
            "If the evidence is insufficient, use UNKNOWN, NONE, OBSERVE, or WAIT as permitted by the schema.",
        ],
        "output_schema_version": ANALYSIS_SCHEMA_VERSION,
        "output_schema": ANALYSIS_JSON_SCHEMA,
    }
    messages = [
        {"role": "system", "content": json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))},
        {"role": "user", "content": json.dumps({
            "input_schema_version": INPUT_SCHEMA_VERSION,
            "market_input_sha256": market_input["market_input_sha256"],
            "market_input": model_input,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))},
    ]
    if len(canonical_json_bytes({"messages": messages})) > MAX_WIRE_PROMPT_BYTES:
        raise MarketOnlyError("GEMINI_RUN_WIRE_PROMPT_TOO_LARGE")
    return messages


def _call_key(decision_id: str, arm_id: str) -> str:
    return canonical_sha256({"authorization_id": AUTHORIZATION_ID, "dataset_id": DATASET_ID,
                             "decision_id": decision_id, "arm_id": arm_id})


@contextmanager
def _exclusive_ledger_lock(ledger_path: Path) -> Iterator[None]:
    lock_path = ledger_path.with_name(ledger_path.name + ".lock")
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise MarketOnlyError("GEMINI_RUN_LEDGER_LOCK_EXISTS_FAIL_CLOSED") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
            lock_file.write(f"pid={os.getpid()} created={_utc_now()}\n")
            lock_file.flush()
            os.fsync(lock_file.fileno())
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if not path.is_file():
        raise MarketOnlyError("GEMINI_RUN_LEDGER_PATH_INVALID")
    events: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                raise MarketOnlyError("GEMINI_RUN_LEDGER_BLANK_LINE_INVALID")
            event = json.loads(line)
            if (not isinstance(event, dict)
                    or event.get("schema_version") != LEDGER_SCHEMA_VERSION
                    or event.get("authorization_id") != AUTHORIZATION_ID
                    or event.get("dataset_id") != DATASET_ID
                    or event.get("event_type") not in {"DISPATCH_INTENT", "RESULT"}
                    or not _SHA256.fullmatch(str(event.get("call_key") or ""))):
                raise MarketOnlyError("GEMINI_RUN_LEDGER_EVENT_INVALID")
            events.append(event)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MarketOnlyError("GEMINI_RUN_LEDGER_INVALID") from exc
    _validate_ledger_chain(events)
    _index_events(events)
    return events


def _validate_ledger_chain(events: list[dict[str, Any]]) -> None:
    previous_digest = "0" * 64
    for expected_sequence, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            raise MarketOnlyError("GEMINI_RUN_LEDGER_EVENT_INVALID")
        event_digest = event.get("event_sha256")
        unsigned = {key: value for key, value in event.items() if key != "event_sha256"}
        if (type(event.get("ledger_sequence")) is not int
                or event["ledger_sequence"] != expected_sequence
                or event.get("previous_event_sha256") != previous_digest
                or not isinstance(event_digest, str)
                or not _SHA256.fullmatch(event_digest)
                or canonical_sha256(unsigned) != event_digest):
            raise MarketOnlyError("GEMINI_RUN_LEDGER_HASH_CHAIN_INVALID")
        previous_digest = event_digest


def _append_event(path: Path, event: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        prior_events = _read_ledger(path)
        stamped = dict(event)
        stamped["ledger_sequence"] = len(prior_events) + 1
        stamped["previous_event_sha256"] = (
            prior_events[-1]["event_sha256"] if prior_events else "0" * 64
        )
        stamped["event_sha256"] = canonical_sha256(stamped)
        encoded = canonical_json_bytes(stamped).decode("utf-8") + "\n"
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise MarketOnlyError("GEMINI_RUN_LEDGER_DURABLE_WRITE_FAILED") from exc
    return stamped


def _index_events(events: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    indexed: dict[str, dict[str, dict[str, Any]]] = {}
    for event in events:
        call_key = event["call_key"]
        entry = indexed.setdefault(call_key, {})
        event_type = event["event_type"]
        if event_type in entry:
            raise MarketOnlyError("GEMINI_RUN_LEDGER_DUPLICATE_EVENT_FAIL_CLOSED")
        entry[event_type] = event
        if event_type == "RESULT" and "DISPATCH_INTENT" not in entry:
            raise MarketOnlyError("GEMINI_RUN_LEDGER_RESULT_WITHOUT_INTENT")
    return indexed


def _safe_usage(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    output: dict[str, int] = {}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = raw.get(field)
        if type(value) is int and 0 <= value <= 1_000_000_000:
            output[field] = value
    return output or None


def _result_event(
    intent: dict[str, Any],
    call_result: ProviderCallResult | None,
    *,
    failure: Exception | None = None,
) -> dict[str, Any]:
    trace_source = call_result.transport_trace if call_result else getattr(failure, "transport_trace", None)
    trace = safe_transport_trace(trace_source)
    common = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "event_type": "RESULT",
        "authorization_id": AUTHORIZATION_ID,
        "dataset_id": DATASET_ID,
        "call_key": intent["call_key"],
        "run_id": intent["run_id"],
        "request_id": intent["request_id"],
        "completed_at_utc": _utc_now(),
        "transport_trace": trace,
        "usage_reported_by_provider": None,
        "provider_cost_usdt": None,
        "provider_cost_status": "UNVERIFIED_NO_PRICE_OR_BILLING_RECEIPT",
        "analysis": None,
        "raw_response_text": None,
        "raw_response_sha256": None,
        "raw_response_chars": None,
        "validation_errors": [],
        "response_model_id": None,
        "model_identity_status": "UNVERIFIED_NO_RESPONSE",
    }
    if failure is not None:
        code = getattr(failure, "code", None)
        safe_code = code if isinstance(code, str) and re.fullmatch(r"MODEL_[A-Z0-9_]{1,96}", code) else None
        common.update(
            status="CALL_ERROR_NO_RETRY",
            error_code=safe_code or "MODEL_CALL_FAILED",
            error_type=type(failure).__name__[:80],
        )
        return common
    if call_result is None or not isinstance(call_result.payload, dict):
        common.update(status="INVALID_PROVIDER_RESPONSE", error_code="PROVIDER_RESPONSE_NOT_OBJECT")
        return common

    response = call_result.payload
    response_model = call_result.response_model_id
    payload_model = response.get("model")
    if payload_model != response_model:
        common.update(
            status="MODEL_IDENTITY_UNVERIFIED" if payload_model is None or response_model is None
            else "MODEL_IDENTITY_MISMATCH",
            response_model_id=payload_model if isinstance(payload_model, str) else None,
            model_identity_status="RESPONSE_MODEL_FIELDS_DISAGREE",
            error_code="RESPONSE_MODEL_FIELDS_DISAGREE",
        )
        return common
    common["response_model_id"] = response_model
    if response_model == DEFAULT_MODEL:
        common["model_identity_status"] = "RESPONSE_MATCHED_REQUESTED_MODEL"
    elif response_model is None:
        common["model_identity_status"] = "UNVERIFIED_NO_RESPONSE_MODEL_ID"
    else:
        common["model_identity_status"] = "RESPONSE_MODEL_ID_MISMATCH"

    choices = response.get("choices")
    first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    finish_reason = first.get("finish_reason") if isinstance(first, dict) else None
    usage = _safe_usage(response.get("usage"))
    common["usage_reported_by_provider"] = usage
    common["finish_reason"] = finish_reason if isinstance(finish_reason, str) else None
    if not isinstance(content, str):
        common.update(status="INVALID_PROVIDER_RESPONSE", error_code="COMPLETION_CONTENT_MISSING")
        return common
    raw_bytes = content.encode("utf-8", errors="replace")
    common["raw_response_sha256"] = hashlib.sha256(raw_bytes).hexdigest()
    common["raw_response_chars"] = len(content)
    if len(content) <= MAX_RECORDED_RESPONSE_CHARS:
        common["raw_response_text"] = content
    if finish_reason != "stop":
        common.update(status="INVALID_FINISH_REASON", error_code="COMPLETION_DID_NOT_FINISH_WITH_STOP")
        return common
    if response_model != DEFAULT_MODEL:
        common.update(
            status="MODEL_IDENTITY_UNVERIFIED" if response_model is None else "MODEL_IDENTITY_MISMATCH",
            error_code="RESPONSE_MODEL_ID_NOT_EXACT_GEMINI_3_8_FLASH_HIGH",
        )
        return common
    if (not _trace_matches_intent(trace, intent)
            or trace.get("phase") != "COMPLETED"
            or trace.get("http_status") != 200
            or trace.get("transport_mode") != "JSON"
            or type(trace.get("request_bytes_written")) not in (int, float)
            or trace["request_bytes_written"] <= 0):
        common.update(status="INVALID_TRANSPORT_EVIDENCE", error_code="COMPLETED_JSON_TRANSPORT_UNVERIFIED")
        return common
    if len(content) > MAX_RECORDED_RESPONSE_CHARS:
        common.update(status="INVALID_PROVIDER_RESPONSE", error_code="COMPLETION_CONTENT_TOO_LARGE")
        return common
    try:
        analysis = json.loads(content)
    except json.JSONDecodeError:
        common.update(status="INVALID_JSON", error_code="COMPLETION_CONTENT_NOT_JSON")
        return common
    errors = validate_market_only_analysis(analysis, valid_evidence_refs=set(intent["valid_evidence_refs"]))
    common["analysis"] = analysis
    common["validation_errors"] = errors
    if errors:
        common.update(status="INVALID_ANALYSIS", error_code="MARKET_ONLY_SCHEMA_OR_EVIDENCE_INVALID")
        return common
    common.update(status="VALID_ANALYSIS", error_code=None)
    return common


def _prepare_intent(
    *, market_input: dict[str, Any], arm_id: str, prompt: dict[str, Any],
    messages: list[dict[str, str]], run_id: str,
) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "event_type": "DISPATCH_INTENT",
        "authorization_id": AUTHORIZATION_ID,
        "dataset_id": DATASET_ID,
        "call_key": _call_key(market_input["decision_id"], arm_id),
        "run_id": run_id,
        "request_id": request_id,
        "created_at_utc": _utc_now(),
        "arm_id": arm_id,
        "prompt_version": prompt["prompt_version"],
        "prompt_file_sha256": PROMPT_HASHES[arm_id],
        "decision_id": market_input["decision_id"],
        "symbol": market_input["symbol"],
        "partition": market_input["partition"],
        "market_input_sha256": market_input["market_input_sha256"],
        "messages_sha256": canonical_sha256(messages),
        "wire_messages_sha256": _transport_wire_messages_sha256(messages),
        "model_id_requested": DEFAULT_MODEL,
        "maximum_output_tokens": MAX_OUTPUT_TOKENS,
        "transport_attempt_limit": 1,
        "valid_evidence_refs": market_input["evidence_refs"],
    }


def _planned_schedule(inputs: list[dict[str, Any]], prompts: dict[str, dict[str, Any]]) -> list[tuple[dict[str, Any], str, dict[str, Any], list[dict[str, str]]]]:
    if (not isinstance(inputs, list) or not inputs or len(inputs) > MAX_CONTEXTS
            or set(prompts) != set(PROMPT_HASHES)):
        raise MarketOnlyError("GEMINI_RUN_PLAN_INPUTS_INVALID")
    verified_prompts = load_prompt_templates(Path(__file__).resolve().parents[3])
    if prompts != verified_prompts:
        raise MarketOnlyError("GEMINI_RUN_PROMPT_MAPPING_NOT_FROZEN")
    seen: set[str] = set()
    schedule = []
    for market_input in sorted(inputs, key=lambda item: (item.get("decision_time", ""), item.get("symbol", ""), item.get("decision_id", ""))):
        if (not validate_stored_market_only_input(market_input)
                or market_input.get("partition") != "optimization"
                or market_input.get("decision_id") in seen):
            raise MarketOnlyError("GEMINI_RUN_OPTIMIZATION_INPUT_INVALID")
        seen.add(market_input["decision_id"])
        for arm_id in ("A1_MARKET_ONLY", "A2_MARKET_ONLY"):
            prompt = prompts[arm_id]
            messages = build_model_messages(market_input, prompt)
            schedule.append((market_input, arm_id, prompt, messages))
    if len(schedule) > MAX_CALLS:
        raise MarketOnlyError("GEMINI_RUN_CALL_LIMIT_EXCEEDED")
    return schedule


def _trace_matches_intent(trace: Any, intent: dict[str, Any]) -> bool:
    return (
        isinstance(trace, dict)
        and safe_transport_trace(trace) == trace
        and trace.get("request_id") == intent.get("request_id")
        and trace.get("correlation_id") == intent.get("request_id")
        and type(trace.get("attempt")) is int
        and trace.get("attempt") == 1
        and trace.get("wire_messages_sha256") == intent.get("wire_messages_sha256")
    )


def _provider_stop_result(result: dict[str, Any]) -> bool:
    trace = result.get("transport_trace")
    return (
        result.get("error_code") in {"MODEL_UPSTREAM_HTTP_402", "MODEL_UPSTREAM_HTTP_429"}
        or isinstance(trace, dict) and trace.get("http_status") in {402, 429}
    )


def _validate_valid_analysis_result(result: dict[str, Any], intent: dict[str, Any]) -> None:
    raw_text = result.get("raw_response_text")
    trace = result.get("transport_trace")
    if (result.get("response_model_id") != DEFAULT_MODEL
            or result.get("model_identity_status") != "RESPONSE_MATCHED_REQUESTED_MODEL"
            or result.get("finish_reason") != "stop"
            or result.get("error_code") is not None
            or not isinstance(raw_text, str)
            or len(raw_text) > MAX_RECORDED_RESPONSE_CHARS
            or result.get("raw_response_chars") != len(raw_text)
            or result.get("raw_response_sha256") != hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
            or not _trace_matches_intent(trace, intent)
            or trace.get("phase") != "COMPLETED"
            or trace.get("http_status") != 200
            or trace.get("transport_mode") != "JSON"
            or type(trace.get("request_bytes_written")) not in (int, float)
            or trace["request_bytes_written"] <= 0):
        raise MarketOnlyError("GEMINI_RUN_VALID_ANALYSIS_PROVENANCE_INVALID")
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise MarketOnlyError("GEMINI_RUN_VALID_ANALYSIS_JSON_INVALID") from exc
    analysis = result.get("analysis")
    if (parsed != analysis
            or validate_market_only_analysis(
                parsed, valid_evidence_refs=set(intent.get("valid_evidence_refs", [])),
            )
            or result.get("validation_errors") != []):
        raise MarketOnlyError("GEMINI_RUN_VALID_ANALYSIS_SCHEMA_OR_EVIDENCE_INVALID")


def _validate_event_pairing(events: list[dict[str, Any]], schedule: list[tuple[dict[str, Any], str, dict[str, Any], list[dict[str, str]]]]) -> dict[str, dict[str, dict[str, Any]]]:
    _validate_ledger_chain(events)
    indexed = _index_events(events)
    expected = {
        _call_key(market_input["decision_id"], arm_id):
        (market_input, arm_id, prompt, messages)
        for market_input, arm_id, prompt, messages in schedule
    }
    if set(indexed) - set(expected):
        raise MarketOnlyError("GEMINI_RUN_LEDGER_HAS_OUT_OF_SCOPE_SAMPLE")
    request_ids: set[str] = set()
    for call_key, pair in indexed.items():
        intent = pair.get("DISPATCH_INTENT")
        result = pair.get("RESULT")
        if intent is None:
            raise MarketOnlyError("GEMINI_RUN_LEDGER_RESULT_WITHOUT_INTENT")
        market_input, arm_id, prompt, messages = expected[call_key]
        expected_intent = {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "event_type": "DISPATCH_INTENT",
            "authorization_id": AUTHORIZATION_ID,
            "dataset_id": DATASET_ID,
            "call_key": call_key,
            "arm_id": arm_id,
            "prompt_version": prompt["prompt_version"],
            "prompt_file_sha256": PROMPT_HASHES[arm_id],
            "decision_id": market_input["decision_id"],
            "symbol": market_input["symbol"],
            "partition": market_input["partition"],
            "market_input_sha256": market_input["market_input_sha256"],
            "messages_sha256": canonical_sha256(messages),
            "wire_messages_sha256": _transport_wire_messages_sha256(messages),
            "model_id_requested": DEFAULT_MODEL,
            "maximum_output_tokens": MAX_OUTPUT_TOKENS,
            "transport_attempt_limit": 1,
            "valid_evidence_refs": market_input["evidence_refs"],
        }
        if any(intent.get(key) != value for key, value in expected_intent.items()):
            raise MarketOnlyError("GEMINI_RUN_LEDGER_INTENT_BINDING_INVALID")
        request_id = intent.get("request_id")
        if (not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id)
                or request_id in request_ids
                or not isinstance(intent.get("run_id"), str)
                or not _REQUEST_ID.fullmatch(intent["run_id"])):
            raise MarketOnlyError("GEMINI_RUN_LEDGER_REQUEST_ID_INVALID")
        request_ids.add(request_id)
        if result is None:
            continue
        if (result.get("schema_version") != LEDGER_SCHEMA_VERSION
                or result.get("event_type") != "RESULT"
                or result.get("authorization_id") != AUTHORIZATION_ID
                or result.get("dataset_id") != DATASET_ID
                or result.get("call_key") != call_key
                or result.get("run_id") != intent.get("run_id")
                or result.get("request_id") != request_id):
            raise MarketOnlyError("GEMINI_RUN_LEDGER_RESULT_BINDING_INVALID")
        trace = result.get("transport_trace")
        if trace is not None and not _trace_matches_intent(trace, intent):
            raise MarketOnlyError("GEMINI_RUN_LEDGER_TRANSPORT_TRACE_BINDING_INVALID")
        if result.get("status") == "VALID_ANALYSIS":
            _validate_valid_analysis_result(result, intent)
    if sum("DISPATCH_INTENT" in pair for pair in indexed.values()) > MAX_CALLS:
        raise MarketOnlyError("GEMINI_RUN_LEDGER_CALL_BUDGET_EXCEEDED")
    return indexed


def execute_authorized_optimization(
    inputs: list[dict[str, Any]],
    prompts: dict[str, dict[str, Any]],
    *,
    authorization: dict[str, Any],
    ledger_path: Path,
    call_model: Callable[[list[dict[str, str]], str], ProviderCallResult],
    max_contexts: int,
) -> dict[str, Any]:
    """Run at most one request per frozen context/arm; never retries any result."""
    if (isinstance(max_contexts, bool) or not isinstance(max_contexts, int)
            or not 1 <= max_contexts <= MAX_CONTEXTS):
        raise MarketOnlyError("GEMINI_RUN_CONTEXT_LIMIT_INVALID")
    if not callable(call_model):
        raise MarketOnlyError("GEMINI_RUN_MODEL_CALLER_REQUIRED")
    _validate_authorization(authorization)
    schedule = _planned_schedule(inputs, prompts)
    run_id = uuid.uuid4().hex
    ledger_path.parent.mkdir(parents=True, exist_ok=True)

    with _exclusive_ledger_lock(ledger_path):
        events = _read_ledger(ledger_path)
        indexed = _validate_event_pairing(events, schedule)
        provider_rejection_latched = any(
            pair.get("RESULT") is not None and _provider_stop_result(pair["RESULT"])
            for pair in indexed.values()
        )
        selected_ids: list[str] = []
        for market_input, arm_id, _, _ in schedule:
            decision_id = market_input["decision_id"]
            if decision_id not in selected_ids:
                call_keys = [_call_key(decision_id, arm) for arm in PROMPT_HASHES]
                if any(key not in indexed for key in call_keys):
                    selected_ids.append(decision_id)
                if len(selected_ids) >= max_contexts:
                    break

        for market_input, arm_id, prompt, messages in schedule:
            if provider_rejection_latched:
                break
            if market_input["decision_id"] not in selected_ids:
                continue
            call_key = _call_key(market_input["decision_id"], arm_id)
            if call_key in indexed:
                # A terminal error and an intent without a result are both
                # immutable. The latter is ambiguous and cannot be retried.
                continue
            if len(indexed) >= MAX_CALLS:
                break
            intent = _prepare_intent(
                market_input=market_input, arm_id=arm_id, prompt=prompt,
                messages=messages, run_id=run_id,
            )
            intent = _append_event(ledger_path, intent)
            indexed[call_key] = {"DISPATCH_INTENT": intent}
            try:
                call_result = call_model(messages, intent["request_id"])
                result = _result_event(intent, call_result)
            except Exception as exc:  # noqa: BLE001 - persist ordinary provider failures, but not process interrupts
                result = _result_event(intent, None, failure=exc)
            result = _append_event(ledger_path, result)
            indexed[call_key]["RESULT"] = result
            if _provider_stop_result(result):
                # A quota/rate response ends this run immediately; other samples
                # remain NOT_ATTEMPTED in the fixed denominator.
                provider_rejection_latched = True
                break
    return review_gemini_ledger(inputs, prompts, _read_ledger(ledger_path))


def review_gemini_ledger(
    inputs: list[dict[str, Any]], prompts: dict[str, dict[str, Any]], events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Rebuild fixed-denominator A1/A2 market-only status without inferring quality."""
    schedule = _planned_schedule(inputs, prompts)
    indexed = _validate_event_pairing(events, schedule)
    rows: list[dict[str, Any]] = []
    statuses: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    intent_count = 0
    terminal_count = 0
    transport_written_count = 0
    response_count = 0
    usage_count = 0
    tokens = Counter()
    for market_input, arm_id, prompt, messages in schedule:
        call_key = _call_key(market_input["decision_id"], arm_id)
        pair = indexed.get(call_key, {})
        intent = pair.get("DISPATCH_INTENT")
        result = pair.get("RESULT")
        if intent is None:
            status = "NOT_ATTEMPTED"
        elif result is None:
            status = "AMBIGUOUS_NO_RESULT_NEVER_RETRY"
            intent_count += 1
        else:
            status = str(result.get("status") or "INVALID_LEDGER_RESULT")
            intent_count += 1
            terminal_count += 1
            trace = result.get("transport_trace")
            if isinstance(trace, dict) and type(trace.get("request_bytes_written")) in (int, float) and trace["request_bytes_written"] > 0:
                transport_written_count += 1
            if isinstance(trace, dict) and trace.get("http_status") in range(200, 300):
                response_count += 1
            usage = result.get("usage_reported_by_provider")
            if isinstance(usage, dict):
                usage_count += 1
                for key, value in usage.items():
                    tokens[key] += value
            analysis = result.get("analysis")
            if status == "VALID_ANALYSIS" and isinstance(analysis, dict):
                actions[analysis["action"]] += 1
        statuses[status] += 1
        rows.append({
            "schema_version": RUN_SCHEMA_VERSION,
            "dataset_id": DATASET_ID,
            "dataset_manifest_sha256": DATASET_MANIFEST_SHA256,
            "arm_id": arm_id,
            "prompt_version": prompt["prompt_version"],
            "prompt_file_sha256": PROMPT_HASHES[arm_id],
            "decision_id": market_input["decision_id"],
            "symbol": market_input["symbol"],
            "partition": market_input["partition"],
            "market_input_sha256": market_input["market_input_sha256"],
            "messages_sha256": canonical_sha256(messages),
            "status": status,
            "intent": intent,
            "result": result,
            "trade_lifecycle": {
                "proposal": "NOT_CREATED",
                "gateway_acceptance": "NOT_OBSERVED",
                "venue_fill": "NOT_OBSERVED",
                "complete_close": "NOT_OBSERVED",
            },
            "authority": {
                "production_authority": False,
                "order_creation_authorized": False,
                "account_risk_pass_claimed": False,
                "net_rr_verified": False,
            },
        })
    denominator = len(schedule)
    report: dict[str, Any] = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_mode": "AUTHORIZED_GEMINI_MARKET_ONLY_OPTIMIZATION",
        "authorization_id": AUTHORIZATION_ID,
        "provider": "Antigravity Tools reverse proxy",
        "requested_model_id": DEFAULT_MODEL,
        "dataset_id": DATASET_ID,
        "dataset_manifest_sha256": DATASET_MANIFEST_SHA256,
        "visible_inputs_sha256": VISIBLE_INPUTS_SHA256,
        "partition": "optimization",
        "time_redaction_policy": TIME_REDACTION_POLICY,
        "planned_attempt_denominator": denominator,
        "dispatch_intent_count": intent_count,
        "terminal_result_count": terminal_count,
        "transport_request_bytes_written_count": transport_written_count,
        "http_success_responses_observed": response_count,
        "provider_usage_receipt_count": usage_count,
        "provider_usage_tokens_reported": dict(tokens),
        "provider_cost_usdt": None,
        "provider_cost_status": "UNVERIFIED_NO_PRICE_OR_BILLING_RECEIPT",
        "provider_internal_deduplication": "UNVERIFIED",
        "provider_rate_or_quota_stop_latched": any(
            result is not None and _provider_stop_result(result)
            for pair in indexed.values()
            for result in [pair.get("RESULT")]
        ),
        "status_counts": dict(sorted(statuses.items())),
        "valid_analysis_records": statuses["VALID_ANALYSIS"],
        "action_counts_valid_analysis_only": dict(sorted(actions.items())),
        "orders_created": 0,
        "proposals_created": 0,
        "gateway_acceptances_observed": 0,
        "venue_fills_observed": 0,
        "complete_closes_observed": 0,
        "blind_labels_available": 0,
        "market_quality_claim_permitted": False,
        "profitability_claim_permitted": False,
        "records": rows,
    }
    report["report_sha256"] = canonical_sha256(report)
    return report
