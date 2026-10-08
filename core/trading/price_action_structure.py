"""Bounded, point-in-time price-structure evidence for AI strategy prompts.

This module describes confirmed structure only.  It does not score a setup,
recommend a side, or authorize an order.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import re
from typing import Any


_TIMEFRAME_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
_ALLOWED_QUALITY = {"VALID", "FRESH", "READY", "RECONSTRUCTED_LATE"}
_ALLOWED_SOURCES = {"gate_native_rest:last", "gate_native_rest", "gate_ccxt_injected"}
_ALLOWED_GATE_PROVIDERS = {"gate", "gate_public_swap"}
_MIN_BARS = 32
_MAX_BARS = 240
_PIVOT_SIDE_BARS = 2
_RANGE_BARS = 20
_SOFT_SUMMARY_CHARS = 700
_MAX_SUMMARY_CHARS = 1_400
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


def _utc(value: object) -> datetime | None:
    try:
        point = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if point.tzinfo is None:
            return None
        return point.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _time_text(value: datetime) -> str:
    point = value.astimezone(timezone.utc)
    precision = "microseconds" if point.microsecond else "seconds"
    return point.isoformat(timespec=precision).replace("+00:00", "Z")


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _rounded(value: float) -> float:
    return float(f"{value:.10g}")


def _stored_payload(row: dict[str, Any]) -> dict[str, Any] | None:
    """Decode optional versioned-row payload metadata without trusting it."""
    value = row.get("payload_json")
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _verified_versioned_gate_identity(row: dict[str, Any]) -> bool:
    """Require the persisted five-part identity and digest when provider is absent."""
    key = str(row.get("instrument_key") or "").strip()
    parts = key.split(":")
    if len(parts) != 5:
        return False
    venue, market_type, native_symbol, settle_currency, price_type = parts
    if (venue, market_type, price_type) != ("gate", "perpetual", "last"):
        return False
    if str(row.get("venue") or "").strip().lower() != venue:
        return False
    if str(row.get("market_type") or "").strip().lower() != market_type:
        return False
    if str(row.get("price_type") or "").strip().lower() != price_type:
        return False
    if str(row.get("native_symbol") or "").strip().upper() != native_symbol:
        return False
    if str(row.get("settle_currency") or "").strip().upper() != settle_currency:
        return False
    raw_hash = str(row.get("raw_hash") or "").strip()
    if not _SHA256_RE.fullmatch(raw_hash):
        return False
    symbol = "".join(char for char in str(row.get("symbol") or "").upper() if char.isalnum())
    native_compact = "".join(char for char in native_symbol.upper() if char.isalnum())
    return bool(symbol and symbol == native_compact)


def has_verified_gate_bar_identity(row: Any) -> bool:
    """Verify Gate source identity across legacy and versioned SQLite rows."""
    if not isinstance(row, dict):
        return False
    payload = _stored_payload(row)
    if payload is None:
        return False
    providers = (row.get("provider"), payload.get("provider"))
    if any(
        provider is not None
        and str(provider).strip().lower() not in _ALLOWED_GATE_PROVIDERS
        for provider in providers
    ):
        return False
    source = str(row.get("source") or "").strip()
    payload_source = payload.get("source")
    if payload_source is not None and str(payload_source).strip() != source:
        return False
    if not (
        str(row.get("venue") or "").strip().lower() == "gate"
        and str(row.get("market_type") or "").strip().lower() == "perpetual"
        and str(row.get("price_type") or "").strip().lower() == "last"
        and source in _ALLOWED_SOURCES
    ):
        return False
    return row.get("provider") is not None or (
        source.startswith("gate_native_") and _verified_versioned_gate_identity(row)
    )


def _unavailable(reason: str, *, as_of: datetime | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "UNAVAILABLE", "reason": reason}
    if as_of is not None:
        result["as_of"] = _time_text(as_of)
    return result


def _bar_record(row: Any, timeframe: str, point: datetime, *, identity_validator=has_verified_gate_bar_identity) -> dict[str, Any] | None:
    """Validate a stored Gate bar before it can contribute to structure."""
    if not isinstance(row, dict):
        return None
    closed = row.get("is_closed")
    if closed is not True and not (type(closed) is int and closed == 1):
        return None
    payload = _stored_payload(row)
    if payload is None:
        return None
    for metadata in (row, payload):
        if "synthetic" in metadata:
            synthetic = metadata.get("synthetic")
            if synthetic is not False and not (type(synthetic) is int and synthetic == 0):
                return None
    minutes = _TIMEFRAME_MINUTES.get(timeframe)
    if minutes is None:
        return None
    start = _utc(row.get("bar_start") or row.get("timestamp"))
    end = _utc(row.get("bar_end"))
    available = _utc(row.get("available_at"))
    if not start or not end or not available or end - start != timedelta(minutes=minutes):
        return None
    if end > point or available > point:
        return None
    if str(row.get("quality_status") or "").strip().upper() not in _ALLOWED_QUALITY:
        return None
    if not identity_validator(row):
        return None
    values = {key: _number(row.get(key)) for key in ("open", "high", "low", "close", "volume")}
    if any(value is None for value in values.values()):
        return None
    opening, high, low, close, volume = (values[key] for key in ("open", "high", "low", "close", "volume"))
    if (
        min(opening, high, low, close) <= 0
        or volume < 0
        or high < max(opening, close, low)
        or low > min(opening, close)
    ):
        return None
    return {
        "bar_start": start,
        "bar_end": end,
        "available_at": available,
        "source": str(row.get("source") or "").strip(),
        **values,
    }


def _event(
    side: str,
    level: float,
    pivot_known_at: datetime,
    bar_at: datetime,
    available_at: datetime,
    confirmed_at: datetime,
) -> dict[str, Any]:
    event = {
        "side": side,
        "level": _rounded(level),
        "pivot_confirmed_at": _time_text(pivot_known_at),
        "bar_at": _time_text(bar_at),
        "confirmed_at": _time_text(confirmed_at),
    }
    # confirmed_at is already max(bar knowledge, pivot knowledge), so an
    # identical availability timestamp is redundant; retain it only when it
    # differs and adds source timing information.
    if available_at != confirmed_at:
        event["available_at"] = _time_text(available_at)
    return event


def _summary_size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _build_price_action_structure(
    rows: Any,
    timeframe: str,
    as_of: object,
    *, identity_validator=has_verified_gate_bar_identity,
) -> dict[str, Any]:
    """Return a compact, closed-bar-only structure summary or an explicit reason.

    A swing becomes usable only after two bars on its right have closed.  BOS
    and sweep levels must already have been confirmed before the event bar.
    The rolling range excludes the latest closed bar.  None of these facts is
    an entry gate; they are source evidence for the model to interpret.
    """
    tf = str(timeframe or "").strip().lower()
    point = _utc(as_of)
    if point is None:
        return _unavailable("AS_OF_INVALID")
    if tf not in _TIMEFRAME_MINUTES:
        return _unavailable("TIMEFRAME_UNSUPPORTED", as_of=point)
    if not isinstance(rows, (list, tuple)):
        return _unavailable("BARS_UNAVAILABLE", as_of=point)

    records: dict[datetime, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_end = _utc(row.get("bar_end"))
        if raw_end is not None and raw_end > point:
            return _unavailable("FUTURE_BAR", as_of=point)
        record = _bar_record(row, tf, point, identity_validator=identity_validator)
        if record is None:
            continue
        if record["bar_end"] in records:
            return _unavailable("DUPLICATE_BAR_END", as_of=point)
        records[record["bar_end"]] = record

    bars = [records[key] for key in sorted(records)][-_MAX_BARS:]
    if len(bars) < _MIN_BARS:
        return _unavailable("INSUFFICIENT_VALID_CLOSED_BARS", as_of=point)
    cadence = timedelta(minutes=_TIMEFRAME_MINUTES[tf])
    if any(right["bar_end"] - left["bar_end"] != cadence for left, right in zip(bars, bars[1:])):
        return _unavailable("NONCONTIGUOUS_CLOSED_BARS", as_of=point)
    latest = bars[-1]["bar_end"]
    if point - latest > cadence + timedelta(minutes=2):
        return _unavailable("STALE_CLOSED_BARS", as_of=point)
    sources = sorted({bar["source"] for bar in bars})

    swings: list[dict[str, Any]] = []
    left_right = _PIVOT_SIDE_BARS
    for index in range(left_right, len(bars) - left_right):
        neighbors = [*range(index - left_right, index), *range(index + 1, index + left_right + 1)]
        candidate_high = bars[index]["high"]
        candidate_low = bars[index]["low"]
        if all(candidate_high > bars[item]["high"] for item in neighbors):
            swings.append({
                "side": "HIGH",
                "price": candidate_high,
                "pivot_at": _time_text(bars[index]["bar_end"]),
                "confirmation_bar_at": bars[index + left_right]["bar_end"],
                "available_at": max(
                    bar["available_at"]
                    for bar in bars[index - left_right:index + left_right + 1]
                ),
                "known_at": max(
                    bars[index + left_right]["bar_end"],
                    max(bar["available_at"]
                        for bar in bars[index - left_right:index + left_right + 1]),
                ),
                "pivot_index": index,
                "known_index": index + left_right,
            })
        if all(candidate_low < bars[item]["low"] for item in neighbors):
            swings.append({
                "side": "LOW",
                "price": candidate_low,
                "pivot_at": _time_text(bars[index]["bar_end"]),
                "confirmation_bar_at": bars[index + left_right]["bar_end"],
                "available_at": max(
                    bar["available_at"]
                    for bar in bars[index - left_right:index + left_right + 1]
                ),
                "known_at": max(
                    bars[index + left_right]["bar_end"],
                    max(bar["available_at"]
                        for bar in bars[index - left_right:index + left_right + 1]),
                ),
                "pivot_index": index,
                "known_index": index + left_right,
            })

    high_swings = [item for item in swings if item["side"] == "HIGH"][-2:]
    low_swings = [item for item in swings if item["side"] == "LOW"][-2:]
    latest_swings = {"HIGH": high_swings[-1] if high_swings else None,
                     "LOW": low_swings[-1] if low_swings else None}

    bos_events: list[dict[str, Any]] = []
    sweep_events: list[dict[str, Any]] = []
    retest_events: list[dict[str, Any]] = []
    seen_bos: list[dict[str, Any]] = []
    for index in range(1, len(bars)):
        bar = bars[index]
        bar_known_at = max(bar["bar_end"], bar["available_at"], bars[index - 1]["available_at"])
        previous_close = bars[index - 1]["close"]
        for prior_bos in seen_bos:
            if prior_bos["state"] != "ACTIVE":
                continue
            invalidated = (
                prior_bos["side"] == "LONG" and bar["close"] < prior_bos["level"]
            ) or (
                prior_bos["side"] == "SHORT" and bar["close"] > prior_bos["level"]
            )
            if invalidated:
                prior_bos["state"] = "INVALIDATED"
                prior_bos["invalidated_bar_at"] = _time_text(bar["bar_end"])
                prior_bos["invalidated_at"] = _time_text(max(bar_known_at, prior_bos["confirmed_at_dt"]))
        available_pivots = [
            item for item in swings
            if item["known_index"] < index and item["known_at"] <= bar_known_at
        ]
        known_highs = [item for item in available_pivots if item["side"] == "HIGH"]
        known_lows = [item for item in available_pivots if item["side"] == "LOW"]
        high_pivot = known_highs[-1] if known_highs else None
        low_pivot = known_lows[-1] if known_lows else None

        if high_pivot and previous_close <= high_pivot["price"] < bar["close"]:
            bos = {
                **_event(
                    "LONG", high_pivot["price"], high_pivot["known_at"], bar["bar_end"],
                    bar_known_at,
                    max(bar_known_at, high_pivot["known_at"]),
                ),
                "bar_index": index,
                "state": "ACTIVE",
                "confirmed_at_dt": max(bar_known_at, high_pivot["known_at"]),
            }
            for prior_bos in seen_bos:
                if prior_bos["state"] == "ACTIVE" and prior_bos["side"] == "LONG":
                    prior_bos["state"] = "SUPERSEDED"
                    prior_bos["superseded_at"] = bos["confirmed_at"]
            bos_events.append(bos)
            seen_bos.append(bos)
        elif low_pivot and previous_close >= low_pivot["price"] > bar["close"]:
            bos = {
                **_event(
                    "SHORT", low_pivot["price"], low_pivot["known_at"], bar["bar_end"],
                    bar_known_at,
                    max(bar_known_at, low_pivot["known_at"]),
                ),
                "bar_index": index,
                "state": "ACTIVE",
                "confirmed_at_dt": max(bar_known_at, low_pivot["known_at"]),
            }
            for prior_bos in seen_bos:
                if prior_bos["state"] == "ACTIVE" and prior_bos["side"] == "SHORT":
                    prior_bos["state"] = "SUPERSEDED"
                    prior_bos["superseded_at"] = bos["confirmed_at"]
            bos_events.append(bos)
            seen_bos.append(bos)

        if high_pivot and bar["high"] > high_pivot["price"] and bar["close"] <= high_pivot["price"]:
            sweep_events.append(_event(
                "SHORT", high_pivot["price"], high_pivot["known_at"], bar["bar_end"],
                bar_known_at,
                max(bar_known_at, high_pivot["known_at"]),
            ))
        if low_pivot and bar["low"] < low_pivot["price"] and bar["close"] >= low_pivot["price"]:
            sweep_events.append(_event(
                "LONG", low_pivot["price"], low_pivot["known_at"], bar["bar_end"],
                bar_known_at,
                max(bar_known_at, low_pivot["known_at"]),
            ))

        for prior_bos in reversed(seen_bos):
            if (
                prior_bos["bar_index"] >= index
                or prior_bos["state"] != "ACTIVE"
                or prior_bos["confirmed_at_dt"] > bar_known_at
            ):
                continue
            level = prior_bos["level"]
            if (
                prior_bos["side"] == "LONG"
                and bar["low"] <= level <= bar["high"]
                and bar["close"] >= level
            ) or (
                prior_bos["side"] == "SHORT"
                and bar["low"] <= level <= bar["high"]
                and bar["close"] <= level
            ):
                retest_events.append({
                    "side": prior_bos["side"],
                    "level": level,
                    "bos_at": prior_bos["confirmed_at"],
                    "bar_at": _time_text(bar["bar_end"]),
                    "confirmed_at": _time_text(max(bar_known_at, prior_bos["confirmed_at_dt"])),
                    "state": "ACTIVE",
                    "bos_ref": prior_bos,
                })
                break

    prior_range_bars = bars[-(_RANGE_BARS + 1):-1]
    prior_range = None
    if len(prior_range_bars) == _RANGE_BARS:
        prior_range = {
            "lookback_bars": _RANGE_BARS,
            "high": _rounded(max(bar["high"] for bar in prior_range_bars)),
            "low": _rounded(min(bar["low"] for bar in prior_range_bars)),
            "as_of": _time_text(prior_range_bars[-1]["bar_end"]),
        }

    def public_swing(item: dict[str, Any]) -> dict[str, Any]:
        result = {
            "side": item["side"],
            "price": _rounded(item["price"]),
            "pivot_at": item["pivot_at"],
            "confirmation_bar_at": _time_text(item["confirmation_bar_at"]),
            "confirmed_at": _time_text(item["known_at"]),
        }
        if item["available_at"] != item["known_at"]:
            result["available_at"] = _time_text(item["available_at"])
        return result

    def public_event(item: dict[str, Any] | None) -> dict[str, Any] | None:
        if item is None:
            return None
        return {key: value for key, value in item.items() if key not in {"bar_index", "confirmed_at_dt", "bos_ref"}}

    latest_retest = None
    for item in reversed(retest_events):
        bos_ref = item.get("bos_ref")
        if isinstance(bos_ref, dict):
            item["state"] = bos_ref["state"]
            if bos_ref.get("invalidated_at"):
                item["invalidated_at"] = bos_ref["invalidated_at"]
                item["invalidated_bar_at"] = bos_ref["invalidated_bar_at"]
        latest_retest = item
        break

    summary: dict[str, Any] = {
        "status": "READY",
        "as_of": _time_text(point),
        "last_closed_at": _time_text(latest),
        "source": sources[0] if len(sources) == 1 else sources,
        "closed_bar_count": len(bars),
        "confirmed_swings": [public_swing(item) for item in (*high_swings, *low_swings)],
        # Preserve the prior confirmed, available pivot price on each side.
        # Latest confirmed rows already retain their own price and timing.
        "prior_swings": {
            side: _rounded(items[-2]["price"])
            for side, items in (("HIGH", high_swings), ("LOW", low_swings))
            if len(items) >= 2
        },
        "prior_range": prior_range,
        "bos": public_event(bos_events[-1] if bos_events else None),
        "sweep_reclaim": public_event(sweep_events[-1] if sweep_events else None),
        "breakout_retest": public_event(latest_retest),
    }

    # Seven hundred characters is a soft prompt target.  First remove older
    # swing rows and redundant range timing, while keeping a visible marker
    # and every latest structure event with its full causal timestamps/state.
    if _summary_size(summary) > _SOFT_SUMMARY_CHARS:
        summary["confirmed_swings"] = [
            public_swing(item)
            for item in (latest_swings["HIGH"], latest_swings["LOW"])
            if item is not None
        ]
        summary["compaction"] = "latest_swing_per_side"
    if _summary_size(summary) > _SOFT_SUMMARY_CHARS:
        # The fixed 20-bar range excludes latest_closed_at by construction;
        # keep that contract explicit without repeating a second timestamp.
        if isinstance(summary.get("prior_range"), dict):
            summary["prior_range"].pop("as_of", None)
            summary["prior_range"]["excludes_latest_close"] = True
    if _summary_size(summary) > _MAX_SUMMARY_CHARS:
        return _unavailable("SUMMARY_BUDGET_EXCEEDED", as_of=point)
    return summary


def build_price_action_structure(rows: Any, timeframe: str, as_of: object) -> dict[str, Any]:
    """Production entry point: Gate identity checks remain mandatory."""
    return _build_price_action_structure(rows, timeframe, as_of)


__all__ = ["build_price_action_structure", "has_verified_gate_bar_identity"]
