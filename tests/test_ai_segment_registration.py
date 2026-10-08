"""Research registration guards; fixtures never contact the real model or Gate."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

import scripts.continue_ai_validation_segments as runner


@pytest.fixture
def registration(monkeypatch):
    points = [datetime(2026, 9, 28, tzinfo=timezone.utc) + timedelta(minutes=5 * i) for i in range(24)]
    windows = [{"id": key, "start": points[0].isoformat(),
                "end": (points[-1] + timedelta(minutes=5)).isoformat()} for key in ("a", "b", "c")]
    plan = {"windows": windows, "symbols": ["BTCUSDT", "ETHUSDT"],
        "templates": [{"execution": {"scan_interval_minutes": n}} for n in (5, 15, 15, 15, 15)],
        "expected_calls_per_window": 56, "expected_total_calls": 168}
    frozen = {"plan_sha256": "plan", "segments": [{"id": key, "history_path": key,
        "manifest_sha256": key} for key in ("a", "b", "c")]}
    histories = {key: SimpleNamespace(payload={"manifest_sha256": key,
        "validation_plan_sha256": "plan", "window_start": windows[0]["start"],
        "window_end": windows[0]["end"], "complete_data": True},
        symbols=("BTCUSDT", "ETHUSDT"), decision_points=points) for key in ("a", "b", "c")}
    monkeypatch.setattr(runner, "ReplayHistory", lambda path: histories[str(path)])
    return plan, frozen, histories


def test_three_windows_are_bound_to_registered_native_schedule(registration):
    plan, frozen, _ = registration
    assert len(runner.validate_segments(plan, frozen, "plan")) == 3


@pytest.mark.parametrize("ids", [[], ["a"], ["a", "b"], ["a", "a", "c"], ["c", "b", "a"], ["a", "b", "c", "c"]])
def test_missing_duplicate_or_reordered_segments_cannot_qualify(registration, ids):
    plan, frozen, _ = registration
    lookup = {s["id"]: s for s in frozen["segments"]}
    frozen["segments"] = [deepcopy(lookup[key]) for key in ids]
    with pytest.raises(ValueError, match="WINDOWS_MISSING_DUPLICATE_OR_REORDERED"):
        runner.validate_segments(plan, frozen, "plan")


@pytest.mark.parametrize("field,value", [("window_start", "2026-09-29T00:00:00+00:00"),
    ("window_end", "2026-09-29T02:00:00+00:00"), ("validation_plan_sha256", "wrong"),
    ("manifest_sha256", "wrong"), ("complete_data", False)])
def test_window_dates_source_hash_and_coverage_cannot_drift(registration, field, value):
    plan, frozen, histories = registration
    histories["b"].payload[field] = value
    with pytest.raises(ValueError, match="HISTORY_DOES_NOT_MATCH"):
        runner.validate_segments(plan, frozen, "plan")


def test_omitting_a_symbol_or_native_scan_cannot_qualify(registration):
    plan, frozen, histories = registration
    histories["b"].symbols = ("BTCUSDT",)
    with pytest.raises(ValueError, match="HISTORY_DOES_NOT_MATCH"):
        runner.validate_segments(plan, frozen, "plan")
    histories["b"].symbols = ("BTCUSDT", "ETHUSDT")
    histories["b"].decision_points = histories["b"].decision_points[:-1]
    with pytest.raises(ValueError, match="NATIVE_SCAN_COUNT"):
        runner.validate_segments(plan, frozen, "plan")


def test_declared_total_must_equal_all_window_scans(registration):
    plan, frozen, _ = registration
    plan["expected_total_calls"] = 1
    with pytest.raises(ValueError, match="TOTAL_NATIVE_SCAN_COUNT"):
        runner.validate_segments(plan, frozen, "plan")


def test_model_identity_is_bound_across_windows(tmp_path):
    database = tmp_path / "isolated.sqlite3"
    pin_path = tmp_path / "model-pin.json"
    pin = {"actual_model_id": "TEST_FIXTURE", "weight_digest": "a" * 64, "context_length": 8192}
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE ai_template_replay_runs(config_json TEXT)")
        connection.execute("INSERT INTO ai_template_replay_runs VALUES (?)", (json.dumps({"model_pin": pin}),))
    assert runner.bind_model_pin(database, pin_path) == pin
    assert runner.bind_model_pin(database, pin_path) == pin
    for key, changed in (("actual_model_id", "DIFFERENT_TEST_FIXTURE"), ("weight_digest", "b" * 64), ("context_length", 4096)):
        other = {**pin, key: changed}
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE ai_template_replay_runs SET config_json=?", (json.dumps({"model_pin": other}),))
        with pytest.raises(ValueError, match="CROSS_WINDOW_MODEL_IDENTITY_DRIFT"):
            runner.bind_model_pin(database, pin_path)


def test_os_lock_prevents_second_driver_and_allows_later_resume(tmp_path):
    with runner.research_driver_lock(tmp_path):
        with pytest.raises(RuntimeError, match="DRIVER_ALREADY_RUNNING"):
            with runner.research_driver_lock(tmp_path):
                pytest.fail("Second driver acquired the lock")
    assert (tmp_path / ".driver.lock").exists()
    with runner.research_driver_lock(tmp_path):
        pass  # A stale file is not a live process lock.
