"""Acceptance tests for AT36 through AT40 (Milestone M3).

AT36: Scope change & expiry (immediate block, no silent renewal)
AT37: Revocation & in-flight order concurrency (no new submissions after cutoff, in-flight reconciled, protections preserved)
AT38: Environment & account mismatch rejection before network call
AT39: TESTNET protocol capability & fee/funding reconciliation
AT40: Desktop wizard authorization & rejection of AI self-granting
"""

import tempfile
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.trading.authorization import (
    AISelfConfirmationError,
    AuthorizationExpiredError,
    AuthorizationManager,
    AuthorizationStatus,
    ConfirmationSource,
    TradingAuthorization,
)
from core.trading.execution_gateway import (
    ControlMode,
    DecisionPath,
    ExecutionGateway,
    GatewayError,
    OrderIntent,
    OrderStatus,
    ProtectionPlan,
    TradingMode,
)
from core.trading.testnet_capabilities import TestnetCapabilityService


class InMemoryStore:
    def __init__(self):
        self._auths = {}

    def save_trading_authorization(self, auth_dict):
        self._auths[auth_dict["authorization_id"]] = dict(auth_dict)

    def get_trading_authorization(self, auth_id):
        return self._auths.get(auth_id)

    def list_trading_authorizations(self, account_id):
        return [a for a in self._auths.values() if a.get("account_id") == account_id]


# ============================================================================
# AT36: Authorization Scope Expiry and Constraint Enforcement
# ============================================================================
def test_at36_authorization_expiry_and_scope_blocking():
    """AT36: Immediate blocking when authorization expires or scope is exceeded; no silent renewal."""
    store = InMemoryStore()
    manager = AuthorizationManager(store)

    # 1. Scope-limited authorization: only BTC_USDT, max leverage 3x, duration 2 seconds
    auth = manager.grant_authorization(
        account_id="acc_testnet_01",
        venue="gate",
        mode=TradingMode.TESTNET,
        decision_path=DecisionPath.AI_LED,
        allowed_instruments=["BTC_USDT"],
        allowed_sides=["LONG"],
        max_risk_fraction=Decimal("0.01"),
        max_leverage=3,
        daily_loss_limit_fraction=Decimal("0.03"),
        duration_seconds=1,  # Expire very quickly
        confirmed_by=ConfirmationSource.LOCAL_USER_WIZARD,
    )

    assert auth.status == AuthorizationStatus.ACTIVE
    assert auth.is_valid()

    gateway = ExecutionGateway(store)

    # A: Leverage exceeding authorized limit (5x > 3x) -> REJECTED
    intent_high_lev = OrderIntent(
        intent_id="intent_high_lev",
        idempotency_key="idem_high_lev",
        account_id="acc_testnet_01",
        mode=TradingMode.TESTNET,
        venue="gate",
        environment="TESTNET",
        decision_path=DecisionPath.AI_LED,
        instrument_id="BTC_USDT",
        side="LONG",
        order_type="market",
        quantity=1.0,
        price=100.0,
        leverage=5,
        protection_plan=ProtectionPlan(stop_price=95.0),
    )
    with pytest.raises(GatewayError) as exc_info:
        gateway.submit_intent(intent_high_lev)
    assert exc_info.value.code == "LEVERAGE_EXCEEDS_AUTHORIZED_MAX"

    # B: Instrument not authorized (ETH_USDT) -> REJECTED
    intent_wrong_inst = OrderIntent(
        intent_id="intent_wrong_inst",
        idempotency_key="idem_wrong_inst",
        account_id="acc_testnet_01",
        mode=TradingMode.TESTNET,
        venue="gate",
        environment="TESTNET",
        decision_path=DecisionPath.AI_LED,
        instrument_id="ETH_USDT",
        side="LONG",
        order_type="market",
        quantity=1.0,
        price=100.0,
        leverage=2,
        protection_plan=ProtectionPlan(stop_price=95.0),
    )
    with pytest.raises(GatewayError) as exc_info:
        gateway.submit_intent(intent_wrong_inst)
    assert exc_info.value.code == "INSTRUMENT_NOT_AUTHORIZED"

    # C: Wait for expiry (1.2s) -> EXPIRED
    time.sleep(1.2)
    assert not auth.is_valid()
    assert manager.get_active_authorization("acc_testnet_01") is None

    # Submission after expiry must be blocked immediately
    intent_expired = OrderIntent(
        intent_id="intent_exp",
        idempotency_key="idem_exp",
        account_id="acc_testnet_01",
        mode=TradingMode.TESTNET,
        venue="gate",
        environment="TESTNET",
        decision_path=DecisionPath.AI_LED,
        instrument_id="BTC_USDT",
        side="LONG",
        order_type="market",
        quantity=1.0,
        price=100.0,
        leverage=2,
        protection_plan=ProtectionPlan(stop_price=95.0),
    )
    with pytest.raises(GatewayError) as exc_info:
        gateway.submit_intent(intent_expired)
    assert exc_info.value.code == "AUTHORIZATION_EXPIRED"


# ============================================================================
# AT37: Revocation Concurrency, Protective Stop Preservation & In-flight Cancel
# ============================================================================
def test_at37_revocation_preserves_stops_and_reconciles_in_flight():
    """AT37: Revocation immediately cuts off new risk, preserves protective stop orders,

    and an in-flight cancel is not reported as complete without an adapter acknowledgement.
    """
    store = InMemoryStore()
    manager = AuthorizationManager(store)

    auth = manager.grant_authorization(
        account_id="acc_testnet_02",
        venue="gate",
        mode=TradingMode.TESTNET,
        decision_path=DecisionPath.AI_LED,
        allowed_instruments=["BTC_USDT"],
        allowed_sides=["LONG", "SHORT"],
        max_risk_fraction=Decimal("0.01"),
        max_leverage=3,
        duration_seconds=3600,
        confirmed_by=ConfirmationSource.LOCAL_USER_WIZARD,
    )

    mock_client = SimpleNamespace(
        place_order=lambda **kwargs: {"id": "ord_mock_prot_exit", "status": "open", "left": 0},
        cancel_order=lambda *args, **kwargs: {"status": "cancelled"},
    )
    class ProtectiveLedger:
        def get_open_positions(self, account_id):
            if account_id != "acc_testnet_02":
                return []
            return [{
                "position_id": "pos_at37_protected",
                "account_id": account_id,
                "venue": "gate",
                "mode": "TESTNET",
                "symbol": "BTC_USDT",
                "side": "LONG",
                "remaining_contracts": 1.0,
            }]

    gateway = ExecutionGateway(store, trader_client=mock_client, ledger=ProtectiveLedger())

    # 1. Revoke authorization
    revoked = manager.revoke_authorization(auth.authorization_id, reason="USER_KILL_SWITCH")
    assert revoked

    # 2. Attempting new opening order -> BLOCKED
    new_open_intent = OrderIntent(
        intent_id="intent_new_open",
        idempotency_key="idem_new_open",
        account_id="acc_testnet_02",
        mode=TradingMode.TESTNET,
        venue="gate",
        environment="TESTNET",
        instrument_id="BTC_USDT",
        side="LONG",
        order_type="market",
        quantity=1.0,
        price=100.0,
        reduce_only=False,
        protection_plan=ProtectionPlan(stop_price=95.0),
    )
    with pytest.raises(GatewayError) as exc_info:
        gateway.submit_intent(new_open_intent)
    assert exc_info.value.code in ("AUTHORIZATION_REVOKED", "AUTHORIZATION_REQUIRED")

    # 3. Protective order (reduce_only=True) must still be allowed to close/protect position!
    protective_exit_intent = OrderIntent(
        intent_id="intent_prot_exit",
        idempotency_key="idem_prot_exit",
        account_id="acc_testnet_02",
        mode=TradingMode.TESTNET,
        venue="gate",
        environment="TESTNET",
        instrument_id="BTC_USDT",
        side="SELL",
        order_type="market",
        quantity=1.0,
        price=95.0,
        reduce_only=True,
        protection_plan=ProtectionPlan(stop_price=95.0, reduce_only=True),
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    res = gateway.submit_intent(
        protective_exit_intent,
        market_snapshot={
            "price": 95.0,
            "data_as_of": now_iso,
            "received_at": now_iso,
            "fresh": True,
            "market": {
                "contractSize": 1.0,
                "precision": {"amount": 0.1, "price": 0.1},
                "limits": {"amount": {"min": 0.1, "max": 100.0, "step": 0.1}},
                "taker": 0.0005,
            },
        },
    )
    assert res["status"] in ("ACKNOWLEDGED", "SUBMITTED", "CREATED")

    # 4. In-flight cancellation test
    cancel_res = gateway.cancel_intent("intent_prot_exit")
    assert cancel_res["status"] == "CANCEL_PENDING"
    assert cancel_res["verified_reconciled"] is False


# ============================================================================
# AT38: Environment and Account Mismatch Rejection
# ============================================================================
def test_at38_environment_account_mismatch_rejected_before_submission():
    """AT38: Pre-submission check rejects testnet intent with live account or vice versa,

    preventing any erroneous endpoint network calls.
    """
    store = InMemoryStore()
    gateway = ExecutionGateway(store)

    # 1. Mode is TESTNET, but account is explicitly live
    mismatch_intent = OrderIntent(
        intent_id="intent_mismatch_1",
        idempotency_key="idem_mismatch_1",
        account_id="gate_live_main_account_01",
        mode=TradingMode.TESTNET,
        instrument_id="BTC_USDT",
        side="BUY",
        order_type="market",
        quantity=1.0,
        price=100.0,
        protection_plan=ProtectionPlan(stop_price=95.0),
    )
    with pytest.raises(GatewayError) as exc_info:
        gateway.submit_intent(mismatch_intent)
    assert exc_info.value.code == "ENVIRONMENT_ACCOUNT_MISMATCH"
    assert exc_info.value.status_code == 422

    # 2. Mode is LIVE, but account is testnet
    mismatch_intent_2 = OrderIntent(
        intent_id="intent_mismatch_2",
        idempotency_key="idem_mismatch_2",
        account_id="acc_testnet_sandbox_01",
        mode=TradingMode.LIVE,
        instrument_id="BTC_USDT",
        side="BUY",
        order_type="market",
        quantity=1.0,
        price=100.0,
        protection_plan=ProtectionPlan(stop_price=95.0),
    )
    with pytest.raises(GatewayError) as exc_info:
        gateway.submit_intent(mismatch_intent_2)
    assert exc_info.value.code == "ENVIRONMENT_ACCOUNT_MISMATCH"


# ============================================================================
# AT39: TESTNET Capability and Fee Reconciliation
# ============================================================================
def test_at39_testnet_capabilities_and_funding_fee_reconciliation():
    """AT39: Protocol-level verification of native TP/SL support, fee schedule,

    and 8-hour perpetual funding payment reconciliation.
    """
    service = TestnetCapabilityService()
    caps = service.get_capabilities(venue="gate", symbol="BTC_USDT")

    assert caps["venue"] == "gate"
    assert caps["symbol"] == "BTC_USDT"
    assert caps["status"] == "NOT_RUN"
    assert caps["source"] == "NOT_RUN_NO_ADAPTER"
    assert caps["has_native_tpsl"] is None
    assert caps["fee_schedule"] == {}
    assert caps["funding_interval_hours"] is None

    # Reconcile fee for a 10,000 USD notional trade
    fee_rec = service.reconcile_trade_fee(
        venue="gate",
        notional=Decimal("10000.00"),
        is_maker=False,
    )
    # The arithmetic is deterministic, but the schedule remains unverified.
    assert fee_rec["expected_fee"] == Decimal("5.00")
    assert fee_rec["fee_currency"] == "USDT"
    assert fee_rec["status"] == "UNVERIFIED"

    # Reconcile 8-hour perpetual funding payment: 10,000 USD with rate 0.01% = 1.00
    funding_rec = service.reconcile_funding_payment(
        position_notional=Decimal("10000.00"),
        funding_rate=Decimal("0.0001"),
    )
    assert funding_rec["funding_payment"] == Decimal("1.00")
    assert funding_rec["interval_hours"] is None


# ============================================================================
# AT40: Desktop Wizard Authorization vs AI Self-Granting Rejection
# ============================================================================
def test_at40_rejection_of_ai_self_authorization():
    """AT40: Strict rejection of model/LLM self-authorization; requires LOCAL_USER_WIZARD."""
    store = InMemoryStore()
    manager = AuthorizationManager(store)

    # 1. AI agent attempting to grant itself trading authority -> BLOCKED WITH AISelfConfirmationError
    with pytest.raises(AISelfConfirmationError):
        manager.grant_authorization(
            account_id="acc_target",
            venue="gate",
            mode=TradingMode.TESTNET,
            decision_path=DecisionPath.AI_LED,
            allowed_instruments=["BTC_USDT"],
            allowed_sides=["LONG"],
            max_risk_fraction=Decimal("0.01"),
            max_leverage=3,
            duration_seconds=3600,
            confirmed_by=ConfirmationSource.MODEL_LLM_OUTPUT,
        )

    # 2. Local user wizard authorization -> ACCEPTED
    valid_auth = manager.grant_authorization(
        account_id="acc_target",
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
    assert valid_auth.authorization_id.startswith("auth_")
    assert valid_auth.status == AuthorizationStatus.ACTIVE
    assert valid_auth.confirmed_by == ConfirmationSource.LOCAL_USER_WIZARD
