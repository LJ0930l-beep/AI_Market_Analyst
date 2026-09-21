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


__all__ = ["AI_ACTION_SCHEMA", "CALIBRATION_PROFILE_SCHEMA", "SIGNAL_SCHEMA", "require_confidence_for_open"]
