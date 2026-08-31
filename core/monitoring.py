"""Python-owned crypto monitoring pipeline for V1.2.

The module keeps all financial calculations, trigger decisions, dedupe,
cooldown, and output validation on the backend.  Ollama is called only after a
closed-bar trigger has passed the deterministic gate, and the Smart tier is
never replaced by the Fast tier when it is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Protocol

from .ai.contracts import LLMError
from .ai.ollama import OllamaProvider
from .alerts import AlertReconciler
from .instruments import AssetType, Instrument
from .model_routing import DEFAULT_SMART_MODEL, ModelRoutingConfig
from .providers.base import Bar, MarketProvider, ProviderError
from .providers.runtime import MarketDataBundle, ProviderChain, build_default_provider, fetch_market_data
from .signals import Action, SignalProposal
from .storage import SQLiteStore


MONITORING_CONTRACT_VERSION = "monitoring_policy_v1"
TRIGGER_POLICY_VERSION = "trigger_policy_v2"
OPPORTUNITY_CONTRACT_VERSION = "opportunity_analysis_v1"
OPPORTUNITY_PROMPT_VERSION = "opportunity_analysis_v1"
REALTIME_CONTRACT_VERSION = "crypto_realtime_v1"
SUPPORTED_TIMEFRAMES = frozenset({"15m", "1h"})
SUPPORTED_TRIGGER_TYPES = (
    "REGIME_CHANGE",
    "BREAKOUT",
    "BREAKDOWN",
    "VOLUME_EXPANSION",
    "VOLATILITY_EXPANSION",
    "LEVEL_PROXIMITY",
    "SIGNAL_INVALIDATION",
    "EVENT_RISK",
    "NEWS_SHOCK",
)

_TRIGGER_TYPE_ALIASES = {
    "REGIME": "REGIME_CHANGE",
    "REGIME_CHANGE": "REGIME_CHANGE",
    "BREAKOUT": "BREAKOUT",
    "BREAKDOWN": "BREAKDOWN",
    "VOLUME": "VOLUME_EXPANSION",
    "VOLUME_EXPANSION": "VOLUME_EXPANSION",
    "VOLATILITY": "VOLATILITY_EXPANSION",
    "VOLATILITY_EXPANSION": "VOLATILITY_EXPANSION",
    "LEVEL_PROXIMITY": "LEVEL_PROXIMITY",
    "INVALIDATION": "SIGNAL_INVALIDATION",
    "SIGNAL_INVALIDATION": "SIGNAL_INVALIDATION",
    "EVENT_RISK": "EVENT_RISK",
    "NEWS_SHOCK": "NEWS_SHOCK",
}


def normalize_trigger_type(value: object) -> str:
    """Return the canonical V1.2 trigger name while accepting V1.2 aliases."""

    normalized = str(value).strip().upper().replace("-", "_").replace(" ", "_")
    try:
        return _TRIGGER_TYPE_ALIASES[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported trigger type: {value}") from exc


class MonitoringError(RuntimeError):
    """A stable monitoring failure that can be surfaced without raw traces."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class SmartModelUnavailable(MonitoringError):
    def __init__(self, message: str = "the configured Smart model is unavailable") -> None:
        super().__init__(message, code="SMART_MODEL_UNAVAILABLE")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _finite(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError("value must be a finite number")
    return float(value)


def _digest(value: object, *, length: int = 32) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def _interval_minutes(timeframe: str) -> int:
    if timeframe == "15m":
        return 15
    if timeframe == "1h":
        return 60
    raise ValueError(f"unsupported monitoring timeframe: {timeframe}")


@dataclass(frozen=True, slots=True)
class MonitoringPolicy:
    instrument_id: str
    enabled: bool = False
    primary_timeframe: str = "15m"
    context_timeframe: str = "1h"
    trigger_types: tuple[str, ...] = SUPPORTED_TRIGGER_TYPES
    min_trigger_score: float = 0.65
    ai_min_confidence: float = 0.60
    cooldown_minutes: int = 60
    quiet_hours: dict[str, object] = field(default_factory=dict)
    notify: dict[str, object] = field(default_factory=lambda: {"desktop": True, "sound": False})
    notify_in_app: bool | None = None
    notify_native_notification: bool | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, str):
            raise ValueError("instrument_id must be a string")
        symbol = self.instrument_id.strip().upper()
        if not symbol:
            raise ValueError("instrument_id is required")
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        if self.primary_timeframe not in SUPPORTED_TIMEFRAMES or self.context_timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError("monitoring timeframes must be 15m or 1h")
        if self.primary_timeframe != "15m":
            raise ValueError("primary_timeframe must be 15m")
        if not isinstance(self.trigger_types, (list, tuple)):
            raise ValueError("trigger_types must be a list or tuple")
        trigger_types = tuple(dict.fromkeys(normalize_trigger_type(item) for item in self.trigger_types if str(item).strip()))
        if not trigger_types or any(item not in SUPPORTED_TRIGGER_TYPES for item in trigger_types):
            raise ValueError("trigger_types contains an unsupported trigger")
        if not isinstance(self.notify, dict):
            raise ValueError("notify must be an object")
        notify_in_app = self.notify.get("in_app", self.notify.get("app", True)) if self.notify_in_app is None else self.notify_in_app
        notify_native = self.notify.get("native_notification", self.notify.get("desktop", True)) if self.notify_native_notification is None else self.notify_native_notification
        if type(notify_in_app) is not bool or type(notify_native) is not bool:
            raise ValueError("notification preferences must be boolean")
        min_trigger_score = _finite(self.min_trigger_score)
        ai_min_confidence = _finite(self.ai_min_confidence)
        if not 0.0 <= min_trigger_score <= 1.0:
            raise ValueError("min_trigger_score must be in [0, 1]")
        if not 0.0 <= ai_min_confidence <= 1.0:
            raise ValueError("ai_min_confidence must be in [0, 1]")
        if isinstance(self.cooldown_minutes, bool) or not isinstance(self.cooldown_minutes, int) or not 1 <= self.cooldown_minutes <= 24 * 60:
            raise ValueError("cooldown_minutes must be between 1 and 1440")
        object.__setattr__(self, "instrument_id", symbol)
        object.__setattr__(self, "trigger_types", trigger_types)
        object.__setattr__(self, "min_trigger_score", min_trigger_score)
        object.__setattr__(self, "ai_min_confidence", ai_min_confidence)
        object.__setattr__(self, "notify_in_app", notify_in_app)
        object.__setattr__(self, "notify_native_notification", notify_native)

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "MonitoringPolicy":
        if not isinstance(payload, Mapping):
            raise ValueError("monitoring policy must be an object")
        allowed = {
            "contract_version",
            "instrument_id", "symbol", "enabled", "primary_timeframe", "context_timeframe",
            "trigger_types", "min_trigger_score", "ai_min_confidence", "cooldown_minutes",
            "quiet_hours", "notify", "notify_preferences", "notify_in_app", "notify_native_notification",
            "created_at", "updated_at",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"unsupported monitoring policy fields: {', '.join(sorted(unknown))}")
        trigger_types = payload.get("trigger_types", list(SUPPORTED_TRIGGER_TYPES))
        if not isinstance(trigger_types, (list, tuple)):
            raise ValueError("trigger_types must be a list")
        quiet = payload.get("quiet_hours", {})
        notify = payload.get("notify", payload.get("notify_preferences", {"desktop": True, "sound": False}))
        if not isinstance(quiet, dict) or not isinstance(notify, dict):
            raise ValueError("quiet_hours and notify must be objects")
        raw_notify_in_app = payload.get("notify_in_app", notify.get("in_app", notify.get("app", True)))
        raw_notify_native = payload.get("notify_native_notification", notify.get("native_notification", notify.get("desktop", True)))
        if type(raw_notify_in_app) is not bool or type(raw_notify_native) is not bool:
            raise ValueError("notification preferences must be boolean")
        raw_instrument_id = payload.get("instrument_id", payload.get("symbol"))
        if not isinstance(raw_instrument_id, str) or not raw_instrument_id.strip():
            raise ValueError("instrument_id must be a non-empty string")
        enabled = payload.get("enabled", False)
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        raw_min_score = payload.get("min_trigger_score", 0.65)
        raw_ai_confidence = payload.get("ai_min_confidence", 0.60)
        if isinstance(raw_min_score, bool) or not isinstance(raw_min_score, (int, float)):
            raise ValueError("min_trigger_score must be a number")
        if isinstance(raw_ai_confidence, bool) or not isinstance(raw_ai_confidence, (int, float)):
            raise ValueError("ai_min_confidence must be a number")
        raw_cooldown = payload.get("cooldown_minutes", 60)
        if isinstance(raw_cooldown, bool) or not isinstance(raw_cooldown, int):
            raise ValueError("cooldown_minutes must be an integer")
        return cls(
            instrument_id=raw_instrument_id,
            enabled=enabled,
            primary_timeframe=str(payload.get("primary_timeframe", "15m")),
            context_timeframe=str(payload.get("context_timeframe", "1h")),
            trigger_types=tuple(str(item) for item in trigger_types),
            min_trigger_score=raw_min_score,
            ai_min_confidence=raw_ai_confidence,
            cooldown_minutes=raw_cooldown,
            quiet_hours=dict(quiet),
            notify=dict(notify),
            notify_in_app=raw_notify_in_app,
            notify_native_notification=raw_notify_native,
            created_at=str(payload["created_at"]) if payload.get("created_at") else None,
            updated_at=str(payload["updated_at"]) if payload.get("updated_at") else None,
        )

    @classmethod
    def defaults(cls, instrument_id: str) -> "MonitoringPolicy":
        return cls(instrument_id=instrument_id, enabled=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": MONITORING_CONTRACT_VERSION,
            "instrument_id": self.instrument_id,
            "enabled": self.enabled,
            "primary_timeframe": self.primary_timeframe,
            "context_timeframe": self.context_timeframe,
            "trigger_types": list(self.trigger_types),
            "min_trigger_score": self.min_trigger_score,
            "ai_min_confidence": self.ai_min_confidence,
            "cooldown_minutes": self.cooldown_minutes,
            "quiet_hours": dict(self.quiet_hours),
            "notify": {
                **dict(self.notify),
                "in_app": self.notify_in_app,
                "native_notification": self.notify_native_notification,
                # Keep the old key stable for V1.1 clients and storage rows.
                "desktop": self.notify_native_notification,
            },
            "notify_in_app": self.notify_in_app,
            "notify_native_notification": self.notify_native_notification,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class BarClose:
    symbol: str
    timeframe: str
    bar_start: datetime
    bar_end: datetime

    @property
    def key(self) -> str:
        return f"{self.symbol.upper()}:{self.timeframe}:{_iso(self.bar_start)}"

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol.upper(),
            "timeframe": self.timeframe,
            "bar_start": _iso(self.bar_start),
            "bar_end": _iso(self.bar_end),
            "key": self.key,
        }


class BarCloseCoordinator:
    """Claim each closed bar exactly once, including across process restarts."""

    def __init__(self, store: SQLiteStore | None = None, *, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._memory: set[str] = set()
        self._lock = threading.Lock()

    def close_for(self, bar: Bar, *, symbol: str, timeframe: str) -> BarClose:
        start = _as_utc(bar.timestamp)
        end = start + timedelta(minutes=_interval_minutes(timeframe))
        return BarClose(symbol.strip().upper(), timeframe, start, end)

    def claim(self, bar: Bar, *, symbol: str, timeframe: str, now: datetime | None = None) -> BarClose | None:
        close = self.close_for(bar, symbol=symbol, timeframe=timeframe)
        point = _as_utc(now or self.clock())
        if close.bar_end > point:
            return None
        with self._lock:
            if close.key in self._memory:
                return None
            if self.store is not None and not self.store.claim_bar_close(
                close.symbol,
                close.timeframe,
                _iso(close.bar_start),
                _iso(close.bar_end),
                closed_at=close.bar_end,
                processed_at=point,
            ):
                self._memory.add(close.key)
                return None
            self._memory.add(close.key)
            return close


class RealtimeBarCache:
    """Bounded in-memory cache used by chart/monitoring requests."""

    def __init__(self, *, max_symbols: int = 50, max_bars_per_symbol: int = 500) -> None:
        self.max_symbols = max(1, min(int(max_symbols), 50))
        self.max_bars_per_symbol = max(60, min(int(max_bars_per_symbol), 2000))
        self._values: OrderedDict[tuple[str, str], list[Bar]] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, symbol: str, timeframe: str, bars: Iterable[Bar]) -> None:
        key = (symbol.strip().upper(), timeframe.lower())
        ordered: dict[datetime, Bar] = {}
        for bar in bars:
            ordered[_as_utc(bar.timestamp)] = bar
        with self._lock:
            self._values[key] = sorted(ordered.values(), key=lambda item: _as_utc(item.timestamp))[-self.max_bars_per_symbol :]
            self._values.move_to_end(key)
            while len({item[0] for item in self._values}) > self.max_symbols:
                self._values.popitem(last=False)

    def get(self, symbol: str, timeframe: str) -> tuple[Bar, ...]:
        key = (symbol.strip().upper(), timeframe.lower())
        with self._lock:
            values = self._values.get(key, [])
            if values:
                self._values.move_to_end(key)
            return tuple(values)

    def resource(self) -> dict[str, object]:
        with self._lock:
            symbols = sorted({symbol for symbol, _ in self._values})
            bars = sum(len(value) for value in self._values.values())
        return {
            "max_symbols": self.max_symbols,
            "active_symbols": len(symbols),
            "symbols": symbols,
            "max_bars_per_symbol": self.max_bars_per_symbol,
            "cached_bars": bars,
            "model_concurrency": 1,
        }


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    current = values[0]
    for value in values[1:]:
        current = alpha * value + (1.0 - alpha) * current
    return current


def _atr(bars: list[Bar], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    true_ranges: list[float] = []
    for previous, current in zip(bars, bars[1:]):
        true_ranges.append(max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close)))
    return sum(true_ranges[-period:]) / min(period, len(true_ranges))


def trigger_candidates(
    bars: Iterable[Bar],
    *,
    symbol: str,
    timeframe: str = "15m",
    trigger_types: Iterable[str] = SUPPORTED_TRIGGER_TYPES,
    min_score: float = 0.65,
    event_risk: bool = False,
    news_shock: bool = False,
) -> list[dict[str, object]]:
    """Calculate deterministic trigger candidates from closed-bar evidence."""

    ordered = sorted(list(bars), key=lambda item: _as_utc(item.timestamp))
    if len(ordered) < 20:
        return []
    allowed = {normalize_trigger_type(item) for item in trigger_types}
    last = ordered[-1]
    closes = [bar.close for bar in ordered]
    previous = ordered[-21:-1]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, min(50, len(closes)))
    atr = _atr(ordered)
    avg_volume = sum(bar.volume for bar in previous) / max(1, len(previous))
    high_level = max(bar.high for bar in previous)
    low_level = min(bar.low for bar in previous)
    distance_to_high = max(0.0, (high_level - last.close) / max(last.close, 1e-12))
    distance_to_low = max(0.0, (last.close - low_level) / max(last.close, 1e-12))
    base = {
        "symbol": symbol.strip().upper(),
        "timeframe": timeframe,
        "bar_start": _iso(_as_utc(last.timestamp)),
        "bar_end": _iso(_as_utc(last.timestamp) + timedelta(minutes=_interval_minutes(timeframe))),
        "close": round(last.close, 12),
        "ema20": round(ema20, 12),
        "ema50": round(ema50, 12),
        "atr": round(atr, 12),
        "average_volume": round(avg_volume, 12),
        "volume": round(last.volume, 12),
        "support": round(low_level, 12),
        "resistance": round(high_level, 12),
    }
    raw: list[tuple[str, float, str, dict[str, object]]] = []
    trend_up = last.close > ema20 > ema50
    trend_down = last.close < ema20 < ema50
    if "REGIME_CHANGE" in allowed and (trend_up or trend_down):
        distance = abs(last.close - ema20) / max(last.close, 1e-12)
        raw.append(("REGIME_CHANGE", min(0.99, 0.66 + distance * 8.0), "trend regime aligned with EMA20/EMA50", {"direction": "up" if trend_up else "down"}))
    if "BREAKOUT" in allowed and last.close > high_level:
        raw.append(("BREAKOUT", min(0.99, 0.72 + (last.close - high_level) / max(last.close, 1e-12) * 12.0), "closed above the prior range high", {"level": high_level}))
    if "BREAKDOWN" in allowed and last.close < low_level:
        raw.append(("BREAKDOWN", min(0.99, 0.72 + (low_level - last.close) / max(last.close, 1e-12) * 12.0), "closed below the prior range low", {"level": low_level}))
    if "VOLUME_EXPANSION" in allowed and avg_volume > 0 and last.volume >= avg_volume * 1.5:
        raw.append(("VOLUME_EXPANSION", min(0.99, 0.65 + (last.volume / avg_volume - 1.5) * 0.15), "closed volume expanded against the recent mean", {"volume_ratio": last.volume / avg_volume}))
    if "VOLATILITY_EXPANSION" in allowed and last.close > 0 and atr / last.close >= 0.008:
        raw.append(("VOLATILITY_EXPANSION", min(0.99, 0.65 + atr / last.close * 8.0), "ATR expansion crossed the volatility threshold", {"atr_ratio": atr / last.close}))
    if "LEVEL_PROXIMITY" in allowed and (distance_to_high <= 0.006 or distance_to_low <= 0.006):
        raw.append(("LEVEL_PROXIMITY", min(0.99, 0.66 + (0.006 - min(distance_to_high, distance_to_low)) * 30), "price is close to a deterministic range level", {"distance_to_high": distance_to_high, "distance_to_low": distance_to_low}))
    previous_closes = closes[:-1]
    previous_ema20 = _ema(previous_closes, min(20, len(previous_closes)))
    previous_trend_up = ordered[-2].close > previous_ema20
    previous_trend_down = ordered[-2].close < previous_ema20
    if "SIGNAL_INVALIDATION" in allowed and ((previous_trend_up and last.close < ema20) or (previous_trend_down and last.close > ema20)):
        raw.append(("SIGNAL_INVALIDATION", 0.78, "closed through the active EMA invalidation level", {"direction": "down" if previous_trend_up else "up"}))
    if "EVENT_RISK" in allowed and event_risk:
        raw.append(("EVENT_RISK", 0.90, "Python event context reports elevated event risk", {"event_risk": True}))
    if "NEWS_SHOCK" in allowed and news_shock:
        raw.append(("NEWS_SHOCK", 0.90, "Python news context reports a news shock", {"news_shock": True}))
    candidates: list[dict[str, object]] = []
    for trigger_type, score, reason, evidence in raw:
        if score < min_score:
            continue
        payload = {**base, "trigger_type": trigger_type, "trigger_score": round(score, 6), "reason": reason, "evidence": evidence, "policy_version": TRIGGER_POLICY_VERSION}
        fingerprint = _digest(payload, length=48)
        candidates.append({
            "trigger_event_id": f"trigger-{fingerprint[:24]}",
            "instrument_id": base["symbol"],
            "timeframe": timeframe,
            "bar_start": base["bar_start"],
            "bar_end": base["bar_end"],
            "trigger_type": trigger_type,
            "trigger_score": score,
            "fingerprint": fingerprint,
            "policy_version": TRIGGER_POLICY_VERSION,
            "payload": payload,
        })
    return candidates


@dataclass(frozen=True, slots=True)
class OpportunityAnalysis:
    symbol: str
    timeframe: str
    bias: str
    confidence: float
    regime: str
    trigger_event_id: str
    evidence: tuple[object, ...]
    news_context: tuple[object, ...]
    event_risk: bool
    watch_zone: dict[str, float] | None
    invalidation: tuple[str, ...]
    invalidation_price: float | None
    targets: tuple[float, ...]
    holding_horizon: str
    re_evaluate_at: datetime
    model_id: str
    prompt_version: str
    data_as_of: datetime
    missing_evidence: tuple[str, ...]
    raw_model_response: str | None = None

    def __post_init__(self) -> None:
        if self.bias not in {"LONG_WATCH", "SHORT_WATCH", "WAIT"}:
            raise ValueError("bias must be LONG_WATCH, SHORT_WATCH, or WAIT")
        if not 0.0 <= self.confidence <= 1.0 or not math.isfinite(self.confidence):
            raise ValueError("confidence must be in [0, 1]")
        if self.model_id != DEFAULT_SMART_MODEL and not self.model_id.endswith(":9b"):
            raise ValueError("OpportunityAnalysis requires the Smart 9B model")
        if self.timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError("opportunity timeframe is unsupported")
        if self.data_as_of.tzinfo is None or self.re_evaluate_at.tzinfo is None:
            raise ValueError("opportunity timestamps must be timezone-aware")
        if self.watch_zone is not None:
            low = _finite(self.watch_zone.get("low"))
            high = _finite(self.watch_zone.get("high"))
            if low <= 0 or high <= 0 or low > high:
                raise ValueError("watch_zone is invalid")
        if any(not math.isfinite(item) or item <= 0 for item in self.targets):
            raise ValueError("targets must be positive finite numbers")
        if self.bias != "WAIT" and (self.watch_zone is None or len(self.targets) < 2 or not self.invalidation):
            raise ValueError("directional opportunities require validated zone, targets, and invalidation")
        if self.bias == "WAIT" and (self.watch_zone is not None or self.targets):
            raise ValueError("WAIT must not include actionable levels")

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
        *,
        symbol: str,
        timeframe: str,
        trigger_event_id: str,
        model_id: str,
        data_as_of: datetime,
        raw_model_response: str | None = None,
        stale: bool = False,
    ) -> "OpportunityAnalysis":
        if not isinstance(payload, Mapping):
            raise ValueError("OpportunityAnalysis output must be an object")
        bias = str(payload.get("bias") or "WAIT").strip().upper()
        confidence = _finite(payload.get("confidence", payload.get("confidence_raw", 0.0)))
        if confidence > 1.0 and confidence <= 100.0:
            confidence /= 100.0
        if stale and confidence > 0.65:
            raise ValueError("stale data cannot produce high-confidence OpportunityAnalysis")
        zone_raw = payload.get("watch_zone")
        watch_zone: dict[str, float] | None = None
        if zone_raw is not None:
            if not isinstance(zone_raw, Mapping):
                raise ValueError("watch_zone must be an object or null")
            watch_zone = {"low": _finite(zone_raw.get("low")), "high": _finite(zone_raw.get("high"))}
        target_raw = payload.get("targets", [])
        if isinstance(target_raw, Mapping):
            target_values = target_raw.get("prices", target_raw.get("values", []))
        else:
            target_values = target_raw
        if not isinstance(target_values, (list, tuple)):
            raise ValueError("targets must be a list")
        targets = tuple(_finite(item) for item in target_values)
        invalidation_raw = payload.get("invalidation", [])
        invalidation_price: float | None = None
        if isinstance(invalidation_raw, Mapping):
            price_raw = invalidation_raw.get("price")
            invalidation_price = _finite(price_raw) if price_raw is not None else None
            raw_conditions = invalidation_raw.get("conditions", [])
            if not isinstance(raw_conditions, (list, tuple)):
                raise ValueError("invalidation.conditions must be a list")
            invalidation = tuple(str(item).strip() for item in raw_conditions if str(item).strip())
        elif isinstance(invalidation_raw, str):
            invalidation = (invalidation_raw.strip(),) if invalidation_raw.strip() else ()
        elif isinstance(invalidation_raw, list):
            invalidation = tuple(str(item).strip() for item in invalidation_raw if str(item).strip())
        else:
            raise ValueError("invalidation must be a string or list")
        if "invalidation_price" in payload and payload.get("invalidation_price") is not None:
            invalidation_price = _finite(payload.get("invalidation_price"))
        reevaluate_raw = payload.get("re_evaluate_at", payload.get("reevaluate_at"))
        if not isinstance(reevaluate_raw, str):
            raise ValueError("re_evaluate_at is required")
        try:
            reevaluate = _as_utc(datetime.fromisoformat(reevaluate_raw.replace("Z", "+00:00")))
        except ValueError as exc:
            raise ValueError("re_evaluate_at is invalid") from exc
        evidence = payload.get("evidence", [])
        news_context = payload.get("news_context", [])
        missing = payload.get("missing_evidence", [])
        if not isinstance(evidence, (list, tuple)) or not isinstance(news_context, (list, tuple)) or not isinstance(missing, (list, tuple)):
            raise ValueError("evidence, news_context, and missing_evidence must be lists")
        return cls(
            symbol=symbol.strip().upper(),
            timeframe=timeframe,
            bias=bias,
            confidence=confidence,
            regime=str(payload.get("regime") or "unknown"),
            trigger_event_id=trigger_event_id,
            evidence=tuple(evidence),
            news_context=tuple(news_context),
            event_risk=bool(payload.get("event_risk", False)),
            watch_zone=watch_zone,
            invalidation=invalidation,
            invalidation_price=invalidation_price,
            targets=targets,
            holding_horizon=str(payload.get("holding_horizon") or "next_4h"),
            re_evaluate_at=reevaluate,
            model_id=model_id,
            prompt_version=str(payload.get("prompt_version") or OPPORTUNITY_PROMPT_VERSION),
            data_as_of=_as_utc(data_as_of),
            missing_evidence=tuple(str(item) for item in missing),
            raw_model_response=raw_model_response,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": OPPORTUNITY_CONTRACT_VERSION,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bias": self.bias,
            "confidence": self.confidence,
            "regime": self.regime,
            "trigger_event_id": self.trigger_event_id,
            "evidence": list(self.evidence),
            "news_context": list(self.news_context),
            "event_risk": self.event_risk,
            "watch_zone": dict(self.watch_zone) if self.watch_zone else None,
            "invalidation": list(self.invalidation),
            "invalidation_price": self.invalidation_price,
            "targets": list(self.targets),
            "holding_horizon": self.holding_horizon,
            "re_evaluate_at": _iso(self.re_evaluate_at),
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "data_as_of": _iso(self.data_as_of),
            "missing_evidence": list(self.missing_evidence),
        }


class OpportunityModel(Protocol):
    def generate_json(self, messages: list[dict[str, str]], *, model_name: str, prompt_version: str, input_hash: str) -> tuple[dict[str, object], str, dict[str, object]]:
        ...


def _opportunity_messages(
    event: Mapping[str, object],
    bars: list[Bar],
    *,
    symbol: str,
    timeframe: str,
    data_as_of: datetime,
    context_bars: list[Bar] | None = None,
) -> list[dict[str, str]]:
    last = bars[-1]
    recent = bars[-8:]
    evidence = {
        "symbol": symbol,
        "timeframe": timeframe,
        "data_as_of": _iso(data_as_of),
        "trigger": dict(event),
        "bars": [{"timestamp": _iso(_as_utc(item.timestamp)), "open": item.open, "high": item.high, "low": item.low, "close": item.close, "volume": item.volume} for item in recent],
        "context_1h_bars": [
            {"timestamp": _iso(_as_utc(item.timestamp)), "open": item.open, "high": item.high, "low": item.low, "close": item.close, "volume": item.volume}
            for item in (context_bars or [])[-6:]
        ],
        "output_schema": {
            "bias": "LONG_WATCH | SHORT_WATCH | WAIT",
            "confidence": "number from 0 to 1",
            "regime": "short string",
            "watch_zone": "{low: positive number, high: positive number} or null",
            "invalidation": "list of concise conditions; empty list for WAIT",
            "invalidation_price": "positive number or null",
            "targets": "list of positive prices in directional order; empty list for WAIT",
            "holding_horizon": "next_4h",
            "re_evaluate_at": "ISO-8601 timestamp with timezone",
            "evidence": "list of observed facts",
            "news_context": "list; use empty list when news is unavailable",
            "event_risk": "boolean",
            "missing_evidence": "list of unavailable facts",
        },
        "instructions": "Return JSON only, with exactly the output_schema fields. Use bias LONG_WATCH, SHORT_WATCH, or WAIT. If the evidence does not justify a directional watch, choose WAIT and set watch_zone, invalidation_price, and targets to null/empty values. For LONG_WATCH use low <= high, stop below the entry zone, and targets above it; for SHORT_WATCH use stop above the entry zone and targets below it. Python validates every level. Do not invent news; state missing evidence. Keep evidence concise.",
    }
    return [
        {"role": "system", "content": "You are the Smart 9B opportunity explanation layer. Never place orders or claim certainty. The Python backend owns calculations, levels, risk, and trigger policy."},
        {"role": "user", "content": json.dumps(evidence, ensure_ascii=False, sort_keys=True)},
    ]


class SmartOpportunityAnalyzer:
    """Call exactly the configured Smart model and validate its JSON output."""

    def __init__(self, llm_provider: object | None, *, smart_model: str | None = None) -> None:
        self.llm_provider = llm_provider
        self.smart_model = smart_model or ModelRoutingConfig.from_env().smart_model
        if not self.smart_model.lower().endswith(":9b"):
            raise ValueError("OpportunityAnalysis requires a configured Smart 9B model")

    def _check_available(self) -> None:
        if self.llm_provider is None:
            raise SmartModelUnavailable()
        health = getattr(self.llm_provider, "health", None)
        if callable(health):
            try:
                status = health()
            except Exception as exc:
                raise SmartModelUnavailable() from exc
            if isinstance(status, Mapping):
                models = status.get("models")
                # An Ollama health response with no exact model inventory is
                # not evidence that the required 9B is installed.  Fail
                # closed so the Smart tier can never be relabelled as Fast.
                if isinstance(models, list) and self.smart_model not in {str(item) for item in models}:
                    raise SmartModelUnavailable()
                if status.get("available") is False or status.get("model_available") is False:
                    raise SmartModelUnavailable()

    def analyze(
        self,
        event: Mapping[str, object],
        bars: list[Bar],
        *,
        symbol: str,
        timeframe: str,
        data_as_of: datetime,
        stale: bool = False,
        context_bars: list[Bar] | None = None,
    ) -> tuple[OpportunityAnalysis, dict[str, object]]:
        self._check_available()
        messages = _opportunity_messages(event, bars, symbol=symbol, timeframe=timeframe, data_as_of=data_as_of, context_bars=context_bars)
        input_hash = hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        provider = self.llm_provider
        raw: str | None = None
        metadata: dict[str, object] = {}
        payload: object
        if isinstance(provider, OllamaProvider):
            try:
                payload, raw, metadata = provider.generate_json(
                    messages,
                    model_name=self.smart_model,
                    prompt_version=OPPORTUNITY_PROMPT_VERSION,
                    input_hash=input_hash,
                    temperature=0.0,
                )
            except LLMError as exc:
                if exc.code in {"MODEL_UNAVAILABLE", "model_http_error", "model_invalid_envelope"}:
                    raise SmartModelUnavailable() from exc
                raise MonitoringError("Smart model call failed", code="SMART_MODEL_CALL_FAILED") from exc
        else:
            method = getattr(provider, "generate_json", None) or getattr(provider, "analyze_opportunity", None) or getattr(provider, "complete_json", None)
            if not callable(method):
                raise SmartModelUnavailable("the injected Smart provider has no structured JSON interface")
            try:
                result = method(messages, model_name=self.smart_model, prompt_version=OPPORTUNITY_PROMPT_VERSION, input_hash=input_hash)
            except TypeError:
                result = method(event, bars, symbol=symbol, timeframe=timeframe, model_id=self.smart_model)
            if isinstance(result, tuple):
                payload = result[0]
                raw = str(result[1]) if len(result) > 1 and result[1] is not None else None
                metadata = dict(result[2]) if len(result) > 2 and isinstance(result[2], Mapping) else {}
            else:
                payload = result
        if isinstance(payload, str):
            raw = payload
            payload = json.loads(payload)
        if not isinstance(payload, Mapping):
            raise MonitoringError("Smart output is not a JSON object", code="SMART_OUTPUT_INVALID")
        mutable = dict(payload)
        supplied_model = mutable.get("model_id")
        if supplied_model is not None and str(supplied_model) != self.smart_model:
            raise MonitoringError("Smart output model_id does not match the configured 9B model", code="SMART_MODEL_MISMATCH")
        mutable["model_id"] = self.smart_model
        mutable.setdefault("prompt_version", OPPORTUNITY_PROMPT_VERSION)
        analysis = OpportunityAnalysis.from_payload(
            mutable,
            symbol=symbol,
            timeframe=timeframe,
            trigger_event_id=str(event["trigger_event_id"]),
            model_id=self.smart_model,
            data_as_of=data_as_of,
            raw_model_response=raw,
            stale=stale,
        )
        metadata.update({"model_id": self.smart_model, "prompt_version": analysis.prompt_version, "input_hash": input_hash, "raw_response": raw, "parse_status": "valid"})
        return analysis, metadata


@dataclass(frozen=True, slots=True)
class MonitoringRunResult:
    status: str
    as_of: datetime
    policies: tuple[dict[str, object], ...]
    items: tuple[dict[str, object], ...]
    resource: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": MONITORING_CONTRACT_VERSION,
            "status": self.status,
            "as_of": _iso(self.as_of),
            "policies": list(self.policies),
            "items": list(self.items),
            "resource": self.resource,
            "capabilities": {
                "financial_calculations_owner": "python",
                "trigger_policy": TRIGGER_POLICY_VERSION,
                "bar_close_exactly_once": True,
                "smart_fallback_to_fast": False,
                "real_orders": False,
                "external_notifications": False,
            },
        }


class MonitoringService:
    """Explicit, bounded monitoring execution; construction has no side effects."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        provider_factory: Callable[[Instrument], object] = build_default_provider,
        llm_provider: object | None = None,
        clock: Callable[[], datetime] | None = None,
        max_symbols: int | None = None,
    ) -> None:
        self.store = store
        self.provider_factory = provider_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_symbols = max(1, min(int(max_symbols or os.environ.get("MAX_MONITOR_SYMBOLS", "50")), 50))
        self.cache = RealtimeBarCache(max_symbols=self.max_symbols)
        self.coordinator = BarCloseCoordinator(store, clock=self.clock)
        self.smart = SmartOpportunityAnalyzer(llm_provider)
        self._model_lock = threading.Lock()

    def list_policies(self, *, enabled: bool | None = None) -> list[dict[str, object]]:
        return self.store.list_monitoring_policies(enabled=enabled)

    def upsert_policy(self, policy: MonitoringPolicy) -> dict[str, object]:
        stored = self.store.upsert_monitoring_policy(policy.to_dict())
        return MonitoringPolicy.from_payload(stored).to_dict()

    def _provider_bundle(self, instrument: Instrument, timeframe: str, limit: int) -> MarketDataBundle:
        provider = self.provider_factory(instrument)
        if isinstance(provider, ProviderChain):
            return provider.get_bundle(instrument, timeframe, limit)
        return fetch_market_data(provider, instrument, timeframe, limit)

    def _realtime_state(self, instrument: Instrument, bundle: MarketDataBundle, *, now: datetime, reconnect_count: int = 0) -> dict[str, object]:
        age = max(0.0, (now - _as_utc(bundle.quote.timestamp)).total_seconds())
        stale = bool(bundle.snapshot.stale) or age > 120
        state = {
            "contract_version": REALTIME_CONTRACT_VERSION,
            "symbol": instrument.symbol,
            "provider": bundle.snapshot.provider,
            "price": bundle.quote.price,
            "change_pct": bundle.quote.change_pct,
            "volume": bundle.quote.volume,
            "last_trade_at": _iso(bundle.quote.timestamp),
            "data_as_of": _iso(bundle.snapshot.data_as_of),
            "freshness_status": "stale" if stale else "fresh",
            "stale_after_seconds": 120,
            "reconnect_count": reconnect_count,
            "last_error": bundle.snapshot.error_code,
            "age_seconds": round(age, 3),
        }
        self.store.save_realtime_state(state, now=now)
        return state

    @staticmethod
    def _cooldown_active(events: list[dict[str, object]], *, trigger_type: str, now: datetime, cooldown_minutes: int) -> bool:
        threshold = now - timedelta(minutes=cooldown_minutes)
        for event in events:
            if str(event.get("trigger_type")) != trigger_type:
                continue
            created = event.get("created_at")
            if isinstance(created, str):
                try:
                    if _as_utc(datetime.fromisoformat(created.replace("Z", "+00:00"))) >= threshold:
                        return True
                except ValueError:
                    continue
        return False

    def _prediction_from_opportunity(self, analysis: OpportunityAnalysis, *, instrument: Instrument, trigger: Mapping[str, object], bars: list[Bar], now: datetime) -> SignalProposal:
        generated_at = _as_utc(now)
        action = Action.LONG if analysis.bias == "LONG_WATCH" else Action.SHORT if analysis.bias == "SHORT_WATCH" else Action.WAIT
        entry_low = entry_high = stop = tp1 = tp2 = None
        if action is not Action.WAIT:
            assert analysis.watch_zone is not None
            entry_low = analysis.watch_zone["low"]
            entry_high = analysis.watch_zone["high"]
            entry = (entry_low + entry_high) / 2.0
            risk = max(_atr(bars), entry * 0.005)
            stop = analysis.invalidation_price
            if stop is None:
                stop = entry - risk if action is Action.LONG else entry + risk
            tp1, tp2 = analysis.targets[:2]
            if action is Action.LONG and not stop < entry < tp1 <= tp2:
                raise MonitoringError("Python rejected LONG opportunity levels", code="OPPORTUNITY_LEVEL_INVALID")
            if action is Action.SHORT and not stop > entry > tp1 >= tp2:
                raise MonitoringError("Python rejected SHORT opportunity levels", code="OPPORTUNITY_LEVEL_INVALID")
            actual_risk = entry - stop if action is Action.LONG else stop - entry
            reward = tp1 - entry if action is Action.LONG else entry - tp1
            if actual_risk <= 0 or reward <= 0 or reward / actual_risk < 1.5 - 1e-9:
                raise MonitoringError("Python rejected opportunity because TP1 R:R is below 1.5", code="OPPORTUNITY_RR_INVALID")
        reevaluate = max(analysis.re_evaluate_at, generated_at + timedelta(minutes=15))
        horizon = 240 if analysis.holding_horizon in {"next_4h", "4h"} else 60
        max_hold = max(horizon, 720)
        return SignalProposal(
            prediction_id=f"prediction-{analysis.trigger_event_id}",
            instrument=instrument,
            analysis_timeframe=analysis.timeframe,
            generated_at=generated_at,
            action=action,
            entry_low=entry_low,
            entry_high=entry_high,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            signal_validity_minutes=max(15, int((reevaluate - generated_at).total_seconds() // 60)),
            expected_hold_minutes=horizon,
            max_hold_minutes=max_hold,
            reevaluate_at=reevaluate,
            invalidation=analysis.invalidation or ("re-evaluate after the next closed 15m bar",),
            raw_confidence=analysis.confidence,
            reason_codes=(str(trigger.get("trigger_type")), TRIGGER_POLICY_VERSION),
            summary=f"{analysis.bias}: Python-validated Smart opportunity for {analysis.symbol}.",
            model_id=analysis.model_id,
            model_version=analysis.model_id,
            prompt_version=analysis.prompt_version,
            input_hash=_digest({"trigger": trigger, "bars": [_iso(_as_utc(item.timestamp)) for item in bars]}),
            data_as_of=analysis.data_as_of,
            context_json=json.dumps({"trigger": trigger, "opportunity": analysis.to_dict()}, sort_keys=True, ensure_ascii=False),
            raw_model_response=analysis.raw_model_response,
            parse_status="SMART_VALIDATED",
            source_type="live",
        )

    def _save_annotations(self, analysis: OpportunityAnalysis, *, trigger: Mapping[str, object], bars: list[Bar], prediction: SignalProposal, now: datetime) -> None:
        annotations: list[dict[str, object]] = [{
            "annotation_id": f"annotation-{_digest({'trigger': analysis.trigger_event_id, 'type': 'trigger'}, length=24)}",
            "symbol": analysis.symbol,
            "timeframe": analysis.timeframe,
            "annotation_type": "trigger",
            "bar_start": trigger.get("bar_start"),
            "price": bars[-1].close,
            "label": str(trigger.get("trigger_type") or "trigger"),
            "source": "python",
            "payload": {"trigger_event_id": analysis.trigger_event_id, "trigger_type": trigger.get("trigger_type"), "score": trigger.get("trigger_score")},
        }]
        trigger_payload = trigger.get("payload") if isinstance(trigger.get("payload"), Mapping) else {}
        for annotation_type, label, value in (
            ("support", "support", trigger_payload.get("support")),
            ("resistance", "resistance", trigger_payload.get("resistance")),
        ):
            if isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) > 0:
                annotations.append({
                    "annotation_id": f"annotation-{_digest({'trigger': analysis.trigger_event_id, 'type': annotation_type}, length=24)}",
                    "symbol": analysis.symbol,
                    "timeframe": analysis.timeframe,
                    "annotation_type": annotation_type,
                    "bar_start": trigger.get("bar_start"),
                    "price": float(value),
                    "label": label,
                    "source": "python",
                    "payload": {"trigger_event_id": analysis.trigger_event_id, "level": float(value)},
                })
        if analysis.watch_zone:
            for label, price in (("watch_low", analysis.watch_zone["low"]), ("watch_high", analysis.watch_zone["high"])):
                annotations.append({
                    "annotation_id": f"annotation-{_digest({'trigger': analysis.trigger_event_id, 'type': label}, length=24)}",
                    "symbol": analysis.symbol,
                    "timeframe": analysis.timeframe,
                    "annotation_type": "watch_zone",
                    "bar_start": trigger.get("bar_start"),
                    "price": price,
                    "label": label,
                    "source": "python",
                    "payload": {"trigger_event_id": analysis.trigger_event_id, "level": price},
                })
        for annotation_type, label, price in (
            ("stop", "stop", prediction.stop),
            ("target", "target_1", prediction.tp1),
            ("target", "target_2", prediction.tp2),
        ):
            if price is None:
                continue
            annotations.append({
                "annotation_id": f"annotation-{_digest({'trigger': analysis.trigger_event_id, 'type': label}, length=24)}",
                "symbol": analysis.symbol,
                "timeframe": analysis.timeframe,
                "annotation_type": annotation_type,
                "bar_start": trigger.get("bar_start"),
                "price": price,
                "label": label,
                "source": "python",
                "payload": {"trigger_event_id": analysis.trigger_event_id, "level": price, "action": prediction.action.value},
            })
        self.store.save_chart_annotations(annotations, now=now)

    def _run_policy(self, policy: MonitoringPolicy, *, now: datetime) -> dict[str, object]:
        try:
            instrument = self.store.resolve_instrument(policy.instrument_id)
        except (TypeError, ValueError) as exc:
            return {"symbol": policy.instrument_id, "status": "ERROR", "error_code": "UNSUPPORTED_INSTRUMENT", "error": str(exc)}
        if instrument.asset_type is not AssetType.CRYPTO:
            return {"symbol": instrument.symbol, "status": "SKIPPED", "skip_reason": "CRYPTO_ONLY"}
        try:
            bundle = self._provider_bundle(instrument, policy.primary_timeframe, 240)
            bars = list(bundle.bars)
            context_bundle = self._provider_bundle(instrument, policy.context_timeframe, 120)
            context_bars = list(context_bundle.bars)
            self.cache.put(instrument.symbol, policy.primary_timeframe, bars)
            self.cache.put(instrument.symbol, policy.context_timeframe, context_bars)
            realtime = self._realtime_state(instrument, bundle, now=now)
            self.store.upsert_market_bars(instrument.symbol, policy.primary_timeframe, bars, provider=bundle.snapshot.provider, data_as_of=bundle.snapshot.data_as_of, now=now)
            self.store.upsert_market_bars(instrument.symbol, policy.context_timeframe, context_bars, provider=context_bundle.snapshot.provider, data_as_of=context_bundle.snapshot.data_as_of, now=now)
        except (ProviderError, ValueError) as exc:
            provider = getattr(exc, "provider", None) or "unknown"
            code = getattr(exc, "code", "provider_error")
            self.store.save_realtime_state({"symbol": instrument.symbol, "provider": provider, "freshness_status": "unavailable", "stale_after_seconds": 120, "last_error": code}, now=now)
            return {"symbol": instrument.symbol, "status": "DEGRADED", "error_code": code, "provider": provider}
        if not bars:
            return {"symbol": instrument.symbol, "status": "DEGRADED", "error_code": "NO_BARS", "realtime": realtime}
        bar_interval = timedelta(minutes=_interval_minutes(policy.primary_timeframe))
        closed_bars = [item for item in bars if _as_utc(item.timestamp) + bar_interval <= now]
        if not closed_bars:
            return {"symbol": instrument.symbol, "status": "WAITING_FOR_CLOSED_BAR", "realtime": realtime}
        latest = sorted(closed_bars, key=lambda item: _as_utc(item.timestamp))[-1]
        close = self.coordinator.claim(latest, symbol=instrument.symbol, timeframe=policy.primary_timeframe, now=now)
        if close is None:
            return {"symbol": instrument.symbol, "status": "NO_NEW_CLOSED_BAR", "realtime": realtime, "latest_bar": _iso(_as_utc(latest.timestamp))}
        candidates = trigger_candidates(closed_bars, symbol=instrument.symbol, timeframe=policy.primary_timeframe, trigger_types=policy.trigger_types, min_score=policy.min_trigger_score)
        if not candidates:
            return {"symbol": instrument.symbol, "status": "CLOSED_BAR_NO_TRIGGER", "bar_close": close.to_dict(), "realtime": realtime}
        existing_events = self.store.list_trigger_events(symbol=instrument.symbol, limit=100)
        event_results: list[dict[str, object]] = []
        for candidate in candidates:
            inserted = self.store.insert_trigger_event(candidate, now=now)
            event = {key: value for key, value in inserted.items() if key != "created"}
            if not bool(inserted.get("created")):
                event["status"] = "DEDUPED"
                event_results.append(event)
                continue
            trigger_type = str(candidate["trigger_type"])
            if self._cooldown_active(existing_events, trigger_type=trigger_type, now=now, cooldown_minutes=policy.cooldown_minutes):
                self.store.update_trigger_event(str(candidate["trigger_event_id"]), status="COOLDOWN", analysis_status="SKIPPED_COOLDOWN", now=now)
                event["status"] = "COOLDOWN"
                event_results.append(event)
                continue
            self.store.update_trigger_event(str(candidate["trigger_event_id"]), status="TRIGGERED", analysis_status="REQUESTED", now=now)
            try:
                with self._model_lock:
                    analysis, metadata = self.smart.analyze(
                        candidate,
                        closed_bars,
                        symbol=instrument.symbol,
                        timeframe=policy.primary_timeframe,
                        data_as_of=bundle.data_as_of,
                        stale=(
                            bool(bundle.snapshot.stale)
                            or bool(context_bundle.snapshot.stale)
                            or str(realtime.get("freshness_status")) != "fresh"
                            or (now - _as_utc(bundle.snapshot.data_as_of)).total_seconds() > 120
                        ),
                        context_bars=context_bars,
                    )
                analysis_record = {
                    "analysis_id": f"opportunity-{analysis.trigger_event_id}",
                    "trigger_event_id": analysis.trigger_event_id,
                    "instrument_id": analysis.symbol,
                    "timeframe": analysis.timeframe,
                    "bias": analysis.bias,
                    "confidence": analysis.confidence,
                    "model_id": analysis.model_id,
                    "prompt_version": analysis.prompt_version,
                    "data_as_of": _iso(analysis.data_as_of),
                    "payload": analysis.to_dict(),
                    "raw_model_response": metadata.get("raw_response"),
                }
                try:
                    prediction = self._prediction_from_opportunity(analysis, instrument=instrument, trigger=candidate, bars=closed_bars, now=now)
                except (MonitoringError, ValueError):
                    # Keep the raw Smart response and structured evidence for
                    # audit, but never mark an analysis VALID before Python
                    # accepts its actionable levels and risk semantics.
                    self.store.save_opportunity_analysis({**analysis_record, "validator_status": "QUARANTINED"}, now=now)
                    raise
                saved_analysis = self.store.save_opportunity_analysis({**analysis_record, "validator_status": "VALID"}, now=now)
                if analysis.confidence >= policy.ai_min_confidence:
                    self.store.save_prediction(prediction)
                    self.store.save_model_run(prediction_id=prediction.prediction_id, model_id=analysis.model_id, started_at=_iso(now), latency_ms=float(metadata.get("latency_ms")) if metadata.get("latency_ms") is not None else None, input_tokens_est=int(metadata.get("input_tokens_est")) if metadata.get("input_tokens_est") is not None else None, output_chars=int(metadata.get("output_chars")) if metadata.get("output_chars") is not None else None, success=True)
                    self._save_annotations(analysis, trigger=candidate, bars=bars, prediction=prediction, now=now)
                    try:
                        AlertReconciler(store=self.store, clock=self.clock).run_once(as_of=now)
                    except Exception:
                        pass
                    self.store.update_trigger_event(str(candidate["trigger_event_id"]), status="ANALYZED", analysis_status="PREDICTION_SAVED", now=now)
                    event.update({"status": "ANALYZED", "analysis_status": "PREDICTION_SAVED", "analysis": saved_analysis, "prediction_id": prediction.prediction_id, "model": metadata})
                else:
                    self.store.update_trigger_event(str(candidate["trigger_event_id"]), status="ANALYZED", analysis_status="BELOW_POLICY_CONFIDENCE", now=now)
                    event.update({"status": "BELOW_POLICY_CONFIDENCE", "analysis_status": "BELOW_POLICY_CONFIDENCE", "analysis": saved_analysis, "model": metadata})
            except SmartModelUnavailable as exc:
                self.store.update_trigger_event(str(candidate["trigger_event_id"]), status="DEGRADED", analysis_status="SMART_UNAVAILABLE", now=now)
                event.update({"status": "DEGRADED", "analysis_status": exc.code, "smart_fallback": False, "error": str(exc)[:240]})
            except (MonitoringError, ValueError, json.JSONDecodeError) as exc:
                self.store.update_trigger_event(str(candidate["trigger_event_id"]), status="QUARANTINED", analysis_status="VALIDATION_FAILED", now=now)
                event.update({"status": "QUARANTINED", "analysis_status": getattr(exc, "code", "SMART_OUTPUT_INVALID"), "error": str(exc)[:240]})
            event_results.append(event)
        return {"symbol": instrument.symbol, "status": "TRIGGERED", "bar_close": close.to_dict(), "realtime": realtime, "events": event_results}

    def run(self, *, symbols: Iterable[str] | None = None, now: datetime | None = None) -> MonitoringRunResult:
        point = _as_utc(now or self.clock())
        policies = [MonitoringPolicy.from_payload(item) for item in self.store.list_monitoring_policies(enabled=True)]
        requested = {str(symbol).strip().upper() for symbol in symbols or () if str(symbol).strip()}
        if requested:
            policies = [policy for policy in policies if policy.instrument_id in requested]
        if len(policies) > self.max_symbols:
            policies = policies[: self.max_symbols]
        if not policies:
            return MonitoringRunResult("DISABLED", point, tuple(), tuple(), self.cache.resource())
        items: list[dict[str, object]] = []
        for policy in policies:
            items.append(self._run_policy(policy, now=point))
        statuses = {str(item.get("status")) for item in items}
        status = "COMPLETED" if statuses <= {"NO_NEW_CLOSED_BAR", "CLOSED_BAR_NO_TRIGGER", "TRIGGERED"} else "COMPLETED_WITH_ERRORS"
        return MonitoringRunResult(status, point, tuple(policy.to_dict() for policy in policies), tuple(items), self.cache.resource())


__all__ = [
    "BarClose",
    "BarCloseCoordinator",
    "MONITORING_CONTRACT_VERSION",
    "MonitoringError",
    "MonitoringPolicy",
    "MonitoringRunResult",
    "MonitoringService",
    "OPPORTUNITY_CONTRACT_VERSION",
    "OpportunityAnalysis",
    "RealtimeBarCache",
    "REALTIME_CONTRACT_VERSION",
    "SmartModelUnavailable",
    "SmartOpportunityAnalyzer",
    "SUPPORTED_TRIGGER_TYPES",
    "TRIGGER_POLICY_VERSION",
    "normalize_trigger_type",
    "trigger_candidates",
]
