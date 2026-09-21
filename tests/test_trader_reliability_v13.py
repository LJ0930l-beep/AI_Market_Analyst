"""Local v1.3 trader-reliability acceptance tests.

These tests deliberately enter through the runtime, gateway, durable research
facade, or API router.  They do not call a real venue, place an external order,
or treat a mock model/adapter as external evidence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from threading import Event, Thread
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.v2 import router_for
from core.analysis.ai_trade_analytics import analyze_ai_trading_ledger
from core.news_revision import NewsRevisionRegistry
from core.monitoring import MonitoringRunResult
from core.monitoring_runtime import MonitoringRuntime
from core.providers.base import Bar
from core.storage import SQLiteStore
from core.trading.execution_gateway import (
    DecisionPath,
    ExecutionGateway,
    GatewayError,
    OrderIntent,
    ProtectionPlan,
    TradingMode,
)
from core.trading.authorization import AuthorizationManager
from core.trading.ledger import AccountLedger
from core.trading.position_guardian import PositionGuardian
from core.trading.session_manager import RuntimeLease
from core.trading.trader_capabilities import TraderCapabilityService, UNKNOWN


@pytest.fixture
def v13_store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "trader-reliability-v13.db")
    store.initialize()
    return store


class _QuietStream:
    """A local stream that never performs network I/O."""

    def __init__(self, symbols: tuple[str, ...]) -> None:
        self.symbols = symbols
        self._stop = Event()

    def run_forever(self, *, on_bar, on_state=None) -> None:
        del on_bar, on_state
        while not self._stop.wait(0.02):
            pass

    def stop(self) -> None:
        self._stop.set()


class _QuietMonitoringService:
    strategy_mode = False
    max_symbols = 10
    account_id = None

    def __init__(self) -> None:
        self.cancel_event = None

    def run(self, *, symbols=None, now=None) -> MonitoringRunResult:
        del symbols
        point = now or datetime.now(timezone.utc)
        return MonitoringRunResult(
            "DISABLED",
            point,
            tuple(),
            tuple(),
            {"max_symbols": self.max_symbols, "active_symbols": 0, "bounded": True},
        )


def _account(
    ledger: AccountLedger,
    account_id: str,
    *,
    mode: str = "PAPER",
    venue: str = "simulated",
    deposit: str = "10000",
) -> None:
    ledger.create_account(
        account_id,
        mode=mode,
        initial_deposit=Decimal(deposit),
        config={"venue": venue},
    )


def _seed_position(
    ledger: AccountLedger,
    account_id: str,
    *,
    mode: str = "PAPER",
    venue: str = "simulated",
    side: str = "LONG",
    position_id: str | None = None,
    price: str = "100",
    stop: float = 90.0,
    trade_id: str | None = None,
) -> dict:
    identity = position_id or f"pos_{account_id}_{trade_id or side.lower()}"
    fill_id = trade_id or f"fill_{account_id}_{identity}"
    return ledger.record_trade_fill(
        account_id=account_id,
        instrument_id="BTCUSDT",
        side=side,
        quantity=Decimal("1"),
        price=Decimal(price),
        fee=Decimal("0"),
        mode=mode,
        venue=venue,
        order_id=f"order_{fill_id}",
        trade_id=fill_id,
        position_id=identity,
        stop_price=stop,
        take_profit=110.0 if side == "LONG" else 90.0,
        protection_status="ACTIVE",
    )


def _bar(
    *,
    point: datetime | None = None,
    open_price: float = 89.0,
    high: float = 90.0,
    low: float = 80.0,
    close: float = 85.0,
) -> Bar:
    return Bar(
        timestamp=point or datetime.now(timezone.utc),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=1.0,
    )


def _fresh_market(point: datetime | None = None, price: float = 100.0) -> dict:
    point = point or datetime.now(timezone.utc)
    return {
        "symbol": "BTCUSDT",
        "price": price,
        "bid": price * 0.999,
        "ask": price * 1.001,
        "data_as_of": point.isoformat(),
        "received_at": point.isoformat(),
        "fresh": True,
        "freshness_status": "fresh",
        "stale_after_seconds": 120,
        "slippage": 0.001,
        "market": {
            "contractSize": 1.0,
            "precision": {"amount": 0.001, "price": 0.01},
            "limits": {"amount": {"min": 0.001, "max": 1_000_000.0, "step": 0.001}},
            "taker": 0.0005,
        },
    }


def _client(store: SQLiteStore, runtime=None) -> TestClient:
    app = FastAPI()
    app.include_router(
        router_for(
            get_store=lambda: store,
            get_runtime=lambda: runtime,
            get_translation=lambda: None,
        )
    )
    return TestClient(app)


def _insert_prediction_record(
    store: SQLiteStore,
    *,
    account_id: str,
    prediction_id: str,
    generated_at: datetime,
    pnl: float,
    data_as_of: datetime | None = None,
    strategy_version: str = "v1",
) -> None:
    context = {
        "account_id": account_id,
        "mode": "PAPER",
        "venue": "simulated",
        "strategy_id": "ema_trend",
        "strategy_version": strategy_version,
        "market_regime": "TREND",
    }
    prediction = {
        "prediction_id": prediction_id,
        "symbol": "BTCUSDT",
        "analysis_timeframe": "15m",
        "generated_at": generated_at.isoformat(),
        "action": "LONG",
        "entry_low": 100.0,
        "entry_high": 100.0,
        "stop": 98.0,
        "tp1": 104.0,
        "tp2": 106.0,
        "strategy_id": "ema_trend",
        "strategy_version": strategy_version,
        "account_id": account_id,
        "mode": "PAPER",
        "venue": "simulated",
        "data_as_of": (data_as_of or generated_at - timedelta(minutes=1)).isoformat(),
        "context": context,
    }
    settled_at = generated_at + timedelta(minutes=15)
    outcome = {
        "prediction_id": prediction_id,
        "settled_at": settled_at.isoformat(),
        "status": "TP1" if pnl > 0 else "STOP",
        "entry_price": 100.0,
        "quantity": 1.0,
        "risk": 2.0,
        "gross_pnl": pnl + 0.3,
        "net_pnl": pnl,
        "realized_r": pnl / 2.0,
        "fee": 0.2,
        "slippage_cost": 0.1,
        "notional": 100.0,
        "mfe_r": 2.0 if pnl > 0 else 0.1,
        "mae_r": 0.1 if pnl > 0 else 1.0,
    }
    with store._connect() as db:
        db.execute(
            """INSERT INTO predictions
               (prediction_id, symbol, generated_at, action, payload_json,
                data_as_of, context_json, source_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'live')""",
            (
                prediction_id,
                "BTCUSDT",
                generated_at.isoformat(),
                "LONG",
                json.dumps(prediction),
                prediction["data_as_of"],
                json.dumps(context),
            ),
        )
        db.execute(
            """INSERT INTO outcomes(prediction_id, settled_at, status, payload_json)
               VALUES (?, ?, ?, ?)""",
            (prediction_id, settled_at.isoformat(), outcome["status"], json.dumps(outcome)),
        )


def test_v13_a_guardian_only_paper_can_locally_close(v13_store: SQLiteStore) -> None:
    ledger = AccountLedger(v13_store)
    _account(ledger, "tn_a", mode="TESTNET", venue="gate", deposit="1000")
    _account(ledger, "paper_a", mode="PAPER", venue="simulated", deposit="1000")
    remote = _seed_position(
        ledger,
        "tn_a",
        mode="TESTNET",
        venue="gate",
        position_id="tn_btc",
        trade_id="tn_entry",
    )

    remote_guardian = PositionGuardian(v13_store, ledger)
    remote_guardian.set_account_scope("tn_a")
    remote_result = remote_guardian.process_bar("BTCUSDT", _bar())

    assert remote_result and remote_result[0]["remote"] is True
    assert remote_result[0]["status"] == "DEGRADED"
    assert remote_result[0]["price"] is None
    assert remote_result[0]["economic_fill_recorded"] is False
    assert ledger.get_open_positions("tn_a", venue="gate", mode="TESTNET")[0]["position_id"] == remote["position_id"]
    assert ledger.get_snapshot("tn_a").realized_pnl == Decimal("0")
    assert ledger.get_snapshot("tn_a").unverified_protection_count == 1

    paper = _seed_position(
        ledger,
        "paper_a",
        mode="PAPER",
        venue="simulated",
        position_id="paper_btc",
        trade_id="paper_entry",
    )
    paper_guardian = PositionGuardian(v13_store, ledger)
    paper_guardian.set_account_scope("paper_a")
    paper_result = paper_guardian.process_bar("BTCUSDT", _bar())

    assert paper_result and paper_result[0].get("remote", False) is not True
    assert paper_result[0]["price"] is not None
    assert ledger.get_open_positions("paper_a", venue="simulated", mode="PAPER") == []
    with v13_store._connect() as db:
        paper_row = db.execute(
            "SELECT status, account_id, mode, venue FROM simulated_positions WHERE position_id=?",
            (paper["position_id"],),
        ).fetchone()
    assert dict(paper_row) == {
        "status": "CLOSED",
        "account_id": "paper_a",
        "mode": "PAPER",
        "venue": "simulated",
    }


def test_v13_a_remote_protection_does_not_duplicate_unresolved_exit(v13_store: SQLiteStore) -> None:
    ledger = AccountLedger(v13_store)
    _account(ledger, "tn_adapter", mode="TESTNET", venue="gate", deposit="1000")
    position = _seed_position(
        ledger,
        "tn_adapter",
        mode="TESTNET",
        venue="gate",
        position_id="tn_adapter_btc",
        trade_id="tn_adapter_entry",
    )

    class PendingClient:
        def __init__(self) -> None:
            self.calls = 0

        def place_order(self, **kwargs):
            self.calls += 1
            return {"status": "open", "order_id": "remote-protection-1", "filled": 0, "amount": kwargs["amount"]}

    adapter = PendingClient()
    gateway = ExecutionGateway(v13_store, ledger=ledger, trader_client=adapter)
    guardian = PositionGuardian(v13_store, ledger, execution_gateway=gateway)
    guardian.set_account_scope("tn_adapter")
    first = guardian.process_bar("BTCUSDT", _bar())
    second = guardian.process_bar("BTCUSDT", _bar(point=datetime.now(timezone.utc) + timedelta(seconds=1)))

    assert first[0]["status"] == "ACKNOWLEDGED"
    assert second[0]["error_code"] == "REMOTE_PROTECTION_IN_FLIGHT"
    assert adapter.calls == 1
    assert ledger.get_open_positions("tn_adapter", venue="gate", mode="TESTNET")[0]["position_id"] == position["position_id"]


def test_v13_b_reduce_only_buy_cannot_open_or_reverse(v13_store: SQLiteStore) -> None:
    ledger = AccountLedger(v13_store)
    _account(ledger, "reduce_a")
    _seed_position(ledger, "reduce_a", position_id="long_one", trade_id="long_entry")
    gateway = ExecutionGateway(v13_store, ledger=ledger)
    intent = OrderIntent(
        intent_id="reduce-buy-intent",
        idempotency_key="reduce-buy-key",
        account_id="reduce_a",
        mode=TradingMode.PAPER,
        instrument_id="BTCUSDT",
        side="BUY",
        order_type="market",
        quantity=1.0,
        protection_plan=ProtectionPlan(stop_price=90.0, reduce_only=True),
        reduce_only=True,
        venue="simulated",
        environment="PAPER",
        position_id="long_one",
    )
    with pytest.raises(GatewayError) as error:
        gateway.submit_intent(intent, market_snapshot=_fresh_market())
    assert error.value.code == "REDUCE_ONLY_POSITION_NOT_FOUND"
    assert len(ledger.get_open_positions("reduce_a", venue="simulated", mode="PAPER")) == 1
    assert ledger.get_open_positions("reduce_a", venue="simulated", mode="PAPER")[0]["side"] == "LONG"


def test_v13_b_stop_then_rebind_is_blocked_until_account_a_recovers(v13_store: SQLiteStore) -> None:
    ledger = AccountLedger(v13_store)
    _account(ledger, "acct_a")
    _account(ledger, "acct_b")
    position = _seed_position(ledger, "acct_a", position_id="a_btc", trade_id="a_entry")
    runtime = MonitoringRuntime(
        store=v13_store,
        service=_QuietMonitoringService(),
        stream_factory=lambda symbols: _QuietStream(symbols),
        poll_interval_seconds=0.05,
        stream_join_timeout_seconds=0.1,
    )
    try:
        runtime.start(account_id="acct_a")
        stopped = runtime.stop()
        assert stopped["state"] == "stopped"
        assert stopped["last_reason"] == "explicit_stop_protection_continues"
        assert runtime.guardian.is_active()

        with pytest.raises(RuntimeError, match="RUNTIME_REBIND_BLOCKED"):
            runtime.start(account_id="acct_b")
        assert runtime.account_id == "acct_a"
        assert runtime.guardian.account_id == "acct_a"

        ledger.record_trade_fill(
            account_id="acct_a",
            instrument_id="BTCUSDT",
            side="SELL",
            quantity=Decimal("1"),
            price=Decimal("100"),
            fee=Decimal("0"),
            mode="PAPER",
            venue="simulated",
            order_id="a_recovery_order",
            trade_id="a_recovery_fill",
            position_id=position["position_id"],
            reduce_only=True,
        )
        final = runtime.stop()
        assert final["state"] == "stopped"
        assert final["guardian"]["open_positions_count"] == 0
    finally:
        runtime.stop()


def test_v13_c_stale_fencing_token_cannot_reach_gateway(v13_store: SQLiteStore, tmp_path: Path) -> None:
    ledger = AccountLedger(v13_store)
    _account(ledger, "fence_a")
    lease_path = tmp_path / "shared-runtime-lease.db"
    lease_a = RuntimeLease(str(lease_path), default_ttl_seconds=2)
    lease_b = RuntimeLease(str(lease_path), default_ttl_seconds=2)
    point = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert lease_a.acquire("monitoring_runtime", "holder-a", ttl_seconds=2, now=point)
    token_a = lease_a.fencing_token
    assert token_a == 1
    assert not lease_b.acquire("monitoring_runtime", "holder-b", ttl_seconds=2, now=point + timedelta(seconds=1))
    assert lease_b.acquire("monitoring_runtime", "holder-b", ttl_seconds=2, now=point + timedelta(seconds=3))
    token_b = lease_b.fencing_token
    assert token_b == 2
    assert not lease_a.renew("monitoring_runtime", "holder-a", int(token_a), now=point + timedelta(seconds=3))
    assert not lease_a.validate("monitoring_runtime", "holder-a", int(token_a), now=point + timedelta(seconds=3))
    assert lease_b.validate("monitoring_runtime", "holder-b", int(token_b), now=point + timedelta(seconds=3))

    gateway = ExecutionGateway(
        v13_store,
        ledger=ledger,
        runtime_fence_validator=lambda _intent: lease_a.validate(
            "monitoring_runtime", "holder-a", int(token_a), now=point + timedelta(seconds=3)
        ),
    )
    intent = OrderIntent(
        intent_id="fenced-open-intent",
        idempotency_key="fenced-open-key",
        account_id="fence_a",
        mode=TradingMode.PAPER,
        instrument_id="BTCUSDT",
        side="LONG",
        order_type="market",
        quantity=1.0,
        protection_plan=ProtectionPlan(stop_price=90.0),
        venue="simulated",
        environment="PAPER",
    )
    with pytest.raises(GatewayError) as error:
        gateway.submit_intent(intent, market_snapshot=_fresh_market(point + timedelta(seconds=3)))
    assert error.value.code == "RUNTIME_LEASE_FENCED"
    with v13_store._connect() as db:
        assert db.execute("SELECT 1 FROM order_intents WHERE idempotency_key=?", (intent.idempotency_key,)).fetchone() is None


def test_v13_c_runtime_lease_loss_cancels_ai_and_pauses_session(v13_store: SQLiteStore) -> None:
    ledger = AccountLedger(v13_store)
    _account(ledger, "lease_cycle")
    now = datetime.now(timezone.utc)
    v13_store.save_realtime_state(
        {
            "symbol": "BTCUSDT",
            "provider": "local-paper-fixture",
            "price": 100.0,
            "data_as_of": now.isoformat(),
            "received_at": now.isoformat(),
            "freshness_status": "fresh",
            "stale_after_seconds": 120,
            "market": {
                "contractSize": 1.0,
                "precision": {"amount": 0.001, "price": 0.01},
                "limits": {"amount": {"min": 0.001, "max": 1_000_000.0, "step": 0.001}},
                "taker": 0.0005,
            },
        },
        now=now,
    )
    # The AI session must pass the real closed-bar calibration gate before the
    # lease-fence test can exercise an in-flight model call.  One deterministic
    # closed fixture bar is sufficient here; production keeps the 500-bar /
    # 30-day requirement.
    v13_store.upsert_market_bars(
        "BTCUSDT",
        "15m",
        [Bar(now - timedelta(minutes=15), 100.0, 101.0, 99.0, 100.0, 10.0)],
        provider="local-paper-fixture",
        data_as_of=now - timedelta(seconds=1),
        now=now,
    )
    AuthorizationManager(v13_store).grant_authorization(
        account_id="lease_cycle",
        venue="simulated",
        mode=TradingMode.PAPER,
        decision_path=DecisionPath.AI_LED,
        allowed_instruments=["BTCUSDT"],
        allowed_sides=["LONG"],
        duration_seconds=600,
    )

    class BlockingProvider:
        provider_name = "local-test-provider"
        model_id = "Bonsai-2-27B-PTQ1_0"
        context_length = 8192
        max_tokens = 1000
        # A deterministic fixture digest satisfies the calibration identity
        # check without pretending to be a production model artifact.
        weight_digest = "b" * 64

        def __init__(self) -> None:
            self.entered = Event()
            self.cancelled = Event()
            self.release = Event()

        def health(self):
            return {
                "available": True,
                "model_available": True,
                "model_id": "Bonsai-2-27B-PTQ1_0",
                "actual_model_id": r"D:\RJ\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
                "model_identity_source": "verified_manifest",
                "models": [r"D:\RJ\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf"],
                "context_length": self.context_length,
            }

        def generate_json(self, *_args, **_kwargs):
            if _kwargs.get("prompt_version") in {"ai_calibration_operation_profile_v1", "ai_calibration_operation_profile_v2"}:
                answer = {
                    "risk_regime": "fixture",
                    "entry_style": "CONSERVATIVE",
                    "order_preference": "AUTO",
                    "max_concurrent_positions": 1,
                    "notes_zh": "deterministic calibration fixture",
                }
                actual = r"D:\RJ\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf"
                return answer, json.dumps(answer), {"model_id": "Bonsai-2-27B-PTQ1_0", "model_version": actual, "actual_model_id": actual, "model_identity_source": "request_bound_to_verified_manifest", "verified_manifest_model_id": actual}
            self.entered.set()
            self.release.wait(5)
            answer = {
                "action": "OPEN_LONG",
                "instrument_id": "BTCUSDT",
                "reason": "local lease-fence test",
                "stop_price": 90.0,
                "evidence_refs": ["local-fixture"],
            }
            actual = r"D:\RJ\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf"
            return answer, json.dumps(answer), {"model_id": "Bonsai-2-27B-PTQ1_0", "model_version": actual, "actual_model_id": actual, "model_identity_source": "request_bound_to_verified_manifest", "verified_manifest_model_id": actual}

        def cancel_generation(self, *_args, **_kwargs):
            self.cancelled.set()
            self.release.set()

    provider = BlockingProvider()
    lease_a = RuntimeLease(v13_store, default_ttl_seconds=1)
    runtime = MonitoringRuntime(
        store=v13_store,
        service=_QuietMonitoringService(),
        stream_factory=lambda symbols: _QuietStream(symbols),
        runtime_lease=lease_a,
        poll_interval_seconds=0.05,
        stream_join_timeout_seconds=0.1,
    )
    runtime.ai_coordinator.model_provider = provider
    runtime.ai_coordinator.calibration_min_bars = 1
    lease_b = RuntimeLease(v13_store, default_ttl_seconds=2)
    cycle_thread = None
    try:
        runtime.start(account_id="lease_cycle", enable_ai=True)
        # Scheduling waits for the strategy boundary. Exercise fencing with
        # an explicit test cycle while the scheduler remains armed.
        from threading import Thread
        cycle_thread = Thread(target=runtime.ai_coordinator.run_cycle_once)
        cycle_thread.start()
        assert provider.entered.wait(2), "AI provider did not enter an in-flight generation"
        time.sleep(1.2)
        assert lease_b.acquire("monitoring_runtime", "external-takeover", ttl_seconds=2)

        # Exercise the runtime's real renewal-loss path after the external
        # process owns the next fencing epoch.  No model or adapter mock is
        # allowed to turn the late OPEN_LONG into an order.
        runtime._lease_heartbeat_loop()
        assert provider.cancelled.wait(1)
        status = runtime.status()
        assert status["execution_blocked"] is True
        assert status["lease"]["valid"] is False
        assert status["session"]["state"] == "PAUSED"
        assert status["ai_session"]["enabled"] is False
        with v13_store._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0] == 0
    finally:
        provider.release.set()
        if cycle_thread is not None:
            cycle_thread.join(timeout=2)
        runtime.stop()
        lease_b.release("monitoring_runtime", "external-takeover")
        runtime.ai_coordinator.close()


def test_v13_d_cockpit_and_api_queries_are_account_scoped(v13_store: SQLiteStore) -> None:
    ledger = AccountLedger(v13_store)
    _account(ledger, "scope_a")
    _account(ledger, "scope_b")
    gateway = ExecutionGateway(v13_store, ledger=ledger)
    for account_id, key in (("scope_a", "scope-a-order"), ("scope_b", "scope-b-order")):
        intent = OrderIntent(
            intent_id=f"intent-{account_id}",
            idempotency_key=key,
            account_id=account_id,
            mode=TradingMode.PAPER,
            instrument_id="BTCUSDT",
            side="LONG",
            order_type="market",
            quantity=1.0,
            protection_plan=ProtectionPlan(stop_price=90.0),
            venue="simulated",
            environment="PAPER",
        )
        receipt = gateway.submit_intent(intent, market_snapshot=_fresh_market())
        assert receipt["status"] == "FILLED"

    service = TraderCapabilityService(v13_store, gateway=gateway)
    cockpit_a = service.risk_snapshot("scope_a")
    cockpit_b = service.risk_snapshot("scope_b")
    assert cockpit_a["account_id"] == "scope_a"
    assert all(item["account_id"] == "scope_a" for item in cockpit_a["positions"])
    assert all(item["account_id"] == "scope_a" for item in cockpit_a["orders"])
    assert all(item["account_id"] == "scope_b" for item in cockpit_b["positions"])
    assert all(item["account_id"] == "scope_b" for item in cockpit_b["orders"])
    assert cockpit_a["mode"] == cockpit_a["positions"][0]["mode"] == "PAPER"
    assert cockpit_a["venue"] == cockpit_a["positions"][0]["venue"] == "simulated"

    client = _client(v13_store)
    positions_a = client.get("/v2/positions?account_id=scope_a").json()
    orders_a = client.get("/v2/orders?account_id=scope_a").json()
    assert positions_a["count"] == 1
    assert all(item["account_id"] == "scope_a" for item in positions_a["positions"])
    assert orders_a["count"] == 1
    assert all(item["account_id"] == "scope_a" for item in orders_a["orders"])

    # A reservation created by an older/unscoped path stays account-owned but
    # cannot be silently relabelled as the current venue/mode.
    assert ledger.reserve_risk(
        "scope_a",
        "legacy-unscoped-reservation",
        amount_risk=Decimal("1"),
        amount_margin=Decimal("1"),
    ) is True
    scoped_after_legacy = service.risk_snapshot("scope_a")
    legacy_reservations = [
        item for item in scoped_after_legacy["reservations"]
        if item["reservation_id"] == "legacy-unscoped-reservation"
    ]
    assert legacy_reservations and legacy_reservations[0]["scope_status"] == UNKNOWN
    assert legacy_reservations[0]["mode"] == UNKNOWN
    assert legacy_reservations[0]["venue"] == UNKNOWN
    assert scoped_after_legacy["capacity"]["status"] == UNKNOWN
    assert any(item["type"] == "RESERVATION_SCOPE" for item in scoped_after_legacy["capacity"]["unknown_risk_items"])


def test_v13_d_legacy_position_remains_visible_without_cross_account_runtime_lock(v13_store: SQLiteStore) -> None:
    ledger = AccountLedger(v13_store)
    _account(ledger, "legacy_a")
    payload = {
        "position_id": "legacy-btc",
        "account_id": "legacy_a",
        "venue": "simulated",
        "mode": "PAPER",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": 100.0,
        "stop": 90.0,
        "remaining_contracts": 1.0,
    }
    with v13_store._connect() as db:
        db.execute(
            """INSERT INTO simulated_positions
               (position_id, symbol, status, payload_json, updated_at,
                account_id, venue, mode, protection_status, legacy_unverified)
               VALUES (?, ?, 'OPEN', ?, ?, ?, ?, ?, 'UNKNOWN', 1)""",
            (
                "legacy-btc",
                "BTCUSDT",
                json.dumps(payload),
                datetime.now(timezone.utc).isoformat(),
                "legacy_a",
                "simulated",
                "PAPER",
            ),
        )
    cockpit = TraderCapabilityService(v13_store).risk_snapshot("legacy_a")
    assert cockpit["legacy_data"]["account_scoped_unverified"] == 1
    assert cockpit["capacity"]["status"] == UNKNOWN
    assert any(item["type"] == "LEGACY_POSITION_SCOPE" for item in cockpit["capacity"]["unknown_risk_items"])

    runtime = MonitoringRuntime(
        store=v13_store,
        service=_QuietMonitoringService(),
        stream_factory=lambda symbols: _QuietStream(symbols),
    )
    try:
        status = runtime.start(account_id="legacy_a")
        assert status["state"] in {"starting", "running"}
    finally:
        runtime.stop()


def test_v13_f_portfolio_budget_includes_unknown_orders(v13_store: SQLiteStore) -> None:
    ledger = AccountLedger(v13_store)
    _account(ledger, "budget_a")
    ExecutionGateway(v13_store, ledger=ledger)
    service = TraderCapabilityService(v13_store)

    calculated = service.risk_snapshot("budget_a")
    assert calculated["capacity"]["status"] == "CALCULATED_FROM_LEDGER"
    assert calculated["capacity"]["single_trade_risk_available"] != calculated["capacity"]["portfolio_risk_available"]
    assert calculated["concentration"]["correlation"]["status"] == UNKNOWN

    now = datetime.now(timezone.utc).isoformat()
    with v13_store._connect() as db:
        db.execute(
            """INSERT INTO order_intents
               (intent_id, idempotency_key, account_id, mode, instrument_id,
                side, order_type, quantity, price, payload_hash, status,
                created_at, updated_at, venue, environment)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'UNKNOWN', ?, ?, ?, ?)""",
            (
                "unknown-budget-order",
                "unknown-budget-order-key",
                "budget_a",
                "PAPER",
                "ETHUSDT",
                "LONG",
                "market",
                1.0,
                None,
                "unknown-payload-hash",
                now,
                now,
                "simulated",
                "PAPER",
            ),
        )

    blocked = service.risk_snapshot("budget_a")
    assert blocked["capacity"]["status"] == UNKNOWN
    assert blocked["concentration"]["strategy_competition"]["status"] == UNKNOWN
    assert any(item["type"] == "ORDER_RECONCILIATION" for item in blocked["capacity"]["unknown_risk_items"])
    assert ledger.reserve_risk(
        "budget_a",
        "blocked-by-unknown-order",
        amount_risk=Decimal("1"),
        amount_margin=Decimal("1"),
        instrument_id="BTCUSDT",
        venue="simulated",
        mode="PAPER",
    ) is False


def test_v13_e_research_is_stored_costed_and_no_lookahead(v13_store: SQLiteStore) -> None:
    ledger = AccountLedger(v13_store)
    _account(ledger, "research_a")
    base = datetime.now(timezone.utc) - timedelta(days=2)
    _insert_prediction_record(v13_store, account_id="research_a", prediction_id="p1", generated_at=base, pnl=2.0)
    _insert_prediction_record(v13_store, account_id="research_a", prediction_id="p2", generated_at=base + timedelta(hours=1), pnl=-1.0)
    _insert_prediction_record(
        v13_store,
        account_id="research_a",
        prediction_id="future-data",
        generated_at=base + timedelta(hours=2),
        pnl=10.0,
        data_as_of=base + timedelta(hours=3),
    )
    _insert_prediction_record(
        v13_store,
        account_id="other",
        prediction_id="other-account",
        generated_at=base + timedelta(hours=3),
        pnl=10.0,
    )

    service = TraderCapabilityService(v13_store)
    result = service.evaluate_stored_strategy(
        account_id="research_a",
        strategy_id="ema_trend",
        strategy_version="v1",
        symbol="BTCUSDT",
        timeframe="15m",
        min_samples=2,
        train_fraction=0.5,
        parameter_perturbations=[{"atr": 1.1}],
    )
    assert result["status"] == "EVALUATED"
    assert result["sample_count"] == 2
    assert result["sample_gate"]["cost_data_complete"] is True
    assert result["temporal_validation"]["no_lookahead"] is True
    assert "DECISION_DATA_AS_OF_AFTER_GENERATION" in result["data_quality"]["issues"]
    assert result["cost_after_metrics"]["stress"]["status"] == "EVALUATED"
    assert result["parameter_perturbation"]["status"] == "NOT_RUN_REPLAY_REQUIRED"
    assert result["provenance"]["arbitrary_trade_array_accepted"] is False
    persisted = service.get_evaluation_task("research_a", result["task_id"])
    assert persisted["result"]["sample_count"] == 2
    assert service.evaluate_stored_strategy(
        account_id="research_a",
        strategy_id="unknown_strategy",
        symbol="BTCUSDT",
        min_samples=1,
    )["status"] == "EVIDENCE_INSUFFICIENT"


def test_v13_f_wait_news_and_unknown_facts_remain_explicit(v13_store: SQLiteStore) -> None:
    ledger = AccountLedger(v13_store)
    _account(ledger, "trader_a")
    service = TraderCapabilityService(v13_store)
    wait = service.create_trade_plan(
        {
            "account_id": "trader_a",
            "symbol": "BTCUSDT",
            "action": "WAIT",
            "evidence": [{"type": "market", "id": "snapshot-unknown"}],
            "entry_trigger": "wait for a fresh break and retest",
            "abandon_chase_condition": "do not chase after the first impulse",
            "why_not_waiting": "WAIT is the formal result until the trigger is observable",
            "worst_loss_budget": 0,
        }
    )
    wait_result = service.execute_trade_plan("trader_a", wait["plan_id"])
    assert wait_result["status"] == "WAIT"
    assert not wait_result["order_created"]

    registry = NewsRevisionRegistry(v13_store)
    revision = registry.record_news(
        "news-1",
        "Unverified headline",
        "Unverified excerpt",
        source_url="https://local.invalid/news-1",
        publisher="unverified-source",
        claim_status="UNVERIFIED",
        source_tier="TIER_C_SOCIAL",
    )
    impact = service.record_news_impact(
        {
            "account_id": "trader_a",
            "news_id": "news-1",
            "revision_id": revision.revision_id,
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "horizon": "4h",
        }
    )
    assert impact["verification_status"] == "UNVERIFIED"
    assert impact["expected_gap"] == UNKNOWN
    assert impact["absorbed"] == UNKNOWN
    assert impact["high_risk_trade_trigger_allowed"] is False
    research = service.get_news_research("trader_a", "news-1")
    assert len(research["revisions"]) == 1
    assert len(research["impacts"]) == 1


def test_v13_g_trade_plan_runs_through_api_runtime_gateway_and_guardian(v13_store: SQLiteStore) -> None:
    ledger = AccountLedger(v13_store)
    _account(ledger, "plan_a")
    now = datetime.now(timezone.utc)
    v13_store.save_realtime_state(
        {
            "symbol": "BTCUSDT",
            "provider": "local-paper-fixture",
            "price": 100.0,
            "bid": 99.9,
            "ask": 100.1,
            "data_as_of": now.isoformat(),
            "received_at": now.isoformat(),
            "freshness_status": "fresh",
            "stale_after_seconds": 120,
            "market": {
                "contractSize": 1.0,
                "precision": {"amount": 0.001, "price": 0.01},
                "limits": {"amount": {"min": 0.001, "max": 1_000_000.0, "step": 0.001}},
                "taker": 0.0005,
            },
        },
        now=now,
    )
    runtime = MonitoringRuntime(
        store=v13_store,
        service=_QuietMonitoringService(),
        stream_factory=lambda symbols: _QuietStream(symbols),
        poll_interval_seconds=0.05,
        stream_join_timeout_seconds=0.1,
    )
    client = _client(v13_store, runtime)
    try:
        assert client.post("/v2/monitoring/sessions/start?account_id=plan_a").status_code == 200
        created = client.post(
            "/v2/trade-plans",
            json={
                "account_id": "plan_a",
                "mode": "PAPER",
                "venue": "simulated",
                "symbol": "BTCUSDT",
                "action": "OPEN_LONG",
                "evidence": [{"type": "local_market_snapshot", "as_of": now.isoformat()}],
                "entry_trigger": "fresh quote at or below 100",
                "abandon_chase_condition": "abandon after adverse move beyond the trigger",
                "entry_price": 100.0,
                "stop_loss": 90.0,
                "take_profit": 110.0,
                "worst_loss_budget": 2.0,
                "why_not_waiting": "the trigger is observable in the stored fresh snapshot",
            },
        )
        assert created.status_code == 200, created.text
        plan_id = created.json()["plan_id"]
        executed = client.post(f"/v2/trade-plans/{plan_id}/execute?account_id=plan_a")
        assert executed.status_code == 200, executed.text
        receipt = executed.json()
        assert receipt["status"] == "FILLED"
        assert receipt["plan_id"] == plan_id
        assert receipt["execution_evidence"]["source"] == "paper_matching_engine"

        runtime.process_market_event("BTCUSDT", _bar())
        positions = client.get("/v2/positions?account_id=plan_a").json()
        assert positions["count"] == 1
        assert positions["positions"][0]["status"] == "CLOSED"
        plans = client.get("/v2/trade-plans?account_id=plan_a").json()
        assert plans["count"] == 1
        assert plans["plans"][0]["status"] == "EXECUTED"
    finally:
        runtime.stop()


def test_v13_g_ai_scorecard_links_receipts_but_not_profit_claims(v13_store: SQLiteStore) -> None:
    ledger = AccountLedger(v13_store)
    _account(ledger, "ai_a")
    _account(ledger, "ai_b")
    with v13_store._connect() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS ai_led_cycles (
                cycle_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                action TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
                latency_ms REAL, order_intent_id TEXT, payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL, session_id TEXT, generation INTEGER,
                authorization_id TEXT, market_snapshot_hash TEXT)"""
        )
        payload = {
            "account_id": "ai_a",
            "action": "WAIT",
            "model_id": "Bonsai-2-27B-PTQ1_0",
            "model_version": "local-config-1",
            "model_digest": "digest-a",
            "prompt_version": "prompt-v1",
            "input_hash": "input-a",
            "strategy_version": "strategy-v1",
            "authorization_id": "auth-a",
            "authorization_version": 3,
            "fencing_token": 7,
            "execution_result": {"status": "WAIT"},
        }
        db.execute(
            """INSERT INTO ai_led_cycles
               (cycle_id, account_id, action, status, reason, latency_ms,
                order_intent_id, payload_json, created_at, session_id, generation,
                authorization_id, market_snapshot_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "cycle-a",
                "ai_a",
                "WAIT",
                "COMPLETED",
                "no trigger",
                12.0,
                None,
                json.dumps(payload),
                datetime.now(timezone.utc).isoformat(),
                "session-a",
                1,
                "auth-a",
                "market-a",
            ),
        )
    scorecard = TraderCapabilityService(v13_store).ai_scorecard("ai_a")
    assert scorecard["status"] == "AVAILABLE"
    assert scorecard["model_digests"] == ["digest-a"]
    assert scorecard["decision_inputs"][0]["input"]["authorization_id"] == "auth-a"
    assert scorecard["same_data_interval_cost_comparison"]["AI_LED"]["status"].startswith("EVIDENCE_INSUFFICIENT")
    assert scorecard["revalidation"]["required"] is True
    assert scorecard["revalidation"]["confidence_is_not_win_rate"] is True
    assert scorecard["error_attribution"]["counts_are_receipt_classifications_not_profit_claims"] is True
    assert TraderCapabilityService(v13_store).ai_scorecard("ai_b")["status"] == UNKNOWN


def test_v13_h_api_runtime_to_gateway_fill_guardian_ledger_attribution(v13_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    # This test covers ledger/API attribution, not the host's local model
    # installation. Keep model readiness deterministic across developer hosts.
    from core.model_client import model_client
    monkeypatch.setattr(model_client, "is_healthy", lambda: False)
    ledger = AccountLedger(v13_store)
    _account(ledger, "e2e_a")
    now = datetime.now(timezone.utc)
    v13_store.save_realtime_state(
        {
            "symbol": "BTCUSDT",
            "provider": "local-paper-fixture",
            "price": 100.0,
            "bid": 99.9,
            "ask": 100.1,
            "data_as_of": now.isoformat(),
            "received_at": now.isoformat(),
            "freshness_status": "fresh",
            "stale_after_seconds": 120,
            "market": {
                "contractSize": 1.0,
                "precision": {"amount": 0.001, "price": 0.01},
                "limits": {"amount": {"min": 0.001, "max": 1_000_000.0, "step": 0.001}},
                "taker": 0.0005,
            },
        },
        now=now,
    )
    runtime = MonitoringRuntime(
        store=v13_store,
        service=_QuietMonitoringService(),
        stream_factory=lambda symbols: _QuietStream(symbols),
        poll_interval_seconds=0.05,
        stream_join_timeout_seconds=0.1,
    )
    client = _client(v13_store, runtime)
    try:
        start_response = client.post("/v2/monitoring/sessions/start?account_id=e2e_a")
        assert start_response.status_code == 200, start_response.text
        assert start_response.json()["account_id"] == "e2e_a"
        order_response = client.post(
            "/v2/order-intents",
            json={
                "symbol": "BTCUSDT",
                "account_id": "e2e_a",
                "venue": "simulated",
                "side": "LONG",
                "quantity": 1.0,
                "price": None,
                "order_type": "market",
                "stop_loss": 90.0,
                "mode": "PAPER",
                "reduce_only": False,
            },
        )
        assert order_response.status_code == 200, order_response.text
        receipt = order_response.json()
        assert receipt["status"] == "FILLED"
        assert receipt["protection_status"] == "ACTIVE"

        cockpit = client.get("/v2/accounts/e2e_a/risk-cockpit")
        assert cockpit.status_code == 200, cockpit.text
        cockpit_payload = cockpit.json()
        assert cockpit_payload["positions"][0]["account_id"] == "e2e_a"
        assert cockpit_payload["protections"][0]["status"] == "ACTIVE"
        assert cockpit_payload["orders"][0]["status"] == "FILLED"

        runtime.process_market_event("BTCUSDT", _bar())
        closed_positions = client.get("/v2/positions?account_id=e2e_a").json()
        assert closed_positions["count"] == 1
        assert closed_positions["positions"][0]["status"] == "CLOSED"
        attribution = client.get("/v2/ai-analysis?account_id=e2e_a")
        assert attribution.status_code == 200, attribution.text
        assert attribution.json()["account"]["total_trades"] >= 1
        assert attribution.json()["account"]["initial_capital_usdt"] == 10000.0

        ai_status = client.get("/v2/ai-session/status?account_id=e2e_a")
        assert ai_status.status_code == 200, ai_status.text
        assert ai_status.json()["model_status"].get("status") != "READY"
    finally:
        runtime.stop()


def test_v13_i_api_never_fakes_start_or_opening_without_runtime(v13_store: SQLiteStore) -> None:
    ledger = AccountLedger(v13_store)
    _account(ledger, "no_runtime")
    client = _client(v13_store, None)
    body = {
        "symbol": "BTCUSDT",
        "account_id": "no_runtime",
        "venue": "simulated",
        "side": "LONG",
        "quantity": 1.0,
        "order_type": "market",
        "stop_loss": 90.0,
        "mode": "PAPER",
        "reduce_only": False,
    }
    order = client.post("/v2/order-intents", json=body)
    assert order.status_code == 503
    assert "RUNTIME_UNAVAILABLE" in str(order.json()["detail"])
    session = client.post("/v2/monitoring/sessions/start?account_id=no_runtime")
    assert session.status_code == 503
    assert "RUNTIME_UNAVAILABLE" in str(session.json()["detail"])
