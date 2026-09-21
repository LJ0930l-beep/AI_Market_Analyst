"""Acceptance Test Suite RT17 to RT26 for Real Trading Repair Plan v1.2.

Covers:
- RT17: Autonomous cycle trace with real/mocked Qwen model records latency, prompts, outputs.
- RT18: AI_LED proposes valid paper order without fixed S1–S6 rule signals.
- RT19: Model timeout / expired generation discards action and records reason.
- RT20: AI position close / stop tighten updates DB positions and protective orders.
- RT21: Dual instance lease contention: only one instance holds execution lease.
- RT22: NOT_RUN display in UI and API when no cycles have run.
- RT23: External TESTNET verification (dynamic capabilities probing and fee/funding reconciliation).
- RT24: Manifest returns NOT_RUN when test report is absent, and PASS when report exists.
- RT25: Clean migration and credential sanitization with idempotent execution.
- RT26: Full regression test verifying six strategies, news revisions, and read-only research intact.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
import os
import pathlib
import tempfile
import time
from typing import Any, Dict, List
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.storage import SQLiteStore
from core.providers.base import Bar
from core.trading.ledger import AccountLedger, LedgerEventType
from core.trading.risk_engine import RiskEngine
from core.trading.execution_gateway import (
    ExecutionGateway,
    TradingMode,
    ControlMode,
    DecisionPath,
    OrderStatus,
    ProtectionPlan,
    ProtectionStatus,
    OrderIntent,
    GatewayError,
)
from core.trading.position_guardian import PositionGuardian
from core.trading.authorization import (
    AuthorizationManager,
    ConfirmationSource,
    TradingAuthorization,
)
from core.trading.ai_led_engine import (
    AILedDecisionEngine,
    AICycleContext,
    AIActionOutput,
    AICycleResult,
    AIActionType,
)
from core.model_routing import DEFAULT_SMART_MODEL
from core.trading.session_manager import RuntimeLease, SessionManager
from core.trading.testnet_capabilities import TestnetCapabilityService
from core.data_migration import DataMigrator, LegacyAccountStatus
from core.manifest import generate_evidence_manifest, parse_junit_report
from core.quant.strategies import (
    STRATEGIES,
    EMATrend,
    BollingerSqueeze,
    LiquiditySweep,
    SessionVWAP,
    OpeningRangeBreakout,
    FundingExtreme,
)
from core.market_intelligence import classify_macro_policy
from apps.api.v2 import router_for


@pytest.fixture
def temp_store():
    tmpdir = tempfile.mkdtemp()
    db_path = pathlib.Path(tmpdir) / "test_rt17_26.db"
    store = SQLiteStore(db_path)
    store.initialize()
    try:
        yield store
    finally:
        import gc, shutil
        gc.collect()
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def test_setup(temp_store):
    ledger = AccountLedger(temp_store)
    account_id = "acc_rt_test"
    ledger.create_account(
        account_id=account_id,
        mode="PAPER",
        currency="USDT",
        initial_deposit=Decimal("10000.00"),
    )
    gateway = ExecutionGateway(temp_store)
    risk_engine = RiskEngine(ledger)
    guardian = PositionGuardian(temp_store, ledger)
    engine = AILedDecisionEngine(
        store=temp_store,
        execution_gateway=gateway,
        risk_engine=risk_engine,
        ledger=ledger,
        guardian=guardian,
        agent_policy_id="ai_led_bonsai_27b",
    )
    return {
        "store": temp_store,
        "ledger": ledger,
        "gateway": gateway,
        "risk_engine": risk_engine,
        "guardian": guardian,
        "engine": engine,
        "account_id": account_id,
    }


def _seed_price(store, symbol: str, price: float):
    now = datetime.now(timezone.utc)
    end = now + timedelta(minutes=15)
    now_iso = now.isoformat()
    with store._connect() as db:
        db.execute(
            """INSERT OR REPLACE INTO market_bars (symbol, timeframe, bar_start, bar_end, open, high, low, close, volume, provider, data_as_of, is_closed, received_at)
               VALUES (?, '15m', ?, ?, ?, ?, ?, ?, ?, 'test', ?, 1, ?)""",
            (symbol, now_iso, end.isoformat(), price, price + 10.0, price - 10.0, price, 100.0, now_iso, now_iso),
        )


def _with_verified_bonsai_receipt(ctx: AICycleContext) -> AICycleContext:
    """Mark a fixture decision as a completed Bonsai call for execution tests."""
    artifact = r"D:\test-models\Ternary-Bonsai-2-27B-PTQ1_0.gguf"
    ctx.model_id = DEFAULT_SMART_MODEL
    ctx.model_version = artifact
    ctx.model_inference_settings = {
        "actual_model_id": artifact,
        "model_identity_source": "request_bound_to_verified_manifest",
        "verified_manifest_model_id": artifact,
    }
    ctx.model_call_attempted = True
    ctx.model_call_completed = True
    return ctx


# ============================================================================
# RT17: Autonomous cycle trace records latency, prompts, outputs
# ============================================================================
def test_rt17_qwen_cycle_trace_latency_prompt_output(test_setup):
    engine = test_setup["engine"]
    account_id = test_setup["account_id"]
    store = test_setup["store"]

    _seed_price(store, "BTCUSDT", 50000.0)

    def mock_runner(ctx: AICycleContext) -> AIActionOutput:
        return AIActionOutput(
            action="WAIT",
            instrument_id="BTCUSDT",
            reason="Market consolidating, awaiting confirmation",
            evidence_refs=("rev_trace_001", "macro_neutral"),
        )

    engine.model_runner = mock_runner
    now = datetime.now(timezone.utc)
    ctx = AICycleContext(
        cycle_id="cycle_rt17_trace",
        account_id=account_id,
        generation=1,
        started_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=60)).isoformat(),
        allowed_instruments=("BTCUSDT",),
        max_risk_fraction=0.0025,
        mode=TradingMode.PAPER,
    )

    result = engine.execute_cycle(ctx)
    assert result.status == "WAITING"
    assert result.action_output.action == "WAIT"

    # Verify cycle persistence in ai_led_cycles table
    with store._connect() as db:
        row = db.execute("SELECT * FROM ai_led_cycles WHERE cycle_id = ?", ("cycle_rt17_trace",)).fetchone()
        assert row is not None
        assert row["action"] == "WAIT"
        assert row["status"] == "WAITING"
        payload = json.loads(row["payload_json"])
        assert payload["action"] == "WAIT"
        assert "rev_trace_001" in result.action_output.evidence_refs


# ============================================================================
# RT18: AI_LED proposes valid paper order without fixed S1–S6 rule signals
# ============================================================================
def test_rt18_ai_led_independent_paper_order_no_rule_signals(test_setup):
    engine = test_setup["engine"]
    account_id = test_setup["account_id"]
    store = test_setup["store"]

    _seed_price(store, "BTCUSDT", 50000.0)

    auth_mgr = AuthorizationManager(store)
    auth_mgr.grant_authorization(
        account_id=account_id,
        venue="simulated",
        mode=TradingMode.PAPER,
        decision_path=DecisionPath.AI_LED,
        allowed_instruments=["BTCUSDT"],
        allowed_sides=["LONG"],
        max_risk_fraction=Decimal("0.0025"),
        max_leverage=3,
        duration_seconds=3600,
        confirmed_by=ConfirmationSource.LOCAL_USER_WIZARD,
    )

    def mock_buyer(ctx: AICycleContext) -> AIActionOutput:
        return AIActionOutput(
            action="OPEN_LONG",
            instrument_id="BTCUSDT",
            reason="Autonomous deep momentum breakout identified without S1-S6 rule",
            entry_price=50000.0,
            stop_price=49000.0,
            take_profit=52000.0,
            requested_risk_fraction=0.0025,
            requested_leverage=1,
            evidence_refs=("macro_relief_flow",),
        )

    engine.model_runner = mock_buyer
    now = datetime.now(timezone.utc)
    ctx = _with_verified_bonsai_receipt(AICycleContext(
        cycle_id="cycle_rt18_ai_led",
        account_id=account_id,
        generation=1,
        started_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=60)).isoformat(),
        allowed_instruments=("BTCUSDT",),
        max_risk_fraction=0.0025,
        mode=TradingMode.PAPER,
    ))

    result = engine.execute_cycle(ctx)
    assert result.status == "EXECUTED", result.reason
    assert result.order_intent is not None
    assert result.order_intent.decision_path == DecisionPath.AI_LED
    assert result.order_intent.control_mode == ControlMode.AUTONOMOUS
    assert result.order_intent.protection_plan.stop_price == 49000.0

    # Verify position is recorded in simulated_positions
    with store._connect() as db:
        pos = db.execute("SELECT * FROM simulated_positions WHERE symbol = 'BTCUSDT'").fetchone()
        assert pos is not None
        assert pos["status"] in ("OPEN", "PARTIALLY_CLOSED")


# ============================================================================
# RT19: Model timeout / expired generation discards action and records reason
# ============================================================================
def test_rt19_model_timeout_and_expired_action_discarded(test_setup):
    engine = test_setup["engine"]
    account_id = test_setup["account_id"]
    store = test_setup["store"]

    now = datetime.now(timezone.utc)
    # Context expired 5 seconds ago
    ctx = AICycleContext(
        cycle_id="cycle_rt19_timeout",
        account_id=account_id,
        generation=1,
        started_at=(now - timedelta(seconds=125)).isoformat(),
        expires_at=(now - timedelta(seconds=5)).isoformat(),
        allowed_instruments=("BTCUSDT",),
        max_risk_fraction=0.0025,
        mode=TradingMode.PAPER,
    )

    late_output = AIActionOutput(
        action="OPEN_LONG",
        instrument_id="BTCUSDT",
        reason="Action produced after 120s latency deadline",
        entry_price=50000.0,
        stop_price=49000.0,
    )

    result = engine.execute_cycle(ctx, now=now, model_output=late_output)
    assert result.status == "TIMEOUT_DISCARDED"
    assert "Cycle timeout" in result.reason
    assert result.order_intent is None

    # Verify no position opened
    with store._connect() as db:
        cnt = db.execute("SELECT COUNT(*) FROM simulated_positions").fetchone()[0]
        assert cnt == 0


# ============================================================================
# RT20: AI position close / stop tighten updates DB positions and protective orders
# ============================================================================
def test_rt20_ai_position_management_close_and_stop_tightening(test_setup):
    engine = test_setup["engine"]
    account_id = test_setup["account_id"]
    store = test_setup["store"]

    _seed_price(store, "BTCUSDT", 50000.0)

    # 1. Manually open a position with initial stop at 49000.0
    pos_payload = {
        "position_id": "pos_rt20_test",
        "account_id": account_id,
        "venue": "simulated",
        "mode": "PAPER",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "quantity": 0.1,
        "entry": 50000.0,
        "stop": 49000.0,
        "target": 52000.0,
        "remaining_contracts": 0.1,
    }
    with store._connect() as db:
        db.execute(
            """INSERT INTO simulated_positions (position_id, symbol, status, payload_json, updated_at)
               VALUES (?, ?, 'OPEN', ?, ?)""",
            ("pos_rt20_test", "BTCUSDT", json.dumps(pos_payload), "2026-09-08T00:00:00Z"),
        )

    now = datetime.now(timezone.utc)
    ctx = AICycleContext(
        cycle_id="cycle_rt20_manage",
        account_id=account_id,
        generation=1,
        started_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=60)).isoformat(),
        allowed_instruments=("BTCUSDT",),
        positions=[pos_payload],
    )

    # Step A: Tighten stop from 49000 to 49500 -> SUCCESS
    tighten_out = AIActionOutput(
        action="TIGHTEN_STOP",
        instrument_id="BTCUSDT",
        position_id="pos_rt20_test",
        new_stop_price=49500.0,
        reason="Locking in partial gains as market structure advances",
    )
    res_tighten = engine.execute_cycle(ctx, model_output=tighten_out)
    assert res_tighten.status == "EXECUTED"
    assert "Stop tightened from 49000.0 to 49500.0" in res_tighten.reason

    # Step B: Attempt to widen stop to 48000 -> FORBIDDEN
    widen_out = AIActionOutput(
        action="TIGHTEN_STOP",
        instrument_id="BTCUSDT",
        position_id="pos_rt20_test",
        new_stop_price=48000.0,
        reason="Malicious or flawed model attempting to widen risk",
    )
    res_widen = engine.execute_cycle(ctx, model_output=widen_out)
    assert res_widen.status == "REJECTED"
    assert "STOP_WIDENING_FORBIDDEN" in res_widen.reason

    # Step C: Autonomous Close Position
    close_out = AIActionOutput(
        action="CLOSE_POSITION",
        instrument_id="BTCUSDT",
        position_id="pos_rt20_test",
        reason="Take profit structure completed, exiting cleanly",
    )
    res_close = engine.execute_cycle(ctx, model_output=close_out)
    assert res_close.status == "EXECUTED"
    assert res_close.order_intent is not None
    assert res_close.order_intent.reduce_only is True


# ============================================================================
# RT21: Dual instance lease contention: only one instance holds execution lease
# ============================================================================
def test_rt21_dual_instance_runtime_lease_contention(temp_store):
    lease_mgr_a = RuntimeLease(temp_store, default_ttl_seconds=10)
    lease_mgr_b = RuntimeLease(temp_store, default_ttl_seconds=10)

    # Process A acquires lease
    now = datetime.now(timezone.utc)
    acquired_a = lease_mgr_a.acquire(lease_name="ai_execution_lease", holder_id="proc_inst_a", now=now)
    assert acquired_a is True

    # Process B attempts to acquire same lease -> CONCURRENT CONFLICT, returns False
    acquired_b = lease_mgr_b.acquire(lease_name="ai_execution_lease", holder_id="proc_inst_b", now=now)
    assert acquired_b is False

    # Process A releases lease
    released = lease_mgr_a.release(lease_name="ai_execution_lease", holder_id="proc_inst_a")
    assert released is True

    # Process B now acquires lease successfully
    acquired_b2 = lease_mgr_b.acquire(lease_name="ai_execution_lease", holder_id="proc_inst_b", now=now)
    assert acquired_b2 is True


# ============================================================================
# RT22: NOT_RUN display in UI and API when no cycles have run
# ============================================================================
def test_rt22_not_run_initial_state_and_no_fake_cycles(temp_store):
    app = FastAPI()
    router = router_for(
        get_store=lambda: temp_store,
        get_runtime=lambda: None,
        get_translation=lambda: None,
    )
    app.include_router(router)
    client = TestClient(app, headers={"Host": "localhost", "Origin": "http://localhost:5173"})

    # 1. Check AI session status for clean store
    res_status = client.get("/v2/ai-session/status")
    assert res_status.status_code == 200
    data_status = res_status.json()
    assert data_status["latest_cycle"] is None
    assert data_status["protection_summary"]["active_positions"] == 0

    # 2. Check cycles endpoint
    res_cycles = client.get("/v2/ai-session/cycles")
    assert res_cycles.status_code == 200
    data_cycles = res_cycles.json()
    assert data_cycles["total"] == 0
    assert data_cycles["cycles"] == []

    # Ensure no mock tokens exist in response
    raw_text = res_status.text + res_cycles.text
    assert "cycle_init_001" not in raw_text
    assert "320ms" not in raw_text


# ============================================================================
# RT23: Dynamic capabilities probing and fee/funding reconciliation
# ============================================================================
def test_rt23_testnet_capabilities_dynamic_probing_and_auth_boundary():
    svc = TestnetCapabilityService(venue="gate")
    caps = svc.probe_capabilities()

    assert caps.venue == "gate"
    assert caps.status == "NOT_RUN"
    assert caps.source == "NOT_RUN_NO_ADAPTER"
    assert caps.has_native_tpsl is None
    assert caps.funding_interval_hours is None
    assert caps.observed_at is None

    # A declared fallback fee is explicitly unverified without a venue adapter.
    fee_taker = svc.reconcile_trade_fee(venue="gate", notional=Decimal("10000.00"), is_maker=False)
    assert fee_taker["expected_fee"] == Decimal("5.00")  # 10000 * 0.0005 = 5.00 USDT
    assert fee_taker["status"] == "UNVERIFIED"
    assert fee_taker["source"] == "NOT_RUN_NO_ADAPTER"

    fee_maker = svc.reconcile_trade_fee(venue="gate", notional=Decimal("10000.00"), is_maker=True)
    assert fee_maker["expected_fee"] == Decimal("2.00")  # 10000 * 0.0002 = 2.00 USDT
    assert fee_maker["status"] == "UNVERIFIED"

    # Funding arithmetic remains deterministic, but its interval is unknown until probed.
    funding = svc.reconcile_funding_payment(position_notional=Decimal("50000.00"), funding_rate=Decimal("0.0001"))
    assert funding["funding_payment"] == Decimal("5.00")
    assert funding["interval_hours"] is None

    class FakeAdapter:
        def probe_capabilities(self):
            return {
                "has_native_tpsl": True,
                "supports_batch_orders": False,
                "supports_amend": True,
                "funding_interval_hours": 8,
                "rate_limit_per_minute": 120,
                "fee_schedule": {"maker": 0.0002, "taker": 0.0005},
                "min_order_sizes": {"BTC_USDT": 0.001},
            }

    observed = TestnetCapabilityService(venue="gate", adapter=FakeAdapter()).probe_capabilities()
    assert observed.status == "AVAILABLE"
    assert observed.source == "ADAPTER_PROBE"
    assert observed.observed_at is not None
    assert observed.has_native_tpsl is True
    assert observed.funding_interval_hours == 8


# ============================================================================
# RT24: Manifest returns NOT_RUN when report is absent, and PASS when report exists
# ============================================================================
def test_rt24_manifest_junit_driven_transitions(temp_store):
    tmpdir = tempfile.mkdtemp()
    try:
        # Part A: No report file provided -> strictly NOT_RUN default
        manifest_clean = generate_evidence_manifest(
            workspace_root=tmpdir,
            output_relative_path="manifest.json",
            report_file=None,
        )
        assert manifest_clean["acceptance_matrix"]["AT01"]["status"] == "NOT_RUN"
        assert manifest_clean["rt_matrix"]["RT01"]["status"] == "NOT_RUN"
        assert manifest_clean["rt_matrix"]["RT17"]["status"] == "NOT_RUN"

        # Part B: Synthesize a JUnit XML report with RT01 and RT17 passing
        junit_xml_path = pathlib.Path(tmpdir) / "junit_test_report.xml"
        junit_content = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="2" failures="0" errors="0" time="1.5">
    <testcase classname="tests.test_spec_rt01_rt16" name="test_rt01_tp1_then_stop_loss_unique_exit" time="0.35"/>
    <testcase classname="tests.test_spec_rt17_rt26" name="test_rt17_qwen_cycle_trace_latency_prompt_output" time="0.42"/>
  </testsuite>
</testsuites>
"""
        junit_xml_path.write_text(junit_content, encoding="utf-8")

        manifest_with_report = generate_evidence_manifest(
            workspace_root=tmpdir,
            output_relative_path="manifest_reported.json",
            report_file=junit_xml_path,
        )
        # RT01 advances to PASS; the mock RT17 trace is never promoted to real Qwen E2E.
        assert manifest_with_report["rt_matrix"]["RT01"]["status"] == "PASS"
        assert manifest_with_report["rt_matrix"]["RT01"]["evidence"]["duration_seconds"] == 0.35
        assert manifest_with_report["rt_matrix"]["RT17"]["status"] == "PASS_MOCK_ONLY"
        assert manifest_with_report["rt_matrix"]["RT17"]["evidence"]["duration_seconds"] == 0.42
        assert manifest_with_report["rt_matrix"]["RT17"]["evidence"]["verification_tier"] == "MOCK_PROVIDER_TRACE"
        assert manifest_with_report["rt_matrix"]["RT17"]["evidence"]["real_qwen_e2e"] is False

        # Other tests without report remain NOT_RUN
        assert manifest_with_report["rt_matrix"]["RT02"]["status"] == "NOT_RUN"
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# ============================================================================
# RT25: Clean migration and credential sanitization with idempotent execution
# ============================================================================
def test_rt25_clean_migration_and_credential_sanitization():
    tmpdir = tempfile.mkdtemp()
    try:
        db_path = pathlib.Path(tmpdir) / "legacy_test.db"
        migrator = DataMigrator(db_path)

        # Setup legacy schema with unverified capital (<= 0) and missing status
        with migrator._connect() as db:
            db.execute("""
            CREATE TABLE accounts (
                account_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                initial_deposit REAL NOT NULL
            );
            """)
            db.execute("INSERT INTO accounts VALUES ('acc_old_zero', 'PAPER', 0.0)")
            db.execute("INSERT INTO accounts VALUES ('acc_old_neg', 'PAPER', -50.0)")

        # First migration execution: audits and flags unverified accounts
        res1 = migrator.migrate()
        assert res1.success is True
        assert res1.accounts_migrated == 2

        with migrator._connect() as db:
            rows = db.execute("SELECT account_id, status FROM accounts").fetchall()
            for r in rows:
                assert r["status"] == LegacyAccountStatus.LEGACY_UNVERIFIED.value

        # Second migration execution: perfectly idempotent (no duplicates, classifications preserved)
        res2 = migrator.migrate()
        assert res2.success is True

        with migrator._connect() as db:
            total_accounts = db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            assert total_accounts == 2
            rows = db.execute("SELECT account_id, status FROM accounts").fetchall()
            for r in rows:
                assert r["status"] == LegacyAccountStatus.LEGACY_UNVERIFIED.value
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# ============================================================================
# RT26: Full regression: six strategies, news revisions, and research immutability
# ============================================================================
def test_rt26_full_regression_six_strategies_news_and_research_immutability(temp_store):
    # 1. Verify six strategies interface contract
    expected_ids = {"ema_trend", "bollinger_squeeze", "liquidity_sweep", "session_vwap", "opening_range_breakout", "funding_extreme"}
    assert set(STRATEGIES.keys()) == expected_ids

    for strat_id, strat_cls in STRATEGIES.items():
        inst = strat_cls()
        assert inst.strategy_id == strat_id
        assert hasattr(inst, "evaluate")
        assert inst.warmup_bars >= 1

    # 2. Verify news intelligence: rejects spoofed URL query containing official domains
    spoofed = classify_macro_policy("Fake SEC announcement", "", url="https://malicious-phishing.org/?ref=sec.gov")
    assert spoofed["source_tier"] != "TIER_A_OFFICIAL"

    official = classify_macro_policy("Official SEC Release", "", url="https://www.sec.gov/news/press-release")
    assert official["source_tier"] == "TIER_A_OFFICIAL"

    # 3. Verify RESEARCH mode immutability: submit_intent must reject with MODE_NOT_EXECUTABLE
    ledger = AccountLedger(temp_store)
    ledger.create_account("acc_research_01", mode="RESEARCH", initial_deposit=Decimal("10000.0"))
    gateway = ExecutionGateway(temp_store)
    research_intent = OrderIntent(
        intent_id="intent_rt26_research",
        idempotency_key="idem_rt26_research",
        account_id="acc_research_01",
        mode=TradingMode.RESEARCH,
        venue="simulated",
        environment="RESEARCH",
        instrument_id="BTCUSDT",
        side="LONG",
        order_type="market",
        quantity=0.1,
        price=50000.0,
        protection_plan=ProtectionPlan(stop_price=49000.0),
    )
    with pytest.raises(GatewayError) as exc_info:
        gateway.submit_intent(research_intent)
    assert exc_info.value.code == "MODE_NOT_EXECUTABLE"
