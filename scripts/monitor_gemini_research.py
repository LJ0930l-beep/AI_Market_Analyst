"""Read current replay checkpoints without contacting a model or exchange."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.replay.research_observation import observe_directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    report = observe_directory(args.directory)
    report["observed_at"] = datetime.now(timezone.utc).isoformat()
    (args.directory/"observation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
