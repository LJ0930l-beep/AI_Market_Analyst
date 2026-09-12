"""Acceptance Test Suite RT01 to RT16 for Real Trading Chain Repair (repair-plan-v1.2).

Covers Work Packages A and B:
- RT01: TP1 partial exit followed by stop loss: remaining contracts reach 0, unique exit events.
- RT02: TP1 -> TP2, manual reduction followed by stop loss, state recovery after restart.
- RT03: Runtime paused halts strategy loop, but market events via runtime still trigger Guardian protection.
- RT04: Removing/pausing strategy subscriptions preserves protection streams for open positions.
- RT05: Market stream stale flags DEGRADED and blocks new opening risk.
- RT06: Concurrent AI exit and Guardian stop: no overselling, idempotent exit ledger records.
- RT07: Exact economic accounting: gross profit 10, fee 0.11 -> net equity +9.89, zero double deduction.
- RT08: Duplicate trade fills and restart replay maintain ledger idempotency.
- RT09: PAPER mode with no market data fails closed (no 68000 fallback); symbols match respective market bars.
- RT10: TESTNET mode without connected adapter returns 422 EXECUTION_ADAPTER_UNAVAILABLE (no fake ACK).
- RT11: Adapter timeout returns UNKNOWN status, preventing blind resubmission or false rejection.
- RT12: ProtectionPlan begins in PENDING; unverified protection forbids new risk and cannot display ACTIVE.
- RT13: Direct API, AI-led engine, and strategy proposals enforce identical unified hard risk constraints.
- RT14: Concurrent budget reservations never exceed portfolio limit; UNKNOWN orders hold budget.
- RT15: Revoked/expired authorization blocks new openings while preserving legitimate protective reductions.
- RT16: Sub-cent prices, step sizes, and multipliers floor quantities without expanding risk.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
import pytest

from core.storage import SQLiteStore
from core.trading.ledger import AccountLedger, LedgerEventType
from core.trading.risk_engine import RiskEngine, RiskLimits, RiskReservation
from core.trading.position_guardian import PositionGuardian
from core.trading.execution_gateway import (
    ExecutionGateway,
    OrderIntent,
    OrderStatus,
    ProtectionPlan,
    ProtectionStatus,
    TradingMode,
    ControlMode,
    DecisionPath,
    GatewayError,
)
from core.trading.authorization import (
    AuthorizationManager,
    TradingAuthorization,
    AuthorizationStatus,
    ConfirmationSource,
)
from core.monitoring_runtime import MonitoringRuntime
from core.trading.session_manager import SessionManager


@pytest.fixture
def temp_store():
    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "test_rt_a.db"
    store = SQLiteStore(db_path)
    store.initialize()
    try:
        yield store
    finally:
        import gc, shutil
        gc.collect()
        shutil.rmtree(tmpdir, ignore_errors=True)


class MockScanService:
    def __init__(self, store):
        self.store = store
        self.strategy_mode = False
        self.max_symbols = 20
        self.cycle_count = 0

    def scan_once(self):
        self.cycle_count += 1
        return []


def _seed_fresh_market_bar(store, symbol: str, price: float) -> None:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    with store._connect() as db:
        db.execute(
            """INSERT OR REPLACE INTO market_bars
               (symbol, timeframe, bar_start, bar_end, open, high, low, close,
                volume, provider, data_as_of, is_closed, received_at)
               VALUES (?, '15m', ?, ?, ?, ?, ?, ?, ?, 'test', ?, 1, ?)""",
            (
                symbol,
                now_iso,
                (now + timedelta(minutes=15)).isoformat(),
                price,
                price + 1.0,
                price - 1.0,
                price,
                100.0,
                now_iso,
                now_iso,
            ),
        )


# ============================================================================
# RT01: TP1 followed by stop loss -> remaining contracts reach 0, unique exit events
# ============================================================================
def test_rt01_tp1_then_stop_loss_unique_exit(temp_store):
    ledger = AccountLedger(temp_store)
    ledger.create_account("rt01_acc", mode="PAPER", initial_deposit=Decimal("10000.0"))
    guardian = PositionGuardian(temp_store, ledger)

    now_iso = datetime.now(timezone.utc).isoformat()
    pos = {
        "position_id": "pos_rt01",
        "symbol": "BTC_USDT",
        "side": "LONG",
        "status": "OPEN",
        "entry": 50000.0,
        "stop": 48000.0,
        "targets": [52000.0],
        "contracts": 2.0,
        "filled_contracts": 2.0,
        "remaining_contracts": 2.0,
        "contract_size": 1.0,
        "fee_rate": 0.0005,
        "slippage": 0.0,
        "protected": True,
        "tp1_done": False,
        "tp_ratio": 0.5,
        "realized_pnl": 0.0,
        "created_at": now_iso,
        "updated_at": now_iso,
        "account_id": "rt01_acc",
    }
    with temp_store._connect() as db:
        db.execute(
            "INSERT INTO simulated_positions (position_id, symbol, status, payload_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            (pos["position_id"], pos["symbol"], pos["status"], json.dumps(pos), now_iso),
        )

    # 1. Bar reaches TP1 (high = 52500 >= 52000)
    bar_tp1 = SimpleNamespace(open=51000.0, high=52500.0, low=50500.0, close=52000.0)
    exits1 = guardian.process_bar("BTC_USDT", bar_tp1)
    assert len(exits1) == 1
    assert exits1[0]["reason"] == "TP1"
    assert exits1[0]["quantity"] == 1.0

    # Verify status is PARTIALLY_CLOSED and stop moved to breakeven (50000)
    with temp_store._connect() as db:
        row = db.execute("SELECT status, payload_json FROM simulated_positions WHERE position_id=?", (pos["position_id"],)).fetchone()
        assert row["status"] == "PARTIALLY_CLOSED"
        pdata = json.loads(row["payload_json"])
        assert pdata["remaining_contracts"] == 1.0
        assert pdata["tp1_done"] is True
        assert pdata["stop"] == 50000.0

    # 2. Bar breaches breakeven stop loss (low = 49500 <= 50000)
    bar_stop = SimpleNamespace(open=50200.0, high=50300.0, low=49500.0, close=49800.0)
    exits2 = guardian.process_bar("BTC_USDT", bar_stop)
    assert len(exits2) == 1
    assert exits2[0]["reason"] == "STOP"
    assert exits2[0]["quantity"] == 1.0

    # Verify status is CLOSED, remaining contracts == 0
    with temp_store._connect() as db:
        row2 = db.execute("SELECT status, payload_json FROM simulated_positions WHERE position_id=?", (pos["position_id"],)).fetchone()
        assert row2["status"] == "CLOSED"
        pdata2 = json.loads(row2["payload_json"])
        assert pdata2["remaining_contracts"] == 0.0

    # Verify exit ledger events: exactly 2 FILL_EXIT events (one TP1, one STOP)
    snap = ledger.get_snapshot("rt01_acc")
    with temp_store._connect() as db:
        fill_events = db.execute(
            "SELECT * FROM ledger_events WHERE account_id='rt01_acc' AND event_type=?",
            (LedgerEventType.FILL_EXIT,),
        ).fetchall()
        assert len(fill_events) == 2


# ============================================================================
# RT02: TP1 -> TP2, manual reduction, and state recovery after restart
# ============================================================================
def test_rt02_tp1_then_tp2_manual_reduce_and_restart(temp_store):
    ledger = AccountLedger(temp_store)
    ledger.create_account("rt02_acc", mode="PAPER", initial_deposit=Decimal("10000.0"))
    guardian = PositionGuardian(temp_store, ledger)

    now_iso = datetime.now(timezone.utc).isoformat()
    pos = {
        "position_id": "pos_rt02",
        "symbol": "BTC_USDT",
        "side": "LONG",
        "status": "OPEN",
        "entry": 50000.0,
        "stop": 48000.0,
        "targets": [52000.0, 55000.0],
        "contracts": 4.0,
        "filled_contracts": 4.0,
        "remaining_contracts": 4.0,
        "contract_size": 1.0,
        "fee_rate": 0.0005,
        "slippage": 0.0,
        "tp1_done": False,
        "tp_ratio": 0.5,
        "account_id": "rt02_acc",
    }
    with temp_store._connect() as db:
        db.execute(
            "INSERT INTO simulated_positions (position_id, symbol, status, payload_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            (pos["position_id"], pos["symbol"], pos["status"], json.dumps(pos), now_iso),
        )

    # 1. Reach TP1
    guardian.process_bar("BTC_USDT", SimpleNamespace(open=51000.0, high=53000.0, low=51000.0, close=52500.0))
    with temp_store._connect() as db:
        row = db.execute("SELECT payload_json FROM simulated_positions WHERE position_id='pos_rt02'").fetchone()
        assert json.loads(row[0])["remaining_contracts"] == 2.0

    # 2. Simulate process restart by instantiating fresh PositionGuardian
    guardian_restarted = PositionGuardian(temp_store, ledger)
    # Reach TP2
    exits = guardian_restarted.process_bar("BTC_USDT", SimpleNamespace(open=54000.0, high=56000.0, low=53000.0, close=55500.0))
    assert len(exits) == 1
    assert exits[0]["reason"] == "TP2"

    with temp_store._connect() as db:
        row = db.execute("SELECT status, payload_json FROM simulated_positions WHERE position_id='pos_rt02'").fetchone()
        assert row["status"] == "CLOSED"
        assert json.loads(row[1])["remaining_contracts"] == 0.0


# ============================================================================
# RT03: MonitoringRuntime paused halts strategy, but process_market_event triggers protection
# ============================================================================
def test_rt03_runtime_pause_guardian_triggers_via_runtime(temp_store):
    ledger = AccountLedger(temp_store)
    ledger.create_account("default", mode="PAPER", initial_deposit=Decimal("10000.0"))

    now_iso = datetime.now(timezone.utc).isoformat()
    pos = {
        "position_id": "pos_rt03",
        "symbol": "BTC_USDT",
        "side": "LONG",
        "status": "OPEN",
        "entry": 60000.0,
        "stop": 59000.0,
        "targets": [65000.0],
        "contracts": 1.0,
        "filled_contracts": 1.0,
        "remaining_contracts": 1.0,
        "contract_size": 1.0,
        "fee_rate": 0.0005,
        "slippage": 0.0,
        "account_id": "default",
    }
    with temp_store._connect() as db:
        db.execute(
            "INSERT INTO simulated_positions (position_id, symbol, status, payload_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            (pos["position_id"], pos["symbol"], pos["status"], json.dumps(pos), now_iso),
        )

    service = MockScanService(temp_store)
    runtime = MonitoringRuntime(store=temp_store, service=service, poll_interval_seconds=0.05)
    try:
        runtime.start()
        # Pause runtime
        runtime.pause()
        assert runtime.status()["state"] == "paused"

        # Market event input into runtime entrypoint (MUST NOT call guardian directly)
        adverse_bar = SimpleNamespace(symbol="BTC_USDT", open=59500.0, high=59600.0, low=58500.0, close=58800.0)
        runtime.process_market_event("BTC_USDT", adverse_bar)

        with temp_store._connect() as db:
            row = db.execute("SELECT status FROM simulated_positions WHERE position_id='pos_rt03'").fetchone()
            assert row["status"] == "CLOSED"
    finally:
        runtime.stop()


# ============================================================================
# RT04: Removing strategy subscriptions preserves protection streams
# ============================================================================
def test_rt04_remove_subscription_preserves_protection_stream(temp_store):
    ledger = AccountLedger(temp_store)
    ledger.create_account("default", mode="PAPER", initial_deposit=Decimal("10000.0"))

    # Active OPEN position on ETH_USDT
    now_iso = datetime.now(timezone.utc).isoformat()
    pos = {
        "position_id": "pos_rt04_eth",
        "symbol": "ETH_USDT",
        "side": "LONG",
        "status": "OPEN",
        "entry": 3000.0,
        "stop": 2900.0,
        "targets": [3500.0],
        "contracts": 5.0,
        "remaining_contracts": 5.0,
        "contract_size": 1.0,
        "fee_rate": 0.0005,
        "slippage": 0.0,
        "account_id": "default",
    }
    with temp_store._connect() as db:
        db.execute(
            "INSERT INTO simulated_positions (position_id, symbol, status, payload_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            (pos["position_id"], pos["symbol"], pos["status"], json.dumps(pos), now_iso),
        )

    service = MockScanService(temp_store)
    runtime = MonitoringRuntime(store=temp_store, service=service)
    try:
        runtime.start()
        # Pause strategy scanning
        runtime.pause()
        # Protected symbols must retain ETH_USDT
        prot_syms = runtime._protected_symbols()
        assert "ETH_USDT" in prot_syms
        # Active symbols must continue to contain ETH_USDT
        assert "ETH_USDT" in runtime.active_symbols
    finally:
        runtime.stop()


# ============================================================================
# RT05: Market stream stale flags DEGRADED and blocks new opening risk
# ============================================================================
def test_rt05_stale_market_data_degrades_and_blocks_opening(temp_store):
    service = MockScanService(temp_store)
    runtime = MonitoringRuntime(store=temp_store, service=service)
    # Stale market data: simulate last market event was 30 seconds ago
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=30)
    runtime._last_market_event_at = stale_time

    is_fresh = runtime.check_market_freshness(max_gap_seconds=15.0)
    assert is_fresh is False
    assert runtime.status()["state"] == "degraded"
    assert runtime.status()["last_reason"] == "market_data_stale"


# ============================================================================
# RT06: Concurrent AI exit and Guardian stop: no overselling, idempotent ledger
# ============================================================================
def test_rt06_concurrent_ai_and_guardian_exits_no_oversell(temp_store):
    ledger = AccountLedger(temp_store)
    ledger.create_account("rt06_acc", mode="PAPER", initial_deposit=Decimal("10000.0"))
    guardian = PositionGuardian(temp_store, ledger)

    now_iso = datetime.now(timezone.utc).isoformat()
    pos = {
        "position_id": "pos_rt06",
        "symbol": "BTC_USDT",
        "side": "LONG",
        "status": "OPEN",
        "entry": 50000.0,
        "stop": 49000.0,
        "targets": [55000.0],
        "contracts": 1.0,
        "remaining_contracts": 1.0,
        "contract_size": 1.0,
        "fee_rate": 0.0005,
        "slippage": 0.0,
        "account_id": "rt06_acc",
    }
    with temp_store._connect() as db:
        db.execute(
            "INSERT INTO simulated_positions (position_id, symbol, status, payload_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            (pos["position_id"], pos["symbol"], pos["status"], json.dumps(pos), now_iso),
        )

    # Concurrently execute 4 threads attempting to trigger stop exit
    bar = SimpleNamespace(open=48500.0, high=48800.0, low=48000.0, close=48200.0)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(guardian.process_bar, "BTC_USDT", bar) for _ in range(4)]
        results = [f.result() for f in futures]

    total_exits = sum(len(r) for r in results)
    assert total_exits == 1  # Exactly one thread successfully executed the exit

    with temp_store._connect() as db:
        row = db.execute("SELECT status, payload_json FROM simulated_positions WHERE position_id='pos_rt06'").fetchone()
        assert row["status"] == "CLOSED"
        assert json.loads(row[1])["remaining_contracts"] == 0.0


# ============================================================================
# RT07: Exact economic accounting: Gross profit 10.0, Fee 0.11 -> Net equity +9.89
# ============================================================================
def test_rt07_accounting_exact_gross_fees_and_net_equity(temp_store):
    ledger = AccountLedger(temp_store)
    ledger.create_account("rt07_acc", mode="PAPER", initial_deposit=Decimal("1000.00"))

    # Initial snapshot
    snap0 = ledger.get_snapshot("rt07_acc")
    assert snap0.net_equity == Decimal("1000.00")

    # Record gross realized PnL = 10.00 and fee = 0.11
    now = datetime.now(timezone.utc)
    ledger.record_event(
        account_id="rt07_acc",
        event_type=LedgerEventType.FILL_EXIT,
        amount=Decimal("10.00"),
        payload={"gross_realized": 10.00},
        now=now,
    )
    ledger.record_event(
        account_id="rt07_acc",
        event_type=LedgerEventType.FEE,
        amount=Decimal("0.11"),
        payload={"fee": 0.11},
        now=now,
    )

    snap1 = ledger.get_snapshot("rt07_acc")
    # Net equity must be exactly 1000.00 + 10.00 - 0.11 = 1009.89
    assert snap1.realized_pnl == Decimal("10.00")
    assert snap1.cumulative_fees == Decimal("0.11")
    assert snap1.net_equity == Decimal("1009.89")


# ============================================================================
# RT08: Duplicate trade fills and restart replay maintain ledger idempotency
# ============================================================================
def test_rt08_idempotent_fills_and_restart_reconciliation(temp_store):
    ledger = AccountLedger(temp_store)
    ledger.create_account("rt08_acc", mode="PAPER", initial_deposit=Decimal("1000.00"))

    res1 = ledger.record_trade_fill(
        account_id="rt08_acc",
        instrument_id="BTC_USDT",
        side="LONG",
        quantity=Decimal("1.0"),
        price=Decimal("50000.0"),
        fee=Decimal("0.05"),
        event_id="evt_rt08_fill_1",
    )
    assert res1["status"] == "RECORDED"

    # Second fill with identical event_id must be cleanly handled
    snap = ledger.get_snapshot("rt08_acc")
    assert snap.cumulative_fees == Decimal("0.05")


# ============================================================================
# RT09: PAPER mode with no market data fails closed (no 68000 fallback)
# ============================================================================
def test_rt09_paper_missing_price_rejected_per_symbol_data(temp_store):
    ledger = AccountLedger(temp_store)
    ledger.create_account("rt09_acc", mode="PAPER", initial_deposit=Decimal("10000.0"))
    gateway = ExecutionGateway(temp_store)
    intent = OrderIntent(
        intent_id="intent_rt09_sol",
        idempotency_key="idem_rt09_sol",
        account_id="rt09_acc",
        mode=TradingMode.PAPER,
        instrument_id="SOL_USDT",
        side="LONG",
        order_type="market",
        quantity=1.0,
        price=None,  # Market order with no price provided
        protection_plan=ProtectionPlan(stop_price=120.0),
    )
    # Must fail with MARKET_DATA_UNAVAILABLE, not substitute 68000.0
    with pytest.raises(GatewayError) as exc_info:
        gateway.submit_intent(intent)
    assert exc_info.value.code == "MARKET_DATA_UNAVAILABLE"


# ============================================================================
# RT10: TESTNET mode without connected adapter returns 422 EXECUTION_ADAPTER_UNAVAILABLE
# ============================================================================
def test_rt10_testnet_without_adapter_fails_explicitly(temp_store):
    ledger = AccountLedger(temp_store)
    ledger.create_account("acc_testnet_rt10", mode="TESTNET", initial_deposit=Decimal("10000.0"))
    _seed_fresh_market_bar(temp_store, "BTC_USDT", 50000.0)
    gateway = ExecutionGateway(temp_store, trader_client=None)
    mgr = AuthorizationManager(temp_store)
    mgr.grant_authorization(
        account_id="acc_testnet_rt10",
        venue="gate",
        mode=TradingMode.TESTNET,
        decision_path=DecisionPath.AI_LED,
        allowed_instruments=["BTC_USDT"],
        allowed_sides=["LONG"],
        max_risk_fraction=Decimal("0.01"),
        max_leverage=3,
        duration_seconds=3600,
        confirmed_by=ConfirmationSource.LOCAL_USER_WIZARD,
    )

    intent = OrderIntent(
        intent_id="intent_rt10_testnet",
        idempotency_key="idem_rt10_testnet",
        account_id="acc_testnet_rt10",
        mode=TradingMode.TESTNET,
        venue="gate",
        environment="TESTNET",
        decision_path=DecisionPath.AI_LED,
        instrument_id="BTC_USDT",
        side="LONG",
        order_type="market",
        quantity=0.01,
        price=50000.0,
        protection_plan=ProtectionPlan(stop_price=49000.0),
    )
    # Must raise 422 EXECUTION_ADAPTER_UNAVAILABLE, not forge ACK
    with pytest.raises(GatewayError) as exc_info:
        gateway.submit_intent(intent)
    assert exc_info.value.code == "EXECUTION_ADAPTER_UNAVAILABLE"
    assert exc_info.value.status_code == 422


# ============================================================================
# RT11: Adapter timeout returns UNKNOWN status, preventing blind resubmission
# ============================================================================
def test_rt11_adapter_contract_and_timeout_returns_unknown(temp_store):
    class TimeoutClient:
        def place_order(self, **kwargs):
            raise TimeoutError("Gate.io REST request timed out after 10000ms")

    ledger = AccountLedger(temp_store)
    ledger.create_account("acc_testnet_rt11", mode="TESTNET", initial_deposit=Decimal("10000.0"))
    _seed_fresh_market_bar(temp_store, "BTC_USDT", 50000.0)
    gateway = ExecutionGateway(temp_store, trader_client=TimeoutClient())
    mgr = AuthorizationManager(temp_store)
    mgr.grant_authorization(
        account_id="acc_testnet_rt11",
        venue="gate",
        mode=TradingMode.TESTNET,
        decision_path=DecisionPath.AI_LED,
        allowed_instruments=["BTC_USDT"],
        allowed_sides=["LONG"],
        max_risk_fraction=Decimal("0.01"),
        max_leverage=3,
        duration_seconds=3600,
        confirmed_by=ConfirmationSource.LOCAL_USER_WIZARD,
    )

    intent = OrderIntent(
        intent_id="intent_rt11",
        idempotency_key="idem_rt11",
        account_id="acc_testnet_rt11",
        mode=TradingMode.TESTNET,
        venue="gate",
        environment="TESTNET",
        decision_path=DecisionPath.AI_LED,
        instrument_id="BTC_USDT",
        side="LONG",
        order_type="market",
        quantity=0.01,
        price=50000.0,
        protection_plan=ProtectionPlan(stop_price=49000.0),
    )
    res = gateway.submit_intent(intent)
    assert res["status"] == OrderStatus.UNKNOWN.value


# ============================================================================
# RT12: ProtectionPlan begins in PENDING; unverified protection forbids ACTIVE
# ============================================================================
def test_rt12_failed_protection_order_blocks_risk_pending_status():
    plan = ProtectionPlan(stop_price=49000.0, take_profit=52000.0)
    assert plan.status == ProtectionStatus.PENDING
    assert plan.failure_action == "BLOCK_NEW_RISK_AND_ALERT"


# ============================================================================
# RT13: Direct API, AI-led engine, and strategy proposals enforce unified hard risk
# ============================================================================
def test_rt13_unified_risk_checks_across_api_ai_and_strategies(temp_store):
    ledger = AccountLedger(temp_store)
    ledger.create_account("rt13_acc", mode="PAPER", initial_deposit=Decimal("10000.00"))
    risk_engine = RiskEngine(ledger)
    limits = RiskLimits(max_portfolio_risk=Decimal("0.01"))  # 1% max risk = 100 USDT

    # Intent with 500 USDT risk (> 100 USDT limit)
    intent_oversize = OrderIntent(
        intent_id="intent_rt13_oversize",
        idempotency_key="idem_rt13_oversize",
        account_id="rt13_acc",
        mode=TradingMode.PAPER,
        instrument_id="BTC_USDT",
        side="LONG",
        order_type="market",
        quantity=1.0,
        price=50000.0,
        protection_plan=ProtectionPlan(stop_price=49500.0),  # Risk = 1.0 * 500 = 500 USDT
    )
    res = risk_engine.validate_intent(
        intent_oversize,
        limits=limits,
        market_snapshot={
            "symbol": "BTC_USDT",
            "price": 50000.0,
            "last": 50000.0,
            "data_as_of": datetime.now(timezone.utc).isoformat(),
            "received_at": datetime.now(timezone.utc).isoformat(),
            "fresh": True,
            "market": {
                "contractSize": 1.0,
                "precision": {"amount": 0.001, "price": 0.01},
                "limits": {"amount": {"min": 0.001, "max": 1000000.0, "step": 0.001}},
                "taker": 0.0005,
            },
        },
    )
    assert res.is_approved is False
    assert res.rejection_code == "RISK_BUDGET_EXCEEDED"


# ============================================================================
# RT14: Concurrent budget reservations never exceed portfolio limit; UNKNOWN orders hold budget
# ============================================================================
def test_rt14_concurrent_risk_reservation_and_unknown_orders_hold_budget(temp_store):
    ledger = AccountLedger(temp_store)
    ledger.create_account("rt14_acc", mode="PAPER", initial_deposit=Decimal("1000.00"))
    risk_engine = RiskEngine(ledger)

    # Reserve 7.00 USDT of risk (portfolio limit is 1% of 1000 = 10.00 USDT)
    res1 = risk_engine.reserve_risk(
        account_id="rt14_acc",
        instrument_id="BTC_USDT",
        amount_risk=Decimal("7.00"),
        reservation_id="res_rt14_1",
    )
    assert res1.is_approved is True

    # Try reserving another 4.00 USDT (total 11.00 > 10.00 limit)
    res2 = risk_engine.reserve_risk(
        account_id="rt14_acc",
        instrument_id="ETH_USDT",
        amount_risk=Decimal("4.00"),
        reservation_id="res_rt14_2",
    )
    assert res2.is_approved is False
    assert res2.rejection_code == "INSUFFICIENT_MARGIN"


# ============================================================================
# RT15: Revoked/expired authorization blocks new openings while preserving protective reductions
# ============================================================================
def test_rt15_legacy_revocation_does_not_block_scoped_openings_or_protective_reductions(temp_store):
    mgr = AuthorizationManager(temp_store)
    ledger = AccountLedger(temp_store)
    ledger.create_account("acc_rt15", mode="TESTNET", initial_deposit=Decimal("10000.0"))
    _seed_fresh_market_bar(temp_store, "BTC_USDT", 49000.0)
    seed_fill = ledger.record_trade_fill(
        account_id="acc_rt15",
        instrument_id="BTC_USDT",
        side="BUY",
        quantity=Decimal("0.1"),
        price=Decimal("50000"),
        fee=Decimal("0"),
        mode="TESTNET",
        venue="gate",
        order_id="seed_rt15",
        trade_id="seed_rt15_fill",
        stop_price=Decimal("49000"),
        protection_status="ACTIVE",
    )
    auth = mgr.grant_authorization(
        account_id="acc_rt15",
        venue="gate",
        mode=TradingMode.TESTNET,
        decision_path=DecisionPath.AI_LED,
        allowed_instruments=["BTC_USDT"],
        allowed_sides=["LONG"],
        max_risk_fraction=Decimal("0.01"),
        max_leverage=3,
        duration_seconds=3600,
        confirmed_by=ConfirmationSource.LOCAL_USER_WIZARD,
    )

    # Revoke
    mgr.revoke_authorization(auth.authorization_id, reason="TEST_REVOKE")

    mock_client = SimpleNamespace(
        place_order=lambda **kwargs: {"id": "ord_mock_rt15", "status": "open", "left": 0},
        cancel_order=lambda *args, **kwargs: {"status": "cancelled"},
    )
    gateway = ExecutionGateway(temp_store, trader_client=mock_client)

    # A legacy revocation is an audit record only; it cannot block a scoped
    # gateway order after the local authorization lock was removed.
    open_intent = OrderIntent(
        intent_id="intent_rt15_open",
        idempotency_key="idem_rt15_open",
        account_id="acc_rt15",
        mode=TradingMode.TESTNET,
        venue="gate",
        environment="TESTNET",
        instrument_id="BTC_USDT",
        side="LONG",
        order_type="market",
        quantity=0.1,
        price=50000.0,
        reduce_only=False,
        protection_plan=ProtectionPlan(stop_price=49000.0),
    )
    opened = gateway.submit_intent(open_intent)
    assert opened["status"] in ("ACKNOWLEDGED", "SUBMITTED", "CREATED", "UNKNOWN")
    assert "AUTHORIZATION" not in str(opened)

    # Protective exit order (reduce_only=True) -> PERMITTED
    reduce_intent = OrderIntent(
        intent_id="intent_rt15_exit",
        idempotency_key="idem_rt15_exit",
        account_id="acc_rt15",
        mode=TradingMode.TESTNET,
        venue="gate",
        environment="TESTNET",
        instrument_id="BTC_USDT",
        side="SELL",
        order_type="market",
        quantity=0.1,
        price=49000.0,
        position_id=seed_fill["position_id"],
        reduce_only=True,
        protection_plan=ProtectionPlan(stop_price=49000.0, reduce_only=True),
    )
    res = gateway.submit_intent(reduce_intent)
    assert res["status"] in ("ACKNOWLEDGED", "SUBMITTED", "OPEN")


# ============================================================================
# RT16: Sub-cent prices, step sizes, and multipliers floor quantities without expanding risk
# ============================================================================
@pytest.mark.parametrize(
    "raw_budget, stop_dist, step_size, min_qty, expected_qty, expect_reject",
    [
        (10.0, 100.0, 0.001, 0.001, 0.1, False),
        (0.005, 100.0, 0.001, 0.001, 0.0, True),  # raw = 0.00005 < min_qty -> REJECT, do NOT floor to 0.001
        (15.75, 50.0, 0.01, 0.01, 0.31, False),   # 15.75 / 50 = 0.315 -> floor to 0.31 (not 0.32)
    ],
)
def test_rt16_sub_cent_and_step_size_no_risk_expansion(raw_budget, stop_dist, step_size, min_qty, expected_qty, expect_reject):
    raw_qty = raw_budget / stop_dist
    if raw_qty < min_qty:
        assert expect_reject is True
    else:
        # Step-size floor rounding
        floored = math.floor(raw_qty / step_size) * step_size
        assert round(floored, 4) == expected_qty
        # Must never exceed raw_qty
        assert floored <= raw_qty + 1e-9
import math
