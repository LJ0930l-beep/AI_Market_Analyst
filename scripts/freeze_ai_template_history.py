"""Fetch public Gate history into an isolated, reproducible research manifest."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.replay.ai_history import BENCHMARK_SYMBOLS, freeze_public_history, utc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--news-database", type=Path)
    parser.add_argument("--symbols", nargs="+", default=list(BENCHMARK_SYMBOLS))
    args = parser.parse_args()
    payload = freeze_public_history(args.output, utc(args.start), utc(args.end), symbols=args.symbols,
                                    database=args.news_database, progress=lambda message: print(message, flush=True))
    print("manifest=" + payload["manifest_sha256"], "complete_data=" + str(payload["complete_data"]), flush=True)
    if not payload["complete_data"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
