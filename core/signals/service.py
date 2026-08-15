"""Phase 1 deterministic signal builder.

The service proves the lifecycle without pretending that a quant baseline is an
LLM. Phase 2 can replace this implementation behind the same schema.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from ..instruments import Instrument
from ..quant.engine import QuantSnapshot
from ..time_rules import TimePolicy, time_rule_for
from .schema import Action, SignalProposal


def _hash_quant(quant: QuantSnapshot) -> str:
    payload = json.dumps(quant.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_signal(
    instrument: Instrument,
    quant: QuantSnapshot,
    *,
    timeframe: str = "1h",
    generated_at: datetime | None = None,
    prediction_id: str | None = None,
    time_policy: TimePolicy | None = None,
    model_id: str = "phase1-quant-baseline",
    model_version: str | None = None,
    prompt_version: str | None = None,
    raw_model_response: str | None = None,
    parse_status: str = "baseline",
    latency_ms: float | None = None,
    input_hash: str | None = None,
    data_as_of: datetime | None = None,
    context_json: str | None = None,
    force_action: Action | None = None,
    reason_codes: tuple[str, ...] | None = None,
    summary: str | None = None,
    reevaluate_minutes: int | None = None,
    confidence_cap: float | None = None,
    source_type: str = "live",
    replay_run_id: str | None = None,
) -> SignalProposal:
    now = (generated_at or quant.timestamp).astimezone(timezone.utc)
    score = 0.65 * quant.trend_score + 0.35 * quant.momentum_score
    # The Phase 1 baseline is intentionally modest; later calibration can raise
    # this threshold after real outcomes exist. WAIT remains the default.
    if force_action is not None:
        action = force_action
    elif score >= 0.08 and quant.market_regime == "bull_trend":
        action = Action.LONG
    elif score <= -0.08 and quant.market_regime == "bear_trend":
        action = Action.SHORT
    else:
        action = Action.WAIT
    if time_policy is None:
        rule = time_rule_for(timeframe, volatility_ratio=max(1.0, quant.atr14 / max(quant.price * 0.01, 1e-9)))
        validity = (rule.signal_validity_min + rule.signal_validity_max) // 2
        max_hold = rule.max_hold_min
        expected = max(30, max_hold // 3)
    else:
        validity_values = time_policy.signal_validity_allowed or (time_policy.signal_validity_min, time_policy.signal_validity_max)
        validity = validity_values[len(validity_values) // 2]
        max_hold = time_policy.holding_horizon_max
        hold_values = time_policy.holding_horizon_allowed or (time_policy.holding_horizon_min, max_hold)
        expected = hold_values[len(hold_values) // 2]
        reevaluate = reevaluate_minutes or (time_policy.reevaluate_allowed[len(time_policy.reevaluate_allowed) // 2] if time_policy.reevaluate_allowed else (time_policy.reevaluate_min + time_policy.reevaluate_max) // 2)
    confidence = min(0.95, max(0.05, 0.5 + abs(score) * 0.45))
    if confidence_cap is not None:
        confidence = min(confidence, max(0.0, min(1.0, confidence_cap)))
    common = {
        "prediction_id": prediction_id or str(uuid4()),
        "instrument": instrument,
        "analysis_timeframe": timeframe,
        "generated_at": now,
        "signal_validity_minutes": validity,
        "expected_hold_minutes": expected,
        "max_hold_minutes": max_hold,
        "reevaluate_at": now + timedelta(minutes=reevaluate if time_policy is not None else validity),
        "raw_confidence": round(confidence, 4),
        "reason_codes": reason_codes or (("ema_alignment", "momentum_alignment") if action is not Action.WAIT else ("insufficient_edge",)),
        "model_id": model_id,
        "prompt_version": prompt_version,
        "input_hash": input_hash or _hash_quant(quant),
        "model_version": model_version,
        "data_as_of": data_as_of,
        "context_json": context_json,
        "raw_model_response": raw_model_response,
        "parse_status": parse_status,
        "latency_ms": latency_ms,
        "source_type": source_type,
        "replay_run_id": replay_run_id,
    }
    if action is Action.WAIT:
        return SignalProposal(
            **common,
            action=action,
            entry_low=None,
            entry_high=None,
            stop=None,
            tp1=None,
            tp2=None,
            invalidation=("new quant snapshot required before entry",),
            summary=summary or "WAIT: current quant context does not provide a sufficient edge.",
        )
    entry = quant.price
    risk = max(quant.atr14 * 1.5, entry * 0.005)
    if action is Action.LONG:
        stop, tp1, tp2 = entry - risk, entry + 1.5 * risk, entry + 2.5 * risk
        invalidation = (f"close below {stop:.4f}", "high-impact negative event")
        summary = "LONG: trend and momentum align; use the ATR-based entry plan."
    else:
        stop, tp1, tp2 = entry + risk, entry - 1.5 * risk, entry - 2.5 * risk
        invalidation = (f"close above {stop:.4f}", "high-impact positive event")
        summary = "SHORT: trend and momentum align; use the ATR-based entry plan."
    return SignalProposal(
        **common,
        action=action,
        entry_low=entry * 0.998,
        entry_high=entry * 1.002,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        invalidation=invalidation,
        summary=summary,
    )
