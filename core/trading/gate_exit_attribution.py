"""Read-only causal attribution for financially settled Gate episodes.

The settlement ledger is the authority for money.  This module independently
checks whether every native exit fill can also be tied to a verified AI reduce
decision or to a succeeded Gate protection trigger and its ordinary child
order.  It never changes settlement or decision-memory rows.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any, Mapping

from core.model_routing import DEFAULT_SMART_MODEL, is_verified_model_receipt
from .ai_strategy_book import TEMPLATES
from .strategy_execution import normalize_execution


_COMPLETED_ACTIONS = {"OPEN_LONG", "OPEN_SHORT", "REDUCE_POSITION", "CLOSE_POSITION"}
_CLOSE_ACTIONS = {"REDUCE_POSITION", "CLOSE_POSITION"}


def _canonical_json(value: Any, *, compact: bool = True) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":") if compact else None,
        allow_nan=False,
    )


def _sha256(value: Any, *, compact: bool = True) -> str:
    return hashlib.sha256(_canonical_json(value, compact=compact).encode("utf-8")).hexdigest()


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def canonical_symbol(value: Any) -> str:
    text = str(value or "").upper().strip().replace(":USDT", "")
    return "".join(char for char in text if char.isalnum())


def canonical_environment(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"testnet", "gate_testnet"}:
        return "testnet"
    if text in {"live", "gate_live"}:
        return "live"
    return text


def _strategy_config_projection(strategy: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the executable/prompt configuration, excluding account metadata.

    Revision stays outside the hash as a separate cohort dimension.  Every
    field that can change the template instructions or execution cadence stays
    inside the hash.
    """
    template_id = str(strategy.get("template_id") or "").strip()
    name = str(strategy.get("name") or "").strip()
    style = str(strategy.get("style") or "").strip()
    profile = strategy.get("profile")
    sections = strategy.get("sections")
    execution = strategy.get("execution")
    if not template_id or not name or not style:
        return None
    if not isinstance(profile, dict) or not isinstance(sections, dict) or not isinstance(execution, dict):
        return None
    cadence = execution.get("scan_interval_minutes")
    if isinstance(cadence, bool):
        return None
    try:
        cadence = int(cadence)
    except (TypeError, ValueError, OverflowError):
        return None
    if cadence <= 0:
        return None
    runtime = strategy.get("nofx_runtime")
    if runtime == {}:
        runtime = None
    if runtime is not None and not isinstance(runtime, dict):
        return None
    return {
        "template_id": template_id,
        "name": name,
        "style": style,
        "scan_interval_minutes": cadence,
        "profile": profile,
        "sections": sections,
        "execution": execution,
        "nofx_runtime": runtime,
    }


def strategy_config_sha256(strategy: Mapping[str, Any] | None) -> str | None:
    if not isinstance(strategy, Mapping):
        return None
    try:
        projection = _strategy_config_projection(strategy)
        return _sha256(projection) if projection is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def frozen_template_config_sha256(template: Mapping[str, Any]) -> str | None:
    """Digest the current factory template in the same shape saved by cycles."""
    try:
        template_id = str(template.get("id") or "").strip()
        cadence = int(template.get("scan_interval_minutes") or 0)
        defaults = template.get("execution_defaults")
        if not template_id or cadence <= 0 or not isinstance(defaults, dict):
            return None
        execution = normalize_execution({
            **dict(defaults),
            "scan_interval_minutes": cadence,
            "order_preference": template.get("order_preference") or defaults.get("order_preference") or "AUTO",
        })
        projection = {
            "template_id": template_id,
            "name": str(template.get("name") or ""),
            "style": str(template.get("style") or ""),
            "scan_interval_minutes": cadence,
            "profile": template.get("profile"),
            "sections": template.get("sections"),
            "execution": execution,
            "nofx_runtime": None,
        }
        return _sha256(projection)
    except (TypeError, ValueError, OverflowError):
        return None


def frozen_template_config_digests() -> dict[str, str]:
    return {
        str(template["id"]): digest
        for template in TEMPLATES
        if (digest := frozen_template_config_sha256(template)) is not None
    }


def _parse_revision(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if str(value).strip() not in {str(parsed), f"+{parsed}"} or parsed < 0:
        return None
    return parsed


def verify_strategy_snapshot(
    cycle: Mapping[str, Any],
    *,
    expected_template_id: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Verify a cycle's exact strategy snapshot and return its cohort identity."""
    payload = _json_object(cycle.get("payload_json"))
    if payload is None:
        return None, "CYCLE_PAYLOAD_INVALID"
    strategy = payload.get("strategy_instructions")
    if not isinstance(strategy, dict):
        return None, "STRATEGY_SNAPSHOT_MISSING"
    template_id = str(strategy.get("template_id") or "").strip()
    if not template_id or (expected_template_id and template_id != expected_template_id):
        return None, "STRATEGY_TEMPLATE_MISMATCH"
    revision = _parse_revision(strategy.get("revision"))
    if revision is None:
        return None, "STRATEGY_REVISION_MISSING_OR_INVALID"

    computed_digest = strategy_config_sha256(strategy)
    if not computed_digest:
        return None, "STRATEGY_CONFIG_DIGEST_UNAVAILABLE"
    stored_values = [
        cycle.get("strategy_config_sha256"),
        payload.get("strategy_config_sha256"),
    ]
    stored_values = [str(value).lower() for value in stored_values if value not in (None, "")]
    if stored_values and any(value != computed_digest for value in stored_values):
        return None, "STRATEGY_CONFIG_DIGEST_MISMATCH"
    if not stored_values:
        # Older rows can still qualify when the actual active strategy object
        # carries its own verifiable digest.  A profile version by itself is
        # never sufficient.
        embedded = str(strategy.get("digest") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", embedded):
            return None, "STRATEGY_CONFIG_DIGEST_UNAVAILABLE"
        full_snapshot = {key: value for key, value in strategy.items() if key != "digest"}
        try:
            if _sha256(full_snapshot, compact=False) != embedded:
                return None, "STRATEGY_SNAPSHOT_DIGEST_MISMATCH"
        except (TypeError, ValueError, OverflowError):
            return None, "STRATEGY_SNAPSHOT_DIGEST_INVALID"

    column_template = cycle.get("strategy_template_id")
    if column_template not in (None, "") and str(column_template) != template_id:
        return None, "STRATEGY_TEMPLATE_PROVENANCE_MISMATCH"
    column_revision = _parse_revision(cycle.get("strategy_revision"))
    if cycle.get("strategy_revision") not in (None, "") and column_revision != revision:
        return None, "STRATEGY_REVISION_PROVENANCE_MISMATCH"
    payload_template = payload.get("strategy_template_id")
    if payload_template not in (None, "") and str(payload_template) != template_id:
        return None, "STRATEGY_TEMPLATE_PROVENANCE_MISMATCH"
    payload_revision = _parse_revision(payload.get("strategy_revision"))
    if payload.get("strategy_revision") not in (None, "") and payload_revision != revision:
        return None, "STRATEGY_REVISION_PROVENANCE_MISMATCH"

    return {
        "template_id": template_id,
        "revision": revision,
        "strategy_config_sha256": computed_digest,
        "strategy": strategy,
        "payload": payload,
    }, None


def _cycle_audit_evidence(
    payload: Mapping[str, Any], expected_action: str,
) -> tuple[bool, str | None]:
    settings = _json_object(payload.get("model_inference_settings")) or {}
    model_receipt = _json_object(payload.get("model_receipt")) or {}
    merged = {**model_receipt, **settings}
    for key in (
        "model_id", "actual_model_id", "model_identity_source",
        "verified_manifest_model_id", "model_version", "local_schema_validation",
        "request_hash",
    ):
        left, right = model_receipt.get(key), settings.get(key)
        if left not in (None, "") and right not in (None, "") and left != right:
            return False, "MODEL_RECEIPT_FIELDS_CONFLICT"
    if str(merged.get("local_schema_validation") or "").upper() != "PASS":
        return False, "MODEL_SCHEMA_VALIDATION_MISSING"

    aliases = [value for value in (payload.get("model"), payload.get("model_id"), merged.get("model_id")) if value not in (None, "")]
    if not aliases or len({str(value) for value in aliases}) != 1:
        return False, "MODEL_REQUEST_ALIAS_MISSING_OR_CONFLICTING"
    model_alias = aliases[0]
    model_versions = [value for value in (payload.get("model_version"), merged.get("model_version")) if value not in (None, "")]
    if len({str(value) for value in model_versions}) > 1:
        return False, "MODEL_VERSION_CONFLICT"
    prompt_versions = [
        value for value in (payload.get("model_call_prompt_version"), payload.get("prompt_version"))
        if isinstance(value, str) and value.strip()
    ]
    if not prompt_versions or len(set(prompt_versions)) != 1:
        return False, "MODEL_PROMPT_VERSION_MISSING_OR_CONFLICTING"
    expected_prompt = prompt_versions[0]
    input_hash = payload.get("input_hash")
    if not isinstance(input_hash, str) or re.fullmatch(r"[0-9a-fA-F]{64}", input_hash) is None:
        return False, "MODEL_INPUT_HASH_MISSING_OR_INVALID"
    request_hash = merged.get("request_hash")
    if request_hash not in (None, "") and str(request_hash).lower() != input_hash.lower():
        return False, "MODEL_REQUEST_HASH_CONFLICT"
    receipt = {
        "model_id": model_alias,
        "actual_model_id": merged.get("actual_model_id"),
        "model_identity_source": merged.get("model_identity_source"),
        "verified_manifest_model_id": merged.get("verified_manifest_model_id"),
        "model_version": model_versions[0] if model_versions else None,
        "prompt_version": expected_prompt,
        "input_hash": input_hash.lower(),
    }
    if not is_verified_model_receipt(receipt):
        return False, "MODEL_RECEIPT_UNVERIFIED"
    audit_receipts = [
        _json_object(value) for value in (
            model_receipt.get("model_response_audit"),
            settings.get("model_response_audit"),
        ) if value not in (None, "")
    ]
    if len(audit_receipts) > 1 and any(item != audit_receipts[0] for item in audit_receipts[1:]):
        return False, "MODEL_RESPONSE_AUDIT_CONFLICT"
    audit = audit_receipts[0] if audit_receipts else None
    attempts = audit.get("attempts") if audit else None
    if not isinstance(attempts, list):
        return False, "MODEL_RESPONSE_AUDIT_MISSING"
    matched_responses: list[tuple[str, str]] = []
    for item in attempts:
        if not isinstance(item, dict) or str(item.get("status") or "").upper() != "COMPLETED":
            continue
        prompt = item.get("prompt_version")
        attempt_hash = item.get("request_hash")
        if prompt != expected_prompt or str(attempt_hash or "").lower() != input_hash.lower():
            continue
        if item.get("validation_error") not in (None, "") or item.get("raw_response_truncated") is True:
            continue
        raw_response = item.get("raw_response")
        try:
            decoded = json.loads(raw_response) if isinstance(raw_response, str) else raw_response
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(decoded, dict):
            try:
                canonical_response = _canonical_json(decoded)
            except (TypeError, ValueError):
                continue
            matched_responses.append((str(decoded.get("action") or "").upper(), canonical_response))
    if not matched_responses:
        return False, "COMPLETED_MODEL_RESPONSE_NOT_BOUND_TO_DECISION"
    selected_raw = payload.get("model_raw_response")
    selected_canonical: str | None = None
    if selected_raw not in (None, ""):
        try:
            selected_decoded = json.loads(selected_raw) if isinstance(selected_raw, str) else selected_raw
            selected_canonical = _canonical_json(selected_decoded)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False, "MODEL_SELECTED_RESPONSE_INVALID"
        if selected_canonical not in {response for _action, response in matched_responses}:
            return False, "MODEL_SELECTED_RESPONSE_NOT_AUDITED"
        matched_responses = [item for item in matched_responses if item[1] == selected_canonical]
    elif len({response for _action, response in matched_responses}) > 1:
        return False, "MODEL_COMPLETED_RESPONSE_AMBIGUOUS"
    if not any(action == expected_action for action, _response in matched_responses):
        return False, "COMPLETED_MODEL_RESPONSE_NOT_BOUND_TO_DECISION"
    return True, None


def verify_model_decision_cycle(
    cycle: Mapping[str, Any],
    *,
    account_id: str,
    environment: str,
    symbol: str,
    expected_actions: set[str],
    order_intent_id: str,
    expected_template_id: str | None = None,
    expected_reduce_only: bool | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Verify completed model, action, scope, intent link, and config snapshot."""
    account = str(account_id or "").strip()
    env = canonical_environment(environment)
    target_symbol = canonical_symbol(symbol)
    cycle_id = str(cycle.get("cycle_id") or "").strip()
    if not cycle_id or str(cycle.get("account_id") or "") != account:
        return None, "CYCLE_ACCOUNT_OR_ID_MISMATCH"
    if cycle.get("environment") not in (None, "") and canonical_environment(cycle.get("environment")) != env:
        return None, "CYCLE_ENVIRONMENT_MISMATCH"
    if str(cycle.get("status") or "").upper() not in {"EXECUTED", "SUBMITTED"} or not str(cycle.get("completed_at") or "").strip():
        return None, "CYCLE_NOT_COMPLETED_EXECUTION"
    if str(cycle.get("decision_origin") or "").upper() != "MODEL":
        return None, "CYCLE_NOT_MODEL_DECISION"
    if str(cycle.get("model_call_status") or "").upper() != "MODEL_DECISION":
        return None, "MODEL_CALL_STATUS_UNVERIFIED"
    if not bool(cycle.get("model_called")):
        return None, "MODEL_CALL_NOT_RECORDED"

    action = str(cycle.get("action") or "").upper()
    if action not in expected_actions or str(cycle.get("model_result") or "").upper() != action:
        return None, "MODEL_ACTION_MISMATCH"
    payload = _json_object(cycle.get("payload_json"))
    if payload is None:
        return None, "CYCLE_PAYLOAD_INVALID"
    if str(payload.get("account_id") or "") != account:
        return None, "CYCLE_PAYLOAD_ACCOUNT_MISMATCH"
    if canonical_environment(payload.get("environment")) != env:
        return None, "CYCLE_PAYLOAD_ENVIRONMENT_MISMATCH"
    mode = str(payload.get("mode") or "").upper()
    if mode != ("TESTNET" if env == "testnet" else "LIVE"):
        return None, "CYCLE_MODE_MISMATCH"
    if str(payload.get("venue") or "").lower() != "gate":
        return None, "CYCLE_PAYLOAD_VENUE_MISMATCH"
    payload_action = str(payload.get("action") or "").upper()
    model_output = _json_object(payload.get("model_output"))
    if (
        payload_action != action
        or str(payload.get("decision_origin") or "").upper() != "MODEL"
        or payload.get("is_model_decision") is not True
        or payload.get("model_called") is not True
        or (model_output is not None and str(model_output.get("action") or "").upper() != action)
    ):
        return None, "CYCLE_PAYLOAD_MODEL_DECISION_MISMATCH"
    payload_symbol = canonical_symbol(payload.get("instrument_id"))
    if payload_symbol != target_symbol or (model_output and canonical_symbol(model_output.get("instrument_id")) != target_symbol):
        return None, "CYCLE_SYMBOL_MISMATCH"
    if str(cycle.get("order_intent_id") or "") != str(order_intent_id):
        return None, "CYCLE_ORDER_INTENT_LINK_MISMATCH"
    payload_intent = _json_object(payload.get("order_intent"))
    if payload_intent is None or str(payload_intent.get("intent_id") or "") != str(order_intent_id):
        return None, "CYCLE_PAYLOAD_INTENT_LINK_MISMATCH"
    if expected_reduce_only is not None and payload_intent.get("reduce_only") is not expected_reduce_only:
        return None, "CYCLE_PAYLOAD_INTENT_REDUCE_ONLY_MISMATCH"

    attempted = cycle.get("model_call_attempted")
    completed = cycle.get("model_call_completed")
    if attempted is not None and not bool(attempted):
        return None, "MODEL_CALL_NOT_ATTEMPTED"
    if completed is not None and not bool(completed):
        return None, "MODEL_CALL_NOT_COMPLETED"
    if payload.get("model_call_attempted") is False:
        return None, "MODEL_CALL_NOT_ATTEMPTED"
    if payload.get("model_call_completed") is False:
        return None, "MODEL_CALL_NOT_COMPLETED"
    model_ok, model_reason = _cycle_audit_evidence(payload, action)
    if not model_ok:
        return None, model_reason
    strategy, strategy_reason = verify_strategy_snapshot(cycle, expected_template_id=expected_template_id)
    if strategy is None:
        return None, strategy_reason

    return {
        "cycle_id": cycle_id,
        "action": action,
        "template_id": strategy["template_id"],
        "strategy_revision": strategy["revision"],
        "strategy_config_sha256": strategy["strategy_config_sha256"],
        "intent_strategy_version": str(payload_intent.get("strategy_version") or ""),
        "payload": payload,
        "strategy": strategy["strategy"],
    }, None


def _table_exists(db: Any, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _native_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value in (0, 1):
            return bool(value)
        return None
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def _native_boolean_fields(value: Mapping[str, Any], names: tuple[str, ...]) -> tuple[bool | None, bool]:
    present = [value[name] for name in names if name in value and value[name] not in (None, "")]
    parsed = [_native_bool(item) for item in present]
    if not parsed or any(item is None for item in parsed):
        return None, bool(present)
    return parsed[0], any(item != parsed[0] for item in parsed[1:])


def _stable_native_id(
    sources: tuple[Mapping[str, Any], ...],
    names: tuple[str, ...],
    *,
    zero_is_absent: bool = False,
) -> tuple[str | None, bool]:
    values: list[str] = []
    for source in sources:
        for name in names:
            if name not in source or source[name] in (None, ""):
                continue
            value = str(source[name]).strip()
            if not value or not value.isdigit():
                return None, True
            values.append(value)
    if not values:
        return None, False
    distinct = set(values)
    if len(distinct) > 1:
        return None, True
    value = values[0]
    if value == "0" and zero_is_absent:
        return None, False
    return value, False


def _native_side(raw: Mapping[str, Any]) -> str | None:
    side = str(raw.get("side") or "").strip().upper()
    if side in {"BUY", "SELL"}:
        return side
    size = raw.get("size")
    if isinstance(size, bool):
        return None
    try:
        parsed = float(size)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or parsed == 0:
        return None
    return "BUY" if parsed > 0 else "SELL"


def _raw_order(row: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = _json_object(row.get("raw_json"))
    if raw is None:
        return None
    nested = raw.get("info")
    if isinstance(nested, dict) and not any(key in raw for key in ("id", "order_id", "contract", "symbol")):
        return nested
    return raw


def _validated_native_order(
    db: Any,
    *,
    account_id: str,
    environment: str,
    symbol: str,
    order_id: str,
    expected_side: str,
) -> tuple[dict[str, Any] | None, str | None]:
    if not order_id or order_id.lower().startswith(("ord_unknown_", "unknown")):
        return None, "NATIVE_ORDER_ID_UNAVAILABLE"
    if not _table_exists(db, "gate_remote_order_evidence"):
        return None, "NATIVE_ORDER_READBACK_MISSING"
    rows = db.execute(
        """SELECT * FROM gate_remote_order_evidence
           WHERE account_id=? AND lower(environment)=? AND order_id=?
           ORDER BY observed_at, evidence_id""",
        (account_id, environment, order_id),
    ).fetchall()
    if not rows:
        return None, "NATIVE_ORDER_READBACK_MISSING"
    valid_raws: list[dict[str, Any]] = []
    for raw_row in rows:
        item = _row_dict(raw_row)
        raw = _raw_order(item)
        if raw is None:
            continue
        native_id, identity_conflict = _stable_native_id(
            (raw,), ("id_string", "id", "order_id"),
        )
        contract_values = [
            str(value).strip() for value in (raw.get("contract"), raw.get("symbol"), item.get("contract"))
            if value not in (None, "")
        ]
        contract = contract_values[0] if contract_values and len(set(map(canonical_symbol, contract_values))) == 1 else None
        reduced, reduce_conflict = _native_boolean_fields(
            raw, ("is_reduce_only", "reduce_only", "reduceOnly"),
        )
        side = _native_side(raw)
        status = str(raw.get("status") or "").strip().lower()
        left = raw.get("left", raw.get("remaining"))
        remaining_ok = True
        if left not in (None, ""):
            try:
                remaining = float(left)
                remaining_ok = math.isfinite(remaining) and remaining == 0
            except (TypeError, ValueError, OverflowError):
                remaining_ok = False
        if (
            not identity_conflict
            and not reduce_conflict
            and (not contract_values or len(set(map(canonical_symbol, contract_values))) == 1)
            and native_id == order_id
            and canonical_symbol(contract) == canonical_symbol(symbol)
            and reduced is True
            and side == expected_side
            and status in {"finished", "closed", "done", "complete"}
            and remaining_ok
            and item.get("is_reduce_only") in (1, True)
            and not isinstance(raw.get("initial"), dict)
            and not isinstance(raw.get("trigger"), dict)
        ):
            valid_raws.append(raw)
    if not valid_raws:
        return None, "NATIVE_ORDER_READBACK_CONTRACT_OR_REDUCE_ONLY_MISMATCH"
    return {"order_id": order_id, "raw": valid_raws[-1]}, None


def _owned_protection_ids(receipt: Mapping[str, Any]) -> dict[str, set[str]]:
    known: dict[str, set[str]] = {"stop_loss": set(), "take_profit": set()}

    def normalized_leg(value: Any) -> str | None:
        text = str(value or "").strip().lower().replace("-", "_")
        if text in {"sl", "stop", "stop_loss", "ordersl"}:
            return "stop_loss"
        if text in {"tp", "take_profit", "takeprofit", "ordertp"}:
            return "take_profit"
        return None

    def add(leg_value: Any, *ids: Any) -> None:
        leg = normalized_leg(leg_value)
        if not leg:
            return
        for value in ids:
            identifier = str(value or "").strip()
            if identifier and identifier.isdigit():
                known[leg].add(identifier)

    def scan_list(value: Any) -> None:
        if not isinstance(value, list):
            return
        for item in value:
            if not isinstance(item, dict):
                continue
            leg = item.get("leg")
            add(leg, item.get("order_id"), item.get("protection_order_id"), item.get("parent_order_id"))
            nested = item.get("leg")
            if isinstance(nested, dict):
                add(nested.get("leg"), nested.get("order_id"), nested.get("protection_order_id"))

    scan_list(receipt.get("protection_orders"))
    scan_list(receipt.get("protection_unverified_orders"))
    scan_list(receipt.get("protection_terminal_observations"))
    replacements = receipt.get("protection_replacements")
    if isinstance(replacements, dict):
        for key, item in replacements.items():
            if not isinstance(item, dict):
                continue
            leg_obj = item.get("leg") if isinstance(item.get("leg"), dict) else {}
            leg = item.get("leg_name") or leg_obj.get("leg") or key
            add(leg, item.get("old_id"), item.get("new_id"), item.get("order_id"), leg_obj.get("order_id"))
    revisions = receipt.get("protection_revisions")
    if isinstance(revisions, list):
        for revision in revisions:
            if not isinstance(revision, dict):
                continue
            scan_list(revision.get("protection_orders"))
            scan_list(revision.get("protection_unverified_orders"))
    elif isinstance(revisions, dict):
        for revision in revisions.values():
            if isinstance(revision, dict):
                scan_list(revision.get("protection_orders"))
                scan_list(revision.get("protection_unverified_orders"))
    return known


def _timestamp_ms(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)) or value <= 0:
            return None
        return int(value if value > 100_000_000_000 else value * 1000)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
            if math.isfinite(number) and number > 0:
                return int(number if number > 100_000_000_000 else number * 1000)
        except ValueError:
            pass
        try:
            point = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if point.tzinfo is None:
            point = point.replace(tzinfo=timezone.utc)
        return int(point.astimezone(timezone.utc).timestamp() * 1000)
    return None


def _protection_child(
    db: Any,
    *,
    observation: Mapping[str, Any],
    entry_receipt: Mapping[str, Any],
    account_id: str,
    environment: str,
    symbol: str,
    entry_order_id: str,
    side: str,
) -> tuple[dict[str, Any] | None, str | None]:
    leg_text = str(observation.get("leg") or "").strip().lower().replace("-", "_")
    leg = {"sl": "stop_loss", "stop": "stop_loss", "stop_loss": "stop_loss",
           "tp": "take_profit", "takeprofit": "take_profit", "take_profit": "take_profit"}.get(leg_text)
    if leg is None:
        return None, "PROTECTION_LEG_INVALID"
    observation_account = str(observation.get("account_id") or "")
    observation_env = canonical_environment(observation.get("environment"))
    observation_symbol = canonical_symbol(observation.get("symbol"))
    if observation_account != account_id or observation_env != environment or observation_symbol != canonical_symbol(symbol):
        return None, "PROTECTION_SCOPE_MISMATCH"
    observed_at = _timestamp_ms(observation.get("observed_at"))
    if observed_at is None:
        return None, "PROTECTION_OBSERVATION_TIME_MISSING"

    readback = _json_object(observation.get("native_readback")) or _json_object(observation.get("readback")) or {}
    raw = _json_object(readback.get("raw")) or _json_object(readback.get("native")) or readback
    if not isinstance(raw, dict):
        return None, "PROTECTION_NATIVE_READBACK_MISSING"
    initial = _json_object(raw.get("initial")) or _json_object(readback.get("initial"))
    if initial is None:
        return None, "PROTECTION_NATIVE_INITIAL_FIELDS_MISSING"
    parent_id = str(
        observation.get("protection_order_id")
        or observation.get("conditional_order_id")
        or observation.get("parent_conditional_order_id")
        or (observation.get("parent_order_id") if str(observation.get("parent_order_id") or "") != entry_order_id else "")
        or ""
    ).strip()
    if not parent_id:
        return None, "PROTECTION_PARENT_ID_MISSING"
    owned = _owned_protection_ids(entry_receipt)
    if parent_id not in owned[leg] or parent_id in owned["take_profit" if leg == "stop_loss" else "stop_loss"]:
        return None, "PROTECTION_PARENT_NOT_OWNED_BY_ENTRY_RECEIPT"
    top_parent = str(observation.get("parent_order_id") or "").strip()
    if top_parent != entry_order_id:
        return None, "PROTECTION_ENTRY_PARENT_LINK_MISMATCH"

    native_parent_id, parent_identity_conflict = _stable_native_id(
        (raw, readback), ("id_string", "id", "order_id"),
    )
    native_child_id, child_identity_conflict = _stable_native_id(
        (raw, readback, observation),
        ("trade_id_string", "trade_id", "triggered_order_id"),
    )
    associated_entry_id, association_conflict = _stable_native_id(
        (raw, readback, observation),
        ("me_order_id_string", "me_order_id"), zero_is_absent=True,
    )
    statuses = {
        str(source.get("status") or "").strip().upper()
        for source in (raw, readback, observation)
        if str(source.get("status") or "").strip()
    }
    finishes = {
        str(source.get("finish_as") or "").strip().lower()
        for source in (raw, readback, observation)
        if str(source.get("finish_as") or "").strip()
    }
    finish_status = next(iter(statuses)) if len(statuses) == 1 else ""
    finish_as = next(iter(finishes)) if len(finishes) == 1 else ""
    reduce_only, reduce_only_conflict = _native_boolean_fields(
        initial, ("reduce_only", "is_reduce_only", "reduceOnly"),
    )
    raw_contract_values = [
        str(value).strip() for value in (initial.get("contract"), initial.get("symbol"))
        if value not in (None, "")
    ]
    raw_contract = raw_contract_values[0] if raw_contract_values and len(set(raw_contract_values)) == 1 else None
    size = initial.get("size")
    texts = [
        str(value).strip() for value in (raw.get("text"), initial.get("text"))
        if value not in (None, "")
    ]
    text_leg_values = {
        "stop_loss" if value.lower().endswith("-sl") else "take_profit"
        for value in texts
        if value.lower().endswith(("-sl", "-tp"))
    }
    if (
        parent_identity_conflict
        or child_identity_conflict
        or association_conflict
        or reduce_only_conflict
        or len(raw_contract_values) > 1 and raw_contract is None
        or len(statuses) != 1
        or len(finishes) != 1
        or native_parent_id != parent_id
        or finish_status != "FINISHED"
        or finish_as != "succeeded"
        or not native_child_id
        or native_child_id == "0"
        or (associated_entry_id is not None and associated_entry_id != entry_order_id)
        or canonical_symbol(raw_contract) != canonical_symbol(symbol)
        or reduce_only is not True
        or _native_side(initial) != ("SELL" if str(side).upper() == "LONG" else "BUY")
        or (text_leg_values and text_leg_values != {leg})
    ):
        return None, "PROTECTION_PARENT_NATIVE_TERMINAL_EVIDENCE_INVALID"
    try:
        size_value = float(size)
    except (TypeError, ValueError, OverflowError):
        return None, "PROTECTION_PARENT_NATIVE_SIZE_INVALID"
    if not math.isfinite(size_value) or size_value == 0:
        return None, "PROTECTION_PARENT_NATIVE_SIZE_INVALID"

    exit_side = "SELL" if str(side).upper() == "LONG" else "BUY"
    child, child_reason = _validated_native_order(
        db,
        account_id=account_id,
        environment=environment,
        symbol=symbol,
        order_id=native_child_id,
        expected_side=exit_side,
    )
    if child is None:
        return None, child_reason
    return {
        "actor": "NATIVE_PROTECTION",
        "leg": leg,
        "parent_order_id": parent_id,
        "child_order_id": native_child_id,
        "observed_at_ms": observed_at,
        "native_order": child,
    }, None


def classify_episode_exit_actor(
    db: Any,
    *,
    account_id: str,
    environment: str,
    episode_id: str,
    symbol: str,
    side: str,
    entry_order_id: str,
    entry_intent_id: str,
    entry_receipt: Mapping[str, Any],
    entry_strategy: Mapping[str, Any],
    entry_intent_strategy_version: str,
    settlement: Mapping[str, Any],
) -> dict[str, Any]:
    """Prove one autonomous exit actor across every native close fill.

    The returned actor is intentionally conservative: a partial set of proof,
    a foreign order, a mixed AI/protection path, or a financial close-size
    attribution without actor evidence is excluded.
    """
    account = str(account_id or "").strip()
    env = canonical_environment(environment)
    target_symbol = canonical_symbol(symbol)
    entry_side = str(side or "").upper()
    exit_side = "SELL" if entry_side == "LONG" else "BUY" if entry_side == "SHORT" else ""
    exit_ids_value = settlement.get("exit_trade_ids")
    if not isinstance(exit_ids_value, list) or not exit_ids_value:
        return {"actor": None, "reason": "SETTLEMENT_EXIT_TRADE_IDS_MISSING", "exit_fill_count": 0}
    exit_ids = [str(value or "").strip() for value in exit_ids_value]
    if any(not value for value in exit_ids) or len(set(exit_ids)) != len(exit_ids):
        return {"actor": None, "reason": "SETTLEMENT_EXIT_TRADE_IDS_INVALID", "exit_fill_count": 0}
    if not exit_side:
        return {"actor": None, "reason": "EPISODE_SIDE_INVALID", "exit_fill_count": len(exit_ids)}
    if not _table_exists(db, "gate_remote_trade_evidence"):
        return {"actor": None, "reason": "NATIVE_EXIT_FILL_EVIDENCE_MISSING", "exit_fill_count": len(exit_ids)}
    if not _table_exists(db, "gate_trade_episode_attributions"):
        return {"actor": None, "reason": "M1_EXIT_EPISODE_ATTRIBUTION_MISSING", "exit_fill_count": len(exit_ids)}

    fills: list[dict[str, Any]] = []
    bases: set[str] = set()
    for trade_id in exit_ids:
        trade_rows = db.execute(
            """SELECT * FROM gate_remote_trade_evidence
               WHERE account_id=? AND lower(environment)=? AND trade_id=?""",
            (account, env, trade_id),
        ).fetchall()
        if len(trade_rows) != 1:
            return {"actor": None, "reason": "NATIVE_EXIT_FILL_ID_MISSING_OR_AMBIGUOUS", "exit_fill_count": len(exit_ids)}
        trade = _row_dict(trade_rows[0])
        if (
            str(trade.get("venue") or "gate").lower() != "gate"
            or canonical_symbol(trade.get("canonical_symbol") or trade.get("contract")) != target_symbol
            or str(trade.get("side") or "").upper() != exit_side
            or not str(trade.get("order_id") or "").strip()
        ):
            return {"actor": None, "reason": "NATIVE_EXIT_FILL_SCOPE_OR_DIRECTION_MISMATCH", "exit_fill_count": len(exit_ids)}
        attribution_rows = db.execute(
            """SELECT economic_role,attribution_status,basis,episode_id
               FROM gate_trade_episode_attributions
               WHERE account_id=? AND lower(environment)=? AND trade_id=?
               ORDER BY attribution_id""",
            (account, env, trade_id),
        ).fetchall()
        verified = [
            _row_dict(row) for row in attribution_rows
            if str(row["episode_id"] or "") == episode_id
            and str(row["economic_role"] or "").upper() == "CLOSE"
            and str(row["attribution_status"] or "").upper() == "VERIFIED"
        ]
        if not verified or any(str(row["episode_id"] or "") != episode_id for row in attribution_rows if str(row["attribution_status"] or "").upper() == "VERIFIED"):
            basis_values = {str(row["basis"] or "").upper() for row in attribution_rows}
            if "EXCLUSIVE_FLAT_BASELINE_NATIVE_CLOSE_SIZE_POSITION_CLOSE" in basis_values:
                reason = "FINANCIAL_CLOSE_SIZE_ONLY_ACTOR_UNPROVEN"
            elif "SYSTEM_REDUCE_ONLY_ORDER_ID" in basis_values:
                reason = "SYSTEM_REDUCE_ORDER_WITHOUT_VALID_AI_EXIT_CYCLE"
            else:
                reason = "M1_EXIT_EPISODE_ATTRIBUTION_UNVERIFIED"
            return {"actor": None, "reason": reason, "exit_fill_count": len(exit_ids)}
        basis_set = {str(row.get("basis") or "").upper() for row in verified}
        if len(basis_set) != 1:
            return {"actor": None, "reason": "M1_EXIT_ATTRIBUTION_BASIS_AMBIGUOUS", "exit_fill_count": len(exit_ids)}
        bases.update(basis_set)
        fills.append({"trade_id": trade_id, "order_id": str(trade["order_id"]), "row": trade})

    # Any known close attribution outside the full-cost settlement's fill set
    # makes the actor set incomplete for this episode.
    all_verified_ids = {
        str(row[0]) for row in db.execute(
            """SELECT DISTINCT trade_id FROM gate_trade_episode_attributions
               WHERE account_id=? AND lower(environment)=? AND episode_id=?
                 AND upper(economic_role)='CLOSE' AND upper(attribution_status)='VERIFIED'""",
            (account, env, episode_id),
        ).fetchall()
    }
    if all_verified_ids != set(exit_ids):
        return {"actor": None, "reason": "M1_EXIT_FILL_SET_MISMATCH", "exit_fill_count": len(exit_ids)}

    # System protection candidate chain: the entry receipt must own the exact
    # conditional ID and leg; Gate's successful trade_id is the child order ID.
    candidates_by_child: dict[str, list[dict[str, Any]]] = {}
    observations = entry_receipt.get("protection_terminal_observations")
    if isinstance(observations, list):
        for item in observations:
            if not isinstance(item, dict):
                continue
            candidate, _reason = _protection_child(
                db,
                observation=item,
                entry_receipt=entry_receipt,
                account_id=account,
                environment=env,
                symbol=target_symbol,
                entry_order_id=entry_order_id,
                side=entry_side,
            )
            if candidate:
                candidates_by_child.setdefault(candidate["child_order_id"], []).append(candidate)

    orders = db.execute(
        """SELECT * FROM order_intents
           WHERE account_id=? AND lower(COALESCE(environment,mode,''))=?
             AND lower(COALESCE(venue,provider,'gate'))='gate'
           ORDER BY created_at,intent_id""",
        (account, env),
    ).fetchall() if _table_exists(db, "order_intents") else []
    intents_by_order: dict[str, list[dict[str, Any]]] = {}
    for raw_intent in orders:
        intent = _row_dict(raw_intent)
        try:
            receipt = _json_object(intent.get("execution_result_json")) or {}
        except Exception:
            receipt = {}
        evidence = _json_object(receipt.get("execution_evidence")) or {}
        native_id = str(receipt.get("order_id") or receipt.get("id") or evidence.get("remote_order_id") or "").strip()
        if native_id and not native_id.lower().startswith("ord_unknown_"):
            intents_by_order.setdefault(native_id, []).append({**intent, "receipt": receipt})

    per_fill: list[dict[str, Any]] = []
    unproven_fill_reasons: list[str] = []
    for fill in fills:
        order_id = fill["order_id"]
        options: list[dict[str, Any]] = []
        for intent in intents_by_order.get(order_id, []):
            intent_id = str(intent.get("intent_id") or "")
            if not intent_id or intent_id in {entry_intent_id} or not bool(intent.get("reduce_only")):
                continue
            if (
                str(intent.get("account_id") or "") != account
                or canonical_environment(intent.get("environment") or intent.get("mode")) != env
                or str(intent.get("venue") or intent.get("provider") or "gate").lower() != "gate"
                or canonical_symbol(intent.get("instrument_id")) != target_symbol
                or str(intent.get("decision_path") or "").upper() != "AI_LED"
                or str(intent.get("control_mode") or "").upper() != "AUTONOMOUS"
                or canonical_symbol(intent.get("instrument_id")) != target_symbol
                or str(intent.get("side") or "").upper() != exit_side
            ):
                continue
            cycle_id = str(intent.get("cycle_id") or "")
            cycle_rows = db.execute(
                "SELECT * FROM ai_led_cycles WHERE account_id=? AND cycle_id=?",
                (account, cycle_id),
            ).fetchall() if cycle_id and _table_exists(db, "ai_led_cycles") else []
            if len(cycle_rows) != 1:
                continue
            cycle_info, cycle_reason = verify_model_decision_cycle(
                _row_dict(cycle_rows[0]),
                account_id=account,
                environment=env,
                symbol=target_symbol,
                expected_actions=_CLOSE_ACTIONS,
                order_intent_id=intent_id,
                expected_template_id=str(entry_strategy.get("template_id") or ""),
                expected_reduce_only=True,
            )
            if cycle_info is None:
                continue
            if (
                cycle_info["strategy_config_sha256"] != entry_strategy.get("strategy_config_sha256")
                or cycle_info["strategy_revision"] != entry_strategy.get("strategy_revision")
                or str(intent.get("strategy_version") or "") != str(entry_intent_strategy_version or "")
            ):
                continue
            native, native_reason = _validated_native_order(
                db,
                account_id=account,
                environment=env,
                symbol=target_symbol,
                order_id=order_id,
                expected_side=exit_side,
            )
            if native is None:
                continue
            options.append({"actor": "AI_REDUCE", "order_id": order_id,
                            "intent_id": intent_id, "cycle_id": cycle_id,
                            "native_order": native})

        protection_options = candidates_by_child.get(order_id, [])
        options.extend(protection_options)
        if len(options) != 1:
            if len(options) > 1:
                reason = "EXIT_ACTOR_AMBIGUOUS"
            elif bases == {"EXCLUSIVE_FLAT_BASELINE_NATIVE_CLOSE_SIZE_POSITION_CLOSE"}:
                reason = "FINANCIAL_CLOSE_SIZE_ONLY_ACTOR_UNPROVEN"
            elif bases == {"SYSTEM_REDUCE_ONLY_ORDER_ID"}:
                reason = "SYSTEM_REDUCE_ORDER_WITHOUT_VALID_AI_EXIT_CYCLE"
            else:
                reason = "EXIT_ORDER_ACTOR_UNPROVEN"
            unproven_fill_reasons.append(reason)
            continue
        per_fill.append({"trade_id": fill["trade_id"], "order_id": order_id, "proof": options[0]})

    if unproven_fill_reasons:
        if per_fill:
            reason = "MIXED_AUTONOMOUS_AND_UNPROVEN_EXIT_SOURCES"
        elif len(set(unproven_fill_reasons)) == 1:
            reason = unproven_fill_reasons[0]
        else:
            reason = "MIXED_UNPROVEN_EXIT_SOURCES"
        return {"actor": None, "reason": reason, "exit_fill_count": len(exit_ids),
                "verified_exit_fill_count": len(per_fill)}

    actor_types = {str(item["proof"]["actor"]) for item in per_fill}
    if len(actor_types) != 1:
        return {"actor": None, "reason": "MIXED_AUTONOMOUS_EXIT_ACTORS", "exit_fill_count": len(exit_ids),
                "verified_exit_fill_count": len(per_fill)}
    actor = next(iter(actor_types))
    if actor == "NATIVE_PROTECTION":
        protection_sources = {
            (item["proof"].get("leg"), item["proof"].get("parent_order_id"), item["proof"].get("child_order_id"))
            for item in per_fill
        }
        if len(protection_sources) != 1:
            return {"actor": None, "reason": "MIXED_PROTECTION_EXIT_LEGS", "exit_fill_count": len(exit_ids),
                    "verified_exit_fill_count": len(per_fill)}
    return {
        "actor": actor,
        "reason": None,
        "exit_fill_count": len(exit_ids),
        "verified_exit_fill_count": len(per_fill),
        "exit_order_ids": sorted({item["order_id"] for item in per_fill}),
        "exit_fill_ids": [item["trade_id"] for item in per_fill],
        "protection_leg": per_fill[0]["proof"].get("leg") if actor == "NATIVE_PROTECTION" else None,
    }


__all__ = [
    "canonical_environment",
    "canonical_symbol",
    "classify_episode_exit_actor",
    "frozen_template_config_digests",
    "strategy_config_sha256",
    "verify_model_decision_cycle",
    "verify_strategy_snapshot",
]
