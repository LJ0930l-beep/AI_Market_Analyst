"""Run resumable Phase 3 Historical Replay / Walk-Forward samples."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.ai import OllamaProvider
from core.ai.mock import MockLLMProvider
from core.replay.runner import ReplayConfig, run_replay


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 3 real Qwen replay runner")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--timeframes", nargs="+", required=True)
    parser.add_argument("--samples", type=int, default=300)
    parser.add_argument("--model", default=os.environ.get("OLLAMA_MODEL", "qwen3.5:4b"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--db", default="data/phase3-replay.sqlite3")
    parser.add_argument("--output")
    parser.add_argument("--manifest")
    parser.add_argument("--mode", choices=("real", "mock"), default="real")
    parser.add_argument("--allow-fixture", action="store_true", help="allow provider fallback; never use for formal real acceptance")
    parser.add_argument("--require-model", action="store_true")
    args = parser.parse_args()

    os.environ["MARKET_DATA_MODE"] = "real" if args.mode == "real" else "fixture"
    if args.mode == "real" and not args.allow_fixture:
        os.environ["DISABLE_FIXTURE_FALLBACK"] = "1"
    if args.mode == "real":
        provider = OllamaProvider(model_name=args.model)
        if args.require_model:
            health = provider.health()
            if not health.get("available") or not health.get("model_available"):
                print(json.dumps({"phase": 3, "status": "FAIL", "error": "required_model_unavailable", "health": health}, ensure_ascii=False))
                return 2
    else:
        provider = MockLLMProvider(model_id=args.model)

    def progress(item: dict[str, object]) -> None:
        print(json.dumps(item, ensure_ascii=False), flush=True)

    config = ReplayConfig(
        symbols=tuple(args.symbols),
        timeframes=tuple(args.timeframes),
        samples=max(1, args.samples),
        model_id=args.model,
        seed=args.seed,
        db_path=args.db,
        output_path=args.output,
        manifest_path=args.manifest,
        resume=args.resume,
    )
    try:
        result = run_replay(config, llm_provider=provider, progress=progress)
    except Exception as exc:
        print(json.dumps({"phase": 3, "status": "FAIL", "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "COMPLETED" else 2


if __name__ == "__main__":
    sys.exit(main())
