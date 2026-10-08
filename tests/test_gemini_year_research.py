"""Explicit offline fixtures; no real data/model/performance acceptance claims."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from core.replay.ai_history import ReplayHistory, manifest_hash, normalize_contract
from core.replay.gemini_research import add_months, file_sha256, freeze_window, research_plan, save_plan
from core.model_routing import DEFAULT_MODEL


def fixture_archive(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    start = datetime(2025, 10, 1, tzinfo=timezone.utc)
    window = {"id": "fixture", "start": start.isoformat(), "end": (start + timedelta(minutes=30)).isoformat(),
              "partition": "optimization"}
    with sqlite3.connect(root / "research.sqlite3") as db:
        db.execute("CREATE TABLE bars(symbol, open_time_ms, open, high, low, close, volume)")
        db.execute("CREATE TABLE funding(symbol, payment_time_ms, interval_hours, rate)")
        first = start - timedelta(days=10)
        db.executemany("INSERT INTO bars VALUES(?,?,?,?,?,?,?)", [("ETHUSDT", int((first + timedelta(minutes=i)).timestamp()*1000),
            100, 101, 99, 100, 12) for i in range(14430)])
        db.execute("INSERT INTO funding VALUES(?,?,8,0.001)", ("ETHUSDT", int((start + timedelta(minutes=15)).timestamp()*1000)))
    manifest = {"complete_data": True, "dataset_sha256": file_sha256(root / "research.sqlite3"),
                "window_start": start.isoformat(), "window_end": add_months(start, 12).isoformat(), "symbols": ["ETHUSDT"]}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    contract = normalize_contract("ETHUSDT", {"quanto_multiplier": "0.01", "order_size_min": "1",
        "order_size_max": "100000", "order_price_round": "0.01", "leverage_max": "100"})
    rules = {"contracts": {"ETHUSDT": contract}, "fixture": "UNIT_TEST_NOT_ACTUAL_MARKET_DATA"}
    rules["manifest_sha256"] = manifest_hash(rules)
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(rules), encoding="utf-8")
    return root, window, path


def test_month_boundaries_and_frozen_split(tmp_path):
    assert add_months(datetime(2025, 1, 31, tzinfo=timezone.utc), 1).day == 28
    archive, _, _ = fixture_archive(tmp_path)
    plan = research_plan(archive)
    assert plan["model_id"] == DEFAULT_MODEL and plan["model_weights_trained"] is False
    assert plan["expected_pilot_decisions"] == 1008
    assert plan["heldout_protocol"]["window_hours"] == 48
    assert plan["heldout_protocol"]["calendar_offsets_days"] == [14, 44, 74]
    assert plan["heldout_protocol"]["minimum_closed_trades_per_strategy"] == 30
    assert [p["start"] for p in plan["partitions"]] == ["2025-10-01T00:00:00+00:00", "2026-04-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"]
    path = tmp_path / "plan.json"
    save_plan(plan, path)
    save_plan(plan, path)
    with pytest.raises(ValueError, match="PLAN_DRIFT"):
        save_plan({**plan, "model_id": "wrong"}, path)


def test_reject_changed_source_database(tmp_path):
    archive, _, _ = fixture_archive(tmp_path)
    with sqlite3.connect(archive / "research.sqlite3") as db:
        db.execute("UPDATE bars SET close=101 WHERE rowid=1")
    with pytest.raises(ValueError, match="NOT_VERIFIED"):
        research_plan(archive)


def test_registered_application_budget_prevents_resume_with_changed_capacity(tmp_path, monkeypatch):
    archive, _, _ = fixture_archive(tmp_path)
    monkeypatch.setenv('AIMA_MODEL_INPUT_BUDGET', '12288')
    first = research_plan(archive)
    assert first['application_input_budget'] == 12288
    target = tmp_path/'registered.json'
    save_plan(first, target)
    monkeypatch.setenv('AIMA_MODEL_INPUT_BUDGET', '8192')
    second = research_plan(archive)
    assert second['application_input_budget'] == 8192
    assert second['pilot_windows'] == first['pilot_windows']
    assert second['templates'] == first['templates']
    with pytest.raises(ValueError, match='PLAN_DRIFT'):
        save_plan(second, target)


def test_real_source_projection_is_causal_and_has_honest_venue(tmp_path):
    archive, window, rules = fixture_archive(tmp_path)
    before = file_sha256(archive / "research.sqlite3")
    plan = research_plan(archive)
    payload = freeze_window(plan, window, rules, tmp_path / "history.json")
    assert payload["complete_data"] is True
    assert file_sha256(archive / "research.sqlite3") == before
    data = ReplayHistory(payload)
    data.set_as_of(data.window_start)
    bars = data.latest_bars("ETHUSDT", "5m")
    assert len(bars) == 240 and all(b["bar_end"] <= window["start"] for b in bars)
    assert bars[-1]["base_volume"] == 60 and bars[-1]["volume"] == 6000
    assert bars[-1]["provider"] == "binance"
    assert data.market_snapshot("ETHUSDT", data.window_start)["source"] == "binance_historical_closed_bar"
    assert payload["funding"][0]["source"] == "binance_official_funding_archive"
    assert payload["news"] == []
    assert freeze_window(plan, window, rules, tmp_path / "history.json") == payload


def test_reject_partition_leak_and_incomplete_minute_history(tmp_path):
    archive, window, rules = fixture_archive(tmp_path)
    plan = research_plan(archive)
    with pytest.raises(ValueError, match="CROSSES_PARTITION"):
        freeze_window(plan, {**window, "start": "2026-03-31T23:45:00+00:00", "end": "2026-04-01T00:15:00+00:00"}, rules, tmp_path / "bad.json")
    with sqlite3.connect(archive / "research.sqlite3") as db:
        db.execute("DELETE FROM bars WHERE rowid=10")
    with pytest.raises(ValueError, match="HISTORY_GAP"):
        freeze_window(plan, window, rules, tmp_path / "bad.json")


def test_cached_window_cannot_hide_changed_contract_rules(tmp_path):
    archive, window, rules = fixture_archive(tmp_path)
    plan = research_plan(archive)
    target = tmp_path / "history.json"
    freeze_window(plan, window, rules, target)
    changed = json.loads(rules.read_text(encoding="utf-8"))
    changed["contracts"]["ETHUSDT"]["contract_size"] = 0.1
    changed["manifest_sha256"] = manifest_hash(changed)
    rules.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="WINDOW_DRIFT"):
        freeze_window(plan, window, rules, target)


def test_research_cli_imports_without_starting_experiment():
    import runpy
    exports = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/run_gemini_year_research.py"))
    assert callable(exports["main"]) and callable(exports["write_cases"])


def test_research_price_structure_accepts_archive_and_production_stays_gate_only(tmp_path):
    from core.replay.gemini_research import research_price_action
    from core.trading.price_action_structure import build_price_action_structure
    archive, window, rules = fixture_archive(tmp_path)
    data = ReplayHistory(freeze_window(research_plan(archive), window, rules, tmp_path / "history.json"))
    rows = data.latest_bars("ETHUSDT", "15m")
    result = research_price_action(rows, "15m", data.window_start)
    assert result["status"] == "READY" and "RESEARCH_ONLY" in result["source_scope"]
    assert build_price_action_structure(rows, "15m", data.window_start)["status"] == "UNAVAILABLE"
    for row in rows:
        row["provider"] = "unknown"
    assert research_price_action(rows, "15m", data.window_start)["status"] == "UNAVAILABLE"
