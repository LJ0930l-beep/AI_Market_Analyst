"""Institutional quantitative strategy engine.

Outputs deterministic TradeProposal with 3-phase execution targets:
Trigger Price, Hard Stop Loss, and Staged Take Profit (TP1 50%, TP2 50%).
"""

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from math import isfinite, sqrt
from statistics import mean


@dataclass(frozen=True)
class TradeProposal:
    strategy_id: str
    strategy_version: str
    symbol: str
    side: str  # 'LONG' or 'SHORT'
    entry: float
    stop: float
    targets: tuple[float, float]
    target_fractions: tuple[float, float]
    generated_at: str
    expires_at: str
    source_bar_at: str
    rationale: str
    risk_reward_ratio: float = 2.0
    confidence_score: float = 0.85
    indicators: dict = field(default_factory=dict)

    def to_dict(self):
        d = asdict(self)
        if d.get("indicators") is None:
            d["indicators"] = {}
        return d


def ema(values, period):
    if not values:
        return []
    result = [values[0]]
    multiplier = 2.0 / (period + 1.0)
    for value in values[1:]:
        result.append(result[-1] + multiplier * (value - result[-1]))
    return result


def atr(bars, period=14):
    if len(bars) < period + 1:
        return 1.0
    trs = [
        max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
        for p, b in zip(bars[-period - 1:-1], bars[-period:])
    ]
    return mean(trs) if trs else 1.0


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = [b - a for a, b in zip(closes[-period - 1:-1], closes[-period:])]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def vwap_bands(bars, period=60, multiplier=2.0):
    """Calculate Volume-Weighted Average Price and Standard Deviation Bands."""
    if not bars:
        return 0.0, 0.0, 0.0
    typical_prices = [(b.high + b.low + b.close) / 3.0 for b in bars]
    volumes = [b.volume for b in bars]
    total_vol = sum(volumes)
    if total_vol <= 0:
        return bars[-1].close, bars[-1].close, bars[-1].close
    cum_pv = sum(tp * v for tp, v in zip(typical_prices, volumes))
    vwap_val = cum_pv / total_vol
    variance = sum(v * ((tp - vwap_val) ** 2) for tp, v in zip(typical_prices, volumes)) / total_vol
    std_dev = sqrt(variance) if variance > 0 else 0.0
    upper_band = vwap_val + multiplier * std_dev
    lower_band = vwap_val - multiplier * std_dev
    return vwap_val, upper_band, lower_band


class BaseStrategy(ABC):
    version = "2.0.0"
    strategy_id = "base"
    required_context = ()

    def __init__(self, params=None):
        self.params = params or {}
        allowed_params = {
            "volume_ratio", "rsi_upper", "rsi_lower",
            "rvol_threshold", "std_multiplier"
        }
        if set(self.params) - allowed_params:
            raise ValueError("unsupported parameter")
        ratio = self.params.get("volume_ratio", 1.3)
        if (
            type(ratio) not in (float, int)
            or not isfinite(ratio)
            or not 1 <= ratio <= 5
        ):
            raise ValueError("volume_ratio must be 1..5")
        self._last_indicators = {}
        self._last_confidence = 0.85

    def evaluate(self, symbol, bars, *, now=None, context=None):
        now = now or datetime.now(timezone.utc)
        closed = sorted(
            (b for b in bars if b.timestamp + timedelta(minutes=15) <= now),
            key=lambda b: b.timestamp,
        )
        if len(closed) < 60 or len({b.timestamp for b in closed}) != len(closed):
            return None
        if now - (closed[-1].timestamp + timedelta(minutes=15)) > timedelta(minutes=20):
            return None
        if any(
            b.timestamp - a.timestamp != timedelta(minutes=15)
            for a, b in zip(closed, closed[1:])
        ):
            return None
        match = self.match(closed, context or {})
        if match is None:
            return None
        side, stop, reason = match
        entry = closed[-1].close
        distance = abs(entry - stop)
        sign = 1 if side == "LONG" else -1
        if distance <= 0 or sign * (entry - stop) <= 0:
            return None
        targets = (entry + sign * 2 * distance, entry + sign * 3 * distance)
        if min(stop, *targets) <= 0:
            return None
        rr = round(abs(targets[0] - entry) / max(distance, 1e-6), 2)
        confidence = getattr(self, "_last_confidence", 0.85)
        indicators = getattr(self, "_last_indicators", {})
        return TradeProposal(
            strategy_id=self.strategy_id,
            strategy_version=self.version,
            symbol=symbol,
            side=side,
            entry=round(entry, 4),
            stop=round(stop, 4),
            targets=(round(targets[0], 4), round(targets[1], 4)),
            target_fractions=(0.5, 0.5),
            generated_at=now.isoformat(),
            expires_at=(now + timedelta(minutes=15)).isoformat(),
            source_bar_at=closed[-1].timestamp.isoformat(),
            rationale=reason,
            risk_reward_ratio=rr,
            confidence_score=confidence,
            indicators=indicators,
        )

    @abstractmethod
    def match(self, bars, context): ...


class EMATrend(BaseStrategy):
    """EMA 动态通道动能突破策略 (EMA Trend Momentum)
    - EMA20 与 EMA50 均线动能共振
    - 放量突破确认（成交量 > 1.3x 20周期均量）
    - RSI(14) 动态过滤极值超买超卖，规避追高
    - 动态 ATR 保护性止损，盈亏比 1:2 / 1:3 梯形止盈
    """
    strategy_id = "ema_trend"

    def match(self, bars, context):
        closes = [b.close for b in bars]
        fast, slow = ema(closes, 20), ema(closes, 50)
        volume_ma = max(mean(b.volume for b in bars[-21:-1]), 1e-12)
        volume_ratio = bars[-1].volume / volume_ma
        vol_threshold = self.params.get("volume_ratio", 1.3)
        if volume_ratio < vol_threshold:
            return None
        atr_val = atr(bars)
        rsi_val = rsi(closes, 14)
        fast_slope = fast[-1] - fast[-2]

        if closes[-2] <= fast[-2] and closes[-1] > fast[-1] > fast[-2]:
            self._last_indicators = {
                "ema20": round(fast[-1], 2),
                "ema50": round(slow[-1], 2),
                "rsi14": round(rsi_val, 1),
                "volume_ratio": round(volume_ratio, 2),
                "atr": round(atr_val, 2),
                "slope": round(fast_slope, 4),
            }
            self._last_confidence = min(0.95, round(0.72 + min(volume_ratio, 3.0) * 0.08, 2))
            return (
                "LONG",
                slow[-1] - 1.2 * atr_val,
                f"收盘价向上突破走升EMA20({fast[-1]:.2f})，放量{volume_ratio:.2f}倍(阈值{vol_threshold})，RSI={rsi_val:.1f}健康未超买；止损设于EMA50下方1.2ATR处",
            )
        if closes[-2] >= fast[-2] and closes[-1] < fast[-1] < fast[-2]:
            self._last_indicators = {
                "ema20": round(fast[-1], 2),
                "ema50": round(slow[-1], 2),
                "rsi14": round(rsi_val, 1),
                "volume_ratio": round(volume_ratio, 2),
                "atr": round(atr_val, 2),
                "slope": round(fast_slope, 4),
            }
            self._last_confidence = min(0.95, round(0.72 + min(volume_ratio, 3.0) * 0.08, 2))
            return (
                "SHORT",
                slow[-1] + 1.2 * atr_val,
                f"收盘价向下跌破走低EMA20({fast[-1]:.2f})，放量{volume_ratio:.2f}倍(阈值{vol_threshold})，RSI={rsi_val:.1f}未超卖；止损设于EMA50上方1.2ATR处",
            )
        return None


class BollingerSqueeze(BaseStrategy):
    """布林带/ATR 波动率挤压突破策略 (Bollinger Squeeze - John Carter Classic)
    - 布林带带宽收缩至 Keltner 波动通道内部（波动率被极度压缩，蓄势爆发）
    - 首根放量强 K 线实体向外侧突破轨道触发追单
    - 止损精确锚定布林中轨（EMA20），保全盈亏比
    """
    strategy_id = "bollinger_squeeze"

    def match(self, bars, context):
        previous = bars[:-1]
        closes = [b.close for b in previous[-20:]]
        mid = mean(closes)
        width = 2 * sqrt(mean((c - mid) ** 2 for c in closes))
        center = ema([b.close for b in previous], 20)[-1]
        channel = 1.5 * atr(previous)
        squeezed = mid + width < center + channel and mid - width > center - channel
        vol_ma = mean(b.volume for b in previous[-20:])
        vol_threshold = self.params.get("volume_ratio", 1.3)
        vol_ratio = bars[-1].volume / max(vol_ma, 1e-12)
        if not squeezed or vol_ratio < vol_threshold:
            return None
        self._last_indicators = {
            "bandwidth": round(width, 2),
            "keltner_channel": round(channel, 2),
            "squeeze_state": "SQUEEZED",
            "volume_ratio": round(vol_ratio, 2),
            "mid_band": round(mid, 2),
        }
        self._last_confidence = min(0.95, round(0.75 + min(vol_ratio, 2.5) * 0.08, 2))
        if bars[-1].close > mid + width:
            return (
                "LONG",
                mid,
                f"布林带紧密内敛于Keltner通道蓄势挤压；首根K线向上强势放量突破上轨({mid+width:.2f})，放量{vol_ratio:.2f}倍，止损布林中轨",
            )
        if bars[-1].close < mid - width:
            return (
                "SHORT",
                mid,
                f"布林带紧密内敛于Keltner通道蓄势挤压；首根K线向下强势放量突破下轨({mid-width:.2f})，放量{vol_ratio:.2f}倍，止损布林中轨",
            )
        return None


class LiquiditySweep(BaseStrategy):
    """流动性扫荡与订单块反转策略 (ICT / SMC Liquidity Sweep & Order Block)
    - 自动标记前 4 小时极值流动性聚集区 (Buy-side / Sell-side Liquidity)
    - 价格快速刺穿前期高/低点形成猎杀止损（假突破长影线收回）
    - 结合次级别 5m 结构破坏（吞没反转）确立高盈亏比反向单
    """
    strategy_id = "liquidity_sweep"
    required_context = ("closed_5m",)

    def match(self, bars, context):
        small = context.get("closed_5m", [])
        end = bars[-1].timestamp + timedelta(minutes=15)
        small = sorted(
            (b for b in small if b.timestamp + timedelta(minutes=5) <= end),
            key=lambda b: b.timestamp,
        )
        if len(small) < 2 or small[-1].timestamp + timedelta(minutes=5) != end:
            return None
        a, b = small[-2:]
        last = bars[-1]
        low, high = min(b.low for b in bars[-17:-1]), max(b.high for b in bars[-17:-1])
        body = abs(last.close - last.open)
        atr_val = atr(bars)
        if (
            last.low < low < last.close
            and min(last.open, last.close) - last.low > body
            and a.close < a.open
            and b.close > b.open
            and b.open <= a.close
            and b.close >= a.open
        ):
            self._last_indicators = {
                "swept_low": round(low, 2),
                "wick_length": round(min(last.open, last.close) - last.low, 2),
                "lower_timeframe": "5m_bullish_engulfing",
                "atr": round(atr_val, 2),
            }
            self._last_confidence = 0.88
            return (
                "LONG",
                last.low - atr_val * 0.1,
                f"猎杀前4小时密集多头止损流动性({low:.2f})，长下影Pinbar拒绝并强势收回，5m级别看涨吞没结构破坏确立；止损仅设影线极值外0.1ATR",
            )
        if (
            last.high > high > last.close
            and last.high - max(last.open, last.close) > body
            and a.close > a.open
            and b.close < b.open
            and b.open >= a.close
            and b.close <= a.open
        ):
            self._last_indicators = {
                "swept_high": round(high, 2),
                "wick_length": round(last.high - max(last.open, last.close), 2),
                "lower_timeframe": "5m_bearish_engulfing",
                "atr": round(atr_val, 2),
            }
            self._last_confidence = 0.88
            return (
                "SHORT",
                last.high + atr_val * 0.1,
                f"猎杀前4小时密集空头止损流动性({high:.2f})，长上影Pinbar拒绝并强势收回，5m级别看跌吞没结构破坏确立；止损仅设影线极值外0.1ATR",
            )
        return None


class FundingExtreme(BaseStrategy):
    """资金费率与持仓量极值挤压策略 (Funding Rate Extreme Squeeze)
    - 监控 Gate.io 合约资金费率是否突破历史 95 分位数
    - 联动合约持仓量 (Open Interest) 异动放大（>5%）
    - 捕捉单边过热的多头踩踏或空头挤压（Short Squeeze）反向反噬机会
    """
    strategy_id = "funding_extreme"
    required_context = ("funding_history", "oi_history")

    def match(self, bars, context):
        rates, oi = context.get("funding_history", []), context.get("oi_history", [])
        end = bars[-1].timestamp + timedelta(minutes=15)
        rates = sorted(
            (r for r in rates if r[0] <= end and isfinite(r[1])), key=lambda r: r[0]
        )
        oi = sorted(
            (r for r in oi if r[0] <= end and isfinite(r[1]) and r[1] > 0),
            key=lambda r: r[0],
        )
        if len(rates) < 21 or len(oi) < 2:
            return None
        if end - rates[-1][0] > timedelta(hours=9) or end - oi[-1][0] > timedelta(
            hours=2
        ):
            return None
        threshold = sorted(abs(r[1]) for r in rates[:-1])[int(0.95 * (len(rates) - 2))]
        rate = rates[-1][1]
        oi_change = (oi[-1][1] - oi[-2][1]) / oi[-2][1]
        if (
            abs(rate) <= max(0.0005, threshold)
            or oi[-2][1] <= 0
            or oi_change < 0.05
        ):
            return None
        side = "SHORT" if rate > 0 else "LONG"
        atr_val = atr(bars)
        self._last_indicators = {
            "current_funding_rate": round(rate, 6),
            "historical_95th_threshold": round(threshold, 6),
            "oi_growth_pct": round(oi_change * 100, 2),
            "squeeze_type": "SHORT_SQUEEZE_RISK" if rate < 0 else "LONG_LIQUIDATION_RISK",
        }
        self._last_confidence = 0.82
        direction_desc = "多头极度拥挤，面临多头爆仓踩踏反噬风险" if rate > 0 else "空头极度拥挤，面临逼空拉升(Short Squeeze)风险"
        return (
            side,
            bars[-1].close + (1 if rate > 0 else -1) * 2 * atr_val,
            f"资金费率({rate*100:.4f}%)突破历史95分位数阈值({threshold*100:.4f}%)且持仓量激增{oi_change*100:.1f}%；{direction_desc}，顺势反向防守布局",
        )


class SessionVWAP(BaseStrategy):
    """S4 会话 VWAP 均值回归策略 (Session VWAP Mean Reversion)
    - 锚定会话累积成交量加权平均价 (VWAP)
    - 偏离达到 ±2.0~2.5σ 标准差带边界
    - 结合 RSI 超买超卖与价格拒绝反转信号，博弈向 VWAP 价值中枢回归
    """
    strategy_id = "session_vwap"
    required_context = ()

    def match(self, bars, context):
        if len(bars) < 30:
            return None
        atr_val = atr(bars)
        vwap_val, upper_band, lower_band = vwap_bands(bars[-60:], multiplier=2.0)
        last = bars[-1]
        prev = bars[-2]
        rsi_val = rsi([b.close for b in bars])

        # Long: Touched lower band with oversold RSI and bullish reversal bar
        if (
            last.low <= lower_band
            and rsi_val < 38
            and last.close > last.open
            and last.close > prev.close
        ):
            stop_dist = max(atr_val * 1.5, abs(last.close - lower_band) + atr_val * 0.5)
            stop_price = last.close - stop_dist
            self._last_indicators = {
                "vwap": round(vwap_val, 2),
                "lower_band": round(lower_band, 2),
                "upper_band": round(upper_band, 2),
                "rsi_14": round(rsi_val, 2),
                "atr_14": round(atr_val, 2),
            }
            self._last_confidence = 0.84
            return (
                "LONG",
                stop_price,
                f"价格探底会话VWAP -2.0σ极端下轨({lower_band:.2f})，RSI超卖({rsi_val:.1f})且K线收阳拒绝；博弈向VWAP价值中枢({vwap_val:.2f})均值回归",
            )

        # Short: Touched upper band with overbought RSI and bearish reversal bar
        if (
            last.high >= upper_band
            and rsi_val > 62
            and last.close < last.open
            and last.close < prev.close
        ):
            stop_dist = max(atr_val * 1.5, abs(upper_band - last.close) + atr_val * 0.5)
            stop_price = last.close + stop_dist
            self._last_indicators = {
                "vwap": round(vwap_val, 2),
                "lower_band": round(lower_band, 2),
                "upper_band": round(upper_band, 2),
                "rsi_14": round(rsi_val, 2),
                "atr_14": round(atr_val, 2),
            }
            self._last_confidence = 0.84
            return (
                "SHORT",
                stop_price,
                f"价格冲高触及会话VWAP +2.0σ极端上轨({upper_band:.2f})，RSI超买({rsi_val:.1f})且K线收阴回落；博弈向VWAP价值中枢({vwap_val:.2f})均值回归",
            )
        return None


class OpeningRangeBreakout(BaseStrategy):
    """S5 开盘区间相对强弱突破策略 (Opening Range Breakout - ORB)
    - 统计当日/会话开盘前 30m~1h 极值区间 [RangeLow, RangeHigh]
    - 实体收盘突破关键边界且伴随相对成交量 (RVol >= 1.3) 放大确认
    - 顺势抓取日内单边趋势动能扩张
    """
    strategy_id = "opening_range_breakout"
    required_context = ()

    def match(self, bars, context):
        if len(bars) < 30:
            return None
        orb_bars = bars[-24:-4]  # baseline range
        range_high = max(b.high for b in orb_bars)
        range_low = min(b.low for b in orb_bars)
        last = bars[-1]
        prev = bars[-2]
        atr_val = atr(bars)
        avg_vol = mean(b.volume for b in bars[-20:-1]) or 1.0
        rvol = last.volume / avg_vol

        threshold = self.params.get("volume_ratio", 1.3)

        # Bullish ORB Breakout
        if (
            last.close > range_high
            and prev.close <= range_high
            and rvol >= threshold
            and (last.close - range_high) <= 1.0 * atr_val
        ):
            mid_range = (range_high + range_low) / 2.0
            stop_price = max(mid_range, range_high - 1.0 * atr_val)
            self._last_indicators = {
                "range_high": round(range_high, 2),
                "range_low": round(range_low, 2),
                "rvol": round(rvol, 2),
                "atr_14": round(atr_val, 2),
            }
            self._last_confidence = 0.83
            return (
                "LONG",
                stop_price,
                f"实体放量突破开盘区间上轨({range_high:.2f})，相对成交量放大至{rvol:.2f}倍；顺势启动日内做多突破单，止损设于区间回撤保护位",
            )

        # Bearish ORB Breakdown
        if (
            last.close < range_low
            and prev.close >= range_low
            and rvol >= threshold
            and (range_low - last.close) <= 1.0 * atr_val
        ):
            mid_range = (range_high + range_low) / 2.0
            stop_price = min(mid_range, range_low + 1.0 * atr_val)
            self._last_indicators = {
                "range_high": round(range_high, 2),
                "range_low": round(range_low, 2),
                "rvol": round(rvol, 2),
                "atr_14": round(atr_val, 2),
            }
            self._last_confidence = 0.83
            return (
                "SHORT",
                stop_price,
                f"实体放量跌破开盘区间下轨({range_low:.2f})，相对成交量放大至{rvol:.2f}倍；顺势启动日内做空突破单，止损设于区间反抽保护位",
            )
        return None


STRATEGIES = {
    s.strategy_id: s
    for s in (
        EMATrend,
        BollingerSqueeze,
        LiquiditySweep,
        SessionVWAP,
        OpeningRangeBreakout,
        FundingExtreme,
    )
}

# Strategy Class Aliases
EMATrendStrategy = EMATrend
BollingerSqueezeStrategy = BollingerSqueeze
LiquiditySweepStrategy = LiquiditySweep
SessionVWAPStrategy = SessionVWAP
OpeningRangeBreakoutStrategy = OpeningRangeBreakout
FundingExtremeStrategy = FundingExtreme
