"""Retention policy tests for the oversized market data tables.

The policy exists because the collector stores one bar revision per *fetch*
rather than per content change, which grew the production database to 2.5 GB
(1 335 784 rows for 7 524 bars).  These tests pin the rule that matters: only
exact duplicate observations are removed, and the earliest one survives.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

from core.storage.retention import (
    DEFAULT_CHECK_INTERVAL_SECONDS,
    DEFAULT_MIN_INTERVAL_MINUTES,
    RETENTION_STATE_FILENAME,
    apply_retention,
    prune_bootstrap_runs,
    prune_market_bar_duplicates,
    read_state,
    run_retention_if_due,
    start_background_retention,
    write_state,
)

BAR_DDL = """
CREATE TABLE market_bar_versions (
    instrument_key TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    bar_start TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    available_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    is_closed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(instrument_key, timeframe, bar_start, revision_id)
)
"""

BOOTSTRAP_DDL = """
CREATE TABLE gate_bootstrap_runs (
    bootstrap_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    environment TEXT NOT NULL,
    symbol TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    source_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
)
"""


def _database(path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path))
    connection.execute(BAR_DDL)
    connection.execute(BOOTSTRAP_DDL)
    return connection


def _insert_bar(connection, *, start, available_at, revision_id, close, is_closed=1, key="gate:perpetual:BTC_USDT:USDT:last", timeframe="15m"):
    connection.execute(
        """INSERT INTO market_bar_versions(
               instrument_key, timeframe, bar_start, revision_id,
               open, high, low, close, volume, available_at, fetched_at, is_closed
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (key, timeframe, start, revision_id, 100.0, 110.0, 90.0, close, 5.0, available_at, available_at, is_closed),
    )


def test_duplicate_observations_collapse_to_the_earliest(tmp_path):
    path = tmp_path / "market.sqlite3"
    connection = _database(path)
    bar = "2026-09-09T15:45:00+00:00"
    # Three fetches of a closed bar that carry byte-identical OHLCV.
    for index, stamp in enumerate(
        ("2026-09-09T16:00:00+00:00", "2026-09-09T16:10:00+00:00", "2026-09-09T16:20:00+00:00")
    ):
        _insert_bar(connection, start=bar, available_at=stamp, revision_id=f"rev{index}", close=102.5)
    connection.commit()

    removed = prune_market_bar_duplicates(connection)

    assert removed == 2
    rows = connection.execute(
        "SELECT revision_id, available_at FROM market_bar_versions"
    ).fetchall()
    assert rows == [("rev0", "2026-09-09T16:00:00+00:00")]
    connection.close()


def test_distinct_contents_and_revisions_survive(tmp_path):
    path = tmp_path / "market.sqlite3"
    connection = _database(path)
    bar = "2026-09-09T15:45:00+00:00"
    # A corrected close is a genuinely different observation and must be kept.
    _insert_bar(connection, start=bar, available_at="2026-09-09T16:00:00+00:00", revision_id="rev0", close=102.5)
    _insert_bar(connection, start=bar, available_at="2026-09-09T16:05:00+00:00", revision_id="rev1", close=102.5)
    _insert_bar(connection, start=bar, available_at="2026-09-09T16:10:00+00:00", revision_id="rev2", close=103.0)
    # A different bar is a different partition and is never touched.
    _insert_bar(connection, start="2026-09-09T16:00:00+00:00", available_at="2026-09-09T16:15:00+00:00", revision_id="rev3", close=102.5)
    connection.commit()

    removed = prune_market_bar_duplicates(connection)

    assert removed == 1
    kept = connection.execute(
        "SELECT bar_start, revision_id, close FROM market_bar_versions ORDER BY bar_start, revision_id"
    ).fetchall()
    assert kept == [
        ("2026-09-09T15:45:00+00:00", "rev0", 102.5),
        ("2026-09-09T15:45:00+00:00", "rev2", 103.0),
        ("2026-09-09T16:00:00+00:00", "rev3", 102.5),
    ]
    connection.close()


def test_formative_tick_path_is_preserved(tmp_path):
    path = tmp_path / "market.sqlite3"
    connection = _database(path)
    bar = "2026-09-09T15:45:00+00:00"
    # An in-progress bar ticks through distinct closes; each one is real
    # information and must survive, only repeats collapse.
    for index, close in enumerate((100.0, 101.0, 100.5, 101.0)):
        _insert_bar(
            connection,
            start=bar,
            available_at=f"2026-09-09T16:0{index}:00+00:00",
            revision_id=f"rev{index}",
            close=close,
            is_closed=0,
        )
    connection.commit()

    removed = prune_market_bar_duplicates(connection)

    assert removed == 1
    assert connection.execute("SELECT COUNT(*) FROM market_bar_versions").fetchone()[0] == 3
    connection.close()


def test_bootstrap_runs_keep_the_newest_per_scope(tmp_path):
    path = tmp_path / "market.sqlite3"
    connection = _database(path)
    for index in range(6):
        connection.execute(
            """INSERT INTO gate_bootstrap_runs(
                   bootstrap_id, provider, environment, symbol, started_at, completed_at, source_hash, payload_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"boot{index}",
                "gate",
                "LIVE_PUBLIC",
                "BTCUSDT",
                f"2026-09-10T10:0{index}:00+00:00",
                f"2026-09-10T10:0{index}:30+00:00",
                f"hash{index}",
                "{}",
            ),
        )
    # A second scope must keep its own newest runs.
    connection.execute(
        """INSERT INTO gate_bootstrap_runs(
               bootstrap_id, provider, environment, symbol, started_at, completed_at, source_hash, payload_json
           ) VALUES ('other0', 'gate', 'LIVE_PUBLIC', 'ETHUSDT', '2026-09-10T11:00:00+00:00',
                     '2026-09-10T11:00:30+00:00', 'hashother', '{}')"""
    )
    connection.commit()

    removed = prune_bootstrap_runs(connection, keep_bootstrap_runs=2)

    assert removed == 4
    kept = connection.execute("SELECT bootstrap_id FROM gate_bootstrap_runs ORDER BY bootstrap_id").fetchall()
    assert kept == [("boot4",), ("boot5",), ("other0",)]
    connection.close()


def test_apply_retention_reports_counts(tmp_path):
    path = tmp_path / "market.sqlite3"
    connection = _database(path)
    for index in range(4):
        _insert_bar(
            connection,
            start="2026-09-09T15:45:00+00:00",
            available_at=f"2026-09-09T16:0{index}:00+00:00",
            revision_id=f"rev{index}",
            close=102.5,
        )
    connection.commit()
    connection.close()

    outcome = apply_retention(path)

    assert outcome["market_bar_versions_before"] == 4
    assert outcome["market_bar_versions_deleted"] == 3
    assert outcome["market_bar_versions_after"] == 1
    assert outcome["reclaimed"] is False


def test_dry_run_never_deletes(tmp_path):
    path = tmp_path / "market.sqlite3"
    connection = _database(path)
    for index in range(3):
        _insert_bar(
            connection,
            start="2026-09-09T15:45:00+00:00",
            available_at=f"2026-09-09T16:0{index}:00+00:00",
            revision_id=f"rev{index}",
            close=102.5,
        )
    connection.commit()
    connection.close()

    outcome = apply_retention(path, dry_run=True)

    assert outcome["market_bar_versions_deleted"] == 2
    check = sqlite3.connect(str(path))
    assert check.execute("SELECT COUNT(*) FROM market_bar_versions").fetchone()[0] == 3
    check.close()


def test_run_if_due_respects_the_interval(tmp_path):
    path = tmp_path / "market.sqlite3"
    connection = _database(path)
    for index in range(2):
        _insert_bar(
            connection,
            start="2026-09-09T15:45:00+00:00",
            available_at=f"2026-09-09T16:0{index}:00+00:00",
            revision_id=f"rev{index}",
            close=102.5,
        )
    connection.commit()
    connection.close()
    state_path = tmp_path / RETENTION_STATE_FILENAME
    now = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)

    first = run_retention_if_due(path, state_path, min_interval_minutes=180, now=now)
    assert first is not None and first["market_bar_versions_deleted"] == 1

    second = run_retention_if_due(
        path, state_path, min_interval_minutes=180, now=now + timedelta(minutes=30)
    )
    assert second is None

    third = run_retention_if_due(
        path, state_path, min_interval_minutes=180, now=now + timedelta(minutes=181)
    )
    assert third is not None
    assert read_state(state_path)["last_run_utc"] == (now + timedelta(minutes=181)).isoformat()


def test_default_cadence_stays_minute_scale():
    """gate_bootstrap_runs grows ~120 rows/hour (~228 MiB/hour).

    An hour-scale cadence let that one table reach ~1.4 GB between passes, so
    the defaults are pinned here: the interval must stay well under an hour and
    the wake-up must never be the slower of the two.
    """

    assert 0 < DEFAULT_MIN_INTERVAL_MINUTES <= 30
    assert 0 < DEFAULT_CHECK_INTERVAL_SECONDS <= DEFAULT_MIN_INTERVAL_MINUTES * 60


def test_failed_pass_does_not_block_the_next_attempt(tmp_path, monkeypatch):
    """A transient failure must not cost a whole interval of accumulated growth."""

    import core.storage.retention as retention

    path = tmp_path / "market.sqlite3"
    connection = _database(path)
    _insert_bar(
        connection,
        start="2026-09-09T15:45:00+00:00",
        available_at="2026-09-09T16:00:00+00:00",
        revision_id="rev0",
        close=102.5,
    )
    connection.commit()
    connection.close()
    state_path = tmp_path / "state.json"
    now = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)

    real = retention.apply_retention
    calls = {"count": 0}

    def flaky(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    monkeypatch.setattr(retention, "apply_retention", flaky)

    assert run_retention_if_due(path, state_path, min_interval_minutes=180, now=now) is None
    state = read_state(state_path)
    assert "last_run_utc" not in state
    assert state["last_error_utc"] == now.isoformat()

    # One second later -- far inside the 180 minute window -- it must retry.
    retried = run_retention_if_due(
        path, state_path, min_interval_minutes=180, now=now + timedelta(seconds=1)
    )
    assert retried is not None
    assert calls["count"] == 2


def test_missing_database_is_a_no_op(tmp_path):
    outcome = apply_retention(tmp_path / "absent.sqlite3")
    assert outcome["exists"] is False
    assert outcome["market_bar_versions_deleted"] == 0


def test_state_writes_are_tolerant_of_bad_json(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text("{not json", encoding="utf-8")
    assert read_state(state_path) == {}
    write_state(state_path, {"last_run_utc": "2026-09-11T00:00:00+00:00"})
    assert read_state(state_path)["last_run_utc"] == "2026-09-11T00:00:00+00:00"


def test_background_retention_runs_and_stays_daemon(tmp_path):
    path = tmp_path / "market.sqlite3"
    connection = _database(path)
    for index in range(3):
        _insert_bar(
            connection,
            start="2026-09-09T15:45:00+00:00",
            available_at=f"2026-09-09T16:0{index}:00+00:00",
            revision_id=f"rev{index}",
            close=102.5,
        )
    connection.commit()
    connection.close()
    state_path = tmp_path / RETENTION_STATE_FILENAME

    thread = start_background_retention(
        path, state_path, startup_delay_seconds=0.05, check_interval_seconds=3600
    )
    assert thread.daemon is True

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if read_state(state_path).get("last_result"):
            break
        time.sleep(0.05)

    outcome = read_state(state_path)["last_result"]
    assert outcome["market_bar_versions_deleted"] == 2
    assert outcome["market_bar_versions_after"] == 1
