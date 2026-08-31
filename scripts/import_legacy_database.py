"""Explicitly import a V1.1 SQLite database into the V1.2 AppData store."""

from __future__ import annotations

import argparse

from core.desktop_runtime import import_legacy_database


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a legacy AI Market Analyst SQLite database")
    parser.add_argument("--source", required=True, help="path to the legacy SQLite database")
    parser.add_argument("--destination", help="optional V1.2 destination database path")
    args = parser.parse_args()
    report = import_legacy_database(source=args.source, destination=args.destination)
    print(report.to_dict())
    return 0 if report.imported or report.reason == "destination_exists" else 2


if __name__ == "__main__":
    raise SystemExit(main())
