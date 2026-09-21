"""Acceptance Test Suite AT31 to AT35 for Milestone M2 (Specification v1.1).

Covers:
- AT31: AI-led independent proposal without S1-S6 rule trigger (independent intent, DecisionPath.AI_LED, no rule forgery).
- AT32: Autonomous continuous execution in PAPER mode across multiple cycles without manual confirmation.
- AT33: Hard risk rejection on over-budget risk fraction, unauthorized instruments, and tampering.
- AT34: PositionGuardian priority & anti-competition (stop widening forbidden, reverse opening without closing forbidden, guardian stop priority).
- AT35: Inference timeout discarding and stale action rejection without batch replay.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
import pathlib
import tempfile
from typing import Any, Dict, List
import pytest

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
    ProtectionStatus,
)
from core.trading.position_guardian import PositionGuardian
from core.trading.ai_led_engine import (
    AILedDecisionEngine,
    AICycleContext,
    AIActionOutput,
    AICycleResult,
    AIActionType,
)
from core.model_routing import DEFAULT_SMART_MODEL


def _with_verified_bonsai_receipt(ctx: AICycleContext) -> AICycleContext:
    """Mark fixture decisions as completed Bonsai calls for execution-gate tests."""
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


@pytest.fixture
def temp_store():
    tmpdir = tempfile.mkdtemp()
    db_path = pathlib.Path(tmpdir) / "test_at31_35.db"
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
    account_id = "test_ai_acc"
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


def test_at31_ai_led_independent_proposal_no_s1_s6_trigger(test_setup):
    """AT31: AI generates independent valid order intent without S1-S6 rule triggers, without forgery."""
    engine = test_setup["engine"]
    account_id = test_setup["account_id"]

    now = datetime.now(timezone.utc)
    ctx = _with_verified_bonsai_receipt(AICycleContext(
        cycle_id="cycle_at31_001",
        account_id=account_id,
        generation=1,
        started_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=15)).isoformat(),
        allowed_instruments=("BTCUSDT", "ETHUSDT"),
        max_risk_fraction=0.0025,
        market_snapshots={"BTCUSDT": {"last": 50000.0, "close": 50000.0}},
    ))

    action_out = AIActionOutput(
        action="OPEN_LONG",
        instrument_id="BTCUSDT",
        reason="AI multi-modal macro momentum breakout",
        entry_price=50000.0,
        stop_price=49000.0,
        take_profit=52000.0,
        requested_risk_fraction=0.002,
        evidence_refs=("macro_rate_cut", "liquidity_surge"),
    )

    result = engine.execute_cycle(ctx, now=now, model_output=action_out)

    assert result.status == "EXECUTED"
    assert result.order_intent is not None
    intent = result.order_intent

    # Must be AI_LED decision path and AUTONOMOUS control mode
    assert intent.decision_path == DecisionPath.AI_LED
    assert intent.control_mode == ControlMode.AUTONOMOUS
    assert intent.mode == TradingMode.PAPER

    # Must NOT forge or copy any S1-S6 deterministic strategy rule IDs
    s1_s6_names = ["s1_", "s2_", "s3_", "s4_", "s5_", "s6_", "trend_breakout", "bollinger_squeeze", "liquidity_sweep", "session_vwap", "opening_range_breakout", "funding_extreme"]
    for s_name in s1_s6_names:
        assert s_name not in intent.strategy_version.lower(), f"Strategy version {intent.strategy_version} forged S1-S6 name {s_name}"

    assert intent.strategy_version == "ai_led_bonsai_27b"

    # Protection plan verified
    assert intent.protection_plan.stop_price == 49000.0
    assert intent.protection_plan.take_profit == 52000.0
    assert intent.protection_plan.status == ProtectionStatus.ACTIVE


def test_at32_autonomous_continuous_execution_paper_mode(test_setup):
    """AT32: Autonomous continuous execution across multiple cycles without manual confirmation."""
    engine = test_setup["engine"]
    ledger = test_setup["ledger"]
    account_id = test_setup["account_id"]

    now = datetime.now(timezone.utc)

    # Cycle 1: AI opens LONG position
    ctx1 = _with_verified_bonsai_receipt(AICycleContext(
        cycle_id="cycle_at32_001",
        account_id=account_id,
        generation=1,
        started_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=15)).isoformat(),
        allowed_instruments=("BTCUSDT", "ETHUSDT"),
        max_risk_fraction=0.0025,
        market_snapshots={"BTCUSDT": {"last": 50000.0, "close": 50000.0}},
    ))
    out1 = AIActionOutput(
        action="OPEN_LONG",
        instrument_id="BTCUSDT",
        reason="Autonomous cycle 1: opening position",
        entry_price=50000.0,
        stop_price=49500.0,
        take_profit=51500.0,
        requested_risk_fraction=0.002,
    )
    res1 = engine.execute_cycle(ctx1, now=now, model_output=out1)
    assert res1.status == "EXECUTED", res1.reason

    open_pos = ledger.get_open_positions(account_id)
    assert len(open_pos) == 1
    assert open_pos[0]["instrument_id"] == "BTCUSDT"
    assert open_pos[0]["side"] == "LONG"
    assert open_pos[0]["stop"] == 49500.0
    # PAPER entry is the adverse executable quote, not the AI's informational price.
    assert open_pos[0]["entry"] == 50050.0

    # Cycle 2: Continuous autonomous cycle -> AI tightens stop without manual confirmation
    now2 = now + timedelta(seconds=60)
    ctx2 = AICycleContext(
        cycle_id="cycle_at32_002",
        account_id=account_id,
        generation=2,
        started_at=now2.isoformat(),
        expires_at=(now2 + timedelta(seconds=15)).isoformat(),
        allowed_instruments=("BTCUSDT", "ETHUSDT"),
        max_risk_fraction=0.0025,
        market_snapshots={"BTCUSDT": {"last": 50800.0, "close": 50800.0}},
        positions=open_pos,
    )
    out2 = AIActionOutput(
        action="TIGHTEN_STOP",
        instrument_id="BTCUSDT",
        reason="Autonomous cycle 2: trailing stop to lock in profit",
        new_stop_price=49900.0,  # Tightened from 49500 to 49900
    )
    res2 = engine.execute_cycle(ctx2, now=now2, model_output=out2)
    assert res2.status == "EXECUTED"
    assert "Stop tightened" in res2.reason

    open_pos2 = ledger.get_open_positions(account_id)
    assert len(open_pos2) == 1
    assert open_pos2[0]["stop"] == 49900.0

    # Cycle 3: Continuous autonomous cycle -> AI decides to take profit and CLOSE_POSITION
    now3 = now2 + timedelta(seconds=60)
    ctx3 = AICycleContext(
        cycle_id="cycle_at32_003",
        account_id=account_id,
        generation=3,
        started_at=now3.isoformat(),
        expires_at=(now3 + timedelta(seconds=15)).isoformat(),
        allowed_instruments=("BTCUSDT", "ETHUSDT"),
        max_risk_fraction=0.0025,
        market_snapshots={"BTCUSDT": {"last": 51500.0, "close": 51500.0}},
        positions=open_pos2,
    )
    out3 = AIActionOutput(
        action="CLOSE_POSITION",
        instrument_id="BTCUSDT",
        reason="Autonomous cycle 3: take profit on target reached",
    )
    res3 = engine.execute_cycle(ctx3, now=now3, model_output=out3)
    assert res3.status == "EXECUTED"

    open_pos3 = ledger.get_open_positions(account_id)
    assert len(open_pos3) == 0  # Cleanly closed


def test_at33_hard_risk_rejection_over_budget_and_unauthorized(test_setup):
    """AT33: Hard risk rejection on over-budget risk fraction, unauthorized instruments, and tampering."""
    engine = test_setup["engine"]
    gateway = test_setup["gateway"]
    account_id = test_setup["account_id"]

    now = datetime.now(timezone.utc)
    ctx = _with_verified_bonsai_receipt(AICycleContext(
        cycle_id="cycle_at33_001",
        account_id=account_id,
        generation=1,
        started_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=15)).isoformat(),
        allowed_instruments=("BTCUSDT", "ETHUSDT"),
        max_risk_fraction=0.0025,
        market_snapshots={"BTCUSDT": {"last": 50000.0}, "DOGEUSDT": {"last": 0.15}},
    ))

    # Subcase A: Over-budget risk fraction (requested 1% vs limit 0.25%)
    out_over_risk = AIActionOutput(
        action="OPEN_LONG",
        instrument_id="BTCUSDT",
        reason="Aggressive bet",
        entry_price=50000.0,
        stop_price=49000.0,
        take_profit=53000.0,
        requested_risk_fraction=0.01,
    )
    res_a = engine.execute_cycle(ctx, now=now, model_output=out_over_risk)
    assert res_a.status == "REJECTED"
    assert "RISK_LIMIT_EXCEEDED" in res_a.reason

    # Subcase B: Unauthorized instrument (DOGEUSDT is not in allowed_instruments pool)
    out_unauth = AIActionOutput(
        action="OPEN_LONG",
        instrument_id="DOGEUSDT",
        reason="Meme coin hype",
        entry_price=0.15,
        stop_price=0.14,
        requested_risk_fraction=0.002,
    )
    res_b = engine.execute_cycle(ctx, now=now, model_output=out_unauth)
    assert res_b.status == "REJECTED"
    assert "INSTRUMENT_NOT_AUTHORIZED" in res_b.reason

    # Subcase C: Invalid stop direction (LONG stop above entry price)
    out_wrong_stop = AIActionOutput(
        action="OPEN_LONG",
        instrument_id="BTCUSDT",
        reason="Erroneous stop",
        entry_price=50000.0,
        stop_price=51000.0,  # Invalid: stop >= entry
        requested_risk_fraction=0.002,
    )
    res_c = engine.execute_cycle(ctx, now=now, model_output=out_wrong_stop)
    assert res_c.status == "REJECTED"
    assert "INVALID_STOP_DIRECTION" in res_c.reason

    # Subcase D: Self-modifying risk limit attempt (AI passes unauthorized extra parameters)
    out_tamper = AIActionOutput(
        action="OPEN_LONG",
        instrument_id="BTCUSDT",
        reason="Attempting to override max risk limit",
        entry_price=50000.0,
        stop_price=49000.0,
        requested_risk_fraction=0.008,
        extra_fields={"max_risk_fraction": 0.05, "override_risk_check": True},
    )
    res_d = engine.execute_cycle(ctx, now=now, model_output=out_tamper)
    assert res_d.status == "REJECTED"
    assert "RISK_LIMIT_EXCEEDED" in res_d.reason


def test_at34_guardian_stop_priority_and_anti_competition(test_setup):
    """AT34: Guardian stop priority and anti-competition rules (no stop widening, no implicit reversal, stop-first priority)."""
    engine = test_setup["engine"]
    ledger = test_setup["ledger"]
    guardian = test_setup["guardian"]
    account_id = test_setup["account_id"]

    now = datetime.now(timezone.utc)

    # Step 1: Open LONG position at 50000.0 with stop at 49500.0
    ctx1 = _with_verified_bonsai_receipt(AICycleContext(
        cycle_id="cycle_at34_001",
        account_id=account_id,
        generation=1,
        started_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=15)).isoformat(),
        allowed_instruments=("BTCUSDT",),
        market_snapshots={"BTCUSDT": {"last": 50000.0, "close": 50000.0}},
    ))
    out1 = AIActionOutput(
        action="OPEN_LONG",
        instrument_id="BTCUSDT",
        reason="Open long",
        entry_price=50000.0,
        stop_price=49500.0,
        take_profit=52000.0,
        requested_risk_fraction=0.002,
    )
    res1 = engine.execute_cycle(ctx1, now=now, model_output=out1)
    assert res1.status == "EXECUTED"

    # Step 2: Attempt STOP WIDENING (new stop 49000.0 is below current stop 49500.0 for LONG)
    ctx2 = _with_verified_bonsai_receipt(AICycleContext(
        cycle_id="cycle_at34_002",
        account_id=account_id,
        generation=2,
        started_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=15)).isoformat(),
        allowed_instruments=("BTCUSDT",),
        market_snapshots={"BTCUSDT": {"last": 49700.0}},
    ))
    out_widen = AIActionOutput(
        action="TIGHTEN_STOP",
        instrument_id="BTCUSDT",
        reason="Attempting to loosen stop to give trade more room",
        new_stop_price=49000.0,  # Widening stop!
    )
    res_widen = engine.execute_cycle(ctx2, now=now, model_output=out_widen)
    assert res_widen.status == "REJECTED"
    assert "STOP_WIDENING_FORBIDDEN" in res_widen.reason

    # Step 3: Attempt DIRECT REVERSE OPENING without closing LONG first
    out_rev = AIActionOutput(
        action="OPEN_SHORT",
        instrument_id="BTCUSDT",
        reason="Flipping short immediately",
        entry_price=49700.0,
        stop_price=50200.0,
        requested_risk_fraction=0.002,
    )
    res_rev = engine.execute_cycle(ctx2, now=now, model_output=out_rev)
    assert res_rev.status == "REJECTED"
    assert "REVERSE_OPENING_FORBIDDEN" in res_rev.reason

    # Step 4: PositionGuardian stop trigger priority
    # Simulate a sudden flash crash bar that breaches the 49500.0 stop level
    crash_bar = Bar(
        timestamp=now + timedelta(seconds=30),
        open=49600.0,
        high=49700.0,
        low=49200.0,  # Breached 49500 stop
        close=49300.0,
        volume=100.0,
    )
    exits = guardian.process_bar("BTCUSDT", crash_bar)
    assert len(exits) > 0
    assert exits[0]["reason"] == "STOP"

    # Confirm position is now CLOSED in ledger
    open_pos = ledger.get_open_positions(account_id)
    assert len(open_pos) == 0

    # Step 5: Late AI attempt to tighten stop or close already exited position is cleanly rejected
    now3 = now + timedelta(seconds=35)
    ctx3 = AICycleContext(
        cycle_id="cycle_at34_003",
        account_id=account_id,
        generation=3,
        started_at=now3.isoformat(),
        expires_at=(now3 + timedelta(seconds=15)).isoformat(),
        allowed_instruments=("BTCUSDT",),
        market_snapshots={"BTCUSDT": {"last": 49300.0}},
    )
    out_late = AIActionOutput(
        action="TIGHTEN_STOP",
        instrument_id="BTCUSDT",
        reason="Late action after stop hit",
        new_stop_price=49600.0,
    )
    res_late = engine.execute_cycle(ctx3, now=now3, model_output=out_late)
    assert res_late.status == "REJECTED"
    assert "NO_OPEN_POSITION_TO_TIGHTEN" in res_late.reason


def test_at35_inference_timeout_and_stale_actions_discard(test_setup):
    """AT35: Actions arriving after cycle expiration are discarded without batch replay."""
    engine = test_setup["engine"]
    account_id = test_setup["account_id"]

    start_time = datetime(2026, 9, 8, 10, 0, 0, tzinfo=timezone.utc)
    expiry_time = start_time + timedelta(seconds=5)  # 5-second deadline

    ctx = AICycleContext(
        cycle_id="cycle_at35_timeout",
        account_id=account_id,
        generation=1,
        started_at=start_time.isoformat(),
        expires_at=expiry_time.isoformat(),
        allowed_instruments=("BTCUSDT",),
        market_snapshots={"BTCUSDT": {"last": 50000.0}},
    )

    out = AIActionOutput(
        action="OPEN_LONG",
        instrument_id="BTCUSDT",
        reason="Stale signal after inference delay",
        entry_price=50000.0,
        stop_price=49500.0,
        requested_risk_fraction=0.002,
    )

    # Case A: Current time is 10:00:08 (3 seconds past expiration)
    late_time = start_time + timedelta(seconds=8)
    res = engine.execute_cycle(ctx, now=late_time, model_output=out)

    assert res.status == "TIMEOUT_DISCARDED"
    assert "Cycle timeout" in res.reason
    assert res.order_intent is None

    # Verify no open position was created
    open_pos = test_setup["ledger"].get_open_positions(account_id)
    assert len(open_pos) == 0

    # Case B: No batch replay of stale actions
    # When a new cycle starts, it starts clean without re-executing the stale order
    fresh_time = late_time + timedelta(seconds=10)
    ctx_fresh = AICycleContext(
        cycle_id="cycle_at35_fresh",
        account_id=account_id,
        generation=2,
        started_at=fresh_time.isoformat(),
        expires_at=(fresh_time + timedelta(seconds=15)).isoformat(),
        allowed_instruments=("BTCUSDT",),
        market_snapshots={"BTCUSDT": {"last": 50100.0}},
    )
    wait_out = AIActionOutput(
        action="WAIT",
        instrument_id="BTCUSDT",
        reason="AI chooses to wait on market clarity",
    )
    res_fresh = engine.execute_cycle(ctx_fresh, now=fresh_time, model_output=wait_out)
    assert res_fresh.status == "WAITING"

    # Confirm ledger still has zero open positions
    assert len(test_setup["ledger"].get_open_positions(account_id)) == 0
