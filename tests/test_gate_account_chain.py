"""Regression and integration checks for the scoped Gate account chain."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from apps.api.v2 import router_for as v2_router_for
from apps.api.v3 import router_for as v3_router_for
from core.security.credentials import CredentialVault
from core.storage import SQLiteStore
from core.trading.execution_gateway import (
    ExecutionGateway,
    LiveDisabledByReleasePolicyError,
    OrderIntent,
    ProtectionPlan,
    TradingMode,
)
from core.trading.gate_accounts import (
    GATE_LIVE_ACCOUNT_ID,
    GATE_PAPER_API_BASE_URL,
    GATE_PAPER_ACCOUNT_ID,
    get_gate_account_profile,
    provision_default_gate_accounts,
    save_gate_account_credentials,
)
from core.trading.ledger import AccountLedger
from core.trading.trader_capabilities import TraderCapabilityError, TraderCapabilityService


def _v2_client(store: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.include_router(v2_router_for(lambda: store, lambda: None, lambda: None))
    return TestClient(app, headers={"Host": "localhost:8000", "Origin": "http://localhost:5173"})


def _v3_client(store: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.include_router(v3_router_for(lambda: store))
    return TestClient(app)


def _fresh_market(now: datetime) -> dict:
    return {
        "symbol": "BTCUSDT",
        "price": 100.0,
        "last": 100.0,
        "data_as_of": (now - timedelta(seconds=5)).isoformat(),
        "received_at": (now - timedelta(seconds=5)).isoformat(),
        "fresh": True,
        "market": {
            "contractSize": 1.0,
            "precision": {"amount": 0.001, "price": 0.01},
            "limits": {"amount": {"min": 0.001, "max": 1_000_000.0, "step": 0.001}},
            "taker": 0.0005,
        },
    }


def test_gate_default_accounts_are_distinct_and_api_provision_is_idempotent(tmp_path):
    store = SQLiteStore(tmp_path / "gate-accounts.db")
    store.initialize()
    client = _v2_client(store)

    first = client.post("/v2/gate/accounts/provision-defaults", json={})
    second = client.post("/v2/gate/accounts/provision-defaults", json={})
    assert first.status_code == second.status_code == 200, first.text

    listed = client.get("/v2/gate/accounts")
    assert listed.status_code == 200, listed.text
    accounts = {item["account_id"]: item for item in listed.json()["accounts"]}
    assert set(accounts) == {GATE_PAPER_ACCOUNT_ID, GATE_LIVE_ACCOUNT_ID}
    assert accounts[GATE_PAPER_ACCOUNT_ID]["mode"] == "PAPER"
    assert accounts[GATE_LIVE_ACCOUNT_ID]["mode"] == "LIVE"
    assert accounts[GATE_PAPER_ACCOUNT_ID]["venue"] == accounts[GATE_LIVE_ACCOUNT_ID]["venue"] == "gate"
    assert accounts[GATE_PAPER_ACCOUNT_ID]["api_environment"] == "TESTNET"
    assert accounts[GATE_LIVE_ACCOUNT_ID]["api_environment"] == "LIVE"
    assert accounts[GATE_PAPER_ACCOUNT_ID]["api_base_url"] != accounts[GATE_LIVE_ACCOUNT_ID]["api_base_url"]
    assert accounts[GATE_LIVE_ACCOUNT_ID]["live_status"] == "LOCKED"
    assert accounts[GATE_PAPER_ACCOUNT_ID]["private_api_access"] == "NOT_ATTEMPTED"
    scoped_config = client.get(f"/v2/gate/config?account_id={GATE_LIVE_ACCOUNT_ID}")
    assert scoped_config.status_code == 200
    assert scoped_config.json()["account_id"] == GATE_LIVE_ACCOUNT_ID
    assert scoped_config.json()["private_api_access"] == "NOT_ATTEMPTED"
    account_list = client.get("/v2/accounts")
    assert account_list.status_code == 200
    listed_modes = {item["account_id"]: item["mode"] for item in account_list.json()["accounts"]}
    # The database/display row keeps PAPER for compatibility, while the
    # account-scope projection is authoritative Gate TestNet.
    assert listed_modes[GATE_PAPER_ACCOUNT_ID] == "TESTNET"
    assert listed_modes[GATE_LIVE_ACCOUNT_ID] == "LIVE"

    with store._connect() as db:
        rows = db.execute(
            "SELECT account_id, mode, initial_deposit, config_json FROM accounts WHERE account_id IN (?, ?)",
            (GATE_PAPER_ACCOUNT_ID, GATE_LIVE_ACCOUNT_ID),
        ).fetchall()
    assert {row["account_id"] for row in rows} == {GATE_PAPER_ACCOUNT_ID, GATE_LIVE_ACCOUNT_ID}
    assert {row["mode"] for row in rows} == {"PAPER", "LIVE"}
    # Repeating provisioning does not reset the local ledger capital or add a
    # second initial-deposit event.
    with store._connect() as db:
        assert int(
            db.execute(
                "SELECT COUNT(*) FROM ledger_events WHERE event_type='INITIAL_DEPOSIT' AND account_id=?",
                (GATE_PAPER_ACCOUNT_ID,),
            ).fetchone()[0]
        ) == 1


def test_gate_credentials_are_encrypted_and_scoped_per_account(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "gate-credentials.db")
    store.initialize()
    provision_default_gate_accounts(store)
    client = _v2_client(store)

    # Public credential writes now perform the explicit read-only verification
    # first.  Keep this deterministic and offline while still exercising the
    # real account-scoped API route and encrypted persistence.
    def fake_validate(self):
        return {
            "valid": True,
            "status": "VERIFIED_READ_ONLY",
            "data_status": "AVAILABLE",
            "total_usdt": 100.0,
            "free_usdt": 100.0,
            "used_usdt": 0.0,
        }

    monkeypatch.setattr("core.trading.gate_live_client.GateLiveTrader.validate_credentials", fake_validate)

    live_key = "live_key_for_isolated_test_1234"
    live_secret = "live_secret_for_isolated_test_5678"
    paper_key = "paper_key_for_isolated_test_9012"
    paper_secret = "paper_secret_for_isolated_test_3456"
    live = client.post(
        f"/v2/gate/accounts/{GATE_LIVE_ACCOUNT_ID}/credentials",
        json={"api_key": live_key, "api_secret": live_secret},
    )
    paper = client.post(
        f"/v2/gate/accounts/{GATE_PAPER_ACCOUNT_ID}/credentials",
        json={"api_key": paper_key, "api_secret": paper_secret},
    )
    assert live.status_code == paper.status_code == 200
    live_payload = json.dumps(live.json(), ensure_ascii=False)
    assert live_secret not in live_payload
    assert paper_secret not in json.dumps(paper.json(), ensure_ascii=False)
    assert live.json()["account"]["credentials"]["api_key_masked"] != paper.json()["account"]["credentials"]["api_key_masked"]
    scoped = client.get(f"/v2/gate/config?account_id={GATE_LIVE_ACCOUNT_ID}")
    assert scoped.status_code == 200
    assert scoped.json()["configured"] is True
    assert scoped.json()["api_key_masked"] == live.json()["account"]["credentials"]["api_key_masked"]

    live_keys = CredentialVault.get_account_in_memory_keys(store, GATE_LIVE_ACCOUNT_ID)
    paper_keys = CredentialVault.get_account_in_memory_keys(store, GATE_PAPER_ACCOUNT_ID)
    assert live_keys == (live_key, live_secret)
    assert paper_keys == (paper_key, paper_secret)
    assert CredentialVault.get_metadata(store)["configured"] is False

    with store._connect() as db:
        rows = db.execute(
            "SELECT account_id, encrypted_key_blob, encrypted_secret_blob FROM secure_account_credentials ORDER BY account_id"
        ).fetchall()
        configs = db.execute(
            "SELECT account_id, config_json FROM accounts WHERE account_id IN (?, ?)",
            (GATE_LIVE_ACCOUNT_ID, GATE_PAPER_ACCOUNT_ID),
        ).fetchall()
    assert {row["account_id"] for row in rows} == {GATE_LIVE_ACCOUNT_ID, GATE_PAPER_ACCOUNT_ID}
    for row in rows:
        assert live_secret.encode() not in bytes(row["encrypted_secret_blob"])
        assert paper_secret.encode() not in bytes(row["encrypted_secret_blob"])
    for row in configs:
        assert live_secret not in row["config_json"]
        assert paper_secret not in row["config_json"]


def test_gate_credential_verify_is_read_only_before_scoped_persist(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "gate-credential-verify.db")
    store.initialize()
    provision_default_gate_accounts(store)
    client = _v2_client(store)
    calls = []

    def fake_validate(self):
        calls.append({
            "api_key": self.api_key,
            "api_secret": self.api_secret,
            "testnet": self.testnet,
            "api_base_url": self.api_base_url,
            "live_trading_enabled": self.live_trading_enabled,
        })
        return {
            "valid": True,
            "account_type": "Gate.io Futures / Swap",
            "data_status": "AVAILABLE",
            "total_usdt": 123.45,
            "free_usdt": 100.00,
            "used_usdt": 23.45,
        }

    monkeypatch.setattr("core.trading.gate_live_client.GateLiveTrader.validate_credentials", fake_validate)
    key = "paper-verify-key-1234"
    secret = "paper-verify-secret-5678"
    response = client.post(
        f"/v2/gate/accounts/{GATE_PAPER_ACCOUNT_ID}/credentials/verify",
        json={"api_key": key, "api_secret": secret},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["saved"] is True
    assert payload["private_api_access"] == "EXPLICITLY_REQUESTED"
    assert payload["validation"]["status"] == "VERIFIED_READ_ONLY"
    assert payload["validation"]["account_id"] == GATE_PAPER_ACCOUNT_ID
    assert payload["validation"]["api_environment"] == "TESTNET"
    assert key not in json.dumps(payload)
    assert secret not in json.dumps(payload)
    assert calls == [{
        "api_key": key,
        "api_secret": secret,
        "testnet": True,
        "api_base_url": GATE_PAPER_API_BASE_URL,
        "live_trading_enabled": False,
    }]
    assert CredentialVault.get_account_in_memory_keys(store, GATE_PAPER_ACCOUNT_ID) == (key, secret)
    assert CredentialVault.get_account_metadata(store, GATE_LIVE_ACCOUNT_ID)["configured"] is False


def test_gate_credential_verify_failure_does_not_replace_existing_slot_or_leak_secret(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "gate-credential-verify-failure.db")
    store.initialize()
    provision_default_gate_accounts(store)
    client = _v2_client(store)
    existing_key = "existing-live-key-1234"
    existing_secret = "existing-live-secret-5678"
    save_gate_account_credentials(store, GATE_LIVE_ACCOUNT_ID, existing_key, existing_secret)
    bad_key = "bad-live-key-1111"
    bad_secret = "bad-live-secret-2222"

    def fake_validate(self):
        return {"valid": False, "reason": f"invalid credential {self.api_key}/{self.api_secret}"}

    monkeypatch.setattr("core.trading.gate_live_client.GateLiveTrader.validate_credentials", fake_validate)
    response = client.post(
        f"/v2/gate/accounts/{GATE_LIVE_ACCOUNT_ID}/credentials/verify",
        json={"api_key": bad_key, "api_secret": bad_secret},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["saved"] is False
    assert payload["validation"]["valid"] is False
    assert payload["validation"]["status"] == "INVALID_OR_UNAVAILABLE"
    assert bad_key not in json.dumps(payload)
    assert bad_secret not in json.dumps(payload)
    assert CredentialVault.get_account_in_memory_keys(store, GATE_LIVE_ACCOUNT_ID) == (existing_key, existing_secret)


def test_gate_testnet_account_does_not_read_local_ledger_or_route_paper_order(tmp_path):
    store = SQLiteStore(tmp_path / "gate-paper-chain.db")
    store.initialize()
    provision_default_gate_accounts(store)
    ledger = AccountLedger(store)
    # The old route wrote a local PAPER fill under a Gate venue.  That is an
    # unsafe cross-environment write now: gate_paper is the official remote
    # TestNet account and local simulator fills must be impossible.
    with pytest.raises(ValueError, match="ACCOUNT_MODE_MISMATCH"):
        ledger.record_trade_fill(
            GATE_PAPER_ACCOUNT_ID,
            "BTCUSDT",
            "BUY",
            Decimal("2"),
            Decimal("100"),
            Decimal("0.20"),
            mode="PAPER",
            venue="gate",
            order_id="paper-order-1",
            trade_id="paper-trade-1",
            position_id="paper-position-1",
            protection_status="ACTIVE",
        )

    client = _v2_client(store)
    account = client.get(f"/v2/gate/account?account_id={GATE_PAPER_ACCOUNT_ID}")
    assert account.status_code == 200, account.text
    assert account.json()["mode"] == "TESTNET"
    assert account.json()["account_type"] == "GATE_TESTNET"
    assert account.json()["api_environment"] == "TESTNET"
    assert account.json()["data_status"] == "NOT_CONFIGURED_NO_TESTNET_CREDENTIALS"
    assert account.json()["balance"]["total"] is None
    assert account.json()["private_api_access"] == "NOT_ATTEMPTED_NO_CREDENTIALS"

    trades = client.get(f"/v2/gate/trades?account_id={GATE_PAPER_ACCOUNT_ID}")
    assert trades.status_code == 200, trades.text
    assert trades.json()["summary"]["source"] == "NOT_CONFIGURED_NO_TESTNET_CREDENTIALS"
    assert trades.json()["summary"]["fee_status"] == "NOT_CONFIGURED"
    assert trades.json()["trades"] == []
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM trade_fills WHERE account_id=?",
            (GATE_PAPER_ACCOUNT_ID,),
        ).fetchone()[0] == 0


def test_gate_live_account_stays_release_locked_without_adapter_call(tmp_path):
    store = SQLiteStore(tmp_path / "gate-live-lock.db")
    store.initialize()
    provision_default_gate_accounts(store)
    client = _v2_client(store)

    account = client.get(f"/v2/gate/account?account_id={GATE_LIVE_ACCOUNT_ID}")
    assert account.status_code == 200
    assert account.json()["mode"] == "LIVE"
    assert account.json()["data_status"] == "NOT_RUN_LIVE_LOCKED"
    assert account.json()["private_api_access"] == "NOT_ATTEMPTED"

    locked_http = client.post(
        "/v2/gate/orders",
        json={
            "account_id": GATE_LIVE_ACCOUNT_ID,
            "venue": "gate",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "amount": 1,
            "price": 100,
            "stop_loss": 90,
            "dry_run": True,
        },
    )
    assert locked_http.status_code == 403
    assert "LIVE_DISABLED_BY_RELEASE_POLICY" in locked_http.json()["detail"]

    with pytest.raises(LiveDisabledByReleasePolicyError):
        ExecutionGateway(store).submit_intent(
            OrderIntent(
                intent_id="live-gate-locked",
                idempotency_key="live-gate-locked-idem",
                account_id=GATE_LIVE_ACCOUNT_ID,
                mode=TradingMode.LIVE,
                venue="gate",
                environment="LIVE",
                instrument_id="BTCUSDT",
                side="LONG",
                order_type="market",
                quantity=Decimal("1"),
                price=100.0,
                protection_plan=ProtectionPlan(stop_price=90.0),
            ),
            market_snapshot=_fresh_market(datetime.now(timezone.utc)),
        )


def test_gate_testnet_trade_plan_stays_remote_and_preserves_scope(tmp_path):
    store = SQLiteStore(tmp_path / "gate-paper-plan-chain.db")
    store.initialize()
    provision_default_gate_accounts(store)
    ledger = AccountLedger(store)
    now = datetime.now(timezone.utc)
    ledger.record_trade_fill(
        GATE_PAPER_ACCOUNT_ID,
        "BTCUSDT",
        "LONG",
        Decimal("1"),
        Decimal("100"),
        Decimal("0"),
        mode="TESTNET",
        venue="gate",
        order_id="gate-paper-plan-seed-order",
        trade_id="gate-paper-plan-seed-fill",
        position_id="gate-paper-plan-position",
        protection_status="ACTIVE",
        event_at=now,
    )

    service = TraderCapabilityService(store, gateway=ExecutionGateway(store))
    plan = service.create_trade_plan(
        {
            "account_id": GATE_PAPER_ACCOUNT_ID,
            "mode": "PAPER",
            "venue": "gate",
            "symbol": "BTCUSDT",
            "action": "CLOSE_POSITION",
            "position_id": "gate-paper-plan-position",
            "evidence": [{"type": "gate_testnet_fill", "fill_id": "gate-paper-plan-seed-fill"}],
            "entry_trigger": "explicit close requested",
            "abandon_chase_condition": "close-only recovery",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "why_not_waiting": "the scoped position is being closed",
        }
    )
    assert plan["mode"] == "TESTNET"
    assert plan["venue"] == "gate"
    result = service.execute_trade_plan(
        GATE_PAPER_ACCOUNT_ID,
        plan["plan_id"],
        market_snapshot=_fresh_market(datetime.now(timezone.utc)),
    )
    assert result["status"] == "NOT_RUN"
    assert result["reason"] == "EXTERNAL_EXECUTION_REQUIRES_SEPARATE_AUTHORIZATION_AND_ADAPTER"
    assert result["mode"] == "TESTNET"
    assert result["venue"] == "gate"
    assert AccountLedger(store).get_open_positions(
        GATE_PAPER_ACCOUNT_ID, venue="gate", mode="TESTNET"
    )
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM trade_fills WHERE account_id=? AND mode=? AND venue=?",
            (GATE_PAPER_ACCOUNT_ID, "TESTNET", "gate"),
        ).fetchone()[0] == 1

    # The persisted plan is not addressable through the other Gate account.
    with pytest.raises(TraderCapabilityError) as error:
        service.execute_trade_plan(GATE_LIVE_ACCOUNT_ID, plan["plan_id"])
    assert error.value.code == "PLAN_NOT_FOUND"


def test_gate_live_trade_plan_reports_release_lock_without_creating_an_order(tmp_path):
    store = SQLiteStore(tmp_path / "gate-live-plan-lock.db")
    store.initialize()
    provision_default_gate_accounts(store)
    service = TraderCapabilityService(store, gateway=ExecutionGateway(store))
    plan = service.create_trade_plan(
        {
            "account_id": GATE_LIVE_ACCOUNT_ID,
            "mode": "LIVE",
            "venue": "gate",
            "symbol": "BTCUSDT",
            "action": "OPEN_LONG",
            "evidence": [{"type": "local_fixture", "id": "live-plan-lock"}],
            "entry_trigger": "fresh quote is available",
            "abandon_chase_condition": "do not chase after the first impulse",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "worst_loss_budget": 1.0,
            "why_not_waiting": "awaiting explicit release policy",
        }
    )
    result = service.execute_trade_plan(
        GATE_LIVE_ACCOUNT_ID,
        plan["plan_id"],
        market_snapshot=_fresh_market(datetime.now(timezone.utc)),
    )
    assert result["status"] == "NOT_RUN"
    assert result["reason"] == "LIVE_DISABLED_BY_RELEASE_POLICY"
    assert result["order_created"] is False
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0] == 0
        assert db.execute(
            "SELECT status FROM trader_trade_plans WHERE plan_id=?", (plan["plan_id"],)
        ).fetchone()[0] == "NOT_RUN_EXTERNAL"


def test_gate_remote_adapter_resolution_is_account_scoped_and_never_falls_back(tmp_path):
    store = SQLiteStore(tmp_path / "gate-adapter-scope.db")
    store.initialize()
    ledger = AccountLedger(store)
    ledger.create_account(
        "gate-testnet-a",
        mode="TESTNET",
        initial_deposit=Decimal("10000"),
        config={"venue": "gate", "gate_account_profile": "gate-account-v1"},
    )
    ledger.create_account(
        "gate-testnet-b",
        mode="TESTNET",
        initial_deposit=Decimal("10000"),
        config={"venue": "gate", "gate_account_profile": "gate-account-v1"},
    )
    save_gate_account_credentials(store, "gate-testnet-a", "account_a_key", "account_a_secret")

    stale_global_client = object()
    gateway = ExecutionGateway(store, trader_client=stale_global_client)
    account_a_intent = OrderIntent(
        intent_id="gate-testnet-a-intent",
        idempotency_key="gate-testnet-a-idem",
        account_id="gate-testnet-a",
        mode=TradingMode.TESTNET,
        venue="gate",
        environment="TESTNET",
        instrument_id="BTCUSDT",
        side="LONG",
        order_type="market",
        quantity=Decimal("1"),
        protection_plan=ProtectionPlan(stop_price=90.0),
    )
    account_a_client = gateway._resolve_scoped_trader_client(account_a_intent, None)
    assert account_a_client is not stale_global_client
    assert account_a_client.api_key == "account_a_key"
    assert account_a_client.api_secret == "account_a_secret"
    assert account_a_client.api_base_url == GATE_PAPER_API_BASE_URL

    account_b_intent = OrderIntent(
        intent_id="gate-testnet-b-intent",
        idempotency_key="gate-testnet-b-idem",
        account_id="gate-testnet-b",
        mode=TradingMode.TESTNET,
        venue="gate",
        environment="TESTNET",
        instrument_id="BTCUSDT",
        side="LONG",
        order_type="market",
        quantity=Decimal("1"),
        protection_plan=ProtectionPlan(stop_price=90.0),
    )
    assert gateway._resolve_scoped_trader_client(account_b_intent, None) is None


def test_research_cancel_uses_authoritative_scope_and_does_not_500(tmp_path):
    store = SQLiteStore(tmp_path / "research-cancel.db")
    store.initialize()
    AccountLedger(store).create_account(
        "research-paper",
        mode="PAPER",
        initial_deposit=Decimal("10000"),
        config={"venue": "simulated"},
    )
    client = _v3_client(store)
    body = {
        "account_id": "research-paper",
        "strategy_id": "ema_trend",
        "symbol": "BTCUSDT",
        "mode": "PAPER",
        "venue": "simulated",
        "timeframe": "15m",
        "min_samples": 1,
        "idempotency_key": "cancel-regression",
    }
    created = client.post("/v3/research/runs", json=body)
    assert created.status_code == 200, created.text
    run_id = created.json()["run"]["run_id"]
    cancelled = client.post(f"/v3/research/runs/{run_id}/cancel?account_id=research-paper")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["run_id"] == run_id
    assert cancelled.json()["status"] == "CANCEL_REQUESTED"


def test_scale_in_existing_position_preserves_active_protection_without_name_error(tmp_path):
    store = SQLiteStore(tmp_path / "scale-in.db")
    store.initialize()
    ledger = AccountLedger(store)
    ledger.create_account(
        "scale-in-account",
        mode="PAPER",
        initial_deposit=Decimal("10000"),
        config={"venue": "simulated"},
    )
    first = ledger.record_trade_fill(
        "scale-in-account",
        "BTCUSDT",
        "LONG",
        Decimal("1"),
        Decimal("100"),
        Decimal("0"),
        mode="PAPER",
        venue="simulated",
        order_id="scale-order-1",
        trade_id="scale-trade-1",
        position_id="scale-position",
        protection_status="ACTIVE",
    )
    second = ledger.record_trade_fill(
        "scale-in-account",
        "BTCUSDT",
        "LONG",
        Decimal("2"),
        Decimal("110"),
        Decimal("0"),
        mode="PAPER",
        venue="simulated",
        order_id="scale-order-2",
        trade_id="scale-trade-2",
        position_id="scale-position",
        protection_status="PENDING",
    )
    assert first["status"] == second["status"] == "RECORDED"
    position = ledger.get_open_positions("scale-in-account", venue="simulated", mode="PAPER")[0]
    assert position["remaining_contracts"] == pytest.approx(3.0)
    assert position["protection_status"] == "ACTIVE"
    assert position["protected"] is True
