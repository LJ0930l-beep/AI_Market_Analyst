"""Preregister fixed Gate BTC/ETH segments, then freeze their public history.

This does not pick intervals using proxy returns, call a model or trade.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.replay.ai_history import freeze_public_history, manifest_hash, utc, canonical, digest
from core.replay.ai_template_runner import frozen_source_fingerprint, frozen_templates


WINDOWS = (
    ("calendar-a", "2026-09-28T00:00:00+00:00", "2026-09-28T02:00:00+00:00"),
    ("calendar-b", "2026-09-30T12:00:00+00:00", "2026-09-30T14:00:00+00:00"),
    ("calendar-c", "2026-10-03T00:00:00+00:00", "2026-10-03T02:00:00+00:00"),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    root = args.directory.resolve()
    root.mkdir(parents=True, exist_ok=True)
    plan = {"version": "btc_eth_gate_ai_segments_v1", "research_only": True,
        "decision_source": "ACTUAL_GEMINI_REQUIRED_NOT_TECHNICAL_PROXY",
        "data_venue": "GATE_PUBLIC_PERPETUAL", "symbols": ["BTCUSDT", "ETHUSDT"],
        "selection": "FIXED_CALENDAR_WINDOWS_NOT_SELECTED_USING_PROXY_RETURNS_OR_AI_RESULTS",
        "windows": [{"id": key, "start": start, "end": end} for key, start, end in WINDOWS],
        "templates": frozen_templates(), "source_sha256": frozen_source_fingerprint(),
        "initial_equity": 1000.0, "expected_calls_per_window": 56, "expected_total_calls": 168,
        "account_policy": "INDEPENDENT_FIVE_ACCOUNTS_PER_WINDOW_NO_CARRY_ACROSS_GAPS",
        "boundary_policy": "MARK_OPEN_POSITIONS_TO_MARKET_AT_END_NOT_FORCED_CLOSE",
        "news": "NO_ARCHIVE_PROVIDED_UNAVAILABLE_NOT_INVENTED",
        "inference_priority": "YIELD_TO_PRODUCTION_SESSION_AND_MODEL_SLOT",
        "acceptance": "FULL_WINDOWS_NO_MISSING_DECISIONS; PRESERVE_AND_REPORT_MODEL_ERRORS; INDEPENDENT_LEDGER_AUDIT",
        "interpretation": "SMALL_TECHNICAL_ONLY_AI_SAMPLE_NOT_ANNUAL_AI_WIN_RATE_OR_GATE_EXECUTION_PROOF"}
    path = root / "validation-plan.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing["plan"] != plan:
            raise ValueError("AI_SEGMENT_PREREGISTRATION_DRIFT_USE_NEW_RUN")
    else:
        path.write_text(canonical({"registered_at": datetime.now(timezone.utc).isoformat(),
            "plan": plan, "plan_sha256": digest(plan)}), encoding="utf-8")
    print("plan_sha256=" + digest(plan), flush=True)
    summary = []
    for key, start, end in WINDOWS:
        directory = root / key
        target = directory / "history.json"
        if target.exists():
            payload = json.loads(target.read_text(encoding="utf-8"))
            if payload.get("manifest_sha256") != manifest_hash(payload):
                raise ValueError("AI_SEGMENT_EXISTING_HISTORY_CORRUPT")
            if payload.get("validation_plan_sha256") != digest(plan):
                raise ValueError("AI_SEGMENT_HISTORY_PLAN_MISMATCH")
        else:
            payload = freeze_public_history(directory, utc(start), utc(end), symbols=plan["symbols"],
                progress=lambda message: print(key + ": " + message, flush=True))
            payload["assumptions"]["universe"] = "FIXED_BTC_ETH_PUBLIC_GATE_BENCHMARK_NOT_ENTIRE_EXCHANGE"
            payload["assumptions"]["selection"] = plan["selection"]
            payload["validation_plan_sha256"] = digest(plan)
            payload["manifest_sha256"] = manifest_hash(payload)
            target.write_text(canonical(payload), encoding="utf-8")
            (directory / "coverage.json").write_text(canonical({k: v for k, v in payload.items()
                if k not in {"bars", "news", "funding"}}), encoding="utf-8")
        if not payload["complete_data"]:
            raise ValueError("AI_SEGMENT_INCOMPLETE_PUBLIC_DATA:" + key)
        summary.append({"id": key, "history_path": str(target), "manifest_sha256": payload["manifest_sha256"],
            "bar_count": len(payload["bars"]), "complete_data": True})
    (root / "frozen-segments.json").write_text(canonical({"plan_sha256": digest(plan), "segments": summary}), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
