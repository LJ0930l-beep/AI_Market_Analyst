"""JSON schemas sent to Qwen/Ollama and used by local validators."""

from __future__ import annotations


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
    "required": ["action", "instrument_id", "reason"],
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
        "requested_leverage": {"type": ["integer", "null"], "minimum": 1, "maximum": 100},
        "new_stop_price": {"type": ["number", "null"]},
        "reduce_fraction": {"type": ["number", "null"]},
        "evidence_refs": {"type": "array", "items": {"type": "string", "maxLength": 200}, "maxItems": 32},
        "order_preference": {"type": "string", "enum": ["MARKET", "LIMIT", "AUTO"]},
        "limit_price": {"type": ["number", "null"]},
        "ttl_seconds": {"type": ["integer", "null"], "minimum": 60, "maximum": 300},
        "candidate_id": {"type": ["string", "null"], "maxLength": 200},
        "closed_15m_bar": {"type": ["string", "null"], "maxLength": 200},
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


__all__ = ["AI_ACTION_SCHEMA", "CALIBRATION_PROFILE_SCHEMA", "SIGNAL_SCHEMA"]
