#!/usr/bin/env python3
"""Audit structured JSON emitted by the real Qwen smoke run without inventing outputs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.ai.contracts import ModelSignalResponse


REQUIRED_FIELDS = {
    "action",
    "confidence_raw",
    "entry_preference",
    "entry_zone",
    "stop",
    "tp1",
    "tp2",
    "signal_validity_minutes",
    "holding_horizon_minutes",
    "re_evaluate_minutes",
    "invalidation",
    "thesis",
    "risk_factors",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="data/phase2-real-smoke-qwen.json")
    parser.add_argument("--output", default="data/phase2-qwen-schema-audit.json")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)

    source = json.loads(Path(args.input).read_text(encoding="utf-8"))
    audits: list[dict[str, object]] = []
    errors = 0
    for record in source.get("records", []):
        symbol = record.get("symbol")
        signal = record.get("signal") if isinstance(record.get("signal"), dict) else {}
        raw = signal.get("raw_model_response")
        item: dict[str, object] = {"symbol": symbol, "parse_status": signal.get("parse_status"), "input_hash": signal.get("input_hash"), "errors": []}
        try:
            decoded = json.loads(raw) if isinstance(raw, str) else None
            if not isinstance(decoded, dict):
                raise ValueError("raw_model_response is not a JSON object")
            item["raw_fields"] = sorted(decoded)
            missing = sorted(REQUIRED_FIELDS - set(decoded))
            if missing:
                raise ValueError(f"missing schema fields: {', '.join(missing)}")
            response = ModelSignalResponse.from_dict(decoded)
            item["action"] = response.action.value
            item["validated_response"] = response.to_dict()
            if response.action.value == "WAIT" and any(value is not None for value in (response.entry_low, response.entry_high, response.stop, response.tp1, response.tp2)):
                raise ValueError("WAIT contains actionable levels")
            if response.action.value != "WAIT" and any(value is None for value in (response.entry_low, response.entry_high, response.stop, response.tp1, response.tp2)):
                raise ValueError("actionable response is missing levels")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            errors += 1
            item["errors"] = [str(exc)]
        audits.append(item)

    observed = sorted({str(item["action"]) for item in audits if item.get("action")})
    missing_actions = sorted({"LONG", "SHORT", "WAIT"} - set(observed))
    report = {
        "source": str(Path(args.input)),
        "records": audits,
        "observed_actions": observed,
        "missing_actions": missing_actions,
        "pass": errors == 0 and (not args.strict or not missing_actions),
        "errors": errors,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
