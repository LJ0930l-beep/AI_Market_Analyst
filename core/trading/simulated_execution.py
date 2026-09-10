"""Authentic Simulated Execution Engine & Slippage/Latency Guard (Chapter 7, AT14).

Implements:
- Strict execution timeline: signal_at -> ai_started_at -> ai_completed_at -> intent_at -> submitted_at -> filled_at.
- Market price deviation guard (AT14): rejects orders if market price has drifted beyond allowed offset threshold.
- Conservative fill prices using post-submission ask/bid.
- Records fill events to SQLite and AccountLedger.
"""

from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
import math
from typing import Any, Dict, Optional, Tuple

from .ledger import AccountLedger, LedgerEventType
from .execution_gateway import (
    DecisionPath,
    ExecutionGateway,
    OrderIntent,
    ProtectionPlan,
    TradingMode,
)


class PriceDeviationExceededError(ValueError):
    """Raised when market price has moved beyond allowable slippage tolerance (AT14)."""
    pass


class ProposalExpiredError(ValueError):
    """Raised when slow model latency causes proposal to expire before submission."""
    pass


@dataclass
class ExecutionTimeline:
    signal_at: datetime
    ai_started_at: datetime
    ai_completed_at: datetime
    intent_at: datetime
    submitted_at: datetime
    filled_at: datetime

    def to_dict(self) -> Dict[str, str]:
        return {
            "signal_at": self.signal_at.isoformat(),
            "ai_started_at": self.ai_started_at.isoformat(),
            "ai_completed_at": self.ai_completed_at.isoformat(),
            "intent_at": self.intent_at.isoformat(),
            "submitted_at": self.submitted_at.isoformat(),
            "filled_at": self.filled_at.isoformat(),
        }


class SimulatedExecutionEngine:
    """Simulates realistic fills with latency, slippage, and price deviation checks."""

    def __init__(self, store: Any, ledger: AccountLedger, max_allowed_offset_pct: float = 0.005):
        self.store = store
        self.ledger = ledger
        self.max_allowed_offset_pct = max_allowed_offset_pct

    def execute_fill(
        self,
        identity: str,
        proposal: Dict[str, Any],
        risk_plan: Dict[str, Any],
        current_market_price: float,
        *,
        signal_time: datetime,
        ai_started_time: Optional[datetime] = None,
        ai_completed_time: Optional[datetime] = None,
        now: Optional[datetime] = None,
        fault: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute simulated market fill with latency check and price deviation verification (AT14)."""
        account_id = str(proposal.get("account_id") or "").strip()
        if not account_id:
            raise ValueError("ACCOUNT_REQUIRED")
        mode = str(proposal.get("mode") or "").upper()
        venue = str(proposal.get("venue") or "").lower()
        if mode != "PAPER" or venue != "simulated":
            raise ValueError("SIMULATED_EXECUTION_SCOPE_MISMATCH")
        now = now or datetime.now(timezone.utc)
        sig_t = min(signal_time, now)
        ai_started = ai_started_time or max(sig_t, now - timedelta(milliseconds=800))
        ai_completed = ai_completed_time or max(ai_started, now - timedelta(milliseconds=500))
        intent_at = max(ai_completed, now - timedelta(milliseconds=300))
        submitted_at = max(intent_at, now - timedelta(milliseconds=100))
        filled_at = max(submitted_at, now)

        timeline = ExecutionTimeline(
            signal_at=sig_t,
            ai_started_at=ai_started,
            ai_completed_at=ai_completed,
            intent_at=intent_at,
            submitted_at=submitted_at,
            filled_at=filled_at,
        )

        # Check proposal expiration
        expires_at_str = proposal.get("expires_at")
        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
            if filled_at > expires_at:
                raise ProposalExpiredError(f"Proposal expired at {expires_at_str}, current fill at {filled_at.isoformat()}")

        # Check market price deviation against signal entry price (AT14)
        signal_entry = float(proposal["entry"])
        if signal_entry <= 0:
            raise ValueError("Invalid signal entry price")

        deviation = abs(current_market_price - signal_entry) / signal_entry
        if deviation > self.max_allowed_offset_pct:
            raise PriceDeviationExceededError(
                f"Market price jumped {deviation*100:.2f}% (from {signal_entry} to {current_market_price}), exceeds allowable tolerance (max offset {self.max_allowed_offset_pct*100:.2f}%)"
            )

        side = str(proposal["side"]).upper()
        slippage = float(risk_plan.get("slippage", 0.001))
        contracts = float(risk_plan["contracts"])
        contract_size = float(risk_plan.get("contract_size", 1.0))
        fee_rate = float(risk_plan.get("fee_rate", 0.00075))
        filled_contracts = contracts * (0.5 if fault == "partial" else 1.0)
        if fault == "protection":
            raise ValueError("PROTECTION_REGISTRATION_FAULT_REQUIRES_GATEWAY_RECONCILIATION")

        # This compatibility facade must use the production gateway.  Direct
        # INSERTs here used to bypass risk reservation, account scope,
        # fee/slippage policy, and protection registration.
        gateway = ExecutionGateway(self.store, ledger=self.ledger)
        stop = float(risk_plan.get("stop", proposal.get("stop", 0.0)))
        targets = [float(t) for t in risk_plan.get("targets", proposal.get("targets", []))]
        market_snapshot = {
            "symbol": str(proposal["symbol"]),
            "price": float(current_market_price),
            "last": float(current_market_price),
            "data_as_of": filled_at.isoformat(),
            "received_at": filled_at.isoformat(),
            "fresh": True,
            "market": {
                "contractSize": contract_size,
                "precision": {"amount": 0.001, "price": 0.01},
                "limits": {"amount": {"min": 0.001, "max": 1_000_000.0, "step": 0.001}},
                "taker": fee_rate,
            },
            "fee_rate": fee_rate,
            "slippage": slippage,
        }
        intent = OrderIntent(
            intent_id=identity,
            idempotency_key=f"simulated:{identity}",
            account_id=account_id,
            mode=TradingMode.PAPER,
            environment="PAPER",
            venue="simulated",
            decision_path=DecisionPath.STRATEGY_DRIVEN,
            instrument_id=str(proposal["symbol"]),
            side=side,
            order_type="market",
            quantity=filled_contracts,
            price=float(proposal.get("entry", current_market_price)),
            leverage=int(risk_plan.get("leverage", proposal.get("leverage", 1)) or 1),
            protection_plan=ProtectionPlan(stop_price=stop, take_profit=targets[0] if targets else None),
            position_id=identity,
        )
        receipt = gateway.submit_intent(intent, market_snapshot=market_snapshot)
        if receipt.get("status") != "FILLED":
            raise ValueError(f"SIMULATED_EXECUTION_NOT_FILLED: {receipt.get('status')}")

        position_record = {
            "position_id": receipt.get("position_id") or identity,
            "account_id": account_id,
            "venue": venue,
            "mode": mode,
            "symbol": proposal["symbol"],
            "side": side,
            "status": "OPEN",
            "entry": float(receipt.get("average_price") or current_market_price),
            "stop": stop,
            "targets": targets,
            "contracts": contracts,
            "filled_contracts": filled_contracts,
            "remaining_contracts": filled_contracts,
            "contract_size": contract_size,
            "fee_rate": fee_rate,
            "slippage": slippage,
            "leverage": float(risk_plan.get("leverage", proposal.get("leverage", 1.0))),
            "realized_pnl": -float(receipt.get("fee") or 0),
            "tp1_done": False,
            "created_at": filled_at.isoformat(),
            "updated_at": filled_at.isoformat(),
            "timeline": timeline.to_dict(),
            "fault": fault,
            "execution_receipt": receipt,
        }
        return position_record
