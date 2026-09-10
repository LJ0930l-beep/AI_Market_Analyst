"""Acceptance Test Suite AT08 to AT14 for Milestone M1 (Specification v1.1).

Covers:
- AT08: Background monitoring runtime paused, PositionGuardian remains active and triggers stop-out.
- AT09: Stale generation / terminated session rejects late or invalid AI proposals.
- AT10: Removing strategy subscriptions keeps existing positions protected; RuntimeLease blocks dual instances.
- AT11: Unified AccountLedger snapshot consistency across analytics, risk, and API.
- AT12: Atomic risk reservations and persistent daily loss circuit breaker across process restarts.
- AT13: Sub-cent price and small tick precision handling without floating-point truncation; rule-based leverage score.
- AT14: Authentic simulated execution timeline (signal_at -> filled_at) and PriceDeviationExceededError guard.
"""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
import pathlib
import tempfile
import time
from types import SimpleNamespace
from typing import Any, Dict
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.storage import SQLiteStore
from core.instruments import Instrument, instrument_for
from core.trading.ledger import AccountLedger, LedgerEventType
from core.trading.risk_engine import RiskEngine, RiskLimits, RiskReservation, compute_rule_based_leverage_score
from core.trading.position_guardian import PositionGuardian
from core.trading.session_manager import SessionManager, RuntimeLease, SessionState
from core.trading.simulated_execution import (
    SimulatedExecutionEngine,
    PriceDeviationExceededError,
    ProposalExpiredError,
)
from core.monitoring_runtime import MonitoringRuntime
from core.analysis.ai_trade_analytics import analyze_ai_trading_ledger
from apps.api.v2 import router_for


@pytest.fixture
def temp_store():
    tmpdir = tempfile.mkdtemp()
    db_path = pathlib.Path(tmpdir) / "test_m1.db"
    store = SQLiteStore(db_path)
    store.initialize()
    try:
        yield store
    finally:
        import gc, shutil
        gc.collect()
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def api_client(temp_store):
    app = FastAPI()
    router = router_for(
        get_store=lambda: temp_store,
        get_runtime=lambda: None,
        get_translation=lambda: None,
    )
    app.include_router(router)
    return TestClient(app)


class MockMonitoringService:
    def __init__(self, store):
        self.store = store
        self.strategy_mode = False
        self.cycle_count = 0
        self.cancel_event = None
        self.max_symbols = 20

    def scan_once(self):
        self.cycle_count += 1
        return []


def test_at08_background_runtime_paused_guardian_stops_out(temp_store):
    """AT08: Runtime paused halts strategy loop, but PositionGuardian remains running and stops out open positions."""
    ledger = AccountLedger(temp_store)
    ledger.create_account("default", mode="PAPER", initial_deposit=Decimal("10000.0"))

    # Create an OPEN position with stop loss at 67000.0
    now_iso = datetime.now(timezone.utc).isoformat()
    pos = {
        "position_id": "pos_at08_btc",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "status": "OPEN",
        "entry": 68000.0,
        "stop": 67000.0,
        "targets": [70000.0],
        "contracts": 10.0,
        "filled_contracts": 10.0,
        "remaining_contracts": 10.0,
        "contract_size": 0.01,
        "fee_rate": 0.0005,
        "slippage": 0.0,
        "protected": True,
        "tp1_done": False,
        "realized_pnl": 0.0,
        "created_at": now_iso,
        "account_id": "default",
        "venue": "simulated",
        "mode": "PAPER",
    }
    with temp_store._connect() as db:
        db.execute(
            "INSERT INTO simulated_positions (position_id, symbol, status, payload_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            (pos["position_id"], pos["symbol"], pos["status"], json.dumps(pos), now_iso),
        )

    service = MockMonitoringService(temp_store)
    runtime = MonitoringRuntime(
        store=temp_store,
        service=service,
        poll_interval_seconds=0.05,
        stream_join_timeout_seconds=0.1,
    )

    try:
        runtime.start()
        assert runtime.status()["state"] in ("starting", "running")
        assert runtime.guardian.is_running()

        # Pause runtime
        runtime.pause()
        status_after_pause = runtime.status()
        assert status_after_pause["state"] == "paused", (
            status_after_pause.get("state"),
            status_after_pause.get("last_reason"),
            status_after_pause.get("last_error"),
            status_after_pause.get("stream"),
        )
        # Guardian MUST remain alive during pause (R05, AT08)
        assert runtime.guardian.is_running()

        # Adverse market tick crosses stop price (low = 66500 <= stop 67000)
        adverse_bar = SimpleNamespace(
            symbol="BTCUSDT",
            open=67200.0,
            high=67300.0,
            low=66500.0,
            close=66800.0,
            timestamp=datetime.now(timezone.utc),
        )
        runtime.process_market_event("BTCUSDT", adverse_bar)

        # Verify position is closed by guardian
        with temp_store._connect() as db:
            row = db.execute("SELECT status, payload_json FROM simulated_positions WHERE position_id=?", (pos["position_id"],)).fetchone()
            assert row is not None
            assert row["status"] == "CLOSED"
            data = json.loads(row["payload_json"])
            assert data["remaining_contracts"] == 0.0
            assert data["realized_pnl"] < 0.0

        # Verify exit and fee recorded in ledger
        snapshot = ledger.get_snapshot("default")
        assert snapshot.realized_pnl < Decimal("0")
        assert snapshot.net_equity < Decimal("10000.0")

    finally:
        runtime.stop()


def test_at09_stale_generation_or_terminated_session_rejects_proposal(temp_store):
    """AT09: Session generation tracking invalidates proposals from older generations or terminated sessions."""
    session_mgr = SessionManager(temp_store)
    session_mgr.start(user_initiated=True)

    status1 = session_mgr.status()
    assert status1["state"] in ("RUNNING", "ACTIVE")
    gen1 = status1["generation"]
    assert gen1 == 1

    # Proposal created under generation 1 is valid while session is active
    assert session_mgr.is_proposal_valid(proposal_generation=gen1) is True

    # Pause session: proposals generated during or submitted during pause are rejected
    session_mgr.pause()
    assert session_mgr.status()["state"] == "PAUSED"
    assert session_mgr.is_proposal_valid(proposal_generation=gen1) is False

    # Resume session: generation increments, invalidating late/stale proposals from gen 1
    session_mgr.resume()
    status2 = session_mgr.status()
    assert status2["state"] in ("RUNNING", "ACTIVE")
    gen2 = status2["generation"]
    assert gen2 == gen1 + 1

    # Stale gen 1 proposal rejected
    assert session_mgr.is_proposal_valid(proposal_generation=gen1) is False
    # Fresh gen 2 proposal accepted
    assert session_mgr.is_proposal_valid(proposal_generation=gen2) is True

    # Terminate session: all proposals rejected
    session_mgr.terminate()
    assert session_mgr.status()["state"] == "TERMINATED"
    assert session_mgr.is_proposal_valid(proposal_generation=gen2) is False


def test_at10_subscription_removed_guardian_active_and_runtime_lease_blocks(temp_store):
    """AT10: Removing strategy subscriptions retains position guardian protection; RuntimeLease blocks dual instances."""
    ledger = AccountLedger(temp_store)
    ledger.create_account("default", mode="PAPER", initial_deposit=Decimal("10000.0"))
    service = MockMonitoringService(temp_store)
    runtime1 = MonitoringRuntime(
        store=temp_store,
        service=service,
        poll_interval_seconds=0.05,
        stream_join_timeout_seconds=0.1,
    )
    runtime1.start()

    # Part A: Second runtime instance with the same store fails to acquire lease
    service2 = MockMonitoringService(temp_store)
    runtime2 = MonitoringRuntime(
        store=temp_store,
        service=service2,
        poll_interval_seconds=0.05,
        stream_join_timeout_seconds=0.1,
    )

    with pytest.raises(RuntimeError, match="Runtime lease held by another instance"):
        runtime2.start()
    assert runtime2.status()["state"] == "degraded"

    # Part B: Position remains protected even if all subscriptions are disabled or removed
    now_iso = datetime.now(timezone.utc).isoformat()
    pos = {
        "position_id": "pos_at10_eth",
        "symbol": "ETHUSDT",
        "side": "SHORT",
        "status": "OPEN",
        "entry": 2500.0,
        "stop": 2600.0,
        "targets": [2300.0],
        "contracts": 5.0,
        "filled_contracts": 5.0,
        "remaining_contracts": 5.0,
        "contract_size": 1.0,
        "fee_rate": 0.0005,
        "slippage": 0.0,
        "protected": True,
        "tp1_done": False,
        "realized_pnl": 0.0,
        "created_at": now_iso,
        "account_id": "default",
        "venue": "simulated",
        "mode": "PAPER",
    }
    with temp_store._connect() as db:
        db.execute(
            "INSERT INTO simulated_positions (position_id, symbol, status, payload_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            (pos["position_id"], pos["symbol"], pos["status"], json.dumps(pos), now_iso),
        )

    # Disable all subscriptions
    temp_store.delete_watchlist_entry("ETHUSDT")

    # Guardian process tick (SHORT position: high >= stop 2600.0 triggers stop)
    stop_bar = SimpleNamespace(
        symbol="ETHUSDT",
        open=2580.0,
        high=2650.0,
        low=2570.0,
        close=2620.0,
        timestamp=datetime.now(timezone.utc),
    )
    runtime1.process_market_event("ETHUSDT", stop_bar)

    with temp_store._connect() as db:
        row = db.execute("SELECT status FROM simulated_positions WHERE position_id=?", (pos["position_id"],)).fetchone()
        assert row["status"] == "CLOSED"

    # Stop runtime1; runtime2 can now successfully acquire the lease and start
    runtime1.stop()
    res2 = runtime2.start()
    assert res2["state"] == "starting" or res2["state"] == "running"
    runtime2.stop()


def test_at11_unified_ledger_snapshot_consistency(temp_store, api_client):
    """AT11: Unified AccountLedger snapshot matches across Analytics, RiskEngine, and API endpoints without capital split."""
    ledger = AccountLedger(temp_store)
    account_id = "trader_main"
    ledger.create_account(account_id, mode="PAPER", initial_deposit=Decimal("10000.0"))

    # Record trade events
    now = datetime.now(timezone.utc)
    ledger.record_event(account_id, LedgerEventType.FILL_ENTRY, Decimal("-100.0"), occurred_at=now)
    ledger.record_event(account_id, LedgerEventType.FILL_EXIT, Decimal("250.0"), occurred_at=now + timedelta(seconds=1))
    ledger.record_event(account_id, LedgerEventType.FEE, Decimal("-6.50"), occurred_at=now + timedelta(seconds=2))
    ledger.record_event(account_id, LedgerEventType.FUNDING_FEE, Decimal("1.25"), occurred_at=now + timedelta(seconds=3))

    # Calculate expected values:
    # wallet_balance = 10000.0 + 250.0 - 6.50 + 1.25 = 10244.75
    # net_equity = wallet_balance + unrealized_pnl (0) = 10244.75
    snapshot = ledger.get_snapshot(account_id)
    assert snapshot.initial_deposit == Decimal("10000.0")
    assert snapshot.realized_pnl == Decimal("250.0")
    assert snapshot.cumulative_fees == Decimal("6.50")
    assert snapshot.cumulative_funding_fees == Decimal("1.25")
    assert snapshot.wallet_balance == Decimal("10244.75")
    assert snapshot.net_equity == Decimal("10244.75")

    # Verify snapshot via HTTP API endpoint
    resp = api_client.get(f"/v2/accounts/{account_id}/snapshot")
    assert resp.status_code == 200
    data = resp.json()
    assert float(data["initial_deposit"]) == 10000.0
    assert float(data["net_equity"]) == 10244.75
    assert float(data["realized_pnl"]) == 250.0
    assert float(data["cumulative_fees"]) == 6.50

    # Verify risk summary endpoint
    risk_resp = api_client.get(f"/v2/accounts/{account_id}/risk")
    assert risk_resp.status_code == 200
    risk_data = risk_resp.json()
    assert risk_data["account_id"] == account_id
    assert risk_data["net_equity"] == 10244.75
    assert risk_data["max_portfolio_risk_budget"] == round(10244.75 * 0.01, 4)
    assert risk_data["circuit_breaker_active"] is False

    # Verify Analytics module uses account table initial_capital (10000.0) rather than hardcoded 1000.0
    analytics = analyze_ai_trading_ledger(temp_store, account_id=account_id)
    assert analytics["account"]["initial_capital_usdt"] == 10000.0
    assert "规则建议评分 / 自适应" in analytics["account"]["leverage_range"]


def test_at12_atomic_risk_reservation_and_daily_loss_persistence(temp_store):
    """AT12: Atomic risk reservations prevent budget overrun; daily loss persists across simulated restarts."""
    ledger = AccountLedger(temp_store)
    account_id = "risk_acct"
    ledger.create_account(account_id, mode="PAPER", initial_deposit=Decimal("10000.0"))

    # Net equity is 10000.0, max portfolio risk is 1.0% = 100.0 USDT
    # Reserve 60.0 USDT risk
    ok1 = ledger.reserve_risk(account_id, "res_1", amount_risk=Decimal("60.0"), amount_margin=Decimal("600.0"))
    assert ok1 is True

    # Second reservation of 50.0 USDT risk exceeds remaining budget (60 + 50 > 100) -> BLOCKED
    ok2 = ledger.reserve_risk(account_id, "res_2", amount_risk=Decimal("50.0"), amount_margin=Decimal("500.0"))
    assert ok2 is False

    # Release reservation 1
    rel_ok = ledger.release_risk(account_id, "res_1")
    assert rel_ok is True

    # Now reservation 2 succeeds
    ok2_retry = ledger.reserve_risk(account_id, "res_2", amount_risk=Decimal("50.0"), amount_margin=Decimal("500.0"))
    assert ok2_retry is True

    # Simulate catastrophic daily loss reaching 1.5% limit (10000.0 * 0.015 = 150.0 USDT)
    now = datetime.now(timezone.utc)
    ledger.record_event(account_id, LedgerEventType.FILL_EXIT, Decimal("-160.0"), occurred_at=now)

    snap = ledger.get_snapshot(account_id)
    assert snap.daily_loss >= Decimal("150.0")
    assert snap.daily_loss_limit_reached is True

    # Risk reservation must now be rejected due to active circuit breaker
    ok_cb = ledger.reserve_risk(account_id, "res_cb", amount_risk=Decimal("10.0"), amount_margin=Decimal("100.0"))
    assert ok_cb is False

    # Simulate full process restart: instantiate brand new AccountLedger connecting to same SQLite store
    ledger_restarted = AccountLedger(temp_store)
    snap_restarted = ledger_restarted.get_snapshot(account_id)
    assert snap_restarted.daily_loss_limit_reached is True
    assert snap_restarted.daily_loss >= Decimal("150.0")


def test_at13_sub_cent_and_small_tick_precision_handling(temp_store):
    """AT13: Sub-cent price tokens and small ticks calculate sizes with strict ROUND_DOWN without zeroing or overflow."""
    ledger = AccountLedger(temp_store)
    ledger.create_account("micro_trader", mode="PAPER", initial_deposit=Decimal("10000.0"))
    risk_engine = RiskEngine(ledger)

    # Micro-cap token: 0.00001234 USDT, stop at 0.00001100 USDT, tick 0.00000001, contract_size = 1000, step_size = 1.0
    micro_spec = {
        "symbol": "MEMEUSDT",
        "contractSize": 1000.0,
        "limits": {
            "amount": {"min": 1.0, "max": 100000000.0, "step": 1.0},
            "price": {"min": 0.00000001, "step": 0.00000001},
        },
    }

    contracts, margin, stop_dist = risk_engine.calculate_position_size(
        account_id="micro_trader",
        entry_price=0.00001234,
        stop_price=0.00001100,
        market_spec=micro_spec,
        risk_budget_pct=0.0025,  # 0.25% of 10000 = 25.0 USDT risk
        leverage=10,
    )

    assert contracts > 0
    # Risk per contract = (0.00001234 - 0.00001100) * 1000 = 0.00134 USDT
    # Max contracts for 25.0 USDT risk = 25 / 0.00134 = ~18656 contracts
    assert contracts == Decimal("18656")
    assert margin > 0

    # High-priced asset with fractional precision: BTC at 68420.55, stop at 67100.10
    btc_spec = {
        "symbol": "BTCUSDT",
        "contractSize": 0.01,
        "limits": {
            "amount": {"min": 1.0, "max": 10000.0, "step": 1.0},
            "price": {"min": 0.1, "step": 0.1},
        },
    }
    btc_contracts, btc_margin, _ = risk_engine.calculate_position_size(
        account_id="micro_trader",
        entry_price=68420.55,
        stop_price=67100.10,
        market_spec=btc_spec,
        risk_budget_pct=0.0025,
        leverage=20,
    )
    assert btc_contracts > 0

    # Test rule-based leverage score (replacing deceptive "AI-decided" leverage claims)
    lev, reason = compute_rule_based_leverage_score(
        distance_to_stop_pct=0.019,
        atr_pct=0.015,
        trend_aligned=True,
    )
    assert 5 <= lev <= 100
    assert "规则计算" in reason or "ATR" in reason


def test_at14_simulated_execution_timeline_and_deviation_guard(temp_store):
    """AT14: Simulated execution enforces timeline verification and rejects price drift exceeding max allowed offset."""
    ledger = AccountLedger(temp_store)
    # The production gateway now re-applies the unified risk budget.  Five
    # contracts at the configured stop/contract size require a larger paper
    # account than the old direct-INSERT helper assumed.
    ledger.create_account("sim_exec_acct", mode="PAPER", initial_deposit=Decimal("30000.0"))
    engine = SimulatedExecutionEngine(temp_store, ledger, max_allowed_offset_pct=0.005)  # 0.5% max drift

    now = datetime.now(timezone.utc)
    signal_time = now - timedelta(seconds=1)

    proposal = {
        "symbol": "BTCUSDT",
        "account_id": "sim_exec_acct",
        "venue": "simulated",
        "mode": "PAPER",
        "side": "LONG",
        "entry": 68000.0,
        "stop": 67000.0,
        "targets": [70000.0],
        "expires_at": (now + timedelta(seconds=10)).isoformat(),
    }
    risk_plan = {
        "contracts": 5.0,
        "contract_size": 0.01,
        "fee_rate": 0.0005,
        "leverage": 10,
        "margin": 34.0,
    }

    # Case A: Normal slippage (68030.0 vs signal 68000.0 -> offset 0.044% <= 0.5%) -> SUCCEEDS
    res = engine.execute_fill(
        identity="fill_valid_01",
        proposal=proposal,
        risk_plan=risk_plan,
        current_market_price=68030.0,
        signal_time=signal_time,
        now=now,
    )
    assert res["status"] == "OPEN"
    assert res["filled_contracts"] == 5.0
    assert "timeline" in res
    tl = res["timeline"]
    assert tl["signal_at"] <= tl["ai_started_at"] <= tl["ai_completed_at"] <= tl["intent_at"] <= tl["submitted_at"] <= tl["filled_at"]

    # Case B: Excessive market price jump (current market 68500.0 vs signal 68000.0 -> offset 0.735% > 0.5%) -> REJECTED
    with pytest.raises(PriceDeviationExceededError, match="exceeds allowable tolerance"):
        engine.execute_fill(
            identity="fill_invalid_offset",
            proposal=proposal,
            risk_plan=risk_plan,
            current_market_price=68500.0,
            signal_time=signal_time,
            now=now,
        )

    # Case C: Expired proposal due to model delay -> REJECTED
    expired_proposal = dict(proposal)
    expired_proposal["expires_at"] = (now - timedelta(seconds=1)).isoformat()
    with pytest.raises(ProposalExpiredError, match="Proposal expired"):
        engine.execute_fill(
            identity="fill_expired",
            proposal=expired_proposal,
            risk_plan=risk_plan,
            current_market_price=68020.0,
            signal_time=signal_time,
            now=now,
        )
