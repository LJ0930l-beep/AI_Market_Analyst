"""Versioned, deterministic trade-plan contract helpers.

The trader service owns persistence and the gateway owns execution.  This
module only normalizes plan behavior and evaluates the small condition
language that may cross that boundary.  Free-form rationale is retained as
evidence, but is never interpreted as an executable instruction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import math
import re
from typing import Any


TRADE_PLAN_SCHEMA_VERSION = "trade_plan_v1.4"
TRADE_CONDITION_SCHEMA_VERSION = "trade_conditions_v1"


class TradePlanContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _finite_number(value: Any, *, name: str, positive: bool = False, non_negative: bool = False) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise TradePlanContractError("PLAN_NUMERIC_INVALID", f"{name} must be a finite number.")
    if not math.isfinite(parsed):
        raise TradePlanContractError("PLAN_NUMERIC_INVALID", f"{name} must be finite.")
    if positive and parsed <= 0:
        raise TradePlanContractError("PLAN_NUMERIC_INVALID", f"{name} must be greater than zero.")
    if non_negative and parsed < 0:
        raise TradePlanContractError("PLAN_NUMERIC_INVALID", f"{name} must not be negative.")
    return parsed


def _parse_time(value: Any, *, name: str, required: bool = False) -> datetime | None:
    if value is None or value == "":
        if required:
            raise TradePlanContractError("PLAN_TIME_INVALID", f"{name} is required.")
        return None
    if isinstance(value, datetime):
        point = value
    else:
        try:
            point = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise TradePlanContractError("PLAN_TIME_INVALID", f"{name} must be an ISO timestamp.")
    if point.tzinfo is None:
        raise TradePlanContractError("PLAN_TIME_INVALID", f"{name} must include a timezone.")
    return point.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


_NUMBER = r"([0-9]+(?:\.[0-9]+)?)"
_PRICE_LTE = re.compile(rf"(?:price|quote|fresh\s+quote).*?(?:at\s+or\s+below|below|<=|lte)\s*[:=]?\s*{_NUMBER}\b", re.I)
_PRICE_GTE = re.compile(rf"(?:price|quote|fresh\s+quote).*?(?:at\s+or\s+above|above|>=|gte)\s*[:=]?\s*{_NUMBER}\b", re.I)
_MAX_CHASE = re.compile(rf"(?:max(?:imum)?\s+)?(?:chase|deviation|偏离).*?{_NUMBER}\s*(?:bps|basis\s*points?|bp)\b", re.I)
_PERCENT_CHASE = re.compile(rf"(?:chase|deviation|偏离).*?{_NUMBER}\s*%", re.I)


def _entry_condition(raw: Any, condition_spec: dict[str, Any] | None) -> dict[str, Any]:
    supplied = condition_spec.get("entry") if isinstance(condition_spec, dict) else None
    supplied = supplied if supplied is not None else (condition_spec.get("entry_trigger") if isinstance(condition_spec, dict) else None)
    if supplied is not None:
        if not isinstance(supplied, dict):
            raise TradePlanContractError("PLAN_CONDITION_INVALID", "conditions.entry must be an object.")
        kind = str(supplied.get("type") or "").upper()
        if kind in {"FRESH_MARK", "IMMEDIATE"}:
            return {"type": "FRESH_MARK", "version": TRADE_CONDITION_SCHEMA_VERSION, "source": "condition_spec"}
        if kind in {"PRICE", "PRICE_THRESHOLD"}:
            operator = str(supplied.get("operator") or "").upper()
            if operator not in {"LTE", "LT", "GTE", "GT"}:
                raise TradePlanContractError("PLAN_CONDITION_INVALID", "entry price condition requires LTE/LT/GTE/GT.")
            value = _finite_number(supplied.get("value", supplied.get("threshold")), name="entry.value", positive=True)
            if value is None:
                raise TradePlanContractError("PLAN_CONDITION_INVALID", "entry.value is required.")
            return {"type": "PRICE", "operator": operator, "value": value, "version": TRADE_CONDITION_SCHEMA_VERSION, "source": "condition_spec"}
        raise TradePlanContractError("PLAN_CONDITION_UNSUPPORTED", f"Unsupported entry condition type: {kind or 'UNKNOWN'}.")

    text = str(raw or "").strip()
    if not text:
        return {"type": "UNSUPPORTED", "version": TRADE_CONDITION_SCHEMA_VERSION, "source": "entry_trigger"}
    match = _PRICE_LTE.search(text)
    if match:
        return {"type": "PRICE", "operator": "LTE", "value": float(match.group(1)), "version": TRADE_CONDITION_SCHEMA_VERSION, "source": "entry_trigger"}
    match = _PRICE_GTE.search(text)
    if match:
        return {"type": "PRICE", "operator": "GTE", "value": float(match.group(1)), "version": TRADE_CONDITION_SCHEMA_VERSION, "source": "entry_trigger"}
    if re.fullmatch(r"(?:immediate|now|fresh\s+quote(?:\s+is\s+available)?)", text, re.I):
        return {"type": "FRESH_MARK", "version": TRADE_CONDITION_SCHEMA_VERSION, "source": "entry_trigger"}
    # These exact legacy phrases were already used by the v1.3 API fixture.
    # They are mapped to a typed freshness condition; arbitrary prose remains
    # unsupported and cannot silently become a market order.
    if text.casefold() in {"fresh quote is available", "fresh quote available"}:
        return {"type": "FRESH_MARK", "version": TRADE_CONDITION_SCHEMA_VERSION, "source": "legacy_entry_trigger"}
    return {"type": "UNSUPPORTED", "version": TRADE_CONDITION_SCHEMA_VERSION, "source": "entry_trigger", "text": text[:240]}


def _abandon_condition(raw: Any, condition_spec: dict[str, Any] | None) -> dict[str, Any]:
    supplied = condition_spec.get("max_chase_bps") if isinstance(condition_spec, dict) else None
    if supplied is None and isinstance(condition_spec, dict):
        abandon = condition_spec.get("abandon_chase")
        if abandon is None:
            abandon = condition_spec.get("abandon_chase_condition")
        if isinstance(abandon, dict):
            supplied = abandon.get("max_chase_bps")
            if supplied is None:
                supplied = abandon.get("max_bps")
            if supplied is None:
                supplied = abandon.get("value")
    if supplied is not None:
        bps = _finite_number(supplied, name="max_chase_bps", non_negative=True)
        if bps is None or bps > 1000:
            raise TradePlanContractError("PLAN_CONDITION_INVALID", "max_chase_bps must be in [0, 1000].")
        return {"type": "MAX_CHASE_BPS", "max_bps": bps, "version": TRADE_CONDITION_SCHEMA_VERSION, "source": "condition_spec"}

    text = str(raw or "").strip()
    match = _MAX_CHASE.search(text)
    if match:
        bps = float(match.group(1))
        if bps > 1000:
            raise TradePlanContractError("PLAN_CONDITION_INVALID", "maximum chase deviation is too large.")
        return {"type": "MAX_CHASE_BPS", "max_bps": bps, "version": TRADE_CONDITION_SCHEMA_VERSION, "source": "abandon_chase_condition"}
    match = _PERCENT_CHASE.search(text)
    if match:
        percent = float(match.group(1))
        if percent < 0 or percent > 10:
            raise TradePlanContractError("PLAN_CONDITION_INVALID", "maximum chase percentage is outside the safe bound.")
        return {"type": "MAX_CHASE_BPS", "max_bps": percent * 100.0, "version": TRADE_CONDITION_SCHEMA_VERSION, "source": "abandon_chase_condition"}
    # Compatibility for the exact v1.3 wording.  The limit is the product's
    # hard 10 bps paper quote tolerance, never a model-selected widening.
    if text.casefold() in {
        "do not chase after the first impulse",
        "abandon after adverse move beyond the trigger",
    }:
        return {"type": "MAX_CHASE_BPS", "max_bps": 10.0, "version": TRADE_CONDITION_SCHEMA_VERSION, "source": "legacy_policy_max_chase"}
    return {"type": "UNSUPPORTED", "version": TRADE_CONDITION_SCHEMA_VERSION, "source": "abandon_chase_condition", "text": text[:240]}


def _normalize_event_conditions(values: Any) -> list[dict[str, Any]]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise TradePlanContractError("PLAN_CONDITION_INVALID", "event_invalidation must be a list.")
    normalized: list[dict[str, Any]] = []
    for item in values:
        if isinstance(item, str) and item.strip():
            normalized.append({"type": "EVENT_STATUS", "event_key": item.strip(), "invalid_if": "ACTIVE_OR_CORRECTED", "evidence_required": True})
        elif isinstance(item, dict):
            key = str(item.get("event_key") or item.get("event_id") or item.get("revision_id") or "").strip()
            if not key:
                raise TradePlanContractError("PLAN_CONDITION_INVALID", "event invalidation entries require event_key/event_id/revision_id.")
            status = str(item.get("invalid_if") or item.get("status") or "ACTIVE_OR_CORRECTED").upper()
            supported_statuses = {
                "ACTIVE_OR_CORRECTED", "ACTIVE_OR_CHANGED", "CORRECTED", "RETRACTED",
                "DISPUTED", "EXPIRED", "INVALIDATED",
            }
            requested_statuses = {
                part.strip().upper()
                for part in status.replace("/", "|").replace(",", "|").split("|")
                if part.strip()
            }
            if not requested_statuses or not requested_statuses.issubset(supported_statuses):
                raise TradePlanContractError("PLAN_CONDITION_UNSUPPORTED", f"Unsupported event invalidation status: {status}.")
            normalized.append({"type": "EVENT_STATUS", "event_key": key, "invalid_if": status, "evidence_required": True})
        else:
            raise TradePlanContractError("PLAN_CONDITION_INVALID", "event invalidation entries must be strings or objects.")
    return normalized


def _normalize_partials(values: Any) -> list[dict[str, Any]]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise TradePlanContractError("PLAN_PARTIALS_INVALID", "partial_take_profits must be a list.")
    if not values:
        return []
    result: list[dict[str, Any]] = []
    scalar_values: list[float] = []
    has_object = False
    for item in values:
        if isinstance(item, dict):
            has_object = True
            price = _finite_number(item.get("price"), name="partial_take_profit.price", positive=True)
            fraction = _finite_number(item.get("fraction"), name="partial_take_profit.fraction", positive=True)
            if price is None or fraction is None or fraction > 1:
                raise TradePlanContractError("PLAN_PARTIALS_INVALID", "partial take-profit price/fraction is invalid.")
            result.append({"price": price, "fraction": fraction, "label": str(item.get("label") or f"TP{len(result) + 1}")[:80]})
        else:
            price = _finite_number(item, name="partial_take_profit.price", positive=True)
            if price is None:
                raise TradePlanContractError("PLAN_PARTIALS_INVALID", "partial take-profit prices are required.")
            scalar_values.append(price)
    if scalar_values:
        if has_object:
            raise TradePlanContractError("PLAN_PARTIALS_INVALID", "partial take-profits must use either all prices or all typed objects.")
        equal_fraction = 1.0 / len(scalar_values)
        result = [{"price": price, "fraction": equal_fraction, "label": f"TP{i + 1}"} for i, price in enumerate(scalar_values)]
    total = sum(item["fraction"] for item in result)
    if total <= 0 or total > 1.0 + 1e-12:
        raise TradePlanContractError("PLAN_PARTIALS_INVALID", "partial take-profit fractions must sum to (0, 1].")
    return result


def _normalize_trailing(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TradePlanContractError("PLAN_TRAILING_INVALID", "trailing_protection must be an object.")
    kind = str(value.get("type") or ("PERCENT" if value.get("percent") is not None else "DISTANCE")).upper()
    if kind not in {"PERCENT", "DISTANCE"}:
        raise TradePlanContractError("PLAN_TRAILING_UNSUPPORTED", "trailing protection supports PERCENT or DISTANCE.")
    raw = value.get("value")
    if raw is None:
        raw = value.get("percent") if kind == "PERCENT" else value.get("distance")
    amount = _finite_number(raw, name="trailing_protection.value", positive=True)
    if amount is None or (kind == "PERCENT" and amount > 50):
        raise TradePlanContractError("PLAN_TRAILING_INVALID", "trailing protection value is outside the safe bound.")
    activation = _finite_number(value.get("activation_price"), name="trailing_protection.activation_price", positive=True) if value.get("activation_price") is not None else None
    return {"type": kind, "value": amount, "activation_price": activation, "version": TRADE_CONDITION_SCHEMA_VERSION}


def normalize_plan_payload(payload: dict[str, Any], scope: dict[str, str], *, now: datetime) -> dict[str, Any]:
    """Validate and canonicalize one plan without dropping behavior fields."""
    allowed = {
        "plan_id", "account_id", "mode", "venue", "position_id", "symbol", "instrument_id", "action",
        "evidence", "entry_trigger", "abandon_chase_condition", "entry_price", "stop_loss", "price_stop",
        "take_profit", "entry_expires_at", "entry_valid_until", "time_exit_at", "event_invalidation",
        "partial_take_profits", "trailing_protection", "worst_loss_budget", "why_not_waiting", "strategy_id",
        "strategy_version", "leverage", "reduce_fraction", "reduce_quantity", "quantity", "condition_spec",
        "news_revision_ids", "plan_version",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise TradePlanContractError("PLAN_UNKNOWN_FIELD", f"Unsupported trade-plan field(s): {', '.join(unknown)}.")
    action = str(payload.get("action") or "WAIT").upper()
    if action not in {"WAIT", "HOLD", "OPEN_LONG", "OPEN_SHORT", "REDUCE_POSITION", "CLOSE_POSITION"}:
        raise TradePlanContractError("PLAN_ACTION_INVALID", "Unsupported trade-plan action.")
    symbol = str(payload.get("symbol") or payload.get("instrument_id") or "").strip().upper()
    if not symbol:
        raise TradePlanContractError("PLAN_SYMBOL_REQUIRED", "Trade plan symbol is required.")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise TradePlanContractError("PLAN_EVIDENCE_REQUIRED", "Every plan must link at least one evidence item.")
    if any(not isinstance(item, dict) for item in evidence):
        raise TradePlanContractError("PLAN_EVIDENCE_INVALID", "Plan evidence must be objects.")
    for field in ("entry_trigger", "abandon_chase_condition", "why_not_waiting"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise TradePlanContractError("PLAN_CONTRACT_INCOMPLETE", f"{field} is required.")

    entry = _finite_number(payload.get("entry_price"), name="entry_price", positive=True) if payload.get("entry_price") is not None else None
    stop = _finite_number(payload.get("stop_loss", payload.get("price_stop")), name="stop_loss", positive=True) if payload.get("stop_loss", payload.get("price_stop")) is not None else None
    take_profit = _finite_number(payload.get("take_profit"), name="take_profit", positive=True) if payload.get("take_profit") is not None else None
    worst_budget = _finite_number(payload.get("worst_loss_budget"), name="worst_loss_budget", non_negative=True) if payload.get("worst_loss_budget") is not None else None
    if worst_budget is None and action in {"REDUCE_POSITION", "CLOSE_POSITION"}:
        # Exits cannot add exposure.  A missing loss budget is represented as
        # zero for the exit plan, while all opening plans still require an
        # explicit positive budget.
        worst_budget = 0.0
    if worst_budget is None:
        raise TradePlanContractError("PLAN_BUDGET_REQUIRED", "worst_loss_budget must be explicit.")
    if action in {"OPEN_LONG", "OPEN_SHORT"}:
        if stop is None or entry is None or worst_budget <= 0:
            raise TradePlanContractError("PLAN_PRICE_OR_BUDGET_REQUIRED", "Actionable plans require entry_price, stop_loss, and a positive worst_loss_budget.")
        if action == "OPEN_LONG" and not stop < entry:
            raise TradePlanContractError("PLAN_LEVELS_INVALID", "LONG stop must be below entry.")
        if action == "OPEN_SHORT" and not stop > entry:
            raise TradePlanContractError("PLAN_LEVELS_INVALID", "SHORT stop must be above entry.")
    if action == "WAIT" and worst_budget != 0:
        raise TradePlanContractError("PLAN_WAIT_BUDGET_INVALID", "WAIT plans cannot reserve an opening loss budget.")

    reduce_fraction = None
    if "reduce_fraction" in payload and payload.get("reduce_fraction") is not None:
        reduce_fraction = _finite_number(payload.get("reduce_fraction"), name="reduce_fraction", positive=True)
        if reduce_fraction is None or reduce_fraction > 1:
            raise TradePlanContractError("PLAN_REDUCE_FRACTION_INVALID", "reduce_fraction must be finite and in (0, 1].")
    reduce_quantity = None
    quantity_value = payload.get("reduce_quantity") if payload.get("reduce_quantity") is not None else payload.get("quantity")
    if quantity_value is not None:
        reduce_quantity = _finite_number(quantity_value, name="reduce_quantity", positive=True)
    if action == "REDUCE_POSITION":
        if reduce_fraction is None and reduce_quantity is None:
            raise TradePlanContractError("PLAN_REDUCE_FRACTION_REQUIRED", "REDUCE_POSITION requires an explicit reduce_fraction or reduce_quantity.")
        if reduce_fraction is not None and reduce_quantity is not None:
            raise TradePlanContractError("PLAN_REDUCE_TARGET_AMBIGUOUS", "Provide reduce_fraction or reduce_quantity, not both.")
    if action == "CLOSE_POSITION" and (reduce_fraction is not None or reduce_quantity is not None):
        raise TradePlanContractError("PLAN_CLOSE_TARGET_INVALID", "CLOSE_POSITION is the explicit full exit; remove reduction fields.")
    leverage = None
    if payload.get("leverage") is not None:
        raw_leverage = _finite_number(payload.get("leverage"), name="leverage", positive=True)
        if raw_leverage is None or raw_leverage != int(raw_leverage) or raw_leverage > 100:
            raise TradePlanContractError("PLAN_LEVERAGE_INVALID", "leverage must be an integer in [1, 100].")
        leverage = int(raw_leverage)

    condition_spec = payload.get("condition_spec")
    if condition_spec is not None and not isinstance(condition_spec, dict):
        raise TradePlanContractError("PLAN_CONDITION_INVALID", "condition_spec must be an object.")
    condition_spec = dict(condition_spec or {})
    unknown_condition_fields = sorted(
        set(condition_spec)
        - {"version", "entry", "entry_trigger", "abandon_chase", "abandon_chase_condition", "max_chase_bps"}
    )
    if unknown_condition_fields:
        raise TradePlanContractError("PLAN_CONDITION_UNKNOWN_FIELD", f"Unsupported condition field(s): {', '.join(unknown_condition_fields)}.")
    condition_version = str(condition_spec.get("version") or TRADE_CONDITION_SCHEMA_VERSION)
    if condition_version != TRADE_CONDITION_SCHEMA_VERSION:
        raise TradePlanContractError("PLAN_CONDITION_VERSION_UNSUPPORTED", f"Unsupported condition schema: {condition_version}.")
    entry_expires = _parse_time(payload.get("entry_expires_at", payload.get("entry_valid_until")), name="entry_expires_at")
    position_time_exit = _parse_time(payload.get("time_exit_at"), name="time_exit_at")
    if entry_expires is not None and position_time_exit is not None and position_time_exit < entry_expires:
        raise TradePlanContractError("PLAN_TIME_FIELDS_INVALID", "position time_exit_at must not precede entry_expires_at.")
    events = _normalize_event_conditions(payload.get("event_invalidation"))
    partials = _normalize_partials(payload.get("partial_take_profits"))
    trailing = _normalize_trailing(payload.get("trailing_protection"))
    news_ids = payload.get("news_revision_ids")
    if news_ids is None or news_ids == []:
        news_ids = [str(item.get("revision_id")) for item in evidence if item.get("revision_id")]
    if not news_ids:
        news_ids = [
            str(item.get("event_key"))
            for item in events
            if isinstance(item, dict) and str(item.get("event_key") or "").startswith("rev_")
        ]
    if not isinstance(news_ids, list) or any(not isinstance(item, str) or not item.strip() for item in news_ids):
        raise TradePlanContractError("PLAN_NEWS_EVIDENCE_INVALID", "news_revision_ids must be a list of non-empty strings.")
    # A news revision referenced by an opening plan is also part of the
    # post-entry protection contract.  The pre-entry gate prevents a stale
    # plan from opening after a correction; this durable event condition makes
    # the same correction visible to PositionGuardian after the position is
    # already open.  Explicit event rules keep their requested status grammar.
    event_keys = {
        str(item.get("event_key") or "")
        for item in events
        if isinstance(item, dict)
    }
    for revision_id in news_ids:
        if revision_id not in event_keys:
            events.append(
                {
                    "type": "EVENT_STATUS",
                    "event_key": revision_id,
                    "invalid_if": "ACTIVE_OR_CORRECTED",
                    "evidence_required": True,
                    "source": "news_revision",
                }
            )
    conditions = {
        "version": TRADE_CONDITION_SCHEMA_VERSION,
        "entry": _entry_condition(payload.get("entry_trigger"), condition_spec),
        "abandon_chase": _abandon_condition(payload.get("abandon_chase_condition"), condition_spec),
        "entry_expires_at": _iso(entry_expires),
        "position_time_exit_at": _iso(position_time_exit),
        "event_invalidation": events,
        "partial_take_profits": partials,
        "trailing_protection": trailing,
    }
    clean = {
        "schema_version": TRADE_PLAN_SCHEMA_VERSION,
        "plan_id": str(payload.get("plan_id") or ""),
        "account_id": scope["account_id"],
        "venue": scope["venue"],
        "mode": scope["mode"],
        "position_id": payload.get("position_id"),
        "symbol": symbol,
        "action": action,
        "evidence": [dict(item) for item in evidence],
        "entry_trigger": str(payload["entry_trigger"]).strip(),
        "abandon_chase_condition": str(payload["abandon_chase_condition"]).strip(),
        "entry_price": entry,
        "stop_loss": stop,
        "entry_expires_at": _iso(entry_expires),
        "time_exit_at": _iso(position_time_exit),
        "event_invalidation": events,
        "partial_take_profits": partials,
        "trailing_protection": trailing,
        "take_profit": take_profit,
        "worst_loss_budget": worst_budget,
        "why_not_waiting": str(payload["why_not_waiting"]).strip(),
        "strategy_id": payload.get("strategy_id"),
        "strategy_version": payload.get("strategy_version"),
        "leverage": leverage,
        "reduce_fraction": reduce_fraction,
        "reduce_quantity": reduce_quantity,
        "news_revision_ids": list(dict.fromkeys(str(item).strip() for item in news_ids)),
        "conditions": conditions,
        "condition_spec": condition_spec,
        "plan_version": TRADE_PLAN_SCHEMA_VERSION,
        "created_at": _iso(now),
    }
    return clean


def _market_price(market: dict[str, Any], side: str) -> float | None:
    key = "ask" if side in {"LONG", "BUY"} else "bid"
    value = market.get(key, market.get("price"))
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _event_status_matches(observed: Any, invalid_if: Any) -> bool:
    """Apply the same explicit invalidation grammar used by the Guardian."""
    observed_clean = str(observed or "").strip().upper()
    requested = str(invalid_if or "ACTIVE_OR_CORRECTED").strip().upper()
    if requested in {"ACTIVE_OR_CORRECTED", "ACTIVE_OR_CHANGED"}:
        return observed_clean in {"CORRECTED", "RETRACTED", "DISPUTED", "EXPIRED", "INVALIDATED"}
    requested_statuses = {
        part.strip().upper()
        for part in requested.replace("/", "|").replace(",", "|").split("|")
        if part.strip()
    }
    return observed_clean in requested_statuses


def evaluate_plan_conditions(plan: dict[str, Any], market: dict[str, Any] | None, *, now: datetime, event_facts: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a durable decision for an ARMED plan before gateway submit."""
    market = market if isinstance(market, dict) else {}
    event_facts = event_facts if isinstance(event_facts, dict) else {}
    freshness = str(market.get("freshness_status") or "").lower()
    raw_data_as_of = market.get("data_as_of") or market.get("timestamp") or market.get("bar_end")
    try:
        data_as_of = _parse_time(raw_data_as_of, name="data_as_of") if raw_data_as_of else None
    except TradePlanContractError:
        # The plan consumer owns the fail-closed decision.  An invalid quote
        # timestamp is missing executable evidence, not a generic plan error.
        data_as_of = None
    try:
        # Preserve an explicitly supplied zero/invalid freshness window.  A
        # caller must not widen a deliberately strict quote policy merely by
        # passing a falsy value.
        stale_after_value = market["stale_after_seconds"] if "stale_after_seconds" in market else 120
        stale_after = float(stale_after_value)
    except (TypeError, ValueError):
        stale_after = 0.0
    # A non-positive explicit window is invalid.  Do not normalize it to a
    # positive default, because that would make a strict caller less safe.
    stale_after = min(stale_after, 120.0) if math.isfinite(stale_after) and stale_after > 0 else 0.0
    age_seconds = (now.astimezone(timezone.utc) - data_as_of).total_seconds() if data_as_of is not None else None
    side = "LONG" if str(plan.get("action")) == "OPEN_LONG" else "SHORT"
    executable_price = _market_price(market, side)
    try:
        mark_price = float(market.get("price"))
    except (TypeError, ValueError):
        mark_price = executable_price
    # A timestamp alone is not an executable quote.  The plan consumer must
    # receive an affirmative freshness state from the quote provider/runtime;
    # missing freshness is UNKNOWN and cannot silently turn an ARMED plan
    # into a market order.
    if (
        mark_price is None
        or not math.isfinite(mark_price)
        or mark_price <= 0
        or executable_price is None
        or market.get("fresh") is not True
        or freshness not in {"fresh", "healthy"}
        or data_as_of is None
        or stale_after <= 0
        or age_seconds is None
        or age_seconds < -5.0
        or age_seconds > stale_after
    ):
        return {
            "status": "BLOCKED_DATA",
            "reason": "FRESH_MARKET_REQUIRED",
            "evidence": {
                "market_data_as_of": market.get("data_as_of") or market.get("timestamp") or market.get("bar_end"),
                "age_seconds": age_seconds,
                "stale_after_seconds": stale_after or None,
            },
        }

    conditions = plan.get("conditions") if isinstance(plan.get("conditions"), dict) else {}
    entry_expiry = _parse_time(conditions.get("entry_expires_at"), name="entry_expires_at") if conditions.get("entry_expires_at") else None
    if entry_expiry is not None and now >= entry_expiry:
        return {"status": "EXPIRED", "reason": "ENTRY_WINDOW_EXPIRED", "evidence": {"now": _iso(now), "entry_expires_at": _iso(entry_expiry)}}
    action = str(plan.get("action") or "").upper()
    if action in {"OPEN_LONG", "OPEN_SHORT"}:
        entry = float(plan.get("entry_price"))
        entry_condition = conditions.get("entry") if isinstance(conditions.get("entry"), dict) else {"type": "UNSUPPORTED"}
        kind = str(entry_condition.get("type") or "UNSUPPORTED").upper()
        if kind == "UNSUPPORTED":
            return {"status": "BLOCKED_DATA", "reason": "UNSUPPORTED_ENTRY_CONDITION", "evidence": {"entry_trigger": plan.get("entry_trigger")}}
        if kind == "PRICE":
            op = str(entry_condition.get("operator") or "").upper()
            threshold = float(entry_condition.get("value"))
            passed = {"LTE": mark_price <= threshold, "LT": mark_price < threshold, "GTE": mark_price >= threshold, "GT": mark_price > threshold}.get(op, False)
            if not passed:
                return {"status": "WAITING_TRIGGER", "reason": "ENTRY_TRIGGER_NOT_REACHED", "evidence": {"price": mark_price, "operator": op, "threshold": threshold, "data_as_of": market.get("data_as_of")}}
        elif kind != "FRESH_MARK":
            return {"status": "BLOCKED_DATA", "reason": "UNSUPPORTED_ENTRY_CONDITION", "evidence": {"condition_type": kind}}
        abandon = conditions.get("abandon_chase") if isinstance(conditions.get("abandon_chase"), dict) else {"type": "UNSUPPORTED"}
        if str(abandon.get("type") or "").upper() == "UNSUPPORTED":
            return {"status": "BLOCKED_DATA", "reason": "UNSUPPORTED_ABANDON_CHASE_CONDITION", "evidence": {"abandon_chase_condition": plan.get("abandon_chase_condition")}}
        max_bps = float(abandon.get("max_bps", 0.0))
        directional_adverse_bps = ((executable_price - entry) / entry * 10000.0) if action == "OPEN_LONG" else ((entry - executable_price) / entry * 10000.0)
        if directional_adverse_bps > max_bps + 1e-9:
            return {"status": "WAITING_TRIGGER", "reason": "ABANDON_CHASE_LIMIT_EXCEEDED", "evidence": {"price": executable_price, "entry": entry, "adverse_bps": directional_adverse_bps, "max_bps": max_bps, "data_as_of": market.get("data_as_of")}}
    for condition in conditions.get("event_invalidation", []) if isinstance(conditions.get("event_invalidation"), list) else []:
        key = str(condition.get("event_key") or "") if isinstance(condition, dict) else ""
        if not key:
            return {"status": "BLOCKED_DATA", "reason": "EVENT_EVIDENCE_MISSING", "evidence": {"event_condition": condition}}
        fact = event_facts.get(key)
        if fact is None:
            return {"status": "BLOCKED_DATA", "reason": "EVENT_EVIDENCE_MISSING", "evidence": {"event_key": key}}
        fact_status = str(fact.get("status") if isinstance(fact, dict) else fact).upper()
        if fact_status in {"", "UNKNOWN", "UNVERIFIED", "UNAVAILABLE", "MISSING"}:
            return {"status": "BLOCKED_DATA", "reason": "EVENT_EVIDENCE_UNKNOWN", "evidence": {"event_key": key, "event_status": fact_status or "UNKNOWN"}}
        invalid_if = str(condition.get("invalid_if") or "ACTIVE_OR_CORRECTED").upper()
        if _event_status_matches(fact_status, invalid_if):
            return {"status": "INVALIDATED", "reason": "EVENT_INVALIDATION_MATCHED", "evidence": {"event_key": key, "event_status": fact_status}}
    return {"status": "TRIGGERED", "reason": "PLAN_CONDITIONS_SATISFIED", "evidence": {"price": mark_price, "executable_price": executable_price, "data_as_of": market.get("data_as_of"), "evaluated_at": _iso(now)}}


def round_down_quantity(value: Decimal, *, step: Decimal, minimum: Decimal) -> Decimal:
    if not value.is_finite() or value <= 0 or not step.is_finite() or step <= 0:
        raise TradePlanContractError("PLAN_QUANTITY_INVALID", "Quantity and market step must be positive finite values.")
    rounded = (value / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step
    if rounded <= 0 or rounded < minimum:
        raise TradePlanContractError("PLAN_REDUCE_QUANTITY_BELOW_MIN", "Requested reduction is below the instrument minimum after step rounding.")
    return rounded
