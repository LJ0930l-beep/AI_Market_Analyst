"""Independently verify frozen proxy artifacts and recompute trade economics."""
from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def audit(directory):
    directory = Path(directory).resolve()
    report = json.loads((directory / "proxy-results.json").read_text(encoding="utf-8"))
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    config = json.loads((directory / "proxy-config.json").read_text(encoding="utf-8"))
    findings = []
    verified = []

    def check(condition, description):
        if not condition:
            findings.append(description)

    def equal(actual, expected, label):
        check(abs(Decimal(str(actual)) - Decimal(str(expected))) <= Decimal("0.000001"), label)

    check(report["status"] == "COMPLETED" and report["ai_calls"] == 0, "proxy scope/status")
    check(report["config"] == config and report["config_sha256"] == digest(config), "config hash")
    check(report["data_manifest"] == manifest, "manifest drift")
    check(hashlib.sha256((directory / "research.sqlite3").read_bytes()).hexdigest()
        == manifest["dataset_sha256"], "database hash")
    source = Path(__file__).resolve().parents[1] / "core/replay/technical_proxy.py"
    check(hashlib.sha256(source.read_bytes()).hexdigest() == report["proxy_source_sha256"], "proxy source drift")
    for artifact in manifest["archived_files"]:
        name = artifact["url"].rsplit("/", 1)[-1]
        cached = directory / "raw_cache" / name
        observed = hashlib.sha256(cached.read_bytes()).hexdigest()
        official = cached.with_name(name + ".CHECKSUM").read_text(encoding="utf-8").split()[0]
        check(observed == official == artifact["sha256"], f"archive checksum: {name}")
        check(cached.stat().st_size == artifact["bytes"], f"archive size: {name}")
    database = directory / "research.sqlite3"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        check(connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity")
        funding_map = {(s, ts): rate for s, ts, rate in connection.execute(
            "SELECT symbol,payment_time_ms,rate FROM funding")}
        for result in report["results"]:
            name = result["template_id"]
            check(result["open_positions"] == result["pending_orders"] == 0,
                  f"{name}: audit requires no remaining exposure")
            gross_total = fees_total = funding_total = net_total = Decimal(0)
            wins = 0
            gap_target_entry_bars = 0
            for trade in result["trades"]:
                quantity, entry, exit_price = (Decimal(str(trade[k])) for k in ("quantity", "entry", "exit"))
                direction = Decimal(1 if trade["side"] == "LONG" else -1)
                gross = direction * quantity * (exit_price - entry)
                entry_rate = config["maker_fee_rate"] if trade["entry_fee_type"] == "MAKER" else config["taker_fee_rate"]
                fees = quantity * (entry * Decimal(str(entry_rate)) + exit_price * Decimal(str(config["taker_fee_rate"])))
                funding = Decimal(0)
                for payment in trade["funding_entries"]:
                    check((trade["symbol"], payment["paid_at_ms"]) in funding_map, f"{name}: unknown funding payment")
                    equal(payment["rate"], funding_map.get((trade["symbol"], payment["paid_at_ms"]), 0), f"{name}: funding rate")
                    amount = -direction * quantity * Decimal(str(payment["mark_price"])) * Decimal(str(payment["rate"]))
                    equal(payment["pnl"], amount, f"{name}: funding economics")
                    funding += amount
                net = gross - fees + funding
                for field, expected in (("gross_pnl", gross), ("fees", fees), ("funding_pnl", funding), ("net_pnl", net)):
                    equal(trade[field], expected, f"{name}: trade {field}")
                check(trade["closed_at_ms"] > trade["opened_at_ms"], f"{name}: execution time ordering")
                if trade["closed_at_ms"] == trade["opened_at_ms"] + 60_000 and trade["exit_reason"] == "TAKE_PROFIT":
                    opening = connection.execute("SELECT open FROM bars WHERE symbol=? AND open_time_ms=?",
                        (trade["symbol"], trade["opened_at_ms"])).fetchone()[0]
                    # Detect a pre-entry gap price being used to credit a newly filled target.
                    if (direction > 0 and opening > entry) or (direction < 0 and opening < entry):
                        unadjusted_exit = exit_price / (1 - direction * Decimal(str(config["slippage_bps"])) / 10_000)
                        if abs(unadjusted_exit - Decimal(str(opening))) < Decimal("0.0000001"):
                            gap_target_entry_bars += 1
                gross_total += gross
                fees_total += fees
                funding_total += funding
                net_total += net
                wins += int(net > 0)
            equal(result["realized_gross_pnl"], gross_total, f"{name}: gross total")
            equal(result["fees"], fees_total, f"{name}: fees total")
            equal(result["funding_pnl"], funding_total, f"{name}: funding total")
            ending = Decimal(str(config["initial_equity"])) + net_total
            equal(result["ending_equity"], ending, f"{name}: ending equity")
            equal(result["roi"], ending / Decimal(str(config["initial_equity"])) - 1, f"{name}: ROI")
            check(wins == result["wins"] and len(result["trades"]) == result["closed_trade_count"], f"{name}: counts")
            if result["trades"]:
                equal(result["win_rate"], Decimal(wins) / len(result["trades"]), f"{name}: win rate")
            else:
                check(result["win_rate"] is None, f"{name}: undefined zero-sample win rate")
            check(result["counts"]["accepted"] == result["counts"]["fills"] + result["counts"].get("expired", 0), f"{name}: order conservation")
            check(result["counts"]["fills"] == len(result["trades"]), f"{name}: fill conservation")
            daily_peak = Decimal(str(config["initial_equity"]))
            daily_dd = Decimal(0)
            for point in result["daily_equity"]:
                value = Decimal(str(point["equity"]))
                daily_peak = max(daily_peak, value)
                daily_dd = max(daily_dd, (daily_peak - value) / daily_peak)
            check(len(result["daily_equity"]) == 365, f"{name}: daily coverage")
            check(Decimal(str(result["max_drawdown"])) + Decimal("0.000001") >= daily_dd, f"{name}: drawdown below daily bound")
            verified.append({"template_id": name, "trades_checked": len(result["trades"]),
                "net_pnl": float(net_total), "wins": wins, "daily_drawdown_lower_bound": float(daily_dd),
                "new_fill_target_gap_credits": gap_target_entry_bars})
    return {"status": "PASS" if not findings else "FAIL", "analysis_type": "TECHNICAL_PROXY_NOT_AI",
        "archives_rechecked": len(manifest["archived_files"]), "ledger": verified,
        "failures": findings, "limitations": ["Minute drawdown is only checked against a daily lower bound here; deterministic replay is a separate check.",
            "Ledger arithmetic verification does not prove historical market fill accuracy."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.directory)
    (args.directory / "independent-proxy-audit.json").write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
