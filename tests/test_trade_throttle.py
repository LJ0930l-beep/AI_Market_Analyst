from datetime import datetime, timedelta, timezone
import pytest

from core.trading.order_selection import TradeThrottleConfig, TradeThrottlePolicy


def test_trade_throttle_open_checks():
    now = datetime.now(timezone.utc)
    
    # 1. Allowed when clean
    ok, code, reason = TradeThrottlePolicy.check_open_throttle(
        "BTCUSDT",
        has_open_position=False,
        last_closed_at=None,
        now=now,
        cooldown_seconds=3600.0,
    )
    assert ok is True
    assert code == "ALLOWED"

    # 2. Blocked if existing position
    ok, code, reason = TradeThrottlePolicy.check_open_throttle(
        "BTCUSDT",
        has_open_position=True,
        last_closed_at=None,
        now=now,
    )
    assert ok is False
    assert code == "STRATEGY_MAX_POSITIONS"

    # 3. Blocked if closed recently within cooldown (e.g. 10m ago with 60m cooldown)
    closed_at = now - timedelta(minutes=10)
    ok, code, reason = TradeThrottlePolicy.check_open_throttle(
        "BTCUSDT",
        has_open_position=False,
        last_closed_at=closed_at,
        now=now,
        cooldown_seconds=3600.0,
    )
    assert ok is False
    assert code == "STRATEGY_REENTRY_COOLDOWN"
    assert "剩余约 50 分钟" in reason

    # 4. Allowed if cooldown expired (e.g. 70m ago with 60m cooldown)
    closed_long_ago = now - timedelta(minutes=70)
    ok, code, reason = TradeThrottlePolicy.check_open_throttle(
        "BTCUSDT",
        has_open_position=False,
        last_closed_at=closed_long_ago,
        now=now,
        cooldown_seconds=3600.0,
    )
    assert ok is True
    assert code == "ALLOWED"


def test_trade_throttle_close_checks():
    now = datetime.now(timezone.utc)
    cfg = TradeThrottleConfig()

    # 1. Unknown entry time -> allow
    ok, code, _ = TradeThrottlePolicy.check_close_throttle("BTCUSDT", entry_time=None, price_pnl_pct=0.5, now=now, config=cfg)
    assert ok is True

    # 2. Hard stop-loss bypass (e.g. -3.5% loss) -> allow even if held for 5 minutes
    entry_5m_ago = now - timedelta(minutes=5)
    ok, code, reason = TradeThrottlePolicy.check_close_throttle("BTCUSDT", entry_time=entry_5m_ago, price_pnl_pct=-3.5, now=now, config=cfg)
    assert ok is True
    assert "紧急止损" in reason

    # 3. Strong take-profit bypass (e.g. +9.0% profit) -> allow even if held for 5 minutes
    ok, code, reason = TradeThrottlePolicy.check_close_throttle("BTCUSDT", entry_time=entry_5m_ago, price_pnl_pct=9.0, now=now, config=cfg)
    assert ok is True
    assert "锁定利润" in reason

    # 4. Panic close within min hold (e.g. held 15m with +0.5% PnL) -> BLOCKED
    ok, code, reason = TradeThrottlePolicy.check_close_throttle("BTCUSDT", entry_time=entry_5m_ago, price_pnl_pct=0.5, now=now, config=cfg)
    assert ok is False
    assert code == "THROTTLE_MIN_HOLD_ACTIVE"
    assert "未达最小持仓时长" in reason

    # 5. Held 100m (>=90m, <180m) with +1.0% PnL (in noise band [-2%, +3%]) -> BLOCKED
    entry_100m_ago = now - timedelta(minutes=100)
    ok, code, reason = TradeThrottlePolicy.check_close_throttle("BTCUSDT", entry_time=entry_100m_ago, price_pnl_pct=1.0, now=now, config=cfg)
    assert ok is False
    assert code == "THROTTLE_NOISE_CLOSE_BLOCKED"
    assert "噪音震荡区间" in reason

    # 6. Held 100m with +4.5% PnL (>3.0% profit ceiling) -> ALLOWED
    ok, code, _ = TradeThrottlePolicy.check_close_throttle("BTCUSDT", entry_time=entry_100m_ago, price_pnl_pct=4.5, now=now, config=cfg)
    assert ok is True

    # 7. Held 200m (>=180m / 3h) with +1.0% PnL -> ALLOWED after noise hold duration
    entry_200m_ago = now - timedelta(minutes=200)
    ok, code, _ = TradeThrottlePolicy.check_close_throttle("BTCUSDT", entry_time=entry_200m_ago, price_pnl_pct=1.0, now=now, config=cfg)
    assert ok is True
