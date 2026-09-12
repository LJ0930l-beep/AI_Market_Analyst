"""Bounded-growth retention for the local market data store.

Why this module exists (measured 2026-09-11)
--------------------------------------------
``market_bar_versions`` is documented as a bitemporal bar store whose identity is
``(instrument_key, timeframe, bar_start, revision_id)`` -- a new *revision* is
supposed to mean "the market content of this bar changed".  The collector
instead persists one revision per *fetch*: ``raw_hash`` is computed over a
payload that embeds ``fetched_at`` / ``received_at`` / ``first_received_at``,
and ``revision_id = raw_hash[:32]``, so re-observing an unchanged bar mints a
brand new revision every single time.

Measured on the 2.50 GB production database:

* 1 335 784 rows covering only 7 524 distinct bars (avg 177 revisions per bar);
* 1 300 841 of those rows are *closed* bars, and content-deduplicating them
  leaves 7 531 -- one 15m bar carried 2 541 byte-identical copies;
* ``gate_bootstrap_runs``: 255 rows all describing the same
  ``(provider, environment, symbol)``, at ~1.9 MB of ``payload_json`` each.

Nothing pruned any of it, so the file grew without bound.

Policy
------
``market_bar_versions`` -- keep one row per distinct observation, where an
observation is ``(instrument_key, timeframe, bar_start, is_closed, open, high,
low, close, volume)``.  The surviving row is the *earliest* one (``min
available_at``, ``rowid`` as tie-break): the moment that value first became
knowable to the system.  A later copy of the same value carries no information
the first one does not -- only ``fetched_at`` differs.  This is deliberately
narrow: it deletes exact duplicates only, so the intra-bar tick path of a
still-forming bar survives as long as it ever produced a distinct OHLCV.

``gate_bootstrap_runs`` -- keep the newest ``keep_bootstrap_runs`` runs per
``(provider, environment, symbol)``.  The table's own scope index is
``(environment, symbol, completed_at DESC)``, i.e. only the latest run is ever
read.

Both passes are bounded per call (:data:`DEFAULT_MAX_DELETES_PER_PASS`) so a
startup pass can never hold the SQLite write lock for an unbounded time.  The
pass runs on a :data:`DEFAULT_MIN_INTERVAL_MINUTES` cadence -- see the constant
for why hour-scale pruning is not enough for ``gate_bootstrap_runs``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_KEEP_BOOTSTRAP_RUNS = 3
DEFAULT_MAX_DELETES_PER_PASS = 200_000

# The cadence is expressed in minutes, not hours.  Measured on 2026-09-11
# 06:58-07:01 UTC while the client was running: ``gate_bootstrap_runs`` grows by
# ~2 rows per minute (~120 rows/hour, ~1.9 MB of ``payload_json`` each -- about
# 228 MiB/hour), because ``save_gate_bootstrap`` derives its id from a payload
# that embeds the live quote and the still-forming bar, so it is never
# idempotent in practice.  An hour-scale interval would let that one table reach
# roughly 1.4 GB between passes on an otherwise 57 MiB database.  30 minutes
# caps the transient peak at ~114 MiB while still being rare enough to be free.
DEFAULT_MIN_INTERVAL_MINUTES = 30
DEFAULT_CHECK_INTERVAL_SECONDS = 900.0
RETENTION_STATE_FILENAME = "retention-state.json"

# A bar "observation" is identified by these columns.  Two rows sharing all of
# them describe the same market fact and differ only in fetch bookkeeping.
_OBSERVATION_COLUMNS = (
    "instrument_key",
    "timeframe",
    "bar_start",
    "is_closed",
    "open",
    "high",
    "low",
    "close",
    "volume",
)

_MARKET_BAR_DUPLICATE_DELETE = f"""
DELETE FROM market_bar_versions
WHERE rowid IN (
    SELECT rowid FROM (
        SELECT rowid,
               ROW_NUMBER() OVER (
                   PARTITION BY {", ".join(_OBSERVATION_COLUMNS)}
                   ORDER BY available_at ASC, rowid ASC
               ) AS observation_seq
        FROM market_bar_versions
    )
    WHERE observation_seq > 1
    LIMIT ?
)
"""

_BOOTSTRAP_RUN_DELETE = """
DELETE FROM gate_bootstrap_runs
WHERE bootstrap_id IN (
    SELECT bootstrap_id FROM (
        SELECT bootstrap_id,
               ROW_NUMBER() OVER (
                   PARTITION BY provider, environment, symbol
                   ORDER BY completed_at DESC, started_at DESC, bootstrap_id DESC
               ) AS run_seq
        FROM gate_bootstrap_runs
    )
    WHERE run_seq > ?
)
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _connect(db_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path), timeout=30.0)
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _row_count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def prune_market_bar_duplicates(
    connection: sqlite3.Connection,
    *,
    max_deletes_per_pass: int = DEFAULT_MAX_DELETES_PER_PASS,
    max_passes: int = 50,
) -> int:
    """Delete superseded duplicate observations. Returns the number removed."""

    if not _table_exists(connection, "market_bar_versions"):
        return 0
    limit = max(1, int(max_deletes_per_pass))
    removed = 0
    for _ in range(max(1, int(max_passes))):
        cursor = connection.execute(_MARKET_BAR_DUPLICATE_DELETE, (limit,))
        connection.commit()
        batch = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        removed += batch
        if batch < limit:
            break
    return removed


def prune_bootstrap_runs(
    connection: sqlite3.Connection,
    *,
    keep_bootstrap_runs: int = DEFAULT_KEEP_BOOTSTRAP_RUNS,
) -> int:
    """Delete superseded bootstrap runs, keeping the newest per scope."""

    if not _table_exists(connection, "gate_bootstrap_runs"):
        return 0
    keep = max(1, int(keep_bootstrap_runs))
    cursor = connection.execute(_BOOTSTRAP_RUN_DELETE, (keep,))
    connection.commit()
    return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


def apply_retention(
    db_path: str | Path,
    *,
    keep_bootstrap_runs: int = DEFAULT_KEEP_BOOTSTRAP_RUNS,
    max_deletes_per_pass: int = DEFAULT_MAX_DELETES_PER_PASS,
    reclaim: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply the retention policy once.

    ``reclaim`` additionally runs ``VACUUM`` -- that needs the database to
    itself, so it is only requested by the offline command line entry point.
    """

    path = Path(db_path)
    result: dict[str, Any] = {
        "database": str(path),
        "exists": path.is_file(),
        "dry_run": bool(dry_run),
        "market_bar_versions_before": 0,
        "market_bar_versions_deleted": 0,
        "market_bar_versions_after": 0,
        "bootstrap_runs_before": 0,
        "bootstrap_runs_deleted": 0,
        "bootstrap_runs_after": 0,
        "bytes_before": 0,
        "bytes_after": 0,
        "reclaimed": False,
    }
    if not path.is_file():
        return result

    result["bytes_before"] = path.stat().st_size

    if dry_run:
        connection = _connect(path)
        try:
            result["market_bar_versions_before"] = _row_count(connection, "market_bar_versions")
            duplicate_rows = connection.execute(
                f"""
                SELECT COUNT(*) FROM (
                    SELECT ROW_NUMBER() OVER (
                               PARTITION BY {", ".join(_OBSERVATION_COLUMNS)}
                               ORDER BY available_at ASC, rowid ASC
                           ) AS observation_seq
                    FROM market_bar_versions
                )
                WHERE observation_seq > 1
                """
            ).fetchone()[0]
            result["market_bar_versions_deleted"] = int(duplicate_rows)
            if _table_exists(connection, "gate_bootstrap_runs"):
                result["bootstrap_runs_before"] = _row_count(connection, "gate_bootstrap_runs")
                superseded = connection.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT ROW_NUMBER() OVER (
                                   PARTITION BY provider, environment, symbol
                                   ORDER BY completed_at DESC, started_at DESC, bootstrap_id DESC
                               ) AS run_seq
                        FROM gate_bootstrap_runs
                    )
                    WHERE run_seq > ?
                    """,
                    (max(1, int(keep_bootstrap_runs)),),
                ).fetchone()[0]
                result["bootstrap_runs_deleted"] = int(superseded)
        finally:
            connection.close()
        result["market_bar_versions_after"] = (
            result["market_bar_versions_before"] - result["market_bar_versions_deleted"]
        )
        result["bootstrap_runs_after"] = (
            result["bootstrap_runs_before"] - result["bootstrap_runs_deleted"]
        )
        result["bytes_after"] = result["bytes_before"]
        return result

    connection = _connect(path)
    try:
        result["market_bar_versions_before"] = _row_count(connection, "market_bar_versions")
        result["bootstrap_runs_before"] = (
            _row_count(connection, "gate_bootstrap_runs")
            if _table_exists(connection, "gate_bootstrap_runs")
            else 0
        )
        result["market_bar_versions_deleted"] = prune_market_bar_duplicates(
            connection, max_deletes_per_pass=max_deletes_per_pass
        )
        result["bootstrap_runs_deleted"] = prune_bootstrap_runs(
            connection, keep_bootstrap_runs=keep_bootstrap_runs
        )
        result["market_bar_versions_after"] = _row_count(connection, "market_bar_versions")
        result["bootstrap_runs_after"] = (
            _row_count(connection, "gate_bootstrap_runs")
            if _table_exists(connection, "gate_bootstrap_runs")
            else 0
        )
        auto_vacuum = int(connection.execute("PRAGMA auto_vacuum").fetchone()[0])
        if reclaim:
            if auto_vacuum == 0:
                # Persist the mode so future deletions release pages instead of
                # only moving them to the freelist, then rebuild the file once.
                connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
            connection.execute("VACUUM")
            result["reclaimed"] = True
        elif auto_vacuum == 2:
            connection.execute("PRAGMA incremental_vacuum")
    finally:
        connection.close()

    result["bytes_after"] = path.stat().st_size
    return result


def read_state(state_path: str | Path) -> dict[str, Any]:
    path = Path(state_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_state(state_path: str | Path, payload: dict[str, Any]) -> None:
    path = Path(state_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        # Bookkeeping must never break startup; the next pass simply runs again.
        pass


def run_retention_if_due(
    db_path: str | Path,
    state_path: str | Path,
    *,
    min_interval_minutes: int = DEFAULT_MIN_INTERVAL_MINUTES,
    keep_bootstrap_runs: int = DEFAULT_KEEP_BOOTSTRAP_RUNS,
    max_deletes_per_pass: int = DEFAULT_MAX_DELETES_PER_PASS,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Run retention unless it already ran within ``min_interval_minutes``.

    Returns ``None`` when the pass was skipped as not-due.  Never raises: a
    failed housekeeping pass must not stop the sidecar from serving.

    A *failed* pass deliberately does not stamp ``last_run_utc``.  Recording the
    attempt as if it were a completed run would turn one transient lock conflict
    into a full interval of accumulated growth; the error is recorded under
    ``last_error_utc`` instead, so the next wake-up retries.
    """

    current = now or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    state = read_state(state_path)
    last_text = state.get("last_run_utc")
    if isinstance(last_text, str):
        try:
            last_run = datetime.fromisoformat(last_text)
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=timezone.utc)
            if current - last_run < timedelta(minutes=max(1, int(min_interval_minutes))):
                return None
        except ValueError:
            pass

    try:
        outcome = apply_retention(
            db_path,
            keep_bootstrap_runs=keep_bootstrap_runs,
            max_deletes_per_pass=max_deletes_per_pass,
        )
    except Exception as exc:  # pragma: no cover - defensive housekeeping path
        write_state(state_path, {**state, "last_error_utc": current.isoformat(),
                                 "last_error": str(exc)[:240]})
        return None

    write_state(
        state_path,
        {"last_run_utc": current.isoformat(), "last_result": outcome},
    )
    return outcome


def start_background_retention(
    db_path: str | Path,
    state_path: str | Path,
    *,
    startup_delay_seconds: float = 15.0,
    check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
    min_interval_minutes: int = DEFAULT_MIN_INTERVAL_MINUTES,
    keep_bootstrap_runs: int = DEFAULT_KEEP_BOOTSTRAP_RUNS,
    max_deletes_per_pass: int = DEFAULT_MAX_DELETES_PER_PASS,
) -> threading.Thread:
    """Run retention for the lifetime of the process on a daemon thread.

    A startup-only pass is not enough: the sidecar is expected to stay up for
    weeks, during which the collector keeps writing.  The thread wakes every
    ``check_interval_seconds`` and lets :func:`run_retention_if_due` decide
    whether the interval has actually elapsed, so the cadence lives in one place
    (the state file).  The check interval must not exceed the retention
    interval, otherwise the effective cadence becomes the check interval.
    """

    def worker() -> None:
        time.sleep(max(0.0, float(startup_delay_seconds)))
        while True:
            try:
                run_retention_if_due(
                    db_path,
                    state_path,
                    min_interval_minutes=min_interval_minutes,
                    keep_bootstrap_runs=keep_bootstrap_runs,
                    max_deletes_per_pass=max_deletes_per_pass,
                )
            except Exception:
                # Housekeeping must never take the sidecar down.
                pass
            time.sleep(max(60.0, float(check_interval_seconds)))

    thread = threading.Thread(target=worker, name="aima-retention", daemon=True)
    thread.start()
    return thread
