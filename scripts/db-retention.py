#!/usr/bin/env python
"""Offline retention pass for the AI Market Analyst database.

Run with the app closed when you pass ``--vacuum`` (VACUUM needs the database to
itself).

    python scripts/db-retention.py --dry-run     # report what would be removed
    python scripts/db-retention.py               # apply the retention policy
    python scripts/db-retention.py --vacuum      # apply, then rebuild the file

Without ``--db`` the database is located through ``core.config.app_data_paths()``,
i.e. ``$AIMA_DATA_ROOT/data/market_analyst.sqlite3`` when that variable is set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import app_data_paths  # noqa: E402
from core.storage.retention import (  # noqa: E402
    DEFAULT_KEEP_BOOTSTRAP_RUNS,
    DEFAULT_MAX_DELETES_PER_PASS,
    RETENTION_STATE_FILENAME,
    apply_retention,
)


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0 or unit == "TiB":
            return f"{value:,.1f} {unit}"
        value /= 1024.0
    return f"{value:,.1f} TiB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", help="explicit database path (defaults to the configured data root)")
    parser.add_argument("--dry-run", action="store_true", help="report without deleting anything")
    parser.add_argument("--vacuum", action="store_true", help="rebuild the file after pruning (app must be closed)")
    parser.add_argument(
        "--keep-bootstrap-runs",
        type=int,
        default=DEFAULT_KEEP_BOOTSTRAP_RUNS,
        help=f"bootstrap runs to keep per (provider, environment, symbol), default {DEFAULT_KEEP_BOOTSTRAP_RUNS}",
    )
    parser.add_argument(
        "--max-deletes-per-pass",
        type=int,
        default=DEFAULT_MAX_DELETES_PER_PASS,
        help=f"row budget per delete batch, default {DEFAULT_MAX_DELETES_PER_PASS}",
    )
    args = parser.parse_args(argv)

    if args.db:
        db_path = Path(args.db).resolve()
        state_path = db_path.parent / RETENTION_STATE_FILENAME
    else:
        paths = app_data_paths()
        db_path = paths.data / "market_analyst.sqlite3"
        state_path = paths.runtime / RETENTION_STATE_FILENAME
        if not os.environ.get("AIMA_DATA_ROOT"):
            # app_data_paths() 在环境变量缺失时会回落到 %LOCALAPPDATA%，于是这个
            # 工具会安静地去操作另一个（很可能是空的）库。实测过：在没有继承该
            # 变量的 shell 里，它指向 C 盘、然后只回一句 "database not found"。
            print(
                "warning: AIMA_DATA_ROOT is not visible to this process, so the data root "
                "fell back to the default under %LOCALAPPDATA%.",
                file=sys.stderr,
            )
            print(
                f"         resolved data root : {db_path.parent.parent}",
                file=sys.stderr,
            )
            print(
                "         pass --db <path> or set AIMA_DATA_ROOT to target the real database.",
                file=sys.stderr,
            )

    print(f"database   : {db_path}")
    # 这个离线入口直接执行 apply_retention，不走间隔限流，也**不写**记账文件：
    # 否则一次手工清理会把守护线程的下一次执行整个推迟掉。这里打印的路径只是
    # 告诉你"哪份状态文件在管这个库的节奏"。
    print(f"cadence governed by (not written here) : {state_path}")
    if not db_path.is_file():
        print("database not found; nothing to do.")
        return 1

    outcome = apply_retention(
        db_path,
        keep_bootstrap_runs=args.keep_bootstrap_runs,
        max_deletes_per_pass=args.max_deletes_per_pass,
        reclaim=args.vacuum,
        dry_run=args.dry_run,
    )
    print(json.dumps(outcome, indent=2, ensure_ascii=False))
    print()
    print(f"market_bar_versions : {outcome['market_bar_versions_before']:,} -> {outcome['market_bar_versions_after']:,}")
    print(f"gate_bootstrap_runs : {outcome['bootstrap_runs_before']:,} -> {outcome['bootstrap_runs_after']:,}")
    print(f"file size           : {_human(outcome['bytes_before'])} -> {_human(outcome['bytes_after'])}")
    if args.dry_run:
        print("\n(dry run: nothing was deleted)")
    elif not args.vacuum:
        print("\nspace is released to the freelist; pass --vacuum with the app closed to shrink the file")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
