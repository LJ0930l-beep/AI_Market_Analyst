"""Bounded, deterministic bar replay for the existing production strategies.

The evaluator in :mod:`strategy_evaluator` measures durable outcomes.  This
module supplies the missing causal input: closed historical bars are replayed
through the same ``STRATEGIES`` registry and a small conservative paper
execution model.  It intentionally does not fit parameters or invent market
data.  A caller can therefore distinguish a real replay from a retrospective
prediction/outcome split.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from typing import Any, Callable

from core.providers.base import Bar
from core.quant.strategies import STRATEGIES


REPLAY_SCHEMA_VERSION = "strategy_bar_replay_v1"


class StrategyReplayError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        point = value
    elif value:
        try:
            point = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    return (point if point.tzinfo is not None else point.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _timeframe_step(timeframe: str) -> timedelta:
    values = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
    seconds = values.get(str(timeframe).lower())
    if seconds is None:
        raise StrategyReplayError("REPLAY_TIMEFRAME_UNSUPPORTED", f"Unsupported replay timeframe: {timeframe}")
    return timedelta(seconds=seconds)


def _normalise_bars(rows: list[dict[str, Any]], *, timeframe: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate closed bars while preserving event and availability times."""
    step = _timeframe_step(timeframe)
    normalised: list[dict[str, Any]] = []
    issues: list[str] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            issues.append("BAR_ROW_INVALID")
            continue
        payload: dict[str, Any] = {}
        raw_payload = raw.get("payload_json")
        if isinstance(raw_payload, str) and raw_payload:
            try:
                decoded = json.loads(raw_payload)
                if isinstance(decoded, dict):
                    payload = decoded
            except json.JSONDecodeError:
                issues.append("BAR_PAYLOAD_INVALID")
        legacy_payload = payload.get("legacy_row") if isinstance(payload.get("legacy_row"), dict) else {}
        def _raw_value(name: str, *aliases: str) -> Any:
            for candidate in (name, *aliases):
                if raw.get(candidate) is not None:
                    return raw.get(candidate)
                if payload.get(candidate) is not None:
                    return payload.get(candidate)
                if legacy_payload.get(candidate) is not None:
                    return legacy_payload.get(candidate)
            return None
        timestamp = _parse_time(_raw_value("bar_start", "timestamp"))
        bar_end = _parse_time(_raw_value("bar_end"))
        if timestamp is None:
            issues.append("BAR_TIMESTAMP_MISSING")
            continue
        if bar_end is None:
            bar_end = timestamp + step
        elif bar_end - timestamp != step:
            issues.append("BAR_INTERVAL_INVALID")
            continue
        key = timestamp.isoformat()
        if key in seen:
            issues.append("BAR_DUPLICATE_TIMESTAMP")
            continue
        seen.add(key)
        if _raw_value("is_closed") not in (1, True, "1", "true", "TRUE"):
            issues.append("OPEN_BAR_EXCLUDED")
            continue
        values = []
        try:
            values = [float(_raw_value(name)) for name in ("open", "high", "low", "close", "volume")]
        except (KeyError, TypeError, ValueError):
            issues.append("BAR_OHLCV_INVALID")
            continue
        if any(not math.isfinite(value) for value in values):
            issues.append("BAR_OHLCV_INVALID")
            continue
        try:
            # Replay inputs are explicitly closed and carry their interval
            # boundary.  Guardian receives a virtual clock from the replay
            # caller; this flag is not inferred for live bars.
            bar = Bar(timestamp, *values, bar_end=bar_end, is_closed=True)
        except (TypeError, ValueError):
            issues.append("BAR_OHLCV_INVALID")
            continue
        data_as_of = _parse_time(_raw_value("data_as_of"))
        received_at = _parse_time(_raw_value("received_at"))
        first_received_at = _parse_time(_raw_value("first_received_at")) or received_at
        available_at = _parse_time(_raw_value("available_at")) or received_at or data_as_of
        fetched_at = _parse_time(_raw_value("fetched_at")) or received_at
        if data_as_of is None:
            issues.append("BAR_DATA_AS_OF_MISSING")
            continue
        if available_at is None:
            issues.append("BAR_AVAILABLE_AT_MISSING")
            continue
        if available_at < data_as_of:
            issues.append("BAR_AVAILABLE_BEFORE_DATA_AS_OF")
            continue
        normalised.append(
            {
                "timestamp": timestamp,
                "bar_end": bar_end,
                "bar": bar,
                "data_as_of": data_as_of,
                "received_at": received_at,
                "event_time": _parse_time(_raw_value("event_time", "event_at")) or timestamp,
                "first_received_at": first_received_at,
                "available_at": available_at,
                "fetched_at": fetched_at,
                "revision_id": _raw_value("revision_id"),
                "quality_status": _raw_value("quality_status") or "VALID",
                "volume_unit": _raw_value("volume_unit"),
                "source": _raw_value("source", "provider"),
                "provider": _raw_value("provider"),
            }
        )
    normalised.sort(key=lambda item: item["timestamp"])
    for previous, current in zip(normalised, normalised[1:]):
        if current["timestamp"] - previous["timestamp"] != step:
            issues.append("BAR_GAP_OR_IRREGULAR_INTERVAL")
            break
    return normalised, sorted(set(issues))


def _replay_context(
    base_context: dict[str, Any],
    builder: Callable[..., dict[str, Any]] | None,
    *,
    as_of: datetime,
    current: dict[str, Any],
    timeframe: str,
) -> dict[str, Any]:
    context = dict(base_context)
    context.setdefault("timeframe", timeframe.lower())
    context.setdefault("as_of", as_of)
    if builder is not None:
        try:
            built = builder(as_of, dict(current))
        except TypeError:
            built = builder(as_of)
        if built is not None:
            if not isinstance(built, dict):
                raise StrategyReplayError("REPLAY_CONTEXT_INVALID", "context_builder must return a dictionary")
            context.update(built)
    return context


def _adverse_price(price: float, side: str, slippage_bps: float, *, entry: bool) -> float:
    slip = max(0.0, slippage_bps) / 10000.0
    if side == "LONG":
        return price * (1.0 + slip if entry else 1.0 - slip)
    return price * (1.0 - slip if entry else 1.0 + slip)


def _pnl(side: str, entry: float, exit_price: float, quantity: float) -> float:
    sign = 1.0 if side == "LONG" else -1.0
    return (exit_price - entry) * quantity * sign


def _trade_from_signal(
    *,
    symbol: str,
    signal_index: int,
    proposal: Any,
    rows: list[dict[str, Any]],
    fee_rate: float,
    slippage_bps: float,
    max_hold_bars: int,
    cancel_check: Callable[[], bool] | None,
    funding_status: str = "NOT_APPLICABLE",
    cost_complete: bool = True,
) -> dict[str, Any] | None:
    """Simulate one proposal with next-bar execution and stop-first bars."""
    entry_index = signal_index + 1
    if entry_index >= len(rows):
        return None
    side = str(proposal.side).upper()
    entry_bar = rows[entry_index]["bar"]
    entry_price = _adverse_price(float(entry_bar.open), side, slippage_bps, entry=True)
    stop = float(proposal.stop)
    targets = [float(value) for value in proposal.targets]
    fractions = [float(value) for value in getattr(proposal, "target_fractions", (0.5, 0.5))]
    if not targets or len(targets) != len(fractions) or any(value <= 0 for value in targets):
        return None
    if side == "LONG" and not (stop < entry_price and all(value > entry_price for value in targets)):
        return None
    if side == "SHORT" and not (stop > entry_price and all(value < entry_price for value in targets)):
        return None

    original_quantity = 1.0
    remaining = original_quantity
    exits: list[dict[str, Any]] = []
    target_done: set[int] = set()
    last_index = min(len(rows) - 1, entry_index + max(1, max_hold_bars))
    max_favorable = 0.0
    max_adverse = 0.0
    exit_reason = "TIME_EXIT"
    for index in range(entry_index, last_index + 1):
        if cancel_check and cancel_check():
            raise StrategyReplayError("REPLAY_CANCELLED", "Strategy replay cancelled before completion.")
        bar = rows[index]["bar"]
        if side == "LONG":
            max_favorable = max(max_favorable, (bar.high - entry_price) / entry_price)
            max_adverse = max(max_adverse, (entry_price - bar.low) / entry_price)
            stop_hit = bar.low <= stop
        else:
            max_favorable = max(max_favorable, (entry_price - bar.low) / entry_price)
            max_adverse = max(max_adverse, (bar.high - entry_price) / entry_price)
            stop_hit = bar.high >= stop
        if stop_hit:
            fill = _adverse_price(min(bar.open, stop) if side == "LONG" else max(bar.open, stop), side, slippage_bps, entry=False)
            exits.append({"price": fill, "quantity": remaining, "type": "STOP", "bar_at": _iso(rows[index]["timestamp"])})
            remaining = 0.0
            exit_reason = "STOP"
            break

        for target_index, (target, fraction) in enumerate(zip(targets, fractions)):
            if target_index in target_done or remaining <= 1e-12:
                continue
            target_hit = bar.high >= target if side == "LONG" else bar.low <= target
            if not target_hit:
                continue
            quantity = min(remaining, original_quantity * fraction)
            if quantity <= 1e-12:
                continue
            fill = _adverse_price(target, side, slippage_bps, entry=False)
            exits.append({"price": fill, "quantity": quantity, "type": f"TP{target_index + 1}", "bar_at": _iso(rows[index]["timestamp"])})
            remaining -= quantity
            target_done.add(target_index)
            exit_reason = f"TP{target_index + 1}"
        if remaining <= 1e-12:
            break
        if index == last_index:
            fill = _adverse_price(float(bar.close), side, slippage_bps, entry=False)
            exits.append({"price": fill, "quantity": remaining, "type": "TIME_EXIT", "bar_at": _iso(rows[index]["timestamp"])})
            remaining = 0.0
            exit_reason = "TIME_EXIT"

    if not exits or remaining > 1e-12:
        return None
    entry_fee = entry_price * original_quantity * max(0.0, fee_rate)
    gross = sum(_pnl(side, entry_price, float(exit_fill["price"]), float(exit_fill["quantity"])) for exit_fill in exits)
    exit_fees = sum(float(exit_fill["price"]) * float(exit_fill["quantity"]) * max(0.0, fee_rate) for exit_fill in exits)
    fees = entry_fee + exit_fees
    risk = abs(entry_price - stop) * original_quantity
    return {
        "trade_id": f"replay:{symbol}:{rows[signal_index]['timestamp'].isoformat()}:{signal_index}",
        "symbol": symbol,
        "side": side,
        "entry_price": entry_price,
        "stop": stop,
        "entry_val": entry_price * original_quantity,
        "quantity": original_quantity,
        "risk": risk,
        "pnl": gross - fees,
        "gross_pnl": gross,
        "fee": fees,
        "slippage_cost": abs(entry_price - float(entry_bar.open)) + sum(
            abs(float(fill["price"]) - float(fill["price"]) / (1.0 - max(0.0, slippage_bps) / 10000.0 if side == "LONG" else 1.0 + max(0.0, slippage_bps) / 10000.0))
            * float(fill["quantity"])
            for fill in exits
        ),
        "funding_fee": 0.0,
        "funding_status": funding_status,
        "cost_complete": cost_complete,
        "opened_at": _iso(rows[entry_index]["timestamp"]),
        "closed_at": str(exits[-1]["bar_at"]),
        "settled_at": str(exits[-1]["bar_at"]),
        "source_bar_at": _iso(rows[signal_index]["timestamp"]),
        "signal_index": signal_index,
        "exit_reason": exit_reason,
        "exit_fills": exits,
        "mae": max_adverse,
        "mfe": max_favorable,
    }


def run_strategy_bar_replay(
    *,
    symbol: str,
    timeframe: str,
    strategy_id: str,
    bars: list[dict[str, Any]],
    params: dict[str, Any] | None = None,
    strategy_version: str | None = None,
    fee_rate: float = 0.0005,
    slippage_bps: float = 2.0,
    signal_start_index: int = 0,
    signal_end_index: int | None = None,
    max_hold_bars: int = 16,
    cancel_check: Callable[[], bool] | None = None,
    context: dict[str, Any] | None = None,
    context_builder: Callable[..., dict[str, Any]] | None = None,
    replay_as_of: datetime | str | None = None,
) -> dict[str, Any]:
    """Replay a bounded closed-bar interval through one existing strategy."""
    symbol = str(symbol).strip().upper()
    strategy_cls = STRATEGIES.get(str(strategy_id).strip())
    if strategy_cls is None:
        return {"status": "NOT_RUN_UNSUPPORTED_STRATEGY", "reason": "STRATEGY_NOT_IN_PRODUCTION_REGISTRY", "strategy_id": strategy_id}
    try:
        fee_rate_value = float(fee_rate)
        slippage_value = float(slippage_bps)
        max_hold_value = int(max_hold_bars)
    except (TypeError, ValueError, OverflowError):
        return {"status": "NOT_RUN_REPLAY_CONFIG_INVALID", "reason": "fee/slippage/max_hold_bars must be numeric", "strategy_id": strategy_id}
    if (
        not math.isfinite(fee_rate_value)
        or not math.isfinite(slippage_value)
        or fee_rate_value < 0
        or slippage_value < 0
        or slippage_value >= 10000
        or max_hold_value < 1
    ):
        return {"status": "NOT_RUN_REPLAY_CONFIG_INVALID", "reason": "fee/slippage/max_hold_bars are outside bounded replay limits", "strategy_id": strategy_id}
    normalised, issues = _normalise_bars(bars, timeframe=timeframe)
    try:
        strategy = strategy_cls(params or {})
    except (TypeError, ValueError, KeyError) as exc:
        return {
            "status": "NOT_RUN_PARAMETER_INVALID",
            "reason": str(exc)[:240],
            "strategy_id": strategy_id,
            "parameters": dict(params or {}),
            "parameters_hash": _hash(dict(params or {})),
        }
    base_context = dict(context or {})
    missing_context = [key for key in getattr(strategy, "required_context", ()) if base_context.get(key) is None]
    if missing_context and context_builder is None:
        return {
            "status": "NOT_RUN_MISSING_CONTEXT",
            "reason": "REQUIRED_CONTEXT_CAPABILITY_NOT_SUPPLIED",
            "missing_context": missing_context,
            "strategy_id": strategy_id,
            "strategy_version": str(getattr(strategy, "version", "UNKNOWN")),
            "parameters": dict(params or {}),
            "parameters_hash": _hash(dict(params or {})),
        }
    actual_version = str(getattr(strategy, "version", "UNKNOWN"))
    if strategy_version and str(strategy_version) != actual_version:
        return {
            "status": "NOT_RUN_STRATEGY_VERSION_MISMATCH",
            "reason": "REQUESTED_VERSION_NOT_EQUAL_TO_PRODUCTION_STRATEGY_VERSION",
            "strategy_id": strategy_id,
            "requested_strategy_version": strategy_version,
            "production_strategy_version": actual_version,
        }
    warmup = int(getattr(strategy, "warmup_bars", 0) or 0)
    step = _timeframe_step(timeframe)
    input_rows = [
        {
            "timestamp": _iso(item["timestamp"]),
            "bar_end": _iso(item["bar_end"]),
            "open": item["bar"].open,
            "high": item["bar"].high,
            "low": item["bar"].low,
            "close": item["bar"].close,
            "volume": item["bar"].volume,
            "data_as_of": _iso(item["data_as_of"]),
            "provider": item["provider"],
            "event_time": _iso(item["event_time"]),
            "first_received_at": _iso(item["first_received_at"]),
            "available_at": _iso(item["available_at"]),
            "fetched_at": _iso(item["fetched_at"]),
            "revision_id": item["revision_id"],
            "quality_status": item["quality_status"],
            "volume_unit": item["volume_unit"],
            "source": item["source"],
        }
        for item in normalised
    ]
    input_hash = _hash(input_rows)
    if issues or len(normalised) < warmup + 2:
        return {
            "status": "NOT_RUN_INSUFFICIENT_CLOSED_BARS",
            "reason": "CLOSED_CONTIGUOUS_BARS_BELOW_STRATEGY_WARMUP_OR_DATA_QUALITY_GATE",
            "strategy_id": strategy_id,
            "strategy_version": actual_version,
            "warmup_bars": warmup,
            "bar_count": len(normalised),
            "issues": issues,
            "input_hash": input_hash,
        }
    requested_replay_as_of = _parse_time(replay_as_of)
    start = max(warmup - 1, int(signal_start_index))
    end = min(len(normalised) - 2, int(signal_end_index) if signal_end_index is not None else len(normalised) - 2)
    if end < start:
        return {
            "status": "NOT_RUN_EMPTY_SIGNAL_INTERVAL",
            "reason": "NO_CLOSED_BAR_SIGNAL_INTERVAL_AFTER_WARMUP",
            "strategy_id": strategy_id,
            "strategy_version": actual_version,
            "warmup_bars": warmup,
            "bar_count": len(normalised),
            "input_hash": input_hash,
        }

    signals: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    availability_exclusions: list[dict[str, str]] = []
    funding_required = "funding_history" in getattr(strategy, "required_context", ())
    for index in range(start, end + 1):
        if cancel_check and cancel_check():
            raise StrategyReplayError("REPLAY_CANCELLED", "Strategy replay cancelled before completion.")
        current = normalised[index]
        as_of = current["timestamp"] + step
        if requested_replay_as_of is not None and as_of > requested_replay_as_of:
            break
        causal_prefix = [
            item for item in normalised[: index + 1]
            if item["available_at"] <= as_of and item["data_as_of"] <= as_of
        ]
        if len(causal_prefix) != index + 1 or not causal_prefix or causal_prefix[-1]["timestamp"] != current["timestamp"]:
            availability_exclusions.append({"bar_start": _iso(current["timestamp"]) or "", "reason": "NOT_AVAILABLE_AT_SIGNAL_AS_OF"})
            continue
        if any(item["timestamp"] - previous["timestamp"] != step for previous, item in zip(causal_prefix, causal_prefix[1:])):
            availability_exclusions.append({"bar_start": _iso(current["timestamp"]) or "", "reason": "CAUSAL_PREFIX_GAP_AFTER_AVAILABILITY_FILTER"})
            continue
        context_payload = _replay_context(
            base_context,
            context_builder,
            as_of=as_of,
            current=current,
            timeframe=timeframe,
        )
        # The strategy receives a strict prefix.  ``now`` is exactly one bar
        # after the last closed bar, so its own stale/future filters remain in
        # force and future rows cannot affect this signal.
        proposal = strategy.evaluate(
            symbol,
            [item["bar"] for item in causal_prefix],
            now=as_of,
            context=context_payload,
        )
        if proposal is None:
            continue
        signal_payload = proposal.to_dict() if hasattr(proposal, "to_dict") else dict(proposal)
        signal_payload.update(
            {
                "signal_index": index,
                "generated_at": _iso(as_of),
                "source_bar_at": _iso(current["timestamp"]),
                "source_data_as_of": _iso(current["data_as_of"]),
                "prefix_input_hash": _hash(input_rows[: index + 1]),
                "execution_timing": "NEXT_BAR_OPEN",
            }
        )
        signals.append(signal_payload)
        trade = _trade_from_signal(
            symbol=symbol,
            signal_index=index,
            proposal=proposal,
            rows=normalised,
            fee_rate=fee_rate_value,
            slippage_bps=slippage_value,
            max_hold_bars=max_hold_value,
            cancel_check=cancel_check,
            funding_status="NOT_AVAILABLE" if funding_required else "NOT_APPLICABLE",
            cost_complete=not funding_required,
        )
        if trade is not None:
            trades.append(trade)

    return {
        "status": "SIGNAL_RESEARCH_ONLY" if funding_required else "EVALUATED",
        "schema_version": REPLAY_SCHEMA_VERSION,
        "strategy_id": strategy_id,
        "strategy_version": actual_version,
        "symbol": symbol,
        "timeframe": timeframe,
        "parameters": dict(params or {}),
        "parameters_hash": _hash(dict(params or {})),
        "strategy_spec": strategy.spec(params or {}).to_dict(),
        "input_hash": input_hash,
        "bars": {
            "count": len(normalised),
            "closed_only": True,
            "start": _iso(normalised[0]["timestamp"]),
            "end": _iso(normalised[-1]["bar_end"]),
            "provider_values": sorted({str(item["provider"] or "UNKNOWN") for item in normalised}),
            "issues": issues,
            "availability_semantics": "available_at_and_data_as_of_must_be_at_or_before_signal_as_of",
            "availability_exclusions": availability_exclusions,
            "replay_as_of": _iso(requested_replay_as_of),
        },
        "warmup_bars": warmup,
        "signal_interval": {"start_index": start, "end_index": end},
        "signals": signals,
        "trades": trades,
        "costs": {
            "fee_rate": fee_rate_value,
            "slippage_bps": slippage_value,
            "funding_status": "NOT_AVAILABLE" if funding_required else "NOT_APPLICABLE",
            "cost_complete": not funding_required,
            "same_bar_policy": "STOP_FIRST_IF_STOP_AND_TARGET_BOTH_TOUCH",
            "entry_policy": "NEXT_BAR_OPEN_ADVERSE_SLIPPAGE",
        },
        "future_data_invariance": {
            "status": "PROVEN_BY_STRICT_PREFIX_INPUTS",
            "signal_prefix_hashes": [item["prefix_input_hash"] for item in signals],
            "future_rows_supplied_to_strategy": False,
            "availability_time_used": True,
        },
        "output_hash": _hash({
            "signals": signals,
            "trades": trades,
            "input_hash": input_hash,
            "parameters_hash": _hash(dict(params or {})),
            "strategy_version": actual_version,
            "signal_interval": {"start": start, "end": end},
            "costs": {"fee_rate": fee_rate_value, "slippage_bps": slippage_value},
        }),
    }
