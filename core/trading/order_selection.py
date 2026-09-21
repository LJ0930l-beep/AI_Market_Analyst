"""Deterministic AI entry-order selection.

The language model can state a preference, but it cannot decide whether a
market order is safe.  This policy consumes timestamped quote/depth evidence,
applies the same Decimal rounding on every caller, and returns an auditable
decision before the gateway sends anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import math
from typing import Any


ORDER_SELECTION_POLICY_VERSION = "order_selection_v1"
MIN_LIMIT_TTL_SECONDS = 60
MAX_LIMIT_TTL_SECONDS = 1800
DEFAULT_LIMIT_TTL_SECONDS = 900


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        point = value
    elif value:
        try:
            point = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    return point.replace(tzinfo=timezone.utc) if point.tzinfo is None else point.astimezone(timezone.utc)


def _quantize_directional(value: Decimal, tick: Decimal, side: str) -> Decimal:
    units = value / tick
    # A buy/long limit must not be rounded above the requested level; a
    # sell/short limit must not be rounded below it.
    rounding = ROUND_FLOOR if side in {"LONG", "BUY"} else ROUND_CEILING
    return units.to_integral_value(rounding=rounding) * tick


@dataclass(frozen=True)
class OrderSelectionInput:
    side: str
    preference: str = "AUTO"
    quote: Any = None
    bid: Any = None
    ask: Any = None
    limit_price: Any = None
    entry_zone_low: Any = None
    entry_zone_high: Any = None
    signal_at: Any = None
    now: Any = None
    max_signal_age_seconds: float = 120.0
    max_spread_bps: float = 15.0
    max_slippage_bps: float = 10.0
    slippage_bps: Any = None
    liquidity_ok: bool | None = None
    tick_size: Any = None
    ttl_seconds: int = DEFAULT_LIMIT_TTL_SECONDS
    pending_candidate: bool = False
    market_is_remote: bool = False
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class OrderSelectionDecision:
    order_type: str | None
    preference: str
    limit_price: float | None
    ttl_seconds: int
    expires_at: str | None
    reason_code: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.order_type is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_type": self.order_type,
            "preference": self.preference,
            "limit_price": self.limit_price,
            "ttl_seconds": self.ttl_seconds,
            "expires_at": self.expires_at,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "evidence": self.evidence,
            "policy_version": ORDER_SELECTION_POLICY_VERSION,
        }


class OrderSelectionPolicy:
    """Select MARKET/LIMIT with no random or model-dependent branch."""

    @staticmethod
    def select(request: OrderSelectionInput) -> OrderSelectionDecision:
        side = str(request.side or "").strip().upper()
        preference = str(request.preference or "AUTO").strip().upper()
        if side not in {"LONG", "SHORT", "BUY", "SELL"}:
            return OrderSelectionDecision(None, preference, None, DEFAULT_LIMIT_TTL_SECONDS, None, "INVALID_SIDE", "方向不受支持。")
        if preference not in {"MARKET", "LIMIT", "AUTO"}:
            return OrderSelectionDecision(None, preference, None, DEFAULT_LIMIT_TTL_SECONDS, None, "INVALID_ORDER_PREFERENCE", "订单偏好不受支持。")

        now = _time(request.now) or datetime.now(timezone.utc)
        signal_at = _time(request.signal_at)
        age: float | None = None
        if signal_at is not None:
            age = (now - signal_at).total_seconds()
        quote = _decimal(request.quote)
        bid = _decimal(request.bid)
        ask = _decimal(request.ask)
        if not request.market_is_remote and quote is not None and quote > 0 and bid is None and ask is None:
            # The local simulator has a single deterministic quote rather
            # than an exchange order book.  This is an explicit local
            # contract, not a remote market-depth claim.
            bid = ask = quote
        tick = _decimal(request.tick_size)
        evidence: dict[str, Any] = {
            "quote": float(quote) if quote is not None else None,
            "bid": float(bid) if bid is not None else None,
            "ask": float(ask) if ask is not None else None,
            "signal_age_seconds": age,
            "max_signal_age_seconds": request.max_signal_age_seconds,
            "max_spread_bps": request.max_spread_bps,
            "liquidity_ok": request.liquidity_ok,
            "market_is_remote": request.market_is_remote,
            "evidence_refs": list(request.evidence_refs),
        }

        fresh = age is not None and 0 <= age <= float(request.max_signal_age_seconds)
        book_valid = bid is not None and ask is not None and bid > 0 and ask >= bid
        spread_bps: float | None = None
        if book_valid:
            midpoint = (bid + ask) / Decimal("2")
            spread_bps = float((ask - bid) / midpoint * Decimal("10000")) if midpoint > 0 else None
        spread_ok = spread_bps is not None and math.isfinite(spread_bps) and spread_bps <= float(request.max_spread_bps)
        slippage = _decimal(request.slippage_bps)
        slippage_ok = slippage is not None and slippage >= 0 and slippage <= Decimal(str(request.max_slippage_bps))
        zone_low = _decimal(request.entry_zone_low)
        zone_high = _decimal(request.entry_zone_high)
        if zone_low is None and zone_high is None:
            zone_ok = quote is not None and quote > 0
        else:
            if zone_low is None:
                zone_low = zone_high
            if zone_high is None:
                zone_high = zone_low
            zone_ok = quote is not None and zone_low <= quote <= zone_high
        depth_ok = request.liquidity_ok is True or (not request.market_is_remote and request.liquidity_ok is None)
        market_ok = bool(fresh and book_valid and spread_ok and slippage_ok and zone_ok and depth_ok)
        evidence.update({"spread_bps": spread_bps, "slippage_bps": float(slippage) if slippage is not None else None, "fresh": fresh, "spread_ok": spread_ok, "slippage_ok": slippage_ok, "zone_ok": zone_ok, "depth_ok": depth_ok})

        ttl = max(MIN_LIMIT_TTL_SECONDS, min(int(request.ttl_seconds or DEFAULT_LIMIT_TTL_SECONDS), MAX_LIMIT_TTL_SECONDS))
        requested_limit = _decimal(request.limit_price)
        if requested_limit is None:
            requested_limit = _decimal(request.quote)
        if requested_limit is None and book_valid:
            requested_limit = bid if side in {"LONG", "BUY"} else ask
        limit_value: Decimal | None = None
        if requested_limit is not None and requested_limit > 0 and tick is not None and tick > 0:
            limit_value = _quantize_directional(requested_limit, tick, side)
            if limit_value <= 0:
                limit_value = None
        elif requested_limit is not None and requested_limit > 0 and not request.market_is_remote:
            # Local simulation contracts may not have an exchange tick.  The
            # gateway still validates the final numeric value; remote paths
            # must provide an observed tick to avoid silently changing price.
            limit_value = requested_limit
        evidence["requested_limit_price"] = float(requested_limit) if requested_limit is not None else None
        evidence["tick_size"] = float(tick) if tick is not None else None
        evidence["quantized_limit_price"] = float(limit_value) if limit_value is not None else None

        if request.pending_candidate:
            return OrderSelectionDecision(None, preference, None, ttl, None, "DUPLICATE_PENDING_CANDIDATE", "同一候选已有未决订单，禁止重复挂单。", evidence)

        # 提高限价单权限：如果明确指定了限价支撑/阻力点位且与当前市价存在回踩间距，优先下发限价预埋单
        is_pullback_limit = (
            preference == "AUTO"
            and requested_limit is not None
            and quote is not None
            and quote > 0
            and abs(float(requested_limit) - float(quote)) / float(quote) > 0.0008
        )
        prefer_limit = preference == "LIMIT" or is_pullback_limit

        if not prefer_limit and preference in {"MARKET", "AUTO"} and market_ok:
            return OrderSelectionDecision("market", preference, None, ttl, None, "MARKET_EVIDENCE_OK", "时间周期对齐且价格处于合理突破位置，满足市价开单条件。", evidence)
        if preference == "MARKET" and not market_ok and limit_value is None:
            return OrderSelectionDecision(None, preference, None, ttl, None, "MARKET_EVIDENCE_INSUFFICIENT", "市价证据不足或未处于合理进场位置，且没有可验证的限价。", evidence)
        if limit_value is None:
            return OrderSelectionDecision(None, preference, None, ttl, None, "LIMIT_PRICE_UNAVAILABLE", "无法依据可验证 tick 生成限价，阻断下单。", evidence)
        expires_at = (now + timedelta(seconds=ttl)).isoformat()
        if is_pullback_limit:
            reason_code = "LIMIT_PREFERRED_PULLBACK"
            reason = "识别到关键支撑/阻力回踩位，优先下发限价预埋单并设置有限 TTL。"
        elif preference == "AUTO":
            reason_code = "LIMIT_FORCED_BY_EVIDENCE"
            reason = "市价证据不足或偏离突破位，按方向量化为限价并设置有限 TTL。"
        elif preference == "MARKET":
            reason_code = "MARKET_FALLBACK_LIMIT"
            reason = "市价证据不足，按可验证价格降级为限价并设置有限 TTL。"
        else:
            reason_code = "LIMIT_REQUESTED"
            reason = "按策略/用户偏好使用方向量化限价。"
        return OrderSelectionDecision("limit", preference, float(limit_value), ttl, expires_at, reason_code, reason, evidence)


@dataclass(frozen=True)
class TradeThrottleConfig:
    min_hold_duration_seconds: float = 5400.0  # 90 minutes
    noise_close_hold_duration_seconds: float = 10800.0  # 3 hours
    reentry_cooldown_seconds: float = 7200.0  # 2 hours
    early_close_stop_loss_bypass_pct: float = -3.0  # Loss <= -3% bypasses hold gate
    early_close_take_profit_bypass_pct: float = 8.0  # Profit >= 8% bypasses hold gate
    noise_loss_floor_pct: float = -2.0  # Inside [-2%, +3%] requires 3h hold
    noise_profit_ceiling_pct: float = 3.0


class TradeThrottlePolicy:
    """Industrial throttle gates ported from NOFX autopilot for position and order safety."""

    @staticmethod
    def check_open_throttle(
        symbol: str,
        *,
        has_open_position: bool,
        last_closed_at: datetime | None = None,
        now: datetime | None = None,
        cooldown_seconds: float = 7200.0,
    ) -> tuple[bool, str, str]:
        """Verify open action against existing position and re-entry cooldown."""
        if has_open_position:
            return False, "STRATEGY_MAX_POSITIONS", f"Trade throttle: {symbol} 已有同标的持仓，禁止重复加仓或双向冲突开仓。"
        now_dt = now or datetime.now(timezone.utc)
        if last_closed_at is not None:
            age_seconds = (now_dt - last_closed_at).total_seconds()
            if 0 <= age_seconds < cooldown_seconds:
                remaining_min = int(round((cooldown_seconds - age_seconds) / 60.0))
                return False, "STRATEGY_REENTRY_COOLDOWN", f"Trade throttle: {symbol} 刚平仓不久，进入重入冷却期（剩余约 {remaining_min} 分钟），防止追涨杀跌磨损。"
        return True, "ALLOWED", "开仓频控检查通过。"

    @staticmethod
    def check_close_throttle(
        symbol: str,
        *,
        entry_time: datetime | None,
        price_pnl_pct: float,
        now: datetime | None = None,
        config: TradeThrottleConfig | None = None,
    ) -> tuple[bool, str, str]:
        """Noise-band and minimum hold gate preventing AI from panic-closing prematurely."""
        cfg = config or TradeThrottleConfig()
        if entry_time is None:
            return True, "ALLOWED", "无入场时间戳，放行平仓。"
        now_dt = now or datetime.now(timezone.utc)
        held_seconds = max(0.0, (now_dt - entry_time).total_seconds())

        # Check hard stop or strong take-profit bypass
        if price_pnl_pct <= cfg.early_close_stop_loss_bypass_pct:
            return True, "ALLOWED", f"价格触及硬止损阈值 ({price_pnl_pct:.2f}% <= {cfg.early_close_stop_loss_bypass_pct}%)，放行紧急止损。"
        if price_pnl_pct >= cfg.early_close_take_profit_bypass_pct:
            return True, "ALLOWED", f"价格触及强力止盈阈值 ({price_pnl_pct:.2f}% >= {cfg.early_close_take_profit_bypass_pct}%)，放行锁定利润。"

        # Min hold duration check
        if held_seconds < cfg.min_hold_duration_seconds:
            remaining_min = int(round((cfg.min_hold_duration_seconds - held_seconds) / 60.0))
            held_min = int(round(held_seconds / 60.0))
            return False, "THROTTLE_MIN_HOLD_ACTIVE", f"Trade throttle: {symbol} 持仓仅 {held_min} 分钟（当前价格盈亏 {price_pnl_pct:.2f}%），未达最小持仓时长（{int(cfg.min_hold_duration_seconds//60)}分钟），防止微小波动频繁换手磨损手续费（剩余约 {remaining_min} 分钟）。"

        # Noise-band check (if held >= 90m but < 3h and PnL is within noise band [-2%, +3%])
        if held_seconds < cfg.noise_close_hold_duration_seconds:
            if cfg.noise_loss_floor_pct <= price_pnl_pct <= cfg.noise_profit_ceiling_pct:
                remaining_min = int(round((cfg.noise_close_hold_duration_seconds - held_seconds) / 60.0))
                held_min = int(round(held_seconds / 60.0))
                return False, "THROTTLE_NOISE_CLOSE_BLOCKED", f"Trade throttle: {symbol} 持仓 {held_min} 分钟，浮动盈亏 {price_pnl_pct:.2f}% 仍处于噪音震荡区间 [{cfg.noise_loss_floor_pct}%, {cfg.noise_profit_ceiling_pct}%]，请持满 3 小时再做平盘处理（剩余约 {remaining_min} 分钟）。"

        return True, "ALLOWED", "平仓频控检查通过。"


__all__ = [
    "DEFAULT_LIMIT_TTL_SECONDS",
    "MAX_LIMIT_TTL_SECONDS",
    "MIN_LIMIT_TTL_SECONDS",
    "ORDER_SELECTION_POLICY_VERSION",
    "OrderSelectionDecision",
    "OrderSelectionInput",
    "OrderSelectionPolicy",
    "TradeThrottleConfig",
    "TradeThrottlePolicy",
]
