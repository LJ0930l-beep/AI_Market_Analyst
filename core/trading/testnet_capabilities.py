"""Testnet Protocol Capabilities and Fee Reconciliation Service (AT39).

Verifies exchange protocol-level features:
1. Native vs synthetic TP/SL capability checks.
2. Taker/Maker fee reconciliation.
3. 8-hour perpetual funding payment calculations and ledger reconciliation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional


@dataclass
class TestnetCapabilities:
    venue: str = "gate"
    has_native_tpsl: Optional[bool] = None
    supports_batch_orders: Optional[bool] = None
    supports_amend: Optional[bool] = None
    funding_interval_hours: Optional[int] = None
    rate_limit_per_minute: Optional[int] = None
    fee_schedule: Dict[str, float] = field(default_factory=dict)
    min_order_sizes: Dict[str, float] = field(default_factory=dict)
    status: str = "NOT_RUN"
    source: str = "NOT_RUN_NO_ADAPTER"
    observed_at: Optional[str] = None
    valid_until: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class TestnetCapabilityService:
    __test__ = False  # Prevent pytest from attempting to collect this class as a test suite

    def __init__(self, venue: str = "gate", adapter: Any = None):
        self.venue = venue
        self.adapter = adapter
        self._capabilities = TestnetCapabilities(venue=venue)

    def probe_capabilities(self, adapter: Any = None) -> TestnetCapabilities:
        """Probe an adapter when present; otherwise report an unverified declaration."""
        selected = adapter or self.adapter
        if selected is not None:
            try:
                probe = getattr(selected, "probe_capabilities", None) or getattr(selected, "get_capabilities", None)
                observed = probe() if callable(probe) else getattr(selected, "capabilities", None)
                if isinstance(observed, dict):
                    values = self._capabilities.to_dict()
                    values.update({key: value for key, value in observed.items() if key in values})
                    values["venue"] = self.venue
                    required = {"has_native_tpsl", "supports_batch_orders", "supports_amend", "funding_interval_hours", "fee_schedule"}
                    complete = required.issubset(observed) and isinstance(observed.get("fee_schedule"), dict)
                    values["status"] = "AVAILABLE" if complete else "DEGRADED"
                    values["source"] = "ADAPTER_PROBE" if complete else "ADAPTER_PROBE_INCOMPLETE"
                    observed_at = datetime.now(timezone.utc)
                    values["observed_at"] = observed_at.isoformat()
                    values["valid_until"] = (observed_at + timedelta(minutes=5)).isoformat() if complete else None
                    values["evidence"] = {
                        "adapter": selected.__class__.__name__,
                        "probe": "adapter_capabilities",
                        "required_fields_present": sorted(required.intersection(observed)),
                    }
                    self._capabilities = TestnetCapabilities(**values)
            except Exception as exc:
                self._capabilities.status = "DEGRADED"
                self._capabilities.source = "ADAPTER_PROBE_ERROR"
                self._capabilities.observed_at = datetime.now(timezone.utc).isoformat()
                self._capabilities.valid_until = None
                self._capabilities.evidence = {"error": str(exc)[:240]}
        return self._capabilities

    def get_capabilities(self, venue: str = "gate", symbol: str = "BTC_USDT") -> Dict[str, Any]:
        """Returns capability metadata matching AT39 contract."""
        caps = self.probe_capabilities()
        return {
            "venue": venue,
            "symbol": symbol,
            "has_native_tpsl": caps.has_native_tpsl,
            "fee_schedule": dict(caps.fee_schedule),
            "funding_interval_hours": caps.funding_interval_hours,
            "supports_batch_orders": caps.supports_batch_orders,
            "supports_amend": caps.supports_amend,
            "status": caps.status,
            "source": caps.source,
            "observed_at": caps.observed_at,
            "valid_until": caps.valid_until,
            "evidence": dict(caps.evidence),
            "capability_claim_valid": (
                caps.status == "AVAILABLE"
                and caps.observed_at is not None
                and caps.valid_until is not None
                and datetime.now(timezone.utc) <= datetime.fromisoformat(caps.valid_until)
            ),
        }

    def reconcile_trade_fee(
        self,
        venue: str = "gate",
        notional: Any = Decimal("0"),
        is_maker: bool = False,
    ) -> Dict[str, Any]:
        """Reconcile trade fee against venue schedule (AT39)."""
        schedule = self._capabilities.fee_schedule
        rate = Decimal(str(schedule.get("maker" if is_maker else "taker", 0.0002 if is_maker else 0.0005)))
        notional_dec = Decimal(str(notional))
        expected_fee = (notional_dec * rate).quantize(Decimal("0.01"))
        return {
            "venue": venue,
            "notional": notional_dec,
            "is_maker": is_maker,
            "expected_fee": expected_fee,
            "fee_currency": "USDT",
            "status": "VERIFIED" if self._capabilities.status == "AVAILABLE" else "UNVERIFIED",
            "source": self._capabilities.source,
        }

    def reconcile_funding_payment(
        self,
        position_notional: Any,
        funding_rate: Any,
    ) -> Dict[str, Any]:
        """Reconcile 8-hour perpetual funding payment (AT39)."""
        notional_dec = Decimal(str(position_notional))
        rate_dec = Decimal(str(funding_rate))
        payment = (notional_dec * rate_dec).quantize(Decimal("0.01"))
        return {
            "funding_payment": payment,
            "interval_hours": self._capabilities.funding_interval_hours,
        }

    def verify_order_protection(self, instrument_id: str, stop_price: Optional[float], take_profit: Optional[float]) -> Dict[str, Any]:
        """Verify whether protection can be natively attached or requires synthetic PositionGuardian (AT39)."""
        caps = self.probe_capabilities()
        if stop_price is None or stop_price <= 0:
            return {
                "valid": False,
                "mode": "NONE",
                "error": "Missing valid stop_price for protection plan",
            }

        if caps.status != "AVAILABLE":
            return {
                "valid": False,
                "mode": "UNVERIFIED",
                "instrument_id": instrument_id,
                "error": "TESTNET_CAPABILITY_UNVERIFIED",
                "status": caps.status,
                "source": caps.source,
                "observed_at": caps.observed_at,
                "valid_until": caps.valid_until,
            }

        if caps.has_native_tpsl is True:
            return {
                "valid": True,
                "mode": "NATIVE_TPSL",
                "instrument_id": instrument_id,
                "stop_price": stop_price,
                "take_profit": take_profit,
                "execution_policy": "EXCHANGE_CONDITIONAL_ORDER",
            }
        else:
            return {
                "valid": True,
                "mode": "SYNTHETIC_GUARDIAN",
                "instrument_id": instrument_id,
                "stop_price": stop_price,
                "take_profit": take_profit,
                "execution_policy": "LOCAL_GUARDIAN_MONITORING",
            }

    def reconcile_fill_fees(
        self,
        notional: float,
        is_maker: bool = False,
        reported_fee: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Verify and reconcile fill transaction fees against venue schedule."""
        caps = self.probe_capabilities()
        rate_value = caps.fee_schedule.get("maker" if is_maker else "taker")
        if rate_value is None:
            return {
                "fee_matched": False,
                "rate": None,
                "notional": notional,
                "expected_fee": None,
                "reported_fee": reported_fee,
                "fee_currency": "USDT",
                "status": "UNVERIFIED",
                "source": caps.source,
                "observed_at": caps.observed_at,
            }
        rate = rate_value
        expected_fee = round(notional * rate, 6)

        if reported_fee is not None:
            discrepancy = abs(reported_fee - expected_fee)
            fee_matched = discrepancy < 1e-4
            reconciliation_status = "MATCHED" if fee_matched else "MISMATCH"
        else:
            # Expected schedule is not evidence of a venue-reported fee.
            fee_matched = False
            reconciliation_status = "UNVERIFIED"

        return {
            "fee_matched": fee_matched,
            "rate": rate,
            "notional": notional,
            "expected_fee": expected_fee,
            "reported_fee": reported_fee,
            "fee_currency": "USDT",
            "status": reconciliation_status,
            "source": caps.source,
            "observed_at": caps.observed_at,
        }

    def reconcile_funding_fee(
        self,
        position_size: float,
        mark_price: float,
        funding_rate: float,
    ) -> Dict[str, Any]:
        """Reconcile 8-hour perpetual funding payment on Testnet."""
        notional = position_size * mark_price
        funding_amount = round(notional * funding_rate, 6)
        return {
            "position_size": position_size,
            "mark_price": mark_price,
            "notional": notional,
            "funding_rate": funding_rate,
            "funding_amount": funding_amount,
            "is_outflow": funding_amount > 0,
        }
