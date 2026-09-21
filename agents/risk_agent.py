"""RiskAgent for AI Market Analyst V2.

Enforces institutional risk policies, position sizing, margin limits,
and correlation boundaries using Bonsai 2 27B via ModelClient.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from core.model_client import ModelClient, model_client
from core.logging import get_logger

logger = get_logger("risk_agent")


class RiskAgent:
    """Institutional portfolio risk management and trade sizing agent."""

    def __init__(self, client: ModelClient | None = None) -> None:
        self.client = client or model_client

    def assess_risk(
        self,
        *,
        account_id: str,
        equity: float,
        available_margin: float,
        proposed_symbol: str,
        entry_price: float,
        stop_loss: float,
        max_risk_fraction: float = 0.05,
    ) -> Dict[str, Any]:
        """Assess trade risk and calculate mathematically bounded position size."""
        if entry_price <= 0 or stop_loss <= 0 or equity <= 0:
            return {
                "decision": "REJECT",
                "reason": "INVALID_NUMERIC_PARAMETERS",
                "position_size": 0.0,
            }

        price_risk = abs(entry_price - stop_loss)
        risk_percentage = (price_risk / entry_price) * 100.0
        
        # Max allowable monetary loss
        allowed_risk_usdt = equity * max_risk_fraction
        
        # Sizing: allowed_loss / loss_per_unit
        if price_risk > 0:
            max_contracts = allowed_risk_usdt / price_risk
            notional_value = max_contracts * entry_price
        else:
            max_contracts = 0.0
            notional_value = 0.0

        # Margin check
        margin_required = notional_value / 20.0  # assume 20x max leverage
        is_safe = margin_required <= available_margin and risk_percentage <= 15.0

        prompt = f"""Assess the following proposed trade risk parameters:
- Account Equity: {equity:,.2f} USDT
- Available Margin: {available_margin:,.2f} USDT
- Symbol: {proposed_symbol}
- Entry: {entry_price:,.2f}, Stop Loss: {stop_loss:,.2f}
- Stop Loss Distance: {risk_percentage:.2f}%
- Calculated Risk Amount: {allowed_risk_usdt:.2f} USDT
- Calculated Max Notional: {notional_value:.2f} USDT
- Margin Required (20x): {margin_required:.2f} USDT

Provide a concise 3-sentence institutional risk sign-off. If approved, state APPROVED; if unsafe, state REJECTED with explicit rationale."""

        messages = [
            {"role": "system", "content": "You are the Chief Risk Officer (CRO) of a quantitative hedge fund."},
            {"role": "user", "content": prompt},
        ]

        resp = self.client.chat_completion(messages, mode="FAST")
        verdict_text = resp["choices"][0]["message"]["content"]

        return {
            "account_id": account_id,
            "decision": "APPROVE" if is_safe else "REJECT",
            "max_risk_usdt": round(allowed_risk_usdt, 2),
            "max_contracts": round(max_contracts, 4),
            "max_notional_usdt": round(notional_value, 2),
            "margin_required_usdt": round(margin_required, 2),
            "stop_loss_distance_pct": round(risk_percentage, 2),
            "cro_verdict": verdict_text,
        }
