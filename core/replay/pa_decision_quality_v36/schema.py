"""Versioned V36 research records and strict model-analysis validation."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

SCHEMA_VERSION = "pa-decision-quality-v36/1"
REGIMES = {"BULL_TREND", "BEAR_TREND", "TRADING_RANGE", "BREAKOUT_TRANSITION", "UNCERTAIN"}
BIASES = {"BULLISH", "BEARISH", "NEUTRAL", "UNCERTAIN"}
SIGNAL_QUALITY = {"STRONG", "CONDITIONAL", "WEAK", "NO_SIGNAL", "UNKNOWN"}
SIGNALS = {
    "H1", "H2", "L1", "L2", "BREAKOUT", "BREAKOUT_PULLBACK",
    "FAILED_BREAKOUT", "REVERSAL", "WEDGE", "TRADING_RANGE_REVERSAL", "NONE", "OTHER",
}
LOCATIONS = {
    "TREND_PULLBACK", "RANGE_UPPER", "RANGE_LOWER", "RANGE_MIDDLE",
    "STRUCTURE_LEVEL", "BREAKOUT_RETEST", "EXTENDED", "OTHER", "UNKNOWN",
}
MANAGEMENT_ACTIONS = {
    "REDUCE_POSITION", "CLOSE_POSITION", "TIGHTEN_STOP",
    "UPDATE_PROTECTION", "CANCEL_ORDER",
}
ACTIONS = {"LONG", "SHORT", "OPEN_LONG", "OPEN_SHORT", "WAIT", "HOLD", *MANAGEMENT_ACTIONS}
ERROR_CATEGORIES = {
    "MARKET_REGIME_ERROR", "DIRECTION_ERROR", "LOCATION_ERROR", "ENTRY_TRIGGER_ERROR",
    "STOP_PLACEMENT_ERROR", "TARGET_UNSUPPORTED", "EXECUTION_OR_COST_ERROR",
    "DATA_OR_MODEL_ERROR", "UNDETERMINED",
}


def validate_analysis(value: Any, valid_evidence_refs: set[str], *,
                      state_snapshot: dict[str, Any] | None = None) -> list[str]:
    """Return schema violations; never repairs or rewrites a model response."""
    if not isinstance(value, dict):
        return ["ANALYSIS_NOT_OBJECT"]
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return ["ANALYSIS_NOT_JSON_SERIALIZABLE"]
    forbidden_fragments = (
        "outcome", "review", "error_label", "pnl", "realized",
        "future_return", "closed_trade",
    )

    def contains_outcome_field(item: Any) -> bool:
        if isinstance(item, dict):
            return any(
                any(fragment in str(key).lower() for fragment in forbidden_fragments)
                or contains_outcome_field(child)
                for key, child in item.items()
            )
        if isinstance(item, list):
            return any(contains_outcome_field(child) for child in item)
        return False

    errors: list[str] = []
    if contains_outcome_field(value):
        errors.append("FUTURE_OR_REVIEW_DATA_FIELD_FORBIDDEN")
    context = value.get("context")
    location = value.get("location")
    signal = value.get("signal")
    risk_reward = value.get("risk_reward")
    if (not isinstance(context, dict) or not isinstance(context.get("market_regime"), str)
            or context.get("market_regime") not in REGIMES):
        errors.append("MARKET_REGIME_INVALID")
    if (not isinstance(context, dict) or not isinstance(context.get("higher_timeframe_bias"), str)
            or context.get("higher_timeframe_bias") not in BIASES):
        errors.append("HIGHER_TIMEFRAME_BIAS_INVALID")
    if (not isinstance(location, dict) or not isinstance(location.get("trade_location"), str)
            or location.get("trade_location") not in LOCATIONS):
        errors.append("TRADE_LOCATION_INVALID")
    if (not isinstance(signal, dict) or not isinstance(signal.get("setup"), str)
            or signal.get("setup") not in SIGNALS):
        errors.append("SIGNAL_INVALID")
    if (not isinstance(signal, dict) or not isinstance(signal.get("signal_quality"), str)
            or signal.get("signal_quality") not in SIGNAL_QUALITY):
        errors.append("SIGNAL_QUALITY_INVALID")
    action = value.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        errors.append("ACTION_INVALID")
    rationale = value.get("decision_rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        errors.append("DECISION_RATIONALE_INVALID")
    counter = value.get("counter_evidence")
    if not isinstance(counter, list) or any(not isinstance(item, str) or not item.strip() for item in counter):
        errors.append("COUNTER_EVIDENCE_INVALID")
    refs = value.get("evidence_refs")
    if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in valid_evidence_refs for ref in refs):
        errors.append("EVIDENCE_REFERENCE_INVALID")
    hypotheses = value.get("future_hypotheses")
    if not isinstance(hypotheses, list) or any(
        not isinstance(item, str) or not item.strip() for item in hypotheses
    ):
        errors.append("FUTURE_HYPOTHESES_INVALID")
    if isinstance(action, str) and action in {"LONG", "SHORT", "OPEN_LONG", "OPEN_SHORT"}:
        if not isinstance(risk_reward, dict):
            errors.append("OPEN_RISK_REWARD_MISSING")
        else:
            for key in (
                "entry_trigger", "invalidation", "entry_price", "stop_price", "target_price",
                "target_structure", "target_evidence_refs", "net_reward_risk", "order_type",
                "proposed_notional_usdt", "requested_leverage",
            ):
                if key not in risk_reward or risk_reward[key] is None:
                    errors.append(f"OPEN_{key.upper()}_MISSING")
            for key in ("entry_trigger", "invalidation", "target_structure"):
                if key in risk_reward and (
                    not isinstance(risk_reward[key], str) or not risk_reward[key].strip()
                ):
                    errors.append(f"OPEN_{key.upper()}_INVALID")
            order_type = risk_reward.get("order_type")
            if not isinstance(order_type, str) or order_type not in {"LIMIT", "MARKET", "limit", "market"}:
                errors.append("OPEN_ORDER_TYPE_INVALID")
            prices: dict[str, Decimal] = {}
            for key in ("entry_price", "stop_price", "target_price",
                        "proposed_notional_usdt", "requested_leverage"):
                try:
                    raw = risk_reward.get(key)
                    if isinstance(raw, bool) or raw is None:
                        raise InvalidOperation
                    number = Decimal(str(raw))
                    if not number.is_finite() or number <= 0:
                        raise InvalidOperation
                    prices[key] = number
                except (InvalidOperation, TypeError, ValueError):
                    errors.append(f"OPEN_{key.upper()}_INVALID")
            side = "LONG" if action in {"LONG", "OPEN_LONG"} else "SHORT"
            if all(key in prices for key in ("entry_price", "stop_price", "target_price")):
                entry, stop, target = (prices[key] for key in ("entry_price", "stop_price", "target_price"))
                if not ((side == "LONG" and stop < entry < target)
                        or (side == "SHORT" and target < entry < stop)):
                    errors.append("OPEN_PRICE_GEOMETRY_INVALID")
            target_refs = risk_reward.get("target_evidence_refs")
            if not isinstance(target_refs, list) or not target_refs or any(
                not isinstance(ref, str) or ref not in valid_evidence_refs for ref in target_refs
            ):
                errors.append("TARGET_EVIDENCE_REFERENCE_INVALID")
            try:
                raw_rr = risk_reward.get("net_reward_risk")
                if isinstance(raw_rr, bool):
                    raise TypeError
                rr = float(raw_rr)
                if not (rr >= 0 and rr < float("inf")):
                    errors.append("NET_REWARD_RISK_INVALID")
            except (TypeError, ValueError, OverflowError):
                errors.append("NET_REWARD_RISK_INVALID")
        if not counter:
            errors.append("OPEN_COUNTER_EVIDENCE_REQUIRED")
    elif isinstance(action, str) and action in MANAGEMENT_ACTIONS:
        management = value.get("management")
        positions = state_snapshot.get("positions", []) if isinstance(state_snapshot, dict) else []
        orders = state_snapshot.get("working_orders", []) if isinstance(state_snapshot, dict) else []
        position_map = {
            row.get("position_ref"): row for row in positions
            if isinstance(row, dict) and isinstance(row.get("position_ref"), str)
        }
        order_refs = {
            row.get("order_ref") for row in orders
            if isinstance(row, dict) and isinstance(row.get("order_ref"), str)
        }
        if not isinstance(management, dict):
            errors.append("MANAGEMENT_PROPOSAL_MISSING")
        else:
            if not refs:
                errors.append("MANAGEMENT_EVIDENCE_REQUIRED")
            if action == "CANCEL_ORDER":
                order_ref = management.get("order_ref")
                if not isinstance(order_ref, str) or order_ref not in order_refs:
                    errors.append("MANAGEMENT_ORDER_REFERENCE_INVALID")
            else:
                position_ref = management.get("position_ref")
                position = position_map.get(position_ref) if isinstance(position_ref, str) else None
                if position is None:
                    errors.append("MANAGEMENT_POSITION_REFERENCE_INVALID")
                if action == "REDUCE_POSITION":
                    try:
                        raw_fraction = management.get("reduce_fraction")
                        if isinstance(raw_fraction, bool):
                            raise TypeError
                        fraction = Decimal(str(raw_fraction))
                        if not fraction.is_finite() or not (0 < fraction <= 1):
                            raise InvalidOperation
                    except (InvalidOperation, TypeError, ValueError):
                        errors.append("REDUCE_FRACTION_INVALID")
                if action in {"TIGHTEN_STOP", "UPDATE_PROTECTION"}:
                    raw_stop = management.get("new_stop_price")
                    raw_target = management.get("new_take_profit")
                    has_stop = raw_stop is not None
                    has_target = raw_target is not None
                    if action == "TIGHTEN_STOP" and not has_stop:
                        errors.append("NEW_STOP_PRICE_REQUIRED")
                    if action == "UPDATE_PROTECTION" and not (has_stop or has_target):
                        errors.append("PROTECTION_UPDATE_REQUIRED")
                    if has_target:
                        try:
                            target = Decimal(str(raw_target))
                            if isinstance(raw_target, bool) or not target.is_finite() or target <= 0:
                                raise InvalidOperation
                        except (InvalidOperation, TypeError, ValueError):
                            errors.append("NEW_TAKE_PROFIT_INVALID")
                    if has_stop:
                        try:
                            new_stop = Decimal(str(raw_stop))
                            if isinstance(raw_stop, bool) or not new_stop.is_finite() or new_stop <= 0:
                                raise InvalidOperation
                            if position is None or position.get("stop_price") is None:
                                raise InvalidOperation
                            current_stop = Decimal(str(position["stop_price"]))
                            side = str(position.get("side") or "").upper()
                            if not current_stop.is_finite() or not (
                                (side == "LONG" and new_stop > current_stop)
                                or (side == "SHORT" and new_stop < current_stop)
                            ):
                                raise InvalidOperation
                        except (InvalidOperation, TypeError, ValueError):
                            errors.append("STOP_UPDATE_MUST_TIGHTEN")
    return sorted(set(errors))
