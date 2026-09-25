"""NoFx-inspired Gate route: isolated fixtures never represent exchange fills."""
import json
import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from core.model_routing import DEFAULT_SMART_MODEL
from core.storage import SQLiteStore
from core.trading.ai_led_engine import AICycleContext, AIActionOutput, AILedDecisionEngine
from core.trading.ai_strategy_book import AIStrategyBook, TEMPLATES
from core.trading.autonomous_strategy import CONTRACT, build_strategy_system_prompt
from core.trading.execution_gateway import ControlMode, DecisionPath, ExecutionGateway, GatewayError, OrderIntent, ProtectionPlan, TradingMode
from core.trading.gate_account_truth import GateAccountTruthService
from core.trading.gate_accounts import provision_default_gate_accounts
from core.trading.ledger import AccountLedger
from core.trading.model_schemas import require_nofx_gate_open_contract


def test_gate_open_contract_requires_model_amount_leverage_and_explicit_order_type():
    proposal = {
        "action": "OPEN_LONG", "entry_price": 100, "stop_price": 95,
        "take_profit": 110, "position_size_usdt": 1000,
        "requested_leverage": 5, "order_preference": "LIMIT",
        "evidence_refs": ["market_snapshot:BTCUSDT:observed"],
    }
    require_nofx_gate_open_contract(proposal)
    for missing in ("position_size_usdt", "requested_leverage", "order_preference"):
        invalid = {key: value for key, value in proposal.items() if key != missing}
        try:
            require_nofx_gate_open_contract(invalid)
        except ValueError as exc:
            assert missing in str(exc)
        else:
            raise AssertionError(f"{missing} must be model-authored")


def test_four_templates_use_one_gate_prompt_without_old_rr_gate():
    for template in TEMPLATES:
        prompt = build_strategy_system_prompt({
            "template_id": template["id"], "name": template["name"],
            "execution": template["execution_defaults"],
            "profile": template["profile"], "sections": template["sections"],
        }, nofx_gate=True)
        assert "position_size_usdt" in prompt
        assert "requested_leverage" in prompt
        assert "LIMIT" in prompt and "MARKET" in prompt
        assert "单笔风险" not in prompt
        assert template["sections"]["entry_standards"][:80] in prompt
        assert template["sections"]["decision_process"][:80] in prompt


def test_gate_model_open_reaches_gateway_without_old_risk_engine():
    now = datetime.now(timezone.utc)
    model_file = r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf"
    template = TEMPLATES[0]
    strategy = {
        "template_id": template["id"], "revision": 65,
        "execution": template["execution_defaults"],
    }
    context = AICycleContext(
        cycle_id="nofx-gate-cycle", account_id="gate_testnet", generation=1,
        started_at=now.isoformat(), expires_at=(now + timedelta(minutes=4)).isoformat(),
        allowed_instruments=("BTCUSDT",), decision_contract=CONTRACT,
        mode=TradingMode.TESTNET, venue="gate", environment="TESTNET",
        model_id=DEFAULT_SMART_MODEL, model_version=model_file,
        model_call_attempted=True, model_call_completed=True,
        model_inference_settings={
            "actual_model_id": model_file,
            "verified_manifest_model_id": model_file,
            "model_identity_source": "completion_response",
        },
        market_snapshots={"BTCUSDT": {
            "price": 100, "slippage": 0.001, "data_as_of": now.isoformat(),
            "market": {
                "contractSize": 1, "taker": 0.0005, "leverage_max": 20,
                "precision": {"amount": 1, "price": 0.1},
                "limits": {"amount": {"min": 1, "max": 1000}, "price": {"step": 0.1}},
            },
        }},
        account_truth={"status": "AVAILABLE", "available_margin": "500", "positions": []},
        strategy_instructions=strategy,
    )
    proposal = AIActionOutput(
        action="OPEN_LONG", instrument_id="BTCUSDT", reason="已收盘突破与量能支持",
        entry_price=99.9, stop_price=95, take_profit=110,
        position_size_usdt=1000, requested_leverage=5,
        order_preference="LIMIT", evidence_refs=("market_snapshot:BTCUSDT:observed",),
    )
    submitted = []

    class Gateway:
        def submit_intent(self, intent, **kwargs):
            submitted.append((intent, kwargs))
            return {"status": "ACKNOWLEDGED", "order_id": "fixture-only"}

    engine = object.__new__(AILedDecisionEngine)
    engine.gateway = Gateway()
    engine.ledger = SimpleNamespace(get_open_positions=lambda *args, **kwargs: [])
    engine.risk_engine = SimpleNamespace(evaluate_intent=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("old risk engine called")))
    engine.agent_policy_id = "fixture-policy"
    engine._persist_cycle = lambda *args, **kwargs: None
    engine._live_execution_quote = lambda *args, **kwargs: None
    result = engine.execute_cycle(context, now=now, model_output=proposal)
    assert result.status == "SUBMITTED"
    assert submitted[0][0].risk_policy == "MARGIN_ONLY"
    assert submitted[0][0].order_type == "limit"
    assert submitted[0][0].leverage == 20  # strategy target 100x, Gate contract ceiling 20x
    assert submitted[0][0].quantity == 10


def test_gate_live_model_open_uses_scoped_margin_only_gateway_without_order_side_effects():
    now = datetime.now(timezone.utc)
    model_file = r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf"
    template = TEMPLATES[0]
    context = AICycleContext(
        cycle_id="live-fixture-cycle", account_id="gate_live", generation=1,
        started_at=now.isoformat(), expires_at=(now + timedelta(minutes=4)).isoformat(),
        allowed_instruments=("BTCUSDT",), decision_contract=CONTRACT,
        mode=TradingMode.LIVE, venue="gate", environment="LIVE",
        model_id=DEFAULT_SMART_MODEL, model_version=model_file,
        model_call_attempted=True, model_call_completed=True,
        model_inference_settings={
            "actual_model_id": model_file,
            "verified_manifest_model_id": model_file,
            "model_identity_source": "completion_response",
        },
        market_snapshots={"BTCUSDT": {
            "price": 100, "slippage": 0.001, "data_as_of": now.isoformat(),
            "market": {"contractSize": 1, "taker": 0.0005, "leverage_max": 20,
                       "precision": {"amount": 1, "price": 0.1},
                       "limits": {"amount": {"min": 1, "max": 1000}, "price": {"step": 0.1}}},
        }},
        account_truth={"status": "AVAILABLE", "available_margin": "500", "positions": []},
        strategy_instructions={"template_id": template["id"], "revision": 2,
                               "execution": template["execution_defaults"]},
    )
    proposal = AIActionOutput(
        action="OPEN_LONG", instrument_id="BTCUSDT", reason="已收盘突破与量能支持",
        entry_price=99.9, stop_price=95, take_profit=110,
        position_size_usdt=1000, requested_leverage=5,
        order_preference="LIMIT", evidence_refs=("market_snapshot:BTCUSDT:observed",),
    )
    submitted = []

    class Gateway:
        def submit_intent(self, intent, **kwargs):
            submitted.append(intent)
            return {"status": "ACKNOWLEDGED", "order_id": "fixture-only"}

    engine = object.__new__(AILedDecisionEngine)
    engine.gateway = Gateway()
    engine.ledger = SimpleNamespace(get_open_positions=lambda *args, **kwargs: [])
    engine.risk_engine = SimpleNamespace(evaluate_intent=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("old risk engine called")))
    engine.agent_policy_id = "fixture-policy"
    engine._persist_cycle = lambda *args, **kwargs: None
    engine._live_execution_quote = lambda *args, **kwargs: None
    result = engine.execute_cycle(context, now=now, model_output=proposal)
    assert result.status == "SUBMITTED"
    assert len(submitted) == 1
    assert submitted[0].account_id == "gate_live"
    assert submitted[0].mode is TradingMode.LIVE
    assert submitted[0].risk_policy == "MARGIN_ONLY"


def test_gate_live_margin_reservation_uses_only_live_remote_equity(tmp_path):
    store = SQLiteStore(tmp_path / "live-margin.sqlite3")
    store.initialize()
    ledger = AccountLedger(store)
    ledger.create_account("gate_live", "LIVE", config={
        "account_type": "GATE_LIVE", "provider": "gate", "execution_mode": "LIVE",
    })
    book = AIStrategyBook(store)
    active = book.active("gate_live")
    active = book.save(
        "gate_live", name=active["name"], sections=active["sections"],
        expected_revision=active["revision"], execution={**active["execution"], "max_margin_pct": 18},
    )
    GateAccountTruthService(store).refresh(
        "gate_live", SimpleNamespace(get_account_truth=lambda **_: {
            "status": "AVAILABLE", "api_environment": "LIVE", "equity": "1000",
            "available_margin": "900", "used_margin": "100", "balance": {"order_margin": 0},
            "positions_status": "AVAILABLE", "pending_orders_status": "AVAILABLE",
            "positions": [], "pending_orders": [],
        }),
    )
    now = datetime.now(timezone.utc)
    approved = ledger.reserve_margin_only(
        "gate_live", "live-reservation", Decimal("70"),
        max_margin_pct=Decimal(str(active["execution"]["max_margin_pct"])),
        instrument_id="BTCUSDT", expires_at=now + timedelta(minutes=10), now=now,
    )
    assert approved
    with store._connect() as db:
        row = db.execute("SELECT account_id,mode,venue,status FROM risk_reservations WHERE reservation_id=?", ("live-reservation",)).fetchone()
    assert dict(row) == {"account_id": "gate_live", "mode": "LIVE", "venue": "gate", "status": "PENDING"}


def test_gate_live_snapshot_and_fill_remain_remote_only(tmp_path):
    store = SQLiteStore(tmp_path / "live-remote-only.sqlite3")
    store.initialize()
    provision_default_gate_accounts(store)
    ledger = AccountLedger(store)
    truth = GateAccountTruthService(store).refresh(
        "gate_live", SimpleNamespace(get_account_truth=lambda **_: {
            "status": "AVAILABLE", "api_environment": "LIVE", "equity": "1000",
            "available_margin": "950", "used_margin": "50",
            "positions_status": "AVAILABLE", "pending_orders_status": "AVAILABLE",
            "positions": [], "pending_orders": [],
        }),
    )
    assert truth["environment"] == "live"
    with store._connect() as db:
        row = db.execute("SELECT environment FROM gate_remote_account_snapshots WHERE snapshot_id=?", (truth["snapshot_id"],)).fetchone()
    assert row["environment"] == "live"
    snapshot = ledger.get_snapshot("gate_live")
    assert snapshot.source == "GATE_LIVE_REMOTE"
    assert snapshot.net_equity == Decimal("1000")
    recorded = ledger.record_trade_fill(
        "gate_live", "BTCUSDT", "BUY", Decimal("1"), Decimal("100"), Decimal("0"),
        mode="LIVE", venue="gate", order_id="remote-live-1", trade_id="fill-live-1",
    )
    assert recorded["status"] == "RECORDED"
    assert ledger.get_open_positions("gate_live", venue="gate", mode="LIVE") == []
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM simulated_positions WHERE account_id='gate_live'").fetchone()[0] == 0


def test_margin_only_reservation_uses_active_strategy_and_remote_equity(tmp_path):
    store = SQLiteStore(tmp_path / "margin-only.sqlite3")
    store.initialize()
    ledger = AccountLedger(store)
    ledger.create_account(
        "gate_testnet", "PAPER", config={
            "account_type": "GATE_TESTNET", "provider": "gate", "execution_mode": "TESTNET",
        },
    )
    book = AIStrategyBook(store)
    current = book.active("gate_testnet")
    book.save(
        "gate_testnet", name=current["name"], sections=current["sections"],
        expected_revision=current["revision"], execution={**current["execution"], "max_margin_pct": 18},
    )
    truth = {
        "status": "AVAILABLE", "api_environment": "TESTNET",
        "equity": "1000", "available_margin": "900", "used_margin": "100",
        "positions_status": "AVAILABLE", "pending_orders_status": "AVAILABLE",
        "positions": [], "pending_orders": [],
    }
    GateAccountTruthService(store).refresh(
        "gate_testnet", SimpleNamespace(get_account_truth=lambda **kwargs: truth),
    )
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(minutes=10)
    assert ledger.reserve_margin_only(
        "gate_testnet", "first", Decimal("70"), max_margin_pct=Decimal("18"),
        instrument_id="BTCUSDT", expires_at=expiry, now=now,
    )
    assert not ledger.reserve_margin_only(
        "gate_testnet", "second", Decimal("20"), max_margin_pct=Decimal("18"),
        instrument_id="ETHUSDT", expires_at=expiry, now=now,
    )


def test_margin_only_blocks_new_open_when_existing_entry_order_margin_is_unknown(tmp_path):
    store = SQLiteStore(tmp_path / "pending-margin.sqlite3")
    store.initialize()
    ledger = AccountLedger(store)
    ledger.create_account("gate_testnet", "PAPER", config={
        "account_type": "GATE_TESTNET", "provider": "gate", "execution_mode": "TESTNET",
    })
    AIStrategyBook(store).active("gate_testnet")
    truth = {
        "status": "AVAILABLE", "api_environment": "TESTNET",
        "equity": "1000", "available_margin": "1000", "used_margin": "0",
        "positions_status": "AVAILABLE", "pending_orders_status": "AVAILABLE",
        "balance": {"order_margin": 0}, "positions": [],
        "pending_orders": [{"order_id": "123", "symbol": "BTCUSDT", "reduce_only": False}],
    }
    GateAccountTruthService(store).refresh(
        "gate_testnet", SimpleNamespace(get_account_truth=lambda **kwargs: truth),
    )
    now = datetime.now(timezone.utc)
    assert not ledger.reserve_margin_only(
        "gate_testnet", "new-open", Decimal("10"), max_margin_pct=Decimal("12"),
        instrument_id="ETHUSDT", expires_at=now + timedelta(minutes=10), now=now,
    )


def test_fixed_usdt_margin_cap_is_enforced_against_remote_position_margin(tmp_path):
    store = SQLiteStore(tmp_path / "fixed-margin.sqlite3")
    store.initialize()
    ledger = AccountLedger(store)
    ledger.create_account("gate_testnet", "PAPER", config={
        "account_type": "GATE_TESTNET", "provider": "gate", "execution_mode": "TESTNET",
    })
    book = AIStrategyBook(store)
    active = book.active("gate_testnet")
    book.save(
        "gate_testnet", name=active["name"], sections=active["sections"],
        expected_revision=active["revision"], execution={
            **active["execution"], "margin_cap_mode": "FIXED_USDT", "max_margin_usdt": 150,
        },
    )
    GateAccountTruthService(store).refresh("gate_testnet", SimpleNamespace(get_account_truth=lambda **_: {
        "status": "AVAILABLE", "api_environment": "TESTNET", "equity": "1000",
        "available_margin": "900", "used_margin": "100", "balance": {"order_margin": 0},
        "positions_status": "AVAILABLE", "pending_orders_status": "AVAILABLE",
        "positions": [{"symbol": "BTCUSDT", "initial_margin": 100}], "pending_orders": [],
    }))
    now = datetime.now(timezone.utc)
    kwargs = dict(max_margin_pct=Decimal(str(active["execution"]["max_margin_pct"])),
                  margin_cap_mode="FIXED_USDT", max_margin_usdt=Decimal("150"),
                  instrument_id="ETHUSDT", expires_at=now + timedelta(minutes=10), now=now)
    assert ledger.reserve_margin_only("gate_testnet", "first-fixed", Decimal("40"), **kwargs)
    assert not ledger.reserve_margin_only("gate_testnet", "exceeds-fixed", Decimal("11"), **kwargs)


def test_gateway_matches_ccxt_swap_symbol_to_gate_contract_for_owned_position():
    assert ExecutionGateway._gate_symbol_key("SPCX/USDT:USDT") == "SPCXUSDT"
    assert ExecutionGateway._gate_symbol_key("SPCX_USDT") == "SPCXUSDT"
    assert ExecutionGateway._gate_symbol_key("SPCXUSDT") == "SPCXUSDT"


def test_ai_can_cancel_only_exact_system_owned_gate_order_id(tmp_path):
    store = SQLiteStore(tmp_path / "owned-cancel.sqlite3")
    store.initialize()
    provision_default_gate_accounts(store)
    gateway = ExecutionGateway(store)
    now = datetime.now(timezone.utc).isoformat()
    with store._connect() as db:
        for intent_id, order_id in (("intent_ai_owned", "123456"), ("external_order", "987654")):
            db.execute(
                """INSERT INTO order_intents
                   (intent_id,idempotency_key,account_id,mode,instrument_id,side,order_type,quantity,
                    payload_hash,status,execution_result_json,created_at,updated_at,venue,reduce_only)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (intent_id, intent_id, "gate_testnet", "TESTNET", "BTCUSDT", "LONG", "limit", 1,
                 "hash", "ACKNOWLEDGED", json.dumps({"order_id": order_id}), now, now, "gate", 0),
            )
    canceled = []
    gateway.cancel_intent = lambda intent_id, reason: canceled.append((intent_id, reason)) or {"status": "CANCELED"}
    result = gateway.cancel_owned_gate_order(account_id="gate_testnet", instrument_id="BTCUSDT", remote_order_id="123456")
    assert result["status"] == "CANCELED"
    assert canceled == [("intent_ai_owned", "AI_CANCEL_ORDER")]
    for remote_id in ("987654", "999999", "not-numeric"):
        try:
            gateway.cancel_owned_gate_order(account_id="gate_testnet", instrument_id="BTCUSDT", remote_order_id=remote_id)
        except GatewayError as exc:
            assert exc.code in {"SYSTEM_ORDER_OWNERSHIP_UNVERIFIED", "REMOTE_ORDER_ID_UNVERIFIED"}
        else:
            raise AssertionError("AI must not cancel an unmatched or external order")


def test_owned_protection_replacement_arms_new_leg_before_cancel_and_can_resume(tmp_path, monkeypatch):
    import core.trading.gate_accounts as gate_accounts

    store = SQLiteStore(tmp_path / "protection-replace.sqlite3")
    store.initialize()
    AccountLedger(store).create_account("gate_testnet", "PAPER", config={
        "account_type": "GATE_TESTNET", "provider": "gate", "execution_mode": "TESTNET",
    })
    gateway = ExecutionGateway(store)
    now = datetime.now(timezone.utc).isoformat()
    receipt = {
        "filled": 2, "average": 100,
        "protection_orders": [
            {"leg": "stop_loss", "order_id": "11", "trigger_price": 110},
            {"leg": "take_profit", "order_id": "12", "trigger_price": 90},
        ],
    }
    with store._connect() as db:
        db.execute(
            """INSERT INTO order_intents
               (intent_id,idempotency_key,account_id,mode,instrument_id,side,order_type,quantity,
                payload_hash,status,execution_result_json,created_at,updated_at,venue,reduce_only)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("intent_ai_filled", "intent_ai_filled", "gate_testnet", "TESTNET", "BTCUSDT", "SHORT",
             "limit", 2, "hash", "FILLED", json.dumps(receipt), now, now, "gate", 0),
        )

    class FakeGate:
        last_positions_status = "AVAILABLE"

        def __init__(self):
            self.orders = {"11": ["OPEN", 110], "12": ["OPEN", 90]}
            self.events = []
            self.fail_cancel_once = True

        def get_positions(self):
            return [{"symbol": "BTC/USDT:USDT", "position_id": 7, "side": "SHORT",
                     "contracts": 2, "entry_price": 100}]

        def fetch_protection_order(self, order_id, symbol):
            status, price = self.orders[str(order_id)]
            return {"order_id": str(order_id), "status": status, "trigger_price": price,
                    "finish_as": "cancelled" if status == "FINISHED" else None, "observed_at": now}

        def place_protection_orders(self, symbol, **kwargs):
            self.events.append("create")
            self.orders["13"] = ["OPEN", kwargs["take_profit"]]
            return [{"leg": "take_profit", "order_id": "13"}]

        def cancel_protection_order(self, order_id, symbol):
            self.events.append("cancel")
            if self.fail_cancel_once:
                self.fail_cancel_once = False
                raise ValueError("temporary readback failure")
            assert self.orders["13"][0] == "OPEN"
            self.orders[str(order_id)][0] = "FINISHED"
            return {"status": "CANCELED"}

    fake = FakeGate()
    monkeypatch.setattr(gate_accounts, "build_gate_trader", lambda *_: fake)
    with pytest.raises(GatewayError, match="Replacement is armed"):
        gateway.update_gate_protection(account_id="gate_testnet", instrument_id="BTCUSDT",
                                       position_id="7", new_take_profit=89)
    with store._connect() as db:
        pending = json.loads(db.execute("SELECT execution_result_json FROM order_intents WHERE intent_id='intent_ai_filled'").fetchone()[0])
    assert pending["protection_replacements"]["take_profit"]["new_id"] == "13"
    assert fake.orders["12"][0] == "OPEN" and fake.orders["13"][0] == "OPEN"
    result = gateway.update_gate_protection(account_id="gate_testnet", instrument_id="BTCUSDT",
                                            position_id="7", new_take_profit=89)
    assert result["status"] == "VERIFIED"
    assert fake.events == ["create", "cancel", "cancel"]
    assert fake.orders["12"][0] == "FINISHED" and fake.orders["13"][0] == "OPEN"
    with store._connect() as db:
        final = json.loads(db.execute("SELECT execution_result_json FROM order_intents WHERE intent_id='intent_ai_filled'").fetchone()[0])
    assert next(item for item in final["protection_orders"] if item["leg"] == "take_profit")["order_id"] == "13"
    assert not final["protection_replacements"]


def test_closed_gate_position_cancels_only_its_owned_orphan_protection(tmp_path, monkeypatch):
    import core.trading.gate_accounts as gate_accounts

    store = SQLiteStore(tmp_path / "closed-protection.sqlite3")
    store.initialize()
    AccountLedger(store).create_account("gate_testnet", "PAPER", config={
        "account_type": "GATE_TESTNET", "provider": "gate", "execution_mode": "TESTNET",
    })
    gateway = ExecutionGateway(store)
    now = datetime.now(timezone.utc).isoformat()
    receipt = {"protection_orders": [{"leg": "take_profit", "order_id": "11", "status": "open"}]}
    with store._connect() as db:
        db.execute(
            """INSERT INTO order_intents
               (intent_id,idempotency_key,account_id,mode,instrument_id,side,order_type,quantity,
                payload_hash,status,execution_result_json,created_at,updated_at,venue,reduce_only)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("intent_ai_closed", "intent_ai_closed", "gate_testnet", "TESTNET", "BTCUSDT", "SHORT",
             "limit", 2, "hash", "FILLED", json.dumps(receipt), now, now, "gate", 0),
        )

    canceled = []

    class FakeGate:
        def fetch_protection_order(self, order_id, symbol):
            assert (order_id, symbol) == ("11", "BTCUSDT")
            return {"status": "OPEN", "reduce_only": True}

        def cancel_protection_order(self, order_id, symbol):
            canceled.append((order_id, symbol))
            return {"status": "CANCELED"}

    monkeypatch.setattr(gate_accounts, "build_gate_trader", lambda *_: FakeGate())
    truth = {"status": "AVAILABLE", "positions_status": "AVAILABLE", "pending_orders_status": "AVAILABLE",
             "positions": [], "pending_orders": [
                 {"order_id": "11", "symbol": "BTCUSDT", "reduce_only": True},
                 {"order_id": "22", "symbol": "ETHUSDT", "reduce_only": True},
             ]}
    assert gateway.cleanup_closed_gate_protection(account_id="gate_testnet", remote_truth={**truth, "positions": [{"symbol": "BTCUSDT"}]}) == []
    cleaned = gateway.cleanup_closed_gate_protection(account_id="gate_testnet", remote_truth=truth)
    assert cleaned == [{"intent_id": "intent_ai_closed", "symbol": "BTCUSDT", "order_id": "11", "leg": "take_profit", "status": "canceled_after_position_close"}]
    assert canceled == [("11", "BTCUSDT")]
    with store._connect() as db:
        final = json.loads(db.execute("SELECT execution_result_json FROM order_intents WHERE intent_id='intent_ai_closed'").fetchone()[0])
    assert final["protection_orders"][0]["status"] == "canceled_after_position_close"


def test_closed_gate_protection_reconciles_terminal_gate_order_without_pending_row(tmp_path, monkeypatch):
    import core.trading.gate_accounts as gate_accounts

    store = SQLiteStore(tmp_path / "terminal-protection.sqlite3")
    store.initialize()
    AccountLedger(store).create_account("gate_testnet", "PAPER", config={
        "account_type": "GATE_TESTNET", "provider": "gate", "execution_mode": "TESTNET",
    })
    gateway = ExecutionGateway(store)
    now = datetime.now(timezone.utc).isoformat()
    with store._connect() as db:
        db.execute(
            """INSERT INTO order_intents
               (intent_id,idempotency_key,account_id,mode,instrument_id,side,order_type,quantity,
                payload_hash,status,execution_result_json,created_at,updated_at,venue,reduce_only)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("intent_ai_terminal", "intent_ai_terminal", "gate_testnet", "TESTNET", "BTCUSDT", "SHORT",
             "limit", 2, "hash", "FILLED", json.dumps({"protection_status": "ACTIVE", "protection_orders": [
                 {"leg": "stop_loss", "order_id": "11", "status": "open"},
                 {"leg": "take_profit", "order_id": "12", "status": "open"},
             ]}), now, now, "gate", 0),
        )

    class FakeGate:
        def fetch_protection_order(self, order_id, symbol):
            assert symbol == "BTCUSDT"
            return {"status": "FINISHED", "finish_as": "triggered" if order_id == "11" else "cancelled", "reduce_only": True}

        def cancel_protection_order(self, *_args):
            raise AssertionError("Gate already ended both orders")

    monkeypatch.setattr(gate_accounts, "build_gate_trader", lambda *_: FakeGate())
    truth = {"status": "AVAILABLE", "positions_status": "AVAILABLE", "pending_orders_status": "AVAILABLE",
             "positions": [], "pending_orders": []}
    result = gateway.cleanup_closed_gate_protection(account_id="gate_testnet", remote_truth=truth)
    assert {item["status"] for item in result} == {"finished_triggered", "finished_cancelled"}
    with store._connect() as db:
        receipt = json.loads(db.execute("SELECT execution_result_json FROM order_intents WHERE intent_id='intent_ai_terminal'").fetchone()[0])
    assert receipt["protection_status"] == "CLOSED_POSITION_RECONCILED"


def test_gateway_margin_reservation_preserves_ai_leverage(tmp_path):
    store = SQLiteStore(tmp_path / "gateway-margin.sqlite3")
    store.initialize()
    book = AIStrategyBook(store)
    current = book.active("gate_testnet")
    book.save("gate_testnet", name=current["name"], sections=current["sections"],
              expected_revision=current["revision"], execution=current["execution"])
    reservations = []
    gateway = object.__new__(ExecutionGateway)
    gateway.store = store
    gateway.ledger = SimpleNamespace(reserve_margin_only=lambda *args, **kwargs: reservations.append((args, kwargs)) or True)
    intent = OrderIntent(
        intent_id="fixture-ai-open", idempotency_key="fixture-ai-open", account_id="gate_testnet",
        mode=TradingMode.TESTNET, venue="gate", instrument_id="BTCUSDT", side="LONG",
        order_type="limit", quantity=10, price=100, leverage=5,
        protection_plan=ProtectionPlan(stop_price=95, take_profit=110), risk_policy="MARGIN_ONLY",
    )
    decision = gateway._reserve_margin_only_ai_intent(intent, {
        "price": 100, "slippage": 0.001,
        "market": {"contractSize": 1, "taker": 0.0005, "leverage_max": 20},
    }, datetime.now(timezone.utc))
    assert decision.approved
    assert decision.leverage == Decimal("5")
    assert decision.notional == Decimal("1000")
    assert decision.allocated_margin == Decimal("201.0000")
    assert reservations[0][1]["max_margin_pct"] == Decimal("12.0")


@pytest.mark.parametrize("account_id,mode,account_type", [
    ("gate_testnet", TradingMode.TESTNET, "GATE_TESTNET"),
    ("gate_live", TradingMode.LIVE, "GATE_LIVE"),
])
def test_unified_gateway_submits_gate_order_without_legacy_risk_engine(tmp_path, monkeypatch, account_id, mode, account_type):
    import core.trading.gate_accounts as gate_accounts

    store = SQLiteStore(tmp_path / "integrated-gate.sqlite3")
    store.initialize()
    ledger = AccountLedger(store)
    ledger.create_account(account_id, mode.value, config={
        "account_type": account_type, "provider": "gate", "execution_mode": mode.value,
    })
    book = AIStrategyBook(store)
    template = TEMPLATES[0]
    book.save(account_id, name=template["name"], sections=template["sections"],
              expected_revision=0, template_id=template["id"], execution=template["execution_defaults"])

    class FakeGate:
        def __init__(self):
            self.orders = []

        def get_account_truth(self, **kwargs):
            return {
                "status": "AVAILABLE", "api_environment": mode.value, "equity": "1000",
                "available_margin": "1000", "used_margin": "0",
                "positions_status": "AVAILABLE", "pending_orders_status": "AVAILABLE",
                "positions": [], "pending_orders": [],
            }

        def place_order(self, **kwargs):
            self.orders.append(kwargs)
            return {"status": "open", "order_id": "fixture-remote-ack", "amount": kwargs["amount"], "filled": 0}

    fake = FakeGate()
    monkeypatch.setattr(gate_accounts, "is_managed_gate_account", lambda *args: True)
    monkeypatch.setattr(gate_accounts, "build_gate_trader", lambda *args: fake)
    gateway = ExecutionGateway(store, ledger=ledger)
    gateway.risk_engine = SimpleNamespace(evaluate_intent=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy risk engine called")))
    now = datetime.now(timezone.utc)
    intent = OrderIntent(
        intent_id="integrated-ai-open", idempotency_key="integrated-ai-open",
        account_id=account_id, mode=mode, venue="gate", environment=mode.value,
        instrument_id="BTCUSDT", side="LONG", order_type="limit", quantity=5,
        price=100, leverage=5, protection_plan=ProtectionPlan(stop_price=95, take_profit=110),
        risk_policy="MARGIN_ONLY", control_mode=ControlMode.AUTONOMOUS,
        decision_path=DecisionPath.AI_LED,
    )
    market = {
        "symbol": "BTCUSDT", "price": 100, "fresh": True,
        "data_as_of": now.isoformat(), "received_at": now.isoformat(), "slippage": 0.001,
        "market": {
            "contractSize": 1, "taker": 0.0005, "leverage_max": 20,
            "precision": {"amount": 1, "price": 0.1},
            "limits": {"amount": {"min": 1, "max": 1000}, "price": {"step": 0.1}},
        },
    }
    receipt = gateway.submit_intent(intent, market_snapshot=market, now=now)
    assert receipt["status"] == "ACKNOWLEDGED"
    assert len(fake.orders) == 1
    assert fake.orders[0]["price"] == 100
    assert fake.orders[0]["leverage"] == 5
    with store._connect() as db:
        row = db.execute("SELECT risk_decision_json FROM order_intents WHERE intent_id=?", (intent.intent_id,)).fetchone()
    assert "MARGIN_ONLY_APPROVED" in row["risk_decision_json"]
