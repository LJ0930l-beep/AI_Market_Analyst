"""Deterministic model adapter for integration tests and offline demos."""

from __future__ import annotations

import json
import time

from ..context import MarketContext
from ..signals.schema import Action
from .contracts import LLMCallMetadata, LLMError, ModelSignalResponse, SignalPolicy
from .prompts import PROMPT_VERSION

MOCK_MODEL_ID = "mock-llm"


class MockLLMProvider:
    provider_name = "mock_llm"

    @property
    def model_id(self) -> str:
        return MOCK_MODEL_ID

    def __init__(self, *, invalid_first: bool = False) -> None:
        self.invalid_first = invalid_first
        self.calls = 0

    def health(self) -> dict[str, object]:
        return {"provider": self.provider_name, "available": True, "model_id": self.model_id, "model_available": True}

    def _payload(self, context: MarketContext, policy: SignalPolicy) -> dict[str, object]:
        price = context.quant.price
        risk = max(context.quant.atr14, price * 0.005)
        validity = policy.signal_validity_allowed[len(policy.signal_validity_allowed) // 2] if policy.signal_validity_allowed else (policy.signal_validity_min + policy.signal_validity_max) // 2
        hold = policy.holding_horizon_allowed[len(policy.holding_horizon_allowed) // 2] if policy.holding_horizon_allowed else max(policy.holding_horizon_min, min(policy.holding_horizon_max, (policy.holding_horizon_min + policy.holding_horizon_max) // 2))
        reevaluate = policy.reevaluate_allowed[len(policy.reevaluate_allowed) // 2] if policy.reevaluate_allowed else max(policy.reevaluate_min, min(policy.reevaluate_max, validity // 2))
        if context.quant.market_regime == "bull_trend":
            return {
                "action": "LONG",
                "confidence_raw": min(72, policy.confidence_cap * 100),
                "entry_preference": "market",
                "entry_zone": {"low": price * 0.999, "high": price * 1.001},
                "stop": price - risk,
                "tp1": price + 1.6 * risk,
                "tp2": price + 2.4 * risk,
                "signal_validity_minutes": validity,
                "holding_horizon_minutes": hold,
                "re_evaluate_minutes": reevaluate,
                "invalidation": ["close below the ATR stop", "high-impact negative event"],
                "thesis": ["trend regime is bullish", "momentum confirms the direction"],
                "risk_factors": ["fixture or stale data if configured"],
            }
        if context.quant.market_regime == "bear_trend":
            return {
                "action": "SHORT",
                "confidence_raw": min(72, policy.confidence_cap * 100),
                "entry_preference": "market",
                "entry_zone": {"low": price * 0.999, "high": price * 1.001},
                "stop": price + risk,
                "tp1": price - 1.6 * risk,
                "tp2": price - 2.4 * risk,
                "signal_validity_minutes": validity,
                "holding_horizon_minutes": hold,
                "re_evaluate_minutes": reevaluate,
                "invalidation": ["close above the ATR stop", "high-impact positive event"],
                "thesis": ["trend regime is bearish", "momentum confirms the direction"],
                "risk_factors": ["fixture or stale data if configured"],
            }
        return {
            "action": "WAIT",
            "confidence_raw": min(45, policy.confidence_cap * 100),
            "entry_preference": "none",
            "entry_zone": None,
            "stop": None,
            "tp1": None,
            "tp2": None,
            "signal_validity_minutes": validity,
            "holding_horizon_minutes": hold,
            "re_evaluate_minutes": reevaluate,
            "invalidation": [],
            "thesis": ["current regime does not provide a directional edge"],
            "risk_factors": ["range conditions"],
        }

    def analyze_market(
        self,
        context: MarketContext,
        policy: SignalPolicy,
        *,
        repair: bool = False,
        repair_error: str | None = None,
    ) -> tuple[ModelSignalResponse, LLMCallMetadata]:
        self.calls += 1
        raw = "not-json" if self.invalid_first and self.calls == 1 and not repair else json.dumps(self._payload(context, policy), separators=(",", ":"))
        started = time.perf_counter()
        try:
            response = ModelSignalResponse.from_dict(json.loads(raw))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise LLMError(str(exc), code="parse_error", raw_response=raw) from exc
        metadata = LLMCallMetadata(
            model_id=self.model_id,
            model_version=self.model_id,
            prompt_version=PROMPT_VERSION,
            input_hash=context.input_hash(),
            latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
            raw_response=raw,
            parse_status="repair_valid" if repair else "valid",
            input_tokens_est=0,
            output_chars=len(raw),
        )
        return response, metadata
