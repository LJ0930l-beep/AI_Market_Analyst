"""v1.4 contract tests.

The first test is intentionally added before the implementation repair.  It
captures the independently reproduced D01 failure at the durable trader
capability boundary; the same test must pass after the repair.
"""

from __future__ import annotations

from datetime import datetime, timezone
from datetime import timedelta
from decimal import Decimal
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.v2 import router_for
from core.storage import SQLiteStore
from core.news_revision import NewsRevisionRegistry
from core.monitoring_runtime import MonitoringRuntime
from core.providers.base import Bar
from core.quant.strategies import STRATEGIES
from core.trading.authorization import AuthorizationManager, ConfirmationSource
from core.trading.execution_gateway import DecisionPath
from core.trading.ledger import AccountLedger
from core.trading.execution_gateway import ExecutionGateway, GatewayError, OrderIntent, ProtectionPlan, TradingMode
from core.trading.gate_live_client import GateLiveTrader
from core.trading.position_guardian import PositionGuardian
from core.trading.trader_capabilities import TraderCapabilityError, TraderCapabilityService, UNKNOWN
from core.trading.trade_plan_contract import evaluate_plan_conditions

from threading import Event


def test_v14_gate_dry_run_preserves_missing_leverage_as_unknown() -> None:
    """A dry-run receipt must not turn an omitted leverage into 100x."""
    trader = GateLiveTrader("local-boundary-key", "local-boundary-secret", live_trading_enabled=False)
    receipt = trader.place_order(
        symbol="BTCUSDT",
        side="LONG",
        amount=0.01,
        stop_loss=90.0,
        leverage=None,
    )
    assert receipt["status"] == "DRY_RUN_ACKNOWLEDGED"
    assert receipt["leverage"] is None


def test_v14_gate_account_and_trade_api_never_fabricate_unscoped_or_local_remote_facts(tmp_path) -> None:
    """The trader workbench must show missing private data as unknown, not zero."""
    store = SQLiteStore(tmp_path / "v14-gate-unknown.db")
    store.initialize()
    AccountLedger(store).create_account(
        "gate-paper-unknown",
        mode="PAPER",
        initial_deposit=Decimal("1000"),
        config={"venue": "simulated"},
    )
    app = FastAPI()
    app.include_router(router_for(lambda: store, lambda: None, lambda: None))
    with TestClient(app) as client:
        unscoped = client.get("/v2/gate/account")
        assert unscoped.status_code == 200
        assert unscoped.json()["data_status"] == "ACCOUNT_SCOPE_REQUIRED"
        assert unscoped.json()["balance"]["total"] is None

        local = client.get("/v2/gate/account", params={"account_id": "gate-paper-unknown"})
        assert local.status_code == 200
        assert local.json()["data_status"] == "NOT_AVAILABLE_LOCAL_LEDGER_SCOPE"
        assert local.json()["balance"]["free"] is None

        trades = client.get("/v2/gate/trades", params={"account_id": "gate-paper-unknown"})
        assert trades.status_code == 200
        assert trades.json()["summary"]["total_fee_cost"] is None
        assert trades.json()["summary"]["fee_status"] == "NOT_RUN_NO_EXTERNAL_AUTH"


def test_v14_remote_fill_without_fee_evidence_is_distinguished_from_paper_fill(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "v14-remote-economic-evidence.db")
    store.initialize()
    ledger = AccountLedger(store)
    ledger.create_account(
        "remote-economic-evidence",
        mode="TESTNET",
        initial_deposit=Decimal("10000"),
        config={"venue": "gate"},
    )
    auth = AuthorizationManager(store).grant_authorization(
        account_id="remote-economic-evidence",
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

    class RemoteFillWithoutFee:
        def place_order(self, **_kwargs):
            return {
                "status": "filled",
                "order_id": "remote-no-fee-1",
                "filled_quantity": 0.01,
                "amount": 0.01,
                "average_price": 100.0,
                "protection_verified": True,
            }

    now = datetime.now(timezone.utc)
    gateway = ExecutionGateway(store, trader_client=RemoteFillWithoutFee(), ledger=ledger)
    intent = OrderIntent(
        intent_id="remote-no-fee-intent",
        idempotency_key="remote-no-fee-idempotency",
        account_id="remote-economic-evidence",
        mode=TradingMode.TESTNET,
        environment="TESTNET",
        venue="gate",
        instrument_id="BTCUSDT",
        side="LONG",
        order_type="market",
        quantity=0.01,
        price=None,
        leverage=2,
        protection_plan=ProtectionPlan(stop_price=95.0),
        decision_path=DecisionPath.AI_LED,
        authorization_id=auth.authorization_id,
        authorization_version=auth.version,
    )
    receipt = gateway.submit_intent(intent, market_snapshot=_fresh_market(store, now))
    assert receipt["status"] == "FILLED"
    assert receipt["execution_evidence"]["simulated"] is False
    assert receipt["economic_reconciliation"]["status"] == "UNVERIFIED"
    assert receipt["economic_reconciliation"]["fee"]["status"] == "UNKNOWN_NOT_PROVIDED"
    assert receipt["economic_reconciliation"]["reconciliation_required"] is True


class _QuietStream:
    def __init__(self, symbols: tuple[str, ...]) -> None:
        self.symbols = symbols
        self._stop = Event()

    def run_forever(self, *, on_bar, on_state=None) -> None:
        del on_bar, on_state
        while not self._stop.wait(0.01):
            pass

    def stop(self) -> None:
        self._stop.set()


class _QuietMonitoringService:
    strategy_mode = False
    max_symbols = 10
    account_id = None

    def __init__(self) -> None:
        self.cancel_event = None

    def run(self, *, symbols=None, now=None):
        del symbols
        from core.monitoring import MonitoringRunResult
        point = now or datetime.now(timezone.utc)
        return MonitoringRunResult("DISABLED", point, tuple(), tuple(), {"max_symbols": 10, "active_symbols": 0, "bounded": True})


def _create_account(store: SQLiteStore, account_id: str, *, deposit: str = "10000") -> AccountLedger:
    ledger = AccountLedger(store)
    ledger.create_account(account_id, mode="PAPER", initial_deposit=Decimal(deposit), config={"venue": "simulated"})
    return ledger


def _seed_position(
    ledger: AccountLedger,
    account_id: str,
    *,
    position_id: str,
    side: str = "LONG",
    stop: float = 90.0,
    protection_contract: dict | None = None,
) -> dict:
    return ledger.record_trade_fill(
        account_id=account_id,
        instrument_id="BTCUSDT",
        side="BUY" if side == "LONG" else "SELL",
        quantity=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("0"),
        mode="PAPER",
        venue="simulated",
        order_id=f"entry-{position_id}",
        trade_id=f"fill-{position_id}",
        position_id=position_id,
        stop_price=stop,
        take_profit=130.0 if side == "LONG" else 70.0,
        protection_status="ACTIVE",
        protection_contract=protection_contract,
    )


def _plan_payload(account_id: str, *, action: str = "OPEN_LONG", **overrides) -> dict:
    payload = {
        "account_id": account_id,
        "mode": "PAPER",
        "venue": "simulated",
        "symbol": "BTCUSDT",
        "action": action,
        "evidence": [{"type": "local_fixture", "as_of": datetime.now(timezone.utc).isoformat()}],
        "entry_trigger": "typed condition is required",
        "abandon_chase_condition": "typed chase condition is required",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "take_profit": 120.0,
        "worst_loss_budget": 20.0,
        "why_not_waiting": "a deterministic plan is ready for its declared condition",
    }
    payload.update(overrides)
    return payload


def _fresh_market(store: SQLiteStore, now: datetime, price: float = 100.0) -> dict:
    snapshot = {
            "symbol": "BTCUSDT",
            "provider": "local-paper-fixture",
            "price": price,
            "bid": price - 0.1,
            "ask": price + 0.1,
            "data_as_of": now.isoformat(),
            "received_at": now.isoformat(),
            "freshness_status": "fresh",
            "fresh": True,
            "stale_after_seconds": 120,
            "slippage": 0.001,
            "market": {
                "contractSize": 1.0,
                "leverage_max": 100,
                "precision": {"amount": 0.001, "price": 0.01},
                "limits": {"amount": {"min": 0.001, "max": 1000000.0, "step": 0.001}},
                "taker": 0.0005,
            },
        }
    store.save_realtime_state(
        snapshot,
        now=now,
    )
    return snapshot


def test_v14_d01_reduce_fraction_and_leverage_survive_create_execute(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "v14-d01-repro.db")
    store.initialize()
    ledger = AccountLedger(store)
    ledger.create_account(
        "plan_reduce_a",
        mode="PAPER",
        initial_deposit=Decimal("10000"),
        config={"venue": "simulated"},
    )
    now = datetime.now(timezone.utc)
    _fresh_market(store, now)
    ledger.record_trade_fill(
        account_id="plan_reduce_a",
        instrument_id="BTCUSDT",
        side="BUY",
        quantity=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("0"),
        mode="PAPER",
        venue="simulated",
        order_id="order-seeded-position",
        trade_id="fill-seeded-position",
        position_id="position-plan-reduce-a",
        stop_price=90.0,
        protection_status="ACTIVE",
    )

    service = TraderCapabilityService(store)
    plan = service.create_trade_plan(
        {
            "account_id": "plan_reduce_a",
            "mode": "PAPER",
            "venue": "simulated",
            "symbol": "BTCUSDT",
            "action": "REDUCE_POSITION",
            "position_id": "position-plan-reduce-a",
            "reduce_fraction": 0.25,
            "leverage": 3,
            "evidence": [{"type": "local_market_snapshot", "as_of": now.isoformat()}],
            "entry_trigger": "fresh quote is available",
            "abandon_chase_condition": "do not chase beyond the recorded quote",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "worst_loss_budget": 1.0,
            "why_not_waiting": "the requested reduction is explicitly authorized by this plan",
        }
    )

    # D01: these assertions are expected to fail against the pre-repair code
    # because create_trade_plan dropped both behavior fields from clean.
    assert plan["reduce_fraction"] == 0.25
    assert plan["leverage"] == 3

    receipt = service.execute_trade_plan("plan_reduce_a", plan["plan_id"])
    assert receipt["status"] == "FILLED"
    assert float(receipt["quantity"]) == 0.25
    positions = AccountLedger(store).get_open_positions(
        "plan_reduce_a", venue="simulated", mode="PAPER"
    )
    assert len(positions) == 1
    assert float(positions[0]["remaining_contracts"]) == 0.75


def test_v14_d01_reduce_fraction_is_directional_and_close_is_the_only_full_exit(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "v14-directions.db")
    store.initialize()
    ledger = _create_account(store, "short-plan")
    now = datetime.now(timezone.utc)
    _fresh_market(store, now)
    _seed_position(ledger, "short-plan", position_id="short-position", side="SHORT", stop=110.0)
    service = TraderCapabilityService(store)
    plan = service.create_trade_plan(_plan_payload(
        "short-plan",
        action="REDUCE_POSITION",
        position_id="short-position",
        reduce_fraction=0.25,
        leverage=4,
        entry_price=100.0,
        stop_loss=110.0,
    ))
    receipt = service.execute_trade_plan("short-plan", plan["plan_id"])
    assert receipt["status"] == "FILLED"
    assert float(receipt["quantity"]) == 0.25
    assert float(ledger.get_open_positions("short-plan", venue="simulated", mode="PAPER")[0]["remaining_contracts"]) == 0.75

    close_plan = service.create_trade_plan(_plan_payload(
        "short-plan",
        action="CLOSE_POSITION",
        position_id="short-position",
        entry_price=100.0,
        stop_loss=110.0,
    ))
    close_receipt = service.execute_trade_plan("short-plan", close_plan["plan_id"])
    assert close_receipt["status"] == "FILLED"
    assert float(close_receipt["quantity"]) == 0.75
    assert ledger.get_open_positions("short-plan", venue="simulated", mode="PAPER") == []


@pytest.mark.parametrize("value", [0.0, -0.1, 1.0001, float("nan"), float("inf")])
def test_v14_d01_invalid_reduce_fraction_is_rejected_without_persistence(tmp_path, value: float) -> None:
    store = SQLiteStore(tmp_path / f"v14-invalid-{str(value).replace('.', '_')}.db")
    store.initialize()
    _create_account(store, "invalid-plan")
    service = TraderCapabilityService(store)
    with pytest.raises(TraderCapabilityError) as error:
        service.create_trade_plan(_plan_payload("invalid-plan", action="REDUCE_POSITION", position_id="p", reduce_fraction=value))
    assert error.value.code in {"PLAN_REDUCE_FRACTION_INVALID", "PLAN_NUMERIC_INVALID"}
    assert service.list_trade_plans("invalid-plan") == []


def test_v14_d01_legacy_reduce_plan_needs_reconfirmation_and_gateway_rejects_ambiguous_position(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "v14-legacy-and-multi.db")
    store.initialize()
    ledger = _create_account(store, "legacy-plan")
    now = datetime.now(timezone.utc)
    _fresh_market(store, now)
    _seed_position(ledger, "legacy-plan", position_id="legacy-position")
    service = TraderCapabilityService(store)
    close_plan = service.create_trade_plan(_plan_payload("legacy-plan", action="CLOSE_POSITION", position_id="legacy-position"))
    legacy_payload = dict(close_plan)
    legacy_payload.update({"action": "REDUCE_POSITION", "reduce_fraction": None, "reduce_quantity": None})
    with store._connect() as db:
        db.execute(
            "UPDATE trader_trade_plans SET action='REDUCE_POSITION', payload_json=?, status='ARMED' WHERE plan_id=?",
            (json.dumps(legacy_payload), close_plan["plan_id"]),
        )
    blocked = service.execute_trade_plan("legacy-plan", close_plan["plan_id"])
    assert blocked["status"] == "BLOCKED"
    assert blocked["error_code"] == "PLAN_REDUCE_FRACTION_REQUIRED"
    assert service.list_trade_plans("legacy-plan")[0]["status"] == "NEEDS_RECONFIRMATION"

    _seed_position(ledger, "legacy-plan", position_id="legacy-position-2")
    gateway = ExecutionGateway(store, ledger=ledger)
    ambiguous = OrderIntent(
        intent_id="ambiguous-reduce",
        idempotency_key="ambiguous-reduce",
        account_id="legacy-plan",
        mode=TradingMode.PAPER,
        instrument_id="BTCUSDT",
        side="SELL",
        order_type="market",
        quantity=0.25,
        protection_plan=ProtectionPlan(stop_price=90.0),
        reduce_only=True,
        venue="simulated",
        environment="PAPER",
    )
    with pytest.raises(GatewayError) as error:
        gateway.submit_intent(ambiguous, market_snapshot=_fresh_market(store, now))
    assert error.value.code == "REDUCE_ONLY_POSITION_ID_REQUIRED"


def test_v14_d02_api_round_trip_preserves_typed_conditions_and_protection_contract(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "v14-api-plan.db")
    store.initialize()
    _create_account(store, "api-plan")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from apps.api.v2 import router_for

    app = FastAPI()
    app.include_router(router_for(get_store=lambda: store, get_runtime=lambda: None, get_translation=lambda: None))
    client = TestClient(app)
    body = _plan_payload(
        "api-plan",
        condition_spec={
            "version": "trade_conditions_v1",
            "entry": {"type": "PRICE", "operator": "LTE", "threshold": 99.0},
            "abandon_chase": {"type": "MAX_CHASE_BPS", "value": 10.0},
        },
        entry_trigger="wait for typed price condition",
        abandon_chase_condition="wait for typed chase condition",
        entry_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        time_exit_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        partial_take_profits=[{"price": 105.0, "fraction": 0.25}, {"price": 110.0, "fraction": 0.25}],
        trailing_protection={"type": "DISTANCE", "value": 3.0},
        event_invalidation=[{"event_key": "rev_api", "invalid_if": "CORRECTED"}],
        leverage=3,
    )
    response = client.post("/v2/trade-plans", json=body)
    assert response.status_code == 200
    plan = response.json()
    assert plan["conditions"]["entry"] == {"type": "PRICE", "operator": "LTE", "value": 99.0, "version": "trade_conditions_v1", "source": "condition_spec"}
    assert plan["partial_take_profits"][0]["fraction"] == 0.25
    assert plan["trailing_protection"]["type"] == "DISTANCE"
    listed = client.get("/v2/trade-plans?account_id=api-plan").json()
    assert listed["plans"][0]["plan"]["leverage"] == 3
    assert listed["plans"][0]["plan"]["time_exit_at"] == plan["time_exit_at"]


def test_v14_d02_runtime_consumes_typed_trigger_and_blocks_untyped_prose(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "v14-runtime-conditions.db")
    store.initialize()
    _create_account(store, "runtime-plan")
    service = TraderCapabilityService(store)
    runtime = MonitoringRuntime(
        store=store,
        service=_QuietMonitoringService(),
        stream_factory=lambda symbols: _QuietStream(symbols),
        poll_interval_seconds=0.02,
        stream_join_timeout_seconds=0.1,
    )
    try:
        waiting = service.create_trade_plan(_plan_payload(
            "runtime-plan",
            condition_spec={
                "version": "trade_conditions_v1",
                "entry": {"type": "PRICE", "operator": "LTE", "value": 99.0},
                "abandon_chase": {"type": "MAX_CHASE_BPS", "value": 10.0},
            },
        ))
        typed_fresh_mark = service.create_trade_plan(_plan_payload(
            "runtime-plan",
            condition_spec={
                "version": "trade_conditions_v1",
                "entry": {"type": "FRESH_MARK"},
                "abandon_chase": {"type": "MAX_CHASE_BPS", "value": 10.0},
            },
            entry_trigger="fresh quote is available",
        ))
        unsupported = service.create_trade_plan(_plan_payload(
            "runtime-plan",
            entry_trigger="when the moon is aligned",
        ))
        runtime.start(account_id="runtime-plan")
        runtime.process_market_event("BTCUSDT", Bar(datetime.now(timezone.utc), 100, 101, 99, 100, 1))
        statuses = {item["plan"]["plan_id"]: item["status"] for item in service.list_trade_plans("runtime-plan")}
        assert statuses[waiting["plan_id"]] == "WAITING_TRIGGER"
        assert statuses[typed_fresh_mark["plan_id"]] == "EXECUTED"
        assert statuses[unsupported["plan_id"]] == "BLOCKED_DATA"
        with store._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM order_intents WHERE account_id='runtime-plan'").fetchone()[0] == 1
        runtime.process_market_event("BTCUSDT", Bar(datetime.now(timezone.utc) + timedelta(seconds=1), 98, 99, 97, 98, 1))
        assert service.list_trade_plans("runtime-plan", status="EXECUTED")
    finally:
        # The local test position is closed through the Guardian stop path so
        # the runtime can release its lease without discarding protection.
        runtime.stop()


def test_v14_d02_time_exit_partial_take_profit_and_trailing_survive_runtime_stop(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "v14-protection-contract.db")
    store.initialize()
    ledger = _create_account(store, "protection-plan")
    now = datetime.now(timezone.utc)
    contract = {
        "stop_price": 90.0,
        "take_profit": 130.0,
        "time_exit_at": (now + timedelta(minutes=30)).isoformat(),
        "partial_take_profits": [
            {"price": 105.0, "fraction": 0.25, "label": "TP1"},
            {"price": 110.0, "fraction": 0.25, "label": "TP2"},
        ],
        "trailing_protection": {"type": "DISTANCE", "value": 3.0},
        "event_invalidation": [],
    }
    _seed_position(ledger, "protection-plan", position_id="protection-position", protection_contract=contract)
    runtime = MonitoringRuntime(
        store=store,
        service=_QuietMonitoringService(),
        stream_factory=lambda symbols: _QuietStream(symbols),
        poll_interval_seconds=0.02,
        stream_join_timeout_seconds=0.1,
    )
    try:
        runtime.start(account_id="protection-plan")
        runtime.stop()
        assert runtime.guardian.is_active()
        runtime.process_market_event("BTCUSDT", Bar(now + timedelta(minutes=1), 100, 103, 100, 102, 1))
        position = ledger.get_open_positions("protection-plan", venue="simulated", mode="PAPER")[0]
        assert float(position["stop"]) == 100.0
        runtime.process_market_event("BTCUSDT", Bar(now + timedelta(minutes=2), 102, 106, 101, 105, 1))
        position = ledger.get_open_positions("protection-plan", venue="simulated", mode="PAPER")[0]
        assert float(position["remaining_contracts"]) == 0.75
        ledger.record_trade_fill(
            account_id="protection-plan", instrument_id="BTCUSDT", side="SELL", quantity=Decimal("0.1"), price=Decimal("104"), fee=Decimal("0"),
            mode="PAPER", venue="simulated", order_id="manual-reduce", trade_id="manual-reduce", position_id="protection-position", reduce_only=True,
        )
        runtime.process_market_event("BTCUSDT", Bar(now + timedelta(minutes=3), 109, 111, 109, 110, 1))
        after_tp2 = ledger.get_open_positions("protection-plan", venue="simulated", mode="PAPER")[0]
        assert float(after_tp2["remaining_contracts"]) == 0.4
        # Duplicate/乱序 delivery has already consumed both targets and must
        # not create a second economic fill.
        runtime.process_market_event("BTCUSDT", Bar(now + timedelta(minutes=2), 102, 106, 101, 105, 1))
        assert float(ledger.get_open_positions("protection-plan", venue="simulated", mode="PAPER")[0]["remaining_contracts"]) == 0.4
        runtime.process_market_event("BTCUSDT", Bar(now + timedelta(minutes=31), 110, 111, 109, 110, 1))
        assert ledger.get_open_positions("protection-plan", venue="simulated", mode="PAPER") == []
    finally:
        runtime.stop()


def test_v14_d04_news_inference_is_unknown_and_revision_correction_invalidates_plan(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "v14-news.db")
    store.initialize()
    _create_account(store, "news-plan")
    registry = NewsRevisionRegistry(store)
    revision = registry.record_news("news-v14", "Headline", "Original body", claim_status="PRIMARY_SOURCE_VERIFIED", source_url="https://local.invalid/news-v14", publisher="fixture")
    service = TraderCapabilityService(store)
    impact = service.record_news_impact({
        "account_id": "news-plan", "news_id": "news-v14", "revision_id": revision.revision_id,
        "symbol": "BTCUSDT", "direction": "LONG", "expected_gap": 99.0, "absorbed": True,
    })
    assert impact["expected_gap"] == UNKNOWN
    assert impact["absorbed"] == UNKNOWN
    assert impact["inference_status"] == UNKNOWN
    assert impact["high_risk_trade_trigger_allowed"] is False
    plan = service.create_trade_plan(_plan_payload("news-plan", news_revision_ids=[revision.revision_id]))
    registry.correct_news("news-v14", "Corrected", "Correction", correction_reason="source correction", claim_status="RETRACTED")
    result = service.execute_trade_plan("news-plan", plan["plan_id"], market_snapshot=_fresh_market(store, datetime.now(timezone.utc)))
    assert result["status"] == "INVALIDATED"
    assert result["order_created"] is False


def test_v14_d03_closed_bar_replay_and_real_parameter_perturbation(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "v14-replay.db")
    store.initialize()
    _create_account(store, "replay-account")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    values = [100.0] * 70 + [100 - 0.5 * i for i in range(1, 11)] + [95.0, 105.0, 107.0, 108.0] + [108.0 + 0.2 * i for i in range(1, 35)] + [115.0 - 0.5 * i for i in range(1, 11)] + [110.0, 120.0, 122.0, 123.0] + [123.0 + 0.2 * i for i in range(1, 35)]
    bars = []
    previous = values[0]
    for index, close in enumerate(values):
        bars.append(Bar(base + timedelta(minutes=15 * index), previous, max(previous, close) + 0.4, min(previous, close) - 0.4, close, 3.0 if index in (81, 129) else 1.0))
        previous = close
    store.upsert_market_bars("BTCUSDT", "15m", bars, provider="local-replay-fixture", data_as_of=base, now=datetime.now(timezone.utc))
    result = TraderCapabilityService(store).evaluate_stored_strategy(
        account_id="replay-account", strategy_id="ema_trend", strategy_version=STRATEGIES["ema_trend"].version,
        symbol="BTCUSDT", timeframe="15m", min_samples=1, train_fraction=0.7,
        parameter_perturbations=[{"volume_ratio": 4.0}],
    )
    replay = result["bar_replay"]
    assert replay["validation_type"] == "FROZEN_OOS_BAR_REPLAY"
    assert replay["frozen_split"]["purge_bars"] == 1
    assert replay["frozen_split"]["embargo_bars"] == 1
    assert replay["future_data_invariance"]["future_rows_supplied_to_strategy"] is False
    assert replay["parameter_perturbation"]["status"] == "EVALUATED"
    variant = replay["parameter_perturbation"]["variants"][0]
    assert variant["recomputed"] is True
    assert variant["parameters_hash"] != replay["input"]["parameters_hash"]
    assert variant["replay"]["output_hash"] != replay["frozen_split"]["out_of_sample"]["replay"]["output_hash"]
    assert replay["cost_stress"]["status"] == "EVALUATED"
    assert result["temporal_validation"]["no_lookahead_proven"] is True


def test_v14_d03_missing_bars_are_not_called_a_no_lookahead_validation(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "v14-replay-missing.db")
    store.initialize()
    _create_account(store, "replay-missing")
    result = TraderCapabilityService(store).evaluate_stored_strategy(
        account_id="replay-missing", strategy_id="ema_trend", strategy_version="v1", symbol="BTCUSDT", min_samples=1,
        parameter_perturbations=[{"volume_ratio": 2.0}],
    )
    assert result["parameter_perturbation"]["status"] == "NOT_RUN_REPLAY_REQUIRED"
    assert result["bar_replay"]["status"] == "NOT_RUN_MISSING_CLOSED_MARKET_BARS"
    assert result["temporal_validation"]["validation_type"] == "RETROSPECTIVE_SPLIT"
    assert result["temporal_validation"]["no_lookahead_proven"] is False


def test_v14_d02_news_correction_after_registry_restart_reaches_protection_guardian(tmp_path) -> None:
    """A cited revision remains a live post-entry invalidation contract."""
    store = SQLiteStore(tmp_path / "v14-news-protection.db")
    store.initialize()
    ledger = _create_account(store, "news-protection")
    now = datetime.now(timezone.utc)
    registry = NewsRevisionRegistry(store)
    revision = registry.record_news(
        "news-protection-1",
        "Verified headline",
        "Original body",
        claim_status="PRIMARY_SOURCE_VERIFIED",
        source_url="https://local.invalid/news-protection-1",
        publisher="fixture",
    )
    runtime = MonitoringRuntime(
        store=store,
        service=_QuietMonitoringService(),
        stream_factory=lambda symbols: _QuietStream(symbols),
        poll_interval_seconds=0.02,
        stream_join_timeout_seconds=0.1,
    )
    try:
        plan = runtime.trade_plan_service.create_trade_plan(
            _plan_payload(
                "news-protection",
                news_revision_ids=[revision.revision_id],
                condition_spec={
                    "version": "trade_conditions_v1",
                    "entry": {"type": "FRESH_MARK"},
                    "abandon_chase": {"type": "MAX_CHASE_BPS", "value": 10.0},
                },
                entry_trigger="fresh quote is available",
                abandon_chase_condition="maximum chase is 10 bps",
            )
        )
        assert any(item["event_key"] == revision.revision_id for item in plan["event_invalidation"])
        runtime.start(account_id="news-protection")
        runtime.process_market_event("BTCUSDT", Bar(now, 100, 101, 99, 100, 1))
        position = ledger.get_open_positions("news-protection", venue="simulated", mode="PAPER")[0]
        assert position["protection_contract"]["event_invalidation"][0]["event_key"] == revision.revision_id

        # Simulate a fresh process receiving the correction.  The new registry
        # has no in-memory revision chain and must still load the durable one.
        restarted_registry = NewsRevisionRegistry(store)
        restarted_registry.correct_news(
            "news-protection-1",
            "Corrected headline",
            "Corrected body",
            correction_reason="source correction",
            claim_status="RETRACTED",
        )
        exits = runtime.process_market_event(
            "BTCUSDT",
            Bar(now + timedelta(minutes=1), 100, 101, 99, 100, 1),
        )
        assert any(item["reason"] == "EVENT_INVALIDATION" for item in exits)
        assert ledger.get_open_positions("news-protection", venue="simulated", mode="PAPER") == []
    finally:
        runtime.stop()


def test_v14_d02_condition_consumer_blocks_invalid_timestamp_and_unknown_event_fact(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "v14-condition-evidence.db")
    store.initialize()
    _create_account(store, "condition-evidence")
    service = TraderCapabilityService(store)
    plan = service.create_trade_plan(
        _plan_payload(
            "condition-evidence",
            condition_spec={
                "version": "trade_conditions_v1",
                "entry": {"type": "FRESH_MARK"},
                "abandon_chase": {"type": "MAX_CHASE_BPS", "value": 10.0},
            },
            event_invalidation=[{"event_key": "event-unknown", "invalid_if": "CORRECTED"}],
        )
    )
    now = datetime.now(timezone.utc)
    stale = {
        "price": 100.0,
        "bid": 99.9,
        "ask": 100.1,
        "fresh": True,
        "freshness_status": "fresh",
        "data_as_of": "not-a-timestamp",
        "stale_after_seconds": 120,
    }
    assert evaluate_plan_conditions(plan, stale, now=now)["status"] == "BLOCKED_DATA"
    unknown_event = _fresh_market(store, now)
    result = evaluate_plan_conditions(
        plan,
        unknown_event,
        now=now,
        event_facts={"event-unknown": {"status": "UNKNOWN"}},
    )
    assert result["status"] == "BLOCKED_DATA"
    assert result["reason"] == "EVENT_EVIDENCE_UNKNOWN"


def test_v14_d02_explicit_zero_freshness_window_is_not_widened(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "v14-zero-freshness.db")
    store.initialize()
    _create_account(store, "zero-freshness")
    service = TraderCapabilityService(store)
    plan = service.create_trade_plan(_plan_payload("zero-freshness"))
    now = datetime.now(timezone.utc)
    market = _fresh_market(store, now)
    market["stale_after_seconds"] = 0
    result = evaluate_plan_conditions(plan, market, now=now)
    assert result["status"] == "BLOCKED_DATA"
    assert result["reason"] == "FRESH_MARKET_REQUIRED"
