"""Focused repair-v1.2 regression evidence.

These tests exercise the production boundaries that the older acceptance
fixtures intentionally model with fakes: account-scoped gateway risk,
reduce-only concurrency, UNKNOWN reconciliation, the real AI coordinator,
runtime protection continuity, and API scope/runtime failure behavior.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.v2 import router_for
from core.monitoring_runtime import MonitoringRuntime
from core.providers import Bar
from core.realtime import RealtimeConnectionState
from core.storage import SQLiteStore
from core.trading.ai_session_coordinator import AISessionCoordinator
from core.trading.authorization import AuthorizationManager, ConfirmationSource
from core.trading.execution_gateway import (
    DecisionPath,
    ExecutionGateway,
    GatewayError,
    OrderIntent,
    ProtectionPlan,
    ProtectionStatus,
    TradingMode,
)
from core.trading.ledger import AccountLedger
from core.trading.position_guardian import PositionGuardian
from core.trading.session_manager import SessionManager
from core.trading.testnet_capabilities import TestnetCapabilityService


@pytest.fixture
def repair_store():
    root = Path(tempfile.mkdtemp(prefix="aima-repair-v12-"))
    store = SQLiteStore(root / "repair.sqlite3")
    store.initialize()
    try:
        yield store
    finally:
        try:
            store.close()
        except Exception:
            pass
        shutil.rmtree(root, ignore_errors=True)


def fresh_market(symbol: str, price: float, *, now: datetime | None = None, contract_size: float = 1.0) -> dict:
    point = now or datetime.now(timezone.utc)
    return {
        "symbol": symbol,
        "price": price,
        "last": price,
        "data_as_of": point.isoformat(),
        "received_at": point.isoformat(),
        "fresh": True,
        "slippage": 0.001,
        "market": {
            "contractSize": contract_size,
            "precision": {"amount": 0.001, "price": 0.01},
            "limits": {"amount": {"min": 0.001, "max": 1_000_000.0, "step": 0.001}},
            "taker": 0.0005,
        },
    }


def make_intent(
    *,
    intent_id: str,
    account_id: str,
    side: str,
    quantity: float,
    price: float | None = 100.0,
    reduce_only: bool = False,
    position_id: str | None = None,
    mode: TradingMode = TradingMode.PAPER,
    venue: str = "simulated",
    stop: float | None = 90.0,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        idempotency_key=f"idem:{intent_id}",
        account_id=account_id,
        mode=mode,
        environment=mode.value,
        venue=venue,
        decision_path=DecisionPath.AI_LED if mode is TradingMode.TESTNET else DecisionPath.STRATEGY_DRIVEN,
        instrument_id="BTCUSDT",
        side=side,
        order_type="market",
        quantity=quantity,
        price=price,
        leverage=1,
        protection_plan=ProtectionPlan(stop_price=stop) if stop is not None and not reduce_only else None,
        reduce_only=reduce_only,
        position_id=position_id,
    )


def test_gateway_requires_fresh_quote_and_unified_budget(repair_store):
    ledger = AccountLedger(repair_store)
    ledger.create_account("audit_acc", mode="PAPER", initial_deposit=Decimal("100.00"))
    gateway = ExecutionGateway(repair_store, ledger=ledger)

    no_market = make_intent(
        intent_id="no_market",
        account_id="audit_acc",
        side="LONG",
        quantity=1_000_000.0,
        price=100.0,
        stop=90.0,
    )
    with pytest.raises(GatewayError) as missing_market:
        gateway.submit_intent(no_market)
    assert missing_market.value.code == "MARKET_DATA_UNAVAILABLE"

    over_budget = make_intent(
        intent_id="over_budget",
        account_id="audit_acc",
        side="LONG",
        quantity=1_000_000.0,
        price=100.0,
        stop=90.0,
    )
    with pytest.raises(GatewayError) as rejected:
        gateway.submit_intent(over_budget, market_snapshot=fresh_market("BTCUSDT", 101.0))
    assert rejected.value.code in {"RISK_BUDGET_EXCEEDED", "SIZE_LIMIT", "RISK_RESERVATION_FAILED"}
    assert ledger.get_open_positions("audit_acc") == []

    executable = make_intent(
        intent_id="fresh_fill",
        account_id="audit_acc",
        side="LONG",
        quantity=0.001,
        price=100.0,
        stop=99.0,
    )
    receipt = gateway.submit_intent(executable, market_snapshot=fresh_market("BTCUSDT", 101.0))
    assert receipt["status"] == "FILLED"
    assert receipt["average_price"] > 101.0
    assert receipt["average_price"] != 100.0
    assert receipt["protection_status"] == ProtectionStatus.ACTIVE.value
    assert ledger.get_open_positions("audit_acc")[0]["entry"] == receipt["average_price"]


def test_legacy_authorization_revocation_does_not_fence_scoped_execution(repair_store, monkeypatch):
    ledger = AccountLedger(repair_store)
    ledger.create_account("fenced_account", mode="PAPER", initial_deposit=Decimal("10000.00"))
    auth = AuthorizationManager(repair_store).grant_authorization(
        account_id="fenced_account",
        venue="simulated",
        mode=TradingMode.PAPER,
        decision_path=DecisionPath.STRATEGY_DRIVEN,
        allowed_instruments=["BTCUSDT"],
        allowed_sides=["LONG"],
        max_risk_fraction=Decimal("0.0025"),
        max_leverage=3,
        duration_seconds=3600,
        confirmed_by=ConfirmationSource.LOCAL_USER_WIZARD,
    )
    gateway = ExecutionGateway(repair_store, ledger=ledger)
    original_evaluate = gateway.risk_engine.evaluate_intent

    def evaluate_then_revoke(*args, **kwargs):
        decision = original_evaluate(*args, **kwargs)
        AuthorizationManager(repair_store).revoke_authorization(auth.authorization_id)
        return decision

    monkeypatch.setattr(gateway.risk_engine, "evaluate_intent", evaluate_then_revoke)
    intent = make_intent(
        intent_id="fenced-open",
        account_id="fenced_account",
        side="LONG",
        quantity=0.001,
        price=100.0,
        stop=99.0,
    )
    intent.authorization_id = auth.authorization_id
    intent.authorization_version = auth.version
    receipt = gateway.submit_intent(intent, market_snapshot=fresh_market("BTCUSDT", 100.0))
    assert receipt["status"] == "FILLED"
    assert len(ledger.get_open_positions("fenced_account")) == 1
    with repair_store._connect() as db:
        assert db.execute("SELECT status FROM risk_reservations WHERE reservation_id=?", ("res_fenced-open",)).fetchone()[0] == "COMMITTED"


def test_reduce_only_direction_scope_and_concurrent_exit_are_safe(repair_store):
    ledger = AccountLedger(repair_store)
    ledger.create_account("account_a", mode="PAPER", initial_deposit=Decimal("1000.00"))
    ledger.create_account("account_b", mode="PAPER", initial_deposit=Decimal("1000.00"))
    for account_id in ("account_a", "account_b"):
        ledger.record_trade_fill(
            account_id=account_id,
            instrument_id="BTCUSDT",
            side="LONG",
            quantity=Decimal("2"),
            price=Decimal("100"),
            fee=Decimal("0"),
            mode="PAPER",
            venue="simulated",
            order_id=f"seed-{account_id}",
            trade_id=f"fill-{account_id}",
            protection_status=ProtectionStatus.ACTIVE.value,
        )

    gateway = ExecutionGateway(repair_store, ledger=ledger)
    wrong_direction = make_intent(
        intent_id="wrong_reduce_direction",
        account_id="account_a",
        side="BUY",
        quantity=1.0,
        reduce_only=True,
        stop=None,
    )
    with pytest.raises(GatewayError) as wrong:
        gateway.submit_intent(wrong_direction, market_snapshot=fresh_market("BTCUSDT", 101.0))
    assert wrong.value.code == "REDUCE_ONLY_POSITION_NOT_FOUND"
    assert ledger.get_open_positions("account_a")[0]["remaining_contracts"] == 2.0

    def close_once(index: int) -> dict:
        intent = make_intent(
            intent_id=f"close-{index}",
            account_id="account_a",
            side="SELL",
            quantity=1.0,
            reduce_only=True,
            stop=None,
        )
        return gateway.submit_intent(intent, market_snapshot=fresh_market("BTCUSDT", 101.0))

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(close_once, (1, 2)))
    assert [item["status"] for item in receipts] == ["FILLED", "FILLED"]
    assert ledger.get_open_positions("account_a") == []
    # The identical symbol in another account remains untouched.
    account_b_positions = ledger.get_open_positions("account_b")
    assert len(account_b_positions) == 1
    assert account_b_positions[0]["remaining_contracts"] == 2.0


def test_unknown_reconciliation_records_concrete_fill_once(repair_store):
    class TimeoutThenFilledAdapter:
        def __init__(self):
            self.fetch_calls = []

        def place_order(self, **_kwargs):
            raise TimeoutError("adapter request timed out")

        def fetch_order(self, order_id, symbol):
            self.fetch_calls.append((order_id, symbol))
            return {
                "id": "remote-order-1",
                "status": "filled",
                "filled": 0.01,
                "amount": 0.01,
                "average": 100.0,
                "fee": 0.05,
                "protection_verified": True,
            }

    ledger = AccountLedger(repair_store)
    ledger.create_account("testnet_account", mode="TESTNET", initial_deposit=Decimal("10000.00"), config={"venue": "gate"})
    auth = AuthorizationManager(repair_store).grant_authorization(
        account_id="testnet_account",
        venue="gate",
        mode=TradingMode.TESTNET,
        decision_path=DecisionPath.AI_LED,
        allowed_instruments=["BTCUSDT"],
        allowed_sides=["LONG"],
        max_risk_fraction=Decimal("0.0025"),
        max_leverage=3,
        duration_seconds=3600,
        confirmed_by=ConfirmationSource.LOCAL_USER_WIZARD,
    )
    adapter = TimeoutThenFilledAdapter()
    gateway = ExecutionGateway(repair_store, trader_client=adapter, ledger=ledger)
    intent = make_intent(
        intent_id="unknown-fill",
        account_id="testnet_account",
        side="LONG",
        quantity=0.01,
        price=100.0,
        mode=TradingMode.TESTNET,
        venue="gate",
        stop=95.0,
    )
    intent.authorization_id = auth.authorization_id
    intent.authorization_version = auth.version
    first = gateway.submit_intent(intent, market_snapshot=fresh_market("BTCUSDT", 100.0))
    assert first["status"] == "UNKNOWN"
    with repair_store._connect() as db:
        assert db.execute("SELECT status FROM risk_reservations").fetchone()[0] == "PENDING"

    result = gateway.reconcile_in_flight_orders("testnet_account", TradingMode.TESTNET)
    assert result[0]["reconciled"] is True
    assert result[0]["reconciled_status"] == "FILLED"
    assert ledger.get_open_positions("testnet_account")[0]["protection_status"] == ProtectionStatus.ACTIVE.value
    with repair_store._connect() as db:
        assert db.execute("SELECT status FROM risk_reservations").fetchone()[0] == "COMMITTED"
        assert db.execute("SELECT COUNT(*) FROM trade_fills WHERE account_id='testnet_account'").fetchone()[0] == 1
    # Reconciliation is idempotent after the order leaves the in-flight set.
    assert gateway.reconcile_in_flight_orders("testnet_account", TradingMode.TESTNET) == []
    assert adapter.fetch_calls


def test_production_ai_coordinator_uses_qwen9b_and_persists_cycles(repair_store):
    now = datetime.now(timezone.utc)

    class FakeQwen9B:
        provider_name = "fake_qwen_for_local_trace"
        # The institutional coordinator requires an actual model-weight
        # digest before calibration can open an AI session.  This fixture
        # is deterministic evidence for the test adapter, not a model
        # name or a production credential.
        weight_digest = "a" * 64

        def __init__(self):
            self.calls = []

        def health(self):
            return {"available": True, "model_available": True, "models": ["qwen3.5:9b"], "model_id": "qwen3.5:9b"}

        def generate_json(self, _messages, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("prompt_version") == "ai_calibration_operation_profile_v1":
                return {
                    "risk_regime": "fixture",
                    "entry_style": "CONSERVATIVE",
                    "order_preference": "AUTO",
                    "max_concurrent_positions": 1,
                    "notes_zh": "deterministic calibration fixture",
                }
            return {"action": "WAIT", "instrument_id": "BTCUSDT", "reason": "local coordinator trace"}

    ledger = AccountLedger(repair_store)
    ledger.create_account("ai_account", mode="PAPER", initial_deposit=Decimal("10000.00"))
    AuthorizationManager(repair_store).grant_authorization(
        account_id="ai_account",
        venue="simulated",
        mode=TradingMode.PAPER,
        decision_path=DecisionPath.AI_LED,
        allowed_instruments=["BTCUSDT"],
        allowed_sides=["LONG", "SHORT"],
        max_risk_fraction=Decimal("0.0025"),
        max_leverage=3,
        duration_seconds=3600,
        confirmed_by=ConfirmationSource.LOCAL_USER_WIZARD,
    )
    repair_store.upsert_market_bars(
        "BTCUSDT",
        "15m",
        [Bar(now - timedelta(minutes=15), 100, 101, 99, 100, 10)],
        provider="local_repair_fixture",
        data_as_of=now - timedelta(seconds=1),
        now=now,
    )
    session_manager = SessionManager(repair_store)
    session_manager.start()
    provider = FakeQwen9B()
    coordinator = AISessionCoordinator(
        store=repair_store,
        service=SimpleNamespace(llm_provider=provider),
        session_manager=session_manager,
        ledger=ledger,
        guardian=PositionGuardian(repair_store, ledger),
        execution_gateway=ExecutionGateway(repair_store, ledger=ledger),
        model_provider=provider,
        clock=lambda: datetime.now(timezone.utc),
        cycle_interval_seconds=60,
        calibration_min_bars=1,
    )
    try:
        coordinator.start(account_id="ai_account")
        # Startup is deliberately no-catch-up: the first explicit cycle
        # calibrates, and the following cycle consumes that ready profile.
        first = coordinator.run_cycle_once()
        assert first.reason == "CALIBRATION_NOT_READY" or coordinator.status()["calibration_state"] == "READY"
        coordinator.run_cycle_once()
        assert len(provider.calls) >= 2
        assert all(call["model_name"] == "qwen3.5:9b" for call in provider.calls)
        with repair_store._connect() as db:
            rows = db.execute("SELECT account_id, session_id, generation, authorization_id, market_snapshot_hash FROM ai_led_cycles WHERE account_id='ai_account'").fetchall()
        assert len(rows) >= 2
        assert all(row["authorization_id"] is None for row in rows)
        assert all(row["market_snapshot_hash"] for row in rows)
        assert coordinator.status()["model"]["required_model"] == "qwen3.5:9b"
    finally:
        coordinator.stop()
        coordinator.close()


def test_runtime_terminate_keeps_scoped_protection_stream(repair_store):
    now = datetime.now(timezone.utc)
    ledger = AccountLedger(repair_store)
    ledger.create_account("runtime_account", mode="PAPER", initial_deposit=Decimal("1000.00"))
    ledger.record_trade_fill(
        account_id="runtime_account",
        instrument_id="BTCUSDT",
        side="LONG",
        quantity=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("0"),
        mode="PAPER",
        venue="simulated",
        order_id="runtime-seed",
        trade_id="runtime-seed-fill",
        protection_status=ProtectionStatus.ACTIVE.value,
    )

    class SilentService:
        strategy_mode = False
        max_symbols = 5
        account_id = None

        def list_monitoring_policies(self, **_kwargs):
            return []

        def run(self, **_kwargs):
            raise AssertionError("no discovery cycle should be required for protection continuity")

    class ProtectionStream:
        def __init__(self, symbols):
            self.symbols = tuple(symbols)
            self.stop_event = threading.Event()
            self.stop_called = False

        def run_forever(self, *, on_bar, on_state=None):
            del on_bar
            if on_state:
                on_state(RealtimeConnectionState("connected", "repair_stream", self.symbols, "15m", 0, now))
            self.stop_event.wait()

        def stop(self):
            self.stop_called = True
            self.stop_event.set()

    created_streams = []

    def stream_factory(symbols):
        stream = ProtectionStream(symbols)
        created_streams.append(stream)
        return stream

    runtime = MonitoringRuntime(
        store=repair_store,
        service=SilentService(),
        stream_factory=stream_factory,
        clock=lambda: now,
        poll_interval_seconds=0.05,
    )
    try:
        runtime.start(account_id="runtime_account")
        deadline = time.monotonic() + 2
        while (not created_streams or not runtime.status()["guardian"]["status"] in {"HEALTHY", "DEGRADED"}) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert created_streams
        stopped = runtime.stop(clear_resume=True)
        assert stopped["state"] == "stopped"
        assert stopped["active_symbols"] == ["BTCUSDT"]
        assert stopped["last_reason"] == "explicit_stop_protection_continues"
        assert runtime.guardian.is_active()
        assert not created_streams[-1].stop_called
    finally:
        if created_streams:
            created_streams[-1].stop()
        runtime.guardian.stop()
        runtime._stop_lease_heartbeat()
        runtime.runtime_lease.release("monitoring_runtime", runtime.holder_id)
        runtime.ai_coordinator.close()


def test_api_scope_and_runtime_unavailable_are_explicit(repair_store):
    ledger = AccountLedger(repair_store)
    ledger.create_account("api_a", mode="PAPER", initial_deposit=Decimal("1000.00"))
    ledger.create_account("api_b", mode="PAPER", initial_deposit=Decimal("1000.00"))
    for account_id in ("api_a", "api_b"):
        ledger.record_trade_fill(
            account_id=account_id,
            instrument_id="BTCUSDT",
            side="LONG",
            quantity=Decimal("1"),
            price=Decimal("100"),
            fee=Decimal("0"),
            mode="PAPER",
            venue="simulated",
            order_id=f"api-{account_id}",
            trade_id=f"api-fill-{account_id}",
            protection_status=ProtectionStatus.ACTIVE.value,
        )
    app = FastAPI()
    app.include_router(router_for(lambda: repair_store, lambda: None, lambda: None))
    with TestClient(app) as client:
        workspace = client.get("/v2/workspace")
        assert workspace.status_code == 200
        assert workspace.json()["scope_required"] is True
        assert workspace.json()["positions"] == []
        scoped = client.get("/v2/positions", params={"account_id": "api_a"})
        assert scoped.status_code == 200
        assert {item["payload"]["account_id"] for item in scoped.json()["positions"]} == {"api_a"}
        denied = client.post("/v2/ai-session/start", params={"account_id": "api_a"})
        assert denied.status_code == 503
        assert "AI_RUNTIME_UNAVAILABLE" in denied.text


def test_testnet_capability_is_not_run_without_adapter_and_verified_with_probe():
    service = TestnetCapabilityService()
    missing = service.get_capabilities()
    assert missing["status"] == "NOT_RUN"
    assert missing["source"] == "NOT_RUN_NO_ADAPTER"
    assert missing["has_native_tpsl"] is None

    class Adapter:
        def probe_capabilities(self):
            return {
                "has_native_tpsl": True,
                "supports_batch_orders": True,
                "supports_amend": True,
                "funding_interval_hours": 8,
                "rate_limit_per_minute": 100,
                "fee_schedule": {"maker": 0.0002, "taker": 0.0005},
                "min_order_sizes": {"BTC_USDT": 0.001},
            }

    verified = TestnetCapabilityService(adapter=Adapter()).get_capabilities()
    assert verified["status"] == "AVAILABLE"
    assert verified["source"] == "ADAPTER_PROBE"
    assert verified["observed_at"]
    assert verified["capability_claim_valid"] is True


def test_paper_limit_reconciliation_and_cancel_keep_local_budget_consistent(repair_store):
    ledger = AccountLedger(repair_store)
    ledger.create_account("paper_limit_account", mode="PAPER", initial_deposit=Decimal("10000.00"))
    gateway = ExecutionGateway(repair_store, ledger=ledger)

    resting = make_intent(
        intent_id="paper-resting-limit",
        account_id="paper_limit_account",
        side="LONG",
        quantity=1.0,
        price=100.0,
        stop=90.0,
    )
    resting.order_type = "limit"
    first = gateway.submit_intent(
        resting,
        market_snapshot={**fresh_market("BTCUSDT", 100.0), "ask": 100.05},
    )
    assert first["status"] == "ACKNOWLEDGED"
    with repair_store._connect() as db:
        assert db.execute("SELECT status FROM risk_reservations").fetchone()[0] == "PENDING"

    now = datetime.now(timezone.utc)
    repair_store.upsert_market_bars(
        "BTCUSDT",
        "15m",
        [Bar(now, 99.0, 99.5, 98.5, 99.0, 10.0)],
        provider="paper_matching_fixture",
        data_as_of=now,
        now=now,
    )
    matched = gateway.reconcile_in_flight_orders("paper_limit_account", TradingMode.PAPER)
    assert matched[0]["reconciled_status"] == "FILLED"
    assert ledger.get_open_positions("paper_limit_account")[0]["remaining_contracts"] == 1.0
    with repair_store._connect() as db:
        assert db.execute("SELECT status FROM risk_reservations").fetchone()[0] == "COMMITTED"

    cancel_intent = make_intent(
        intent_id="paper-cancel-limit",
        account_id="paper_limit_account",
        side="LONG",
        quantity=0.1,
        price=80.0,
        stop=90.0,
    )
    cancel_intent.order_type = "limit"
    cancel_first = gateway.submit_intent(
        cancel_intent,
        market_snapshot={**fresh_market("BTCUSDT", 100.0), "ask": 100.05},
    )
    assert cancel_first["status"] == "ACKNOWLEDGED"
    canceled = gateway.cancel_intent("paper-cancel-limit")
    assert canceled["status"] == "CANCELED"
    with repair_store._connect() as db:
        rows = db.execute("SELECT reservation_id, status FROM risk_reservations").fetchall()
        statuses = {row[0]: row[1] for row in rows}
        assert statuses["res_paper-resting-limit"] == "COMMITTED"
        assert statuses["res_paper-cancel-limit"] == "RELEASED"


def test_scoped_emergency_stop_does_not_mutate_global_subscription_policy(repair_store):
    ledger = AccountLedger(repair_store)
    ledger.create_account("emergency_a", mode="PAPER", initial_deposit=Decimal("1000.00"))
    ledger.create_account("emergency_b", mode="PAPER", initial_deposit=Decimal("1000.00"))
    with repair_store._connect() as db:
        db.execute(
            "INSERT INTO strategy_subscriptions(symbol, strategy_id, enabled, params_json, updated_at) VALUES(?,?,?,?,?)",
            ("BTCUSDT", "ema_trend", 1, "{}", datetime.now(timezone.utc).isoformat()),
        )

    class RuntimeStub:
        account_id = "emergency_a"

        def stop(self):
            return {"state": "stopped", "active": False, "account_id": self.account_id}

    app = FastAPI()
    app.include_router(router_for(lambda: repair_store, lambda: RuntimeStub(), lambda: None))
    with TestClient(app) as client:
        response = client.post("/v2/emergency-stop", params={"account_id": "emergency_a"})
        assert response.status_code == 200
        body = response.json()
        assert body["account_id"] == "emergency_a"
        assert body["strategy_policy"]["status"] == "NOT_MUTATED"
        denied = client.post("/v2/emergency-stop", params={"account_id": "emergency_b"})
        assert denied.status_code == 409

    with repair_store._connect() as db:
        assert db.execute(
            "SELECT enabled FROM strategy_subscriptions WHERE symbol='BTCUSDT' AND strategy_id='ema_trend'"
        ).fetchone()[0] == 1
        event = db.execute(
            "SELECT payload_json FROM simulation_events WHERE position_id=? ORDER BY event_id DESC LIMIT 1",
            ("runtime:emergency_a",),
        ).fetchone()
    assert json.loads(event[0])["account_id"] == "emergency_a"


def test_api_testnet_capabilities_are_account_scoped_and_unverified_without_adapter(repair_store):
    ledger = AccountLedger(repair_store)
    ledger.create_account("cap_testnet", mode="TESTNET", initial_deposit=Decimal("1000.00"), config={"venue": "gate"})
    ledger.create_account("cap_paper", mode="PAPER", initial_deposit=Decimal("1000.00"))
    app = FastAPI()
    app.include_router(router_for(lambda: repair_store, lambda: None, lambda: None))
    with TestClient(app) as client:
        testnet = client.get("/v2/testnet/capabilities", params={"account_id": "cap_testnet"})
        assert testnet.status_code == 200
        testnet_body = testnet.json()
        assert testnet_body["account_id"] == "cap_testnet"
        assert testnet_body["scope_required"] is False
        assert testnet_body["status"] == "NOT_RUN"
        assert testnet_body["source"] == "NOT_RUN_NO_ADAPTER"
        paper = client.get("/v2/testnet/capabilities", params={"account_id": "cap_paper"})
        assert paper.status_code == 200
        assert paper.json()["status"] == "NOT_RUN_ENVIRONMENT_MISMATCH"
        assert paper.json()["capability_claim_valid"] is False


def test_replayed_fill_with_empty_legacy_payload_returns_auditable_receipt(repair_store):
    ledger = AccountLedger(repair_store)
    ledger.create_account("replay_account", mode="PAPER", initial_deposit=Decimal("1000.00"))
    first = ledger.record_trade_fill(
        account_id="replay_account",
        instrument_id="BTCUSDT",
        side="LONG",
        quantity=Decimal("0.1"),
        price=Decimal("100"),
        fee=Decimal("0"),
        mode="PAPER",
        venue="simulated",
        order_id="replay-order",
        trade_id="replay-trade",
        protection_status=ProtectionStatus.ACTIVE.value,
    )
    with repair_store._connect() as db:
        db.execute(
            "UPDATE trade_fills SET payload_json='{}' WHERE account_id=? AND trade_id=?",
            ("replay_account", "replay-trade"),
        )
    replay = ledger.record_trade_fill(
        account_id="replay_account",
        instrument_id="BTCUSDT",
        side="LONG",
        quantity=Decimal("0.1"),
        price=Decimal("100"),
        fee=Decimal("0"),
        mode="PAPER",
        venue="simulated",
        order_id="replay-order",
        trade_id="replay-trade",
        protection_status=ProtectionStatus.ACTIVE.value,
    )
    assert replay["status"] == "RECORDED"
    assert replay["fill_id"] == first["fill_id"]
    assert replay["replayed"] is True
    assert len(ledger.get_open_positions("replay_account")) == 1


def test_fee_currency_requires_traceable_valuation_and_blocks_new_risk(repair_store):
    ledger = AccountLedger(repair_store)
    ledger.create_account("fee_currency_account", mode="PAPER", currency="USDT", initial_deposit=Decimal("1000.00"))

    ledger.record_event(
        "fee_currency_account",
        "FEE",
        Decimal("0.002"),
        currency="BTC",
        payload={"fx_rate": "50000", "fx_quote": "USDT_per_BTC"},
        event_id="fee-valued",
    )
    valued = ledger.get_snapshot("fee_currency_account")
    assert valued.cumulative_fees == Decimal("100.000")
    assert valued.wallet_balance == Decimal("900.000")
    assert valued.unvalued_fee_events == 0

    ledger.record_event(
        "fee_currency_account",
        "FEE",
        Decimal("0.1"),
        currency="ETH",
        payload={"source": "adapter_without_conversion"},
        event_id="fee-unvalued",
    )
    unvalued = ledger.get_snapshot("fee_currency_account")
    assert unvalued.cumulative_fees == Decimal("100.000")
    assert unvalued.unvalued_fee_events == 1
    assert unvalued.daily_loss_limit_reached is True
    assert ledger.reserve_risk(
        "fee_currency_account",
        "reservation-blocked-by-unvalued-fee",
        Decimal("1"),
        Decimal("1"),
        instrument_id="BTCUSDT",
        venue="simulated",
        mode="PAPER",
    ) is False


def test_reduce_only_requires_position_identity_for_multiple_scoped_positions(repair_store):
    ledger = AccountLedger(repair_store)
    ledger.create_account("multi_position_account", mode="PAPER", initial_deposit=Decimal("1000.00"))
    for suffix in ("one", "two"):
        ledger.record_trade_fill(
            account_id="multi_position_account",
            instrument_id="BTCUSDT",
            side="LONG",
            quantity=Decimal("0.1"),
            price=Decimal("100"),
            fee=Decimal("0"),
            mode="PAPER",
            venue="simulated",
            order_id=f"multi-order-{suffix}",
            trade_id=f"multi-trade-{suffix}",
            position_id=f"multi-position-{suffix}",
            protection_status=ProtectionStatus.ACTIVE.value,
        )

    gateway = ExecutionGateway(repair_store, ledger=ledger)
    reduce_without_identity = make_intent(
        intent_id="reduce-without-position-id",
        account_id="multi_position_account",
        side="SELL",
        quantity=0.1,
        reduce_only=True,
        stop=None,
    )
    with pytest.raises(GatewayError) as rejected:
        gateway.submit_intent(reduce_without_identity, market_snapshot=fresh_market("BTCUSDT", 101.0))
    assert rejected.value.code == "REDUCE_ONLY_POSITION_ID_REQUIRED"
    assert {p["position_id"] for p in ledger.get_open_positions("multi_position_account")} == {
        "multi-position-one",
        "multi-position-two",
    }


def test_legacy_authorization_portfolio_cap_does_not_override_unified_risk_engine(repair_store):
    ledger = AccountLedger(repair_store)
    ledger.create_account("auth_cap_account", mode="PAPER", initial_deposit=Decimal("10000.00"))
    auth = AuthorizationManager(repair_store).grant_authorization(
        account_id="auth_cap_account",
        venue="simulated",
        mode=TradingMode.PAPER,
        decision_path=DecisionPath.STRATEGY_DRIVEN,
        allowed_instruments=["BTCUSDT"],
        allowed_sides=["LONG"],
        max_risk_fraction=Decimal("0.01"),
        max_portfolio_risk_fraction=Decimal("0.0000001"),
        max_cluster_risk_fraction=Decimal("0.0000001"),
        max_leverage=3,
        duration_seconds=3600,
        confirmed_by=ConfirmationSource.LOCAL_USER_WIZARD,
    )
    intent = make_intent(
        intent_id="auth-portfolio-cap",
        account_id="auth_cap_account",
        side="LONG",
        quantity=0.001,
        price=100.0,
        stop=90.0,
    )
    intent.authorization_id = auth.authorization_id
    intent.authorization_version = auth.version
    receipt = ExecutionGateway(repair_store, ledger=ledger).submit_intent(
        intent,
        market_snapshot=fresh_market("BTCUSDT", 100.0),
    )
    assert receipt["status"] == "FILLED"
    assert len(ledger.get_open_positions("auth_cap_account")) == 1
