"""Read-only, causal reconstruction of historical 5m/15m/1h/4h proxy inputs."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from core.replay.pa_decision_quality_v36.context import (
    REQUIRED_TIMEFRAMES,
    build_context,
)

FRAMES_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
SOURCE_KIND = "BINANCE_UM_OFFICIAL_MONTHLY_ARCHIVE_RECONSTRUCTED"
PRICE_EVIDENCE_GRADE = "VERIFIED_ARCHIVE_RECONSTRUCTION"
AVAILABILITY_EVIDENCE_GRADE = "ASSUMED_PROXY"
AVAILABILITY_BASIS = "ASSUMED_PROXY_60S_AFTER_BAR_END_NOT_OBSERVED"
QUALITY_STATUS = "FROZEN_RESEARCH"
DEFAULT_AVAILABILITY_DELAY_SECONDS = 60
MINIMUM_CONTEXT_BARS = 32
MAXIMUM_CONTEXT_BARS = 48
HISTORY_WINDOW = timedelta(days=8)


class EvidenceBuildError(ValueError):
    """Stable failure code for incomplete or unsafe reconstruction inputs."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvidenceBuildError("RECONSTRUCTION_INPUT_NOT_CANONICAL_JSON") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise EvidenceBuildError("SOURCE_FILE_UNAVAILABLE") from exc
    return digest.hexdigest()


def open_sqlite_readonly(path: Path) -> sqlite3.Connection:
    """Open a database with both URI read-only and SQLite query-only guards."""
    if not path.is_file():
        raise EvidenceBuildError("SOURCE_DATABASE_UNAVAILABLE")
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        connection.row_factory = sqlite3.Row
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            connection.close()
            raise EvidenceBuildError("SQLITE_QUERY_ONLY_NOT_ENABLED")
        return connection
    except sqlite3.Error as exc:
        raise EvidenceBuildError("SOURCE_DATABASE_READ_ONLY_OPEN_FAILED") from exc


def _parse_time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise EvidenceBuildError("DECISION_TIME_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceBuildError("DECISION_TIME_MUST_INCLUDE_TIMEZONE")
    return parsed.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _safe_number(value: Any) -> float:
    if isinstance(value, bool):
        raise EvidenceBuildError("SOURCE_OHLCV_INVALID")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EvidenceBuildError("SOURCE_OHLCV_INVALID") from exc
    if not math.isfinite(number):
        raise EvidenceBuildError("SOURCE_OHLCV_NONFINITE")
    return number


def _manifest(archive_directory: Path) -> tuple[dict[str, Any], Path, str, dict[tuple[str, str], dict[str, Any]]]:
    manifest_path = archive_directory / "manifest.json"
    database_path = archive_directory / "research.sqlite3"
    try:
        record = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceBuildError("ARCHIVE_MANIFEST_UNAVAILABLE") from exc
    if (not isinstance(record, dict) or record.get("complete_data") is not True
            or record.get("research_only") is not True or record.get("not_gate_data") is not True
            or record.get("exchange") != "BINANCE_UM"
            or record.get("source_type") != "OFFICIAL_MONTHLY_PUBLIC_ARCHIVES"):
        raise EvidenceBuildError("ARCHIVE_MANIFEST_SCOPE_INVALID")
    database_hash = sha256_file(database_path)
    if record.get("dataset_sha256") != database_hash:
        raise EvidenceBuildError("ARCHIVE_DATABASE_HASH_MISMATCH")
    archived_files = record.get("archived_files")
    if not isinstance(archived_files, list):
        raise EvidenceBuildError("ARCHIVE_FILE_MANIFEST_INVALID")
    archives: dict[tuple[str, str], dict[str, Any]] = {}
    for item in archived_files:
        if not isinstance(item, dict):
            raise EvidenceBuildError("ARCHIVE_FILE_MANIFEST_INVALID")
        if item.get("kind") != "klines" or "/1m/" not in str(item.get("url") or ""):
            continue
        symbol, month = item.get("symbol"), item.get("month")
        if not isinstance(symbol, str) or not isinstance(month, str):
            raise EvidenceBuildError("ARCHIVE_FILE_MANIFEST_INVALID")
        key = (symbol, month)
        if key in archives:
            raise EvidenceBuildError("ARCHIVE_FILE_MANIFEST_DUPLICATE")
        archives[key] = item
    return record, database_path, canonical_sha256(record), archives


def _verify_raw_archive(archive_directory: Path, item: dict[str, Any]) -> dict[str, Any]:
    url = item.get("url")
    if not isinstance(url, str):
        raise EvidenceBuildError("ARCHIVE_URL_MISSING")
    filename = Path(urlsplit(url).path).name
    if not filename:
        raise EvidenceBuildError("ARCHIVE_URL_INVALID")
    path = archive_directory / "raw_cache" / filename
    if not path.is_file():
        raise EvidenceBuildError("RAW_ARCHIVE_UNAVAILABLE")
    actual_hash = sha256_file(path)
    try:
        expected_size = int(item.get("bytes"))
    except (TypeError, ValueError) as exc:
        raise EvidenceBuildError("RAW_ARCHIVE_MANIFEST_INVALID") from exc
    if (item.get("official_checksum_verified") is not True
            or item.get("sha256") != actual_hash or path.stat().st_size != expected_size):
        raise EvidenceBuildError("RAW_ARCHIVE_CHECKSUM_MISMATCH")
    return {
        "path": str(path),
        "sha256": actual_hash,
        "bytes": expected_size,
        "official_checksum_verified": True,
        "url": url,
    }


def _validate_minute_rows(rows: list[dict[str, Any]], *, expected_count: int) -> None:
    if len(rows) != expected_count or not rows:
        raise EvidenceBuildError("SOURCE_MINUTE_COVERAGE_INCOMPLETE")
    previous: int | None = None
    for row in rows:
        opened = row.get("open_time_ms")
        if isinstance(opened, bool) or not isinstance(opened, int):
            raise EvidenceBuildError("SOURCE_MINUTE_TIMESTAMP_INVALID")
        if previous is not None and opened - previous != 60_000:
            raise EvidenceBuildError("SOURCE_MINUTE_DUPLICATE_OR_GAP")
        previous = opened
        opening, high, low, close, volume = (
            _safe_number(row.get(key)) for key in ("open", "high", "low", "close", "volume")
        )
        if (min(opening, high, low, close) <= 0 or volume < 0
                or high < max(opening, close, low) or low > min(opening, close)):
            raise EvidenceBuildError("SOURCE_OHLCV_GEOMETRY_INVALID")


def aggregate_timeframe_bars(
    minute_rows: Iterable[dict[str, Any]], *, symbol: str,
    timeframe: str, decision_time: datetime,
    archive_hash_by_month: dict[str, str], manifest_sha256: str,
    source_database_sha256: str,
    availability_delay_seconds: int = DEFAULT_AVAILABILITY_DELAY_SECONDS,
) -> list[dict[str, Any]]:
    """Aggregate complete minute rows without interpolating gaps or using future bars."""
    if timeframe not in FRAMES_MINUTES:
        raise EvidenceBuildError("TIMEFRAME_UNSUPPORTED")
    if (isinstance(availability_delay_seconds, bool)
            or not isinstance(availability_delay_seconds, int)
            or availability_delay_seconds < 0):
        raise EvidenceBuildError("AVAILABILITY_DELAY_INVALID")
    point = _parse_time(decision_time)
    minute_rows = list(minute_rows)
    _validate_minute_rows(minute_rows, expected_count=len(minute_rows))
    width_minutes = FRAMES_MINUTES[timeframe]
    width_ms = width_minutes * 60_000
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in minute_rows:
        opened_ms = row.get("open_time_ms")
        if isinstance(opened_ms, bool) or not isinstance(opened_ms, int):
            raise EvidenceBuildError("SOURCE_MINUTE_TIMESTAMP_INVALID")
        bucket_ms = opened_ms // width_ms * width_ms
        groups[bucket_ms].append(row)

    output: list[dict[str, Any]] = []
    delay = timedelta(seconds=availability_delay_seconds)
    for bucket_ms, group in sorted(groups.items()):
        group = sorted(group, key=lambda item: item["open_time_ms"])
        expected_times = [bucket_ms + minute * 60_000 for minute in range(width_minutes)]
        actual_times = [item["open_time_ms"] for item in group]
        # The first/last clipped bucket may be partial because the requested
        # history window starts/ends mid-bar. It is omitted, never interpolated.
        if actual_times != expected_times:
            continue
        start = datetime.fromtimestamp(bucket_ms / 1000, UTC)
        end = start + timedelta(minutes=width_minutes)
        available = end + delay
        if end >= point or available >= point:
            continue
        month_hashes: set[str] = set()
        for item in group:
            month = datetime.fromtimestamp(item["open_time_ms"] / 1000, UTC).strftime("%Y-%m")
            archive_hash = archive_hash_by_month.get(month)
            if not archive_hash:
                raise EvidenceBuildError("RAW_ARCHIVE_PROVENANCE_MISSING")
            month_hashes.add(archive_hash)
        source_file_hashes = sorted(month_hashes)
        source_file_hash = (
            source_file_hashes[0] if len(source_file_hashes) == 1
            else canonical_sha256(source_file_hashes)
        )
        opening = _safe_number(group[0].get("open"))
        high = max(_safe_number(item.get("high")) for item in group)
        low = min(_safe_number(item.get("low")) for item in group)
        close = _safe_number(group[-1].get("close"))
        volume = math.fsum(_safe_number(item.get("volume")) for item in group)
        if (min(opening, high, low, close) <= 0 or volume < 0
                or high < max(opening, close, low) or low > min(opening, close)):
            raise EvidenceBuildError("AGGREGATED_OHLCV_GEOMETRY_INVALID")
        output.append({
            "symbol": symbol,
            "timeframe": timeframe,
            "bar_start": _stamp(start),
            "bar_end": _stamp(end),
            "available_at": _stamp(available),
            "available_at_basis": AVAILABILITY_BASIS,
            "available_at_evidence_grade": AVAILABILITY_EVIDENCE_GRADE,
            "source": SOURCE_KIND,
            "source_exchange": "BINANCE_UM",
            "source_file_hash": source_file_hash,
            "source_file_hashes": source_file_hashes,
            "source_manifest_sha256": manifest_sha256,
            "source_database_sha256": source_database_sha256,
            "price_evidence_grade": PRICE_EVIDENCE_GRADE,
            "quality_status": QUALITY_STATUS,
            "is_closed": True,
            "volume_unit": "BINANCE_BASE_ASSET_VOLUME",
            "open": opening,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        })
    return output


def build_reconstructed_point(
    archive_directory: Path, *, symbol: str, decision_time: str,
    decision_id: str, partition: str,
    availability_delay_seconds: int = DEFAULT_AVAILABILITY_DELAY_SECONDS,
) -> dict[str, Any]:
    """Build a V36-compatible decision point and retain provenance for every bar."""
    archive_directory = Path(archive_directory).resolve()
    point_time = _parse_time(decision_time)
    if point_time.second or point_time.microsecond:
        raise EvidenceBuildError("DECISION_TIME_MUST_ALIGN_TO_MINUTE")
    if not isinstance(symbol, str) or not symbol.strip():
        raise EvidenceBuildError("SYMBOL_INVALID")
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise EvidenceBuildError("DECISION_ID_INVALID")
    if partition not in {"optimization", "validation", "untouched_test"}:
        raise EvidenceBuildError("PARTITION_INVALID")

    source_manifest, database_path, manifest_hash, archives = _manifest(archive_directory)
    if symbol not in source_manifest.get("symbols", []):
        raise EvidenceBuildError("SYMBOL_NOT_IN_SOURCE_MANIFEST")
    window_start = _parse_time(source_manifest.get("window_start"))
    window_end = _parse_time(source_manifest.get("window_end"))
    warmup_start = _parse_time(source_manifest.get("warmup_start"))
    query_start = point_time - HISTORY_WINDOW
    if query_start < warmup_start or not (window_start <= point_time < window_end):
        raise EvidenceBuildError("DECISION_OUTSIDE_VERIFIED_SOURCE_WINDOW")

    month_first = query_start.strftime("%Y-%m")
    month_last = (point_time - timedelta(minutes=1)).strftime("%Y-%m")
    needed_months = sorted({month_first, month_last})
    archive_details: list[dict[str, Any]] = []
    archive_hash_by_month: dict[str, str] = {}
    for month in needed_months:
        item = archives.get((symbol, month))
        if item is None:
            raise EvidenceBuildError("RAW_ARCHIVE_PROVENANCE_MISSING")
        details = _verify_raw_archive(archive_directory, item)
        archive_details.append(details)
        archive_hash_by_month[month] = details["sha256"]

    start_ms = int(query_start.timestamp() * 1000)
    decision_ms = int(point_time.timestamp() * 1000)
    expected_count = int(HISTORY_WINDOW.total_seconds() / 60)
    db = open_sqlite_readonly(database_path)
    try:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(bars)")}
        required_columns = {"symbol", "open_time_ms", "open", "high", "low", "close", "volume"}
        if not required_columns.issubset(columns):
            raise EvidenceBuildError("SOURCE_BAR_SCHEMA_INVALID")
        rows = [dict(row) for row in db.execute(
            "SELECT symbol,open_time_ms,open,high,low,close,volume FROM bars "
            "WHERE symbol=? AND open_time_ms>=? AND open_time_ms<? ORDER BY open_time_ms",
            (symbol, start_ms, decision_ms),
        )]
    except sqlite3.Error as exc:
        raise EvidenceBuildError("SOURCE_DATABASE_READ_FAILED") from exc
    finally:
        db.close()
    if any(row.get("symbol") != symbol for row in rows):
        raise EvidenceBuildError("SOURCE_SYMBOL_MISMATCH")
    _validate_minute_rows(rows, expected_count=expected_count)
    source_database_hash = str(source_manifest["dataset_sha256"])

    bars_by_timeframe: dict[str, list[dict[str, Any]]] = {}
    for timeframe in REQUIRED_TIMEFRAMES:
        bars = aggregate_timeframe_bars(
            rows, symbol=symbol, timeframe=timeframe, decision_time=point_time,
            archive_hash_by_month=archive_hash_by_month,
            manifest_sha256=manifest_hash,
            source_database_sha256=source_database_hash,
            availability_delay_seconds=availability_delay_seconds,
        )
        bars_by_timeframe[timeframe] = bars[-MAXIMUM_CONTEXT_BARS:]

    context = build_context(
        bars_by_timeframe, point_time,
        required_timeframes=REQUIRED_TIMEFRAMES,
        minimum_bars=MINIMUM_CONTEXT_BARS,
        maximum_bars=MAXIMUM_CONTEXT_BARS,
    )
    if context.status != "READY":
        reasons = sorted({
            str(frame.get("reason"))
            for frame in context.frames.values()
            if isinstance(frame, dict) and frame.get("status") != "READY"
        })
        raise EvidenceBuildError("V36_CONTEXT_INVALID:" + ",".join(reasons))
    for timeframe, bars in context.bars.items():
        if len(bars) < MINIMUM_CONTEXT_BARS:
            raise EvidenceBuildError("V36_CONTEXT_BAR_COUNT_INVALID")
        for bar in bars:
            if bar.end >= point_time or bar.available_at >= point_time:
                raise EvidenceBuildError("FUTURE_OR_UNAVAILABLE_BAR_INCLUDED")
        for swing in context.frames[timeframe]["objective_facts"]["confirmed_swings"]:
            if max(_parse_time(swing[key]) for key in ("confirmed_at", "available_at", "known_at")) >= point_time:
                raise EvidenceBuildError("FUTURE_SWING_CONFIRMATION_INCLUDED")

    evidence_material = {
        "schema_version": "pa-decision-quality-v37.1/reconstructed-market-input-1",
        "decision_id": decision_id,
        "decision_time": _stamp(point_time),
        "symbol": symbol,
        "partition": partition,
        "price_evidence_grade": PRICE_EVIDENCE_GRADE,
        "availability_evidence_grade": AVAILABILITY_EVIDENCE_GRADE,
        "availability_delay_seconds": availability_delay_seconds,
        "archive_manifest_sha256": manifest_hash,
        "source_database_sha256": source_database_hash,
        "source_archives": sorted(archive_details, key=lambda item: item["path"]),
        "bars_by_timeframe": bars_by_timeframe,
        "v36_context_sha256": context.input_sha256,
    }
    evidence_hash = canonical_sha256(evidence_material)
    return {
        "schema_version": "pa-decision-quality-v37.1/reconstructed-market-input-1",
        "dataset_kind": "RECONSTRUCTED_MARKET_BENCHMARK",
        "decision_id": decision_id,
        "decision_time": _stamp(point_time),
        "symbol": symbol,
        "partition": partition,
        "price_evidence_grade": PRICE_EVIDENCE_GRADE,
        "availability_evidence_grade": AVAILABILITY_EVIDENCE_GRADE,
        "availability_delay_seconds": availability_delay_seconds,
        "availability_is_observed_fact": False,
        "not_a_v25_original_decision": True,
        "bars_by_timeframe": bars_by_timeframe,
        "context": context.record(),
        "context_input_sha256": context.input_sha256,
        "evidence_input_sha256": evidence_hash,
        "source_manifest_sha256": manifest_hash,
        "source_database_sha256": source_database_hash,
        "source_archives": sorted(archive_details, key=lambda item: item["path"]),
    }
