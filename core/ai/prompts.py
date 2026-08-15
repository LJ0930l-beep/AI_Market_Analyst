"""Versioned, compact prompts for the local structured model."""

from __future__ import annotations

import json

from ..context import MarketContext
from .contracts import SignalPolicy


PROMPT_VERSION = "phase2-json-v8"


def _schema_text(policy: SignalPolicy) -> str:
    """Return a valid, policy-aware example instead of misleading zero placeholders."""

    validity = policy.signal_validity_allowed[0] if policy.signal_validity_allowed else policy.signal_validity_min
    holding = policy.holding_horizon_allowed[0] if policy.holding_horizon_allowed else policy.holding_horizon_min
    reevaluate = policy.reevaluate_allowed[0] if policy.reevaluate_allowed else policy.reevaluate_min
    wait_example = json.dumps(
        {
            "action": "WAIT",
            "confidence_raw": 0,
            "entry_preference": "none",
            "entry_zone": None,
            "stop": None,
            "tp1": None,
            "tp2": None,
            "signal_validity_minutes": validity,
            "holding_horizon_minutes": holding,
            "re_evaluate_minutes": reevaluate,
            "invalidation": [],
            "thesis": ["insufficient evidence"],
            "risk_factors": ["no clear directional edge"],
        },
        separators=(",", ":"),
    )
    return (
        "Required JSON keys: action, confidence_raw, entry_preference, entry_zone, stop, tp1, tp2, "
        "signal_validity_minutes, holding_horizon_minutes, re_evaluate_minutes, invalidation, thesis, risk_factors. "
        "Allowed action literals: LONG, SHORT, WAIT. Allowed entry_preference literals: market, pullback, breakout, limit, none. "
        f"Valid WAIT example using allowed policy values: {wait_example}"
    )


def build_system_prompt(policy: SignalPolicy, *, repair: bool = False) -> str:
    repair_line = "Repair the prior output and every listed Python validation error: return only one valid JSON object matching the schema." if repair else "Return one valid JSON object matching the schema."
    return (
        "You are a cautious market-analysis assistant. You do not place orders. "
        "Use only the supplied structured context; do not invent prices, indicators, timestamps, or news. "
        "WAIT is valid and preferred when evidence is insufficient. "
        "Hard WAIT rule: when action is WAIT, entry_zone, stop, tp1, and tp2 MUST all be JSON null; never emit price levels for WAIT. "
        "Hard actionable rule: when action is LONG or SHORT, entry_zone.low, entry_zone.high, stop, tp1, and tp2 MUST all be numeric and coherent. "
        "For LONG or SHORT, invalidation MUST be a non-empty JSON array of short strings. "
        "invalidation, thesis, and risk_factors MUST each be JSON arrays whose items are strings, never a single string. "
        "LONG price order MUST be stop < entry_zone midpoint < tp1 <= tp2; SHORT price order MUST be stop > entry_zone midpoint > tp1 >= tp2. "
        "For SHORT calculate midpoint E first: stop must be above E, TP1 must be below E, and TP2 must be at or below TP1; never put a SHORT stop below E. "
        "For LONG calculate midpoint E first: stop must be below E and both profit targets must be above E. "
        "Risk/reward is numeric: for SHORT set TP1 <= E - 1.5 * (stop - E); for LONG set TP1 >= E + 1.5 * (E - stop). "
        "The Python policy is authoritative: select times only inside its ranges and keep price levels coherent. "
        "Use exactly one of the allowed time values supplied by python_policy; do not invent a time value. "
        "Directional gate: if time_policy.event_risk is true or quant.market_regime is range, choose WAIT. "
        "If python_policy.news_available is false, choose WAIT in live mode; if python_policy.technical_only is true, historical news is unavailable by design, use only supplied technical context, and follow the Python decision gate without inventing news. "
        "Otherwise, when quant.market_regime is bull_trend with trend_score >= 0.10, action MUST be LONG; "
        "when it is bear_trend with trend_score <= -0.10, action MUST be SHORT; use WAIT only when the trend is weaker or mixed. "
        "For LONG/SHORT, anchor the entry zone near price.last, keep the stop within python_policy.stop_distance_max, "
        "and make TP1 at least 1.5R from the entry so the Python validator can verify the risk/reward. "
        "The user payload contains python_decision_gate.action; this Python-owned gate is authoritative for action. "
        "Output exactly that action and do not replace a LONG or SHORT gate with WAIT. "
        "No markdown, no code fences, no commentary. "
        f"{repair_line} Schema: {_schema_text(policy)}"
    )


def _python_decision_gate(context: MarketContext, policy: SignalPolicy) -> dict[str, object]:
    """Expose the bounded Phase 2 action gate so the small local model cannot ignore it."""

    time_policy = context.time_policy
    event_risk = bool(time_policy.event_risk) if time_policy is not None else False
    trend_score = float(context.quant.trend_score)
    regime = context.quant.market_regime
    if policy.stale_data or (not policy.news_available and not policy.technical_only):
        action = "WAIT"
        reason = "stale_or_unavailable_supporting_data"
    elif event_risk or regime == "range":
        action = "WAIT"
        reason = "event_risk_or_range_regime"
    elif regime == "bull_trend" and trend_score >= 0.10:
        action = "LONG"
        reason = "bull_trend_gate"
    elif regime == "bear_trend" and trend_score <= -0.10:
        action = "SHORT"
        reason = "bear_trend_gate"
    else:
        action = "WAIT"
        reason = "weak_or_mixed_trend"
    return {
        "action": action,
        "reason": reason,
        "event_risk": event_risk,
        "market_regime": regime,
        "trend_score": trend_score,
    }


def build_user_prompt(context: MarketContext, policy: SignalPolicy) -> str:
    payload = {
        "context": context.to_prompt_payload(max_news=8),
        "python_policy": policy.to_dict(),
        "python_decision_gate": _python_decision_gate(context, policy),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def build_prompt_messages(context: MarketContext, policy: SignalPolicy, *, repair: bool = False) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": build_system_prompt(policy, repair=repair)},
        {"role": "user", "content": build_user_prompt(context, policy)},
    ]


def build_repair_messages(context: MarketContext, policy: SignalPolicy, error: str) -> list[dict[str, str]]:
    messages = build_prompt_messages(context, policy, repair=True)
    messages.append({"role": "user", "content": json.dumps({"repair_error": error[:500]}, ensure_ascii=True, separators=(",", ":"))})
    return messages
