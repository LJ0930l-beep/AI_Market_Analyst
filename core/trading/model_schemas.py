"""JSON schemas sent to the local Gemini route and used by validators."""

from __future__ import annotations
import copy
import math
import re


FROZEN_DECISION_SCHEMA_ID = "aima:frozen-decision:v1"


def frozen_decision_schema(inputs, base_schema=None, *, gate_wait=False):
    """Constrain identity and references to facts actually visible to this call."""
    result = copy.deepcopy(base_schema or AI_ACTION_SCHEMA)
    result["$id"] = FROZEN_DECISION_SCHEMA_ID
    instruments = inputs.get("allowed_instruments")
    refs = inputs.get("evidence_refs")
    if not isinstance(instruments, list) or not instruments or any(
        not isinstance(item, str) or not item for item in instruments
    ):
        raise ValueError("MODEL_VISIBLE_INSTRUMENTS_REQUIRED")
    if not isinstance(refs, list) or any(not isinstance(item, str) or not item or len(item) > 200 for item in refs):
        raise ValueError("MODEL_VISIBLE_EVIDENCE_REFS_INVALID")
    instrument_schema = result["properties"]["instrument_id"]
    pinned = instrument_schema.get("enum")
    permitted = [item for item in dict.fromkeys(instruments) if pinned is None or item in pinned]
    if not permitted:
        raise ValueError("MODEL_VISIBLE_INSTRUMENTS_OUTSIDE_PINNED_SCHEMA")
    instrument_schema["enum"] = permitted
    ref_schema = result["properties"]["evidence_refs"]
    if refs:
        ref_schema["items"]["enum"] = list(dict.fromkeys(refs))
    else:
        ref_schema["maxItems"] = 0
    if gate_wait:
        result = _gate_wait_generation_schema(result)
    return result


def _gate_wait_generation_schema(schema):
    """Keep WAIT explanatory facts mandatory without prescribing a trade action."""
    actions = schema["properties"]["action"].get("enum", [])
    if "WAIT" not in actions:
        return schema
    # Full object alternatives also work with the pinned native converter.
    # Keep the common root properties for call-site introspection and local bounds.
    common = copy.deepcopy(schema)
    common.pop("$id", None)
    common.pop("anyOf", None)
    branches = []
    other_actions = [action for action in actions if action != "WAIT"]
    if other_actions:
        other = copy.deepcopy(common)
        other["properties"]["action"]["enum"] = other_actions
        branches.append(other)
    waiting = copy.deepcopy(common)
    waiting["properties"]["action"]["enum"] = ["WAIT"]
    if "strategy_analysis" not in waiting["required"]:
        waiting["required"].append("strategy_analysis")
    analysis = waiting["properties"]["strategy_analysis"]
    analysis["type"] = "object"
    analysis["required"] = list(dict.fromkeys([*analysis.get("required", []), "missing_conditions"]))
    missing = analysis["properties"]["missing_conditions"]
    missing["minItems"] = 1
    missing["items"].update({"minLength": 1, "pattern": r"^[\s\S]*\S[\s\S]*$"})
    for field, kind in (("next_trigger_price", "number"), ("entry_condition", "string")):
        branch = copy.deepcopy(waiting)
        if field not in branch["required"]:
            branch["required"].append(field)
        trigger = branch["properties"][field]
        trigger["type"] = kind
        if kind == "string":
            trigger.update({"minLength": 1, "pattern": r"^[\s\S]*\S[\s\S]*$"})
        branches.append(branch)
    schema["anyOf"] = branches
    return schema


def frozen_response_format(schema):
    """Native llama.cpp grammar projection; full bounds are validated locally.

    Nullable type lists with bounds fail on the pinned converter. Project them
    to anyOf and leave size/range checks to the unchanged local contract. Exact
    action, instrument and evidence enums remain native generation constraints.
    """
    omitted = {"$id", "minLength", "maxLength", "minimum", "maximum",
               "exclusiveMinimum", "exclusiveMaximum", "minItems", "maxItems"}
    def project(value):
        if isinstance(value, list):
            return [project(item) for item in value]
        if not isinstance(value, dict):
            return value
        if value.get("type") == "array" and value.get("maxItems") == 0:
            return {"enum": [[]]}
        result = {key: project(item) for key, item in value.items() if key not in omitted}
        if result.get("pattern") == r"^[\s\S]*\S[\s\S]*$":
            # The pinned converter cannot handle \s/\S and falls back to any
            # string, skipping minLength too. Native generation enforces only
            # nonempty text; full Unicode whitespace rejection stays local.
            result.pop("pattern")
        # The converter accepts lower cardinality bounds on concrete types;
        # only nullable bound combinations need the compatibility projection.
        if value.get("type") == "string" and "minLength" in value:
            result["minLength"] = value["minLength"]
        if value.get("type") == "array" and "minItems" in value:
            result["minItems"] = value["minItems"]
        if isinstance(result.get("type"), list):
            kinds = result.pop("type")
            result = {"anyOf": [dict(result, type=kind) for kind in kinds]}
        return result
    return {"type": "json_schema", "json_schema": {
        "name": "aima_frozen_decision", "strict": True, "schema": project(schema),
    }}


def gemini_response_format(schema):
    """Project the common contract without llama-specific union/array enums.

    Antigravity 4.9.4 merges object anyOf branches and stringifies enum values.
    Sending the old empty-array enum therefore generates a string instead of
    evidence_refs. Keep native field types and exact string identities. The
    unchanged full schema remains authoritative for conditional requirements,
    ranges and bounds after generation.
    """
    projected = copy.deepcopy(schema)
    gate_wait_contract = any(
        branch.get("properties", {}).get("action", {}).get("enum") == ["WAIT"]
        for branch in schema.get("anyOf", [])
        if isinstance(branch, dict)
    )
    projected.pop("$id", None)
    projected.pop("anyOf", None)
    # The relay drops required keys whose type includes null. Confidence is
    # required by the local action contract, so request a concrete score.
    confidence = projected.get("properties", {}).get("confidence")
    if isinstance(confidence, dict):
        confidence["type"] = "number"
    ttl = projected.get("properties", {}).get("ttl_seconds")
    if isinstance(ttl, dict):
        # The relay retains object properties/required/string enum, but loses
        # numeric bounds and string length/pattern. Four digit enums avoid
        # free-form duration prose without narrowing any valid integer choice.
        lower, upper = ttl.get("minimum"), ttl.get("maximum")
        pinned = f"{lower:04d}" if type(lower) is int and lower == upper and 60 <= lower <= 1800 else None
        digits = ("d3", "d2", "d1", "d0")
        ttl.clear()
        ttl.update(type="object", additionalProperties=False, required=list(digits),
                   properties={key: {"type": "string", "enum": [pinned[index]] if pinned else
                               list("01" if index == 0 else "0123456789")}
                               for index, key in enumerate(digits)})
        ttl["description"] = (
            '委托有效期60至1800整数秒的四位十进制数字对象，d3千位/d2百位/d1十位/d0个位；'
            '例如900秒写{"d3":"0","d2":"9","d1":"0","d0":"0"}。'
            "每位只填单个枚举数字，执行前严格还原同一整数，不填写说明文本。"
            "WAIT/HOLD等不需要委托有效期的动作请省略这个可选字段，不用0占位或连续补0。"
            "若补齐契约要求保留已有ttl_seconds，保持原值。"
        )
    condition = projected.get("properties", {}).get("entry_condition")
    if isinstance(condition, dict):
        condition["type"] = "string"
        condition["minLength"] = 1
        condition["description"] = "本轮动作的事实条件；WAIT写下一次需满足的可核验条件，未知价格不得编造。"
        projected["required"] = list(dict.fromkeys([*projected.get("required", []), "entry_condition"]))
    analysis = projected.get("properties", {}).get("strategy_analysis", {})
    for alias in ("entry_condition", "next_trigger_price"):
        analysis.get("properties", {}).pop(alias, None)
    if gate_wait_contract and isinstance(analysis, dict):
        # The relay drops the conditional WAIT branches. Request their text
        # container explicitly instead of inventing missing conditions after
        # a completion. OPEN/HOLD may use [] and retain their existing rules;
        # local validation still requires nonblank conditions for WAIT only.
        analysis["type"] = "object"
        analysis["required"] = list(dict.fromkeys([*analysis.get("required", []), "missing_conditions"]))
        missing = analysis["properties"]["missing_conditions"]
        missing["description"] = "WAIT必须逐条填写尚未成立的可核验条件，至少一条；其他动作没有缺失条件时使用空数组。"
        projected["required"] = list(dict.fromkeys([*projected.get("required", []), "strategy_analysis"]))
    return {"type": "json_schema", "json_schema": {
        "name": "aima_gemini_decision", "strict": True, "schema": projected,
    }}


def validate_schema(value, schema, path="output"):
    """Validate the bounded JSON-schema subset used by these local contracts.

    Native generation constraints never replace local semantic validation.
    """
    kinds = schema.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    actual = ("null" if value is None else "boolean" if isinstance(value, bool) else
              "integer" if isinstance(value, int) else "number" if isinstance(value, float) else
              "string" if isinstance(value, str) else "array" if isinstance(value, list) else
              "object" if isinstance(value, dict) else "invalid")
    if "type" in schema and actual not in kinds and not (actual == "integer" and "number" in kinds):
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
    if actual == "string" and "pattern" in schema and re.search(schema["pattern"], value) is None:
        raise ValueError(f"INVALID_ACTION_SCHEMA:{path}:pattern")
    if actual == "object":
        props = schema.get("properties", {})
        if set(schema.get("required", [])) - set(value) or (schema.get("additionalProperties") is False and set(value) - set(props)):
            raise ValueError(f"INVALID_ACTION_SCHEMA:{path}:fields")
        for key, item in value.items():
            if key in props:
                validate_schema(item, props[key], path + "." + key)
    if actual == "array" and "items" in schema:
        for item in value:
            validate_schema(item, schema["items"], path + "[]")
    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            try:
                validate_schema(value, branch, path)
            except ValueError:
                continue
            break
        else:
            raise ValueError(f"INVALID_ACTION_SCHEMA:{path}:anyOf")


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
        "action": {"type": "string", "enum": ["WAIT", "HOLD", "OPEN_LONG", "OPEN_SHORT", "REDUCE_POSITION", "CLOSE_POSITION", "TIGHTEN_STOP", "UPDATE_PROTECTION", "CANCEL_ORDER"]},
        "instrument_id": {"type": "string", "minLength": 1, "maxLength": 80},
        "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
        "position_id": {"type": ["string", "null"], "maxLength": 160},
        # CANCEL_ORDER is accepted only for a unique, locally owned Gate
        # intent after execution_gateway verifies account, mode, venue and ID.
        "order_id": {"type": ["string", "null"], "maxLength": 160},
        "entry_condition": {"type": ["string", "null"], "maxLength": 500},
        "entry_price": {"type": ["number", "null"]},
        "stop_price": {"type": ["number", "null"]},
        "take_profit": {"type": ["number", "null"]},
        "requested_risk_fraction": {"type": ["number", "null"]},
        "position_size_usdt": {"type": ["number", "null"], "exclusiveMinimum": 0},
        "requested_leverage": {"type": ["integer", "null"], "minimum": 1},
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
    """Losslessly accept canonical TTL digits/text or one unambiguous alias.

    The strategy profile calls this value ``limit_ttl_seconds`` while the
    action contract calls it ``ttl_seconds``.  Preserve all other schema
    errors, including conflicting fields and out-of-range values.  The raw
    model response is retained separately by the coordinator.
    """
    if not isinstance(decoded, dict):
        return None
    alias = "limit_ttl_seconds" in decoded
    if alias and "ttl_seconds" in decoded:
        return None
    field = "limit_ttl_seconds" if alias else "ttl_seconds"
    value = decoded.get(field)
    if (isinstance(value, dict) and set(value) == {"d3", "d2", "d1", "d0"}
            and all(type(value[key]) is str and value[key] in "0123456789"
                    and len(value[key]) == 1 for key in ("d3", "d2", "d1", "d0"))):
        number = int("".join(value[key] for key in ("d3", "d2", "d1", "d0")))
        if number == 0 and decoded.get("action") == "WAIT":
            decoded.pop(field, None)
            decoded["ttl_seconds"] = None
            return {"field": field, "raw": value, "normalized_field": "ttl_seconds",
                    "normalized": None, "source": "WAIT_UNUSED_TTL_ZERO_TO_NULL"}
        if not 60 <= number <= 1800:
            return None
    elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{1,3}", value):
        number = int(value)
        if not 60 <= number <= 1800:
            return None
    elif alias and type(value) is int and 60 <= value <= 1800:
        number = value
    else:
        return None
    if alias:
        decoded.pop(field)
    decoded["ttl_seconds"] = number
    return {"field": field, "raw": value, "normalized_field": "ttl_seconds",
            "normalized": number, "source": "LOSSLESS_CANONICAL_TTL"}


def normalize_strategy_analysis_aliases(decoded: object) -> dict[str, object] | None:
    """Move recognized flat analysis fields under ``strategy_analysis``.

    Gemini sometimes emits these optional explanation fields beside the action
    instead of inside their declared object. Preserve only a complete,
    type-valid, non-conflicting projection; execution fields are untouched.
    """
    if not isinstance(decoded, dict):
        return None
    aliases = ("matched_conditions", "missing_conditions", "trigger_completion_pct")
    present = [key for key in aliases if key in decoded]
    if not present:
        return None
    analysis = decoded.get("strategy_analysis")
    if analysis is not None and not isinstance(analysis, dict):
        return None
    analysis = dict(analysis or {})
    for key in present:
        value = decoded[key]
        if key in analysis and analysis[key] != value:
            return None
        if key in {"matched_conditions", "missing_conditions"} and (
            not isinstance(value, list)
            or len(value) > 32
            or any(not isinstance(item, str) or len(item) > 300 for item in value)
        ):
            return None
        if key == "trigger_completion_pct" and value is not None and (
            type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100
        ):
            return None
        analysis[key] = value
    decoded["strategy_analysis"] = analysis
    for key in present:
        decoded.pop(key, None)
    return {
        "source": "KNOWN_FLAT_STRATEGY_ANALYSIS_ALIAS",
        "normalized_fields": present,
    }


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


def normalize_protection_update_aliases(decoded: object) -> dict[str, object] | None:
    """Copy exact model-authored update prices; conflicting or invalid values fail closed."""
    if not isinstance(decoded, dict) or decoded.get("action") != "UPDATE_PROTECTION":
        return None
    changes = {}
    # Validate all fields first: an invalid second leg cannot leave the first mutated.
    for old, new in (("stop_price", "new_stop_price"), ("take_profit", "new_take_profit")):
        source, canonical = decoded.get(old), decoded.get(new)
        for value in (source, canonical):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
                raise ValueError("MODEL_PROTECTION_PRICE_INVALID")
        if source is not None and canonical is not None and source != canonical:
            raise ValueError("MODEL_PROTECTION_FIELDS_CONFLICT")
        if source is not None and canonical is None:
            changes[new] = {"source_field": old, "source_value": source,
                            "canonical_original_value": canonical}
    if not changes:
        return None
    for field, detail in changes.items():
        decoded[field] = detail["source_value"]
    return {"normalization": "EXACT_MODEL_AUTHORED_UPDATE_PROTECTION_ALIAS",
            "normalized_fields": list(changes), "field_sources": changes,
            "original_price_fields_preserved": True, "execution_prices_generated": False}


def normalize_wait_unused_protection(decoded: object) -> dict[str, object] | None:
    """Only WAIT zero placeholders become null; execution prices stay strict."""
    if not isinstance(decoded, dict) or decoded.get("action") != "WAIT":
        return None
    fields = [key for key in ("stop_price", "take_profit", "take_profit_1", "take_profit_2")
              if type(decoded.get(key)) in (int, float) and decoded[key] == 0]
    if not fields:
        return None
    original = {key: decoded[key] for key in fields}
    for key in fields:
        decoded[key] = None
    return {"normalization": "WAIT_UNUSED_PROTECTION_ZERO_TO_NULL",
            "normalized_fields": fields, "original_values": original,
            "action_changed": False, "execution_prices_generated": False}


def gate_wait_text_repair_fields(proposal, error, allowed_instruments):
    """One AI-authored repair of a WAIT timeframe description, never a trade."""
    if not isinstance(proposal, dict) or proposal.get("action") != "WAIT":
        return frozenset()
    if proposal.get("instrument_id") not in allowed_instruments:
        return frozenset()
    if not isinstance(proposal.get("reason"), str) or not proposal["reason"].strip():
        return frozenset()
    match = re.fullmatch(r"INVALID_ACTION_SCHEMA:output\.timeframe_analysis\.(5m|15m|1h|4h|1d):length", str(error))
    if match is None:
        return frozenset()
    timeframe = match.group(1)
    analysis = proposal.get("timeframe_analysis")
    if not isinstance(analysis, dict) or not isinstance(analysis.get(timeframe), str) or len(analysis[timeframe]) <= 800:
        return frozenset()
    return frozenset({"timeframe_analysis." + timeframe})


def assert_wait_text_repair_preserves_decision(before, after, mutable_fields):
    """The explanatory repair cannot add/remove keys or change any other value."""
    if not isinstance(before, dict) or before.get("action") != "WAIT":
        return
    if not isinstance(after, dict) or set(after) != set(before):
        raise ValueError("MODEL_REPAIR_CHANGED_WAIT_DECISION")
    analysis_before, analysis_after = before.get("timeframe_analysis"), after.get("timeframe_analysis")
    if not isinstance(analysis_before, dict) or not isinstance(analysis_after, dict) or set(analysis_before) != set(analysis_after):
        raise ValueError("MODEL_REPAIR_CHANGED_WAIT_DECISION")
    if any(after[key] != value for key, value in before.items() if key != "timeframe_analysis"):
        raise ValueError("MODEL_REPAIR_CHANGED_WAIT_DECISION")
    if any(analysis_after[key] != value for key, value in analysis_before.items()
           if "timeframe_analysis." + key not in mutable_fields):
        raise ValueError("MODEL_REPAIR_CHANGED_WAIT_DECISION")


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


def gate_open_schema_repair_fields(proposal, error, allowed_instruments):
    """Allow one model repair of incomplete, already recognizable OPENs only.

    This returns mutable field names, never replacement trading values.
    Existing prices, direction, symbol, size and leverage remain immutable.
    Missing prices may only be authored by the model in that one repair.
    Unknown references, provider failures and malformed identity are not repairs.
    """
    if not isinstance(proposal, dict) or proposal.get("action") not in {"OPEN_LONG", "OPEN_SHORT"}:
        return frozenset()
    if proposal.get("instrument_id") not in allowed_instruments or not str(proposal.get("reason") or "").strip():
        return frozenset()
    for field in ("entry_price", "stop_price", "take_profit", "confidence"):
        value = proposal.get(field)
        if field != "confidence" and value is None:
            continue
        if type(value) not in (int, float) or not math.isfinite(value):
            return frozenset()
        if (field == "confidence" and not 0 <= value <= 100) or (field != "confidence" and value <= 0):
            return frozenset()
    error = str(error)
    prefix = "NOFX_GATE_OPEN_CONTRACT_MISSING:"
    ttl_error = error == "INVALID_ACTION_SCHEMA:output.ttl_seconds:range"
    null_price_error = any(
        error == f"INVALID_ACTION_SCHEMA:output.{field}:type" and proposal.get(field) is None
        for field in ("entry_price", "stop_price", "take_profit")
    )
    if not ttl_error and not null_price_error and not error.startswith(prefix):
        return frozenset()
    fields = {key for key in ("entry_price", "stop_price", "take_profit", "position_size_usdt", "requested_leverage", "order_preference")
              if proposal.get(key) in (None, "")}
    if not proposal.get("evidence_refs"):
        fields.add("evidence_refs")
    if error.startswith(prefix) and not set(error[len(prefix):].split(",")).issubset(fields):
        return frozenset()
    if ttl_error:
        if type(proposal.get("ttl_seconds")) is not int:
            return frozenset()
        fields.add("ttl_seconds")
    return frozenset(fields)


def pin_gate_repair_trade_fields(schema, proposal, mutable_fields):
    """Ask the model to reproduce existing trade scalars exactly.

    Numeric enums are stringified by the relay, so use equal numeric bounds.
    Missing values remain for the model to author; this never fills prices,
    sizes or leverage, and the independent post-completion check stays active.
    """
    pinned = copy.deepcopy(schema)
    fields = (
        "action", "instrument_id", "entry_price", "stop_price", "take_profit",
        "requested_risk_fraction", "position_size_usdt", "requested_leverage",
        "order_preference", "limit_price", "ttl_seconds", "candidate_id", "strategy_candidate_id",
    )
    for name in fields:
        value = proposal.get(name)
        if name in mutable_fields or value is None or name not in pinned["properties"]:
            continue
        field = pinned["properties"][name]
        if type(value) in (int, float) and math.isfinite(value):
            field["type"] = "integer" if name in {"requested_leverage", "ttl_seconds"} else "number"
            field.update(minimum=value, maximum=value)
        elif isinstance(value, str):
            field.update(type="string", enum=[value])
        else:
            continue
        if name not in pinned["required"]:
            pinned["required"].append(name)
    return pinned


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
        for field in ("position_size_usdt", "requested_leverage", "order_preference", "entry_price", "stop_price", "take_profit")
        if proposal.get(field) in (None, "")
    ]
    if not proposal.get("evidence_refs"):
        missing.append("evidence_refs")
    if missing:
        raise ValueError("NOFX_GATE_OPEN_CONTRACT_MISSING:" + ",".join(missing))
    for field in ("entry_price", "stop_price", "take_profit"):
        value = proposal[field]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("NOFX_GATE_OPEN_PRICE_INVALID:" + field)


__all__ = [
    "AI_ACTION_SCHEMA",
    "CALIBRATION_PROFILE_SCHEMA",
    "SIGNAL_SCHEMA",
    "normalize_limit_ttl_alias",
    "normalize_misplaced_strategy_plan",
    "normalize_news_impact",
    "normalize_strategy_analysis_aliases",
    "normalize_wait_conditions",
    "normalize_wait_unused_protection",
    "gate_wait_text_repair_fields",
    "assert_wait_text_repair_preserves_decision",
    "normalize_wait_symbol_alias",
    "require_confidence_for_open",
    "require_entry_analysis_for_open",
    "require_nofx_gate_open_contract",
    "validate_schema",
]
