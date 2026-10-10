"""Focused regression coverage for the Gate TestNet + AI main chain.

These tests use isolated SQLite stores and deterministic provider doubles. A
fixture passing here proves the contract and accounting boundaries, not the
availability or quality of a real Gate credential or Bonsai inference.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.v2 import router_for
from core.storage import SQLiteStore
from core.trading.account_aliases import GATE_TESTNET_ACCOUNT_ID
from core.trading.ai_led_engine import AICycleContext, AIActionOutput, AILedDecisionEngine
from core.trading.authorization import AuthorizationManager, ConfirmationSource
from core.trading.execution_gateway import ExecutionGateway, GatewayError, OrderIntent, ProtectionPlan, TradingMode, DecisionPath
from core.trading.gate_account_truth import GateAccountTruthService
from core.trading.gate_live_client import GateLiveTrader
from core.trading.gate_testnet_e2e import GateTestnetE2EService
from core.trading.gate_accounts import provision_default_gate_accounts
from core.trading.ledger import AccountLedger
from core.trading.position_guardian import PositionGuardian
from core.trading.risk_engine import RiskEngine
from core.trading.trader_capabilities import TraderCapabilityService


def _store(tmp_path):
    store = SQLiteStore(tmp_path / "gate-testnet-ai-chain.db")
    store.initialize()
    # Tests that exercise managed-entry ownership need the actual settlement
    # schema so an absent ledger is not confused with an empty ledger.
    from core.trading.gate_trade_settlement import GateTradeSettlementService
    settlement_service = GateTradeSettlementService(store)
    with store._connect() as db:
        settlement_service._ensure(db)
    return store


class _RemoteTruthTrader:
    testnet = True
    live_trading_enabled = True

    def __init__(self):
        self.truth = {
            "status": "AVAILABLE",
            "source": "fixture_gate_testnet_private_api",
            "observed_at": "2030-01-02T12:00:00+00:00",
            "equity": 50000.25,
            "available_margin": 48000.25,
            "used_margin": 2000.0,
            "unrealized_pnl": 12.5,
            "realized_pnl": 88.0,
            "balance": {"total": 50000.0, "free": 48000.25, "used": 2000.0},
            "positions": [{
                "symbol": "BTCUSDT",
                "side": "LONG",
                "contracts": "2",
                "entry_price": "100",
                "mark_price": "101",
                "contract_size": "1",
                "unrealized_pnl": "2",
                "position_id": "remote-pos-1",
            }],
            "pending_orders": [{"order_id": "protect-1", "reduce_only": True}],
            "fills": [{"id": "remote-fill-1", "fee_cost": "0.25"}],
        }

    def get_account_truth(self, *, include_trades=False):
        return dict(self.truth)


def test_gate_remote_truth_is_authoritative_and_no_local_mirror_is_written(tmp_path):
    store = _store(tmp_path)
    provision_default_gate_accounts(store)
    ledger = AccountLedger(store)

    before = ledger.get_snapshot(GATE_TESTNET_ACCOUNT_ID)
    assert before.source == "LOCAL_LEDGER"
    assert before.net_equity == Decimal("0")

    service = GateAccountTruthService(store, clock=lambda: datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc))
    recorded = service.refresh(GATE_TESTNET_ACCOUNT_ID, _RemoteTruthTrader(), include_trades=True)
    assert recorded["status"] == "AVAILABLE"
    assert Decimal(str(recorded["equity"])) == Decimal("50000.25")

    snapshot = ledger.get_snapshot(
        GATE_TESTNET_ACCOUNT_ID,
        now=datetime(2030, 1, 2, 12, 0, 30, tzinfo=timezone.utc),
    )
    assert snapshot.source == "GATE_TESTNET_REMOTE"
    assert snapshot.remote_truth_status == "AVAILABLE"
    assert snapshot.net_equity == Decimal("50000.25")
    assert snapshot.available_margin == Decimal("48000.25")
    assert ledger.get_open_positions(GATE_TESTNET_ACCOUNT_ID, venue="gate", mode="TESTNET") == []
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM trade_fills").fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM simulated_positions WHERE account_id=? AND venue='gate' AND mode='TESTNET'",
            (GATE_TESTNET_ACCOUNT_ID,),
        ).fetchone()[0] == 0


def test_gate_risk_cockpit_projects_latest_remote_equity_and_positions(tmp_path):
    store = _store(tmp_path)
    provision_default_gate_accounts(store)
    now = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)
    trader = _RemoteTruthTrader()
    trader.truth["observed_at"] = now.isoformat()
    GateAccountTruthService(store, clock=lambda: now).refresh(GATE_TESTNET_ACCOUNT_ID, trader)

    cockpit = TraderCapabilityService(store, clock=lambda: now).risk_snapshot(GATE_TESTNET_ACCOUNT_ID)

    assert cockpit["account_truth_authority"] == "REMOTE_GATE_TESTNET_PRIVATE_API"
    assert cockpit["risk"]["net_equity"] == 50000.25
    assert cockpit["ledger_snapshot"]["net_equity"] == "50000.25"
    assert cockpit["positions"][0]["symbol"] == "BTCUSDT"
    assert cockpit["positions"][0]["remote_truth"] is True
    assert cockpit["capacity"]["basis"].startswith("REMOTE_GATE_TESTNET_PRIVATE_API")


def test_live_position_read_models_use_gate_snapshot_not_local_simulator(tmp_path):
    store = _store(tmp_path)
    provision_default_gate_accounts(store)
    trader = _RemoteTruthTrader()
    trader.truth["api_environment"] = "LIVE"
    trader.truth["pending_orders"] = []
    GateAccountTruthService(store).refresh("gate_live", trader)
    app = FastAPI()
    app.include_router(router_for(lambda: store, lambda: None, lambda: None))
    with TestClient(app) as client:
        positions = client.get("/v2/positions", params={"account_id": "gate_live"})
        workspace = client.get("/v2/workspace", params={"account_id": "gate_live"})
        status = client.get("/v2/ai-session/status", params={"account_id": "gate_live"})
    assert positions.status_code == workspace.status_code == status.status_code == 200
    assert positions.json()["count"] == 1
    assert positions.json()["positions"][0]["symbol"] == "BTCUSDT"
    assert len(workspace.json()["positions"]) == 1
    protection = status.json()["protection_summary"]
    assert protection["active_positions"] == 1
    assert protection["protected_positions"] is None
    assert protection["protection_status"] == "NOT_VERIFIED_BY_ACCOUNT_SNAPSHOT"


def test_gate_remote_truth_required_for_new_risk_but_not_reduce_only_boundary(tmp_path):
    store = _store(tmp_path)
    provision_default_gate_accounts(store)
    ledger = AccountLedger(store)
    now = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)

    assert not ledger.reserve_risk(
        GATE_TESTNET_ACCOUNT_ID,
        "reserve-without-remote",
        Decimal("10"),
        Decimal("20"),
        now=now,
        instrument_id="BTCUSDT",
        venue="gate",
        mode="TESTNET",
    )

    service = GateAccountTruthService(store, clock=lambda: now)
    trader = _RemoteTruthTrader()
    trader.truth["observed_at"] = now.isoformat()
    trader.truth["positions"] = []
    trader.truth["pending_orders"] = []
    service.refresh(GATE_TESTNET_ACCOUNT_ID, trader)
    assert ledger.reserve_risk(
        GATE_TESTNET_ACCOUNT_ID,
        "reserve-with-remote",
        Decimal("10"),
        Decimal("20"),
        now=now,
        instrument_id="BTCUSDT",
        venue="gate",
        mode="TESTNET",
        max_single_risk_fraction=Decimal("0.01"),
        max_portfolio_risk_fraction=Decimal("0.01"),
        max_cluster_risk_fraction=Decimal("0.01"),
    )


def test_gate_gateway_open_and_reduce_only_use_remote_position_without_local_mirror(tmp_path, monkeypatch):
    store = _store(tmp_path)
    provision_default_gate_accounts(store)
    ledger = AccountLedger(store)
    now = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)

    class RemoteOrderTrader:
        testnet = True
        live_trading_enabled = True

        def __init__(self):
            self.open = False
            self.orders = []
            self.position_contracts = "1"

        def get_market_metadata(self, symbol):
            return {
                "symbol": symbol,
                "precision": {"amount": 1, "price": 0.5},
                "limits": {"amount": {"step": 1, "min": 1, "max": 100}},
                "contractSize": 1,
                "leverage_max": 100,
                "taker": 0.0005,
            }

        def get_account_truth(self, *, include_trades=False):
            return {
                "status": "AVAILABLE",
                "observed_at": now.isoformat(),
                "equity": 1000.0,
                "available_margin": 990.0 if self.open else 1000.0,
                "used_margin": 10.0 if self.open else 0.0,
                "positions": ([{
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "contracts": self.position_contracts,
                    "entry_price": "100",
                    "mark_price": "100",
                    "contract_size": "1",
                    "position_id": "remote-gateway-pos",
                }] if self.open else []),
                "pending_orders": ([{
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "reduce_only": True,
                    "stop_price": 99,
                }] if self.open else []),
                "fills": [],
            }

        def place_order(self, **kwargs):
            self.orders.append(dict(kwargs))
            if kwargs.get("reduce_only"):
                self.open = False
                return {"status": "FILLED", "order_id": "123456002", "filled": kwargs["amount"], "amount": kwargs["amount"], "average_price": 101.0, "fee": 0.01, "contract_size": 1}
            self.open = True
            return {"status": "FILLED", "order_id": "123456001", "filled": kwargs["amount"], "amount": kwargs["amount"], "average_price": 100.0, "fee": 0.01, "contract_size": 1, "protection_verified": True}

    trader = RemoteOrderTrader()
    monkeypatch.setattr("core.trading.gate_accounts.build_gate_trader", lambda _store, _account_id: trader)
    auth = AuthorizationManager(store).grant_authorization(
        account_id=GATE_TESTNET_ACCOUNT_ID,
        venue="gate",
        mode=TradingMode.TESTNET,
        decision_path=DecisionPath.STRATEGY_DRIVEN,
        allowed_instruments=["BTCUSDT"],
        allowed_sides=["LONG"],
        max_risk_fraction=Decimal("0.0025"),
        max_leverage=2,
        duration_seconds=3600,
        confirmed_by=ConfirmationSource.LOCAL_USER_WIZARD,
    )
    market = {
        "price": 100.0,
        "data_as_of": now.isoformat(),
        "received_at": now.isoformat(),
        "fresh": True,
        "executable": True,
        "freshness_status": "FRESH",
        "stale_after_seconds": 120,
        "slippage": 0.001,
        "market": trader.get_market_metadata("BTCUSDT"),
    }
    gateway = ExecutionGateway(store, ledger=ledger)
    opened = gateway.submit_intent(
        OrderIntent(
            intent_id="intent_ai_remote_gateway_open",
            idempotency_key="remote-gateway-open-idem",
            account_id=GATE_TESTNET_ACCOUNT_ID,
            mode=TradingMode.TESTNET,
            environment="TESTNET",
            venue="gate",
            instrument_id="BTCUSDT",
            side="LONG",
            order_type="market",
            quantity=1,
            leverage=100,
            decision_path=DecisionPath.AI_LED,
            control_mode="AUTONOMOUS",
            protection_plan=ProtectionPlan(stop_price=99, take_profit=101),
            authorization_id=auth.authorization_id,
            authorization_version=auth.version,
        ),
        trader_client=trader,
        market_snapshot=market,
        now=now,
    )
    assert opened["status"] == "FILLED"
    assert trader.orders[0]["leverage"] == 30
    with store._connect() as db:
        persisted = db.execute(
            "SELECT leverage FROM order_intents WHERE intent_id='intent_ai_remote_gateway_open'"
        ).fetchone()
    assert persisted["leverage"] == 30
    assert opened["protection_status"] == "ACTIVE"

    # Gate exposes a net position. A same-side manual addition makes the
    # remote quantity differ from the system's recorded fills and must block
    # management before the adapter sees a reduction.
    trader.position_contracts = "2"
    with pytest.raises(GatewayError) as mixed:
        gateway.submit_intent(
            OrderIntent(
                intent_id="intent_close_mixed_position",
                idempotency_key="remote-gateway-mixed-close-idem",
                account_id=GATE_TESTNET_ACCOUNT_ID,
                mode=TradingMode.TESTNET,
                environment="TESTNET",
                venue="gate",
                instrument_id="BTCUSDT",
                side="SELL",
                order_type="market",
                quantity=1,
                reduce_only=True,
                position_id="remote-gateway-pos",
                decision_path=DecisionPath.AI_LED,
                control_mode="AUTONOMOUS",
            ),
            trader_client=trader,
            market_snapshot=market,
            now=now,
        )
    assert mixed.value.code == "SYSTEM_POSITION_OWNERSHIP_UNVERIFIED"
    assert "REMOTE_NET_QUANTITY_MISMATCH" in mixed.value.message
    assert len(trader.orders) == 1
    trader.position_contracts = "1"

    # The ledger may preserve either its canonical position-side spelling or
    # an exchange BUY/SELL spelling. Both LONG->BUY and SHORT->SELL entry
    # encodings are valid; an adverse side is never ownership evidence.
    with store._connect() as db:
        fill = db.execute(
            "SELECT fill_id,payload_json FROM trade_fills WHERE order_id='123456001'"
        ).fetchone()
        fill_payload = json.loads(fill["payload_json"])
        fill_payload["side"] = "BUY"
        db.execute(
            "UPDATE trade_fills SET side='BUY',payload_json=? WHERE fill_id=?",
            (json.dumps(fill_payload), fill["fill_id"]),
        )
        for historical_id, historical_status, historical_receipt in (
            ("intent_ai_historical_rejected", "REJECTED", {"status": "REJECTED"}),
            ("intent_ai_historical_resting", "ACKNOWLEDGED", {"status": "ACKNOWLEDGED", "order_id": "123456090", "filled": 0}),
        ):
            db.execute(
                """INSERT INTO order_intents
                   (intent_id,idempotency_key,account_id,mode,instrument_id,side,order_type,quantity,
                    payload_hash,status,execution_result_json,created_at,updated_at,venue,environment,
                    reduce_only,decision_path,control_mode,selection_evidence_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    historical_id, historical_id + "_idem", GATE_TESTNET_ACCOUNT_ID,
                    "TESTNET", "BTCUSDT", "LONG", "market", 1, "historical-hash",
                    historical_status, json.dumps(historical_receipt), now.isoformat(), now.isoformat(),
                    "gate", "TESTNET", 0, "AI_LED", "AUTONOMOUS", "{}",
                ),
            )

    # A wrong-way fill is rejected before any reduction reaches Gate.
    with store._connect() as db:
        fill = db.execute(
            "SELECT fill_id,payload_json FROM trade_fills WHERE order_id='123456001'"
        ).fetchone()
        fill_payload = json.loads(fill["payload_json"])
        fill_payload["side"] = "SELL"
        db.execute(
            "UPDATE trade_fills SET side='SELL',payload_json=? WHERE fill_id=?",
            (json.dumps(fill_payload), fill["fill_id"]),
        )
    with pytest.raises(GatewayError) as wrong_way:
        gateway.submit_intent(
            OrderIntent(
                intent_id="intent_close_wrong_way_fill",
                idempotency_key="remote-gateway-wrong-way-idem",
                account_id=GATE_TESTNET_ACCOUNT_ID,
                mode=TradingMode.TESTNET,
                environment="TESTNET",
                venue="gate",
                instrument_id="BTCUSDT",
                side="SELL",
                order_type="market",
                quantity=1,
                reduce_only=True,
                position_id="remote-gateway-pos",
                decision_path=DecisionPath.AI_LED,
                control_mode="AUTONOMOUS",
            ),
            trader_client=trader,
            market_snapshot=market,
            now=now,
        )
    assert wrong_way.value.code == "SYSTEM_POSITION_OWNERSHIP_UNVERIFIED"
    assert "SYSTEM_FILL_SCOPE_OR_DIRECTION_MISMATCH" in wrong_way.value.message
    assert len(trader.orders) == 1
    with store._connect() as db:
        fill = db.execute(
            "SELECT fill_id,payload_json FROM trade_fills WHERE order_id='123456001'"
        ).fetchone()
        fill_payload = json.loads(fill["payload_json"])
        fill_payload["side"] = "BUY"
        db.execute(
            "UPDATE trade_fills SET side='BUY',payload_json=? WHERE fill_id=?",
            (json.dumps(fill_payload), fill["fill_id"]),
        )

    closed = gateway.submit_intent(
        OrderIntent(
            intent_id="intent_close_remote_gateway_close",
            idempotency_key="remote-gateway-close-idem",
            account_id=GATE_TESTNET_ACCOUNT_ID,
            mode=TradingMode.TESTNET,
            environment="TESTNET",
            venue="gate",
            instrument_id="BTCUSDT",
            side="SELL",
            order_type="market",
            quantity=1,
            decision_path=DecisionPath.AI_LED,
            control_mode="AUTONOMOUS",
            reduce_only=True,
            position_id="remote-gateway-pos",
        ),
        trader_client=trader,
        market_snapshot=market,
        now=now,
    )
    assert closed["status"] == "FILLED"
    assert ledger.get_open_positions(GATE_TESTNET_ACCOUNT_ID, venue="gate", mode="TESTNET") == []
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM simulated_positions WHERE account_id=? AND venue='gate' AND mode='TESTNET'",
            (GATE_TESTNET_ACCOUNT_ID,),
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM trade_fills WHERE account_id=? AND venue='gate' AND mode='TESTNET'",
            (GATE_TESTNET_ACCOUNT_ID,),
        ).fetchone()[0] == 2
        close_row = db.execute(
            "SELECT selection_evidence_json FROM order_intents WHERE intent_id='intent_close_remote_gateway_close'"
        ).fetchone()
    close_proof = json.loads(close_row["selection_evidence_json"])["system_position_ownership"]
    assert close_proof["status"] == "VERIFIED"
    assert close_proof["owned_net_quantity"] == "1"

    # A durable FILLED claim without trade_fills cannot be attributed after a
    # restart and must never be silently omitted from the ownership balance.
    with store._connect() as db:
        db.execute(
            """INSERT INTO order_intents
               (intent_id,idempotency_key,account_id,mode,instrument_id,side,order_type,quantity,
                payload_hash,status,execution_result_json,created_at,updated_at,venue,environment,
                reduce_only,decision_path,control_mode,selection_evidence_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "intent_ai_claimed_fill_without_ledger", "claimed-fill-without-ledger-idem",
                GATE_TESTNET_ACCOUNT_ID, "TESTNET", "BTCUSDT", "LONG", "market", 1,
                "missing-fill-hash", "FILLED",
                json.dumps({"status": "FILLED", "intent_id": "intent_ai_claimed_fill_without_ledger", "order_id": "123456099", "filled": 1}),
                now.isoformat(), now.isoformat(), "gate", "TESTNET", 0, "AI_LED", "AUTONOMOUS", "{}",
            ),
        )
    with pytest.raises(GatewayError) as missing_fill:
        gateway._gate_remote_position_ownership_evidence(
            account_id=GATE_TESTNET_ACCOUNT_ID,
            mode="TESTNET",
            environment="TESTNET",
            instrument_id="BTCUSDT",
            side="LONG",
            position={
                "venue": "gate", "mode": "TESTNET", "symbol": "BTCUSDT", "side": "LONG",
                "contracts": "1", "entry_price": "100", "position_id": "remote-gateway-pos",
            },
            truth={"status": "AVAILABLE", "account_id": GATE_TESTNET_ACCOUNT_ID, "api_environment": "TESTNET"},
        )
    assert missing_fill.value.code == "SYSTEM_POSITION_OWNERSHIP_UNVERIFIED"
    assert "SYSTEM_FILL_RECONCILIATION_MISSING" in missing_fill.value.message


@pytest.mark.parametrize(
    "events,remote_position,expected_entry",
    [
        (
            [
                {"order": "810001", "side": "LONG", "qty": "1", "price": "100", "at": 1},
                {"order": "810002", "side": "SELL", "qty": "0.5", "price": "105", "reduce": True, "at": 2, "pre_qty": "1", "pre_entry": "100", "position_id": "epoch-1"},
                {"order": "810003", "side": "BUY", "qty": "1", "price": "120", "at": 3},
            ],
            {"contracts": "1.5", "entry_price": str(Decimal("170") / Decimal("1.5")), "position_id": "epoch-1"},
            Decimal("170") / Decimal("1.5"),
        ),
        (
            [
                {"order": "820001", "side": "LONG", "qty": "1", "price": "100", "at": 1},
                {"order": "820002", "side": "SELL", "qty": "1", "price": "101", "reduce": True, "at": 2, "pre_qty": "1", "pre_entry": "100", "position_id": "epoch-old"},
                {"order": "820003", "side": "BUY", "qty": "2", "price": "120", "at": 3},
            ],
            {"contracts": "2", "entry_price": "120", "position_id": "epoch-new"},
            Decimal("120"),
        ),
    ],
    ids=("partial-reduce-then-add", "flat-then-reopen"),
)
def test_gate_ownership_replays_cost_basis_after_reduce_and_reentry(tmp_path, events, remote_position, expected_entry):
    store = _store(tmp_path)
    provision_default_gate_accounts(store)
    ledger = AccountLedger(store)
    gateway = ExecutionGateway(store, ledger=ledger)
    start = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)
    current_quantity = Decimal("0")
    current_cost = Decimal("0")
    entry_order_ids = []
    reduction_order_ids = []

    def persist_intent(event, proof=None):
        is_reduction = bool(event.get("reduce"))
        intent_id = f"intent_close_{event['order']}" if is_reduction else f"intent_ai_{event['order']}"
        side = event["side"]
        stamp = (start + timedelta(minutes=event["at"])).isoformat()
        receipt = {
            "intent_id": intent_id, "order_id": event["order"], "status": "FILLED",
            "filled": event["qty"], "average_price": event["price"],
        }
        selection = {"system_position_ownership": proof} if proof else {}
        with store._connect() as db:
            db.execute(
                """INSERT INTO order_intents
                   (intent_id,idempotency_key,account_id,mode,instrument_id,side,order_type,quantity,
                    payload_hash,status,execution_result_json,created_at,updated_at,venue,environment,
                    reduce_only,position_id,decision_path,control_mode,selection_evidence_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    intent_id, intent_id + "_idem", GATE_TESTNET_ACCOUNT_ID, "TESTNET", "BTCUSDT",
                    side, "market", float(event["qty"]), "replay-hash", "FILLED",
                    json.dumps(receipt), stamp, stamp, "gate", "TESTNET", int(is_reduction),
                    event.get("position_id"), "AI_LED", "AUTONOMOUS", json.dumps(selection),
                ),
            )
        ledger.record_trade_fill(
            account_id=GATE_TESTNET_ACCOUNT_ID,
            instrument_id="BTCUSDT",
            side=side,
            quantity=Decimal(event["qty"]),
            price=Decimal(event["price"]),
            fee=Decimal("0"),
            mode="TESTNET",
            venue="gate",
            order_id=event["order"],
            event_id=f"event-{event['order']}",
            trade_id=f"trade-{event['order']}",
            reduce_only=is_reduction,
            position_id=event.get("position_id"),
            environment="TESTNET",
            event_at=start + timedelta(minutes=event["at"]),
        )

    for event in events:
        if event.get("reduce"):
            average = current_cost / current_quantity
            proof = {
                "version": "gate_system_position_ownership_v1", "status": "VERIFIED",
                "account_id": GATE_TESTNET_ACCOUNT_ID, "environment": "TESTNET", "venue": "gate",
                "instrument_id": "BTCUSDT", "side": "LONG",
                "remote_position_id": event["position_id"],
                "remote_quantity": event["pre_qty"], "owned_net_quantity": event["pre_qty"],
                "remote_entry_price": event["pre_entry"], "owned_average_entry_price": event["pre_entry"],
                "entry_order_ids": list(entry_order_ids), "reduction_order_ids": list(reduction_order_ids),
            }
            assert current_quantity == Decimal(event["pre_qty"])
            assert average == Decimal(event["pre_entry"])
            current_quantity -= Decimal(event["qty"])
            current_cost -= average * Decimal(event["qty"])
            if current_quantity == 0:
                current_cost = Decimal("0")
            reduction_order_ids.append(event["order"])
            persist_intent(event, proof)
        else:
            current_quantity += Decimal(event["qty"])
            current_cost += Decimal(event["qty"]) * Decimal(event["price"])
            entry_order_ids.append(event["order"])
            persist_intent(event)

    evidence = gateway._gate_remote_position_ownership_evidence(
        account_id=GATE_TESTNET_ACCOUNT_ID,
        mode="TESTNET",
        environment="TESTNET",
        instrument_id="BTCUSDT",
        side="LONG",
        position={
            "venue": "gate", "mode": "TESTNET", "symbol": "BTCUSDT", "side": "LONG",
            **remote_position,
        },
        truth={"status": "AVAILABLE", "account_id": GATE_TESTNET_ACCOUNT_ID, "api_environment": "TESTNET"},
    )
    assert evidence["owned_net_quantity"] == remote_position["contracts"]
    assert Decimal(evidence["owned_average_entry_price"]) == pytest.approx(expected_entry)


def test_gate_gateway_rejects_manual_remote_position_without_system_fills(tmp_path, monkeypatch):
    store = _store(tmp_path)
    provision_default_gate_accounts(store)
    ledger = AccountLedger(store)
    now = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)

    class ManualPositionTrader:
        testnet = True
        live_trading_enabled = True

        def __init__(self):
            self.orders = []

        def get_market_metadata(self, symbol):
            return {
                "symbol": symbol,
                "precision": {"amount": 1, "price": 0.5},
                "limits": {"amount": {"step": 1, "min": 1, "max": 100}},
                "contractSize": 1,
                "leverage_max": 100,
                "taker": 0.0005,
            }

        def get_account_truth(self, *, include_trades=False):
            return {
                "status": "AVAILABLE",
                "api_environment": "TESTNET",
                "observed_at": now.isoformat(),
                "equity": 1000.0,
                "available_margin": 990.0,
                "used_margin": 10.0,
                "positions": [{
                    "symbol": "BTCUSDT", "side": "LONG", "contracts": "1",
                    "entry_price": "100", "mark_price": "100", "contract_size": "1",
                    "position_id": "manual-gate-position",
                }],
                "pending_orders": [],
                "fills": [],
            }

        def place_order(self, **kwargs):
            self.orders.append(dict(kwargs))
            return {"status": "FILLED", "order_id": "987654321", "filled": kwargs["amount"],
                    "amount": kwargs["amount"], "average_price": 100.0, "fee": 0.01}

    trader = ManualPositionTrader()
    monkeypatch.setattr("core.trading.gate_accounts.build_gate_trader", lambda _store, _account_id: trader)
    gateway = ExecutionGateway(store, ledger=ledger)
    with pytest.raises(GatewayError) as rejected:
        gateway.submit_intent(
            OrderIntent(
                intent_id="intent_close_manual_position",
                idempotency_key="manual-gate-close-idem",
                account_id=GATE_TESTNET_ACCOUNT_ID,
                mode=TradingMode.TESTNET,
                environment="TESTNET",
                venue="gate",
                instrument_id="BTCUSDT",
                side="SELL",
                order_type="market",
                quantity=1,
                reduce_only=True,
                position_id="manual-gate-position",
                decision_path=DecisionPath.AI_LED,
                control_mode="AUTONOMOUS",
            ),
            trader_client=trader,
            market_snapshot={
                "price": 100.0,
                "data_as_of": now.isoformat(),
                "received_at": now.isoformat(),
                "fresh": True,
                "executable": True,
                "freshness_status": "FRESH",
                "stale_after_seconds": 120,
                "market": trader.get_market_metadata("BTCUSDT"),
            },
            now=now,
        )

    assert rejected.value.code == "SYSTEM_POSITION_OWNERSHIP_UNVERIFIED"
    assert "SYSTEM_FILLED_ENTRY_NOT_FOUND" in rejected.value.message
    assert trader.orders == []
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM trade_fills").fetchone()[0] == 0


def test_ai_cycle_persists_manual_remote_position_ownership_rejection(tmp_path):
    store = _store(tmp_path)
    provision_default_gate_accounts(store)
    ledger = AccountLedger(store)
    gateway = ExecutionGateway(store, ledger=ledger)
    engine = AILedDecisionEngine(
        store=store,
        execution_gateway=gateway,
        risk_engine=RiskEngine(ledger),
        ledger=ledger,
        guardian=PositionGuardian(store, ledger),
    )
    now = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)
    context = AICycleContext(
        cycle_id="cycle-manual-gate-position-close",
        account_id=GATE_TESTNET_ACCOUNT_ID,
        generation=1,
        started_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=1)).isoformat(),
        allowed_instruments=("BTCUSDT",),
        mode=TradingMode.TESTNET,
        venue="gate",
        environment="TESTNET",
        execution_environment="TESTNET",
        account_truth={
            "status": "AVAILABLE", "account_id": GATE_TESTNET_ACCOUNT_ID,
            "api_environment": "TESTNET", "positions_status": "AVAILABLE",
            "positions": [{
                "symbol": "BTCUSDT", "side": "LONG", "contracts": "1",
                "entry_price": "100", "mark_price": "100", "position_id": "manual-position",
            }],
            "pending_orders": [],
        },
    )
    result = engine.execute_cycle(
        context,
        now=now,
        model_output=AIActionOutput(
            action="CLOSE_POSITION", instrument_id="BTCUSDT", position_id="manual-position",
            reason="Fixture model requested a close.",
        ),
    )
    assert result.status == "REJECTED"
    assert result.reason == "GATE_SYSTEM_POSITION_OWNERSHIP_UNVERIFIED:SYSTEM_FILLED_ENTRY_NOT_FOUND"
    with store._connect() as db:
        row = db.execute(
            "SELECT action,status,payload_json FROM ai_led_cycles WHERE cycle_id=?",
            (context.cycle_id,),
        ).fetchone()
    assert row["action"] == "CLOSE_POSITION"
    assert row["status"] == "REJECTED"
    payload = json.loads(row["payload_json"])
    assert payload["analysis"]["system_position_ownership"] == {
        "status": "UNVERIFIED", "reason_code": "SYSTEM_FILLED_ENTRY_NOT_FOUND",
    }


def test_ai_system_block_is_not_persisted_as_model_wait(tmp_path):
    store = _store(tmp_path)
    ledger = AccountLedger(store)
    account_id = "ai-paper-fixture"
    ledger.create_account(account_id, mode="PAPER", initial_deposit=Decimal("10000"))
    engine = AILedDecisionEngine(
        store=store,
        execution_gateway=ExecutionGateway(store),
        risk_engine=RiskEngine(ledger),
        ledger=ledger,
        guardian=PositionGuardian(store, ledger),
    )
    now = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)

    blocked_context = AICycleContext(
        cycle_id="cycle-system-block",
        account_id=account_id,
        generation=1,
        started_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=1)).isoformat(),
        allowed_instruments=("BTCUSDT",),
    )
    blocked = engine.execute_cycle(blocked_context, now=now)
    assert blocked.status == "BLOCKED"
    with store._connect() as db:
        row = db.execute(
            "SELECT action, decision_origin, model_called, model_result, block_stage FROM ai_led_cycles WHERE cycle_id=?",
            (blocked_context.cycle_id,),
        ).fetchone()
    assert tuple(row) == ("SYSTEM_BLOCKED", "SYSTEM", 0, "NOT_RUN", "AI_MODEL")

    model_context = AICycleContext(
        cycle_id="cycle-model-wait",
        account_id=account_id,
        generation=2,
        started_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=1)).isoformat(),
        allowed_instruments=("BTCUSDT",),
    )
    model_wait = engine.execute_cycle(
        model_context,
        now=now,
        model_output=AIActionOutput(
            action="WAIT",
            instrument_id="BTCUSDT",
            reason="Fixture model has no confirmed edge.",
        ),
    )
    assert model_wait.status == "WAITING"
    with store._connect() as db:
        row = db.execute(
            "SELECT action, decision_origin, model_called, model_result FROM ai_led_cycles WHERE cycle_id=?",
            (model_context.cycle_id,),
        ).fetchone()
    assert tuple(row) == ("WAIT", "MODEL", 1, "WAIT")


class _ReadOnlyExchange:
    def __init__(self):
        self.create_order_calls = 0

    def fetch_balance(self):
        return {"USDT": {"total": 100.0, "free": 100.0, "used": 0.0}}

    def fetch_positions(self):
        return []

    def fetch_open_orders(self, symbol=None, limit=None):
        return []

    def load_markets(self):
        return {}

    def create_order(self, *args, **kwargs):
        self.create_order_calls += 1
        raise AssertionError("read-only connection test must never create an order")


def test_gate_futures_balance_uses_native_margin_and_unrealised_pnl_fields():
    class Exchange:
        def fetch_balance(self):
            return {
                "USDT": {"total": 0.0, "free": 50153.16, "used": -50153.16},
                "info": [{
                    "currency": "USDT",
                    "total": "1000",
                    "available": "970",
                    "position_margin": "25",
                    "order_margin": "5",
                    "unrealised_pnl": "-12.5",
                    "history": {"pnl": "7.25"},
                }],
            }

    trader = GateLiveTrader("key", "secret", testnet=True, exchange=Exchange(), live_trading_enabled=False)
    balance = trader.get_account_balance()

    assert balance["data_status"] == "AVAILABLE"
    assert balance["total"] == 1000.0
    assert balance["free"] == 970.0
    assert balance["used"] == 30.0
    assert balance["used"] >= 0
    assert balance["equity"] == 987.5
    assert balance["unrealized_pnl"] == -12.5
    assert balance["realized_pnl"] == 7.25
    assert balance["equity_basis"] == "GATE_TOTAL_PLUS_UNREALISED_PNL"


def test_connection_test_is_read_only_and_does_not_create_authorization(tmp_path):
    exchange = _ReadOnlyExchange()
    trader = GateLiveTrader("key", "secret", testnet=True, exchange=exchange, live_trading_enabled=False)
    result = trader.connection_test()
    assert result["read_only"] is True
    assert result["orders_sent"] == 0
    assert result["model_called"] is False
    assert result["authorization_created"] is False
    assert exchange.create_order_calls == 0


@pytest.mark.parametrize(
    "finish_as,expected_triggered",
    [("succeeded", "9002"), ("cancelled", None), ("failed", None), ("expired", None)],
)
def test_gate_protection_readback_preserves_native_identity_without_fabricating_trigger_actor(finish_as, expected_triggered):
    class Exchange:
        def privateFuturesGetSettlePriceOrdersOrderId(self, params):
            assert params == {"settle": "usdt", "order_id": "9001"}
            return {
                "id": 9001,
                "id_string": "9001",
                "status": "finished",
                "finish_time": 20300102120000,
                "finish_as": finish_as,
                "trade_id": "9002",
                "me_order_id": "8001",
                "initial": {"contract": "BTC_USDT", "size": -2, "is_reduce_only": True, "text": "t-e2e-sl"},
                "trigger": {"price": "89.0", "price_type": 1},
                "api_secret": "must-not-persist",
            }

    trader = GateLiveTrader("key", "secret", testnet=True, exchange=Exchange())

    result = trader.fetch_protection_order("9001", "BTCUSDT")

    assert result["order_id"] == result["id_string"] == "9001"
    assert result["initial"] == {"contract": "BTC_USDT", "size": -2, "is_reduce_only": True, "text": "t-e2e-sl"}
    assert result["finish_time"] == 20300102120000
    assert result["trade_id"] == "9002"
    assert result["me_order_id"] == "8001"
    assert result["triggered_order_id"] == expected_triggered
    assert result["raw"]["initial"]["contract"] == "BTC_USDT"
    assert "api_secret" not in result["raw"]


def test_gate_protection_readback_rejects_conflicting_native_reduce_only_flags():
    class Exchange:
        def privateFuturesGetSettlePriceOrdersOrderId(self, _params):
            return {
                "id": "9001", "status": "finished", "finish_as": "cancelled",
                "initial": {
                    "contract": "BTC_USDT", "size": -1,
                    "reduce_only": False, "is_reduce_only": True,
                },
                "trigger": {"price": "89.0", "price_type": 1},
            }

    trader = GateLiveTrader("key", "secret", testnet=True, exchange=Exchange())

    with pytest.raises(ValueError, match="GATE_PROTECTION_REDUCE_ONLY_CONFLICT"):
        trader.fetch_protection_order("9001", "BTCUSDT")


def test_gate_protection_readback_normalizes_gate_is_reduce_only_without_reduce_only_field():
    class Exchange:
        def privateFuturesGetSettlePriceOrdersOrderId(self, _params):
            return {
                "id": "9001", "status": "open",
                "initial": {"contract": "BTC_USDT", "size": -1, "is_reduce_only": True},
                "trigger": {"price": "89.0", "price_type": 1},
            }

    trader = GateLiveTrader("key", "secret", testnet=True, exchange=Exchange())

    result = trader.fetch_protection_order("9001", "BTCUSDT")

    assert result["reduce_only"] is True
    assert "reduce_only" not in result["initial"]
    assert result["initial"]["is_reduce_only"] is True


class _E2EFixtureTrader:
    testnet = True
    live_trading_enabled = True

    def __init__(self):
        self.open = False
        self.calls: list[dict[str, Any]] = []
        self.canceled: list[str] = []
        self.protection_status = "OPEN"
        self.protection_cleanup_mode = "success"
        self.protection_native_is_reduce_only = False

    def get_market_metadata(self, symbol):
        return {
            "symbol": symbol,
            "precision": {"amount": 0.1, "price": 0.5},
            "limits": {"amount": {"step": 0.1, "min": 0.1, "max": 10}},
            "contractSize": 1,
            "leverage_max": 100,
            "taker": 0.0005,
            "source": "fixture_gate_metadata",
        }

    def get_ticker(self, symbol):
        return {"status": "AVAILABLE", "symbol": symbol, "last": 100.0, "observed_at": "2030-01-02T12:00:00+00:00"}

    def set_leverage(self, symbol, leverage):
        return {"acknowledged": True, "dry_run": False, "symbol": symbol, "leverage": leverage}

    def place_order(self, **kwargs):
        self.calls.append(dict(kwargs))
        if kwargs.get("reduce_only"):
            self.open = False
            return {"status": "FILLED", "order_id": "cleanup-1", "filled": kwargs["amount"]}
        self.open = True
        return {
            "status": "FILLED",
            "order_id": "entry-1",
            "filled": kwargs["amount"],
            "protection_status": "PROTECTED",
            "protection_orders": [{"leg": "stop_loss", "order_id": "9001"}],
        }

    def get_account_truth(self, *, include_trades=False):
        return {
            "status": "AVAILABLE",
            "observed_at": "2030-01-02T12:00:00+00:00",
            "equity": 1000.0,
            "available_margin": 1000.0 if not self.open else 990.0,
            "used_margin": 0.0 if not self.open else 10.0,
            "positions": ([{"symbol": "BTCUSDT", "side": "LONG", "contracts": "0.1", "position_id": "remote-pos"}] if self.open else []),
            "pending_orders": [],
            "fills": [],
        }

    def cancel_order(self, order_id, symbol):
        self.canceled.append(order_id)
        return {"status": "CANCELED", "order_id": order_id}

    def fetch_protection_order(self, order_id, symbol):
        initial = {"contract": "BTC_USDT", "size": "0.1", "reduce_only": True, "text": "t-e2e-sl"}
        if self.protection_native_is_reduce_only:
            initial = {"contract": "BTC_USDT", "size": "0.1", "is_reduce_only": True, "text": "t-e2e-sl"}
        return {
            "order_id": str(order_id), "symbol": symbol, "status": self.protection_status,
            "finish_as": "cancelled" if self.protection_status == "FINISHED" else None,
            "reduce_only": True,
            "initial": initial,
            "observed_at": "2030-01-02T12:00:00+00:00",
        }

    def cancel_protection_order(self, order_id, symbol):
        self.canceled.append(order_id)
        if self.protection_cleanup_mode == "delete_fails":
            raise RuntimeError("DELETE failed")
        if self.protection_cleanup_mode != "nonterminal":
            self.protection_status = "FINISHED"
        return {"status": "CANCELED", "order_id": order_id}


def _assert_gate_testnet_e2e_uses_remote_fill_and_cleans_without_local_fill(
    tmp_path, *, native_is_reduce_only: bool,
):
    store = _store(tmp_path)
    provision_default_gate_accounts(store)
    trader = _E2EFixtureTrader()
    trader.protection_native_is_reduce_only = native_is_reduce_only
    request = {
        "account_id": "gate_paper",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "stop_type": "PRICE",
        "stop_value": 99,
        "take_profit_type": "PRICE",
        "take_profit_value": 101,
        "leverage": 1,
        "cleanup": True,
        "confirm_testnet": True,
        "idempotency_key": "fixture-e2e-1",
    }
    result = GateTestnetE2EService(store, clock=lambda: datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)).run(request, trader)
    assert result["status"] == "COMPLETED"
    assert result["account_id"] == GATE_TESTNET_ACCOUNT_ID
    assert result["orders_sent"] == 2
    assert result["local_fill_created"] is False
    assert len(trader.calls) == 2
    assert trader.calls[1]["reduce_only"] is True
    assert trader.calls[1]["leverage"] is None
    assert trader.canceled == ["9001"]
    assert result["protection_cleanup"][0]["finish_as"] == "cancelled"
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM trade_fills").fetchone()[0] == 0
    replay = GateTestnetE2EService(store).run(request, trader)
    assert replay["idempotent_replay"] is True
    assert len(trader.calls) == 2
    replay_without_credentials = GateTestnetE2EService(store).run(request, None)
    assert replay_without_credentials["idempotent_replay"] is True


def test_gate_testnet_e2e_uses_remote_fill_and_cleans_without_local_fill(tmp_path):
    _assert_gate_testnet_e2e_uses_remote_fill_and_cleans_without_local_fill(
        tmp_path, native_is_reduce_only=False,
    )


def test_gate_testnet_e2e_handles_native_is_reduce_only_field(tmp_path):
    _assert_gate_testnet_e2e_uses_remote_fill_and_cleans_without_local_fill(
        tmp_path, native_is_reduce_only=True,
    )


@pytest.mark.parametrize("cleanup_mode", ["delete_fails", "nonterminal"])
def test_gate_testnet_e2e_never_completes_when_native_protection_cleanup_is_unverified(tmp_path, cleanup_mode):
    store = _store(tmp_path)
    provision_default_gate_accounts(store)
    trader = _E2EFixtureTrader()
    trader.protection_cleanup_mode = cleanup_mode
    request = {
        "account_id": GATE_TESTNET_ACCOUNT_ID,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "stop_type": "PRICE",
        "stop_value": 99,
        "take_profit_type": "PRICE",
        "take_profit_value": 101,
        "leverage": 1,
        "cleanup": True,
        "confirm_testnet": True,
        "idempotency_key": f"fixture-e2e-protection-{cleanup_mode}",
    }

    result = GateTestnetE2EService(store).run(request, trader)

    assert result["status"] == "RECONCILIATION_REQUIRED"
    assert result["error_code"] == "PROTECTION_CLEANUP_FAILED"
    assert not any(stage["stage"] == "CLEANUP" and stage["status"] == "COMPLETED" for stage in result["stages"])
