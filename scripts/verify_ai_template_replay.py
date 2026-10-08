"""Independently audit frozen AI decisions and economic ledger, read-only."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def point(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("AUDIT_NAIVE_TIME")
    return result.astimezone(timezone.utc)


def digest(value, *, ascii=True):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=ascii, separators=(",", ":"),
                                    default=str).encode()).hexdigest()


def number(value):
    if isinstance(value, dict):
        value = value["__decimal__"]
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("AUDIT_NONFINITE_AMOUNT")
    return result


def equal(actual, expected, label):
    actual, expected = number(actual), number(expected)
    tolerance = max(Decimal("0.00000001"), abs(expected) * Decimal("0.000000001"))
    if abs(actual - expected) > tolerance:
        raise ValueError(f"AUDIT_{label}: {actual} != {expected}")


def audit_account(account, reported=None):
    """Recompute P&L from lot prices/quantities; never call simulator.summary."""
    state, config = account["state"], account["config"]
    initial = number(config["initial_equity"])
    cutoff = point(state["last_advanced_through"]) if state["last_advanced_through"] else None
    events = state["events"]
    accepted = {e["order_id"]: e for e in events if e.get("status") == "ACCEPTED" and e.get("order_type")}

    def order_fee_type(order_id):
        order = accepted.get(order_id)
        if not order:
            raise ValueError("AUDIT_FEE_ORDER_ACCEPTANCE_MISSING")
        if order["order_type"] == "MARKET":
            expected = "TAKER"
        elif order["order_type"] == "LIMIT" and order.get("marketability_assumption") == "RESTING_LIMIT_PROXY":
            expected = "MAKER"
        elif order["order_type"] == "LIMIT" and order.get("marketability_assumption") == "SUBMISSION_LAST_PRICE_PROXY":
            expected = "TAKER"
        else:
            raise ValueError("AUDIT_FEE_MARKETABILITY_UNKNOWN")
        if order.get("fee_type") != expected:
            raise ValueError("AUDIT_ACCEPTED_ORDER_FEE_TYPE")
        equal(order["fee_rate"], config["maker_fee_rate" if expected == "MAKER" else "taker_fee_rate"], "ACCEPTED_ORDER_FEE_RATE")
        return expected

    def fee_event(lot, exit_row=None):
        if exit_row is None:
            matches = [e for e in events if e.get("lot_id") == lot["lot_id"]
                and e.get("action") in {"OPEN_LONG", "OPEN_SHORT"} and e.get("fill_price") is not None]
            fee_type = order_fee_type(lot["order_id"])
            quantity, price = number(lot["quantity"]), number(lot["entry_price"])
        else:
            matches = [e for e in events if e.get("lot_id") == lot["lot_id"]
                and e.get("order_id") == exit_row["order_id"] and e.get("event_time") == exit_row["at"]
                and e.get("status") == "FILLED" and e.get("action") == exit_row["reason"]]
            fee_type = order_fee_type(exit_row["order_id"]) if exit_row["reason"] == "ONE_WAY_NETTING" else "TAKER"
            quantity, price = number(exit_row["quantity"]), number(exit_row["price"])
        if len(matches) != 1:
            raise ValueError("AUDIT_UNIQUE_FEE_FILL_EVENT_REQUIRED")
        event = matches[0]
        if event.get("fee_type") != fee_type:
            raise ValueError("AUDIT_FILL_FEE_TYPE")
        rate = number(config["maker_fee_rate" if fee_type == "MAKER" else "taker_fee_rate"])
        equal(event["fee_rate"], rate, "FILL_FEE_RATE")
        equal(event["fill_price"], price, "FEE_FILL_PRICE")
        equal(event.get("opened_quantity", event["quantity"]) if exit_row is None else event["quantity"], quantity, "FEE_FILL_QUANTITY")
        expected = price * quantity * number(lot["contract_size"]) * rate
        equal(event["fee"], expected, "FILL_FEE_FORMULA")
        equal(lot["entry_fee"] if exit_row is None else exit_row["fee"], expected, "LOT_FEE_FORMULA")
        return expected

    gross = fees = funding = floating = Decimal(0)
    position_results = {}
    for position_id, position in state["positions"].items():
        direction = 1 if position["side"] == "LONG" else -1
        own_gross = own_fees = own_funding = remaining = Decimal(0)
        for lot in position["lots"]:
            quantity, entry, multiplier = (number(lot[k]) for k in ("quantity", "entry_price", "contract_size"))
            exits = lot.get("exits", [])
            closed = sum((number(e["quantity"]) for e in exits), Decimal(0))
            equal(lot["remaining_qty"], quantity - closed, "REMAINING_QUANTITY")
            if quantity < closed or number(lot["remaining_qty"]) < 0:
                raise ValueError("AUDIT_NEGATIVE_POSITION_QUANTITY")
            for exit_row in exits:
                when = point(exit_row["at"])
                if when < point(lot["opened_at"]) or (cutoff and when > cutoff):
                    raise ValueError("AUDIT_EXIT_TIME")
                pnl = direction * (number(exit_row["price"]) - entry) * number(exit_row["quantity"]) * multiplier
                equal(exit_row["gross_pnl"], pnl, "EXIT_GROSS_PNL")
                own_gross += pnl
            own_fees += fee_event(lot) + sum((fee_event(lot, e) for e in exits), Decimal(0))
            equal(lot["exit_fees"], sum((number(e["fee"]) for e in exits), Decimal(0)), "EXIT_FEES")
            lot_funding = Decimal(0)
            for record in lot.get("funding_entries", []):
                paid_at = point(record["at"])
                if paid_at < point(lot["opened_at"]) or (cutoff and max(paid_at, point(record["available_at"])) > cutoff):
                    raise ValueError("AUDIT_FUNDING_TIME")
                payment_quantity = quantity - sum((number(e["quantity"]) for e in exits if point(e["at"]) <= paid_at), Decimal(0))
                equal(record["quantity"], payment_quantity, "FUNDING_QUANTITY")
                payment = -direction * payment_quantity * multiplier * number(record["mark_price"]) * number(record["rate"])
                equal(record["funding_pnl"], payment, "FUNDING_PAYMENT")
                lot_funding += payment
            equal(lot["funding_pnl"], lot_funding, "LOT_FUNDING")
            own_funding += lot_funding
            remaining += number(lot["remaining_qty"])
            if number(lot["remaining_qty"]) > 0:
                mark = number(state["last_prices"][position["instrument_id"]])
                floating += direction * (mark - entry) * number(lot["remaining_qty"]) * multiplier
        position_results[position_id] = {"gross": own_gross, "fees": own_fees,
                                         "funding": own_funding, "remaining": remaining}
        gross += own_gross
        fees += own_fees
        funding += own_funding
    equal(state["realized_gross_pnl"], gross, "ACCOUNT_GROSS_PNL")
    equal(state["total_fees"], fees, "ACCOUNT_FEES")
    equal(state["funding_pnl"], funding, "ACCOUNT_FUNDING")
    equal(sum((number(e.get("fee", 0)) for e in events), Decimal(0)), fees, "EVENT_FEES")
    equal(sum((number(e.get("funding_pnl", 0)) for e in events if e["action"] == "FUNDING"), Decimal(0)), funding, "EVENT_FUNDING")
    closed_ids, winners = set(), 0
    for trade in state["completed_trades"]:
        position_id = trade["position_id"]
        if position_id in closed_ids:
            raise ValueError("AUDIT_DUPLICATE_CLOSED_POSITION")
        closed_ids.add(position_id)
        values = position_results[position_id]
        if values["remaining"] != 0:
            raise ValueError("AUDIT_OPEN_POSITION_COUNTED_CLOSED")
        net = values["gross"] + values["funding"] - values["fees"]
        for key, expected in (("realized_gross_pnl", values["gross"]), ("fees", values["fees"]),
                              ("funding_pnl", values["funding"]), ("net_pnl", net)):
            equal(trade[key], expected, "TRADE_" + key.upper())
        winners += net > 0
    equity = initial + gross + floating + funding - fees
    peak, drawdown = initial, Decimal(0)
    for row in state["equity_history"]:
        observed = number(row["equity"])
        peak = max(peak, observed)
        if peak > 0:
            drawdown = max(drawdown, (peak - observed) / peak)
    halted = bool(state["halted_reason"])
    if state["equity_history"] and not halted:
        equal(state["equity_history"][-1]["equity"], equity, "ENDING_EQUITY_MARK")
    win_rate = Decimal(winners) / len(closed_ids) if closed_ids else None
    result = {"ending_equity": None if halted else float(equity),
              "roi": None if halted else float(equity / initial - 1),
              "closed_trade_count": len(closed_ids), "wins": winners,
              "win_rate": None if win_rate is None else float(win_rate),
              "fees": float(fees), "funding_pnl": float(funding),
              "realized_gross_pnl": float(gross), "unrealized_pnl": float(floating),
              "max_drawdown": float(drawdown), "halted_reason": state["halted_reason"]}
    if reported is not None:
        for key, expected in result.items():
            if key == "halted_reason":
                if reported[key] != expected:
                    raise ValueError("AUDIT_REPORT_HALT")
            elif expected is None:
                if reported[key] is not None:
                    raise ValueError("AUDIT_REPORT_UNDEFINED_" + key.upper())
            else:
                equal(reported[key], expected, "REPORT_" + key.upper())
    return result


def _frozen_projection(projected, original, *, path=()):
    """Check retained facts; only old supplementary candles may round to 6 digits."""
    if isinstance(projected, dict) and isinstance(original, dict):
        return all(key in original and _frozen_projection(value, original[key], path=(*path, key))
                   for key, value in projected.items())
    if isinstance(projected, list) and isinstance(original, list):
        if projected == original:
            return True
        if path and path[-1] == "candles" and len(projected) == len(original):
            return all(new == old or (index < len(original) - 1 and isinstance(old, list)
                and new == [float(f"{float(v):.6g}") if type(v) in (int, float) else v for v in old])
                for index, (new, old) in enumerate(zip(projected, original)))
        # Removing older rows/symbols must preserve ordering and every retained fact.
        cursor = iter(original)
        return all(any(_frozen_projection(value, source, path=(*path, "item")) for source in cursor)
                   for value in projected)
    return type(projected) is type(original) and projected == original or (
        type(projected) in (int, float) and type(original) in (int, float) and projected == original)


def audit_ttl_wire_value(proposal):
    """Independently check the exact decimal representation, without production normalization."""
    import re
    result = dict(proposal)
    value = result.get("ttl_seconds")
    if isinstance(value, dict):
        fields = ('d3', 'd2', 'd1', 'd0')
        if (set(value) != set(fields) or any(type(value[k]) is not str
                or len(value[k]) != 1 or not '0' <= value[k] <= '9' for k in fields)):
            raise ValueError("AUDIT_TTL_WIRE_INVALID")
        number = sum((ord(value[k]) - ord('0')) * 10 ** (3 - index) for index, k in enumerate(fields))
        if number == 0 and result.get("action") == "WAIT":
            result["ttl_seconds"] = None
            return result
        if not 60 <= number <= 1800:
            raise ValueError("AUDIT_TTL_WIRE_INVALID")
        result["ttl_seconds"] = number
    elif isinstance(value, str):
        if not re.fullmatch(r"[1-9][0-9]{1,3}", value) or not 60 <= int(value) <= 1800:
            raise ValueError("AUDIT_TTL_WIRE_INVALID")
        result["ttl_seconds"] = int(value)
    return result


def audit_effective_request(ctx, payload, load_bundle):
    """Verify the original prompt and each bound repair before accepting its schema."""
    from core.trading.model_schemas import validate_schema
    original = payload["prompt_request"]
    if digest({"messages": original["messages"]}) != ctx["input_hash"] or ctx["input_hash"] != original["request_hash"]:
        raise ValueError("AUDIT_MODEL_INPUT_HASH")
    if digest(original["local_validation_schema"]) != original["response_schema_sha256"]:
        raise ValueError("AUDIT_SCHEMA_HASH")
    effective = original
    settings = ctx["model_inference_settings"]
    final = audit_ttl_wire_value(json.loads(ctx["model_raw_response"]))
    for attempt in settings.get("model_attempts", []):
        repair = load_bundle(attempt["evidence_bundle_id"])
        request = repair["prompt_request"]
        if (repair.get("source_bundle_id") != ctx["evidence_bundle_id"]
                or digest({"messages": request["messages"]}) != request["request_hash"]
                or request["request_hash"] != attempt["request_hash"]
                or digest(request["local_validation_schema"]) != request["response_schema_sha256"]):
            raise ValueError("AUDIT_REPAIR_REQUEST_BINDING")
        wrapper = json.loads(request["messages"][1]["content"])
        inputs = wrapper["inputs"]
        if (inputs.get("account_truth") != payload["prompt_inputs"].get("account_truth")
                or not _frozen_projection(inputs, payload["prompt_inputs"])):
            raise ValueError("AUDIT_REPAIR_CHANGED_FROZEN_FACTS")
        if attempt["prompt_version"] == ctx.get("model_call_prompt_version"):
            previous = wrapper.get("previous_decision") or {}
            mutable = set(wrapper.get("mutable_fields") or [])
            if previous.get("action") in {"OPEN_LONG", "OPEN_SHORT"}:
                price_fields = {"entry_price", "stop_price", "take_profit"}
                if mutable - {"position_size_usdt", "requested_leverage", "order_preference", "ttl_seconds", "evidence_refs", *price_fields}:
                    raise ValueError("AUDIT_REPAIR_MUTABLE_FIELDS")
                if any(field in mutable and previous.get(field) is not None for field in price_fields):
                    raise ValueError("AUDIT_REPAIR_CHANGED_TRADE")
                for field in ("action", "instrument_id", "entry_price", "stop_price", "take_profit",
                              "requested_risk_fraction", "position_size_usdt", "requested_leverage", "order_preference",
                              "limit_price", "ttl_seconds", "candidate_id", "strategy_candidate_id"):
                    if field in previous and field not in mutable and final.get(field) != previous[field]:
                        raise ValueError("AUDIT_REPAIR_CHANGED_TRADE")
            effective = request
    if digest(effective["local_validation_schema"]) != settings["response_schema_sha256"]:
        raise ValueError("AUDIT_SCHEMA_HASH")
    validate_schema(final, effective["local_validation_schema"])
    from core.trading.model_schemas import require_nofx_gate_open_contract
    require_nofx_gate_open_contract(final)
    return effective


def audit_protection_update_values(raw, decision, normalizations):
    """Independently compare recorded model prices to executed update fields."""
    if raw.get("action") != "UPDATE_PROTECTION" and decision.get("action") != "UPDATE_PROTECTION":
        return
    for key in ("action", "instrument_id", "position_id"):
        if raw.get(key) != decision.get(key):
            raise ValueError("AUDIT_PROTECTION_IDENTITY_CHANGED")
    proofs = [item for item in normalizations if isinstance(item, dict)
              and item.get("normalization") == "EXACT_MODEL_AUTHORED_UPDATE_PROTECTION_ALIAS"]
    for alias, canonical in (("stop_price", "new_stop_price"), ("take_profit", "new_take_profit")):
        source, original = raw.get(alias), raw.get(canonical)
        for value in (source, original):
            if value is not None and (type(value) not in (int, float)
                    or not number(value).is_finite() or number(value) <= 0):
                raise ValueError("AUDIT_PROTECTION_INVALID_PRICE")
        if source is not None and original is not None and number(source) != number(original):
            raise ValueError("AUDIT_PROTECTION_CONFLICT")
        expected = original if original is not None else source
        actual = decision.get(canonical)
        if (actual is None) != (expected is None) or (expected is not None
                and (type(actual) not in (int, float) or number(actual) != number(expected))):
            raise ValueError("AUDIT_PROTECTION_PRICE_LOST_OR_CHANGED")
        if decision.get(alias) != source:
            raise ValueError("AUDIT_PROTECTION_ALIAS_CHANGED")
        if source is not None and original is None:
            if len(proofs) != 1:
                raise ValueError("AUDIT_PROTECTION_PROVENANCE_REQUIRED")
            proof = proofs[0]
            detail = (proof.get("field_sources") or {}).get(canonical)
            if (detail != {"source_field": alias, "source_value": source, "canonical_original_value": None}
                    or canonical not in (proof.get("normalized_fields") or [])
                    or proof.get("execution_prices_generated") is not False
                    or proof.get("original_price_fields_preserved") is not True):
                raise ValueError("AUDIT_PROTECTION_PROVENANCE_INVALID")


def audit_replay(database, manifest_path, *, require_complete=False, sft_marker=None):
    from core.replay.ai_template_runner import frozen_source_fingerprint, _verify_model_receipt
    from types import SimpleNamespace
    from core.trading.model_schemas import validate_schema
    database, manifest_path = Path(database).resolve(), Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_bars = {(b["symbol"], b["timeframe"], b["bar_end"]): b for b in manifest["bars"]}
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        runs = db.execute("SELECT * FROM ai_template_replay_runs").fetchall()
        if len(runs) != 1:
            raise ValueError("AUDIT_ONE_RUN_REQUIRED")
        run = dict(runs[0])
        config, checkpoint = json.loads(run["config_json"]), json.loads(run["checkpoint_json"])
        report = json.loads(run["result_json"]) if run["result_json"] else None
        if digest(config, ascii=False) != run["config_hash"] or config["source_sha256"] != frozen_source_fingerprint():
            raise ValueError("AUDIT_CONFIG_OR_SOURCE_HASH")
        if digest({k: v for k, v in manifest.items() if k != "manifest_sha256"}, ascii=False) != run["manifest_hash"]:
            raise ValueError("AUDIT_MANIFEST_HASH")
        if config["model_source"] != "PRODUCTION_GEMINI":
            raise ValueError("AUDIT_ACTUAL_MODEL_REQUIRED")
        rows = [dict(r) for r in db.execute("SELECT * FROM ai_template_replay_decisions ORDER BY rowid")]
        completed = [r for r in rows if r["status"] == "COMPLETED"]
        start, end = point(manifest["window_start"]), point(manifest["window_end"])
        expected_keys = set()
        when = start
        while when < end:
            for template in config["templates"]:
                if when.minute % int(template["execution"]["scan_interval_minutes"]) == 0:
                    expected_keys.add((template["template_id"], when.isoformat()))
            when += timedelta(minutes=5)
        actual_keys = {(r["template_id"], r["as_of"]) for r in rows}
        if len(actual_keys) != len(rows) or not actual_keys <= expected_keys:
            raise ValueError("AUDIT_NATIVE_SCHEDULE")
        statuses = dict(Counter(r["status"] for r in rows))
        if require_complete and (run["status"] != "COMPLETED" or actual_keys != expected_keys
                                 or len(completed) != len(rows) or checkpoint["errors"]
                                 or not report or not report["comparison_eligible"]):
            raise ValueError("AUDIT_COMPLETE_COMPARISON_REQUIRED")
        token_counts, widths = [], Counter()
        for row in completed:
            ctx, receipt = json.loads(row["context_json"]), json.loads(row["result_json"])
            evidence = db.execute("SELECT payload_json FROM evidence_bundles WHERE bundle_id=?",
                                  (ctx["evidence_bundle_id"],)).fetchone()
            payload = json.loads(evidence[0])
            request, inputs, settings = payload["prompt_request"], payload["prompt_inputs"], ctx["model_inference_settings"]
            def load_bundle(bundle_id):
                record = db.execute("SELECT payload_json FROM evidence_bundles WHERE bundle_id=?", (bundle_id,)).fetchone()
                if record is None:
                    raise ValueError("AUDIT_REPAIR_BUNDLE_MISSING")
                return json.loads(record[0])
            audit_effective_request(ctx, payload, load_bundle)
            audit_protection_update_values(json.loads(ctx["model_raw_response"]),
                json.loads(row["decision_json"]), settings.get("model_output_normalizations") or [])
            if hashlib.sha256(ctx["model_raw_response"].encode()).hexdigest() != row["response_sha256"]:
                raise ValueError("AUDIT_MODEL_RESPONSE_HASH")
            pin = config["model_pin"]
            try:
                _verify_model_receipt(SimpleNamespace(**ctx), pin)
            except ValueError as exc:
                raise ValueError("AUDIT_MODEL_IDENTITY") from exc
            visible, deferred = set(inputs["allowed_instruments"]), set(settings["prompt_compaction"]["deferred_symbols"])
            if visible & deferred or visible | deferred != set(ctx["allowed_instruments"]):
                raise ValueError("AUDIT_CANDIDATE_VISIBILITY")
            widths[len(visible)] += 1
            tokens = settings["estimated_input_tokens"]
            if tokens + settings["output_token_reserve"] + 256 > pin["context_length"]:
                raise ValueError("AUDIT_CONTEXT_WINDOW")
            token_counts.append(tokens)
            as_of, submission = point(row["as_of"]), point(row["submission_at"])
            elapsed_end = as_of + timedelta(seconds=row["wall_elapsed_seconds"])
            if submission < elapsed_end or submission - elapsed_end >= timedelta(minutes=1) or submission.second or submission.microsecond:
                raise ValueError("AUDIT_SUBMISSION_CLOCK")
            if receipt["private_exchange_calls"] != 0:
                raise ValueError("AUDIT_PRIVATE_CALLS")
            for revision in ctx["news_revisions"]:
                if max(point(revision["published_at"]), point(revision["known_at"])) > as_of:
                    raise ValueError("AUDIT_FUTURE_NEWS")
            for symbol, item in ctx["technical_context"].items():
                for timeframe, frame in item.get("timeframes", {}).items():
                    for bar in frame.get("bars", []):
                        original = source_bars[(symbol, timeframe, bar["bar_end"])]
                        if max(point(bar["bar_end"]), point(original["available_at"])) > as_of or any(bar[k] != original[k] for k in ("open", "high", "low", "close", "volume")):
                            raise ValueError("AUDIT_HISTORICAL_BAR")
                    fact = frame.get("closed_bar_breakout", {})
                    if fact.get("status") == "READY" and not point(fact["ref_end"]) < point(fact["bar_end"]) <= as_of:
                        raise ValueError("AUDIT_BREAKOUT_REFERENCE")
            if any(point(s["data_as_of"]) != submission for s in receipt["execution_market_snapshots"].values()):
                raise ValueError("AUDIT_EXECUTION_SNAPSHOT_TIME")
        if sft_marker:
            marker = json.loads(Path(sft_marker).read_text(encoding="utf-8"))
            production_sft = Path(r"D:\RJ\AI Market Analyst\data\fin_tuning\crypto_sft_dataset.jsonl")
            if production_sft.stat().st_size != marker["size"] or hashlib.sha256(production_sft.read_bytes()).hexdigest() != marker["sha256"]:
                raise ValueError("AUDIT_PRODUCTION_SFT_CHANGED")
        reported = {r["template_id"]: r for r in report["results"]} if require_complete else {}
        templates = {t["template_id"]: t for t in config["templates"]}
        if set(checkpoint["accounts"]) != set(templates):
            raise ValueError("AUDIT_ALL_FIVE_TEMPLATE_ACCOUNTS_REQUIRED")
        accounts = {}
        for key, value in checkpoint["accounts"].items():
            for field in ("initial_equity", "maker_fee_rate", "taker_fee_rate"):
                equal(value["config"][field], config[field], "FROZEN_ACCOUNT_" + field.upper())
            equal(value["config"]["margin_cap_pct"], templates[key]["execution"]["max_margin_pct"], "FROZEN_MARGIN_CAP")
            accounts[key] = audit_account(value, reported.get(key))
        if require_complete and any(a["halted_reason"] for a in accounts.values()):
            raise ValueError("AUDIT_ACCOUNT_HALTED")
    return {"status": "PASS", "observed_at": datetime.now(timezone.utc).isoformat(),
            "scope": "COMPLETE_HISTORICAL_COMPARISON" if require_complete else "PARTIAL_LEDGER_NOT_FINAL_RETURNS",
            "run_id": run["run_id"], "expected_native_scans": len(expected_keys),
            "completed_decisions": len(completed), "row_statuses": statuses,
            "visible_candidate_widths": dict(widths), "max_input_tokens": max(token_counts, default=0),
            "accounts": accounts, "production_sft_checked": bool(sft_marker),
            "private_exchange_execution_proven": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--sft-marker", type=Path)
    args = parser.parse_args()
    result = audit_replay(args.database, args.manifest, require_complete=args.require_complete, sft_marker=args.sft_marker)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
