"""Unified RiskEngine - Strict Risk Budgets, Sizing, and Atomic Reservations (R07).

Enforces:
- Trend single risk: max 0.25%
- Counter-trend single risk: max 0.125%
- Portfolio open risk: max 1.0%
- Single instrument/cluster risk: max 0.5%
- Daily circuit breaker: 1.5% daily loss limit halts all new risk proposals
- Downward step rounding without budget overshoot
- Decimal precision for low-price tokens (AT13)
- Elimination of deceptive "AI recommended leverage" (rule-based score only)
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import math
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .ledger import AccountLedger, AccountSnapshot


@dataclass
class RiskLimits:
    trend_risk_fraction: Decimal = Decimal("0.0025")
    counter_risk_fraction: Decimal = Decimal("0.00125")
    max_portfolio_risk: Decimal = Decimal("0.01")
    max_cluster_risk: Decimal = Decimal("0.005")
    daily_circuit_breaker: Decimal = Decimal("0.015")


@dataclass
class RiskReservation:
    reservation_id: str
    account_id: str
    amount_risk: Decimal
    amount_margin: Decimal
    created_at: datetime
    expires_at: Optional[datetime] = None


@dataclass
class RiskDecision:
    decision_id: str
    approved: bool
    reason_code: str
    contracts: Decimal
    notional: Decimal
    risk_amount: Decimal
    allocated_margin: Decimal
    leverage: Decimal
    entry_price: Decimal
    stop_price: Decimal
    targets: List[Decimal]
    expires_at: datetime
    reservation_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "approved": self.approved,
            "reason_code": self.reason_code,
            "contracts": str(self.contracts),
            "notional": str(self.notional),
            "risk_amount": str(self.risk_amount),
            "allocated_margin": str(self.allocated_margin),
            "leverage": str(self.leverage),
            "entry_price": str(self.entry_price),
            "stop_price": str(self.stop_price),
            "targets": [str(t) for t in self.targets],
            "expires_at": self.expires_at.isoformat(),
            "reservation_id": self.reservation_id,
        }


def compute_rule_based_leverage_score(
    strategy_id: str = "",
    confidence_score: float = 0.8,
    entry: float = 0.0,
    stop: float = 0.0,
    atr_val: float = 1.0,
    *,
    distance_to_stop_pct: Optional[float] = None,
    atr_pct: Optional[float] = None,
    trend_aligned: bool = True,
) -> Tuple[int, str]:
    """Rule-based leverage scoring heuristic (R07).
    
    NOTE: This is a heuristic rule score, NOT an empirical or verified AI recommendation.
    Effective real-world leverage is strictly bounded by user authorization and exchange limits.
    """
    if distance_to_stop_pct is not None:
        stop_distance_pct = distance_to_stop_pct
    else:
        stop_distance_pct = abs(entry - stop) / entry if entry > 0 else 0.05

    if stop_distance_pct <= 0.005:
        base_lev = 50.0 if confidence_score >= 0.85 else 30.0
        reason = f"规则计算 (ATR动态评估): 止损距离窄({stop_distance_pct*100:.2f}%)，规则建议中高杠杆"
    elif stop_distance_pct <= 0.015:
        base_lev = 30.0 if confidence_score >= 0.8 else 20.0
        reason = f"规则计算 (ATR动态评估): 日内标准止损带宽({stop_distance_pct*100:.2f}%)，规则建议平衡杠杆"
    elif stop_distance_pct <= 0.03:
        base_lev = 15.0 if confidence_score >= 0.8 else 10.0
        reason = f"规则计算 (ATR动态评估): 趋势通道正常回调保护位({stop_distance_pct*100:.2f}%)，规则建议稳健杠杆"
    else:
        base_lev = 5.0 if confidence_score >= 0.8 else 3.0
        reason = f"规则计算 (ATR动态评估): 逆势或高波动率宽止损({stop_distance_pct*100:.2f}%)，规则建议保守低杠杆"

    if not trend_aligned:
        base_lev = min(base_lev, 10.0)
    if strategy_id == "funding_extreme":
        base_lev = min(10.0, base_lev)

    final_lev = int(max(1, min(100, round(base_lev))))
    return final_lev, reason


class RiskEngine:
    """Evaluates proposals, calculates precision-safe contract quantities, and reserves risk."""

    TREND_RISK_FRACTION = Decimal("0.0025")       # 0.25%
    COUNTER_RISK_FRACTION = Decimal("0.00125")   # 0.125%
    MAX_PORTFOLIO_RISK = Decimal("0.01")         # 1.0%
    MAX_CLUSTER_RISK = Decimal("0.005")          # 0.5%
    DAILY_CIRCUIT_BREAKER = Decimal("0.015")     # 1.5%

    def __init__(self, ledger: Optional[AccountLedger] = None):
        self.ledger = ledger

    def calculate_position_size(
        self,
        account_id: str,
        entry_price: Any,
        stop_price: Any,
        market_spec: Dict[str, Any],
        risk_budget_pct: Any = Decimal("0.0025"),
        leverage: Any = 10,
    ) -> Tuple[Decimal, Decimal, Decimal]:
        """Calculates contracts, required margin, and stop distance using strict step rounding."""
        snapshot = self.ledger.get_snapshot(account_id)
        entry_d = Decimal(str(entry_price))
        stop_d = Decimal(str(stop_price))
        risk_pct_d = Decimal(str(risk_budget_pct))
        lev_d = Decimal(str(leverage))

        stop_dist = abs(entry_d - stop_d)
        if stop_dist <= Decimal("0"):
            return Decimal("0"), Decimal("0"), Decimal("0")

        contract_size = Decimal(str(market_spec.get("contractSize", 1.0)))
        limits = market_spec.get("limits", {}).get("amount", {})
        step = Decimal(str(limits.get("step", 1.0)))
        min_amount = Decimal(str(limits.get("min", step)))
        max_amount = Decimal(str(limits.get("max", Decimal("1000000000"))))

        risk_budget = snapshot.net_equity * risk_pct_d
        risk_per_contract = stop_dist * contract_size
        if risk_per_contract <= Decimal("0"):
            return Decimal("0"), Decimal("0"), stop_dist

        raw_contracts = risk_budget / risk_per_contract
        contracts = (raw_contracts / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step

        if contracts > max_amount:
            contracts = max_amount
        if contracts < min_amount:
            contracts = Decimal("0")

        notional = contracts * contract_size * entry_d
        margin = notional / lev_d if lev_d > Decimal("0") else notional
        return contracts, margin, stop_dist

    def evaluate_proposal(
        self,
        account_id: str,
        proposal: Dict[str, Any],
        market: Dict[str, Any],
        *,
        now: Optional[datetime] = None,
        is_trend: bool = True,
        open_positions: Optional[List[Dict[str, Any]]] = None,
        user_max_leverage: Optional[Decimal] = None,
        ttl_minutes: int = 15,
        requested_contracts: Optional[Any] = None,
        max_single_risk_fraction: Optional[Any] = None,
        max_portfolio_risk_fraction: Optional[Any] = None,
        max_cluster_risk_fraction: Optional[Any] = None,
        max_daily_loss_fraction: Optional[Any] = None,
        exclude_intent_id: Optional[str] = None,
    ) -> RiskDecision:
        now = now or datetime.now(timezone.utc)
        decision_id = str(proposal.get("decision_id") or f"risk_{proposal.get('symbol', 'UNK')}_{uuid.uuid4().hex[:12]}")

        # 1. Check account snapshot & daily circuit breaker
        snapshot = self.ledger.get_snapshot(account_id, open_positions=open_positions, now=now)
        daily_limit_fraction = self.DAILY_CIRCUIT_BREAKER
        if max_daily_loss_fraction is not None:
            try:
                requested_daily_limit = Decimal(str(max_daily_loss_fraction))
            except (InvalidOperation, TypeError, ValueError):
                return RiskDecision(
                    decision_id=decision_id,
                    approved=False,
                    reason_code="AUTHORIZATION_DAILY_LIMIT_INVALID",
                    contracts=Decimal("0"),
                    notional=Decimal("0"),
                    risk_amount=Decimal("0"),
                    allocated_margin=Decimal("0"),
                    leverage=Decimal("1"),
                    entry_price=Decimal("0"),
                    stop_price=Decimal("0"),
                    targets=[],
                    expires_at=now,
                )
            if requested_daily_limit <= 0:
                return RiskDecision(
                    decision_id=decision_id,
                    approved=False,
                    reason_code="AUTHORIZATION_DAILY_LIMIT_INVALID",
                    contracts=Decimal("0"),
                    notional=Decimal("0"),
                    risk_amount=Decimal("0"),
                    allocated_margin=Decimal("0"),
                    leverage=Decimal("1"),
                    entry_price=Decimal("0"),
                    stop_price=Decimal("0"),
                    targets=[],
                    expires_at=now,
                )
            # Authorization can narrow a policy, never widen the product's
            # hard daily circuit breaker.
            daily_limit_fraction = min(daily_limit_fraction, requested_daily_limit)
        if snapshot.daily_loss_limit_reached or (
            snapshot.daily_loss > 0
            and snapshot.daily_loss >= snapshot.initial_deposit * daily_limit_fraction
        ):
            return RiskDecision(
                decision_id=decision_id,
                approved=False,
                reason_code="CIRCUIT_BREAKER_ACTIVE",
                contracts=Decimal("0"),
                notional=Decimal("0"),
                risk_amount=Decimal("0"),
                allocated_margin=Decimal("0"),
                leverage=Decimal("1"),
                entry_price=Decimal("0"),
                stop_price=Decimal("0"),
                targets=[],
                expires_at=now,
            )

        # 2. Check proposal validity
        try:
            entry = Decimal(str(proposal["entry"]))
            stop = Decimal(str(proposal["stop"]))
            targets = [Decimal(str(t)) for t in proposal["targets"]]
            side = str(proposal["side"]).upper()
        except Exception:
            return RiskDecision(
                decision_id=decision_id,
                approved=False,
                reason_code="INVALID_LEVELS",
                contracts=Decimal("0"),
                notional=Decimal("0"),
                risk_amount=Decimal("0"),
                allocated_margin=Decimal("0"),
                leverage=Decimal("1"),
                entry_price=Decimal("0"),
                stop_price=Decimal("0"),
                targets=[],
                expires_at=now,
            )

        # Market boundary & instrument type validation (AT22)
        market_type = str(market.get("market_type", "")).lower()
        contract_type = str(market.get("contract_type", market.get("type", "perp"))).lower()
        symbol = str(proposal.get("symbol", "")).lower()
        if (market_type == "equity" or "equity" in symbol or "nasdaq" in symbol or "nyse" in symbol) and side in ("SHORT", "SELL"):
            if not market.get("allow_borrow_short", False):
                return RiskDecision(
                    decision_id=decision_id,
                    approved=False,
                    reason_code="EQUITY_SHORT_UNAUTHORIZED",
                    contracts=Decimal("0"),
                    notional=Decimal("0"),
                    risk_amount=Decimal("0"),
                    allocated_margin=Decimal("0"),
                    leverage=Decimal("1"),
                    entry_price=entry,
                    stop_price=stop,
                    targets=targets,
                    expires_at=now,
                )

        if contract_type == "spot":
            if side == "SHORT":
                return RiskDecision(
                    decision_id=decision_id,
                    approved=False,
                    reason_code="SPOT_SHORTING_UNSUPPORTED",
                    contracts=Decimal("0"),
                    notional=Decimal("0"),
                    risk_amount=Decimal("0"),
                    allocated_margin=Decimal("0"),
                    leverage=Decimal("1"),
                    entry_price=entry,
                    stop_price=stop,
                    targets=targets,
                    expires_at=now,
                )
            if side in ("SELL", "CLOSE"):
                available_balance = Decimal(str(market.get("available_balance", "0")))
                if available_balance <= Decimal("0"):
                    return RiskDecision(
                        decision_id=decision_id,
                        approved=False,
                        reason_code="SPOT_OVERSELL_EXCEEDED",
                        contracts=Decimal("0"),
                        notional=Decimal("0"),
                        risk_amount=Decimal("0"),
                        allocated_margin=Decimal("0"),
                        leverage=Decimal("1"),
                        entry_price=entry,
                        stop_price=stop,
                        targets=targets,
                        expires_at=now,
                    )

        sign = Decimal("1") if side in ("LONG", "BUY") else Decimal("-1") if side in ("SHORT", "SELL", "CLOSE") else Decimal("0")
        if sign == Decimal("0") or min(entry, stop, *(targets if targets else [entry])) <= Decimal("0"):
            return RiskDecision(
                decision_id=decision_id,
                approved=False,
                reason_code="INVALID_LEVELS",
                contracts=Decimal("0"),
                notional=Decimal("0"),
                risk_amount=Decimal("0"),
                allocated_margin=Decimal("0"),
                leverage=Decimal("1"),
                entry_price=entry,
                stop_price=stop,
                targets=targets,
                expires_at=now,
            )

        if contract_type != "spot" or side not in ("SELL", "CLOSE"):
            if sign * (entry - stop) <= Decimal("0") or any(sign * (t - entry) <= Decimal("0") for t in targets):
                return RiskDecision(
                    decision_id=decision_id,
                    approved=False,
                    reason_code="INVALID_STOP_OR_TARGET_DIRECTION",
                    contracts=Decimal("0"),
                    notional=Decimal("0"),
                    risk_amount=Decimal("0"),
                    allocated_margin=Decimal("0"),
                    leverage=Decimal("1"),
                    entry_price=entry,
                    stop_price=stop,
                    targets=targets,
                    expires_at=now,
                )

        # 3. Market metadata & precision
        try:
            contract_size = Decimal(str(market.get("contractSize", market.get("contract_size", 1.0))))
            precision = market.get("precision", {}) or {}
            limits = market.get("limits", {}).get("amount", {}) or {}
            step = Decimal(str(precision.get("amount", limits.get("step", 1.0))))
            tick = Decimal(str(precision.get("price", market.get("price_tick", 0.01))))
            min_amount = Decimal(str(limits.get("min", step)))
            max_amount = Decimal(str(limits.get("max", Decimal("1000000000"))))
        except Exception:
            return RiskDecision(
                decision_id=decision_id,
                approved=False,
                reason_code="METADATA_MISSING",
                contracts=Decimal("0"),
                notional=Decimal("0"),
                risk_amount=Decimal("0"),
                allocated_margin=Decimal("0"),
                leverage=Decimal("1"),
                entry_price=entry,
                stop_price=stop,
                targets=targets,
                expires_at=now,
            )

        # Precision align prices
        def quantize_price(p: Decimal) -> Decimal:
            # Round with tick precision
            steps = (p / tick).quantize(Decimal("1"), rounding=ROUND_DOWN)
            return steps * tick

        fee_rate = Decimal(str(proposal.get("fee_rate", market.get("taker", 0.00075))))
        slippage = Decimal(str(proposal.get("slippage", 0.001)))

        entry_eff = quantize_price(entry * (Decimal("1") + sign * slippage))
        stop_eff = quantize_price(stop)
        targets_eff = [quantize_price(t) for t in targets]

        if sign * (entry_eff - stop_eff) <= Decimal("0"):
            return RiskDecision(
                decision_id=decision_id,
                approved=False,
                reason_code="ROUNDED_LEVELS_INVALID",
                contracts=Decimal("0"),
                notional=Decimal("0"),
                risk_amount=Decimal("0"),
                allocated_margin=Decimal("0"),
                leverage=Decimal("1"),
                entry_price=entry_eff,
                stop_price=stop_eff,
                targets=targets_eff,
                expires_at=now,
            )

        # 4. Budget determination
        target_fraction = self.TREND_RISK_FRACTION if is_trend else self.COUNTER_RISK_FRACTION
        if max_single_risk_fraction is not None:
            try:
                authorization_fraction = Decimal(str(max_single_risk_fraction))
            except Exception:
                authorization_fraction = Decimal("-1")
            if authorization_fraction <= Decimal("0"):
                return RiskDecision(
                    decision_id=decision_id,
                    approved=False,
                    reason_code="AUTHORIZATION_RISK_LIMIT_INVALID",
                    contracts=Decimal("0"),
                    notional=Decimal("0"),
                    risk_amount=Decimal("0"),
                    allocated_margin=Decimal("0"),
                    leverage=Decimal("1"),
                    entry_price=entry,
                    stop_price=stop,
                    targets=targets,
                    expires_at=now,
                )
            target_fraction = min(target_fraction, authorization_fraction)
        portfolio_limit = self.MAX_PORTFOLIO_RISK
        cluster_limit = self.MAX_CLUSTER_RISK
        try:
            if max_portfolio_risk_fraction is not None:
                portfolio_limit = min(portfolio_limit, Decimal(str(max_portfolio_risk_fraction)))
            if max_cluster_risk_fraction is not None:
                cluster_limit = min(cluster_limit, Decimal(str(max_cluster_risk_fraction)))
        except (InvalidOperation, TypeError, ValueError):
            return RiskDecision(
                decision_id=decision_id,
                approved=False,
                reason_code="AUTHORIZATION_PORTFOLIO_LIMIT_INVALID",
                contracts=Decimal("0"),
                notional=Decimal("0"),
                risk_amount=Decimal("0"),
                allocated_margin=Decimal("0"),
                leverage=Decimal("1"),
                entry_price=entry,
                stop_price=stop,
                targets=targets,
                expires_at=now,
            )
        if portfolio_limit <= 0 or cluster_limit <= 0:
            return RiskDecision(
                decision_id=decision_id,
                approved=False,
                reason_code="AUTHORIZATION_PORTFOLIO_LIMIT_INVALID",
                contracts=Decimal("0"),
                notional=Decimal("0"),
                risk_amount=Decimal("0"),
                allocated_margin=Decimal("0"),
                leverage=Decimal("1"),
                entry_price=entry,
                stop_price=stop,
                targets=targets,
                expires_at=now,
            )
        risk_budget = snapshot.net_equity * target_fraction

        # Unit risk = (stop distance + adverse stop slippage + entry and exit fees) * contract_size
        unit_risk = (abs(entry_eff - stop_eff) + stop_eff * slippage + (entry_eff + stop_eff) * fee_rate) * contract_size
        if unit_risk <= Decimal("0"):
            return RiskDecision(
                decision_id=decision_id,
                approved=False,
                reason_code="ZERO_UNIT_RISK",
                contracts=Decimal("0"),
                notional=Decimal("0"),
                risk_amount=Decimal("0"),
                allocated_margin=Decimal("0"),
                leverage=Decimal("1"),
                entry_price=entry_eff,
                stop_price=stop_eff,
                targets=targets_eff,
                expires_at=now,
            )

        raw_contracts = risk_budget / unit_risk
        if requested_contracts is not None:
            try:
                requested_dec = Decimal(str(requested_contracts))
            except Exception:
                requested_dec = Decimal("-1")
            if requested_dec <= 0:
                return RiskDecision(
                    decision_id=decision_id,
                    approved=False,
                    reason_code="INVALID_QUANTITY",
                    contracts=Decimal("0"),
                    notional=Decimal("0"),
                    risk_amount=Decimal("0"),
                    allocated_margin=Decimal("0"),
                    leverage=Decimal("1"),
                    entry_price=entry_eff,
                    stop_price=stop_eff,
                    targets=targets_eff,
                    expires_at=now,
                )
            raw_contracts = min(raw_contracts, requested_dec)

        # Round down to step
        # For multi-target staged exits (e.g. 50/50), use 2*step for cleanly split exit sizes
        step_factor = Decimal("2") * step if len(targets) > 1 else step
        steps_count = (raw_contracts / step_factor).quantize(Decimal("1"), rounding=ROUND_DOWN)
        contracts = steps_count * step_factor

        # 5. Check constraints & budget overshoot
        actual_risk = contracts * unit_risk
        if actual_risk > risk_budget:
            return RiskDecision(
                decision_id=decision_id,
                approved=False,
                reason_code="RISK_BUDGET_EXCEEDED",
                contracts=Decimal("0"),
                notional=Decimal("0"),
                risk_amount=Decimal("0"),
                allocated_margin=Decimal("0"),
                leverage=Decimal("1"),
                entry_price=entry_eff,
                stop_price=stop_eff,
                targets=targets_eff,
                expires_at=now,
            )

        if contracts < min_amount or contracts <= Decimal("0"):
            return RiskDecision(
                decision_id=decision_id,
                approved=False,
                reason_code="SIZE_LIMIT",
                contracts=Decimal("0"),
                notional=Decimal("0"),
                risk_amount=Decimal("0"),
                allocated_margin=Decimal("0"),
                leverage=Decimal("1"),
                entry_price=entry_eff,
                stop_price=stop_eff,
                targets=targets_eff,
                expires_at=now,
            )

        if contracts > max_amount:
            contracts = max_amount

        notional = contracts * contract_size * entry_eff

        # Leverage
        rule_lev, _ = compute_rule_based_leverage_score(
            proposal.get("strategy_id", ""),
            float(proposal.get("confidence", 0.8)),
            float(entry_eff),
            float(stop_eff),
        )
        effective_leverage = Decimal(str(rule_lev))
        if user_max_leverage and user_max_leverage > Decimal("0"):
            effective_leverage = min(effective_leverage, user_max_leverage)

        allocated_margin = notional / effective_leverage if effective_leverage > Decimal("0") else notional
        if allocated_margin > snapshot.cash:
            return RiskDecision(
                decision_id=decision_id,
                approved=False,
                reason_code="INSUFFICIENT_MARGIN",
                contracts=Decimal("0"),
                notional=Decimal("0"),
                risk_amount=Decimal("0"),
                allocated_margin=Decimal("0"),
                leverage=effective_leverage,
                entry_price=entry_eff,
                stop_price=stop_eff,
                targets=targets_eff,
                expires_at=now,
            )

        # Portfolio and same-instrument cluster limits are evaluated against
        # the account-scoped, manageable positions supplied by the ledger.
        scope_mode = proposal.get("mode")
        scope_venue = proposal.get("venue")
        if open_positions is None:
            scoped_positions = self.ledger.get_open_positions(
                account_id,
                venue=str(scope_venue) if scope_venue else None,
                mode=str(scope_mode) if scope_mode else None,
            )
        else:
            scoped_positions = []
            for position in open_positions:
                if not isinstance(position, dict):
                    continue
                if str(position.get("account_id") or "") != str(account_id):
                    continue
                if int(position.get("legacy_unverified", 0) or 0):
                    continue
                if scope_mode and str(position.get("mode") or "").upper() != str(scope_mode).upper():
                    continue
                if scope_venue and str(position.get("venue") or "").lower() != str(scope_venue).lower():
                    continue
                scoped_positions.append(position)
        existing_risk = Decimal("0")
        existing_cluster_risk = Decimal("0")
        symbol_key = str(proposal.get("symbol", "")).upper()
        for position in scoped_positions:
            try:
                remaining = Decimal(str(position.get("remaining_contracts", position.get("contracts", 0))))
                position_entry = Decimal(str(position.get("entry", position.get("entry_price", 0))))
                position_stop = Decimal(str(position.get("stop", position.get("stop_loss", 0))))
                position_contract = Decimal(str(position.get("contract_size", 1)))
                if position_stop <= Decimal("0"):
                    position_risk = position_entry * Decimal("0.05") * remaining * position_contract
                else:
                    position_risk = max(Decimal("0"), abs(position_entry - position_stop) * remaining * position_contract)
            except Exception:
                continue
            existing_risk += position_risk
            if str(position.get("symbol", position.get("instrument_id", ""))).upper() == symbol_key:
                existing_cluster_risk += position_risk
        if existing_risk + actual_risk > snapshot.net_equity * portfolio_limit:
            return RiskDecision(
                decision_id=decision_id,
                approved=False,
                reason_code="PORTFOLIO_RISK_EXCEEDED",
                contracts=Decimal("0"),
                notional=Decimal("0"),
                risk_amount=Decimal("0"),
                allocated_margin=Decimal("0"),
                leverage=effective_leverage,
                entry_price=entry_eff,
                stop_price=stop_eff,
                targets=targets_eff,
                expires_at=now,
            )
        if existing_cluster_risk + actual_risk > snapshot.net_equity * cluster_limit:
            return RiskDecision(
                decision_id=decision_id,
                approved=False,
                reason_code="CLUSTER_RISK_EXCEEDED",
                contracts=Decimal("0"),
                notional=Decimal("0"),
                risk_amount=Decimal("0"),
                allocated_margin=Decimal("0"),
                leverage=effective_leverage,
                entry_price=entry_eff,
                stop_price=stop_eff,
                targets=targets_eff,
                expires_at=now,
            )

        # 6. Reserve risk atomically
        expires_at = now + timedelta(minutes=ttl_minutes)
        res_id = f"res_{decision_id}"
        reserved = self.ledger.reserve_risk(
            account_id=account_id,
            reservation_id=res_id,
            amount_risk=actual_risk,
            amount_margin=allocated_margin,
            expires_at=expires_at,
            now=now,
            instrument_id=str(proposal.get("symbol", "")) or None,
            venue=str(scope_venue) if scope_venue else None,
            mode=str(scope_mode).upper() if scope_mode else None,
            max_single_risk_fraction=target_fraction,
            max_portfolio_risk_fraction=portfolio_limit,
            max_cluster_risk_fraction=cluster_limit,
            max_daily_loss_fraction=daily_limit_fraction,
            exclude_intent_id=exclude_intent_id,
        )
        if not reserved:
            return RiskDecision(
                decision_id=decision_id,
                approved=False,
                reason_code="RISK_RESERVATION_FAILED",
                contracts=Decimal("0"),
                notional=Decimal("0"),
                risk_amount=Decimal("0"),
                allocated_margin=Decimal("0"),
                leverage=effective_leverage,
                entry_price=entry_eff,
                stop_price=stop_eff,
                targets=targets_eff,
                expires_at=now,
            )

        return RiskDecision(
            decision_id=decision_id,
            approved=True,
            reason_code="APPROVED",
            contracts=contracts,
            notional=notional,
            risk_amount=actual_risk,
            allocated_margin=allocated_margin,
            leverage=effective_leverage,
            entry_price=entry_eff,
            stop_price=stop_eff,
            targets=targets_eff,
            expires_at=expires_at,
            reservation_id=res_id,
        )

    def evaluate_intent(
        self,
        intent: Any,
        market_snapshot: Dict[str, Any],
        *,
        now: Optional[datetime] = None,
        open_positions: Optional[List[Dict[str, Any]]] = None,
        max_single_risk_fraction: Optional[Any] = None,
        max_portfolio_risk_fraction: Optional[Any] = None,
        max_cluster_risk_fraction: Optional[Any] = None,
        max_daily_loss_fraction: Optional[Any] = None,
    ) -> RiskDecision:
        """Run the complete risk contract for a gateway opening intent.

        The gateway must provide a fresh executable price.  This method never
        reads an intent price as a market-data substitute and applies the
        same fee, slippage, contract-size, step, margin, portfolio, cluster,
        daily-loss, and atomic reservation checks used by strategy proposals.
        """
        now = now or datetime.now(timezone.utc)
        snapshot = dict(market_snapshot or {})
        if snapshot.get("fresh") is not True or snapshot.get("stale") is True or snapshot.get("executable") is False:
            return RiskDecision(
                decision_id=f"risk_{getattr(intent, 'intent_id', uuid.uuid4().hex)}",
                approved=False,
                reason_code="MARKET_DATA_UNAVAILABLE",
                contracts=Decimal("0"),
                notional=Decimal("0"),
                risk_amount=Decimal("0"),
                allocated_margin=Decimal("0"),
                leverage=Decimal("1"),
                entry_price=Decimal("0"),
                stop_price=Decimal("0"),
                targets=[],
                expires_at=now,
            )
        freshness_status = str(snapshot.get("freshness_status", "fresh")).upper()
        if freshness_status in {"STALE", "DEGRADED", "UNKNOWN", "UNAVAILABLE"}:
            return RiskDecision(
                decision_id=f"risk_{getattr(intent, 'intent_id', uuid.uuid4().hex)}",
                approved=False,
                reason_code="MARKET_DATA_UNAVAILABLE",
                contracts=Decimal("0"),
                notional=Decimal("0"),
                risk_amount=Decimal("0"),
                allocated_margin=Decimal("0"),
                leverage=Decimal("1"),
                entry_price=Decimal("0"),
                stop_price=Decimal("0"),
                targets=[],
                expires_at=now,
            )
        market_reference = snapshot.get("data_as_of") or snapshot.get("timestamp") or snapshot.get("bar_end")
        try:
            if isinstance(market_reference, datetime):
                reference = market_reference
            else:
                reference = datetime.fromisoformat(str(market_reference).replace("Z", "+00:00"))
            reference = reference.replace(tzinfo=timezone.utc) if reference.tzinfo is None else reference.astimezone(timezone.utc)
            max_age_value = snapshot["stale_after_seconds"] if "stale_after_seconds" in snapshot else 120
            max_age = float(max_age_value)
            if not math.isfinite(max_age) or max_age <= 0:
                raise ValueError
            max_age = min(max_age, 120.0)
            age = (now - reference).total_seconds()
            if age < -5.0 or age > max_age:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return RiskDecision(
                decision_id=f"risk_{getattr(intent, 'intent_id', uuid.uuid4().hex)}",
                approved=False,
                reason_code="MARKET_DATA_UNAVAILABLE",
                contracts=Decimal("0"),
                notional=Decimal("0"),
                risk_amount=Decimal("0"),
                allocated_margin=Decimal("0"),
                leverage=Decimal("1"),
                entry_price=Decimal("0"),
                stop_price=Decimal("0"),
                targets=[],
                expires_at=now,
            )
        price_value = snapshot.get("price", snapshot.get("last"))
        if price_value is None:
            return RiskDecision(
                decision_id=f"risk_{getattr(intent, 'intent_id', uuid.uuid4().hex)}",
                approved=False,
                reason_code="MARKET_DATA_UNAVAILABLE",
                contracts=Decimal("0"),
                notional=Decimal("0"),
                risk_amount=Decimal("0"),
                allocated_margin=Decimal("0"),
                leverage=Decimal("1"),
                entry_price=Decimal("0"),
                stop_price=Decimal("0"),
                targets=[],
                expires_at=now,
            )
        try:
            price = Decimal(str(price_value))
            if not price.is_finite() or price <= 0:
                raise ValueError
        except Exception:
            return RiskDecision(
                decision_id=f"risk_{getattr(intent, 'intent_id', uuid.uuid4().hex)}",
                approved=False,
                reason_code="MARKET_DATA_UNAVAILABLE",
                contracts=Decimal("0"),
                notional=Decimal("0"),
                risk_amount=Decimal("0"),
                allocated_margin=Decimal("0"),
                leverage=Decimal("1"),
                entry_price=Decimal("0"),
                stop_price=Decimal("0"),
                targets=[],
                expires_at=now,
            )
        plan = getattr(intent, "protection_plan", None)
        stop_value = getattr(plan, "stop_price", None)
        side = str(getattr(intent, "side", "")).upper()
        direction = "LONG" if side in ("LONG", "BUY") else "SHORT" if side in ("SHORT", "SELL") else side
        if stop_value is None:
            return RiskDecision(
                decision_id=f"risk_{getattr(intent, 'intent_id', uuid.uuid4().hex)}",
                approved=False,
                reason_code="INVALID_LEVELS",
                contracts=Decimal("0"),
                notional=Decimal("0"),
                risk_amount=Decimal("0"),
                allocated_margin=Decimal("0"),
                leverage=Decimal("1"),
                entry_price=price,
                stop_price=Decimal("0"),
                targets=[],
                expires_at=now,
            )
        take_profit = getattr(plan, "take_profit", None)
        if take_profit is None:
            take_profit = price * (Decimal("1.02") if direction == "LONG" else Decimal("0.98"))
        market = dict(snapshot.get("market") or snapshot.get("metadata") or {})
        market.setdefault("contractSize", snapshot.get("contractSize", snapshot.get("contract_size", 1)))
        market.setdefault("precision", snapshot.get("precision", {"amount": snapshot.get("step", 0.001), "price": snapshot.get("tick", 0.01)}))
        limits = dict(market.get("limits", {}).get("amount", {}) or {})
        precision = market.get("precision", {}) or {}
        step = precision.get("amount", snapshot.get("step", 0.001))
        limits.setdefault("step", step)
        limits.setdefault("min", snapshot.get("min_quantity", step))
        limits.setdefault("max", snapshot.get("max_quantity", 1000000000))
        market["limits"] = {**(market.get("limits", {}) or {}), "amount": limits}
        market.setdefault("taker", snapshot.get("taker_fee", 0.0005))
        proposal = {
            "decision_id": f"{getattr(intent, 'intent_id', uuid.uuid4().hex)}",
            "symbol": str(getattr(intent, "instrument_id", "")),
            "side": direction,
            "entry": price,
            "stop": stop_value,
            "targets": [take_profit],
            "fee_rate": snapshot.get("fee_rate", market.get("taker", 0.0005)),
            "slippage": snapshot.get("slippage", 0.001),
            "strategy_id": getattr(getattr(intent, "decision_path", None), "value", "gateway"),
            "confidence": 1.0,
            "venue": str(getattr(intent, "venue", "simulated") or "simulated"),
            "mode": str(getattr(getattr(intent, "mode", None), "value", getattr(intent, "mode", "PAPER"))).upper(),
        }
        decision = self.evaluate_proposal(
            account_id=str(getattr(intent, "account_id", "")),
            proposal=proposal,
            market=market,
            now=now,
            is_trend=True,
            open_positions=open_positions,
            user_max_leverage=Decimal(str(getattr(intent, "leverage", None) or 100)),
            requested_contracts=getattr(intent, "quantity", None),
            max_single_risk_fraction=max_single_risk_fraction,
            max_portfolio_risk_fraction=max_portfolio_risk_fraction,
            max_cluster_risk_fraction=max_cluster_risk_fraction,
            max_daily_loss_fraction=max_daily_loss_fraction,
            exclude_intent_id=str(getattr(intent, "intent_id", "")) or None,
        )
        requested = Decimal(str(getattr(intent, "quantity", 0)))
        if decision.approved and requested > decision.contracts:
            if decision.reservation_id:
                self.ledger.release_risk(str(getattr(intent, "account_id", "")), decision.reservation_id)
            return replace(decision, approved=False, reason_code="RISK_BUDGET_EXCEEDED", contracts=Decimal("0"), risk_amount=Decimal("0"), notional=Decimal("0"), allocated_margin=Decimal("0"), reservation_id=None)
        return decision

    def get_risk_summary(
        self,
        account_id: str,
        now: Optional[datetime] = None,
        *,
        open_positions: Optional[list[dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Produce a comprehensive summary of account risk limits and utilization."""
        snapshot = self.ledger.get_snapshot(account_id, open_positions=open_positions, now=now)
        max_budget = round(snapshot.net_equity * self.MAX_PORTFOLIO_RISK, 4)
        return {
            "account_id": account_id,
            "net_equity": float(snapshot.net_equity),
            "max_portfolio_risk_budget": float(max_budget),
            "circuit_breaker_active": snapshot.daily_loss_limit_reached,
            "cash": str(snapshot.cash),
            "allocated_margin": str(snapshot.allocated_margin),
            "reserved_risk": str(snapshot.reserved_risk),
            "max_portfolio_risk": str(snapshot.net_equity * self.MAX_PORTFOLIO_RISK),
            "daily_loss": str(snapshot.daily_loss),
            "daily_loss_limit": str(snapshot.initial_deposit * self.DAILY_CIRCUIT_BREAKER),
            "daily_loss_limit_reached": snapshot.daily_loss_limit_reached,
            "unverified_protection_count": snapshot.unverified_protection_count,
            "new_risk_blocked": bool(snapshot.unverified_protection_count or snapshot.unvalued_fee_events or snapshot.daily_loss_limit_reached),
            "new_risk_block_reasons": [
                *(["PROTECTION_UNVERIFIED"] if snapshot.unverified_protection_count else []),
                *(["FEE_UNVALUED"] if snapshot.unvalued_fee_events else []),
                *(["DAILY_LOSS_LIMIT"] if snapshot.daily_loss_limit_reached else []),
            ],
            "risk_limits": {
                "trend_risk_fraction": str(self.TREND_RISK_FRACTION),
                "counter_risk_fraction": str(self.COUNTER_RISK_FRACTION),
                "max_portfolio_risk": str(self.MAX_PORTFOLIO_RISK),
                "max_cluster_risk": str(self.MAX_CLUSTER_RISK),
                "daily_circuit_breaker": str(self.DAILY_CIRCUIT_BREAKER),
            },
        }

    def reserve_risk(
        self,
        account_id: str,
        instrument_id: str,
        amount_risk: Decimal,
        reservation_id: str,
        *,
        exclude_intent_id: Optional[str] = None,
    ) -> Any:
        """Atomically reserve risk budget for an order (repair-plan B3, RT14)."""
        try:
            amount = Decimal(str(amount_risk))
        except Exception:
            amount = Decimal("-1")
        if amount <= 0:
            return type("RiskResResult", (), {"is_approved": False, "rejection_code": "INVALID_RISK_AMOUNT"})()
        # AccountLedger owns the transaction and rechecks wallet, margin,
        # open-position allocation, daily loss and existing reservations under
        # BEGIN IMMEDIATE.  A pre-read snapshot here would be stale and could
        # disagree with the unified gateway path under concurrency.
        ok = self.ledger.reserve_risk(
            account_id=account_id,
            reservation_id=reservation_id,
            amount_risk=amount,
            amount_margin=amount,
            instrument_id=instrument_id or None,
            exclude_intent_id=exclude_intent_id,
        )
        return type("RiskResResult", (), {"is_approved": ok, "rejection_code": "OK" if ok else "INSUFFICIENT_MARGIN"})()

    def validate_intent(
        self,
        intent: Any,
        limits: Optional[RiskLimits] = None,
        market_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Validate an intent through the same fresh-market path as Gateway.

        The former implementation used ``intent.price`` (or a hard-coded
        50,000 fallback), omitted fees/slippage/step/margin/portfolio checks,
        and therefore allowed a direct caller to disagree with the gateway.
        Direct validation now requires the caller to provide the executable
        market snapshot and delegates to ``evaluate_intent``.  ``limits`` is
        retained for API compatibility; the unified engine's hard limits are
        authoritative.
        """
        del limits
        if not market_snapshot:
            return type(
                "RiskValidationResult",
                (),
                {"is_approved": False, "rejection_code": "MARKET_DATA_UNAVAILABLE"},
            )()
        decision = self.evaluate_intent(intent, market_snapshot)
        return type(
            "RiskValidationResult",
            (),
            {"is_approved": decision.approved, "rejection_code": decision.reason_code},
        )()
