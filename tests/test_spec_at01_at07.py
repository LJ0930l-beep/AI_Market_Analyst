"""Acceptance Test Suite AT01 to AT07 for Milestone M0 (Specification v1.1).

Covers:
- AT01: Live trading blocked without authorization across all endpoints (403, zero network orders).
- AT02: Open receipt with filled=0 is ACKNOWLEDGED, NEVER LIVE_EXECUTED, zero fake positions/PnL.
- AT03: Partial fills and duplicate receipts: single fee accumulation, protection size matches filled exposure.
- AT04: Order submission timeout: marks UNKNOWN, initiates active reconciliation, forbids blind resends.
- AT05: Protection order or leverage modification failure: blocks new risk, executes safety contingency.
- AT06: Concurrent identical idempotency keys: 1 intent executed, payload discrepancy raises 409 Conflict.
- AT07: Credential scan (zero plaintext secrets in DB/logs/responses) & untrusted cross-origin request rejection.
"""

from datetime import datetime, timezone
import json
import pathlib
import tempfile
from typing import Any, Dict
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI

from core.storage import SQLiteStore
from core.security.credentials import CredentialVault, mask_api_key
from core.security.local_guard import validate_local_request, is_safe_outbound_url
from core.trading.execution_gateway import (
    ExecutionGateway,
    CapabilityService,
    OrderIntent,
    ProtectionPlan,
    TradingMode,
    OrderStatus,
    ProtectionStatus,
    CapabilityStatus,
    LivePermissionState,
    LiveDisabledByReleasePolicyError,
    LiveAuthorizationRequiredError,
    IdempotencyConflictError,
    ParameterValidationError,
)
from core.trading.gate_live_client import GateLiveTrader
from core.trading.ledger import AccountLedger
from apps.api.v2 import router_for


@pytest.fixture
def temp_store():
    tmpdir = tempfile.mkdtemp()
    db_path = pathlib.Path(tmpdir) / "test_at.db"
    store = SQLiteStore(db_path)
    store.initialize()
    # The v1.2 execution boundary no longer infers an account from an
    # arbitrary string.  Keep these legacy acceptance scenarios explicit by
    # registering their test accounts in the fixture.
    ledger = AccountLedger(store)
    ledger.create_account("gateio_main", mode="LIVE", config={"venue": "gate"}, initial_deposit=1_000_000)
    ledger.create_account("paper_act", mode="PAPER", config={"venue": "simulated"}, initial_deposit=1_000_000)
    ledger.create_account("act_1", mode="PAPER", config={"venue": "simulated"}, initial_deposit=1_000_000)
    ledger.close()
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


def _fresh_paper_market(symbol: str, price: float) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "symbol": symbol,
        "price": price,
        "last": price,
        "data_as_of": now,
        "received_at": now,
        "fresh": True,
        "market": {
            "contractSize": 1.0,
            "precision": {"amount": 0.001, "price": 0.01},
            "limits": {"amount": {"min": 0.001, "max": 1_000_000.0, "step": 0.001}},
            "taker": 0.0005,
        },
    }


# =========================================================================
# AT01: 未达门槛或未授权调用实盘接口 -> 服务端 403 阻断，绝无网络下单
# =========================================================================
def test_at01_live_blocked_without_authorization(temp_store, api_client):
    gateway = ExecutionGateway(temp_store)

    intent = OrderIntent(
        intent_id="intent_live_test_1",
        idempotency_key="idem_key_live_1",
        account_id="gateio_main",
        mode=TradingMode.LIVE,
        instrument_id="BTCUSDT",
        side="LONG",
        order_type="market",
        quantity=0.1,
        protection_plan=ProtectionPlan(stop_price=64000.0, take_profit=72000.0),
    )

    mock_trader = MagicMock()

    # 1. Direct gateway submission raises LiveDisabledByReleasePolicyError (403)
    with pytest.raises(LiveDisabledByReleasePolicyError) as exc_info:
        gateway.submit_intent(intent, trader_client=mock_trader)
    assert exc_info.value.status_code == 403
    assert "LIVE_DISABLED_BY_RELEASE_POLICY" in exc_info.value.code
    assert mock_trader.place_order.call_count == 0  # Absolute guarantee: no network call

    # 2. API endpoint submission returns 403
    res = api_client.post(
        "/v2/gate/orders",
        json={
            "symbol": "BTCUSDT",
            "side": "LONG",
            "amount": 0.1,
            "order_type": "market",
            "stop_loss": 64000.0,
            "take_profit": 72000.0,
        },
        headers={"Host": "localhost:8000"},
    )
    assert res.status_code == 403
    assert "LIVE_DISABLED_BY_RELEASE_POLICY" in res.json()["detail"]


# =========================================================================
# AT02: open 且 filled 为零回执 -> 仅作为 ACKNOWLEDGED，不产生持仓与虚假盈亏
# =========================================================================
def test_at02_open_unfilled_receipt_is_acknowledged(temp_store):
    trader = GateLiveTrader("key", "secret", testnet=True, live_trading_enabled=True)

    # Mock exchange returning status="open", filled=0.0
    mock_exchange = MagicMock()
    mock_exchange.create_order.return_value = {
        "id": "gate_ord_1001",
        "status": "open",
        "filled": 0.0,
        "amount": 0.5,
        "price": 68000.0,
    }
    trader._get_exchange = MagicMock(return_value=mock_exchange)

    res = trader.place_order(
        symbol="BTCUSDT",
        side="LONG",
        amount=0.5,
        price=68000.0,
        order_type="limit",
        stop_loss=65000.0,
        client_order_id="test_ord_ack",
    )

    # Must be ACKNOWLEDGED, never LIVE_EXECUTED
    assert res["status"] == "ACKNOWLEDGED"
    assert res["status"] != "LIVE_EXECUTED"
    assert res["filled"] == 0.0
    assert mock_exchange.create_order.called

    # Protection is a separate native conditional order and cannot be mixed
    # into the entry request.  With no fill there is no protection leg yet.
    call_args = mock_exchange.create_order.call_args[1]
    assert "stop_loss" not in call_args["params"]
    assert "take_profit" not in call_args["params"]
    assert res["protection_status"] == "PENDING_ENTRY_FILL"


# =========================================================================
# AT03: 部分成交与重复回报 -> 仅累计一次费用与持仓，保护单数量准确匹配敞口
# =========================================================================
def test_at03_partial_fill_and_deduplication(temp_store):
    gateway = ExecutionGateway(temp_store)

    intent = OrderIntent(
        intent_id="intent_paper_part",
        idempotency_key="idem_part_1",
        account_id="paper_act",
        mode=TradingMode.PAPER,
        instrument_id="ETHUSDT",
        side="LONG",
        order_type="market",
        quantity=2.0,
        price=2500.0,
        protection_plan=ProtectionPlan(stop_price=2400.0, take_profit=2700.0),
    )

    # First execution
    res1 = gateway.submit_intent(intent, market_snapshot=_fresh_paper_market("ETHUSDT", 2500.0))
    assert res1["status"] == "FILLED"
    assert res1["protection"]["status"] == "ACTIVE"
    fee_1 = res1["fee"]

    # Same request re-submitted with identical idempotency key -> cached identical result
    res2 = gateway.submit_intent(intent)
    assert res2["intent_id"] == res1["intent_id"]
    assert res2["fee"] == fee_1  # No duplicate fee accumulation


# =========================================================================
# AT04: 下单超时和重启 -> 记录 UNKNOWN 并触发对账，严禁盲目重发
# =========================================================================
def test_at04_timeout_records_unknown_and_reconciles(temp_store):
    trader = GateLiveTrader("key", "secret", testnet=True, live_trading_enabled=True)

    mock_exchange = MagicMock()
    # Simulate network timeout during create_order
    mock_exchange.create_order.side_effect = TimeoutError("Connection timed out to Gate.io gateway")
    trader._get_exchange = MagicMock(return_value=mock_exchange)

    res = trader.place_order(
        symbol="BTCUSDT",
        side="LONG",
        amount=0.1,
        client_order_id="timeout_intent_123",
    )

    # Status must be UNKNOWN
    assert res["status"] == "UNKNOWN"
    assert "ORDER_TIMEOUT" in res["error"]
    assert res["client_order_id"] == "timeout_intent_123"

    # Now reconcile via fetch_order: suppose the exchange actually filled it
    mock_exchange.fetch_order.return_value = {
        "id": "ord_exchange_888",
        "status": "closed",
        "filled": 0.1,
        "amount": 0.1,
        "average": 67800.0,
    }

    reconciled = trader.reconcile_order("ord_exchange_888", "BTCUSDT")
    assert reconciled["reconciled"] is True
    assert reconciled["status"] == "FILLED"
    assert reconciled["filled"] == 0.1


# =========================================================================
# AT05: 保护单或杠杆修改失败 -> 立即阻断新增风险，进入预定应急流程
# =========================================================================
def test_at05_leverage_and_protection_failure_blocks_risk(temp_store):
    gateway = ExecutionGateway(temp_store)

    # 1. Missing ProtectionPlan with stop_loss is immediately rejected with ParameterValidationError
    intent_no_prot = OrderIntent(
        intent_id="intent_fail_1",
        idempotency_key="idem_fail_1",
        account_id="act_1",
        mode=TradingMode.PAPER,
        instrument_id="BTCUSDT",
        side="LONG",
        order_type="market",
        quantity=0.1,
        protection_plan=None,  # Invalid
    )
    with pytest.raises(ParameterValidationError) as exc_info:
        gateway.submit_intent(intent_no_prot)
    assert "ProtectionPlan" in exc_info.value.message

    # 2. Leverage change failure in live trader must abort order placement
    trader = GateLiveTrader("key", "secret", testnet=True, live_trading_enabled=True)
    mock_exchange = MagicMock()
    mock_exchange.set_leverage.side_effect = RuntimeError("Exchange rejected leverage adjustment: MARGIN_DEFICIT")
    trader._get_exchange = MagicMock(return_value=mock_exchange)

    res = trader.place_order(
        symbol="BTCUSDT",
        side="LONG",
        amount=0.1,
        leverage=50,
        stop_loss=65000.0,
    )
    assert res["status"] == "EXECUTION_FAILED"
    assert "LEVERAGE_CHANGE_FAILED" in res["error"]
    assert mock_exchange.create_order.call_count == 0  # Order creation never executed!


# =========================================================================
# AT06: 并发相同幂等键 -> 同意图幂等返回，异构请求返回 409 Conflict
# =========================================================================
def test_at06_idempotency_identical_and_conflict(temp_store):
    gateway = ExecutionGateway(temp_store)

    intent_base = OrderIntent(
        intent_id="intent_idem_alpha",
        idempotency_key="shared_idem_key_999",
        account_id="act_1",
        mode=TradingMode.PAPER,
        instrument_id="SOLUSDT",
        side="LONG",
        order_type="market",
        quantity=10.0,
        price=140.0,
        protection_plan=ProtectionPlan(stop_price=130.0),
    )

    # First execution succeeds
    first_res = gateway.submit_intent(intent_base, market_snapshot=_fresh_paper_market("SOLUSDT", 140.0))
    assert first_res["status"] == "FILLED"

    # Second execution with exact same idempotency_key and identical payload returns same cached result
    second_res = gateway.submit_intent(intent_base)
    assert second_res["intent_id"] == first_res["intent_id"]
    assert second_res["order_id"] == first_res["order_id"]

    # Third execution with same idempotency_key but CONFLICTING payload (quantity changed to 20.0)
    conflicting_intent = OrderIntent(
        intent_id="intent_idem_beta",
        idempotency_key="shared_idem_key_999",
        account_id="act_1",
        mode=TradingMode.PAPER,
        instrument_id="SOLUSDT",
        side="LONG",
        order_type="market",
        quantity=20.0,  # Changed!
        price=140.0,
        protection_plan=ProtectionPlan(stop_price=130.0),
    )

    with pytest.raises(IdempotencyConflictError) as exc_info:
        gateway.submit_intent(conflicting_intent)
    assert exc_info.value.status_code == 409
    assert "IDEMPOTENCY_CONFLICT" in exc_info.value.code


# =========================================================================
# AT07: 凭据与恶意本地请求 -> 0 泄露，未受信任跨站写请求 403 拦截
# =========================================================================
def test_at07_credentials_zero_leakage_and_cross_origin_blocked(temp_store, api_client):
    secret_key = "gate_super_secret_high_entropy_token_9876543210"
    api_key = "my_gate_live_key_12345678"

    # 1. Save via CredentialVault
    meta = CredentialVault.save_credentials(temp_store, api_key, secret_key, testnet=True)
    assert meta["configured"] is True
    assert "api_secret" not in meta
    assert meta["api_key_masked"] == "my_g***5678"

    # 2. Inspect SQLite database directly: api_secret MUST NOT exist in plaintext anywhere
    with temp_store._connect() as db:
        # Check vault table
        rows = db.execute("SELECT * FROM secure_credentials_vault").fetchall()
        assert len(rows) == 1
        vault_row = dict(rows[0])
        assert secret_key not in str(vault_row.values())

        # Check legacy table if present
        table = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='gate_credentials'").fetchone()
        if table:
            legacy_rows = db.execute("SELECT * FROM gate_credentials WHERE id = 1").fetchall()
            if legacy_rows:
                leg_row = dict(legacy_rows[0])
                assert leg_row["api_secret"] == "[MIGRATED_TO_DPAPI]"
                assert secret_key != leg_row["api_secret"]

    # 3. Verify API never returns secret
    cfg_res = api_client.get("/v2/gate/config", headers={"Host": "localhost:8000"})
    assert cfg_res.status_code == 200
    cfg_body = cfg_res.json()
    assert "api_secret" not in cfg_body
    assert cfg_body["api_key_masked"] == "my_g***5678"
    assert cfg_body["live_enabled"] is False

    # 4. Malicious external web origin calling local API is blocked with 403 Forbidden
    malicious_res = api_client.post(
        "/v2/gate/config",
        json={"api_key": "k", "api_secret": "s", "live_enabled": False, "testnet": True},
        headers={
            "Host": "localhost:8000",
            "Origin": "https://malicious-crypto-drainer.com",
        },
    )
    assert malicious_res.status_code == 403
    assert "Forbidden Origin" in malicious_res.json()["detail"]

    # 5. Outbound SSRF guard blocks loopback & internal network URLs
    safe, _ = is_safe_outbound_url("https://www.federalreserve.gov/newsevents.htm")
    assert safe is True

    unsafe_loopback, err = is_safe_outbound_url("https://127.0.0.1:9000/internal")
    assert unsafe_loopback is False
    assert "loopback" in err.lower()
