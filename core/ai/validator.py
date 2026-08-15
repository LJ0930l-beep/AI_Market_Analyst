"""Business validation between model JSON and the durable SignalProposal."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Protocol

from ..context import MarketContext
from ..signals.schema import Action, SignalProposal
from .contracts import LLMCallMetadata, LLMError, ModelSignalResponse, SignalPolicy


class SignalValidationError(ValueError):
    def __init__(self, errors: tuple[str, ...]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class ModelProvider(Protocol):
    def analyze_market(
        self,
        context: MarketContext,
        policy: SignalPolicy,
        *,
        repair: bool = False,
        repair_error: str | None = None,
    ) -> tuple[ModelSignalResponse, LLMCallMetadata]:
        ...


def validate_model_response(response: ModelSignalResponse, context: MarketContext, policy: SignalPolicy) -> tuple[str, ...]:
    errors: list[str] = []
    if not policy.signal_validity_min <= response.signal_validity_minutes <= policy.signal_validity_max:
        errors.append("signal_validity_minutes outside Python policy")
    elif policy.signal_validity_allowed and response.signal_validity_minutes not in policy.signal_validity_allowed:
        errors.append("signal_validity_minutes is not an allowed Python value")
    if not policy.holding_horizon_min <= response.holding_horizon_minutes <= policy.holding_horizon_max:
        errors.append("holding_horizon_minutes outside Python policy")
    elif policy.holding_horizon_allowed and response.holding_horizon_minutes not in policy.holding_horizon_allowed:
        errors.append("holding_horizon_minutes is not an allowed Python value")
    if not policy.reevaluate_min <= response.re_evaluate_minutes <= policy.reevaluate_max:
        errors.append("re_evaluate_minutes outside Python policy")
    elif policy.reevaluate_allowed and response.re_evaluate_minutes not in policy.reevaluate_allowed:
        errors.append("re_evaluate_minutes is not an allowed Python value")
    if response.action is Action.WAIT:
        return tuple(errors)
    levels = (response.entry_low, response.entry_high, response.stop, response.tp1, response.tp2)
    if any(value is None or value <= 0 for value in levels):
        errors.append("actionable levels must be positive")
        return tuple(errors)
    assert response.entry_low is not None
    assert response.entry_high is not None
    assert response.stop is not None
    assert response.tp1 is not None
    assert response.tp2 is not None
    if response.entry_low > response.entry_high:
        errors.append("entry_low exceeds entry_high")
    entry = (response.entry_low + response.entry_high) / 2.0
    if abs(entry - policy.price) > policy.entry_distance_limit:
        errors.append("entry zone is too far from current price")
    if response.action is Action.LONG:
        if not response.stop < entry < response.tp1 <= response.tp2:
            errors.append("LONG levels violate stop < entry < tp1 <= tp2")
    elif not response.stop > entry > response.tp1 >= response.tp2:
        errors.append("SHORT levels violate stop > entry > tp1 >= tp2")
    risk = abs(entry - response.stop)
    if risk <= 0 or risk > policy.stop_distance_limit:
        errors.append("stop distance is outside allowed ATR bounds")
    if risk > 0 and abs(response.tp1 - entry) / risk < 1.5 - 1e-9:
        errors.append("TP1 R:R is below 1.5")
    if not response.invalidation:
        errors.append("actionable response requires invalidation")
    return tuple(errors)


def _call_provider(
    provider: ModelProvider,
    context: MarketContext,
    policy: SignalPolicy,
    *,
    repair: bool,
    repair_error: str | None = None,
) -> tuple[ModelSignalResponse, LLMCallMetadata]:
    try:
        return provider.analyze_market(context, policy, repair=repair, repair_error=repair_error)
    except TypeError as exc:
        if "repair" not in str(exc):
            raise
        return provider.analyze_market(context, policy)  # type: ignore[call-arg]


def analyze_with_repair(provider: ModelProvider, context: MarketContext, policy: SignalPolicy) -> tuple[ModelSignalResponse, LLMCallMetadata]:
    """Run once, then allow exactly one repair attempt for parse/business errors."""

    try:
        response, metadata = _call_provider(provider, context, policy, repair=False)
        errors = validate_model_response(response, context, policy)
        if not errors:
            return response, metadata
        first_error = SignalValidationError(errors)
    except LLMError as exc:
        if exc.code not in {"parse_error", "output_too_long", "model_invalid_envelope"}:
            raise
        first_error = exc
    try:
        repaired, metadata = _call_provider(provider, context, policy, repair=True, repair_error="; ".join(first_error.errors) if isinstance(first_error, SignalValidationError) else str(first_error))
        errors = validate_model_response(repaired, context, policy)
        if errors:
            raise LLMError(
                f"repair failed: {'; '.join(errors)}",
                code="repair_failed",
                raw_response=metadata.raw_response,
            )
        return repaired, metadata.with_status("repair_valid")
    except LLMError as exc:
        raise LLMError(f"repair failed: {exc}", code="repair_failed", raw_response=exc.raw_response) from exc
    except SignalValidationError as exc:
        raise LLMError(f"repair failed: {exc}", code="repair_failed") from exc


def to_signal_proposal(
    response: ModelSignalResponse,
    context: MarketContext,
    policy: SignalPolicy,
    metadata: LLMCallMetadata,
    *,
    generated_at: datetime | None = None,
    prediction_id: str | None = None,
    source_type: str = "live",
    replay_run_id: str | None = None,
) -> SignalProposal:
    errors = validate_model_response(response, context, policy)
    if errors:
        raise SignalValidationError(errors)
    now = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    confidence = min(policy.confidence_cap, response.confidence_raw / 100.0)
    reasons = ["structured_model_output", f"regime:{context.quant.market_regime}"]
    if policy.stale_data:
        reasons.append("confidence_capped_stale_data")
    if not policy.news_available:
        reasons.append("news_unavailable")
    if metadata.parse_status.startswith("repair"):
        reasons.append("one_repair_attempt")
    summary = " ".join(response.thesis[:2]) if response.thesis else f"{response.action.value}: structured model decision."
    if response.risk_factors:
        summary = f"{response.action.value}: {summary} Risks: {'; '.join(response.risk_factors[:2])}."
    else:
        summary = f"{response.action.value}: {summary}."
    invalidation = response.invalidation or ("new context required before entry",)
    data_as_of = context.provider_snapshot.data_as_of if context.provider_snapshot is not None else context.quant.timestamp
    payload_json = json.dumps(context.to_prompt_payload(max_news=8), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return SignalProposal(
        prediction_id=prediction_id or f"llm-{now.strftime('%Y%m%d%H%M%S%f')}",
        instrument=context.instrument,
        analysis_timeframe=policy.timeframe,
        generated_at=now,
        action=response.action,
        entry_low=response.entry_low,
        entry_high=response.entry_high,
        stop=response.stop,
        tp1=response.tp1,
        tp2=response.tp2,
        signal_validity_minutes=response.signal_validity_minutes,
        expected_hold_minutes=response.holding_horizon_minutes,
        max_hold_minutes=policy.holding_horizon_max,
        reevaluate_at=now + timedelta(minutes=response.re_evaluate_minutes),
        invalidation=invalidation,
        raw_confidence=round(confidence, 4),
        reason_codes=tuple(reasons),
        summary=summary,
        model_id=metadata.model_id,
        model_version=metadata.model_version,
        prompt_version=metadata.prompt_version,
        input_hash=metadata.input_hash,
        data_as_of=data_as_of,
        context_json=payload_json,
        raw_model_response=metadata.raw_response,
        parse_status=metadata.parse_status,
        latency_ms=metadata.latency_ms,
        source_type=source_type,
        replay_run_id=replay_run_id,
    )
