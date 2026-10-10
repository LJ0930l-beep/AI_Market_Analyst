"""Frozen definitions and prompt contracts for A0/A1/A2/A3."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any

from .context import CausalContext, _canonical_sha, _stamp, _utc
from .validation import schema_version_for_experiment

EXPERIMENTS: dict[str, dict[str, Any]] = {
    "A0": {
        "name": "V35_original_decision_flow",
        "prompt_version": "v35-as-recorded",
        "kind": "EXACT_CACHE_REPLAY_ONLY",
        "requires_exact_cache": True,
        "calls_model": False,
        "analysis_schema_version": schema_version_for_experiment("A0"),
    },
    "A1": {
        "name": "context_location_signal",
        "prompt_version": "pa_decision_quality_v36-A1.1",
        "kind": "STRUCTURED_PRICE_ACTION_ANALYSIS",
        "requires_exact_cache": False,
        "calls_model": True,
        "analysis_schema_version": schema_version_for_experiment("A1"),
    },
    "A2": {
        "name": "A1_target_space_and_counter_evidence",
        "prompt_version": "pa_decision_quality_v36-A2.1",
        "kind": "STRUCTURED_ANALYSIS_WITH_TARGET_EVIDENCE",
        "requires_exact_cache": False,
        "calls_model": True,
        "analysis_schema_version": schema_version_for_experiment("A2"),
    },
    "A3": {
        "name": "fixed_failed_breakout_rule_baseline",
        "prompt_version": "pa_decision_quality_v36-A3.1",
        "kind": "DETERMINISTIC_RESEARCH_ONLY",
        "requires_exact_cache": False,
        "calls_model": False,
        "analysis_schema_version": None,
    },
}


def experiment_prompt(experiment_id: str, context: CausalContext,
                      state_snapshot: Any = None, risk_inputs: Any = None,
                      risk_mode: str = "FIXED_NOTIONAL") -> dict[str, Any]:
    """Return a bounded structured user payload; it never includes trade outcomes."""
    key = str(experiment_id or "").upper()
    if key not in {"A1", "A2"}:
        raise ValueError("PROMPT_NOT_AVAILABLE_FOR_EXPERIMENT")
    instructions = [
            "Interpret Context, Location and Signal separately. Report higher-timeframe bias and signal quality. Do not treat Python facts as a predicted outcome.",
        "Choose OPEN_LONG, OPEN_SHORT, WAIT or HOLD. H1/H2/L1/L2 labels alone do not authorize a trade.",
        "You may propose CLOSE_POSITION, REDUCE_POSITION, TIGHTEN_STOP, UPDATE_PROTECTION or CANCEL_ORDER only for an exact position_ref or order_ref present in the current state snapshot. These are research proposals and never execute orders.",
        "For OPEN_LONG or OPEN_SHORT, state a trigger, entry, structural invalidation, stop, target structure, proposed notional, leverage, order type, net reward/risk and strongest counter-evidence.",
        "Cite only evidence_refs included in the payload. If required evidence or an exact management target is missing, choose WAIT/HOLD and say what is unknown.",
        "WAIT and HOLD are valid decisions; do not increase trade frequency to satisfy an experiment objective.",
        "For an opening proposal, obey execution_constraints exactly. If the complete constraint snapshot is missing, choose WAIT. Never use leverage to bypass the stop-risk limit and never resize a rejected proposal silently.",
    ]
    if key == "A2":
        instructions.extend([
            "For each proposed target, cite the structure that makes it plausible and explain whether price has room to reach it.",
            "Name the strongest evidence against the direction. Do not move a target farther away just to reach the 2.0 net-RR floor.",
            "If the nearest structurally supported target cannot satisfy risk and cost constraints, choose WAIT.",
        ])
    payload = {
        "experiment_id": key,
        "prompt_version": EXPERIMENTS[key]["prompt_version"],
        "analysis_schema_version": EXPERIMENTS[key]["analysis_schema_version"],
        "analysis_instructions": instructions,
        "causal_context": context.prompt_payload(),
        "state_snapshot": safe_state_snapshot(state_snapshot),
        "execution_constraints": safe_execution_constraints(risk_inputs, risk_mode),
        "required_output_schema": {
            "context": {
                "market_regime": sorted({"BULL_TREND", "BEAR_TREND", "TRADING_RANGE", "BREAKOUT_TRANSITION", "UNCERTAIN"}),
                "higher_timeframe_bias": sorted({"BULLISH", "BEARISH", "NEUTRAL", "UNCERTAIN"}),
            },
            "location": {"trade_location": sorted({"TREND_PULLBACK", "RANGE_UPPER", "RANGE_LOWER", "RANGE_MIDDLE", "STRUCTURE_LEVEL", "BREAKOUT_RETEST", "EXTENDED", "OTHER", "UNKNOWN"})},
            "signal": {
                "setup": sorted({"H1", "H2", "L1", "L2", "BREAKOUT", "BREAKOUT_PULLBACK", "FAILED_BREAKOUT", "REVERSAL", "WEDGE", "TRADING_RANGE_REVERSAL", "NONE", "OTHER"}),
                "signal_quality": sorted({"STRONG", "CONDITIONAL", "WEAK", "NO_SIGNAL", "UNKNOWN"}),
            },
            "action": [
                "OPEN_LONG", "OPEN_SHORT", "WAIT", "HOLD", "REDUCE_POSITION",
                "CLOSE_POSITION", "TIGHTEN_STOP", "UPDATE_PROTECTION", "CANCEL_ORDER",
            ],
            "risk_reward": {"entry_trigger": None, "invalidation": None, "entry_price": None,
                            "stop_price": None, "target_price": None, "target_evidence_refs": [],
                            "target_structure": None, "net_reward_risk": None, "order_type": None,
                            "proposed_notional_usdt": None, "requested_leverage": None},
            "management": {
                "position_ref": None, "order_ref": None, "reduce_fraction": None,
                "new_stop_price": None, "new_take_profit": None,
            },
            "decision_rationale": None,
            "counter_evidence": [], "evidence_refs": [],
            "future_hypotheses": [],
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > 32_000:
        raise ValueError("PROMPT_SIZE_LIMIT_EXCEEDED")
    return payload


def safe_state_snapshot(value: Any) -> dict[str, Any] | None:
    """Project only current inventory/capacity facts, never realized outcomes."""
    if not isinstance(value, dict):
        return None
    allowed = ("equity_usdt", "available_margin_usdt", "allowed_instruments", "positions", "working_orders")
    result = {key: value[key] for key in allowed if key in value}
    if "positions" in result:
        if (not isinstance(result["positions"], list)
                or any(not isinstance(position, dict) for position in result["positions"])):
            return None
        fields = ("symbol", "side", "quantity", "entry_price", "stop_price", "target_price", "opened_at")
        projected_positions = []
        for position in result["positions"]:
            identity = {key: position[key] for key in ("symbol", "side", "quantity", "entry_price", "opened_at")
                        if key in position}
            if not all(key in identity for key in ("symbol", "side", "quantity")):
                return None
            position_ref = "position:" + hashlib.sha256(
                _canonical_sha(identity).encode("ascii")
            ).hexdigest()[:16]
            projected_positions.append({
                "position_ref": position_ref,
                **{key: position[key] for key in fields if key in position},
            })
        if len({item["position_ref"] for item in projected_positions}) != len(projected_positions):
            return None
        result["positions"] = projected_positions
    if "working_orders" in result:
        if (not isinstance(result["working_orders"], list)
                or any(not isinstance(order, dict) for order in result["working_orders"])):
            return None
        fields = ("symbol", "side", "order_type", "price", "quantity", "status", "created_at")
        projected_orders = []
        for order in result["working_orders"]:
            identity = {key: order[key] for key in ("symbol", "side", "order_type", "price", "quantity", "created_at")
                        if key in order}
            if not all(key in identity for key in ("symbol", "order_type", "quantity")):
                return None
            order_ref = "order:" + hashlib.sha256(_canonical_sha(identity).encode("ascii")).hexdigest()[:16]
            projected_orders.append({
                "order_ref": order_ref,
                **{key: order[key] for key in fields if key in order},
            })
        if len({item["order_ref"] for item in projected_orders}) != len(projected_orders):
            return None
        result["working_orders"] = projected_orders
    if "allowed_instruments" in result:
        instruments = result["allowed_instruments"]
        if not isinstance(instruments, list) or any(
            not isinstance(item, str) or not item.strip() for item in instruments
        ):
            return None
    if not result:
        return None
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return None
    return result


def safe_execution_constraints(value: Any, mode: str) -> dict[str, Any]:
    """Expose only current risk, cost and contract facts needed for a proposal."""
    required = (
        "equity", "available_margin", "risk_per_trade_pct", "min_net_rr",
        "taker_fee_rate", "slippage_rate", "contract_size", "amount_step",
        "price_tick", "min_amount", "max_amount", "min_notional",
        "max_notional", "max_leverage", "quote",
    )
    if str(mode or "").strip().upper() == "FIXED_NOTIONAL":
        required = (*required, "fixed_notional_usdt")
    allowed = (*required, "max_margin_pct", "quote_source")
    if not isinstance(value, dict):
        return {"status": "INCOMPLETE", "mode": str(mode or "").strip().upper(),
                "missing_fields": list(required), "values": {}}
    values: dict[str, Any] = {}
    invalid_fields: list[str] = []
    numeric_fields = set(required) | {"max_margin_pct"}
    for key in allowed:
        if key not in value:
            continue
        if key == "quote_source":
            if isinstance(value[key], str) and value[key].strip():
                values[key] = value[key]
            else:
                invalid_fields.append(key)
            continue
        try:
            if isinstance(value[key], bool):
                raise InvalidOperation
            number = Decimal(str(value[key]))
            if not number.is_finite():
                raise InvalidOperation
            if key in numeric_fields and number <= 0 and key not in {"available_margin"}:
                raise InvalidOperation
            if key == "available_margin" and number < 0:
                raise InvalidOperation
            if key == "max_margin_pct" and not (0 < number <= 100):
                raise InvalidOperation
            values[key] = str(number)
        except (InvalidOperation, TypeError, ValueError):
            invalid_fields.append(key)
    if str(mode or "").strip().upper() not in {"FIXED_NOTIONAL", "RISK_BUDGETED_NOTIONAL"}:
        invalid_fields.append("mode")
    missing = sorted(set(required) - set(values))
    return {
        "status": "COMPLETE" if not missing and not invalid_fields else "INCOMPLETE",
        "mode": str(mode or "").strip().upper(),
        "missing_fields": missing,
        "invalid_fields": invalid_fields,
        "values": values,
        "proposal_must_remain_unchanged": True,
    }


def a0_cache_identity(point: dict[str, Any]) -> dict[str, str] | None:
    """Recompute A0 hashes from preserved prompt and input objects; never trust caller hashes."""
    source = point.get("a0_original")
    if not isinstance(source, dict):
        return None
    prompt = source.get("prompt")
    model_input = source.get("model_input")
    state = safe_state_snapshot(source.get("state_snapshot"))
    model_id = source.get("model_id")
    prompt_version = source.get("prompt_version")
    analysis_schema_version = source.get("analysis_schema_version")
    expected_schema_version = schema_version_for_experiment("A0")
    point_time = _utc(point.get("decision_time"))
    if (not isinstance(prompt, str) or not prompt or not isinstance(model_input, dict)
            or state is None or point_time is None):
        return None
    if not isinstance(model_id, str) or not model_id.strip() or not isinstance(prompt_version, str) or not prompt_version.strip():
        return None
    if analysis_schema_version != expected_schema_version:
        return None
    forbidden_fragments = (
        "outcome", "review", "error_label", "pnl", "realized",
        "future_return", "closed_trade",
    )
    def contains_forbidden(value: Any) -> bool:
        if isinstance(value, dict):
            return any(
                any(fragment in str(key).lower() for fragment in forbidden_fragments)
                or contains_forbidden(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(contains_forbidden(item) for item in value)
        return False
    if contains_forbidden(model_input) or contains_forbidden(state):
        return None
    try:
        data_hash = _canonical_sha(model_input)
        state_hash = _canonical_sha(state)
    except (TypeError, ValueError):
        return None
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return {
        "experiment_id": "A0",
        "model_id": model_id.strip(),
        "prompt_sha256": prompt_hash,
        "data_sha256": data_hash,
        "decision_time": _stamp(point_time),
        "state_sha256": state_hash,
        "prompt_version": prompt_version.strip(),
        "analysis_schema_version": expected_schema_version,
    }


def prompt_sha256(experiment_id: str, context: CausalContext,
                  state_snapshot: Any = None, risk_inputs: Any = None,
                  risk_mode: str = "FIXED_NOTIONAL") -> str:
    return _canonical_sha(experiment_prompt(
        experiment_id, context, state_snapshot, risk_inputs, risk_mode,
    ))


def exact_cache_identity(*, experiment_id: str, model_id: str,
                         prompt_sha: str, data_sha: str, decision_time: str,
                         state_sha: str, prompt_version: str,
                         analysis_schema_version: str) -> dict[str, str]:
    return {
        "experiment_id": str(experiment_id),
        "model_id": str(model_id),
        "prompt_sha256": str(prompt_sha),
        "data_sha256": str(data_sha),
        "decision_time": str(decision_time),
        "state_sha256": str(state_sha),
        "prompt_version": str(prompt_version),
        "analysis_schema_version": str(analysis_schema_version),
    }


def find_exact_cache(cache_rows: Any, expected_identity: dict[str, str]) -> dict[str, Any] | None:
    """Only return an exact identity match; no prompt/version/data relabeling."""
    if not isinstance(cache_rows, list):
        return None
    for row in cache_rows:
        if isinstance(row, dict) and row.get("identity") == expected_identity:
            return row
    return None
