"""Command-line wrapper for the safe Phase 7 SQLite backup/restore artifact."""

from __future__ import annotations

import argparse
import json
import sys

from core.backup import BackupError, backup_database, restore_database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create or restore a validated local SQLite backup artifact.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup", help="create a new backup directory")
    backup.add_argument("--database", required=True, help="source SQLite database path")
    backup.add_argument("--output", required=True, help="new empty backup directory")

    restore = subparsers.add_parser("restore", help="restore a validated backup directory")
    restore.add_argument("--input", required=True, help="backup artifact directory")
    restore.add_argument("--database", required=True, help="target SQLite database path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "backup":
            result = backup_database(args.database, args.output)
        else:
            result = restore_database(args.input, args.database)
    except BackupError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
