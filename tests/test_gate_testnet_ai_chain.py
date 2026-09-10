"""Focused regression coverage for the Gate TestNet + AI main chain.

These tests use isolated SQLite stores and deterministic provider doubles.  A
fixture passing here proves the contract and accounting boundaries, not the
availability or quality of a real Gate credential or Qwen inference.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from core.storage import SQLiteStore
from core.trading.account_aliases import GATE_TESTNET_ACCOUNT_ID
from core.trading.ai_led_engine import AICycleContext, AIActionOutput, AILedDecisionEngine
from core.trading.execution_gateway import ExecutionGateway
from core.trading.gate_account_truth import GateAccountTruthService
from core.trading.gate_live_client import GateLiveTrader
from core.trading.gate_testnet_e2e import GateTestnetE2EService
from core.trading.gate_accounts import provision_default_gate_accounts
from core.trading.ledger import AccountLedger
from core.trading.position_guardian import PositionGuardian
from core.trading.risk_engine import RiskEngine


def _store(tmp_path):
    store = SQLiteStore(tmp_path / "gate-testnet-ai-chain.db")
    store.initialize()
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


def test_gate_remote_truth_is_authoritative_and_mirror_is_not_a_fill(tmp_path):
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
    assert ledger.get_open_positions(GATE_TESTNET_ACCOUNT_ID, venue="gate", mode="TESTNET")
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM trade_fills").fetchone()[0] == 0


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


def test_connection_test_is_read_only_and_does_not_create_authorization(tmp_path):
    exchange = _ReadOnlyExchange()
    trader = GateLiveTrader("key", "secret", testnet=True, exchange=exchange, live_trading_enabled=False)
    result = trader.connection_test()
    assert result["read_only"] is True
    assert result["orders_sent"] == 0
    assert result["model_called"] is False
    assert result["authorization_created"] is False
    assert exchange.create_order_calls == 0


class _E2EFixtureTrader:
    testnet = True
    live_trading_enabled = True

    def __init__(self):
        self.open = False
        self.calls: list[dict[str, Any]] = []
        self.canceled: list[str] = []

    def get_market_metadata(self, symbol):
        return {
            "symbol": symbol,
            "precision": {"amount": 0.1, "price": 0.5},
            "limits": {"amount": {"step": 0.1, "min": 0.1, "max": 10}},
            "contractSize": 1,
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
            "protection_orders": [{"order_id": "sl-tp-1"}],
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


def test_gate_testnet_e2e_uses_remote_fill_and_cleans_without_local_fill(tmp_path):
    store = _store(tmp_path)
    provision_default_gate_accounts(store)
    trader = _E2EFixtureTrader()
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
    assert trader.canceled == ["sl-tp-1"]
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM trade_fills").fetchone()[0] == 0
    replay = GateTestnetE2EService(store).run(request, trader)
    assert replay["idempotent_replay"] is True
    assert len(trader.calls) == 2
    replay_without_credentials = GateTestnetE2EService(store).run(request, None)
    assert replay_without_credentials["idempotent_replay"] is True
