"""Read-only verification of fixed entry value and model-selected leverage.

Accepted historical simulation orders are not proof of private Gate orders.
Prices at later market fills may differ from the submission sizing reference.
"""
import argparse
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import json
from pathlib import Path
import sqlite3


def value(raw):
    if isinstance(raw, bool):
        raise ValueError("AUDIT_BOOLEAN_AMOUNT")
    result = Decimal(str(raw))
    if not result.is_finite() or result <= 0:
        raise ValueError("AUDIT_INVALID_AMOUNT")
    return result


def audit_entry(decision, event, snapshot, execution):
    if execution.get("sizing_mode") != "FIXED_NOTIONAL" or value(execution["fixed_notional_usdt"]) != 2000:
        raise ValueError("AUDIT_FIXED_2000_REQUIRED")
    if execution.get("leverage_mode") != "VENUE_LIMIT":
        raise ValueError("AUDIT_MODEL_LEVERAGE_MODE_REQUIRED")
    if event.get("action") != decision["action"] or event.get("instrument_id") != decision["instrument_id"]:
        raise ValueError("AUDIT_ENTRY_IDENTITY")
    market = snapshot["market"]
    step = value(market["precision"]["amount"])
    multiplier = value(market["contractSize"])
    order_type = event["order_type"]
    if order_type != decision["order_preference"]:
        raise ValueError("AUDIT_MODEL_ORDER_TYPE")
    if order_type == "LIMIT":
        tick = value(market["precision"]["price"])
        raw = value(decision.get("limit_price") or decision["entry_price"])
        rounding = ROUND_DOWN if decision["action"] == "OPEN_LONG" else ROUND_UP
        reference = (raw / tick).to_integral_value(rounding=rounding) * tick
        if value(event["limit_price"]) != reference:
            raise ValueError("AUDIT_LIMIT_PRICE_ROUNDING")
    elif order_type == "MARKET":
        reference = value(snapshot["price"])
    else:
        raise ValueError("AUDIT_UNKNOWN_ORDER_TYPE")
    target = value(execution["fixed_notional_usdt"])
    expected = (target / (reference * multiplier) / step).to_integral_value(rounding=ROUND_DOWN) * step
    actual = value(event["quantity"])
    if actual != expected or value(event["requested_quantity"]) != expected or event.get("quantity_capped"):
        raise ValueError("AUDIT_ENTRY_SILENT_RESIZING")
    requested = decision["requested_leverage"]
    if type(requested) is not int or requested < 1:
        raise ValueError("AUDIT_MODEL_LEVERAGE_REQUIRED")
    ceiling = value(market["limits"]["leverage"]["max"])
    if value(event["leverage"]) != min(Decimal(requested), ceiling):
        raise ValueError("AUDIT_LEVERAGE_CHANGED_OUTSIDE_VENUE_CEILING")
    submitted = actual * reference * multiplier
    quantum = reference * multiplier * step
    if not 0 <= target - submitted < quantum:
        raise ValueError("AUDIT_NOTIONAL_ROUNDING_OUTSIDE_ONE_STEP")
    return {"order_id": event["order_id"], "instrument_id": event["instrument_id"],
            "target_notional_usdt": str(target), "submission_reference_price": str(reference),
            "accepted_notional_usdt": str(submitted), "rounding_quantum_usdt": str(quantum),
            "model_requested_leverage": requested, "accepted_leverage": event["leverage"],
            "venue_leverage_ceiling": str(ceiling), "quantity": str(actual)}


def audit_directory(directory):
    entries = []
    for database in sorted(Path(directory).glob("*/results.sqlite3")):
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.execute("BEGIN")
            config = json.loads(db.execute("SELECT config_json FROM ai_template_replay_runs").fetchone()[0])
            templates = {t["template_id"]: t["execution"] for t in config["templates"]}
            for template, raw_decision, raw_result in db.execute(
                    "SELECT template_id,decision_json,result_json FROM ai_template_replay_decisions WHERE status='COMPLETED'"):
                decision, result = json.loads(raw_decision), json.loads(raw_result)
                if result.get("private_exchange_calls") != 0:
                    raise ValueError("AUDIT_RESEARCH_PRIVATE_CALLS")
                for event in result.get("events", []):
                    if event.get("status") == "ACCEPTED" and event.get("action") in {"OPEN_LONG", "OPEN_SHORT"}:
                        entry = audit_entry(decision, event,
                            result["execution_market_snapshots"][event["instrument_id"]], templates[template])
                        entries.append({"window": database.parent.name, "template_id": template, **entry})
    return {"status": "PASS" if entries else "NO_ACCEPTED_ENTRY_EVIDENCE", "observed_at": datetime.now(timezone.utc).isoformat(),
            "scope": "ACCEPTED_SIMULATION_ORDER_SIZING_NOT_GATE_FILL_OR_PROFIT_ACCEPTANCE",
            "accepted_entries_checked": len(entries), "entries": entries, "private_exchange_execution_proven": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    report = audit_directory(args.directory)
    (args.directory / "fixed-entry-audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
