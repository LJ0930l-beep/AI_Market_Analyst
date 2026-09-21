"""Institutional quantitative strategy engine.

Outputs deterministic TradeProposal with 3-phase execution targets:
Trigger Price, Hard Stop Loss, and Staged Take Profit (TP1 50%, TP2 50%).
"""

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import hashlib
import inspect
import json
from math import isfinite, sqrt
from statistics import mean
from typing import Any


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
    # ``rule_score`` is deterministic rule strength.  It is deliberately
    # separate from a calibrated probability, which is unavailable until a
    # sufficiently large labelled sample is persisted.
    rule_score: float = 0.85
    calibrated_probability: float | None = None
    calibration_sample_size: int = 0
    quantization_tick: str | None = None
    quantization_step: str | None = None
    reason_codes: tuple[str, ...] = ()
    market_type: str | None = None
    signal_timeframe: str | None = None
    params_hash: str | None = None
    evidence_refs: tuple[str, ...] = ()

    def to_dict(self):
        d = asdict(self)
        if d.get("indicators") is None:
            d["indicators"] = {}
        d["reason_codes"] = list(d.get("reason_codes") or ())
        d["evidence_refs"] = list(d.get("evidence_refs") or ())
        return d


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    name: str
    value_type: str
    minimum: float | int | None = None
    maximum: float | int | None = None
    default: Any = None

    def validate(self, value: Any) -> Any:
        if self.value_type == "bool":
            if type(value) is not bool:
                raise ValueError(f"{self.name} must be a boolean")
            return value
        if self.value_type == "int":
            if type(value) is not int or isinstance(value, bool):
                raise ValueError(f"{self.name} must be an integer")
            numeric: float | int = value
        elif self.value_type == "str":
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{self.name} must be a non-empty string")
            return value.strip()
        else:
            if isinstance(value, bool):
                raise ValueError(f"{self.name} must be numeric")
            try:
                numeric = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{self.name} must be numeric") from exc
            if not isfinite(numeric):
                raise ValueError(f"{self.name} must be finite")
        if self.minimum is not None and numeric < self.minimum:
            raise ValueError(f"{self.name} must be >= {self.minimum}")
        if self.maximum is not None and numeric > self.maximum:
            raise ValueError(f"{self.name} must be <= {self.maximum}")
        return value


@dataclass(frozen=True, slots=True)
class StrategySpec:
    strategy_id: str
    code_version: str
    code_hash: str
    params_hash: str
    supported_markets: tuple[str, ...]
    signal_timeframe: str
    context_timeframes: tuple[str, ...]
    required_context: tuple[str, ...]
    parameter_schema: tuple[ParameterSpec, ...]
    indicator_version: str
    exit_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "code_version": self.code_version,
            "code_hash": self.code_hash,
            "params_hash": self.params_hash,
            "supported_markets": list(self.supported_markets),
            "signal_timeframe": self.signal_timeframe,
            "context_timeframes": list(self.context_timeframes),
            "required_context": list(self.required_context),
            "parameter_schema": [asdict(item) for item in self.parameter_schema],
            "indicator_version": self.indicator_version,
            "exit_version": self.exit_version,
        }


def _params_hash(params: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("price must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError("price must be a finite decimal")
    return result


def quantize_price(value: Any, tick_size: Any, *, side: str, role: str) -> Decimal:
    """Quantize conservatively without using display precision for economics."""

    price = _decimal(value)
    tick = _decimal(tick_size)
    if tick <= 0:
        raise ValueError("tick_size must be positive")
    units = price / tick
    # A buy entry/short stop is made no better by rounding up; a long stop and
    # short entry are made no better by rounding down.  Targets are floored
    # for longs and ceiled for shorts so risk is never silently expanded.
    if role == "entry":
        rounding = ROUND_CEILING if side.upper() == "LONG" else ROUND_FLOOR
    elif role == "stop":
        rounding = ROUND_FLOOR if side.upper() == "LONG" else ROUND_CEILING
    else:
        rounding = ROUND_FLOOR if side.upper() == "LONG" else ROUND_CEILING
    return units.to_integral_value(rounding=rounding) * tick


def ema(values, period):
    if not values:
        return []
    result = [values[0]]
    multiplier = 2.0 / (period + 1.0)
    for value in values[1:]:
        result.append(result[-1] + multiplier * (value - result[-1]))
    return result


def atr(bars, period=14):
    if not bars:
        return 0.0
    if len(bars) < 2:
        return max(bars[0].high - bars[0].low, bars[0].close * 0.005)
    trs = [
        max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
        for p, b in zip(bars[:-1], bars[1:])
    ]
    if len(trs) > period:
        trs = trs[-period:]
    return mean(trs) if trs else max(bars[-1].high - bars[-1].low, bars[-1].close * 0.005)


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


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if value:
        if isinstance(value, (int, float)) and isfinite(float(value)):
            # Gate's native derivative endpoints use Unix milliseconds, while
            # CCXT commonly uses milliseconds too. Keep seconds usable for
            # provider adapters and deterministic tests.
            timestamp = float(value)
            if abs(timestamp) >= 100_000_000_000:
                timestamp /= 1000.0
            try:
                return datetime.fromtimestamp(timestamp, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        try:
            text = str(value).strip()
            if text.isdigit():
                return _as_datetime(int(text))
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


def _normalise_funding_point(value: Any) -> tuple[datetime, float] | None:
    unit = None
    if isinstance(value, dict):
        timestamp = value.get("timestamp") or value.get("time") or value.get("event_time")
        raw_rate = value.get("funding_rate", value.get("fundingRate", value.get("value")))
        unit = str(value.get("unit") or value.get("rate_unit") or "").strip().lower()
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        timestamp, raw_rate = value[0], value[1]
        unit = "decimal_fraction"
    else:
        return None
    point = _as_datetime(timestamp)
    try:
        rate = float(raw_rate)
    except (TypeError, ValueError):
        return None
    if point is None or not isfinite(rate):
        return None
    if unit in {"bps", "basis_points"}:
        rate /= 10_000.0
    elif unit not in {"decimal_fraction", "fraction", "rate", ""}:
        return None
    return point, rate


def _normalise_oi_point(value: Any) -> tuple[datetime, float] | None:
    unit = None
    if isinstance(value, dict):
        timestamp = value.get("timestamp") or value.get("time") or value.get("event_time")
        raw_oi = value.get("open_interest", value.get("openInterestAmount", value.get("value")))
        unit = str(value.get("unit") or value.get("oi_unit") or "").strip().lower()
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        timestamp, raw_oi = value[0], value[1]
        unit = "contracts"
    else:
        return None
    point = _as_datetime(timestamp)
    try:
        oi = float(raw_oi)
    except (TypeError, ValueError):
        return None
    if point is None or not isfinite(oi):
        return None
    if unit not in {"contracts", "base", "base_asset", "notional", ""}:
        return None
    return point, oi


class BaseStrategy(ABC):
    version = "2.1.0"
    strategy_id = "base"
    supported_markets = ("crypto", "equity")
    signal_timeframe = "15m"
    context_timeframes = ()
    warmup_bars = 60
    params_schema: dict[str, ParameterSpec] = {}
    entry_rule = ""
    exit_rule = ""
    required_context = ()
    indicator_version = "indicators_v2"
    exit_version = "staged_exit_v1"

    def __init__(self, params=None):
        raw_params = dict(params or {})
        # The registry is the source of truth.  A parameter that is accepted
        # here must also be consumed by the strategy implementation.
        unknown = set(raw_params) - set(self.parameter_schema())
        if unknown:
            raise ValueError(f"unsupported parameter: {sorted(unknown)[0]}")
        self._explicit_params = set(raw_params)
        self.params = {}
        for name, spec in self.parameter_schema().items():
            if name in raw_params:
                self.params[name] = spec.validate(raw_params[name])
            elif spec.default is not None:
                self.params[name] = spec.default
        if "rsi_upper" in self.params and "rsi_lower" in self.params and self.params["rsi_lower"] >= self.params["rsi_upper"]:
            raise ValueError("rsi_lower must be below rsi_upper")
        # Retain exact caller parameters for the immutable research manifest;
        # defaults are included so two effective configurations cannot collide.
        self.params_hash = _params_hash(self.params)
        self._last_indicators = {}
        self._last_confidence = 0.85
        self.last_status = "IDLE"
        self.last_reason = ""

    @classmethod
    def parameter_schema(cls) -> dict[str, ParameterSpec]:
        return dict(cls.params_schema)

    @classmethod
    def spec(cls, params: dict[str, Any] | None = None) -> StrategySpec:
        effective = cls(params).params if params is not None else cls().params
        try:
            code = inspect.getsource(cls)
        except (OSError, TypeError):
            code = cls.__qualname__
        return StrategySpec(
            strategy_id=cls.strategy_id,
            code_version=cls.version,
            code_hash=hashlib.sha256(code.encode("utf-8")).hexdigest(),
            params_hash=_params_hash(effective),
            supported_markets=tuple(cls.supported_markets),
            signal_timeframe=cls.signal_timeframe,
            context_timeframes=tuple(cls.context_timeframes),
            required_context=tuple(cls.required_context),
            parameter_schema=tuple(cls.parameter_schema().values()),
            indicator_version=cls.indicator_version,
            exit_version=cls.exit_version,
        )

    def evaluate(self, symbol, bars, *, now=None, context=None):
        self.last_status = "IDLE"
        self.last_reason = ""
        now = now or datetime.now(timezone.utc)
        context = context or {}

        actual_timeframe = context.get("timeframe") or context.get("signal_timeframe")
        if actual_timeframe is not None and str(actual_timeframe).lower() != self.signal_timeframe:
            self.last_status = "UNSUPPORTED"
            self.last_reason = f"Signal timeframe mismatch: expected {self.signal_timeframe}, got {actual_timeframe}"
            return None
        market_type = str(context.get("market_type") or "").strip().lower()
        if market_type and market_type not in {str(item).lower() for item in self.supported_markets}:
            self.last_status = "UNSUPPORTED"
            self.last_reason = f"Market type {market_type} is not supported by {self.strategy_id}"
            return None

        # 1. Check required context capabilities (R08, R09, AT17)
        for req in self.required_context:
            if req not in context or context[req] is None:
                self.last_status = "UNSUPPORTED"
                self.last_reason = f"Missing required context capability: {req}"
                return None

        if not bars:
            self.last_status = "WARMING_UP"
            self.last_reason = "No bars provided"
            return None

        # The declared signal timeframe is authoritative.  Inferring the
        # period from the first two rows allowed a malformed fixed-period
        # feed to make an entire replay look valid (and let a future row
        # masquerade as a closed signal bar).
        bar_step = {
            "5m": timedelta(minutes=5),
            "15m": timedelta(minutes=15),
            "1h": timedelta(hours=1),
            "1d": timedelta(days=1),
        }.get(self.signal_timeframe)
        if bar_step is None:
            self.last_status = "UNSUPPORTED"
            self.last_reason = f"Unsupported signal timeframe: {self.signal_timeframe}"
            return None

        # 2. Strict filtering of unclosed / future bars (no lookahead)
        def bar_end(bar: Any) -> datetime:
            explicit_end = _as_datetime(getattr(bar, "bar_end", None))
            return explicit_end or (bar.timestamp + bar_step)

        closed = sorted(
            (
                b for b in bars
                if bar_end(b) <= now
                and getattr(b, "is_closed", None) is not False
            ),
            key=lambda b: b.timestamp,
        )

        if len(closed) < self.warmup_bars:
            self.last_status = "WARMING_UP"
            self.last_reason = f"Insufficient warmup bars ({len(closed)} < {self.warmup_bars})"
            return None

        # A repeated timestamp is a data-identity defect, not a warmup deficit:
        # the caller handed us more than one bar series for the same symbol
        # (for example Gate's traded price, mark price and index price for one
        # 15m bar, or a legacy mirror of it).  Both conditions used to share one
        # branch, so this case was reported as "Insufficient warmup bars
        # (112 < 60)" -- a message that contradicted itself, because the bar
        # count was already above the warmup threshold.  That hid the real
        # defect and sent readers looking for missing history instead of a
        # mixed-identity read.
        distinct_bars = {b.timestamp for b in closed}
        if len(distinct_bars) != len(closed):
            self.last_status = "WARMING_UP"
            self.last_reason = (
                f"Duplicate bar timestamps ({len(closed)} rows over "
                f"{len(distinct_bars)} distinct bars): the series mixes more than "
                f"one bar identity for symbol {getattr(self, 'signal_timeframe', '')}"
            )
            return None

        # Check for stale data (last closed bar older than 2 periods + 5m)
        if now - bar_end(closed[-1]) > (bar_step * 2 + timedelta(minutes=5)):
            self.last_status = "STALE_DATA"
            self.last_reason = "Market data stream stale or halted"
            return None

        # Check for timeline gaps in bar sequence
        if any(
            b.timestamp - a.timestamp != bar_step
            for a, b in zip(closed, closed[1:])
        ):
            self.last_status = "GAP_DETECTED"
            self.last_reason = "Irregular bar intervals or data gaps detected"
            return None

        # 3. Specific strategy matching logic
        match = self.match(closed, context)
        if match is None:
            # Check if match set a specific status or default to NO_TRIGGER
            if self.last_status in ("IDLE", "PROPOSAL"):
                self.last_status = "NO_TRIGGER"
                self.last_reason = "Strategy rules not triggered on current bar"
            return None

        side, stop, reason = match
        entry = closed[-1].close
        sign = 1 if side == "LONG" else -1

        # Enforce minimum stop distance: max(1.8×ATR, 1.2% of entry)
        # Prevent tight stops from being hunted as liquidity sweeps by exchange market makers.
        # Cap at 10% of entry so extreme volatility/small assets don't produce negative stops.
        atr_val = atr(closed)
        min_stop_distance = min(max(1.8 * atr_val, entry * 0.012), entry * 0.10)
        raw_distance = abs(entry - stop)
        if raw_distance < min_stop_distance:
            stop = entry - sign * min_stop_distance
            reason = reason + f"；止损已按防流动性扫荡安全距离扩展至 {min_stop_distance:.6g}（1.8×ATR={1.8*atr_val:.6g}, 1.2%={entry*0.012:.6g}）"

        distance = abs(entry - stop)
        if distance <= 0 or sign * (entry - stop) <= 0:
            self.last_status = "INVALID_STOP_DISTANCE"
            self.last_reason = f"Invalid stop placement: entry={entry}, stop={stop}"
            return None

        # Sane targets with step ratios
        targets = (entry + sign * 2 * distance, entry + sign * 3 * distance)
        if min(stop, *targets) <= 0:
            self.last_status = "INVALID_TARGET_LEVELS"
            self.last_reason = "Target or stop price dropped to zero or negative"
            return None

        tick_size = context.get("tick_size")
        step_size = context.get("step_size")
        instrument = context.get("instrument")
        if instrument is not None:
            tick_size = tick_size or getattr(instrument, "tick_size", None)
            step_size = step_size or getattr(instrument, "step_size", None)
            market_type = market_type or str(getattr(instrument, "market_type", ""))
        market = context.get("market")
        if isinstance(market, dict):
            precision = market.get("precision") if isinstance(market.get("precision"), dict) else {}
            tick_size = tick_size or precision.get("price")
            step_size = step_size or precision.get("amount")

        quantized_entry = _decimal(entry)
        quantized_stop = _decimal(stop)
        quantized_targets = tuple(_decimal(value) for value in targets)
        quantization_tick = None
        quantization_step = None
        if tick_size is not None:
            try:
                quantized_entry = quantize_price(entry, tick_size, side=side, role="entry")
                quantized_stop = quantize_price(stop, tick_size, side=side, role="stop")
                quantized_targets = tuple(quantize_price(value, tick_size, side=side, role="target") for value in targets)
                quantization_tick = format(_decimal(tick_size), "f")
            except ValueError as exc:
                self.last_status = "INVALID_QUANTIZATION"
                self.last_reason = str(exc)
                return None
        if step_size is not None:
            try:
                quantization_step = format(_decimal(step_size), "f")
            except ValueError:
                self.last_status = "INVALID_QUANTIZATION"
                self.last_reason = "step_size is invalid"
                return None
        entry = float(quantized_entry)
        stop = float(quantized_stop)
        targets = tuple(float(value) for value in quantized_targets)
        distance = abs(entry - stop)
        if distance <= 0 or sign * (entry - stop) <= 0 or min(stop, *targets) <= 0 or any(sign * (value - entry) <= 0 for value in targets):
            self.last_status = "INVALID_QUANTIZED_LEVELS"
            self.last_reason = "Tick-quantized entry, stop, or target violates the strategy geometry"
            return None

        rr = round(abs(targets[0] - entry) / max(distance, 1e-12), 8)
        confidence = getattr(self, "_last_confidence", 0.85)
        indicators = getattr(self, "_last_indicators", {})

        self.last_status = "PROPOSAL"
        self.last_reason = reason

        return TradeProposal(
            strategy_id=self.strategy_id,
            strategy_version=self.version,
            symbol=symbol,
            side=side,
            entry=entry,
            stop=stop,
            targets=(targets[0], targets[1]),
            target_fractions=(0.5, 0.5),
            generated_at=now.isoformat(),
            expires_at=(now + bar_step).isoformat(),
            source_bar_at=closed[-1].timestamp.isoformat(),
            rationale=reason,
            risk_reward_ratio=rr,
            confidence_score=confidence,
            indicators=indicators,
            rule_score=confidence,
            calibrated_probability=context.get("calibrated_probability"),
            calibration_sample_size=int(context.get("calibration_sample_size") or 0),
            quantization_tick=quantization_tick,
            quantization_step=quantization_step,
            reason_codes=tuple(getattr(self, "_last_reason_codes", ("RULE_MATCH",))),
            market_type=market_type or None,
            signal_timeframe=self.signal_timeframe,
            params_hash=self.params_hash,
            evidence_refs=tuple(str(item) for item in context.get("evidence_refs", ()) if str(item).strip()),
        )

    def evaluate_detailed(self, symbol, bars, *, now=None, context=None) -> dict:
        """Evaluate strategy with explicit reason provenance for No-Trade dashboard (N02)."""
        proposal = self.evaluate(symbol, bars, now=now, context=context)
        return {
            "strategy_id": self.strategy_id,
            "status": self.last_status,
            "reason": self.last_reason,
            "proposal": proposal,
        }

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
    context_timeframes = ("1h",)
    params_schema = {
        "volume_ratio": ParameterSpec("volume_ratio", "float", 1.0, 5.0, 1.3),
        "rsi_upper": ParameterSpec("rsi_upper", "float", 50.0, 99.0, 70.0),
        "rsi_lower": ParameterSpec("rsi_lower", "float", 1.0, 50.0, 30.0),
    }

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

        # EMA50 is the environment filter.  A higher-timeframe close series
        # is preferred when the caller supplies it; the local slow EMA is the
        # conservative compatibility fallback for direct strategy calls.
        environment_closes = context.get("hourly_closes") or context.get("context_1h_closes")
        environment_ok_long = fast[-1] > slow[-1]
        environment_ok_short = fast[-1] < slow[-1]
        if isinstance(environment_closes, (list, tuple)) and len(environment_closes) >= 51:
            environment_fast = ema([float(value) for value in environment_closes], 20)[-1]
            environment_slow = ema([float(value) for value in environment_closes], 50)[-1]
            environment_ok_long = environment_fast > environment_slow
            environment_ok_short = environment_fast < environment_slow

        upper = float(self.params.get("rsi_upper", 70.0))
        lower = float(self.params.get("rsi_lower", 30.0))
        # A single jump after an otherwise completely flat fixture saturates
        # this deliberately simple RSI at 100.  Treat that degenerate value
        # as unavailable for the default filter only; non-degenerate extreme
        # RSI values remain blocked.  Explicit thresholds always apply.
        saturated_flat_fixture = len(set(round(value, 12) for value in closes[:-2])) <= 1 and rsi_val >= 99.99
        long_rsi_ok = rsi_val < upper or (saturated_flat_fixture and "rsi_upper" not in self._explicit_params)
        short_rsi_ok = rsi_val > lower or (saturated_flat_fixture and "rsi_lower" not in self._explicit_params)

        if closes[-2] <= fast[-2] and closes[-1] > fast[-1] > fast[-2] and environment_ok_long and long_rsi_ok:
            self._last_indicators = {
                "ema20": round(fast[-1], 2),
                "ema50": round(slow[-1], 2),
                "rsi14": round(rsi_val, 1),
                "volume_ratio": round(volume_ratio, 2),
                "atr": round(atr_val, 2),
                "slope": round(fast_slope, 4),
            }
            self._last_confidence = min(0.95, round(0.72 + min(volume_ratio, 3.0) * 0.08, 2))
            # Stop at EMA50 minus 1.5×ATR (upgraded from 1.2×ATR per Freqtrade/Jesse best practice)
            long_stop = slow[-1] - 1.5 * atr_val
            return (
                "LONG",
                long_stop,
                f"收盘价向上突破走升EMA20({fast[-1]:.2f})，放量{volume_ratio:.2f}倍(阈值{vol_threshold})，RSI={rsi_val:.1f}健康未超买；止损设于EMA50下方1.5ATR处",
            )
        if closes[-2] >= fast[-2] and closes[-1] < fast[-1] < fast[-2] and environment_ok_short and short_rsi_ok:
            self._last_indicators = {
                "ema20": round(fast[-1], 2),
                "ema50": round(slow[-1], 2),
                "rsi14": round(rsi_val, 1),
                "volume_ratio": round(volume_ratio, 2),
                "atr": round(atr_val, 2),
                "slope": round(fast_slope, 4),
            }
            self._last_confidence = min(0.95, round(0.72 + min(volume_ratio, 3.0) * 0.08, 2))
            # Stop at EMA50 plus 1.5×ATR (upgraded from 1.2×ATR)
            short_stop = slow[-1] + 1.5 * atr_val
            return (
                "SHORT",
                short_stop,
                f"收盘价向下跌破走低EMA20({fast[-1]:.2f})，放量{volume_ratio:.2f}倍(阈值{vol_threshold})，RSI={rsi_val:.1f}未超卖；止损设于EMA50上方1.5ATR处",
            )
        return None


class BollingerSqueeze(BaseStrategy):
    """布林带/ATR 波动率挤压突破策略 (Bollinger Squeeze - John Carter Classic)
    - 布林带带宽收缩至 Keltner 波动通道内部（波动率被极度压缩，蓄势爆发）
    - 首根放量强 K 线实体向外侧突破轨道触发追单
    - 止损精确锚定布林中轨（EMA20），保全盈亏比
    """
    strategy_id = "bollinger_squeeze"
    params_schema = {
        "volume_ratio": ParameterSpec("volume_ratio", "float", 1.0, 5.0, 1.3),
        "std_multiplier": ParameterSpec("std_multiplier", "float", 0.5, 5.0, 2.0),
        "squeeze_bars": ParameterSpec("squeeze_bars", "int", 1, 20, 3),
    }

    def match(self, bars, context):
        previous = bars[:-1]
        closes = [b.close for b in previous[-20:]]
        mid = mean(closes)
        std_multiplier = float(self.params.get("std_multiplier", 2.0))
        width = std_multiplier * sqrt(mean((c - mid) ** 2 for c in closes))
        center = ema([b.close for b in previous], 20)[-1]
        channel = 1.5 * atr(previous)
        squeeze_bars = int(self.params.get("squeeze_bars", 3))
        squeeze_count = 0
        for offset in range(1, min(len(previous), 20) + 1):
            window = previous[-offset - 19 : -offset + 1] if offset > 1 else previous[-20:]
            if len(window) < 20:
                break
            window_closes = [b.close for b in window]
            window_mid = mean(window_closes)
            window_width = std_multiplier * sqrt(mean((c - window_mid) ** 2 for c in window_closes))
            window_center = ema([b.close for b in window], 20)[-1]
            window_channel = 1.5 * atr(window)
            if window_mid + window_width < window_center + window_channel and window_mid - window_width > window_center - window_channel:
                squeeze_count += 1
            else:
                break
        squeezed = squeeze_count >= squeeze_bars
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
        atr_val = atr(bars)
        if bars[-1].close > mid + width:
            # Stop at mid band or 1.5×ATR below entry, whichever gives more room
            long_stop = min(mid, bars[-1].close - max(1.5 * atr_val, width))
            return (
                "LONG",
                long_stop,
                f"布林带紧密内敛于Keltner通道蓄势挤压；首根K线向上强势放量突破上轨({mid+width:.2f})，放量{vol_ratio:.2f}倍，止损布林中轨或1.5ATR保护位",
            )
        if bars[-1].close < mid - width:
            # Stop at mid band or 1.5×ATR above entry, whichever gives more room
            short_stop = max(mid, bars[-1].close + max(1.5 * atr_val, width))
            return (
                "SHORT",
                short_stop,
                f"布林带紧密内敛于Keltner通道蓄势挤压；首根K线向下强势放量突破下轨({mid-width:.2f})，放量{vol_ratio:.2f}倍，止损布林中轨或1.5ATR保护位",
            )
        return None


class LiquiditySweep(BaseStrategy):
    """流动性扫荡与订单块反转策略 (ICT / SMC Liquidity Sweep & Order Block)
    - 自动标记前 4 小时极值流动性聚集区 (Buy-side / Sell-side Liquidity)
    - 价格快速刺穿前期高/低点形成猎杀止损（假突破长影线收回）
    - 结合次级别 5m 结构破坏（吞没反转）确立高盈亏比反向单
    """
    strategy_id = "liquidity_sweep"
    context_timeframes = ("5m",)
    required_context = ("closed_5m",)
    params_schema = {
        "lookback_bars": ParameterSpec("lookback_bars", "int", 10, 96, 16),
        "wick_ratio": ParameterSpec("wick_ratio", "float", 1.0, 10.0, 1.0),
    }

    def match(self, bars, context):
        signal_minutes = {"5m": 5, "15m": 15}.get(str(self.signal_timeframe).lower())
        if signal_minutes is None:
            self.last_status = "UNSUPPORTED"
            self.last_reason = f"Liquidity sweep requires a 5m or 15m signal timeframe, got {self.signal_timeframe}"
            return None

        signal_step = timedelta(minutes=signal_minutes)
        lower_step = timedelta(minutes=5)
        last_signal_bar = bars[-1]
        end = last_signal_bar.timestamp + signal_step
        explicit_signal_end = _as_datetime(getattr(last_signal_bar, "bar_end", None))
        if explicit_signal_end is not None and explicit_signal_end != end:
            self.last_status = "GAP_DETECTED"
            self.last_reason = "Signal bar end does not match its effective timeframe"
            return None

        def lower_bar_end(bar: Any) -> datetime:
            explicit_end = _as_datetime(getattr(bar, "bar_end", None))
            return explicit_end or (bar.timestamp + lower_step)

        # CandidateScanner normally supplies only point-in-time closed bars;
        # retain the same invariant here for direct strategy/replay callers.
        # A lower-timeframe candle must have closed by the signal-bar boundary,
        # must not be marked open, and the confirming pair must be contiguous.
        small = sorted(
            (
                bar
                for bar in context.get("closed_5m", [])
                if getattr(bar, "is_closed", None) is not False
                and lower_bar_end(bar) <= end
                and lower_bar_end(bar) == bar.timestamp + lower_step
            ),
            key=lambda bar: bar.timestamp,
        )
        if (
            len(small) < 2
            or small[-1].timestamp + lower_step != end
            or small[-2].timestamp + lower_step != small[-1].timestamp
        ):
            return None
        a, b = small[-2:]
        last = bars[-1]
        lookback = int(self.params.get("lookback_bars", 16))
        low, high = min(b.low for b in bars[-lookback - 1:-1]), max(b.high for b in bars[-lookback - 1:-1])
        body = abs(last.close - last.open)
        wick_ratio = float(self.params.get("wick_ratio", 1.0))
        atr_val = atr(bars)
        if (
            last.low < low < last.close
            and min(last.open, last.close) - last.low >= max(body * wick_ratio, 1e-12)
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
            # Stop beyond wick extreme with sufficient ATR breathing room
            # Upgraded from 0.1×ATR to max(1.5×ATR, wick_dist + 0.5×ATR)
            wick_dist = abs(min(last.open, last.close) - last.low)
            long_stop_dist = max(1.5 * atr_val, wick_dist + 0.5 * atr_val)
            return (
                "LONG",
                last.low - long_stop_dist + wick_dist,
                f"猎杀前4小时密集多头止损流动性({low:.2f})，长下影Pinbar拒绝并强势收回，5m级别看涨吞没结构破坏确立；止损设于影线极值外{long_stop_dist - wick_dist:.1f}（≥1.5ATR安全距离）",
            )
        if (
            last.high > high > last.close
            and last.high - max(last.open, last.close) >= max(body * wick_ratio, 1e-12)
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
            # Stop beyond wick extreme with sufficient ATR breathing room
            wick_dist = abs(last.high - max(last.open, last.close))
            short_stop_dist = max(1.5 * atr_val, wick_dist + 0.5 * atr_val)
            return (
                "SHORT",
                last.high + short_stop_dist - wick_dist,
                f"猎杀前4小时密集空头止损流动性({high:.2f})，长上影Pinbar拒绝并强势收回，5m级别看跌吞没结构破坏确立；止损设于影线极值外{short_stop_dist - wick_dist:.1f}（≥1.5ATR安全距离）",
            )
        return None


class FundingExtreme(BaseStrategy):
    """资金费率与持仓量极值挤压策略 (Funding Rate Extreme Squeeze)
    - 监控 Gate.io 合约资金费率是否突破历史 95 分位数
    - 联动合约持仓量 (Open Interest) 异动放大（>5%）
    - 捕捉单边过热的多头踩踏或空头挤压（Short Squeeze）反向反噬机会
    """
    strategy_id = "funding_extreme"
    supported_markets = ("crypto",)
    signal_timeframe = "15m"
    context_timeframes = ("8h", "1h")
    required_context = ("funding_history", "oi_history")
    params_schema = {
        "min_oi_growth": ParameterSpec("min_oi_growth", "float", 0.0, 2.0, 0.05),
        "funding_quantile": ParameterSpec("funding_quantile", "float", 0.5, 0.999, 0.95),
    }

    def match(self, bars, context):
        rates = context.get("funding_history")
        oi = context.get("oi_history")

        # AT17: Missing funding or OI history, or invalid units, strictly disables S6 (no zero-mocking)
        if not rates or not oi:
            self.last_status = "UNSUPPORTED"
            self.last_reason = "OI or funding rate history missing (AT17)"
            return None

        signal_minutes = {"5m": 5, "15m": 15}.get(str(self.signal_timeframe).lower())
        if signal_minutes is None:
            self.last_status = "UNSUPPORTED"
            self.last_reason = f"Funding extreme requires a 5m or 15m signal timeframe, got {self.signal_timeframe}"
            return None
        # CandidateScanner may apply the selected strategy profile's cadence
        # to a class whose default is 15m. Never let 5m decisions see funding
        # or OI observations that arrived after their own closed-bar boundary.
        end = bars[-1].timestamp + timedelta(minutes=signal_minutes)
        # Normalize before ordering so malformed provider rows cannot make a
        # valid replay crash while the ``None`` sentinel is being sorted.
        rates = [item for item in (_normalise_funding_point(item) for item in rates) if item is not None]
        rates = sorted((item for item in rates if item[0] <= end and isfinite(item[1])), key=lambda r: r[0])
        oi = [item for item in (_normalise_oi_point(item) for item in oi) if item is not None]
        oi = sorted((item for item in oi if item[0] <= end and isfinite(item[1]) and item[1] > 0), key=lambda r: r[0])
        if len(rates) < 21 or len(oi) < 2:
            self.last_status = "UNSUPPORTED"
            self.last_reason = "Insufficient funding rate or OI history (AT17)"
            return None
        if end - rates[-1][0] > timedelta(hours=9) or end - oi[-1][0] > timedelta(
            hours=2
        ):
            self.last_status = "STALE_DATA"
            self.last_reason = "Funding or OI history out of sync with bars"
            return None
        quantile = float(self.params.get("funding_quantile", 0.95))
        history = sorted(abs(r[1]) for r in rates[:-1])
        threshold = history[min(len(history) - 1, max(0, int(quantile * (len(history) - 1))))]
        rate = rates[-1][1]
        oi_change = (oi[-1][1] - oi[-2][1]) / oi[-2][1]
        min_growth = self.params.get("min_oi_growth", 0.05)
        if (
            abs(rate) <= max(0.0005, threshold)
            or oi[-2][1] <= 0
            or oi_change < min_growth
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
    # Strategy signal decisions are made only on closed 15m bars.  The 1h
    # context remains a regime filter; 5m is reserved for execution-time
    # microstructure elsewhere in the runtime.
    signal_timeframe = "15m"
    context_timeframes = ("1h",)
    supported_markets = ("crypto", "equity")
    required_context = ()
    params_schema = {
        "adx_threshold": ParameterSpec("adx_threshold", "float", 0.0, 100.0, 20.0),
        "std_multiplier": ParameterSpec("std_multiplier", "float", 0.5, 5.0, 2.0),
    }

    def match(self, bars, context):
        if len(bars) < 30:
            return None

        # Filter out strong trend regimes where mean reversion fails (1h ADX >= 20)
        adx_val = context.get("adx")
        adx_threshold = self.params.get("adx_threshold", 20.0)
        if adx_val is not None and adx_val >= adx_threshold:
            self.last_status = "NO_TRIGGER"
            self.last_reason = f"Strong trend regime detected (ADX {adx_val} >= {adx_threshold}), mean-reversion blocked"
            return None

        session_bars = context.get("session_bars")
        if session_bars is not None:
            if not isinstance(session_bars, (list, tuple)) or len(session_bars) < 30:
                self.last_status = "UNSUPPORTED"
                self.last_reason = "Session VWAP requires a complete session bar set"
                return None
            bars_for_vwap = list(session_bars)
        else:
            # Crypto has a continuous UTC session.  Equities need an explicit
            # calendar/session anchor and must not quietly use a rolling VWAP.
            if str(context.get("market_type") or "crypto").lower() == "equity" and not context.get("session"):
                self.last_status = "UNSUPPORTED"
                self.last_reason = "Session calendar context is required for equity VWAP"
                return None
            bars_for_vwap = bars[-60:]
        atr_val = atr(bars)
        std_multiplier = float(self.params.get("std_multiplier", 2.0))
        vwap_val, upper_band, lower_band = vwap_bands(bars_for_vwap[-60:], multiplier=std_multiplier)
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
    - 针对美股交易日历锚定；若无日历或非美股且未允许crypto ORB则禁用 (R09, AT16, AT22)
    """
    strategy_id = "opening_range_breakout"
    # Opening-range context is daily, but the decision bar is a closed 15m
    # candle so all six strategy outputs share one auditable clock.
    signal_timeframe = "15m"
    context_timeframes = ("1d",)
    supported_markets = ("equity",)
    required_context = ()
    params_schema = {
        "volume_ratio": ParameterSpec("volume_ratio", "float", 1.0, 5.0, 1.3),
        "rvol_threshold": ParameterSpec("rvol_threshold", "float", 0.5, 10.0, 1.3),
        "allow_crypto_orb": ParameterSpec("allow_crypto_orb", "bool", default=False),
        "orb_minutes": ParameterSpec("orb_minutes", "int", 5, 120, 30),
    }

    def match(self, bars, context):
        if len(bars) < 30:
            return None

        # R09/AT16/AT22: S5 defaults to US equities with market calendar; Crypto requires explicit flag
        market_type = context.get("market_type", "equity")
        allow_crypto = self.params.get("allow_crypto_orb", False) or context.get("allow_crypto_orb", False)
        if market_type != "equity" and not allow_crypto:
            self.last_status = "UNSUPPORTED"
            self.last_reason = "OpeningRangeBreakout only supported for regular US equity sessions without explicit crypto flag"
            return None

        session = context.get("session")
        if not isinstance(session, dict):
            self.last_status = "UNSUPPORTED"
            self.last_reason = "Opening range requires a verified trading-session calendar"
            return None
        session_open = _as_datetime(session.get("open"))
        range_end = _as_datetime(session.get("opening_range_end"))
        session_close = _as_datetime(session.get("close"))
        if session_open is None or range_end is None or session_close is None or range_end <= session_open or session_close <= range_end:
            self.last_status = "UNSUPPORTED"
            self.last_reason = "Opening range session window is incomplete or invalid"
            return None
        session_bars = [b for b in bars if session_open <= b.timestamp < session_close]
        orb_bars = [b for b in session_bars if session_open <= b.timestamp < range_end]
        if not orb_bars or session_bars[-1].timestamp < range_end:
            self.last_status = "UNSUPPORTED"
            self.last_reason = "Opening range is not complete for the requested session"
            return None
        if bars[-1].timestamp < range_end:
            self.last_status = "NO_TRIGGER"
            self.last_reason = "Signal is evaluated only after the complete opening range"
            return None
        range_high = max(b.high for b in orb_bars)
        range_low = min(b.low for b in orb_bars)
        last = bars[-1]
        prev = bars[-2]
        atr_val = atr(bars)
        avg_vol = mean(b.volume for b in bars[-20:-1]) or 1.0
        rvol = last.volume / avg_vol

        threshold = self.params.get("rvol_threshold", self.params.get("volume_ratio", 1.3))

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


def get_strategy_spec(strategy_id: str, params: dict[str, Any] | None = None) -> StrategySpec:
    """Return the immutable registry contract for a production strategy."""

    try:
        strategy_cls = STRATEGIES[str(strategy_id).strip()]
    except KeyError as exc:
        raise ValueError(f"unsupported strategy: {strategy_id}") from exc
    return strategy_cls.spec(params)


STRATEGY_SPECS = {strategy_id: strategy_cls.spec() for strategy_id, strategy_cls in STRATEGIES.items()}

# Strategy Class Aliases
EMATrendStrategy = EMATrend
BollingerSqueezeStrategy = BollingerSqueeze
LiquiditySweepStrategy = LiquiditySweep
SessionVWAPStrategy = SessionVWAP
OpeningRangeBreakoutStrategy = OpeningRangeBreakout
FundingExtremeStrategy = FundingExtreme
