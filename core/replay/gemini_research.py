"""Chronological research using verified year archives, never live execution.

Only bounded windows are materialized. Price/funding come from Binance's actual
archives; Gate's frozen current rules are an explicitly disclosed execution
proxy. This cannot establish historical Gate returns or train cloud weights.
"""
from calendar import monthrange
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from pathlib import Path
import sqlite3

from .ai_history import canonical, digest, manifest_hash, normalize_bar, utc, FRAME_MINUTES
from .ai_template_runner import frozen_source_fingerprint, frozen_templates
from core.model_routing import DEFAULT_MODEL


def research_price_action(rows, timeframe, as_of):
    """Offline-only Binance identity, without relabeling bars as Gate data."""
    from core.trading.price_action_structure import _build_price_action_structure
    def valid(row):
        if not isinstance(row, dict):
            return False
        symbol = row.get("symbol")
        metadata = row.get("payload_json")
        return (isinstance(symbol, str) and symbol in {"BTCUSDT", "ETHUSDT"}
                and row.get("provider") == row.get("venue") == "binance"
                and row.get("source") == "binance_official_monthly_archive"
                and row.get("instrument_key") == f"binance:perpetual:{symbol}:USDT:last"
                and row.get("native_symbol") == symbol and row.get("synthetic") is False
                and isinstance(metadata, dict) and metadata.get("provider") == "binance"
                and metadata.get("source") == row["source"] and metadata.get("synthetic") is False
                and re.fullmatch(r"[a-f0-9]{64}", str(row.get("raw_hash") or "")) is not None)
    result = _build_price_action_structure(rows, timeframe, as_of, identity_validator=valid)
    result["source_scope"] = "BINANCE_FROZEN_ARCHIVE_RESEARCH_ONLY_NOT_GATE_EVIDENCE"
    return result


def file_sha256(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def add_months(point, count):
    index = point.year * 12 + point.month - 1 + count
    year, month0 = divmod(index, 12)
    return point.replace(year=year, month=month0 + 1,
                         day=min(point.day, monthrange(year, month0 + 1)[1]))


def research_plan(archive_directory, *, template_ids=None, optimization_window_hours=12):
    if optimization_window_hours not in (12, 48):
        raise ValueError("RESEARCH_OPTIMIZATION_WINDOW_HOURS_INVALID")
    root = Path(archive_directory).resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    database = root / "research.sqlite3"
    if manifest.get("complete_data") is not True or file_sha256(database) != manifest.get("dataset_sha256"):
        raise ValueError("RESEARCH_YEAR_DATA_NOT_VERIFIED")
    start, end = utc(manifest["window_start"]), utc(manifest["window_end"])
    if add_months(start, 12) != end:
        raise ValueError("RESEARCH_TWELVE_MONTH_DATA_REQUIRED")
    six, nine = add_months(start, 6), add_months(start, 9)
    # Chosen by calendar only, before any model decisions or strategy returns.
    windows = [{"id": f"pilot-{index + 1}", "start": (start + timedelta(days=days)).isoformat(),
                "end": (start + timedelta(days=days, hours=optimization_window_hours)).isoformat(), "partition": "optimization"}
               for index, days in enumerate((14, 74, 134))]
    templates = frozen_templates(template_ids=template_ids)
    from core.trading.ai_session_coordinator import _session_context_length
    input_budget = _session_context_length()
    scans = sum(int(optimization_window_hours * 60 / int(t["execution"]["scan_interval_minutes"])) for t in templates)
    return {"schema_version": "gemini_year_research_v1", "research_only": True,
            "model_id": DEFAULT_MODEL, "model_weights_trained": False,
            "application_input_budget": input_budget,
            "archive_database": str(database), "dataset_sha256": manifest["dataset_sha256"],
            "symbols": manifest["symbols"], "window_start": start.isoformat(), "window_end": end.isoformat(),
            "partitions": [{"id": name, "start": lower.isoformat(), "end": upper.isoformat()}
                           for name, lower, upper in (("optimization", start, six), ("validation", six, nine),
                                                     ("untouched_test", nine, end))],
            "pilot_windows": windows, "expected_pilot_decisions": scans * len(windows),
            "templates": templates, "source_sha256": frozen_source_fingerprint(),
            "news": "NO_HISTORICAL_ARCHIVE_UNKNOWN_NOT_INVENTED",
            "model_funding": "LAST_SETTLED_BINANCE_RATE_PAYMENT_PLUS_60S_RESEARCH_PUBLICATION_LAG_NOT_GATE_CURRENT_RATE",
            "selection": "FIXED_CALENDAR_WINDOWS_IN_OPTIMIZATION_ONLY_NO_RETURN_SELECTION",
            "objectives": ["net_return_after_costs", "max_drawdown", "profit_factor", "trade_count", "wait_causes"],
            "protocol": "OPTIMIZE_FIRST_SIX_MONTHS_VALIDATE_NEXT_THREE_TEST_LAST_THREE_ONCE",
            "heldout_protocol": {"window_hours": 48, "calendar_offsets_days": [14, 44, 74],
                "minimum_closed_trades_per_strategy": 30,
                "scope": "PREREGISTERED_BEFORE_HELDOUT_RESULTS_SAMPLE_THRESHOLD_STILL_REQUIRED"},
            "deployment": "NO_AUTOMATIC_STRATEGY_ACTIVATION_NO_PRODUCTION_WRITES"}


def save_plan(plan, path):
    target = Path(path)
    value = {"plan": plan, "plan_sha256": digest(plan)}
    if target.exists() and json.loads(target.read_text(encoding="utf-8")) != value:
        raise ValueError("RESEARCH_PLAN_DRIFT_USE_NEW_DIRECTORY")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(canonical(value), encoding="utf-8")


def freeze_window(plan, window, rules_manifest, output):
    """Export real closed bars without writing to the source database."""
    start, end = utc(window["start"]), utc(window["end"])
    split = next(p for p in plan["partitions"] if p["id"] == window["partition"])
    if not utc(split["start"]) <= start < end <= utc(split["end"]):
        raise ValueError("RESEARCH_WINDOW_CROSSES_PARTITION")
    if (end - start) > timedelta(days=2) or start.minute % 15 or start.second or start.microsecond:
        raise ValueError("RESEARCH_BOUNDED_ALIGNED_WINDOW_REQUIRED")
    rules = json.loads(Path(rules_manifest).read_text(encoding="utf-8"))
    if rules.get("manifest_sha256") != manifest_hash(rules):
        raise ValueError("RESEARCH_FROZEN_RULES_CORRUPT")
    contracts = {s: deepcopy(rules["contracts"][s]) for s in plan["symbols"]}
    first = start - timedelta(days=10)
    bars, funding, coverage = [], [], {}
    db_path = Path(plan["archive_database"])
    observed = datetime.now(timezone.utc)
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.row_factory = sqlite3.Row
        for symbol in plan["symbols"]:
            records = [dict(r) for r in db.execute(
                "SELECT * FROM bars WHERE symbol=? AND open_time_ms>=? AND open_time_ms<? ORDER BY open_time_ms",
                (symbol, int(first.timestamp() * 1000), int(end.timestamp() * 1000)))]
            expected = int((end - first).total_seconds() / 60)
            if (len(records) != expected or not records or
                    records[0]["open_time_ms"] != int(first.timestamp() * 1000) or
                    any(b["open_time_ms"] - a["open_time_ms"] != 60000 for a, b in zip(records, records[1:]))):
                raise ValueError("RESEARCH_MINUTE_HISTORY_GAP:" + symbol)
            contract = contracts[symbol]
            for frame, width in FRAME_MINUTES.items():
                groups = defaultdict(list)
                for record in records:
                    bucket = record["open_time_ms"] // (width * 60000) * (width * 60000)
                    groups[bucket].append(record)
                count = 0
                for stamp, group in sorted(groups.items()):
                    if len(group) != width:
                        continue
                    volume = sum(r["volume"] for r in group)
                    raw = {"t": stamp // 1000, "o": group[0]["open"], "h": max(r["high"] for r in group),
                           "l": min(r["low"] for r in group), "c": group[-1]["close"],
                           "v": volume / contract["contract_size"]}
                    bar = normalize_bar(symbol, frame, raw, observed, contract)
                    bar.update(venue="binance", provider="binance", source="binance_official_monthly_archive",
                               instrument_key=f"binance:perpetual:{symbol}:USDT:last", native_symbol=symbol,
                               base_volume=volume, volume_unit="GATE_CONTRACTS_CONVERTED_FROM_BINANCE_BASE_VOLUME",
                               execution_rules_basis="FROZEN_GATE_RULES_PROXY_NOT_BINANCE_EXECUTION",
                               payload_json={"provider": "binance", "synthetic": False,
                                             "source": "binance_official_monthly_archive"})
                    bars.append(bar)
                    count += 1
                coverage[f"{symbol}:{frame}"] = {"count": count, "expected": expected // width,
                                                   "complete": count == expected // width}
            for item in db.execute("SELECT * FROM funding WHERE symbol=? AND payment_time_ms>? AND payment_time_ms<=?",
                                   (symbol, int((start - timedelta(days=2)).timestamp() * 1000), int(end.timestamp() * 1000))):
                at = datetime.fromtimestamp(item["payment_time_ms"] / 1000, timezone.utc).isoformat()
                funding.append({"symbol": symbol, "instrument_id": symbol, "timestamp": at, "payment_time": at,
                                "rate": item["rate"], "source": "binance_official_funding_archive"})
    points = [start + timedelta(minutes=5 * i) for i in range(int((end - start).total_seconds() / 300))]
    payload = {"schema_version": "ai_template_history_v1", "symbols": plan["symbols"],
               "window_start": start.isoformat(), "window_end": end.isoformat(), "bars": bars,
               "contracts": contracts, "funding": funding, "news": [],
               "decision_points": [p.isoformat() for p in points], "coverage": coverage,
               "complete_data": all(c["complete"] for c in coverage.values()),
               "research_plan_sha256": digest(plan), "source_dataset_sha256": plan["dataset_sha256"],
               "rules_manifest_sha256": rules["manifest_sha256"], "partition": window["partition"],
               "assumptions": {"data_venue": "binance", "execution_rules": "FROZEN_CURRENT_GATE_RULES_CROSS_VENUE_PROXY",
                   "funding": "ACTUAL_BINANCE_CALCULATION_TIMES", "bar_availability": "KNOWN_AT_CLOSE_ASSUMPTION",
                   "news": "UNAVAILABLE_TECHNICAL_ONLY", "open_interest": "UNKNOWN", "order_book": "UNKNOWN",
                   "selection": plan["selection"], "liquidation": "BASE_MAINTENANCE_RATE_PROXY_NOT_HISTORICAL_TIERS"}}
    payload["manifest_sha256"] = manifest_hash(payload)
    target = Path(output)
    if target.exists():
        old = json.loads(target.read_text(encoding="utf-8"))
        if old.get("manifest_sha256") != payload["manifest_sha256"]:
            # fetched_at is intentionally bound on first export; do not regenerate.
            if (old.get("source_dataset_sha256") != plan["dataset_sha256"] or
                    old.get("research_plan_sha256") != digest(plan)
                    or old.get("rules_manifest_sha256") != rules["manifest_sha256"]
                    or old.get("window_start") != start.isoformat() or old.get("window_end") != end.isoformat()
                    or old.get("partition") != window["partition"]
                    or old.get("manifest_sha256") != manifest_hash(old)):
                raise ValueError("RESEARCH_WINDOW_DRIFT_USE_NEW_RUN")
            return old
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(canonical(payload), encoding="utf-8")
    return payload
