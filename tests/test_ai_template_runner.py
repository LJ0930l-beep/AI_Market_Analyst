from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from core.replay.ai_history import ReplayHistory, manifest_hash, normalize_bar, normalize_contract
from core.replay import ai_template_runner as runner
from core.trading.ai_led_engine import AIActionOutput


START = datetime(2026, 10, 2, tzinfo=timezone.utc)


def history(minutes=30, future_price=None):
    """Explicit unit fixture; never presented as real acceptance market data."""
    contract = normalize_contract("ETHUSDT", {"quanto_multiplier": "0.01", "order_price_round": "0.01",
        "order_size_min": "0.1", "order_size_max": "100000", "leverage_max": "200", "enable_decimal": True})
    bars = []
    for frame, width in (("1m", 1), ("5m", 5), ("15m", 15), ("1h", 60)):
        for offset in range(-240, (minutes + width - 1) // width):
            start = START + timedelta(minutes=offset * width)
            price = future_price if future_price is not None and start >= START + timedelta(minutes=5) else 100
            bars.append(normalize_bar("ETHUSDT", frame, {"t": int(start.timestamp()),
                "o": str(price), "h": str(price + 1), "l": str(price - 1), "c": str(price), "v": "1000000"},
                START + timedelta(days=1), contract))
    payload = {"schema_version": "ai_template_history_v1", "symbols": ["ETHUSDT"],
        "window_start": START.isoformat(), "window_end": (START + timedelta(minutes=minutes)).isoformat(),
        "decision_points": [(START + timedelta(minutes=i)).isoformat() for i in range(0, minutes, 5)],
        "bars": bars, "news": [], "contracts": {"ETHUSDT": contract}, "funding": [],
        "complete_data": True, "assumptions": {"fixture": "UNIT_TEST_ONLY_NOT_REAL_MARKET_DATA"}}
    payload["manifest_sha256"] = manifest_hash(payload)
    return ReplayHistory(payload)


def wait(context):
    return AIActionOutput(action="WAIT", instrument_id="ETHUSDT", reason="单测：等待有效触发条件")


def rows(path):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return db.execute("SELECT * FROM ai_template_replay_decisions ORDER BY as_of,template_id").fetchall()


def test_all_five_native_cadence_and_zero_trades_are_not_wins(tmp_path):
    seen = []

    def decide(context):
        seen.append((context.strategy_instructions["template_id"], context.started_at, context.account_id))
        assert context.account_truth["source"] == "AI isolated historical simulation"
        assert context.market_radar_snapshot["status"] == "UNAVAILABLE"
        assert context.market_snapshots["ETHUSDT"]["liquidity_ok"] is None
        assert context.market_snapshots["ETHUSDT"]["market"]["taker"] == .00075
        assert context.performance_context["source"] == "AI_REPLAY_SIMULATION"
        assert context.performance_context["research_only"] is True
        assert context.performance_context["closed_trade_count"] == 0 and context.performance_context["win_rate"] is None
        assert all(item["bar_end"] <= context.started_at for item in context.technical_context.get("bars", []))
        return wait(context)

    data = history()
    result = runner.run_ai_template_replay(data, db_path=tmp_path / "replay.sqlite3", model_decider=decide)
    intervals = {s["template_id"]: s["execution"]["scan_interval_minutes"] for s in runner.frozen_templates()}
    counts = Counter(template for template, _, _ in seen)
    assert counts == {key: 30 // interval for key, interval in intervals.items()}
    assert {template for template, at, _ in seen if at == START.isoformat()} == set(runner.TEMPLATE_IDS)
    assert len({account for _, _, account in seen}) == 5
    assert result["status"] == "COMPLETED" and result["complete_window"] is True
    assert result["manifest_sha256"] == data.payload["manifest_sha256"]
    assert result["comparison_eligible"] is False and result["private_exchange_calls"] == 0
    assert result["decision_source"] == "TEST_INJECTED"
    assert result["configuration_scope"] == "FROZEN_BUILTIN_TEMPLATES_WITH_DEFAULTS"
    assert result["common_initial_equity_usdt"] == 1000 and result["user_live_account_returns"] is False
    for item in result["results"]:
        assert item["initial_equity"] == item["ending_equity"] == 1000
        assert item["closed_trade_count"] == 0 and item["win_rate"] is None
        assert item["action_counts"] == {"WAIT": counts[item["template_id"]]}


def test_limit_is_resumable_and_does_not_drop_waits_or_duplicate_calls(tmp_path):
    calls = []

    def decide(context):
        calls.append(context.cycle_id)
        return wait(context)

    path = tmp_path / "replay.sqlite3"
    first = runner.run_ai_template_replay(history(), db_path=path, model_decider=decide, max_decisions=5)
    assert first["status"] == "PAUSED" and first["pause_reason"] == "PILOT_DECISION_LIMIT"
    assert first["decision_count"] == 5 and first["complete_window"] is False
    assert {row["template_id"] for row in rows(path)} == set(runner.TEMPLATE_IDS)
    final = runner.run_ai_template_replay(history(), db_path=path, model_decider=decide, resume=True)
    assert final["status"] == "COMPLETED" and len(calls) == len(set(calls)) == final["decision_count"]
    again = runner.run_ai_template_replay(history(), db_path=path, model_decider=decide, resume=True)
    assert again["decision_count"] == len(calls)
    assert again["results"] == final["results"]


def test_live_priority_pause_has_no_model_claim_or_call(tmp_path):
    called = []

    def busy():
        raise runner.ReplayPaused("PRODUCTION_NEXT_SCAN_HAS_PRIORITY")

    path = tmp_path / "replay.sqlite3"
    result = runner.run_ai_template_replay(history(), db_path=path, model_decider=lambda c: called.append(c), priority_guard=busy)
    assert result["pause_reason"] == "PRODUCTION_NEXT_SCAN_HAS_PRIORITY"
    assert not called and not rows(path)


def test_ambiguous_interrupted_call_is_never_called_again(tmp_path):
    path = tmp_path / "replay.sqlite3"

    def interrupted(context):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        runner.run_ai_template_replay(history(), db_path=path, model_decider=interrupted)
    assert rows(path)[0]["status"] == "MODEL_STARTED"
    result = runner.run_ai_template_replay(history(), db_path=path, model_decider=lambda c: pytest.fail("duplicate model call"), resume=True)
    assert result["status"] == "INTERRUPTED" and result["pause_reason"] == "MODEL_RESULT_AMBIGUOUS_DO_NOT_RECALL"


def test_saved_model_result_recovers_after_execution_failure_without_recall(tmp_path, monkeypatch):
    path = tmp_path / "replay.sqlite3"
    calls = []
    actual = runner._ReplayEngine.execute_cycle

    def fail_execution(*args, **kwargs):
        raise RuntimeError("execution checkpoint crash fixture")

    monkeypatch.setattr(runner._ReplayEngine, "execute_cycle", fail_execution)
    with pytest.raises(RuntimeError, match="checkpoint crash"):
        runner.run_ai_template_replay(history(5), db_path=path, model_decider=lambda c: (calls.append(c.cycle_id), wait(c))[1])
    assert rows(path)[0]["status"] == "MODEL_DONE"
    monkeypatch.setattr(runner._ReplayEngine, "execute_cycle", actual)
    final = runner.run_ai_template_replay(history(5), db_path=path, model_decider=lambda c: (calls.append(c.cycle_id), wait(c))[1], resume=True)
    assert final["status"] == "COMPLETED" and len(calls) == len(set(calls)) == 5


def test_model_errors_are_counted_and_never_retried(tmp_path):
    path = tmp_path / "replay.sqlite3"
    calls = []

    def broken(context):
        calls.append(context.cycle_id)
        raise ValueError("invalid production schema fixture")

    result = runner.run_ai_template_replay(history(5), db_path=path, model_decider=broken)
    assert result["decision_count"] == len(result["errors"]) == len(calls) == 5
    assert result["comparison_eligible"] is False
    runner.run_ai_template_replay(history(5), db_path=path, model_decider=broken, resume=True)
    assert len(calls) == 5


def test_refuses_other_database_and_history_mismatch(tmp_path):
    path = tmp_path / "production.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE production_secret(value)")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="NON_REPLAY"):
        runner.run_ai_template_replay(history(), db_path=path, model_decider=wait, resume=True)
    assert path.read_bytes() == before
    replay = tmp_path / "replay.sqlite3"
    runner.run_ai_template_replay(history(5), db_path=replay, model_decider=wait)
    with pytest.raises(ValueError, match="MISMATCH"):
        runner.run_ai_template_replay(history(10), db_path=replay, model_decider=wait, resume=True)


@pytest.mark.parametrize("state,reason", [
    ({"state": "RUNNING", "enabled": True, "schedule": {"last_started_at": START.isoformat()}}, "CYCLE_IN_PROGRESS"),
    ({"state": "RUNNING", "enabled": True, "schedule": {"next_scan_at": (START + timedelta(seconds=89)).isoformat()}}, "NEXT_SCAN"),
    ({}, "STATUS_UNAVAILABLE"),
])
def test_priority_guard_fails_closed(state, reason):
    idle, code = runner.production_model_idle({"ai_session": state}, budget_seconds=70, now=START)
    assert idle is False and reason in code


def test_priority_guard_safe_gap_and_url():
    idle, _ = runner.production_model_idle({"ai_session": {"state": "RUNNING", "enabled": True,
        "schedule": {"last_started_at": (START - timedelta(minutes=3)).isoformat(),
                     "last_completed_at": (START - timedelta(minutes=2)).isoformat(),
                     "next_scan_at": (START + timedelta(minutes=2)).isoformat()}}}, budget_seconds=70, now=START)
    assert idle is True
    with pytest.raises(ValueError, match="MUST_BE_LOCAL"):
        runner.runtime_priority_guard("http://secret@example.org/status", budget_seconds=70)


class FixtureProvider:
    """Pure local fake, recorded by runner as TEST_PROVIDER, never acceptance."""
    model_id = runner.DEFAULT_SMART_MODEL
    model_version = runner.DEFAULT_SMART_MODEL
    context_length = 16384
    max_tokens = 512

    def __init__(self, invalid_ref=False):
        self.invalid_ref = invalid_ref
        self.payloads = []
        self.raw_responses = []

    def generate_json(self, messages, **options):
        payload = json.loads(messages[1]["content"])
        self.payloads.append(payload)
        assert options["schema"]["properties"]["instrument_id"]["enum"] == ["ETHUSDT"]
        refs = [ref for ref in payload["evidence_refs"] if ref.startswith(("market_snapshot:", "technical_snapshot:"))]
        decision = {"action": "OPEN_LONG", "instrument_id": "ETHUSDT", "reason": "单测账户连续开仓验证",
            "confidence": 80, "entry_price": 100, "stop_price": 90, "take_profit": 200,
            "position_size_usdt": 2000, "requested_leverage": 40, "order_preference": "MARKET",
            "evidence_refs": ["future_or_invented_evidence"] if self.invalid_ref else refs}
        raw = json.dumps(decision, ensure_ascii=False)
        self.raw_responses.append(raw)
        artifact = runner.DEFAULT_SMART_MODEL
        return decision, raw, {"model_id": runner.DEFAULT_SMART_MODEL, "model_version": artifact,
            "actual_model_id": artifact, "model_identity_source": "completion_response",
            "verified_manifest_model_id": artifact}


def test_production_prompt_schema_receipt_and_engine_route_are_isolated(tmp_path, monkeypatch):
    from core.model_client import model_client
    from core.trading import ai_session_coordinator
    monkeypatch.setattr(model_client, "count_tokens", lambda content, **kwargs: max(1, len(content) // 3))
    monkeypatch.setattr(ai_session_coordinator, "_load_market_radar_snapshot", lambda *args: pytest.fail("present-day radar must not be read"))
    provider = FixtureProvider()
    path = tmp_path / "replay.sqlite3"
    result = runner.run_ai_template_replay(history(5), db_path=path, model_provider=provider, priority_guard=lambda: None)
    assert result["status"] == "COMPLETED" and result["errors"] == []
    assert result["decision_source"] == "TEST_PROVIDER" and result["comparison_eligible"] is False
    assert len(provider.payloads) == 5
    assert {payload["active_strategy"]["template_id"] for payload in provider.payloads} == set(runner.TEMPLATE_IDS)
    assert all(payload["market_radar"]["status"] == "UNAVAILABLE" for payload in provider.payloads)
    assert all(payload["dynamic_risk"]["policy"] == "MARGIN_ONLY" for payload in provider.payloads)
    by_hash = {hashlib.sha256(raw.encode()).hexdigest() for raw in provider.raw_responses}
    for row in rows(path):
        assert row["response_sha256"] in by_hash
        context = json.loads(row["context_json"])
        assert context["model_call_attempted"] and context["model_call_completed"]
        assert context["evidence_bundle_id"] and context["input_hash"]
        receipt = json.loads(row["result_json"])
        assert receipt["status"] == "SUBMITTED" and receipt["events"][0]["status"] == "ACCEPTED"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM evidence_bundles").fetchone()[0] == 5
        assert db.execute("SELECT COUNT(*) FROM ai_led_cycles").fetchone()[0] == 5
        assert db.execute("SELECT COUNT(*) FROM ai_cycle_stages").fetchone()[0] >= 5
    for summary in result["results"]:
        assert summary["ending_equity"] < 1000  # Entry slippage and fee persist in the continuous account.
        assert summary["fills"] == 1 and summary["closed_trade_count"] == 0 and summary["win_rate"] is None


def test_production_schema_rejects_invented_references_before_any_order(tmp_path, monkeypatch):
    from core.model_client import model_client
    monkeypatch.setattr(model_client, "count_tokens", lambda content, **kwargs: max(1, len(content) // 3))
    provider = FixtureProvider(invalid_ref=True)
    result = runner.run_ai_template_replay(history(5), db_path=tmp_path / "replay.sqlite3", model_provider=provider, priority_guard=lambda: None)
    assert len(result["errors"]) == len(provider.payloads) == 5
    assert all("SCHEMA" in item["error"] or "EVIDENCE" in item["error"] for item in result["errors"])
    assert all(item["fills"] == 0 and item["ending_equity"] == 1000 for item in result["results"])


def test_stage_trace_observes_committed_cycle_from_another_connection(tmp_path, monkeypatch):
    from core.trading import ai_led_engine
    real_persist = ai_led_engine.persist_stage_trace
    observed = []

    def persist_after_commit(store, *, cycle_id, account_id, trace):
        with sqlite3.connect(store.path) as external:
            row = external.execute("SELECT cycle_id FROM ai_led_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
        assert row == (cycle_id,)
        observed.append(cycle_id)
        return real_persist(store, cycle_id=cycle_id, account_id=account_id, trace=trace)

    monkeypatch.setattr(ai_led_engine, "persist_stage_trace", persist_after_commit)
    result = runner.run_ai_template_replay(history(5), db_path=tmp_path / "replay.sqlite3", model_decider=wait)
    assert result["status"] == "COMPLETED" and len(observed) == 5


def test_production_prompt_manages_positions_across_scans(tmp_path, monkeypatch):
    from core.model_client import model_client
    monkeypatch.setattr(model_client, "count_tokens", lambda content, **kwargs: max(1, len(content) // 3))

    class ManagingProvider(FixtureProvider):
        def __init__(self):
            super().__init__()
            self.scans = Counter()
            self.actions = Counter()

        def generate_json(self, messages, **options):
            payload = json.loads(messages[1]["content"])
            template = payload["active_strategy"]["template_id"]
            scan = self.scans[template]
            self.scans[template] += 1
            if scan == 0:
                return super().generate_json(messages, **options)
            truth = payload["account_truth"]
            if scan in (1, 2):
                assert len(truth["managed_positions"]) == 1
                position = truth["managed_positions"][0]
                assert position["ownership"] == "VERIFIED_SYSTEM"
                assert truth["external_or_unverified_positions"] == []
                decision = {"action": "UPDATE_PROTECTION" if scan == 1 else "CLOSE_POSITION",
                    "instrument_id": "ETHUSDT", "position_id": position["position_id"],
                    "reason": "单测账户后续管理验证", "confidence": None}
                if scan == 1:
                    decision["new_stop_price"] = 95
                self.actions[decision["action"]] += 1
            else:
                decision = {"action": "WAIT", "instrument_id": "ETHUSDT", "reason": "单测平仓后等待", "confidence": None,
                    "entry_condition": "等待测试中的下一次有效触发", "strategy_analysis": {"missing_conditions": ["尚无新入场触发"]}}
            return decision, json.dumps(decision), {"model_id": runner.DEFAULT_SMART_MODEL}

    provider = ManagingProvider()
    result = runner.run_ai_template_replay(history(45, future_price=110), db_path=tmp_path / "replay.sqlite3",
        model_provider=provider, priority_guard=lambda: None)
    assert result["errors"] == []
    assert provider.actions == {"UPDATE_PROTECTION": 5, "CLOSE_POSITION": 5}
    for item in result["results"]:
        assert item["closed_trade_count"] == 1 and item["win_rate"] == 1
        assert item["ending_equity"] > 1000 and item["fees"] > 0


def test_production_prompt_manages_pending_entry_and_cancels_it(tmp_path, monkeypatch):
    from core.model_client import model_client
    monkeypatch.setattr(model_client, "count_tokens", lambda content, **kwargs: max(1, len(content) // 3))

    class CancelingProvider(FixtureProvider):
        def generate_json(self, messages, **options):
            payload = json.loads(messages[1]["content"])
            entries = payload["account_truth"].get("owned_entry_orders") or []
            if entries:
                assert entries[0]["ownership"] == "SYSTEM_ORDER_ID_MATCH"
                decision = {"action": "CANCEL_ORDER", "instrument_id": "ETHUSDT", "order_id": entries[0]["order_id"],
                            "reason": "单测撤销隔离账户挂单", "confidence": None}
                return decision, json.dumps(decision), {"model_id": runner.DEFAULT_SMART_MODEL}
            decision, _, metadata = super().generate_json(messages, **options)
            decision.update(order_preference="LIMIT", entry_price=95, limit_price=95, stop_price=90, ttl_seconds=1800)
            return decision, json.dumps(decision), metadata

    result = runner.run_ai_template_replay(history(20), db_path=tmp_path / "replay.sqlite3",
        model_provider=CancelingProvider(), priority_guard=lambda: None)
    assert result["errors"] == []
    assert all(item["action_counts"].get("CANCEL_ORDER", 0) >= 1 for item in result["results"])
    assert all(item["fills"] == 0 and item["win_rate"] is None for item in result["results"])


def test_resume_rejects_a_changed_source_fingerprint(tmp_path, monkeypatch):
    path = tmp_path / "replay.sqlite3"
    original = runner.frozen_source_fingerprint()
    runner.run_ai_template_replay(history(5), db_path=path, model_decider=wait, max_decisions=1)
    changed = dict(original)
    changed["core/trading/ai_led_engine.py"] = "0" * 64
    monkeypatch.setattr(runner, "frozen_source_fingerprint", lambda: changed)
    with pytest.raises(ValueError, match="MISMATCH"):
        runner.run_ai_template_replay(history(5), db_path=path, model_decider=lambda c: pytest.fail("changed code must not call model"), resume=True)


def test_replay_never_calls_production_training_collector_or_writes_app_dataset(tmp_path, monkeypatch):
    from core.model_client import model_client
    from core.trading import ai_session_coordinator
    from core.trading.fin_dataset_collector import DATASET_FILE
    monkeypatch.setattr(model_client, "count_tokens", lambda content, **kwargs: max(1, len(content) // 3))
    before = hashlib.sha256(DATASET_FILE.read_bytes()).hexdigest() if DATASET_FILE.exists() else None

    def forbidden_collector(**kwargs):
        pytest.fail("historical research must not write the production training dataset")

    monkeypatch.setattr(ai_session_coordinator, "record_sft_sample", forbidden_collector)
    result = runner.run_ai_template_replay(history(5), db_path=tmp_path / "replay.sqlite3",
        model_provider=FixtureProvider(), priority_guard=lambda: None)
    after = hashlib.sha256(DATASET_FILE.read_bytes()).hexdigest() if DATASET_FILE.exists() else None
    assert result["errors"] == [] and before == after
    assert result["production_sft_writes"] == 0


def _changed_history(data, change):
    payload = dict(data.payload)
    payload["bars"] = [dict(bar) for bar in data.payload["bars"]]
    change(payload)
    payload["manifest_sha256"] = manifest_hash(payload)
    return ReplayHistory(payload)


def test_complete_model_wall_latency_cannot_backfill_the_thinking_minute(tmp_path, monkeypatch):
    from core.model_client import model_client
    monkeypatch.setattr(model_client, "count_tokens", lambda content, **kwargs: max(1, len(content) // 3))
    monkeypatch.setattr(runner, "_model_wall_elapsed", lambda *args, **kwargs: 40.0)

    class LimitProvider(FixtureProvider):
        def generate_json(self, messages, **options):
            decision, _, metadata = super().generate_json(messages, **options)
            decision.update(order_preference="LIMIT", entry_price=95, limit_price=95, stop_price=90, ttl_seconds=1800)
            return decision, json.dumps(decision), metadata

    def cross_only_before_submit(payload):
        for bar in payload["bars"]:
            if bar["timeframe"] == "1m" and bar["bar_start"] == START.isoformat():
                bar["low"] = 94.0
                bar["high"] = 106.0
                bar["close"] = 105.0

    data = _changed_history(history(5), cross_only_before_submit)
    path = tmp_path / "replay.sqlite3"
    result = runner.run_ai_template_replay(data, db_path=path, model_provider=LimitProvider(), priority_guard=lambda: None)
    assert result["errors"] == []
    assert all(item["fills"] == 0 and item["pending_order_count"] == 1 for item in result["results"])
    for row in rows(path):
        context = json.loads(row["context_json"])
        receipt = json.loads(row["result_json"])
        assert row["wall_elapsed_seconds"] == 40
        assert row["submission_at"] == (START + timedelta(minutes=1)).isoformat()
        assert context["started_at"] == START.isoformat()
        assert context["market_snapshots"]["ETHUSDT"]["price"] == 100
        assert receipt["execution_market_snapshots"]["ETHUSDT"]["price"] == 105
        assert receipt["execution_account_truth"]["observed_at"] == row["submission_at"]
        assert receipt["events"][0]["event_time"] == row["submission_at"]


def test_sixty_five_second_valid_completion_is_not_expired_by_minute_rounding(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_model_wall_elapsed", lambda *args, **kwargs: 65.0)
    result = runner.run_ai_template_replay(history(5), db_path=tmp_path / "replay.sqlite3", model_decider=wait,
                                         model_budget_seconds=70)
    assert result["errors"] == []
    assert all(item["execution_counts"] == {"WAITING": 1} for item in result["results"])
    assert all(json.loads(row["result_json"])["submission_at"] == (START + timedelta(minutes=2)).isoformat()
               for row in rows(tmp_path / "replay.sqlite3"))


def test_model_wall_time_over_budget_is_discarded_before_any_gateway_event(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_model_wall_elapsed", lambda *args, **kwargs: 71.0)
    result = runner.run_ai_template_replay(history(5), db_path=tmp_path / "replay.sqlite3", model_decider=wait)
    assert len(result["errors"]) == 5
    assert all("DEADLINE_EXCEEDED" in error["error"] for error in result["errors"])
    assert all(item["fills"] == 0 for item in result["results"])


def test_saved_model_done_preserves_submission_clock_and_does_not_reinvoke(tmp_path, monkeypatch):
    path = tmp_path / "replay.sqlite3"
    calls = []
    actual = runner._ReplayEngine.execute_cycle
    monkeypatch.setattr(runner, "_model_wall_elapsed", lambda *args, **kwargs: 40.0)
    monkeypatch.setattr(runner._ReplayEngine, "execute_cycle", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("crash after MODEL_DONE")))
    with pytest.raises(RuntimeError, match="MODEL_DONE"):
        runner.run_ai_template_replay(history(5), db_path=path, model_decider=lambda context: (calls.append(context.cycle_id), wait(context))[1])
    saved = rows(path)[0]
    assert saved["status"] == "MODEL_DONE" and saved["wall_elapsed_seconds"] == 40
    assert saved["submission_at"] == (START + timedelta(minutes=1)).isoformat()
    monkeypatch.setattr(runner._ReplayEngine, "execute_cycle", actual)
    result = runner.run_ai_template_replay(history(5), db_path=path,
        model_decider=lambda context: pytest.fail("saved model result must not be invoked again"), resume=True, max_decisions=1)
    assert result["decision_count"] == len(calls) == 1
    assert rows(path)[0]["status"] == "COMPLETED"
    assert json.loads(rows(path)[0]["result_json"])["submission_at"] == saved["submission_at"]


def test_stale_model_management_cannot_update_protection_that_already_closed(tmp_path, monkeypatch):
    from core.model_client import model_client
    monkeypatch.setattr(model_client, "count_tokens", lambda content, **kwargs: max(1, len(content) // 3))
    monkeypatch.setattr(runner, "_model_wall_elapsed", lambda *args, **kwargs: 40.0)

    class ProtectionProvider(FixtureProvider):
        def __init__(self):
            super().__init__()
            self.seen_positions = []

        def generate_json(self, messages, **options):
            payload = json.loads(messages[1]["content"])
            positions = payload["account_truth"].get("managed_positions") or []
            if positions:
                if payload["account_truth"]["observed_at"] != (START + timedelta(minutes=15)).isoformat():
                    decision = {"action": "HOLD", "instrument_id": "ETHUSDT", "reason": "单测保留原保护直到十五分钟", "confidence": None}
                    return decision, json.dumps(decision), {"model_id": runner.DEFAULT_SMART_MODEL}
                self.seen_positions.append(positions[0]["position_id"])
                decision = {"action": "UPDATE_PROTECTION", "instrument_id": "ETHUSDT",
                    "position_id": positions[0]["position_id"], "new_stop_price": 80,
                    "reason": "单测模型思考中旧止损先触发", "confidence": None}
                return decision, json.dumps(decision), {"model_id": runner.DEFAULT_SMART_MODEL}
            return super().generate_json(messages, **options)

    def cross_at_second_scan(payload):
        for bar in payload["bars"]:
            if bar["timeframe"] == "1m" and bar["bar_start"] == (START + timedelta(minutes=15)).isoformat():
                bar["low"] = 85.0

    path = tmp_path / "replay.sqlite3"
    provider = ProtectionProvider()
    result = runner.run_ai_template_replay(_changed_history(history(20), cross_at_second_scan), db_path=path,
                                         model_provider=provider, priority_guard=lambda: None)
    management = [json.loads(row["result_json"]) for row in rows(path)
                  if json.loads(row["decision_json"] or "{}").get("action") == "UPDATE_PROTECTION"]
    assert len(management) >= 5 and len(provider.seen_positions) >= 5
    assert all(receipt["execution_account_truth"]["positions"] == [] for receipt in management)
    assert all(receipt["status"] == "REJECTED" and not receipt["events"] for receipt in management)
    assert all(item["closed_trade_count"] >= 1 for item in result["results"])


def test_account_specific_advance_does_not_rewind_or_double_funding():
    data = history(5)
    data.payload["funding"] = [{"payment_time": (START + timedelta(minutes=offset)).isoformat()} for offset in (1, 3)]

    class SpyAccount:
        def __init__(self, advanced):
            self.last_advanced_through = advanced.isoformat()
            self.times, self.payments = [], []

        def advance(self, bars, through):
            assert through >= datetime.fromisoformat(self.last_advanced_through)
            self.times.append(through)
            self.last_advanced_through = through.isoformat()

        def apply_funding(self, records, through):
            self.payments += [row["payment_time"] for row in records]

    advanced = SpyAccount(START + timedelta(minutes=1))
    other = SpyAccount(START)
    accounts = {"advanced": advanced, "other": other}
    runner._advance_accounts(accounts, data, START, START + timedelta(minutes=5))
    assert advanced.payments == [(START + timedelta(minutes=3)).isoformat()]
    assert len(other.payments) == 2
    counts = (len(advanced.times), len(other.times), len(advanced.payments), len(other.payments))
    runner._advance_accounts(accounts, data, START, START + timedelta(minutes=5))
    assert counts == (len(advanced.times), len(other.times), len(advanced.payments), len(other.payments))


def gemini_pin():
    return {"actual_model_id": runner.DEFAULT_SMART_MODEL, "model_id": runner.DEFAULT_SMART_MODEL,
            "weight_digest": None, "digest_status": "REMOTE_WEIGHTS_NOT_EXPOSED", "context_length": 8192,
            "context_length_source": "APPLICATION_INPUT_BUDGET", "endpoint": runner.DEFAULT_BASE_URL,
            "reasoning_effort": "high"}


def pinned_health(pin):
    return {"available": True, "model_available": True, "model_identity_source": "completion_probe", **pin}


def test_production_pin_requires_complete_identity_and_exact_receipt():
    pin = gemini_pin()
    provider = SimpleNamespace(health=lambda **kwargs: pinned_health(pin))
    assert runner._production_model_pin(provider)[0] == pin
    context = SimpleNamespace(model_call_completed=True, model_digest=None,
        model_inference_settings={"actual_model_id": pin["actual_model_id"],
            "verified_manifest_model_id": pin["model_id"], "context_length": 8192,
            "model_identity_source": "completion_response"})
    runner._verify_model_receipt(context, pin)
    context.model_inference_settings["context_length"] = 4096
    with pytest.raises(runner.ReplayModelIdentityMismatch, match="RECEIPT"):
        runner._verify_model_receipt(context, pin)
    provider.health = lambda **kwargs: {**pinned_health(pin), "weight_digest": "a" * 64}
    with pytest.raises(runner.ReplayModelIdentityMismatch, match="INCOMPLETE"):
        runner._production_model_pin(provider)


def test_health_transport_failure_skips_one_scan_and_resume_keeps_error(tmp_path, monkeypatch):
    from core.ai import ollama
    pin = gemini_pin()
    health_calls = []
    model_calls = []

    def health(**kwargs):
        health_calls.append(None)
        if len(health_calls) == 2:
            return {"available": False, "model_available": False,
                    "error_type": "ModelTimeoutError", "error_code": "timed out"}
        return pinned_health(pin)

    monkeypatch.setattr(ollama, "OllamaProvider", lambda **kwargs: SimpleNamespace(health=health))
    monkeypatch.setattr(runner, "_model_wall_elapsed", lambda *args, **kwargs: 0.0)

    def output(self, context, **kwargs):
        model_calls.append(context.cycle_id)
        context.model_call_completed = True
        context.model_inference_settings = {"actual_model_id": pin["actual_model_id"],
            "verified_manifest_model_id": pin["model_id"], "context_length": 8192,
            "model_identity_source": "completion_response"}
        return wait(context)

    monkeypatch.setattr(runner.AISessionCoordinator, "_model_output", output)
    path = tmp_path / "replay.sqlite3"
    first = runner.run_ai_template_replay(history(5), db_path=path, priority_guard=lambda: None, max_decisions=1)
    assert first["status"] == "PAUSED" and len(model_calls) == 0
    assert rows(path)[0]["status"] == "ERROR"
    assert "ReplayModelUnavailable" in rows(path)[0]["error_code"]
    result = runner.run_ai_template_replay(history(5), db_path=path, priority_guard=lambda: None, resume=True)
    assert result["status"] == "COMPLETED" and result["comparison_eligible"] is False
    assert result["decision_count"] == 5 and len(model_calls) == 4
    assert len(result["errors"]) == 1
    assert [r["status"] for r in rows(path)].count("ERROR") == 1
    assert all(item["fills"] == 0 for item in result["results"])


@pytest.mark.parametrize("slow_probe", [2, 3])
def test_submission_clock_includes_predecision_health_latency(tmp_path, monkeypatch, slow_probe):
    from core.ai import ollama
    pin = gemini_pin()
    clock = [1000.0]
    probes = []
    def health(**kwargs):
        probes.append(None)
        if len(probes) == slow_probe:
            clock[0] += 20
        return pinned_health(pin)

    monkeypatch.setattr(ollama, "OllamaProvider", lambda **kwargs: SimpleNamespace(health=health))
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    def output(self, context, **kwargs):
        assert kwargs["deadline_monotonic"] == 1170
        clock[0] += 50
        context.model_call_completed = True
        context.model_inference_settings = {"actual_model_id": pin["actual_model_id"],
            "verified_manifest_model_id": pin["model_id"], "context_length": 8192,
            "model_identity_source": "completion_response"}
        return wait(context)
    monkeypatch.setattr(runner.AISessionCoordinator, "_model_output", output)
    path = tmp_path / "replay.sqlite3"
    runner.run_ai_template_replay(history(5), db_path=path, priority_guard=lambda: None, max_decisions=1,
                                  model_budget_seconds=170)
    first = rows(path)[0]
    assert first["wall_elapsed_seconds"] == 70
    assert first["submission_at"] == (START + timedelta(minutes=2)).isoformat()


def test_health_consuming_whole_scan_deadline_never_invokes_decision(tmp_path, monkeypatch):
    from core.ai import ollama
    pin = gemini_pin()
    clock, calls = [1000.0], []
    def health(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            clock[0] += 171
        return pinned_health(pin)
    monkeypatch.setattr(ollama, "OllamaProvider", lambda **kwargs: SimpleNamespace(health=health))
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    def forbidden(*args, **kwargs):
        pytest.fail("an expired scan cannot request a decision")
    monkeypatch.setattr(runner.AISessionCoordinator, "_model_output", forbidden)
    path = tmp_path / "replay.sqlite3"
    result = runner.run_ai_template_replay(history(5), db_path=path, priority_guard=lambda: None, max_decisions=1)
    first = rows(path)[0]
    assert first["status"] == "ERROR" and "DEADLINE_EXCEEDED" in first["error_code"]
    assert first["wall_elapsed_seconds"] == 171
    assert all(item["fills"] == 0 for item in result["results"])
    assert calls[1]["timeout_sec"] == 30


@pytest.mark.parametrize("error_type", ["ModelClientError", "LLMError", None])
def test_unproven_health_or_wrong_identity_is_still_fatal(error_type):
    provider = SimpleNamespace(health=lambda **kwargs: {
        "available": False, "model_available": False, "error_type": error_type,
        "error_code": "MODEL_RESPONSE_IDENTITY_MISMATCH"})
    with pytest.raises(runner.ReplayModelIdentityMismatch):
        runner._production_model_pin(provider)


def test_changed_production_receipt_stops_run_and_resume_never_executes(tmp_path, monkeypatch):
    from core.ai import ollama
    pin = gemini_pin()
    provider = SimpleNamespace(health=lambda **kwargs: pinned_health(pin))
    monkeypatch.setattr(ollama, "OllamaProvider", lambda **kwargs: provider)
    calls = []

    def changed_receipt(self, context, **kwargs):
        calls.append(context.cycle_id)
        context.model_call_completed = True
        context.model_digest = "b" * 64
        context.model_inference_settings = {"actual_model_id": pin["actual_model_id"], "verified_manifest_model_id": pin["model_id"], "context_length": 8192, "model_identity_source": "completion_response"}
        return wait(context)

    monkeypatch.setattr(runner.AISessionCoordinator, "_model_output", changed_receipt)
    path = tmp_path / "replay.sqlite3"
    result = runner.run_ai_template_replay(history(5), db_path=path, priority_guard=lambda: None)
    assert result["status"] == "FAILED" and result["comparison_eligible"] is False
    assert result["model_pin"] == pin and len(calls) == 1
    assert all(item["fills"] == 0 for item in result["results"])
    resumed = runner.run_ai_template_replay(history(5), db_path=path, priority_guard=lambda: None, resume=True)
    assert resumed == result and len(calls) == 1
    with sqlite3.connect(path) as db:
        config = json.loads(db.execute("SELECT config_json FROM ai_template_replay_runs").fetchone()[0])
    assert config["model_pin"] == pin


@pytest.mark.parametrize("failure", [URLError(TimeoutError("timed out")), HTTPError("http://127.0.0.1", 500, "failed", {}, None)])
def test_stopped_runtime_exception_does_not_allow_timeout_or_http_error(monkeypatch, failure):
    monkeypatch.setattr(runner, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(failure))
    with pytest.raises(runner.ReplayPaused, match="PRIORITY_STATUS_UNAVAILABLE"):
        runner.runtime_priority_guard("http://127.0.0.1:18765/status", budget_seconds=70, allow_stopped_runtime=True)()


def test_explicit_stopped_runtime_requires_connection_refused_without_local_slots(monkeypatch):
    calls = []
    def read(url, **kwargs):
        calls.append(url)
        raise URLError(ConnectionRefusedError("backend has no listener"))
    monkeypatch.setattr(runner, "urlopen", read)
    with pytest.raises(runner.ReplayPaused, match="PRIORITY_STATUS_UNAVAILABLE"):
        runner.runtime_priority_guard("http://127.0.0.1:18765/status", budget_seconds=70)()
    runner.runtime_priority_guard("http://127.0.0.1:18765/status", budget_seconds=70, allow_stopped_runtime=True)()
    assert calls == ["http://127.0.0.1:18765/status"] * 2


def test_priority_status_budget_covers_health_probe_and_still_requires_idle_scheduler(monkeypatch):
    import io
    observed = []
    state = {"ai_session": {"enabled": False, "state": "STOPPED"}}
    def read(url, **kwargs):
        observed.append(kwargs["timeout"])
        return io.BytesIO(json.dumps(state).encode())
    monkeypatch.setattr(runner, "urlopen", read)
    guard = runner.runtime_priority_guard("http://127.0.0.1:18765/status", budget_seconds=170)
    guard()
    assert observed == [35]
    state["ai_session"] = {"enabled": True, "state": "RUNNING"}
    with pytest.raises(runner.ReplayPaused, match="NEXT_SCAN_HAS_PRIORITY"):
        guard()


def test_research_performance_never_includes_a_later_closed_trade():
    account = SimpleNamespace(completed_trades=[
        {"instrument_id": "ETHUSDT", "closed_at": START.isoformat(), "net_pnl": 12.0},
        {"instrument_id": "ETHUSDT", "closed_at": (START + timedelta(minutes=5)).isoformat(), "net_pnl": -30.0}],
        summary=lambda prices: {"fees": 1.0, "funding_pnl": -.2})
    context = runner._research_performance_context(account, {}, START)
    assert context["closed_trade_count"] == context["wins"] == 1
    assert context["losses"] == 0 and context["net_closed_pnl_usdt"] == 12 and context["win_rate"] == 1
    assert context["research_only"] is True and context["source"] == "AI_REPLAY_SIMULATION"
    assert context["latest_closed_trade"]["closed_at"] == START.isoformat()


def test_halted_research_account_stops_calls_and_never_reports_recovered_roi(tmp_path, monkeypatch):
    from core.replay.ai_simulation import ReplayAccount
    original = ReplayAccount.advance
    halted_id = next(strategy["template_id"] for strategy in runner.frozen_templates()
                     if strategy["execution"]["scan_interval_minutes"] == 5)

    def halt_one(self, bars, through):
        result = original(self, bars, through)
        if self.account_id.endswith(":" + halted_id) and through >= START + timedelta(minutes=5):
            self.halted_reason = "UNSUPPORTED_LIQUIDATION_PATH"
        return result

    monkeypatch.setattr(ReplayAccount, "advance", halt_one)
    calls = []
    path = tmp_path / "replay.sqlite3"
    result = runner.run_ai_template_replay(history(20), db_path=path,
        model_decider=lambda context: (calls.append(context.strategy_instructions["template_id"]), wait(context))[1])
    halted = next(item for item in result["results"] if item["template_id"] == halted_id)
    assert calls.count(halted_id) == 1 and halted["skipped_halted_scans"] == 3
    assert halted["roi"] is None and halted["economic_eligible"] is False and halted["complete_window"] is False
    assert result["economic_eligible"] is False and result["comparison_eligible"] is False
    skipped = [row for row in rows(path) if row["status"] == "ACCOUNT_HALTED"]
    assert len(skipped) == 3 and all(row["decision_json"] is None for row in skipped)
    assert all(calls.count(item["template_id"]) == 2 and item["complete_window"] for item in result["results"] if item["template_id"] != halted_id)


def _install_pinned_test_model(monkeypatch, calls, mutable_pin, *, change_after_completion=False):
    from core.ai import ollama
    provider = SimpleNamespace(health=lambda **kwargs: pinned_health(mutable_pin))
    monkeypatch.setattr(ollama, "OllamaProvider", lambda **kwargs: provider)
    monkeypatch.setattr(runner, "_model_wall_elapsed", lambda *args, **kwargs: 0.0)

    def receipt(self, context, **kwargs):
        calls.append(context.cycle_id)
        context.model_call_attempted = context.model_call_completed = True
        context.model_digest = mutable_pin["weight_digest"]
        context.model_inference_settings = {"actual_model_id": mutable_pin["actual_model_id"],
            "verified_manifest_model_id": mutable_pin["model_id"], "context_length": mutable_pin["context_length"], "model_identity_source": "completion_response"}
        if change_after_completion:
            mutable_pin["context_length"] = 4096
        return wait(context)

    monkeypatch.setattr(runner.AISessionCoordinator, "_model_output", receipt)


def test_model_pin_is_part_of_resume_identity_and_rejects_other_weights(tmp_path, monkeypatch):
    pin = gemini_pin()
    calls = []
    _install_pinned_test_model(monkeypatch, calls, pin)
    path = tmp_path / "replay.sqlite3"
    result = runner.run_ai_template_replay(history(5), db_path=path, priority_guard=lambda: None, max_decisions=1)
    assert result["status"] == "PAUSED" and result["model_pin"]["weight_digest"] is None
    pin["context_length"] = 4096
    with pytest.raises(ValueError, match="MISMATCH"):
        runner.run_ai_template_replay(history(5), db_path=path, priority_guard=lambda: None, resume=True)
    assert len(calls) == 1


def test_post_completion_weight_swap_stops_before_execution(tmp_path, monkeypatch):
    pin = gemini_pin()
    calls = []
    _install_pinned_test_model(monkeypatch, calls, pin, change_after_completion=True)
    result = runner.run_ai_template_replay(history(5), db_path=tmp_path / "replay.sqlite3", priority_guard=lambda: None)
    assert result["status"] == "FAILED" and result["pause_reason"] == "REPLAY_MODEL_COMPLETION_IDENTITY_MISMATCH"
    assert result["comparison_eligible"] is False and len(calls) == 1
    assert all(item["fills"] == 0 and not item["execution_counts"] for item in result["results"])


def test_deterministic_injected_model_uses_zero_simulated_wall_latency(monkeypatch):
    monkeypatch.setattr(runner.time, "monotonic", lambda: 104.25)
    assert runner._model_wall_elapsed(100.0, production=False) == 0
    assert runner._model_wall_elapsed(100.0, production=True) == 4.25
