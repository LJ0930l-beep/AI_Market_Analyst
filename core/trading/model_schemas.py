"""JSON schemas sent to the local Bonsai route and used by validators."""

from __future__ import annotations
import math


def validate_schema(value, schema, path="output"):
    """Validate the bounded JSON-schema subset used by these local contracts.

    The local OpenAI-compatible runtime may fall back to JSON mode after a
    grammar rejection, so native
    schema enforcement is never assumed to replace local validation.
    """
    kinds = schema.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    actual = ("null" if value is None else "boolean" if isinstance(value, bool) else
              "integer" if isinstance(value, int) else "number" if isinstance(value, float) else
              "string" if isinstance(value, str) else "array" if isinstance(value, list) else
              "object" if isinstance(value, dict) else "invalid")
    if actual not in kinds and not (actual == "integer" and "number" in kinds):
        raise ValueError(f"INVALID_ACTION_SCHEMA:{path}:type")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"INVALID_ACTION_SCHEMA:{path}:enum")
    if actual in {"integer", "number"}:
        if not math.isfinite(value) or value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise ValueError(f"INVALID_ACTION_SCHEMA:{path}:range")
        if value <= schema.get("exclusiveMinimum", -math.inf) or value >= schema.get("exclusiveMaximum", math.inf):
            raise ValueError(f"INVALID_ACTION_SCHEMA:{path}:exclusive_range")
    if actual in {"string", "array"}:
        prefix = "Length" if actual == "string" else "Items"
        if not schema.get("min" + prefix, 0) <= len(value) <= schema.get("max" + prefix, math.inf):
            raise ValueError(f"INVALID_ACTION_SCHEMA:{path}:length")
    if actual == "object":
        props = schema.get("properties", {})
        if set(schema.get("required", [])) - set(value) or (schema.get("additionalProperties") is False and set(value) - set(props)):
            raise ValueError(f"INVALID_ACTION_SCHEMA:{path}:fields")
        for key, item in value.items():
            if key in props:
                validate_schema(item, props[key], path + "." + key)
    if actual == "array":
        for item in value:
            validate_schema(item, schema["items"], path + "[]")


CALIBRATION_PROFILE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["risk_regime", "entry_style", "order_preference", "max_concurrent_positions", "notes_zh"],
    "properties": {
        "risk_regime": {"type": "string", "minLength": 1, "maxLength": 40},
        "entry_style": {"type": "string", "minLength": 1, "maxLength": 40},
        "order_preference": {"type": "string", "enum": ["MARKET", "LIMIT", "AUTO"]},
        "max_concurrent_positions": {"type": "integer", "minimum": 1, "maximum": 5},
        "notes_zh": {"type": "string", "maxLength": 320},
    },
}


AI_ACTION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    # Keep confidence present on every response. WAIT/HOLD may explicitly use
    # null, while OPEN is tightened to a number by the bounded repair schema.
    # Making the key part of the native grammar prevents small local models
    # from silently omitting the field altogether.
    "required": ["action", "instrument_id", "reason", "confidence"],
    "properties": {
        "action": {"type": "string", "enum": ["WAIT", "HOLD", "OPEN_LONG", "OPEN_SHORT", "REDUCE_POSITION", "CLOSE_POSITION", "TIGHTEN_STOP"]},
        "instrument_id": {"type": "string", "minLength": 1, "maxLength": 80},
        "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
        "position_id": {"type": ["string", "null"], "maxLength": 160},
        "entry_condition": {"type": ["string", "null"], "maxLength": 500},
        "entry_price": {"type": ["number", "null"]},
        "stop_price": {"type": ["number", "null"]},
        "take_profit": {"type": ["number", "null"]},
        "requested_risk_fraction": {"type": ["number", "null"]},
        "position_size_usdt": {"type": ["number", "null"], "exclusiveMinimum": 0},
        "requested_leverage": {"type": ["integer", "null"], "minimum": 1, "maximum": 100},
        "new_stop_price": {"type": ["number", "null"]},
        "reduce_fraction": {"type": ["number", "null"]},
        "evidence_refs": {"type": "array", "items": {"type": "string", "maxLength": 200}, "maxItems": 32},
        "order_preference": {"type": "string", "enum": ["MARKET", "LIMIT", "AUTO"]},
        "limit_price": {"type": ["number", "null"]},
        "ttl_seconds": {"type": ["integer", "null"], "minimum": 60, "maximum": 1800},
        "candidate_id": {"type": ["string", "null"], "maxLength": 200},
        "closed_15m_bar": {"type": ["string", "null"], "maxLength": 200},
        "strategy_candidate_id": {"type": ["string", "null"], "maxLength": 200},
        "strategy_id": {"type": ["string", "null"], "maxLength": 100},
        "market_summary": {"type": ["string", "null"], "maxLength": 1000},
        "timeframe_analysis": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {
                "5m": {"type": ["string", "null"], "maxLength": 800},
                "15m": {"type": ["string", "null"], "maxLength": 800},
                "1h": {"type": ["string", "null"], "maxLength": 800},
                "4h": {"type": ["string", "null"], "maxLength": 800},
                "1d": {"type": ["string", "null"], "maxLength": 800},
            },
        },
        "strategy_analysis": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {
                "strategy_id": {"type": ["string", "null"], "maxLength": 100},
                "matched_conditions": {"type": "array", "items": {"type": "string", "maxLength": 300}, "maxItems": 32},
                "missing_conditions": {"type": "array", "items": {"type": "string", "maxLength": 300}, "maxItems": 32},
                "trigger_completion_pct": {"type": ["number", "null"], "minimum": 0, "maximum": 100},
        "next_trigger_price": {"type": ["number", "null"], "exclusiveMinimum": 0},
        "entry_condition": {"type": ["string", "null"], "maxLength": 500},
            },
        },
        "news_context": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {
                "impact": {"type": ["string", "null"], "enum": ["POSITIVE", "NEGATIVE", "NEUTRAL", "UNKNOWN", None]},
                "summary": {"type": ["string", "null"], "maxLength": 800},
            },
        },
        "entry_zone": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {
                "low": {"type": ["number", "null"]},
                "high": {"type": ["number", "null"]},
            },
        },
        "take_profit_1": {"type": ["number", "null"]},
        "take_profit_2": {"type": ["number", "null"]},
        "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 100},
        "invalidation_condition": {"type": ["string", "null"], "maxLength": 800},
    },
}


SIGNAL_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "confidence_raw", "entry_preference", "entry_zone", "stop", "tp1", "tp2", "signal_validity_minutes", "holding_horizon_minutes", "re_evaluate_minutes", "invalidation", "thesis", "risk_factors"],
    "properties": {
        "action": {"type": "string", "enum": ["LONG", "SHORT", "WAIT"]},
        "confidence_raw": {"type": "number", "minimum": 0, "maximum": 100},
        "entry_preference": {"type": "string", "enum": ["market", "pullback", "breakout", "limit", "none"]},
        "entry_zone": {"type": ["object", "null"], "additionalProperties": False, "properties": {"low": {"type": ["number", "null"]}, "high": {"type": ["number", "null"]}}},
        "stop": {"type": ["number", "null"]},
        "tp1": {"type": ["number", "null"]},
        "tp2": {"type": ["number", "null"]},
        "signal_validity_minutes": {"type": "integer"},
        "holding_horizon_minutes": {"type": "integer"},
        "re_evaluate_minutes": {"type": "integer"},
        "invalidation": {"type": "array", "items": {"type": "string"}},
        "thesis": {"type": "array", "items": {"type": "string"}},
        "risk_factors": {"type": "array", "items": {"type": "string"}},
    },
}


AI_ACTION_SCHEMA["properties"]["strategy_plan"] = {
    "type": ["object", "null"], "additionalProperties": False,
    "required": ["name", "thesis", "entry_conditions", "exit_conditions"],
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 100},
        "thesis": {"type": "string", "minLength": 1, "maxLength": 800},
        "entry_conditions": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "string", "minLength": 1, "maxLength": 300}},
        "exit_conditions": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "string", "minLength": 1, "maxLength": 300}},
    },
}


def require_confidence_for_open(decoded: object) -> None:
    """OPEN_LONG/OPEN_SHORT must carry a concrete numeric confidence.

    ``confidence`` is optional in the schema (WAIT/HOLD legitimately omit it),
    but ``autonomous_strategy.validate_entry`` hard-rejects a null confidence on
    an OPEN action with ``AI_CONFIDENCE_BELOW_POLICY``.  Rejecting it here — inside
    the model's bounded repair loop — forces the model to emit a number instead of
    letting the first valid OPEN die silently at the last gate.
    """
    if not isinstance(decoded, dict):
        return
    if decoded.get("action") in ("OPEN_LONG", "OPEN_SHORT") and decoded.get("confidence") is None:
        raise ValueError("INVALID_ACTION_SCHEMA: OPEN_LONG/OPEN_SHORT requires a numeric confidence in [0,100]")




# The misplaced-plan normalizer promotes these two keys to the top level, and
# AIActionOutput carries them, so the contract must accept them there. Without
# these properties every hoisted response failed validate_schema with
# "output:fields extra=[...]" even though the model authored the values.
AI_ACTION_SCHEMA["properties"]["next_trigger_price"] = {"type": ["number", "null"], "exclusiveMinimum": 0}
AI_ACTION_SCHEMA["properties"]["new_take_profit"] = {"type": ["number", "null"], "exclusiveMinimum": 0}


def normalize_news_impact(decoded: object) -> dict[str, object] | None:
    """Project a bounded model-authored sentiment score onto the enum.

    Local JSON-mode responses sometimes use a signed score in [-1, 1] where
    the schema requires a label. This only categorizes the model's own news
    opinion, never market facts or entry parameters. The raw response remains
    in the audit record; out-of-range and nonnumeric values still fail schema.
    """
    if not isinstance(decoded, dict):
        return None
    news = decoded.get("news_context")
    if not isinstance(news, dict):
        return None
    impact = news.get("impact")
    if type(impact) not in (int, float) or not math.isfinite(impact):
        return None
    if not -1 <= impact <= 1:
        return None
    label = "NEGATIVE" if impact <= -0.2 else "POSITIVE" if impact >= 0.2 else "NEUTRAL"
    news["impact"] = label
    return {"field": "news_context.impact", "raw": impact, "normalized": label}


def normalize_limit_ttl_alias(decoded: object) -> dict[str, object] | None:
    """Accept one unambiguous spelling of the limit-order TTL.

    The strategy profile calls this value ``limit_ttl_seconds`` while the
    action contract calls it ``ttl_seconds``.  Preserve all other schema
    errors, including conflicting fields and out-of-range values.  The raw
    model response is retained separately by the coordinator.
    """
    if not isinstance(decoded, dict) or "limit_ttl_seconds" not in decoded or "ttl_seconds" in decoded:
        return None
    value = decoded["limit_ttl_seconds"]
    if type(value) is not int or not 60 <= value <= 1800:
        return None
    decoded["ttl_seconds"] = decoded.pop("limit_ttl_seconds")
    return {"field": "limit_ttl_seconds", "raw": value, "normalized_field": "ttl_seconds"}


def normalize_misplaced_strategy_plan(decoded: object) -> dict[str, object] | None:
    """Move an exact strategy-plan shape accidentally nested in strategy_analysis.

    Some model responses place the optional plan object under
    ``strategy_analysis``. Accept only the complete, recognized alias shape;
    conflicting top-level values and unrelated extra fields remain for strict
    schema validation to reject. The original raw model response is retained
    separately by the coordinator.
    """
    if not isinstance(decoded, dict):
        return None
    analysis = decoded.get("strategy_analysis")
    if not isinstance(analysis, dict):
        return None

    plan_fields = ("name", "thesis", "entry_conditions", "exit_conditions")
    present_plan_fields = set(plan_fields).intersection(analysis)
    has_trigger_price = "next_trigger_price" in analysis
    if not present_plan_fields and not has_trigger_price:
        return None

    plan: dict[str, object] | None = None
    if present_plan_fields:
        # Do not guess or fabricate an incomplete plan. Leave it malformed so
        # the bounded repair and strict validator can handle it explicitly.
        if not all(field in analysis for field in plan_fields):
            return None
        plan = {field: analysis[field] for field in plan_fields}
        existing_plan = decoded.get("strategy_plan")
        if existing_plan is not None and existing_plan != plan:
            return None

    if has_trigger_price and (
        "next_trigger_price" in decoded
        and decoded["next_trigger_price"] != analysis["next_trigger_price"]
    ):
        return None

    moved_fields: list[str] = []
    keys_to_remove: set[str] = set()
    if plan is not None:
        decoded["strategy_plan"] = plan
        keys_to_remove.update(plan_fields)
        moved_fields.append("strategy_plan")
    if has_trigger_price:
        decoded["next_trigger_price"] = analysis["next_trigger_price"]
        keys_to_remove.add("next_trigger_price")
        moved_fields.append("next_trigger_price")

    decoded["strategy_analysis"] = {
        key: value for key, value in analysis.items() if key not in keys_to_remove
    }
    return {
        "field": "strategy_analysis",
        "normalized_fields": moved_fields,
        "source": "EXACT_KNOWN_MODEL_ALIAS",
    }


def normalize_wait_conditions(decoded: object) -> dict[str, object] | None:
    """Keep model-authored WAIT evidence when it uses a flat list.

    This is explanatory text only; it cannot change an entry, size or price.
    Invalid or conflicting structures still fail the strict schema check.
    """
    if not isinstance(decoded, dict) or decoded.get("action") not in {"WAIT", "HOLD"}:
        return None
    conditions = decoded.get("missing_conditions")
    if not isinstance(conditions, list) or len(conditions) > 32 or any(
        not isinstance(item, str) or len(item) > 300 for item in conditions
    ):
        return None
    analysis = decoded.get("strategy_analysis")
    if analysis is not None and (not isinstance(analysis, dict) or "missing_conditions" in analysis):
        return None
    decoded["strategy_analysis"] = {**(analysis or {}), "missing_conditions": conditions}
    decoded.pop("missing_conditions")
    return {"field": "missing_conditions", "normalized_field": "strategy_analysis.missing_conditions"}


def normalize_wait_symbol_alias(decoded: object, allowed_instruments: object) -> dict[str, object] | None:
    """Accept an authorized symbol alias only for non-executing decisions.

    The model's raw response remains in the audit record. OPEN and position
    management actions continue to require the exact instrument_id contract.
    """
    if not isinstance(decoded, dict) or decoded.get("action") not in {"WAIT", "HOLD"}:
        return None
    if "instrument_id" in decoded or "symbol" not in decoded:
        return None
    symbol = decoded.get("symbol")
    if not isinstance(symbol, str) or not symbol or symbol != symbol.strip().upper():
        return None
    if not isinstance(allowed_instruments, (list, tuple, set, frozenset)) or symbol not in allowed_instruments:
        return None
    decoded["instrument_id"] = decoded.pop("symbol")
    return {"field": "symbol", "normalized_field": "instrument_id", "value": symbol}


def require_entry_analysis_for_open(decoded: object) -> None:
    """Require executable prices and risk, not a prescribed explanation shape."""
    if not isinstance(decoded, dict) or decoded.get("action") not in ("OPEN_LONG", "OPEN_SHORT"):
        return
    missing: list[str] = []
    for field in ("entry_price", "stop_price", "take_profit", "requested_risk_fraction"):
        if isinstance(decoded.get(field), bool) or not isinstance(decoded.get(field), (int, float)):
            missing.append(field)
    if not isinstance(decoded.get("evidence_refs"), list) or not decoded["evidence_refs"]:
        missing.append("evidence_refs")
    if missing:
        raise ValueError("INVALID_ACTION_SCHEMA:OPEN_CONTEXT_REQUIRED:" + ",".join(missing))


def require_nofx_gate_open_contract(proposal: object) -> None:
    """The NOFX-style Gate route lets the model size its own order.

    Position size, leverage and order preference must therefore be
    model-authored on every OPEN. A missing one is a contract violation to be
    reported by name, never a default to be filled in silently.
    """
    if not isinstance(proposal, dict) or proposal.get("action") not in ("OPEN_LONG", "OPEN_SHORT"):
        return
    missing = [
        field
        for field in ("position_size_usdt", "requested_leverage", "order_preference")
        if proposal.get(field) in (None, "")
    ]
    if missing:
        raise ValueError("NOFX_GATE_OPEN_CONTRACT_MISSING:" + ",".join(missing))


__all__ = [
    "AI_ACTION_SCHEMA",
    "CALIBRATION_PROFILE_SCHEMA",
    "SIGNAL_SCHEMA",
    "normalize_limit_ttl_alias",
    "normalize_misplaced_strategy_plan",
    "normalize_news_impact",
    "normalize_wait_conditions",
    "normalize_wait_symbol_alias",
    "require_confidence_for_open",
    "require_entry_analysis_for_open",
    "require_nofx_gate_open_contract",
    "validate_schema",
]

