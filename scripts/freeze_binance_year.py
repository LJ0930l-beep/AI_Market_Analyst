"""Freeze public Binance USD-M history in a dedicated research DB."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.replay.binance_history import DEFAULT_END, DEFAULT_START, freeze_binance_history


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("reports/btc-eth-year-proxy-20261004"))
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 5))
    parser.add_argument("--resume", action="store_true", help="Same-window research databases and verified archives resume automatically")
    arguments = parser.parse_args()
    manifest = freeze_binance_history(arguments.output, start=DEFAULT_START, end=DEFAULT_END, workers=arguments.workers,
                                     progress=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True))
    print(json.dumps({"manifest": str(arguments.output.resolve() / "manifest.json"),
                      "database": str(arguments.output.resolve() / "research.sqlite3"),
                      "dataset_sha256": manifest["dataset_sha256"], "complete_data": manifest["complete_data"]}), flush=True)


if __name__ == "__main__":
    main()
