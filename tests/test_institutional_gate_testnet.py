"""Focused institutional acceptance tests for the Gate TestNet boundary.

These tests use deterministic exchange doubles.  They verify routing and
response semantics without claiming that a fixture is evidence of a live
Qwen or Gate account.
"""

from types import ModuleType
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from apps.api.v2 import router_for
from core.security.credentials import CredentialVault
from core.storage import SQLiteStore
from core.trading.gate_accounts import (
    GATE_LIVE_ACCOUNT_ID,
    GATE_PAPER_ACCOUNT_ID,
    build_gate_trader,
    provision_default_gate_accounts,
    save_gate_account_credentials,
)
from core.trading.gate_live_client import GateLiveTrader


def _client(store: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.include_router(router_for(lambda: store, lambda: None, lambda: None))
    return TestClient(app, headers={"Host": "localhost:8000", "Origin": "http://localhost:5173"})


def test_gate_adapter_constructs_the_explicit_testnet_endpoint_and_maps_schema(monkeypatch):
    captured = {}

    class Exchange:
        urls = {
            "api": {
                "public": {"futures": "https://api-testnet.gateapi.io/api/v4"},
                "private": {"futures": "https://api-testnet.gateapi.io/api/v4"},
            }
        }

        def set_sandbox_mode(self, enabled):
            assert enabled is True

        def fetch_balance(self):
            # This is the historical shape that used to cause a raw
            # ``TypeError: list indices must be integers`` in the UI.
            return {"USDT": []}

    def gate_factory(config):
        captured.update(config)
        return Exchange()

    fake_ccxt = ModuleType("ccxt")
    fake_ccxt.gate = gate_factory
    monkeypatch.setitem(sys.modules, "ccxt", fake_ccxt)

    trader = GateLiveTrader("testnet-key", "testnet-secret", testnet=True, live_trading_enabled=True)
    result = trader.validate_credentials()

    assert result["valid"] is False
    assert result["status"] == "VERIFICATION_FAILED"
    assert result["code"] == "GATE_BALANCE_SCHEMA_INVALID"
    # CCXT owns the nested Gate route map.  The adapter must select TestNet
    # through its sandbox switch instead of flattening ``urls`` and losing
    # the futures route.
    assert "urls" not in captured
    assert "testnet-secret" not in str(result)


def test_gate_testnet_remote_receipt_reconcile_cancel_and_symbol_mapping():
    class Exchange:
        def __init__(self):
            self.create_calls = []
            self.cancel_calls = []
            self.fetch_calls = []
            self.trade_calls = []
            self.cancel_all_calls = []
            self.leverage_calls = []

        def load_markets(self):
            return {
                "BTC/USDT:USDT": {
                    "symbol": "BTC/USDT:USDT",
                    "id": "BTC_USDT",
                    "base": "BTC",
                    "quote": "USDT",
                    "settle": "USDT",
                    "swap": True,
                    "linear": True,
                }
            }

        def set_leverage(self, leverage, symbol, params=None):
            self.leverage_calls.append((leverage, symbol, params))
            return {"leverage": leverage, "symbol": symbol}

        def fetch_positions(self):
            return [{"symbol": "BTC/USDT:USDT", "side": "LONG", "contracts": 1.0, "marginMode": "cross"}]

        def create_order(self, **kwargs):
            self.create_calls.append(kwargs)
            return {
                "id": "gate-testnet-order-1",
                "status": "open",
                "filled": 0.0,
                "amount": 1.0,
                "price": 100.0,
            }

        def fetch_order(self, order_id, symbol):
            self.fetch_calls.append((order_id, symbol))
            return {
                "id": order_id,
                "status": "closed",
                "filled": 1.0,
                "amount": 1.0,
                "average": 100.1,
            }

        def cancel_order(self, order_id, symbol):
            self.cancel_calls.append((order_id, symbol))
            return {"id": order_id, "status": "canceled"}

        def fetch_my_trades(self, symbol, limit):
            self.trade_calls.append((symbol, limit))
            return [{
                "id": "gate-testnet-trade-1",
                "order": "gate-testnet-order-1",
                "symbol": symbol,
                "timestamp": 1_700_000_000_000,
                "side": "buy",
                "price": 100.1,
                "amount": 1.0,
                "cost": 100.1,
                "fee": {"cost": 0.01, "currency": "USDT"},
            }]

        def cancel_all_orders(self, symbol):
            self.cancel_all_calls.append(symbol)
            return [{"id": "gate-testnet-order-1", "status": "canceled"}]

    exchange = Exchange()
    trader = GateLiveTrader("testnet-key", "testnet-secret", testnet=True, live_trading_enabled=True)
    trader._exchange = exchange

    receipt = trader.place_order(
        symbol="BTCUSDT",
        side="LONG",
        amount=1.0,
        price=100.0,
        order_type="limit",
        leverage=3,
        client_order_id="intent-remote-1",
    )
    assert receipt["status"] == "ACKNOWLEDGED"
    assert receipt["filled"] == 0.0
    assert exchange.create_calls[0]["symbol"] == "BTC/USDT:USDT"
    assert exchange.create_calls[0]["side"] == "buy"
    assert exchange.create_calls[0]["params"]["text"] == "t-intent-remote-1"
    assert exchange.leverage_calls == [(3, "BTC/USDT:USDT", {"marginMode": "cross"})]

    reconciled = trader.reconcile_order("gate-testnet-order-1", "BTCUSDT")
    assert reconciled["reconciled"] is True
    assert reconciled["status"] == "FILLED"
    assert exchange.fetch_calls == [("gate-testnet-order-1", "BTC/USDT:USDT")]

    canceled = trader.cancel_order("gate-testnet-order-1", "BTCUSDT")
    assert canceled["cancelled"] is True
    assert exchange.cancel_calls == [("gate-testnet-order-1", "BTC/USDT:USDT")]

    trades = trader.get_trades(symbol="BTC_USDT", limit=25)
    assert len(trades) == 1
    assert exchange.trade_calls == [("BTC/USDT:USDT", 25)]

    canceled_all = trader.cancel_all_orders(symbol="BTC_USDT")
    assert canceled_all["cancelled_all"] is True
    assert exchange.cancel_all_calls == ["BTC/USDT:USDT"]

    closed = trader.close_position("BTC_USDT")
    assert closed["closed"] is True
    assert exchange.create_calls[-1]["symbol"] == "BTC/USDT:USDT"
    assert exchange.create_calls[-1]["params"]["reduceOnly"] is True


def test_gate_leverage_uses_explicit_default_only_after_remote_empty_position_read():
    class Exchange:
        def __init__(self):
            self.calls = []

        def load_markets(self):
            return {
                "SOL/USDT:USDT": {
                    "symbol": "SOL/USDT:USDT", "id": "SOL_USDT", "base": "SOL", "quote": "USDT",
                    "settle": "USDT", "swap": True, "linear": True,
                }
            }

        def fetch_positions(self, _symbols):
            return []

        def set_leverage(self, leverage, symbol, params=None):
            self.calls.append((leverage, symbol, params))
            return {"leverage": leverage, "symbol": symbol}

    exchange = Exchange()
    trader = GateLiveTrader("testnet-key", "testnet-secret", testnet=True, exchange=exchange, live_trading_enabled=True)
    result = trader.set_leverage("SOLUSDT", 2)

    assert result["acknowledged"] is True
    assert result["margin_mode"] == "isolated"
    assert result["margin_mode_source"] == "NO_EXISTING_POSITION_DEFAULT"
    assert exchange.calls == [(2, "SOL/USDT:USDT", {"marginMode": "isolated"})]


def test_gate_credentials_are_verified_before_direct_compatibility_write(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "gate-verified-write.db")
    store.initialize()
    provision_default_gate_accounts(store)
    client = _client(store)
    old_key = "old-testnet-key"
    old_secret = "old-testnet-secret"
    save_gate_account_credentials(store, GATE_PAPER_ACCOUNT_ID, old_key, old_secret)

    bad_key = "bad-testnet-key"
    bad_secret = "bad-testnet-secret"

    def failed_validation(self):
        return {
            "valid": False,
            "status": "VERIFICATION_FAILED",
            "code": "GATE_SIGNATURE_INVALID",
            "reason": f"bad pair {self.api_key}/{self.api_secret}",
        }

    monkeypatch.setattr(GateLiveTrader, "validate_credentials", failed_validation)
    failed = client.post(
        f"/v2/gate/accounts/{GATE_PAPER_ACCOUNT_ID}/credentials",
        json={"api_key": bad_key, "api_secret": bad_secret},
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["saved"] is False
    assert bad_key not in failed.text
    assert bad_secret not in failed.text
    assert CredentialVault.get_account_in_memory_keys(store, GATE_PAPER_ACCOUNT_ID) == (old_key, old_secret)

    unscoped = client.post(
        "/v2/gate/config",
        json={"api_key": "global-key", "api_secret": "global-secret", "testnet": True},
    )
    assert unscoped.status_code == 422
    assert "ACCOUNT_SCOPE_REQUIRED" in unscoped.json()["detail"]


def test_gate_dashboard_is_read_only_and_uses_testnet_scope(tmp_path):
    store = SQLiteStore(tmp_path / "gate-dashboard.db")
    store.initialize()
    provision_default_gate_accounts(store)
    client = _client(store)

    with store._connect() as db:
        before = int(db.execute("PRAGMA data_version").fetchone()[0])
        before_rows = int(db.execute("SELECT COUNT(*) FROM ai_calibration_runs").fetchone()[0])
    response = client.get(f"/v2/ai-analysis/dashboard?account_id={GATE_PAPER_ACCOUNT_ID}")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["scope"]["mode"] == "TESTNET"
    assert payload["scope"]["venue"] == "gate"
    assert payload["status"] == "EMPTY"
    assert payload["data_quality"]["is_sample"] is False
    assert payload["timeline"] == []
    with store._connect() as db:
        assert int(db.execute("PRAGMA data_version").fetchone()[0]) == before
        assert int(db.execute("SELECT COUNT(*) FROM ai_calibration_runs").fetchone()[0]) == before_rows
    # The LIVE row has its own scope and remains release-locked; a TestNet
    # dashboard request cannot cross into it.
    live = client.get(f"/v2/ai-analysis/dashboard?account_id={GATE_LIVE_ACCOUNT_ID}")
    assert live.status_code == 200
    assert live.json()["scope"]["mode"] == "LIVE"
    assert live.json()["scope"]["venue"] == "gate"
    assert live.json()["status"] == "EMPTY"
