"""Isolated archive fixtures for the Binance research-history collector.

These ZIP bytes are test fixtures, never historical-market acceptance evidence.
Every download is replaced by a local callable and every database is temporary.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import socket
import sqlite3
from urllib.parse import urlsplit
import zipfile

import pytest

from core.replay.binance_history import freeze_binance_history


START = datetime(2025, 10, 1, tzinfo=timezone.utc)
END = START + timedelta(hours=8)
SYMBOL = "BTCUSDT"
MINUTE_MS = 60_000
FUNDING_MS = 8 * 60 * 60 * 1000
KLINE_HEADER = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
]
FUNDING_HEADER = ["calc_time", "funding_interval_hours", "last_funding_rate"]


def milliseconds(point: datetime) -> int:
    return int(point.timestamp() * 1000)


def kline_rows(start=START, end=END):
    return [
        [stamp, "100", "102", "99", "101", "2.5", stamp + MINUTE_MS - 1,
         "252.5", "7", "1.0", "101", "0"]
        for stamp in range(milliseconds(start), milliseconds(end), MINUTE_MS)
    ]


def funding_rows(start=START, end=END, *, offsets=None):
    stamps = list(range(milliseconds(start), milliseconds(end), FUNDING_MS))
    offsets = offsets or [17] * len(stamps)
    return [[stamp + offset, "8", "0.0001"] for stamp, offset in zip(stamps, offsets)]


def csv_bytes(header, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    if header:
        writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


class ArchiveFeed:
    """Supply only the URL-shaped fixture bytes requested by the collector."""

    def __init__(self, *, bars=None, funding=None, start=START, end=END,
                 extra_members=None, headers=True, bad_checksum=False,
                 wrong_member=False, duplicate_member=False, partition_months=False):
        self.bars = kline_rows(start, end) if bars is None else bars
        self.funding = funding_rows(start, end) if funding is None else funding
        self.extra_members = extra_members or {}
        self.headers = headers
        self.bad_checksum = bad_checksum
        self.wrong_member = wrong_member
        self.duplicate_member = duplicate_member
        self.partition_months = partition_months
        self.calls = []
        self.archives = {}

    def archive(self, url):
        archive_url = url.removesuffix(".CHECKSUM")
        if archive_url not in self.archives:
            basename = Path(urlsplit(archive_url).path).name
            assert basename.endswith(".zip"), archive_url
            if "/klines/" in archive_url:
                header, rows = KLINE_HEADER, self.bars
            elif "/fundingRate/" in archive_url:
                header, rows = FUNDING_HEADER, self.funding
            else:
                raise AssertionError(f"Unexpected archive URL: {archive_url}")
            if self.partition_months:
                month = basename[-11:-4]
                rows = [row for row in rows if datetime.fromtimestamp(int(row[0]) / 1000, timezone.utc).strftime("%Y-%m") == month]
            body = csv_bytes(header if self.headers else None, rows)
            expected_member = basename.removesuffix(".zip") + ".csv"
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("wrong.csv" if self.wrong_member else expected_member, body)
                for name, payload in self.extra_members.items():
                    archive.writestr(name, payload)
                if self.duplicate_member:
                    archive.writestr(expected_member, body)
            self.archives[archive_url] = stream.getvalue()
        return self.archives[archive_url]

    def __call__(self, url):
        self.calls.append(url)
        payload = self.archive(url)
        if url.endswith(".CHECKSUM"):
            digest = "0" * 64 if self.bad_checksum else hashlib.sha256(payload).hexdigest()
            name = Path(urlsplit(url.removesuffix(".CHECKSUM")).path).name
            return f"{digest}  {name}\n".encode("ascii")
        return payload


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Archive unit tests must not open network connections")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def freeze(output, feed, *, start=START, end=END, warmup_days=0, symbols=(SYMBOL,), workers=1):
    return freeze_binance_history(
        output, start=start, end=end, warmup_days=warmup_days,
        symbols=symbols, workers=workers, fetch=feed,
    )


def freeze_incomplete(output, feed, **arguments):
    with pytest.raises(ValueError, match="BINANCE_HISTORY_COVERAGE_INCOMPLETE"):
        freeze(output, feed, **arguments)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    coverage = json.loads((output / "coverage.json").read_text(encoding="utf-8"))
    assert manifest["complete_data"] is coverage["complete_data"] is False
    assert manifest["coverage"] == coverage["coverage"]
    return manifest


def test_complete_window_has_verified_archives_and_exact_coverage(tmp_path):
    feed = ArchiveFeed()
    result = freeze(tmp_path, feed)

    assert result["complete_data"] is True
    assert result["window_start"] == START.isoformat()
    assert result["window_end"] == END.isoformat()
    assert result["symbols"] == [SYMBOL]
    assert len(result["dataset_sha256"]) == 64
    bars = result["coverage"][SYMBOL]["bars"]
    assert bars["expected"] == bars["actual"] == 480
    assert bars["missing_minutes"] == bars["duplicate_count"] == 0
    assert bars["first_open_time_ms"] == milliseconds(START)
    assert bars["last_open_time_ms"] == milliseconds(END) - MINUTE_MS
    funding = result["coverage"][SYMBOL]["funding"]
    assert funding["expected"] == funding["actual"] == 1
    assert funding["missing_slots"] == []
    assert funding["duplicate_slots"] == 0
    assert all(url.startswith("https://data.binance.vision/") for url in feed.calls)
    assert any(url.endswith(".CHECKSUM") for url in feed.calls)
    assert (tmp_path / "research.sqlite3").is_file()
    assert json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))["complete_data"] is True
    assert (tmp_path / "coverage.json").is_file()


def test_official_checksum_mismatch_rejects_before_csv_read(tmp_path, monkeypatch):
    original_open = zipfile.ZipFile.open

    def forbidden(archive, name, mode="r", *args, **kwargs):
        if mode == "r":
            raise AssertionError("Unverified archive must not be parsed")
        return original_open(archive, name, mode, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", forbidden)
    with pytest.raises(ValueError, match="BINANCE_ARCHIVE_CHECKSUM_MISMATCH"):
        freeze(tmp_path, ArchiveFeed(bad_checksum=True))
    assert not (tmp_path / "manifest.json").exists()


@pytest.mark.parametrize("checksum", [
    b"not a sha256 checksum\n",
    b"0" * 64 + b"  unexpected.zip\n",
    b"0" * 64 + b"  BTCUSDT-1m-2025-10.zip\nsecond line\n",
])
def test_invalid_official_checksum_record_rejects_before_zip_fetch(tmp_path, checksum):
    calls = []

    def fetch(url):
        calls.append(url)
        assert url.endswith(".CHECKSUM"), "Bad checksum must be rejected before ZIP download"
        return checksum

    with pytest.raises(ValueError, match="BINANCE_CHECKSUM_(FORMAT|FILENAME)_INVALID"):
        freeze(tmp_path, fetch)
    assert calls and all(url.endswith(".CHECKSUM") for url in calls)


def test_zip_reads_only_expected_csv_and_never_extracts(tmp_path, monkeypatch):
    opened = []
    original_open = zipfile.ZipFile.open

    def record_open(archive, name, mode="r", *args, **kwargs):
        if mode == "r":
            member = name.filename if isinstance(name, zipfile.ZipInfo) else name
            opened.append(member)
            assert member == Path(archive.filename).name.removesuffix(".zip") + ".csv"
        return original_open(archive, name, mode, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("Collector must read expected CSV without ZIP extraction")

    monkeypatch.setattr(zipfile.ZipFile, "open", record_open)
    monkeypatch.setattr(zipfile.ZipFile, "extract", forbidden)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", forbidden)
    feed = ArchiveFeed(extra_members={"../../escaped.txt": b"evil", "unrelated.csv": b"invalid csv"})
    result = freeze(tmp_path / "research", feed)

    assert result["complete_data"] is True
    assert len(opened) == 2
    assert all(name.endswith(".csv") and "/" not in name and "\\" not in name for name in opened)
    assert not (tmp_path / "escaped.txt").exists()


def test_zip_missing_expected_member_rejects(tmp_path):
    with pytest.raises(ValueError, match="BINANCE_EXPECTED_CSV_REQUIRED"):
        freeze(tmp_path, ArchiveFeed(wrong_member=True))


def test_zip_duplicate_expected_member_rejects(tmp_path):
    with pytest.warns(UserWarning, match="Duplicate name"):
        with pytest.raises(ValueError, match="BINANCE_EXPECTED_CSV_REQUIRED"):
            freeze(tmp_path, ArchiveFeed(duplicate_member=True))


def test_same_window_dedicated_research_database_resumes_without_fetch(tmp_path):
    first = freeze(tmp_path, ArchiveFeed())

    def forbidden(url):
        raise AssertionError(f"Verified completed archive must resume locally: {url}")

    second = freeze(tmp_path, forbidden)
    assert second["complete_data"] is True
    assert second["dataset_sha256"] == first["dataset_sha256"]
    assert second["coverage"] == first["coverage"]


def test_resume_refuses_tampered_cached_zip_without_fetch(tmp_path):
    freeze(tmp_path, ArchiveFeed())
    archive = next((tmp_path / "raw_cache").glob("*-1m-*.zip"))
    archive.write_bytes(archive.read_bytes() + b"tampered")
    before = (tmp_path / "research.sqlite3").read_bytes()

    def forbidden(url):
        raise AssertionError("Cache integrity rejection must not refetch or overwrite")

    with pytest.raises(ValueError, match="BINANCE_CACHED_CHECKSUM_OR_PROVENANCE_INVALID"):
        freeze(tmp_path, forbidden)
    assert (tmp_path / "research.sqlite3").read_bytes() == before


@pytest.mark.parametrize("existing_kind", ["sqlite", "empty", "non_sqlite"])
def test_arbitrary_existing_database_refused_without_mutation_or_fetch(tmp_path, existing_kind):
    path = tmp_path / "research.sqlite3"
    if existing_kind == "sqlite":
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE production_records(secret TEXT)")
            connection.execute("INSERT INTO production_records VALUES ('keep me')")
    elif existing_kind == "empty":
        path.write_bytes(b"")
    else:
        path.write_bytes(b"existing unrelated file")
    before = path.read_bytes()

    def forbidden(url):
        raise AssertionError("Existing database identity must be checked before fetch")

    with pytest.raises(ValueError, match="EXISTING_DB_IS_NOT_BINANCE_RESEARCH"):
        freeze(tmp_path, forbidden)
    assert path.read_bytes() == before
    assert not (tmp_path / "raw_cache").exists()
    assert not (tmp_path / "manifest.json").exists()


@pytest.mark.parametrize("changed", ["window", "symbols", "warmup"])
def test_different_research_identity_refused_without_overwriting(tmp_path, changed):
    freeze(tmp_path, ArchiveFeed())
    files = {name: (tmp_path / name).read_bytes() for name in ("research.sqlite3", "manifest.json", "coverage.json")}

    def forbidden(url):
        raise AssertionError("Research identity mismatch must be checked before fetch")

    arguments = {}
    if changed == "window":
        arguments = {"start": START + timedelta(hours=8), "end": END + timedelta(hours=8)}
    elif changed == "symbols":
        arguments = {"symbols": ("ETHUSDT",)}
    else:
        arguments = {"warmup_days": 1}
    with pytest.raises(ValueError, match="EXISTING_RESEARCH_DB_WINDOW_OR_SCHEMA_MISMATCH"):
        freeze(tmp_path, forbidden, **arguments)
    assert all((tmp_path / name).read_bytes() == data for name, data in files.items())


@pytest.mark.parametrize("index,value", [
    (1, "0"), (1, "nan"), (2, "100"), (2, "inf"),
    (3, "101"), (4, "-1"), (5, "-0.1"), (5, "nan"),
])
def test_invalid_minute_ohlcv_rejects(tmp_path, index, value):
    rows = kline_rows()
    rows[10][index] = value
    with pytest.raises(ValueError, match="BINANCE_BAR_OHLC_INVALID"):
        freeze(tmp_path, ArchiveFeed(bars=rows))


def test_invalid_archive_rolls_back_partial_minute_ingestion(tmp_path):
    rows = kline_rows()
    rows[10][2] = "nan"
    with pytest.raises(ValueError, match="BINANCE_BAR_OHLC_INVALID"):
        freeze(tmp_path, ArchiveFeed(bars=rows))
    with sqlite3.connect(tmp_path / "research.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM research_meta WHERE key LIKE 'completed_archive:%-1m-%'").fetchone()[0] == 0


@pytest.mark.parametrize("field", ["open_time", "close_time"])
def test_minute_timestamp_and_close_boundary_are_validated(tmp_path, field):
    rows = kline_rows()
    rows[10][0 if field == "open_time" else 6] += 1
    with pytest.raises(ValueError, match="BINANCE_BAR_TIMESTAMP_INVALID"):
        freeze(tmp_path, ArchiveFeed(bars=rows))


@pytest.mark.parametrize("conflict", [False, True])
def test_duplicate_minute_timestamp_rejects_instead_of_silent_dedupe(tmp_path, conflict):
    rows = kline_rows()
    duplicate = list(rows[10])
    if conflict:
        duplicate[4] = "100.5"
    rows.append(duplicate)
    with pytest.raises(ValueError, match="BINANCE_DUPLICATE_TIMESTAMP"):
        freeze(tmp_path, ArchiveFeed(bars=rows))


@pytest.mark.parametrize("missing_index", [0, 10, -1])
def test_missing_minute_marks_incomplete_even_at_window_boundaries(tmp_path, missing_index):
    rows = kline_rows()
    del rows[missing_index]
    result = freeze_incomplete(tmp_path, ArchiveFeed(bars=rows))
    coverage = result["coverage"][SYMBOL]["bars"]
    assert result["complete_data"] is False
    assert coverage["expected"] == 480
    assert coverage["actual"] == 479
    assert coverage["missing_minutes"] == 1


def test_outside_window_minutes_are_filtered_without_changing_coverage(tmp_path):
    rows = kline_rows(START - timedelta(minutes=1), END + timedelta(minutes=1))
    result = freeze(tmp_path, ArchiveFeed(bars=rows))
    assert result["complete_data"] is True
    coverage = result["coverage"][SYMBOL]["bars"]
    assert coverage["actual"] == 480
    assert coverage["first_open_time_ms"] == milliseconds(START)
    assert coverage["last_open_time_ms"] == milliseconds(END) - MINUTE_MS


def test_warmup_minutes_are_complete_across_month_boundary(tmp_path):
    warmup_start = START - timedelta(days=1)
    feed = ArchiveFeed(bars=kline_rows(warmup_start, END), partition_months=True)
    result = freeze(tmp_path, feed, warmup_days=1)
    coverage = result["coverage"][SYMBOL]
    assert result["complete_data"] is True
    assert result["warmup_start"] == warmup_start.isoformat()
    assert coverage["bars"]["expected"] == coverage["bars"]["actual"] == 1920
    assert coverage["bars"]["warmup_minutes"] == 1440
    assert coverage["bars"]["evaluation_minutes"] == 480
    assert coverage["bars"]["first_open_time_ms"] == milliseconds(warmup_start)
    assert coverage["funding"]["expected"] == coverage["funding"]["actual"] == 1


def test_invalid_outside_window_ohlc_is_not_silently_discarded(tmp_path):
    rows = kline_rows(START - timedelta(minutes=1), END)
    rows[0][2] = "nan"
    with pytest.raises(ValueError, match="BINANCE_BAR_OHLC_INVALID"):
        freeze(tmp_path, ArchiveFeed(bars=rows))


def test_funding_slot_coverage_preserves_original_millisecond_offsets(tmp_path):
    end = START + timedelta(hours=16)
    rows = funding_rows(START, end, offsets=[17, 37])
    result = freeze(tmp_path, ArchiveFeed(start=START, end=end, funding=rows), end=end)
    coverage = result["coverage"][SYMBOL]["funding"]
    assert result["complete_data"] is True
    assert coverage["expected"] == coverage["actual"] == 2
    assert coverage["missing_slots"] == []
    assert coverage["duplicate_slots"] == 0
    assert coverage["first_payment_time_ms"] == milliseconds(START) + 17
    assert coverage["last_payment_time_ms"] == milliseconds(START + timedelta(hours=8)) + 37
    with sqlite3.connect(tmp_path / "research.sqlite3") as connection:
        stored = connection.execute("SELECT payment_time_ms,interval_hours,rate FROM funding ORDER BY payment_time_ms").fetchall()
    assert stored == [(int(row[0]), 8, 0.0001) for row in rows]


@pytest.mark.parametrize("missing_index", [0, -1])
def test_funding_missing_slot_marks_incomplete(tmp_path, missing_index):
    end = START + timedelta(hours=16)
    rows = funding_rows(START, end)
    del rows[missing_index]
    result = freeze_incomplete(tmp_path, ArchiveFeed(start=START, end=end, funding=rows), end=end)
    coverage = result["coverage"][SYMBOL]["funding"]
    assert result["complete_data"] is False
    assert coverage["expected"] == 2
    assert coverage["actual"] == 1
    assert len(coverage["missing_slots"]) == 1


def test_funding_duplicate_slot_rejects_even_with_distinct_raw_milliseconds(tmp_path):
    rows = funding_rows()
    rows.append([milliseconds(START) + 37, "8", "0.0002"])
    result = freeze_incomplete(tmp_path, ArchiveFeed(funding=rows))
    coverage = result["coverage"][SYMBOL]["funding"]
    assert coverage["duplicate_slots"] == 1
    assert coverage["missing_slots"] == []


@pytest.mark.parametrize("index,value", [(1, "4"), (2, "nan"), (2, "inf")])
def test_invalid_funding_interval_or_rate_rejects(tmp_path, index, value):
    rows = funding_rows()
    rows[0][index] = value
    with pytest.raises(ValueError, match="BINANCE_FUNDING_INTERVAL_OR_RATE_INVALID"):
        freeze(tmp_path, ArchiveFeed(funding=rows))


def test_funding_outside_window_does_not_replace_expected_slot(tmp_path):
    rows = funding_rows(START - timedelta(hours=8), END + timedelta(hours=8))
    result = freeze(tmp_path, ArchiveFeed(funding=rows))
    coverage = result["coverage"][SYMBOL]["funding"]
    assert result["complete_data"] is True
    assert coverage["actual"] == 1
    assert coverage["first_payment_time_ms"] == milliseconds(START) + 17


def test_both_symbols_have_independent_coverage_with_parallel_downloads(tmp_path):
    result = freeze(tmp_path, ArchiveFeed(), symbols=("BTCUSDT", "ETHUSDT"), workers=2)
    assert result["complete_data"] is True
    assert result["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert set(result["coverage"]) == {"BTCUSDT", "ETHUSDT"}
    assert all(item["bars"]["actual"] == 480 and item["funding"]["actual"] == 1 for item in result["coverage"].values())


def test_archives_without_csv_headers_are_supported(tmp_path):
    result = freeze(tmp_path, ArchiveFeed(headers=False))
    assert result["complete_data"] is True


@pytest.mark.parametrize("arguments", [
    {"start": START.replace(tzinfo=None)},
    {"start": START + timedelta(minutes=1)},
    {"start": START + timedelta(microseconds=1)},
    {"end": END + timedelta(microseconds=1)},
    {"end": START},
    {"warmup_days": -1},
])
def test_invalid_window_is_rejected_before_fetch_or_database_creation(tmp_path, arguments):
    def forbidden(url):
        raise AssertionError("Invalid window must not fetch archive bytes")

    with pytest.raises(ValueError):
        freeze(tmp_path, forbidden, **arguments)
    assert not (tmp_path / "research.sqlite3").exists()
