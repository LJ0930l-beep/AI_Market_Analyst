"""Read-only trigger-touch diagnostics; a price touch never proves a full setup."""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.replay.ai_history import digest, manifest_hash, utc


def analyze_waits(rows, bars):
    grouped = defaultdict(list)
    market = defaultdict(list)
    for bar in bars:
        if bar.get("timeframe") == "1m":
            market[bar["symbol"]].append(bar)
    for row in rows:
        grouped[row["template_id"]].append(row)
    findings = []
    for template, scans in sorted(grouped.items()):
        scans.sort(key=lambda r: utc(r["as_of"]))
        for index, row in enumerate(scans):
            decision = json.loads(row.get("decision_json") or "{}")
            if row["status"] != "COMPLETED" or decision.get("action") != "WAIT":
                continue
            following = scans[index+1] if index+1 < len(scans) else None
            analysis = decision.get("strategy_analysis") or {}
            missing = analysis.get("missing_conditions") or []
            trigger = decision.get("next_trigger_price")
            valid_trigger = type(trigger) in (int, float) and math.isfinite(trigger) and trigger > 0
            symbol = decision.get("instrument_id")
            entry = {
                "template_id": template, "as_of": row["as_of"], "symbol": symbol,
                "next_trigger_price": trigger if valid_trigger else None,
                "entry_condition": decision.get("entry_condition"), "missing_conditions": missing,
                "reason": decision.get("reason"), "issues": [],
                "first_price_touch_at": None, "next_action": None,
                "next_execution_status": None, "next_same_symbol": None,
                "full_setup_confirmed": False,
            }
            if not valid_trigger:
                entry["issues"].append("NO_VERIFIABLE_TRIGGER_PRICE")
            if not isinstance(missing, list) or not any(isinstance(x, str) and x.strip() for x in missing):
                entry["issues"].append("NO_EXPLICIT_MISSING_CONDITIONS")
            if following is not None:
                next_decision = json.loads(following.get("decision_json") or "{}")
                result = json.loads(following.get("result_json") or "{}")
                entry.update(next_action=next_decision.get("action"),
                             next_reason=next_decision.get("reason"),
                             next_missing_conditions=(next_decision.get("strategy_analysis") or {}).get("missing_conditions"),
                             next_execution_status=result.get("status"),
                             next_same_symbol=next_decision.get("instrument_id") == symbol,
                             next_scan_status=following["status"], next_scan_at=following["as_of"])
            if not following or following["status"] in {"MODEL_STARTED", "MODEL_DONE"}:
                entry["status"] = "AWAITING_NEXT_SCAN"
            elif not valid_trigger or not row.get("submission_at"):
                entry["status"] = "UNVERIFIABLE_TRIGGER"
            else:
                start, end = utc(row["submission_at"]), utc(following["as_of"])
                # Bars completed during model thinking cannot be opportunities
                # acted on after this WAIT. Only assess the following scan's
                # available history, never the remaining future window.
                touched = [b for b in market[symbol] if start < utc(b["bar_end"]) <= end
                           and utc(b["available_at"]) <= end
                           and b["low"] <= trigger <= b["high"]]
                if touched:
                    entry["first_price_touch_at"] = min(b["bar_end"] for b in touched)
                    entry["status"] = "PRICE_TOUCHED_SETUP_NOT_PROVEN"
                    if following["status"] == "ERROR":
                        entry["issues"].append("NEXT_SCAN_ERROR_AFTER_TOUCH")
                    elif entry["next_action"] == "WAIT":
                        entry["issues"].append("REVIEW_WAIT_AFTER_PRICE_TOUCH_NOT_AUTOMATIC_FAILURE")
                else:
                    entry["status"] = "NO_PRICE_TOUCH_BEFORE_NEXT_SCAN"
            findings.append(entry)
    return findings


def audit_directory(directory):
    directory = Path(directory).resolve()
    registration = json.loads((directory / "research-plan.json").read_text(encoding="utf-8"))
    if digest(registration["plan"]) != registration["plan_sha256"]:
        raise ValueError("RESEARCH_PLAN_HASH_MISMATCH")
    windows = []
    for window in registration["plan"]["pilot_windows"]:
        path = (directory / window["id"]).resolve()
        if path.parent != directory:
            raise ValueError("WAIT_FOLLOWTHROUGH_PATH_OUTSIDE_EXPERIMENT")
        database = path / "results.sqlite3"
        if not database.exists():
            continue
        history = json.loads((path / "history.json").read_text(encoding="utf-8"))
        if manifest_hash(history) != history["manifest_sha256"]:
            raise ValueError("HISTORY_HASH_MISMATCH")
        if history.get("research_plan_sha256") != registration["plan_sha256"]:
            raise ValueError("WAIT_FOLLOWTHROUGH_HISTORY_PLAN_MISMATCH")
        with closing(sqlite3.connect(database.as_uri()+"?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            rows = [dict(r) for r in db.execute("SELECT * FROM ai_template_replay_decisions")]
        findings = analyze_waits(rows, history["bars"])
        windows.append({"window": window["id"], "partition": window["partition"],
            "manifest_sha256": history["manifest_sha256"], "decision_snapshot_sha256": digest(rows),
            "statuses": dict(Counter(f["status"] for f in findings)), "findings": findings})
    return {"scope": "READ_ONLY_WAIT_PRICE_TOUCH_DIAGNOSTICS_NOT_FULL_SETUP_OR_PROFIT_ACCEPTANCE",
        "observed_at": datetime.now(timezone.utc).isoformat(), "windows": windows,
        "future_bars_given_to_model": False, "private_exchange_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    report = audit_directory(args.directory)
    (args.directory / "wait-followthrough.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"scope": report["scope"], "windows": [
        {"window": w["window"], "statuses": w["statuses"]} for w in report["windows"]]}))


if __name__ == "__main__":
    main()
