"""Strict contracts for model output and auditable model calls."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from ..context import MarketContext
from ..signals.schema import Action


class LLMError(RuntimeError):
    """A bounded, user-visible model failure with no hidden fallback."""

    def __init__(self, message: str, *, code: str, raw_response: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.raw_response = raw_response


@dataclass(frozen=True, slots=True)
class SignalPolicy:
    """Python-owned choices the model is allowed to select from."""

    timeframe: str
    signal_validity_min: int
    signal_validity_max: int
    holding_horizon_min: int
    holding_horizon_max: int
    reevaluate_min: int
    reevaluate_max: int
    price: float
    atr14: float
    market_regime: str
    stale_data: bool = False
    news_available: bool = True
    technical_only: bool = False
    confidence_cap: float = 1.0
    signal_validity_allowed: tuple[int, ...] = ()
    holding_horizon_allowed: tuple[int, ...] = ()
    reevaluate_allowed: tuple[int, ...] = ()

    @classmethod
    def from_context(cls, context: MarketContext) -> "SignalPolicy":
        if context.time_policy is None:
            raise ValueError("MarketContext must include a TimePolicy before model analysis")
        policy = context.time_policy
        stale = bool(context.provider_snapshot.stale) if context.provider_snapshot is not None else False
        market_context = context.market_context or {}
        news_available = bool(market_context.get("news_available", True))
        capabilities = market_context.get("context_capabilities")
        technical_only = bool(capabilities.get("technical_only")) if isinstance(capabilities, dict) else False
        confidence_cap = 1.0
        if stale:
            confidence_cap = min(confidence_cap, 0.65)
        if not news_available:
            confidence_cap = min(confidence_cap, 0.60)
        return cls(
            timeframe=policy.timeframe,
            signal_validity_min=policy.signal_validity_min,
            signal_validity_max=policy.signal_validity_max,
            holding_horizon_min=policy.holding_horizon_min,
            holding_horizon_max=policy.holding_horizon_max,
            reevaluate_min=policy.reevaluate_min,
            reevaluate_max=policy.reevaluate_max,
            price=context.quant.price,
            atr14=context.quant.atr14,
            market_regime=context.quant.market_regime,
            stale_data=stale,
            news_available=news_available,
            technical_only=technical_only,
            confidence_cap=confidence_cap,
            signal_validity_allowed=policy.signal_validity_allowed,
            holding_horizon_allowed=policy.holding_horizon_allowed,
            reevaluate_allowed=policy.reevaluate_allowed,
        )

    @property
    def entry_distance_limit(self) -> float:
        return max(self.atr14 * 3.0, self.price * 0.03)

    @property
    def stop_distance_limit(self) -> float:
        return max(self.atr14 * 4.0, self.price * 0.08)

    def to_dict(self) -> dict[str, object]:
        return {
            "timeframe": self.timeframe,
            "allowed_actions": [Action.LONG.value, Action.SHORT.value, Action.WAIT.value],
            "signal_validity_minutes": [self.signal_validity_min, self.signal_validity_max],
            "holding_horizon_minutes": [self.holding_horizon_min, self.holding_horizon_max],
            "re_evaluate_minutes": [self.reevaluate_min, self.reevaluate_max],
            "allowed_values": {
                "signal_validity_minutes": list(self.signal_validity_allowed),
                "holding_horizon_minutes": list(self.holding_horizon_allowed),
                "re_evaluate_minutes": list(self.reevaluate_allowed),
            },
            "entry_price_bounds": [self.price - self.entry_distance_limit, self.price + self.entry_distance_limit],
            "stop_distance_max": self.stop_distance_limit,
            "confidence_max": self.confidence_cap * 100.0,
            "stale_data": self.stale_data,
            "news_available": self.news_available,
            "technical_only": self.technical_only,
        }


def _number(value: Any, field: str, *, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    if integer:
        if number != round(number):
            raise ValueError(f"{field} must be an integer")
        return int(number)
    return number


def _optional_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    return float(_number(value, field))


def _strings(value: Any, field: str, *, required: bool = False) -> tuple[str, ...]:
    if value is None:
        if required:
            raise ValueError(f"{field} is required")
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


@dataclass(frozen=True, slots=True)
class ModelSignalResponse:
    action: Action
    confidence_raw: float
    entry_preference: str
    entry_low: float | None
    entry_high: float | None
    stop: float | None
    tp1: float | None
    tp2: float | None
    signal_validity_minutes: int
    holding_horizon_minutes: int
    re_evaluate_minutes: int
    invalidation: tuple[str, ...]
    thesis: tuple[str, ...]
    risk_factors: tuple[str, ...]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ModelSignalResponse":
        if not isinstance(payload, dict):
            raise ValueError("model output must be a JSON object")
        try:
            action = Action(str(payload["action"]).upper())
        except (KeyError, ValueError) as exc:
            raise ValueError("action must be LONG, SHORT, or WAIT") from exc
        confidence = float(_number(payload.get("confidence_raw", payload.get("confidence")), "confidence_raw"))
        if not 0.0 <= confidence <= 100.0:
            raise ValueError("confidence_raw must be in [0, 100]")
        zone = payload.get("entry_zone")
        if zone is not None and not isinstance(zone, dict):
            raise ValueError("entry_zone must be an object or null")
        entry_low = _optional_number((zone or {}).get("low", payload.get("entry_low")), "entry_zone.low")
        entry_high = _optional_number((zone or {}).get("high", payload.get("entry_high")), "entry_zone.high")
        entry_preference = str(payload.get("entry_preference", "none" if action is Action.WAIT else "market")).strip().lower()
        if entry_preference not in {"market", "pullback", "breakout", "limit", "none"}:
            raise ValueError("entry_preference is unsupported")
        response = cls(
            action=action,
            confidence_raw=confidence,
            entry_preference=entry_preference,
            entry_low=entry_low,
            entry_high=entry_high,
            stop=_optional_number(payload.get("stop"), "stop"),
            tp1=_optional_number(payload.get("tp1"), "tp1"),
            tp2=_optional_number(payload.get("tp2"), "tp2"),
            signal_validity_minutes=int(_number(payload.get("signal_validity_minutes"), "signal_validity_minutes", integer=True)),
            holding_horizon_minutes=int(_number(payload.get("holding_horizon_minutes"), "holding_horizon_minutes", integer=True)),
            re_evaluate_minutes=int(_number(payload.get("re_evaluate_minutes", payload.get("reevaluate_minutes")), "re_evaluate_minutes", integer=True)),
            invalidation=_strings(payload.get("invalidation"), "invalidation", required=action is not Action.WAIT),
            thesis=_strings(payload.get("thesis"), "thesis"),
            risk_factors=_strings(payload.get("risk_factors"), "risk_factors"),
        )
        if action is Action.WAIT and any(value is not None for value in (entry_low, entry_high, response.stop, response.tp1, response.tp2)):
            raise ValueError("WAIT must not include actionable price levels")
        if action is not Action.WAIT and any(value is None for value in (entry_low, entry_high, response.stop, response.tp1, response.tp2)):
            raise ValueError("LONG/SHORT requires entry zone, stop, tp1, and tp2")
        return response

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            "confidence_raw": self.confidence_raw,
            "entry_preference": self.entry_preference,
            "entry_zone": {"low": self.entry_low, "high": self.entry_high} if self.entry_low is not None else None,
            "stop": self.stop,
            "tp1": self.tp1,
            "tp2": self.tp2,
            "signal_validity_minutes": self.signal_validity_minutes,
            "holding_horizon_minutes": self.holding_horizon_minutes,
            "re_evaluate_minutes": self.re_evaluate_minutes,
            "invalidation": list(self.invalidation),
            "thesis": list(self.thesis),
            "risk_factors": list(self.risk_factors),
        }


@dataclass(frozen=True, slots=True)
class LLMCallMetadata:
    model_id: str
    model_version: str | None
    prompt_version: str
    input_hash: str
    latency_ms: float
    raw_response: str | None
    parse_status: str
    error_code: str | None = None
    input_tokens_est: int | None = None
    output_chars: int | None = None
    schema_enforcement: str | None = None
    actual_model_id: str | None = None
    model_identity_source: str | None = None
    verified_manifest_model_id: str | None = None

    def with_status(self, status: str, *, error_code: str | None = None) -> "LLMCallMetadata":
        return replace(self, parse_status=status, error_code=error_code)

    def to_dict(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "input_hash": self.input_hash,
            "latency_ms": self.latency_ms,
            "raw_response": self.raw_response,
            "parse_status": self.parse_status,
            "error_code": self.error_code,
            "input_tokens_est": self.input_tokens_est,
            "output_chars": self.output_chars,
            "schema_enforcement": self.schema_enforcement,
            "actual_model_id": self.actual_model_id,
            "model_identity_source": self.model_identity_source,
            "verified_manifest_model_id": self.verified_manifest_model_id,
        }
