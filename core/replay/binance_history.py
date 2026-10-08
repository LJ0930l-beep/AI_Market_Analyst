"""Checksum-verified Binance USD-M archives for an isolated technical proxy study.

This module never contacts a private API, a model server, Gate, or production storage.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from urllib.request import Request, urlopen
from zipfile import ZipFile


PUBLIC_ROOT = "https://data.binance.vision/data/futures/um/monthly"
SCHEMA_VERSION = "binance_um_research_history_v1"
DEFAULT_START = datetime(2025, 10, 1, tzinfo=timezone.utc)
DEFAULT_END = datetime(2026, 10, 1, tzinfo=timezone.utc)
MINUTE_MS = 60000
FUNDING_SLOT_MS = 8 * 60 * MINUTE_MS


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def file_sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _utc(value):
    point = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if point.tzinfo is None:
        raise ValueError("RESEARCH_TIMEZONE_REQUIRED")
    return point.astimezone(timezone.utc)


def _write_json(path, value):
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(_json(value), encoding="utf-8")
    temporary.replace(path)


def _put_meta(connection, key, value):
    encoded = _json(value)
    old = connection.execute("SELECT value_json FROM research_meta WHERE key=?", (key,)).fetchone()
    if not old or old[0] != encoded:
        connection.execute("INSERT OR REPLACE INTO research_meta VALUES(?,?)", (key, encoded))


def fetch_public_bytes(url):
    if not url.startswith(PUBLIC_ROOT + "/"):
        raise ValueError("BINANCE_PUBLIC_ARCHIVE_URL_REQUIRED")
    for attempt in range(4):
        try:
            request = Request(url, headers={"User-Agent": "AI-Market-Analyst-Research/1"})
            with urlopen(request, timeout=45) as response:
                if not response.geturl().startswith("https://data.binance.vision/"):
                    raise ValueError("BINANCE_UNEXPECTED_REDIRECT")
                data = response.read(32 * 1024 * 1024 + 1)
                if len(data) > 32 * 1024 * 1024:
                    raise ValueError("BINANCE_ARCHIVE_DOWNLOAD_SIZE_EXCEEDED")
                return data
        except Exception:
            if attempt == 3:
                raise
            time.sleep(min(4, attempt + 1))
    raise RuntimeError("BINANCE_PUBLIC_DOWNLOAD_FAILED")


@dataclass(frozen=True)
class Archive:
    symbol: str
    kind: str
    month: str

    @property
    def filename(self):
        suffix = "1m" if self.kind == "klines" else "fundingRate"
        return f"{self.symbol}-{suffix}-{self.month}.zip"

    @property
    def url(self):
        folder = f"klines/{self.symbol}/1m" if self.kind == "klines" else f"fundingRate/{self.symbol}"
        return f"{PUBLIC_ROOT}/{folder}/{self.filename}"


def _months(start, end):
    point = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while point < end:
        yield point.strftime("%Y-%m")
        point = point.replace(year=point.year + 1, month=1) if point.month == 12 else point.replace(month=point.month + 1)


def _official_digest(content, filename):
    lines = content.decode("utf-8-sig").strip().splitlines()
    if len(lines) != 1:
        raise ValueError("BINANCE_CHECKSUM_FORMAT_INVALID")
    match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?([^\s]+)", lines[0])
    if not match or match[2] != filename:
        raise ValueError("BINANCE_CHECKSUM_FILENAME_INVALID")
    return match[1].lower()


def _download_archive(archive, cache, fetch):
    path = cache / archive.filename
    checksum_path = cache / (archive.filename + ".CHECKSUM")
    provenance_path = cache / (archive.filename + ".provenance.json")
    if path.exists() and checksum_path.exists() and provenance_path.exists():
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        expected = _official_digest(checksum_path.read_bytes(), archive.filename)
        if (provenance.get("url") != archive.url or provenance.get("sha256") != expected
                or not provenance.get("fetched_at") or file_sha256(path) != expected):
            raise ValueError("BINANCE_CACHED_CHECKSUM_OR_PROVENANCE_INVALID:" + archive.filename)
        return archive, path, provenance
    checksum = fetch(archive.url + ".CHECKSUM")
    expected = _official_digest(checksum, archive.filename)
    data = fetch(archive.url)
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError("BINANCE_ARCHIVE_CHECKSUM_MISMATCH:" + archive.filename)
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(data)
    temporary.replace(path)
    checksum_path.write_bytes(checksum)
    provenance = {
        "url": archive.url, "checksum_url": archive.url + ".CHECKSUM",
        "fetched_at": datetime.now(timezone.utc).isoformat(), "sha256": actual,
        "bytes": len(data), "official_checksum_verified": True,
    }
    _write_json(provenance_path, provenance)
    return archive, path, provenance


def _prepare_db(path, identity):
    if path.exists():
        try:
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                          if not row[0].startswith("sqlite_")}
                if tables != {"bars", "funding", "research_meta"}:
                    raise ValueError("EXISTING_DB_IS_NOT_BINANCE_RESEARCH")
                row = connection.execute("SELECT value_json FROM research_meta WHERE key='identity'").fetchone()
                if not row or json.loads(row[0]) != identity:
                    raise ValueError("EXISTING_RESEARCH_DB_WINDOW_OR_SCHEMA_MISMATCH")
                for table, expected in (("bars", ["symbol", "open_time_ms", "open", "high", "low", "close", "volume"]),
                                        ("funding", ["symbol", "payment_time_ms", "interval_hours", "rate"]),
                                        ("research_meta", ["key", "value_json"])):
                    columns = list(connection.execute(f"PRAGMA table_info({table})"))
                    primary = [r[1] for r in sorted(columns, key=lambda r: r[5]) if r[5]]
                    wanted_primary = ["key"] if table == "research_meta" else expected[:2]
                    if [r[1] for r in columns] != expected or primary != wanted_primary:
                        raise ValueError("EXISTING_RESEARCH_DB_TABLE_SCHEMA_MISMATCH")
                manifest_path = path.parent / "manifest.json"
                if manifest_path.exists():
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if manifest.get("complete_data") and manifest.get("dataset_sha256") != file_sha256(path):
                        raise ValueError("EXISTING_RESEARCH_DB_DIGEST_MISMATCH")
        except sqlite3.DatabaseError as exc:
            raise ValueError("EXISTING_DB_IS_NOT_BINANCE_RESEARCH") from exc
        return sqlite3.connect(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb"):
        pass
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("CREATE TABLE bars(symbol TEXT,open_time_ms INTEGER,open REAL,high REAL,low REAL,close REAL,volume REAL,PRIMARY KEY(symbol,open_time_ms))")
        connection.execute("CREATE TABLE funding(symbol TEXT,payment_time_ms INTEGER,interval_hours INTEGER,rate REAL,PRIMARY KEY(symbol,payment_time_ms))")
        connection.execute("CREATE TABLE research_meta(key TEXT PRIMARY KEY,value_json TEXT)")
        connection.execute("INSERT INTO research_meta VALUES('identity',?)", (_json(identity),))
    return connection


def _archive_rows(archive, path, lower_ms, end_ms):
    expected_name = archive.filename[:-4] + ".csv"
    with ZipFile(path) as zipped:
        matches = [item for item in zipped.infolist() if item.filename == expected_name and not item.is_dir()]
        if len(matches) != 1:
            raise ValueError("BINANCE_EXPECTED_CSV_REQUIRED:" + archive.filename)
        if matches[0].file_size > 128 * 1024 * 1024:
            raise ValueError("BINANCE_CSV_SIZE_EXCEEDED")
        with zipped.open(matches[0]) as source:
            reader = csv.reader(io.TextIOWrapper(source, encoding="utf-8-sig", newline=""))
            seen = set()
            for row in reader:
                if not row:
                    continue
                if row[0] in {"open_time", "calc_time"}:
                    continue
                stamp = int(row[0])
                if stamp in seen:
                    raise ValueError("BINANCE_DUPLICATE_TIMESTAMP:" + archive.filename)
                seen.add(stamp)
                if archive.kind == "klines":
                    if len(row) < 7 or stamp % MINUTE_MS or int(row[6]) != stamp + MINUTE_MS - 1:
                        raise ValueError("BINANCE_BAR_TIMESTAMP_INVALID")
                    values = tuple(float(value) for value in row[1:6])
                    opening, high, low, close, volume = values
                    if (not all(math.isfinite(value) for value in values) or min(values[:4]) <= 0 or volume < 0
                            or high < max(opening, close, low) or low > min(opening, close)):
                        raise ValueError("BINANCE_BAR_OHLC_INVALID")
                    if lower_ms <= stamp < end_ms:
                        yield (archive.symbol, stamp, *values)
                else:
                    if len(row) != 3:
                        raise ValueError("BINANCE_FUNDING_COLUMNS_INVALID")
                    interval, rate = int(row[1]), float(row[2])
                    if interval != 8 or not math.isfinite(rate):
                        raise ValueError("BINANCE_FUNDING_INTERVAL_OR_RATE_INVALID")
                    if lower_ms <= stamp < end_ms:
                        yield (archive.symbol, stamp, interval, rate)


def _coverage(connection, symbols, warmup_ms, start_ms, end_ms):
    result = {}
    for symbol in symbols:
        for values in connection.execute("SELECT open,high,low,close,volume FROM bars WHERE symbol=?", (symbol,)):
            if (any(type(value) not in {int, float} or not math.isfinite(value) for value in values)
                    or min(values[:4]) <= 0 or values[4] < 0
                    or values[1] < max(values[0], values[2], values[3])
                    or values[2] > min(values[0], values[3])):
                raise ValueError("BINANCE_RESEARCH_DB_OHLC_INVALID")
        count = 0
        first = last = None
        gaps = []
        for (stamp,) in connection.execute("SELECT open_time_ms FROM bars WHERE symbol=? ORDER BY open_time_ms", (symbol,)):
            if first is None:
                first = stamp
            if last is not None and stamp != last + MINUTE_MS:
                gaps.append({"after_ms": last, "before_ms": stamp})
            last = stamp
            count += 1
        expected = (end_ms - warmup_ms) // MINUTE_MS
        missing = expected - count
        rows = list(connection.execute("SELECT payment_time_ms,interval_hours,rate FROM funding WHERE symbol=? ORDER BY payment_time_ms", (symbol,)))
        if any(interval != 8 or type(rate) not in {int, float} or not math.isfinite(rate) for _, interval, rate in rows):
            raise ValueError("BINANCE_RESEARCH_DB_FUNDING_INVALID")
        stamps = [row[0] for row in rows]
        slots = [stamp // FUNDING_SLOT_MS * FUNDING_SLOT_MS for stamp in stamps]
        expected_slots = set(range(start_ms, end_ms, FUNDING_SLOT_MS))
        duplicate_slots = len(slots) - len(set(slots))
        missing_slots = sorted(expected_slots - set(slots))
        result[symbol] = {
            "bars": {"expected": expected, "actual": count, "missing_minutes": missing, "duplicate_count": 0,
                     "first_open_time_ms": first, "last_open_time_ms": last, "gap_count": len(gaps), "gap_examples": gaps[:10],
                     "evaluation_minutes": (end_ms - start_ms) // MINUTE_MS, "warmup_minutes": (start_ms - warmup_ms) // MINUTE_MS,
                     "complete": missing == 0 and not gaps and first == warmup_ms and last == end_ms - MINUTE_MS},
            "funding": {"expected": len(expected_slots), "actual": len(rows), "missing_slots": missing_slots,
                        "duplicate_slots": duplicate_slots, "first_payment_time_ms": stamps[0] if stamps else None,
                        "last_payment_time_ms": stamps[-1] if stamps else None,
                        "complete": not missing_slots and not duplicate_slots and set(slots) == expected_slots,
                        "time_basis": "ORIGINAL_CALC_TIME_MS_NOT_SNAPPED_TO_8H_SLOT"},
        }
    return result


def freeze_binance_history(output_dir, *, start=DEFAULT_START, end=DEFAULT_END, warmup_days=10,
                           symbols=("BTCUSDT", "ETHUSDT"), workers=4, fetch=None, progress=None):
    """Resume only an identical research DB; otherwise never overwrite existing DBs."""
    start, end = _utc(start), _utc(end)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    if (end <= start or start.microsecond or end.microsecond or start_ms % FUNDING_SLOT_MS or end_ms % FUNDING_SLOT_MS
            or type(warmup_days) is not int or not 0 <= warmup_days <= 365
            or type(workers) is not int or not 1 <= workers <= 4 or not symbols or len(set(symbols)) != len(symbols)
            or any(symbol not in {"BTCUSDT", "ETHUSDT"} for symbol in symbols)):
        raise ValueError("BINANCE_RESEARCH_WINDOW_OR_SYMBOLS_INVALID")
    warmup = start - timedelta(days=warmup_days)
    warmup_ms = int(warmup.timestamp() * 1000)
    output = Path(output_dir).resolve()
    identity = {"schema_version": SCHEMA_VERSION, "research_only": True, "exchange": "BINANCE_UM",
                "window_start": start.isoformat(), "window_end": end.isoformat(), "warmup_days": warmup_days,
                "symbols": list(symbols), "not_gate_data": True}
    connection = _prepare_db(output / "research.sqlite3", identity)
    cache = output / "raw_cache"
    cache.mkdir(exist_ok=True)
    archives = [Archive(symbol, kind, month) for symbol in symbols for kind, lower in
                (("klines", warmup), ("fundingRate", start)) for month in _months(lower, end)]
    notify = progress or (lambda *_args: None)
    source_records = []
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = {pool.submit(_download_archive, archive, cache, fetch or fetch_public_bytes): archive for archive in archives}
            for completed, future in enumerate(as_completed(pending), 1):
                archive, path, provenance = future.result()
                source_records.append({"symbol": archive.symbol, "kind": archive.kind, "month": archive.month, **provenance})
                key = "completed_archive:" + archive.filename
                old = connection.execute("SELECT value_json FROM research_meta WHERE key=?", (key,)).fetchone()
                if old:
                    if json.loads(old[0])["sha256"] != provenance["sha256"]:
                        raise ValueError("BINANCE_ARCHIVE_CHANGED_AFTER_INGEST:" + archive.filename)
                    notify({"event": "archive_resumed", "completed": completed, "total": len(archives), "archive": archive.filename})
                    continue
                table = "bars" if archive.kind == "klines" else "funding"
                placeholders = ",".join("?" for _ in range(7 if table == "bars" else 4))
                lower_ms = warmup_ms if table == "bars" else start_ms
                with connection:
                    before = connection.total_changes
                    try:
                        connection.executemany(f"INSERT INTO {table} VALUES({placeholders})", _archive_rows(archive, path, lower_ms, end_ms))
                    except sqlite3.IntegrityError as exc:
                        raise ValueError("BINANCE_DUPLICATE_TIMESTAMP") from exc
                    inserted = connection.total_changes - before
                    connection.execute("INSERT INTO research_meta VALUES(?,?)", (key, _json(provenance)))
                notify({"event": "archive_ingested", "completed": completed, "total": len(archives), "archive": archive.filename,
                        "inserted_rows": inserted})
        coverage = _coverage(connection, symbols, warmup_ms, start_ms, end_ms)
        complete = all(item[part]["complete"] for item in coverage.values() for part in ("bars", "funding"))
        old_manifest = connection.execute("SELECT value_json FROM research_meta WHERE key='manifest_without_dataset_sha256'").fetchone()
        previous = json.loads(old_manifest[0]) if old_manifest else {}
        metadata = {**identity, "warmup_start": warmup.isoformat(), "coverage": coverage, "complete_data": complete,
                    "source_type": "OFFICIAL_MONTHLY_PUBLIC_ARCHIVES", "analysis_type": "TECHNICAL_RULE_PROXY_NOT_REAL_AI_OR_GATE",
                    "historical_availability_assumption": "1M_BAR_KNOWN_AT_CLOSE;_FUNDING_AT_ORIGINAL_CALC_TIME",
                    "archived_files": sorted(source_records, key=lambda record: record["url"]),
                    "frozen_at": previous.get("frozen_at") or datetime.now(timezone.utc).isoformat()}
        with connection:
            _put_meta(connection, "coverage", coverage)
            _put_meta(connection, "manifest_without_dataset_sha256", metadata)
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("BINANCE_RESEARCH_DB_INTEGRITY_FAILED")
    finally:
        connection.close()
    metadata["dataset_sha256"] = file_sha256(output / "research.sqlite3")
    _write_json(output / "coverage.json", {**identity, "coverage": coverage, "complete_data": complete})
    _write_json(output / "manifest.json", metadata)
    notify({"event": "complete", "complete_data": complete, "dataset_sha256": metadata["dataset_sha256"]})
    if not complete:
        raise ValueError("BINANCE_HISTORY_COVERAGE_INCOMPLETE:" + str(output / "coverage.json"))
    return metadata
