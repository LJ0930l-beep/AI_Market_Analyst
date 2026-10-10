"""Independent reconstruction of the frozen V38 visible market-only inputs.

This verifier intentionally uses only the Python standard library. It rebuilds the
causal V36 frames and V38 market-input hashes without importing application code,
opens only the plan, dataset manifest, and visible optimization/validation input
document, and never inspects the sealed untouched-test payload.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAN = ROOT / "configs/research/datasets/v38-stratified-purged-sample-plan-v1.json"
DEFAULT_DATASET = ROOT / "reports/v38+/dataset-stratified-purged-20261009-v1"
DEFAULT_OUTPUT = ROOT / "reports/v38+/verification/v38-market-input-independent-audit-20261009-v1.json"

PLAN_SHA256 = "094ee37ff932b3f237c4eef77a42dc94cf496928a46afd5a5c30efa35cfa873f"
DATASET_MANIFEST_SHA256 = "1f9e157bad2df4c32f863c2ae7a779b7d9ed573ad09d37f49ca44590eca4352a"
VISIBLE_INPUTS_SHA256 = "4fd074eafb73ecd113c8734c5417c05878b4ee6ad6afbcb75f736b74c173399d"
SOURCE_DATABASE_SHA256 = "c04d69fa13b68efd5e9e64e02442524961589a2d805354017f773f46169ff4d2"
SOURCE_MANIFEST_SHA256 = "2f348e6a190690e173f0237b5f5614102bebeb8e0573a1266014c04f5b25012a"
PARTITION_POLICY_SHA256 = "5169daa812c643cb35b40e3c5327bfbadf30b0c6b9c7db78b172de5c561f7ac7"
EXPECTED_VISIBLE_POINT_COUNT = 54
EXPECTED_PARTITION_COUNTS = {"optimization": 36, "untouched_test": 18, "validation": 18}
EXPECTED_SEALED_POINT_COUNT = 18

INPUT_SCHEMA_VERSION = "pa-market-only-v38/input-1"
DATASET_INPUT_SCHEMA_VERSION = "pa-market-only-v38/stratified-purged-market-input-dataset-1"
DATASET_MANIFEST_SCHEMA_VERSION = "pa-market-only-v38/stratified-purged-dataset-manifest-1"
CONTEXT_SCHEMA_VERSION = "pa-decision-quality-v36/context-1"
REQUIRED_TIMEFRAMES = ("15m", "5m", "1h", "4h")
TIMEFRAME_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
PROMPT_BAR_LIMITS = {"15m": 8, "5m": 6, "1h": 4, "4h": 4}
VALID_QUALITY = {"VALID", "FRESH", "READY", "RECONSTRUCTED_LATE", "FROZEN_RESEARCH"}
ALLOWED_SOURCES = {
    "BINANCE_UM_OFFICIAL_MONTHLY_ARCHIVE_RECONSTRUCTED",
    "fixture:deterministic",
}
PARTITIONS = {
    "optimization": ("2025-10-01T00:00:00Z", "2026-04-01T00:00:00Z"),
    "validation": ("2026-04-01T00:00:00Z", "2026-07-01T00:00:00Z"),
    "untouched_test": ("2026-07-01T00:00:00Z", "2026-10-01T00:00:00Z"),
}
_ALLOWED_PARTITIONS = frozenset(PARTITIONS)
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{3,20}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class IndependentAuditError(ValueError):
    """Stable, path-free failure code from the independent verifier."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise IndependentAuditError(code)


def _utc(value: Any, code: str = "TIME_INVALID") -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise IndependentAuditError(code) from exc
    else:
        raise IndependentAuditError(code)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IndependentAuditError(code)
    return parsed.astimezone(UTC)


def _optional_utc(value: Any) -> datetime | None:
    try:
        return _utc(value)
    except IndependentAuditError:
        return None


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise IndependentAuditError("CANONICAL_JSON_INVALID") from exc


def _canonical_sha(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise IndependentAuditError("FROZEN_FILE_UNAVAILABLE") from exc
    return digest.hexdigest()


def _read_json(path: Path, code: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IndependentAuditError(code) from exc


def _partition_for(point: datetime) -> str | None:
    for label, (start_raw, end_raw) in PARTITIONS.items():
        if _utc(start_raw) <= point < _utc(end_raw):
            return label
    return None


def _frame_bars(rows: Any, timeframe: str, decision_time: datetime,
                *, minimum_bars: int = 32, maximum_bars: int = 240) -> tuple[dict[str, Any], ...]:
    if timeframe not in TIMEFRAME_MINUTES:
        raise IndependentAuditError("TIMEFRAME_UNSUPPORTED")
    if not isinstance(rows, list):
        raise IndependentAuditError("BARS_MISSING")
    duration = timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
    causal_ends: list[datetime] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        end = _optional_utc(row.get("bar_end"))
        available = _optional_utc(row.get("available_at"))
        if end is None or available is None or end >= decision_time or available >= decision_time:
            continue
        if causal_ends and end < causal_ends[-1]:
            raise IndependentAuditError("BAR_ORDER_INVALID")
        causal_ends.append(end)

    output: list[dict[str, Any]] = []
    seen: set[datetime] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise IndependentAuditError("BAR_NOT_OBJECT")
        start = _optional_utc(row.get("bar_start", row.get("timestamp")))
        end = _optional_utc(row.get("bar_end"))
        available = _optional_utc(row.get("available_at"))
        if start is None or end is None or available is None:
            raise IndependentAuditError("BAR_TIME_UNKNOWN")
        # Do not inspect or hash future/equal rows after parsing their timestamps.
        if end >= decision_time or available >= decision_time:
            continue
        if row.get("is_closed") is not True:
            raise IndependentAuditError("BAR_NOT_CONFIRMED_CLOSED")
        if row.get("timeframe") not in (None, timeframe):
            raise IndependentAuditError("BAR_TIMEFRAME_MISMATCH")
        if end - start != duration:
            raise IndependentAuditError("BAR_DURATION_INVALID")
        if str(row.get("quality_status") or "").strip().upper() not in VALID_QUALITY:
            raise IndependentAuditError("BAR_QUALITY_UNKNOWN")
        source = str(row.get("source") or "").strip()
        if not source:
            raise IndependentAuditError("BAR_SOURCE_UNKNOWN")
        values = {key: _number(row.get(key)) for key in ("open", "high", "low", "close", "volume")}
        if any(value is None for value in values.values()):
            raise IndependentAuditError("BAR_OHLCV_INVALID")
        opening, high, low, close, volume = (values[key] for key in ("open", "high", "low", "close", "volume"))
        if (min(opening, high, low, close) <= 0 or volume < 0
                or high < max(opening, close, low) or low > min(opening, close)):
            raise IndependentAuditError("BAR_OHLCV_GEOMETRY_INVALID")
        if end in seen:
            raise IndependentAuditError("DUPLICATE_BAR_END")
        seed = f"{timeframe}|{_stamp(start)}|{_stamp(end)}|{source}"
        ref = "bar:" + timeframe + ":" + hashlib.sha256(seed.encode()).hexdigest()[:16]
        output.append({
            "timeframe": timeframe,
            "bar_start": _stamp(start),
            "bar_end": _stamp(end),
            "available_at": _stamp(available),
            "source": source,
            "open": opening,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "evidence_ref": ref,
            "_end": end,
            "_start": start,
            "_available": available,
            "_ref": ref,
        })
        seen.add(end)

    previous_end: datetime | None = None
    for item in output:
        end = item["_end"]
        if previous_end is not None:
            if end < previous_end:
                raise IndependentAuditError("BAR_ORDER_INVALID")
            if end - previous_end != duration:
                raise IndependentAuditError("BAR_GAP_OR_DUPLICATE")
        previous_end = end
    output = output[-maximum_bars:]
    if len(output) < minimum_bars:
        raise IndependentAuditError("INSUFFICIENT_CAUSAL_BARS")
    if decision_time - output[-1]["_end"] > duration + timedelta(minutes=2):
        raise IndependentAuditError("STALE_CAUSAL_BARS")
    return tuple(output)


def _rounded(value: float | None, digits: int = 8) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _record(bar: dict[str, Any]) -> dict[str, Any]:
    return {key: bar[key] for key in (
        "timeframe", "bar_start", "bar_end", "available_at", "source", "open", "high",
        "low", "close", "volume", "evidence_ref",
    )}


def _frame_facts(timeframe: str, bars: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    recent = list(bars[-20:])
    last = bars[-1]
    prior = list(bars[-21:-1])
    changes = [right["close"] - left["close"] for left, right in pairwise(recent)]
    true_ranges: list[float] = []
    for left, right in pairwise(bars[-15:]):
        true_ranges.append(max(
            right["high"] - right["low"],
            abs(right["high"] - left["close"]),
            abs(right["low"] - left["close"]),
        ))
    atr = sum(true_ranges) / len(true_ranges) if true_ranges else None
    overlaps: list[float] = []
    for left, right in pairwise(recent):
        denominator = min(left["high"] - left["low"], right["high"] - right["low"])
        overlaps.append(
            max(0.0, min(left["high"], right["high"]) - max(left["low"], right["low"])) / denominator
            if denominator > 0 else 0.0
        )

    swings: list[dict[str, Any]] = []
    for index in range(2, len(bars) - 2):
        window = bars[index - 2:index + 3]
        pivot = bars[index]
        known_at = max(window[-1]["_end"], *(item["_available"] for item in window))
        confirmation_refs = [item["_ref"] for item in window]
        if all(pivot["high"] > item["high"] for item in window if item is not pivot):
            swings.append({
                "side": "HIGH", "price": _rounded(pivot["high"]),
                "pivot_at": _stamp(pivot["_end"]), "confirmed_at": _stamp(window[-1]["_end"]),
                "available_at": _stamp(max(item["_available"] for item in window)),
                "known_at": _stamp(known_at), "evidence_ref": pivot["_ref"],
                "confirmation_evidence_refs": confirmation_refs,
            })
        if all(pivot["low"] < item["low"] for item in window if item is not pivot):
            swings.append({
                "side": "LOW", "price": _rounded(pivot["low"]),
                "pivot_at": _stamp(pivot["_end"]), "confirmed_at": _stamp(window[-1]["_end"]),
                "available_at": _stamp(max(item["_available"] for item in window)),
                "known_at": _stamp(known_at), "evidence_ref": pivot["_ref"],
                "confirmation_evidence_refs": confirmation_refs,
            })
    swings.sort(key=lambda item: (item["pivot_at"], item["side"]))
    highs = [item for item in swings if item["side"] == "HIGH"][-2:]
    lows = [item for item in swings if item["side"] == "LOW"][-2:]
    progression = {
        "highs": "HIGHER_HIGH" if len(highs) == 2 and highs[-1]["price"] > highs[-2]["price"] else (
            "LOWER_HIGH" if len(highs) == 2 and highs[-1]["price"] < highs[-2]["price"] else "UNKNOWN"
        ),
        "lows": "HIGHER_LOW" if len(lows) == 2 and lows[-1]["price"] > lows[-2]["price"] else (
            "LOWER_LOW" if len(lows) == 2 and lows[-1]["price"] < lows[-2]["price"] else "UNKNOWN"
        ),
    }
    previous_high = max(item["high"] for item in prior) if prior else None
    previous_low = min(item["low"] for item in prior) if prior else None
    range_high_refs = sorted({item["_ref"] for item in prior if item["high"] == previous_high})
    range_low_refs = sorted({item["_ref"] for item in prior if item["low"] == previous_low})
    range_position = None
    location_zone = "UNKNOWN"
    if previous_high is not None and previous_low is not None and previous_high > previous_low:
        range_position = (last["close"] - previous_low) / (previous_high - previous_low)
        if last["close"] > previous_high or last["close"] < previous_low:
            location_zone = "OUTSIDE_PRIOR_RANGE"
        elif range_position <= 0.2:
            location_zone = "LOWER_RANGE_AREA"
        elif range_position >= 0.8:
            location_zone = "UPPER_RANGE_AREA"
        else:
            location_zone = "RANGE_MIDDLE"
    breakout = "NONE"
    breakout_refs: list[str] = []
    if previous_high is not None and last["close"] > previous_high:
        breakout = "CLOSE_ABOVE_PRIOR_RANGE"
        breakout_refs = [last["_ref"], *range_high_refs]
    elif previous_low is not None and last["close"] < previous_low:
        breakout = "CLOSE_BELOW_PRIOR_RANGE"
        breakout_refs = [last["_ref"], *range_low_refs]

    failed_breakout = None
    start_index = max(21, len(bars) - 5)
    for index in range(start_index, len(bars)):
        event_bar = bars[index - 1]
        return_bar = bars[index]
        if index < 21:
            continue
        frozen = bars[index - 21:index - 1]
        if len(frozen) != 20:
            continue
        frozen_high = max(item["high"] for item in frozen)
        frozen_low = min(item["low"] for item in frozen)
        if event_bar["close"] > frozen_high and return_bar["close"] <= frozen_high:
            failed_breakout = {
                "direction": "UPPER_FAILURE", "level": _rounded(frozen_high),
                "event_bar_at": _stamp(event_bar["_end"]),
                "confirmed_at": _stamp(return_bar["_end"]),
                "evidence_refs": [event_bar["_ref"], return_bar["_ref"]],
            }
        elif event_bar["close"] < frozen_low and return_bar["close"] >= frozen_low:
            failed_breakout = {
                "direction": "LOWER_FAILURE", "level": _rounded(frozen_low),
                "event_bar_at": _stamp(event_bar["_end"]),
                "confirmed_at": _stamp(return_bar["_end"]),
                "evidence_refs": [event_bar["_ref"], return_bar["_ref"]],
            }

    extension = None
    if atr and atr > 0:
        mean_close = sum(item["close"] for item in recent) / len(recent)
        extension = (last["close"] - mean_close) / atr
    direction_fraction = sum(change > 0 for change in changes) / len(changes) if changes else None
    refs = [last["_ref"]]
    recent_limit = PROMPT_BAR_LIMITS[timeframe]
    recent_bars = [_record(item) for item in bars[-recent_limit:]]
    refs.extend(item["_ref"] for item in bars[-recent_limit:])
    refs.extend(item["evidence_ref"] for item in (*highs, *lows))
    refs.extend(ref for item in (*highs, *lows) for ref in item.get("confirmation_evidence_refs", []))
    refs.extend(range_high_refs)
    refs.extend(range_low_refs)
    refs.extend(breakout_refs)
    if failed_breakout:
        refs.extend(failed_breakout["evidence_refs"])
    return {
        "status": "READY",
        "data_as_of": _stamp(last["_end"]),
        "bar_count": len(bars),
        "recent_candles": recent_bars,
        "source": sorted({item["source"] for item in bars}),
        "evidence_refs": sorted(set(refs)),
        "objective_facts": {
            "last_close": _rounded(last["close"]),
            "close_change_20_fraction": _rounded((last["close"] / recent[0]["close"] - 1) if recent[0]["close"] else None),
            "up_close_fraction_19": _rounded(direction_fraction),
            "mean_adjacent_range_overlap": _rounded(sum(overlaps) / len(overlaps) if overlaps else None),
            "atr14_simple": _rounded(atr),
            "extension_from_mean_in_atr": _rounded(extension),
            "confirmed_swings": [*highs, *lows],
            "swing_progression": progression,
            "prior_20_range": {
                "high": _rounded(previous_high), "high_evidence_refs": range_high_refs,
                "low": _rounded(previous_low), "low_evidence_refs": range_low_refs,
            },
            "position_in_prior_range": _rounded(range_position),
            "location_zone_fact": location_zone,
            "close_breakout_fact": breakout,
            "close_breakout_evidence_refs": sorted(set(breakout_refs)),
            "failed_breakout_fact": failed_breakout,
        },
        "model_interpretation_required": [
            "market_regime", "higher_timeframe_bias", "trade_location", "setup_quality",
        ],
    }


def _context(bars_by_timeframe: dict[str, Any], decision_time: datetime) -> tuple[dict[str, Any], dict[str, tuple[dict[str, Any], ...]]]:
    frames: dict[str, dict[str, Any]] = {}
    bars: dict[str, tuple[dict[str, Any], ...]] = {}
    frame_hashes: dict[str, Any] = {}
    for timeframe in REQUIRED_TIMEFRAMES:
        try:
            causal_bars = _frame_bars(bars_by_timeframe.get(timeframe), timeframe, decision_time)
            bars[timeframe] = causal_bars
            frames[timeframe] = _frame_facts(timeframe, causal_bars)
            frame_hashes[timeframe] = [_record(item) for item in causal_bars]
        except IndependentAuditError as exc:
            frames[timeframe] = {
                "status": "UNAVAILABLE", "reason": exc.code, "data_as_of": None,
                "evidence_refs": [], "objective_facts": {},
            }
            frame_hashes[timeframe] = {"status": "UNAVAILABLE", "reason": exc.code}
    all_ready = all(frames.get(tf, {}).get("status") == "READY" for tf in REQUIRED_TIMEFRAMES)
    input_hash = _canonical_sha({"decision_time": _stamp(decision_time), "timeframes": frame_hashes})
    return ({
        "status": "READY" if all_ready else "INCOMPLETE",
        "input_sha256": input_hash,
        "frames": frames,
    }, bars)


def _source_provenance(bars_by_timeframe: dict[str, Any], bars: dict[str, tuple[dict[str, Any], ...]]) -> dict[str, Any]:
    used = {
        (timeframe, item["bar_end"], item["available_at"])
        for timeframe, series in bars.items()
        for item in series
    }
    selected: list[dict[str, Any]] = []
    for timeframe, rows in bars_by_timeframe.items():
        for row in rows:
            end = _stamp(_utc(row.get("bar_end"), "BAR_TIME_INVALID"))
            available = _stamp(_utc(row.get("available_at"), "BAR_TIME_INVALID"))
            if (timeframe, end, available) in used:
                selected.append(row)
    sources = sorted({str(row.get("source", "")) for row in selected})
    _require(all(source in ALLOWED_SOURCES for source in sources), "MARKET_SOURCE_NOT_ALLOWLISTED")
    file_hashes: set[str] = set()
    database_hashes: set[str] = set()
    manifest_hashes: set[str] = set()
    for row in selected:
        hashes = row.get("source_file_hashes", [])
        if hashes is None:
            hashes = []
        _require(isinstance(hashes, list), "SOURCE_FILE_HASHES_INVALID")
        one_hash = row.get("source_file_hash")
        if one_hash is not None:
            hashes = [*hashes, one_hash]
        for digest in hashes:
            _require(isinstance(digest, str) and _HASH_RE.fullmatch(digest), "SOURCE_FILE_HASH_INVALID")
            file_hashes.add(digest)
        for field, collected in (
            ("source_database_sha256", database_hashes),
            ("source_manifest_sha256", manifest_hashes),
        ):
            digest = row.get(field)
            if digest is not None:
                _require(isinstance(digest, str) and _HASH_RE.fullmatch(digest), "SOURCE_MANIFEST_HASH_INVALID")
                collected.add(digest)

    def grades(field: str, default: str) -> list[str]:
        return sorted({str(row.get(field) or default) for row in selected})

    return {
        "source_kind": sources,
        "source_exchange": sorted({str(row.get("source_exchange") or "UNVERIFIED") for row in selected}),
        "price_evidence_grade": grades("price_evidence_grade", "UNVERIFIED"),
        "availability_evidence_grade": grades("available_at_evidence_grade", "UNVERIFIED"),
        "availability_basis": grades("available_at_basis", "UNSPECIFIED"),
        "volume_unit": grades("volume_unit", "UNSPECIFIED"),
        "archive_file_sha256s": sorted(file_hashes),
        "archive_database_sha256s": sorted(database_hashes),
        "archive_manifest_sha256s": sorted(manifest_hashes),
    }


def reconstruct_market_only_input(point: Any) -> dict[str, Any]:
    """Rebuild one V38 market-only input without importing application modules."""
    _require(isinstance(point, dict), "MARKET_POINT_INVALID")
    allowed = {"decision_id", "decision_time", "symbol", "partition", "bars_by_timeframe"}
    extras = set(point) - allowed
    if extras:
        forbidden = {
            "account", "account_snapshot", "available_margin", "equity", "equity_usdt",
            "execution_constraints", "entry_price", "stop_price", "target_price", "order_type",
            "proposed_notional_usdt", "requested_leverage", "leverage", "position", "positions",
            "position_size", "working_orders", "net_reward_risk", "net_rr", "risk_pass",
            "eligible_proposal", "proposal", "gateway_accepted", "venue_fill", "complete_close",
            "fill", "trade_id", "state_snapshot", "risk_inputs",
        }
        code = "MARKET_ONLY_ACCOUNT_OR_EXECUTION_INPUT_FORBIDDEN" if any(
            str(key).strip().lower().replace("-", "_").replace(" ", "_") in forbidden
            for key in extras
        ) else "MARKET_POINT_FIELD_NOT_ALLOWED"
        raise IndependentAuditError(code)
    decision_id = point.get("decision_id")
    symbol = point.get("symbol")
    partition = point.get("partition")
    _require(isinstance(decision_id, str) and bool(decision_id.strip()), "DECISION_ID_INVALID")
    _require(isinstance(symbol, str) and _SYMBOL_RE.fullmatch(symbol), "MARKET_SYMBOL_INVALID")
    _require(isinstance(partition, str) and partition in _ALLOWED_PARTITIONS, "PARTITION_INVALID")
    decision_time = _utc(point.get("decision_time"), "DECISION_TIME_INVALID")
    _require(_partition_for(decision_time) == partition, "PARTITION_LABEL_MISMATCH")
    bars_by_timeframe = point.get("bars_by_timeframe")
    _require(isinstance(bars_by_timeframe, dict) and set(bars_by_timeframe) == set(REQUIRED_TIMEFRAMES),
             "MARKET_TIMEFRAMES_INVALID")
    for timeframe in REQUIRED_TIMEFRAMES:
        rows = bars_by_timeframe.get(timeframe)
        _require(isinstance(rows, list), "MARKET_BARS_INVALID")
        for row in rows:
            _require(isinstance(row, dict), "MARKET_BAR_INVALID")
            bar_end = _utc(row.get("bar_end"), "BAR_TIME_INVALID")
            available_at = _utc(row.get("available_at"), "BAR_TIME_INVALID")
            _require(available_at >= bar_end, "BAR_AVAILABILITY_PRECEDES_CONFIRMATION")

    context, causal_bars = _context(bars_by_timeframe, decision_time)
    _require(context["status"] == "READY", "MARKET_CONTEXT_INCOMPLETE")
    provenance = _source_provenance(bars_by_timeframe, causal_bars)
    refs = sorted({ref for frame in context["frames"].values() for ref in frame.get("evidence_refs", [])})
    _require(bool(refs), "MARKET_EVIDENCE_REFS_MISSING")
    available_by_timeframe = {
        timeframe: _stamp(max(_utc(bar["available_at"]) for bar in series))
        for timeframe, series in sorted(causal_bars.items()) if series
    }
    _require(set(available_by_timeframe) == set(REQUIRED_TIMEFRAMES), "MARKET_TIMEFRAMES_INCOMPLETE")
    _require(all(_utc(value) < decision_time for value in available_by_timeframe.values()), "MARKET_INPUT_NOT_CAUSAL")
    payload: dict[str, Any] = {
        "schema_version": INPUT_SCHEMA_VERSION,
        "track": "MARKET_ONLY",
        "decision_id": decision_id.strip(),
        "symbol": symbol,
        "partition": partition,
        "decision_time": _stamp(decision_time),
        "data_available_through_by_timeframe": available_by_timeframe,
        "market_context": {
            "schema_version": CONTEXT_SCHEMA_VERSION,
            "status": context["status"],
            "input_sha256": context["input_sha256"],
            "data_as_of_by_timeframe": {
                timeframe: frame.get("data_as_of") for timeframe, frame in sorted(context["frames"].items())
            },
            "frames": {timeframe: frame for timeframe, frame in sorted(context["frames"].items())},
        },
        "evidence_refs": refs,
        "provenance": provenance,
        "authority": {
            "production_authority": False,
            "order_creation_authorized": False,
            "account_state_included": False,
            "execution_constraints_included": False,
        },
    }
    payload["market_input_sha256"] = _canonical_sha(payload)
    return payload


def _finding(code: str) -> dict[str, str]:
    return {"code": code}


def _is_regular_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def audit_visible_market_inputs(plan_path: Path, dataset_directory: Path) -> dict[str, Any]:
    """Reconstruct visible inputs and compare them to frozen V38 descriptors."""
    findings: list[dict[str, str]] = []
    counts = {
        "visible_inputs_reconstructed": 0,
        "market_input_hashes_matched": 0,
        "evidence_ref_counts_matched": 0,
    }
    hashes: list[dict[str, Any]] = []
    try:
        plan_path = Path(plan_path)
        dataset_directory = Path(dataset_directory)
        if not _is_regular_file(plan_path):
            raise IndependentAuditError("FROZEN_PLAN_UNAVAILABLE")
        if dataset_directory.is_symlink() or not dataset_directory.is_dir():
            raise IndependentAuditError("FROZEN_DATASET_DIRECTORY_UNAVAILABLE")
        plan_file_observed = _file_sha(plan_path)
        plan = _read_json(plan_path, "FROZEN_PLAN_INVALID")
        plan_observed = _canonical_sha(plan) if isinstance(plan, dict) else None
        if plan_observed != PLAN_SHA256:
            findings.append(_finding("PLAN_SHA256_MISMATCH"))
        if not isinstance(plan, dict) or plan.get("schema_version") != "pa-market-only-v38/stratified-purged-sample-plan-1":
            findings.append(_finding("PLAN_SCHEMA_INVALID"))
        plan_source = plan.get("source") if isinstance(plan, dict) else None
        if (not isinstance(plan, dict) or not isinstance(plan_source, dict)
                or plan.get("partition_policy_sha256") != PARTITION_POLICY_SHA256
                or plan_source.get("source_database_sha256") != SOURCE_DATABASE_SHA256
                or plan_source.get("source_manifest_sha256") != SOURCE_MANIFEST_SHA256):
            findings.append(_finding("PLAN_SOURCE_OR_PARTITION_BINDING_INVALID"))

        manifest_path = dataset_directory / "dataset-manifest.json"
        inputs_path = dataset_directory / "optimization-validation-inputs.json"
        if not _is_regular_file(manifest_path) or not _is_regular_file(inputs_path):
            raise IndependentAuditError("VISIBLE_DATASET_FILE_UNAVAILABLE")
        manifest_file_observed = _file_sha(manifest_path)
        inputs_observed = _file_sha(inputs_path)
        if inputs_observed != VISIBLE_INPUTS_SHA256:
            findings.append(_finding("VISIBLE_INPUTS_SHA256_MISMATCH"))
        manifest = _read_json(manifest_path, "DATASET_MANIFEST_INVALID")
        inputs = _read_json(inputs_path, "VISIBLE_INPUTS_INVALID")
        manifest_observed = None
        if isinstance(manifest, dict):
            manifest_unsigned = dict(manifest)
            manifest_observed = manifest_unsigned.pop("manifest_sha256", None)
            calculated_manifest_sha = _canonical_sha(manifest_unsigned)
            if (manifest_observed != DATASET_MANIFEST_SHA256
                    or calculated_manifest_sha != DATASET_MANIFEST_SHA256):
                findings.append(_finding("DATASET_MANIFEST_SHA256_MISMATCH"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != DATASET_MANIFEST_SCHEMA_VERSION:
            findings.append(_finding("DATASET_MANIFEST_SCHEMA_INVALID"))
        if (not isinstance(manifest, dict) or manifest.get("plan_sha256") != PLAN_SHA256
                or manifest.get("partition_counts") != EXPECTED_PARTITION_COUNTS
                or manifest.get("optimization_validation_input_count") != EXPECTED_VISIBLE_POINT_COUNT
                or manifest.get("untouched_test_hash_only_count") != EXPECTED_SEALED_POINT_COUNT
                or manifest.get("untouched_test_payloads_included") is not False
                or manifest.get("model_outputs") != 0
                or manifest.get("gemini_research_calls_used") != 0
                or manifest.get("orders_created") != 0):
            findings.append(_finding("DATASET_MANIFEST_SCOPE_OR_AUTHORITY_INVALID"))
        if not isinstance(inputs, dict) or inputs.get("schema_version") != DATASET_INPUT_SCHEMA_VERSION:
            findings.append(_finding("VISIBLE_INPUTS_SCHEMA_INVALID"))
        if (not isinstance(inputs, dict) or inputs.get("dataset_kind") != "MARKET_ONLY_CAUSAL_STRATIFIED_PURGED_PAIRED_CONTEXTS_NO_LABELS"
                or inputs.get("partition_scope") != ["optimization", "validation"]
                or inputs.get("plan_sha256") != PLAN_SHA256
                or inputs.get("partition_policy_sha256") != PARTITION_POLICY_SHA256
                or inputs.get("source_database_sha256") != SOURCE_DATABASE_SHA256
                or inputs.get("source_manifest_sha256") != SOURCE_MANIFEST_SHA256
                or inputs.get("model_outputs_included") is not False
                or inputs.get("outcome_labels_included") is not False):
            findings.append(_finding("VISIBLE_INPUTS_METADATA_INVALID"))
        files = manifest.get("files", {}) if isinstance(manifest, dict) else {}
        if not isinstance(files, dict) or files.get("optimization-validation-inputs.json") != VISIBLE_INPUTS_SHA256:
            findings.append(_finding("MANIFEST_VISIBLE_INPUT_FILE_BINDING_INVALID"))
        if not isinstance(manifest, dict) or not isinstance(inputs, dict):
            raise IndependentAuditError("FROZEN_DOCUMENT_STRUCTURE_INVALID")
        points = inputs.get("decision_points")
        descriptors = manifest.get("points")
        if not isinstance(points, list) or len(points) != EXPECTED_VISIBLE_POINT_COUNT:
            findings.append(_finding("VISIBLE_POINT_COUNT_INVALID"))
            points = points if isinstance(points, list) else []
        if not isinstance(descriptors, list):
            findings.append(_finding("MANIFEST_DESCRIPTOR_LIST_INVALID"))
            descriptors = []
        visible_descriptors = [
            item for item in descriptors
            if isinstance(item, dict) and item.get("partition") in ("optimization", "validation")
        ]
        if len(visible_descriptors) != EXPECTED_VISIBLE_POINT_COUNT:
            findings.append(_finding("VISIBLE_DESCRIPTOR_COUNT_INVALID"))
        descriptor_by_id: dict[str, dict[str, Any]] = {}
        for descriptor in visible_descriptors:
            decision_id = descriptor.get("decision_id")
            if not isinstance(decision_id, str) or decision_id in descriptor_by_id:
                findings.append(_finding("VISIBLE_DESCRIPTOR_ID_INVALID"))
            else:
                descriptor_by_id[decision_id] = descriptor
        point_ids: set[str] = set()
        for point in points:
            if not isinstance(point, dict):
                findings.append(_finding("VISIBLE_MARKET_POINT_INVALID"))
                continue
            decision_id = point.get("decision_id")
            if not isinstance(decision_id, str) or decision_id in point_ids:
                findings.append(_finding("VISIBLE_MARKET_POINT_ID_INVALID"))
                continue
            point_ids.add(decision_id)
            descriptor = descriptor_by_id.get(decision_id)
            if descriptor is None:
                findings.append(_finding("VISIBLE_DESCRIPTOR_MISSING"))
                continue
            if any(point.get(field) != descriptor.get(field) for field in ("decision_id", "partition", "symbol")):
                findings.append(_finding("VISIBLE_POINT_DESCRIPTOR_IDENTITY_MISMATCH"))
            try:
                rebuilt = reconstruct_market_only_input(point)
            except IndependentAuditError as exc:
                findings.append(_finding(exc.code))
                continue
            counts["visible_inputs_reconstructed"] += 1
            observed_hash = rebuilt["market_input_sha256"]
            observed_ref_count = len(rebuilt["evidence_refs"])
            hashes.append({
                "decision_id": decision_id,
                "partition": point.get("partition"),
                "symbol": point.get("symbol"),
                "market_input_sha256": observed_hash,
                "evidence_ref_count": observed_ref_count,
            })
            if observed_hash == descriptor.get("market_input_sha256"):
                counts["market_input_hashes_matched"] += 1
            else:
                findings.append(_finding("MARKET_INPUT_HASH_MISMATCH"))
            if observed_ref_count == descriptor.get("evidence_ref_count"):
                counts["evidence_ref_counts_matched"] += 1
            else:
                findings.append(_finding("EVIDENCE_REF_COUNT_MISMATCH"))
        if point_ids != set(descriptor_by_id):
            findings.append(_finding("VISIBLE_POINT_DESCRIPTOR_SET_MISMATCH"))
    except IndependentAuditError as exc:
        findings.append(_finding(exc.code))

    code_path = Path(__file__)
    try:
        auditor_hash = _file_sha(code_path)
    except IndependentAuditError:
        auditor_hash = "UNAVAILABLE"
        findings.append(_finding("AUDITOR_CODE_HASH_UNAVAILABLE"))
    findings = sorted({item["code"]: item for item in findings}.values(), key=lambda item: item["code"])
    return {
        "schema_version": "v38-independent-visible-market-input-audit-1",
        "status": "VERIFIED" if not findings and counts["visible_inputs_reconstructed"] == EXPECTED_VISIBLE_POINT_COUNT else "FAILED_WITH_EVIDENCE",
        "frozen_hashes": {
            "plan_sha256_expected": PLAN_SHA256,
            "plan_sha256_observed": locals().get("plan_observed"),
            "plan_file_bytes_sha256_observed": locals().get("plan_file_observed"),
            "dataset_manifest_sha256_expected": DATASET_MANIFEST_SHA256,
            "dataset_manifest_sha256_observed": locals().get("manifest_observed"),
            "dataset_manifest_file_bytes_sha256_observed": locals().get("manifest_file_observed"),
            "visible_inputs_sha256_expected": VISIBLE_INPUTS_SHA256,
            "visible_inputs_sha256_observed": locals().get("inputs_observed"),
            "auditor_code_sha256": auditor_hash,
        },
        "scope": {
            "partitions": ["optimization", "validation"],
            "untouched_test_payloads_read": False,
            "visible_input_points_expected": EXPECTED_VISIBLE_POINT_COUNT,
            "sealed_untouched_test_points_expected": EXPECTED_SEALED_POINT_COUNT,
        },
        "counts": counts,
        "visible_input_hashes": hashes,
        "findings": findings,
        "sealed_test_payloads_read": False,
        "operations": {
            "network_calls": 0,
            "model_calls": 0,
            "orders_created": 0,
            "account_or_database_opened": False,
            "untouched_test_payloads_read": False,
            "source_writes": 0,
        },
    }


def _write_exclusive(path: Path, payload: dict[str, Any], forbidden_root: Path) -> None:
    resolved = path.resolve()
    root = forbidden_root.resolve()
    if resolved == root or root in resolved.parents:
        raise IndependentAuditError("REPORT_PATH_INSIDE_DATASET")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
    except FileExistsError as exc:
        raise IndependentAuditError("REPORT_ALREADY_EXISTS") from exc
    except OSError as exc:
        raise IndependentAuditError("REPORT_WRITE_FAILED") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    report = audit_visible_market_inputs(args.plan, args.dataset)
    try:
        _write_exclusive(args.output, report, args.dataset)
    except IndependentAuditError as exc:
        print(json.dumps({"status": "FAILED_WITH_EVIDENCE", "code": exc.code}, sort_keys=True))
        return 2
    print(json.dumps({"status": report["status"], "output_written": True}, sort_keys=True))
    return 0 if report["status"] == "VERIFIED" else 2


if __name__ == "__main__":
    sys.exit(main())
