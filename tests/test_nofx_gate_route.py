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
from core.trading.gate_live_client import GateLiveTrader, GateProtectionPlacementError
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


@pytest.mark.parametrize("field", ["entry_price", "stop_price", "take_profit"])
@pytest.mark.parametrize("value", [None, 0, -1, float("nan"), True])
def test_gate_open_cannot_accept_missing_or_invalid_protection_prices(field, value):
    proposal = {"action": "OPEN_LONG", "entry_price": 100, "stop_price": 95,
        "take_profit": 110, "position_size_usdt": 2000, "requested_leverage": 20,
        "order_preference": "LIMIT", "evidence_refs": ["market_snapshot:BTCUSDT:observed"]}
    proposal[field] = value
    with pytest.raises(ValueError, match="NOFX_GATE_OPEN_"):
        require_nofx_gate_open_contract(proposal)


def test_four_templates_use_one_gate_prompt_without_old_rr_gate():
    for template in TEMPLATES:
        prompt = build_strategy_system_prompt({
            "template_id": template["id"], "name": template["name"],
            "execution": template["execution_defaults"],
            "profile": template["profile"], "sections": template["sections"],
        }, nofx_gate=True)
        assert "position_size_usdt" in prompt
        assert "requested_leverage" in prompt
        assert "每笔开仓名义价值固定为 2000.0 USDT" in prompt
        assert "不受策略旧的固定杠杆数字限制" in prompt
        assert "不要机械填写最高杠杆" in prompt
        assert "LIMIT" in prompt and "MARKET" in prompt
        assert "单笔风险" not in prompt
        assert template["sections"]["entry_standards"][:80] in prompt
        assert template["sections"]["decision_process"][:80] in prompt


def test_partial_gate_fill_extends_both_protection_legs_and_resumes_by_order_id(tmp_path):
    store = SQLiteStore(tmp_path / "coverage.sqlite3")
    store.initialize()
    gateway = ExecutionGateway(store)
    intent = OrderIntent(
        intent_id="intent_partial", idempotency_key="intent_partial", account_id="gate_testnet",
        mode=TradingMode.TESTNET, instrument_id="BTCUSDT", side="LONG",
        order_type="limit", quantity=2, price=100,
        protection_plan=ProtectionPlan(stop_price=95, take_profit=110),
    )
    now = datetime.now(timezone.utc).isoformat()
    with store._connect() as db:
        db.execute(
            """INSERT INTO order_intents
               (intent_id,idempotency_key,account_id,mode,instrument_id,side,order_type,
                quantity,payload_hash,status,created_at,updated_at,venue,reduce_only)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (intent.intent_id, intent.idempotency_key, intent.account_id, "TESTNET", "BTCUSDT",
             "LONG", "limit", 2, "hash", "PARTIALLY_FILLED", now, now, "gate", 0),
        )

    class FakeGate:
        def __init__(self):
            self.orders = {}
            self.next_id = 10
            self.events = []
            self.fail_cancel_once = True

        def place_protection_orders(self, symbol, **kwargs):
            self.next_id += 1
            oid = str(self.next_id)
            leg = "stop_loss" if kwargs.get("stop_loss") is not None else "take_profit"
            price = kwargs.get("stop_loss") or kwargs.get("take_profit")
            self.orders[oid] = {"amount": kwargs["amount"], "trigger_price": price, "status": "OPEN"}
            self.events.append(("create", oid, leg, kwargs["amount"]))
            return [{"leg": leg, "order_id": oid, "trigger_price": price, "amount": kwargs["amount"]}]

        def fetch_protection_order(self, order_id, symbol):
            assert symbol == "BTCUSDT"
            order = self.orders[str(order_id)]
            return {
                "order_id": str(order_id), "symbol": symbol, "reduce_only": True,
                "initial": {"contract": "BTC_USDT", "size": -order["amount"], "reduce_only": True, "text": "t-test"},
                "finish_as": "cancelled" if order["status"] == "FINISHED" else None,
                **order,
            }

        def cancel_protection_order(self, order_id, symbol):
            self.events.append(("cancel", str(order_id)))
            if self.fail_cancel_once:
                self.fail_cancel_once = False
                raise ValueError("temporary failure")
            self.orders[str(order_id)]["status"] = "FINISHED"
            return {"status": "CANCELED"}

    fake = FakeGate()
    first = gateway._reconcile_gate_entry_protection(intent, fake, Decimal("1"), {})
    assert first["protection_verified"] is True
    assert {leg["order_id"] for leg in first["protection_orders"]} == {"11", "12"}
    second = gateway._reconcile_gate_entry_protection(
        intent, fake, Decimal("2"), {"protection_orders": first["protection_orders"]},
    )
    assert second["protection_verified"] is False
    with store._connect() as db:
        receipt = json.loads(db.execute(
            "SELECT execution_result_json FROM order_intents WHERE intent_id=?", (intent.intent_id,)
        ).fetchone()[0])
    assert receipt["protection_replacements"]["stop_loss"]["new_id"] == "13"
    # A new gateway instance stands in for a process restart.  Only persisted
    # Gate IDs, not the old in-memory helper state, are available to it.
    resumed = ExecutionGateway(store)._reconcile_gate_entry_protection(intent, fake, Decimal("2"), receipt)
    assert resumed["protection_verified"] is True
    assert {leg["order_id"] for leg in resumed["protection_orders"]} == {"13", "14"}
    assert not resumed["protection_replacements"]
    assert fake.orders["11"]["status"] == "FINISHED"
    assert fake.orders["12"]["status"] == "FINISHED"
    assert len([event for event in fake.events if event[0] == "create"]) == 4


@pytest.mark.parametrize("parent_status,filled", [("FILLED", 2), ("CANCELED", 1)])
def test_gateway_reconcile_resumes_terminal_parent_protection_replacement_without_model_call(tmp_path, monkeypatch, parent_status, filled):
    import core.trading.gate_accounts as gate_accounts

    store = SQLiteStore(tmp_path / f"resume-protection-{parent_status.lower()}.sqlite3")
    store.initialize()
    AccountLedger(store).create_account("gate_testnet", "PAPER", config={
        "account_type": "GATE_TESTNET", "provider": "gate", "execution_mode": "TESTNET",
    })
    gateway = ExecutionGateway(store)
    now = datetime.now(timezone.utc).isoformat()
    receipt = {
        "intent_id": "intent_ai_resume_protection",
        "order_id": "8100",
        "status": parent_status,
        "filled_quantity": filled,
        "average_price": 100,
        "protection_status": "PENDING_VERIFICATION",
        "protection_orders": [
            {"leg": "stop_loss", "order_id": "11", "status": "open", "trigger_price": 95},
            {"leg": "take_profit", "order_id": "12", "status": "open", "trigger_price": 110},
        ],
        "protection_replacements": {
            "stop_loss": {"old_id": "11", "new_id": "13", "target_price": 94.0},
        },
    }
    with store._connect() as db:
        db.execute(
            """INSERT INTO order_intents
               (intent_id,idempotency_key,account_id,mode,environment,venue,instrument_id,side,order_type,
                quantity,protection_plan_json,payload_hash,status,execution_result_json,created_at,updated_at,
                reduce_only,decision_path,control_mode)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("intent_ai_resume_protection", "resume-protection", "gate_testnet", "TESTNET", "TESTNET", "gate",
             "BTCUSDT", "LONG", "limit", 2, json.dumps({"stop_price": 95, "take_profit": 110}),
             "resume-hash", parent_status, json.dumps(receipt), now, now, 0, "AI_LED", "AUTONOMOUS"),
        )

    class FakeGate:
        def __init__(self):
            self.orders = {
                "11": {"status": "OPEN", "amount": filled, "trigger_price": 95},
                "12": {"status": "OPEN", "amount": filled, "trigger_price": 110},
                "13": {"status": "OPEN", "amount": filled, "trigger_price": 94},
            }
            self.canceled = []
            self.parent_fetches = 0

        def fetch_protection_order(self, order_id, symbol):
            order = self.orders[str(order_id)]
            return {
                "order_id": str(order_id), "symbol": symbol, "status": order["status"],
                "amount": order["amount"], "trigger_price": order["trigger_price"], "reduce_only": True,
                "finish_as": "cancelled" if order["status"] == "FINISHED" else None,
                "trade_id": None, "me_order_id": "8100",
                "initial": {"contract": "BTC_USDT", "size": -order["amount"], "reduce_only": True, "text": "t-resume"},
                "raw": {"id": str(order_id), "initial": {"contract": "BTC_USDT", "size": -order["amount"], "reduce_only": True, "text": "t-resume"}},
                "observed_at": now,
            }

        def cancel_protection_order(self, order_id, symbol):
            self.canceled.append(str(order_id))
            self.orders[str(order_id)]["status"] = "FINISHED"
            return {"status": "CANCELED", "order_id": str(order_id)}

        def place_protection_orders(self, *args, **kwargs):
            raise AssertionError("recovery must use the persisted new ID, not create another leg")

        def normalize_protection_price(self, symbol, price):
            return float(price)

        def fetch_order(self, *args):
            self.parent_fetches += 1
            raise AssertionError("terminal parent protection recovery must not need a model or parent refetch")

    fake = FakeGate()
    monkeypatch.setattr(gate_accounts, "build_gate_trader", lambda *_: fake)

    result = gateway.reconcile_in_flight_orders("gate_testnet", TradingMode.TESTNET)

    assert result and result[0]["reconciled"] is True
    assert result[0]["reconciled_status"] == parent_status
    assert fake.canceled == ["11"]
    assert fake.parent_fetches == 0
    with store._connect() as db:
        row = db.execute("SELECT status,execution_result_json FROM order_intents WHERE intent_id=?", ("intent_ai_resume_protection",)).fetchone()
    final = json.loads(row["execution_result_json"])
    assert row["status"] == parent_status
    assert final["protection_status"] == "PROTECTED"
    assert not final["protection_replacements"]
    assert {item["order_id"] for item in final["protection_orders"]} == {"12", "13"}
    assert final["protection_terminal_observations"][0]["protection_order_id"] == "11"


def test_filled_gate_entry_without_verified_protection_stays_reconcilable():
    gateway = object.__new__(ExecutionGateway)
    gateway._record_exchange_fill_report = lambda *_args: {
        "recorded": True, "ledger_record": {"status": "RECORDED"},
        "protection_status": "PENDING", "protection_evidence": None,
        "economic_evidence": None,
    }
    intent = OrderIntent(
        intent_id="filled-unprotected", idempotency_key="filled-unprotected",
        account_id="gate_testnet", mode=TradingMode.TESTNET, venue="gate",
        instrument_id="BTCUSDT", side="LONG", order_type="market", quantity=1,
        protection_plan=ProtectionPlan(stop_price=95, take_profit=110),
    )

    class FakeGate:
        def place_order(self, **_kwargs):
            return {"status": "closed", "order_id": "123456", "amount": 1,
                    "filled_quantity": 1, "protection_verified": False}

    receipt = gateway._execute_exchange(intent, FakeGate())
    assert receipt["status"] == "UNKNOWN"
    assert receipt["order_id"] == "123456"
    assert receipt["fill_reconciliation"] == "PROTECTION_READBACK_REQUIRED"


def test_gate_native_trigger_is_normalized_to_contract_tick_before_readback():
    class FakeExchange:
        def __init__(self):
            self.params = None

        def price_to_precision(self, _symbol, _price):
            return "2585.45"

        def create_order(self, **kwargs):
            self.params = kwargs["params"]
            return {"id": "1234567", "status": "open", "info": {"status": "open"}}

    exchange = FakeExchange()
    trader = object.__new__(GateLiveTrader)
    trader.live_trading_enabled = True
    trader._get_exchange = lambda: exchange
    trader._exchange_symbol = lambda _exchange, symbol: symbol
    trader.fetch_protection_order = lambda order_id, _symbol: {
        "order_id": order_id, "status": "OPEN", "trigger_price": 2585.45,
        "amount": 1, "reduce_only": True,
    }

    legs = trader.place_protection_orders(
        "ETHUSDT", side="LONG", amount=1, stop_loss=2585.47,
    )

    assert exchange.params["stopLossPrice"] == 2585.45
    assert legs[0]["order_id"] == "1234567"
    assert legs[0]["trigger_price"] == 2585.45


def test_gate_native_trigger_rejects_undercovered_partial_fill():
    class FakeExchange:
        def price_to_precision(self, _symbol, price):
            return str(price)

        def create_order(self, **_kwargs):
            return {"id": "1234567", "status": "open", "info": {"status": "open"}}

    trader = object.__new__(GateLiveTrader)
    trader.live_trading_enabled = True
    trader._get_exchange = lambda: FakeExchange()
    trader._exchange_symbol = lambda _exchange, symbol: symbol
    trader.fetch_protection_order = lambda order_id, _symbol: {
        "order_id": order_id, "status": "OPEN", "trigger_price": 95,
        "amount": 1, "reduce_only": True,
    }

    with pytest.raises(GateProtectionPlacementError) as caught:
        trader.place_protection_orders("ETHUSDT", side="LONG", amount=2, stop_loss=95)
    assert caught.value.unverified_legs[0]["order_id"] == "1234567"


@pytest.mark.parametrize("requested_leverage,expected_leverage,expected_quantity,cap_mode,cap_usdt,equity,used,available,requested_notional", [
    (5, 5, 6, "PERCENT", 1000, 100000, 50, 130, 1000),
    (25, 20, 10, "PERCENT", 1000, 100000, 50, 500, 1000),
    (5, 5, 2, "FIXED_USDT", 100, 100000, 50, 500, 1000),
    (5, 5, 25, "PERCENT", 1000, 100000, 0, 50000, 10000),
])
def test_gate_model_open_uses_ai_leverage_and_account_margin_without_old_risk_engine(requested_leverage, expected_leverage, expected_quantity, cap_mode, cap_usdt, equity, used, available, requested_notional):
    now = datetime.now(timezone.utc)
    model_file = DEFAULT_SMART_MODEL
    template = next(item for item in TEMPLATES if item["id"] == "price_action_structure")
    strategy = {
        "template_id": template["id"], "revision": 65,
        "execution": {**template["execution_defaults"], "sizing_mode": "RISK_BASED", "leverage_mode": "STRATEGY_LIMIT", "max_notional_usdt": 2500,
                      "margin_cap_mode": cap_mode, "max_margin_usdt": cap_usdt},
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
        account_truth={"status": "AVAILABLE", "equity": str(equity), "available_margin": str(available), "used_margin": str(used), "positions": []},
        strategy_instructions=strategy,
    )
    proposal = AIActionOutput(
        action="OPEN_LONG", instrument_id="BTCUSDT", reason="已收盘突破与量能支持",
        entry_price=99.9, stop_price=95, take_profit=112,
        position_size_usdt=requested_notional, requested_leverage=requested_leverage,
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
    assert submitted[0][0].leverage == expected_leverage
    assert submitted[0][0].quantity == expected_quantity
    assert proposal.extra_fields["leverage_selection"]["model_requested"] == requested_leverage


@pytest.mark.parametrize("mode,account_type", [(TradingMode.TESTNET, "GATE_TESTNET"), (TradingMode.LIVE, "GATE_LIVE")])
@pytest.mark.parametrize("failure,target,equity,entry,limit,expected_reason", [
    ("RR", 110, 1000000, 100, 100, "AI_NET_REWARD_RISK_TOO_LOW"),
    ("LOWERED_PA_RR", 110, 1000000, 100, 100, "AI_NET_REWARD_RISK_TOO_LOW"),
    ("RISK", 113, 10000, 100, 100, "STOP_RISK_LIMIT_EXCEEDED"),
    ("MISSING_ENTRY", 113, 1000000, None, 100, "AI_ENTRY_PRICE_REQUIRED"),
    ("CONFLICTING_ENTRY", 113, 1000000, 100, 100.1, "AI_LIMIT_ENTRY_PRICE_CONFLICT"),
])
def test_gate_ai_rejects_invalid_economics_before_gateway(mode, account_type, failure, target, equity, entry, limit, expected_reason):
    now = datetime.now(timezone.utc)
    template = next(item for item in TEMPLATES if item["id"] == "price_action_structure")
    execution = {
        **template["execution_defaults"], "sizing_mode": "FIXED_NOTIONAL",
        "fixed_notional_usdt": 2000, "max_notional_usdt": 2500,
        "leverage_mode": "VENUE_LIMIT",
    }
    if failure == "LOWERED_PA_RR":
        execution["min_net_rr"] = 1.5
    model_id = DEFAULT_SMART_MODEL
    context = AICycleContext(
        cycle_id=f"risk-check-{failure}-{mode.value}", account_id="managed-gate", generation=1,
        started_at=now.isoformat(), expires_at=(now + timedelta(minutes=4)).isoformat(),
        allowed_instruments=("BTCUSDT",), decision_contract=CONTRACT,
        mode=mode, venue="gate", environment=mode.value,
        model_id=model_id, model_version=model_id, model_call_attempted=True, model_call_completed=True,
        model_inference_settings={"actual_model_id": model_id, "verified_manifest_model_id": model_id,
                                 "model_identity_source": "completion_response"},
        market_snapshots={"BTCUSDT": {
            "price": 100, "slippage": 0.001, "fee_rate": 0.00075,
            "data_as_of": now.isoformat(),
            "market": {"contractSize": 1, "taker": 0.00075, "leverage_max": 20,
                       "precision": {"amount": 1, "price": 0.1},
                       "limits": {"amount": {"min": 1, "max": 10000}, "price": {"step": 0.1}}},
        }},
        account_truth={"status": "AVAILABLE", "equity": str(equity), "available_margin": str(equity),
                       "used_margin": "0", "positions": [], "pending_orders": []},
        strategy_instructions={"template_id": template["id"], "revision": 1, "execution": execution},
    )
    proposal = AIActionOutput(
        action="OPEN_LONG", instrument_id="BTCUSDT", reason="fixture proposal",
        entry_price=entry, limit_price=limit, stop_price=95, take_profit=target,
        position_size_usdt=2000, requested_leverage=5, order_preference="LIMIT",
        evidence_refs=("market_snapshot:BTCUSDT:fixture",), extra_fields={"confidence": 80},
    )
    submitted = []

    class Gateway:
        def submit_intent(self, intent, **kwargs):
            submitted.append(intent)
            return {"status": "ACKNOWLEDGED"}

    engine = object.__new__(AILedDecisionEngine)
    engine.gateway = Gateway()
    engine.ledger = SimpleNamespace(get_open_positions=lambda *args, **kwargs: [])
    engine.risk_engine = SimpleNamespace(evaluate_intent=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy risk engine called")))
    engine.agent_policy_id = "fixture-policy"
    engine._persist_cycle = lambda *args, **kwargs: None
    engine._live_execution_quote = lambda *args, **kwargs: None

    result = engine.execute_cycle(context, now=now, model_output=proposal)
    assert result.status in {"REJECTED", "BLOCKED"}
    assert expected_reason in result.reason
    assert submitted == []
    if failure == "RISK":
        # Stop-risk failure must reject the unchanged 2,000-USDT proposal;
        # it must not be silently resized to manufacture an approval.
        assert proposal.position_size_usdt == 2000
        assert "notional_adjustment" not in proposal.extra_fields


def test_gate_live_model_open_uses_scoped_margin_only_gateway_without_order_side_effects():
    now = datetime.now(timezone.utc)
    model_file = DEFAULT_SMART_MODEL
    template = next(item for item in TEMPLATES if item["id"] == "price_action_structure")
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
        account_truth={"status": "AVAILABLE", "equity": "100000", "available_margin": "50000", "used_margin": "0", "positions": []},
        strategy_instructions={"template_id": template["id"], "revision": 2,
                               "execution": template["execution_defaults"]},
    )
    proposal = AIActionOutput(
        action="OPEN_LONG", instrument_id="BTCUSDT", reason="已收盘突破与量能支持",
        entry_price=99.9, stop_price=95, take_profit=112,
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
        amount_risk=Decimal("1"), risk_per_trade_pct=Decimal("0.15"), min_net_rr=Decimal("2.0"),
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
        amount_risk=Decimal("1"), risk_per_trade_pct=Decimal("0.15"), min_net_rr=Decimal("2.0"),
        instrument_id="BTCUSDT", expires_at=expiry, now=now,
    )
    assert not ledger.reserve_margin_only(
        "gate_testnet", "second", Decimal("20"), max_margin_pct=Decimal("18"),
        amount_risk=Decimal("1"), risk_per_trade_pct=Decimal("0.15"), min_net_rr=Decimal("2.0"),
        instrument_id="ETHUSDT", expires_at=expiry, now=now,
    )


def test_margin_only_ledger_rejects_a_lowered_persisted_pa_rr_policy(tmp_path):
    store = SQLiteStore(tmp_path / "margin-only-pa-rr-floor.sqlite3")
    store.initialize()
    ledger = AccountLedger(store)
    ledger.create_account("gate_testnet", "PAPER", config={
        "account_type": "GATE_TESTNET", "provider": "gate", "execution_mode": "TESTNET",
    })
    book = AIStrategyBook(store)
    active = book.active("gate_testnet")
    saved = book.save("gate_testnet", name=active["name"], sections=active["sections"],
                      expected_revision=active["revision"], execution=active["execution"])
    assert saved["execution"]["min_net_rr"] == 2.0
    with store._connect() as db:
        row = db.execute(
            "SELECT revision,execution_json FROM ai_strategy_instructions WHERE account_id=? ORDER BY revision DESC LIMIT 1",
            ("gate_testnet",),
        ).fetchone()
        config = json.loads(row["execution_json"])
        config["min_net_rr"] = 1.5
        db.execute("UPDATE ai_strategy_instructions SET execution_json=? WHERE account_id=? AND revision=?",
                   (json.dumps(config), "gate_testnet", row["revision"]))
    GateAccountTruthService(store).refresh(
        "gate_testnet", SimpleNamespace(get_account_truth=lambda **_: {
            "status": "AVAILABLE", "api_environment": "TESTNET", "equity": "100000",
            "available_margin": "90000", "used_margin": "10000",
            "positions_status": "AVAILABLE", "pending_orders_status": "AVAILABLE",
            "positions": [], "pending_orders": [],
        }),
    )
    now = datetime.now(timezone.utc)
    assert not ledger.reserve_margin_only(
        "gate_testnet", "lowered-pa-rr", Decimal("70"),
        amount_risk=Decimal("1"), risk_per_trade_pct=Decimal("0.15"), min_net_rr=Decimal("1.5"),
        max_margin_pct=Decimal(str(saved["execution"]["max_margin_pct"])),
        instrument_id="BTCUSDT", expires_at=now + timedelta(minutes=10), now=now,
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
        amount_risk=Decimal("1"), risk_per_trade_pct=Decimal("0.15"), min_net_rr=Decimal("2.0"),
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
                  amount_risk=Decimal("1"), risk_per_trade_pct=Decimal("0.15"), min_net_rr=Decimal("2.0"),
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
        "order_id": "1000", "filled": 2, "average": 100,
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
            return {"order_id": str(order_id), "symbol": symbol, "status": status, "trigger_price": price,
                    "reduce_only": True,
                    "initial": {"contract": "BTC_USDT", "size": -2, "reduce_only": True, "text": "t-test"},
                    "finish_as": "cancelled" if status == "FINISHED" else None, "observed_at": now}

        def place_protection_orders(self, symbol, **kwargs):
            self.events.append("create")
            self.orders["13"] = ["OPEN", kwargs["take_profit"]]
            return [{"leg": "take_profit", "order_id": "13"}]

        def normalize_protection_price(self, symbol, price):
            assert symbol == "BTCUSDT"
            return float((Decimal(str(price)) / Decimal("0.1")).to_integral_value(rounding="ROUND_DOWN") * Decimal("0.1"))

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
                                       position_id="7", new_take_profit=89.04)
    with store._connect() as db:
        pending = json.loads(db.execute("SELECT execution_result_json FROM order_intents WHERE intent_id='intent_ai_filled'").fetchone()[0])
    assert pending["protection_replacements"]["take_profit"]["new_id"] == "13"
    assert pending["protection_replacements"]["take_profit"]["target_price"] == 89.0
    assert fake.orders["12"][0] == "OPEN" and fake.orders["13"][0] == "OPEN"
    result = gateway.update_gate_protection(account_id="gate_testnet", instrument_id="BTCUSDT",
                                            position_id="7", new_take_profit=89.04)
    assert result["status"] == "VERIFIED"
    assert fake.events == ["create", "cancel", "cancel"]
    assert fake.orders["12"][0] == "FINISHED" and fake.orders["13"][0] == "OPEN"
    assert fake.orders["13"][1] == 89.0
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
    receipt = {"order_id": "123456", "protection_orders": [{"leg": "take_profit", "order_id": "11", "status": "open"}]}
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
    fetched = []

    class FakeGate:
        def fetch_protection_order(self, order_id, symbol):
            assert (order_id, symbol) == ("11", "BTCUSDT")
            fetched.append(order_id)
            status = "FINISHED" if (order_id, symbol) in canceled else "OPEN"
            return {"order_id": order_id, "symbol": symbol, "status": status,
                    "finish_as": "cancelled" if status == "FINISHED" else None,
                    "reduce_only": True,
                    "initial": {"contract": "BTC_USDT", "size": -2, "reduce_only": True, "text": "t-test"}}

        def cancel_protection_order(self, order_id, symbol):
            canceled.append((order_id, symbol))
            return {"status": "CANCELED", "order_id": order_id,
                    "after": {"order_id": order_id, "symbol": symbol, "status": "FINISHED",
                              "finish_as": "cancelled", "reduce_only": True,
                              "initial": {"contract": "BTC_USDT", "size": -2, "reduce_only": True, "text": "t-test"}}}

    monkeypatch.setattr(gate_accounts, "build_gate_trader", lambda *_: FakeGate())
    truth = {"status": "AVAILABLE", "positions_status": "AVAILABLE", "pending_orders_status": "AVAILABLE",
             "positions": [], "pending_orders": [
                 # Gate ordinary pending orders omit native price triggers.
                 {"order_id": "22", "symbol": "ETHUSDT", "reduce_only": True},
             ]}
    assert gateway.cleanup_closed_gate_protection(account_id="gate_testnet", remote_truth={**truth, "positions": [{"symbol": "BTCUSDT"}]}) == []
    cleaned = gateway.cleanup_closed_gate_protection(account_id="gate_testnet", remote_truth=truth)
    assert cleaned == [{"intent_id": "intent_ai_closed", "symbol": "BTCUSDT", "order_id": "11", "leg": "take_profit", "status": "canceled_after_position_close"}]
    assert fetched == ["11"]
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
             "limit", 2, "hash", "FILLED", json.dumps({"order_id": "88", "protection_status": "ACTIVE", "protection_orders": [
                 {"leg": "stop_loss", "order_id": "11", "status": "open"},
                 {"leg": "take_profit", "order_id": "12", "status": "open"},
             ]}), now, now, "gate", 0),
        )

    class FakeGate:
        def fetch_protection_order(self, order_id, symbol):
            assert symbol == "BTCUSDT"
            native_initial = {"contract": "BTC_USDT", "size": -2, "is_reduce_only": True, "text": "t-test"}
            return {"order_id": order_id, "symbol": symbol, "status": "FINISHED", "finish_as": "succeeded" if order_id == "11" else "cancelled", "reduce_only": True,
                    "initial": native_initial,
                    "trade_id": "99" if order_id == "11" else None, "triggered_order_id": "99" if order_id == "11" else None,
                    "me_order_id": "0", "raw": {"initial": native_initial}}

        def cancel_protection_order(self, *_args):
            raise AssertionError("Gate already ended both orders")

    monkeypatch.setattr(gate_accounts, "build_gate_trader", lambda *_: FakeGate())
    truth = {"status": "AVAILABLE", "positions_status": "AVAILABLE", "pending_orders_status": "AVAILABLE",
             "positions": [], "pending_orders": []}
    result = gateway.cleanup_closed_gate_protection(account_id="gate_testnet", remote_truth=truth)
    assert {item["status"] for item in result} == {"finished_succeeded", "finished_cancelled"}
    with store._connect() as db:
        receipt = json.loads(db.execute("SELECT execution_result_json FROM order_intents WHERE intent_id='intent_ai_terminal'").fetchone()[0])
    assert receipt["protection_status"] == "CLOSED_POSITION_RECONCILED"
    assert receipt["protection_terminal_observations"][0]["triggered_order_id"] == "99"
    assert receipt["protection_terminal_observations"][0]["native_readback"]["initial"]["is_reduce_only"] is True
    assert receipt["protection_terminal_observations"][0]["native_readback"]["me_order_id"] == "0"


def test_terminal_observation_rejects_conflicting_native_reduce_only_aliases(tmp_path):
    store = SQLiteStore(tmp_path / "terminal-protection-conflict.sqlite3")
    store.initialize()
    gateway = ExecutionGateway(store)
    receipt = {
        "order_id": "88",
        "protection_orders": [{"leg": "stop_loss", "order_id": "11"}],
    }
    native_initial = {
        "contract": "BTC_USDT", "size": -2,
        "reduce_only": False, "is_reduce_only": True,
    }
    observed = {
        "order_id": "11", "symbol": "BTCUSDT", "status": "FINISHED",
        "finish_as": "cancelled", "reduce_only": True,
        "initial": native_initial, "raw": {"initial": native_initial},
    }

    recorded = gateway._append_gate_protection_terminal_observation(
        receipt, leg="stop_loss", account_id="gate_testnet", environment="TESTNET",
        symbol="BTCUSDT", protection_order_id="11", observed=observed,
    )

    assert recorded is False
    assert "protection_terminal_observations" not in receipt


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
        protection_plan=ProtectionPlan(stop_price=95, take_profit=112), risk_policy="MARGIN_ONLY",
    )
    decision = gateway._reserve_margin_only_ai_intent(intent, {
        "price": 100, "slippage": 0.001,
        "market": {"contractSize": 1, "taker": 0.0005, "leverage_max": 20,
                   "precision": {"amount": 1, "price": 0.1},
                   "limits": {"price": {"step": 0.1}}},
    }, datetime.now(timezone.utc))
    assert decision.approved
    assert decision.leverage == Decimal("5")
    assert decision.notional == Decimal("1000")
    assert decision.allocated_margin == Decimal("201.0000")
    assert reservations[0][1]["max_margin_pct"] == Decimal("12.0")


def test_gateway_margin_reservation_uses_observed_fee_when_gate_market_taker_is_null(tmp_path):
    store = SQLiteStore(tmp_path / "gateway-observed-fee.sqlite3")
    store.initialize()
    book = AIStrategyBook(store)
    current = book.active("gate_testnet")
    book.save("gate_testnet", name=current["name"], sections=current["sections"],
              expected_revision=current["revision"], execution=current["execution"])
    gateway = object.__new__(ExecutionGateway)
    gateway.store = store
    gateway.ledger = SimpleNamespace(reserve_margin_only=lambda *args, **kwargs: True)
    intent = OrderIntent(
        intent_id="observed-fee-open", idempotency_key="observed-fee-open", account_id="gate_testnet",
        mode=TradingMode.TESTNET, venue="gate", instrument_id="ETHUSDT", side="SHORT",
        order_type="limit", quantity=10, price=100, leverage=5,
        protection_plan=ProtectionPlan(stop_price=105, take_profit=88), risk_policy="MARGIN_ONLY",
    )
    decision = gateway._reserve_margin_only_ai_intent(intent, {
        "price": 100, "slippage": 0.001, "fee_rate": 0.0005,
        "market": {"contractSize": 1, "taker": None, "leverage_max": 20,
                   "precision": {"amount": 1, "price": 0.1},
                   "limits": {"price": {"step": 0.1}}},
    }, datetime.now(timezone.utc))
    assert decision.approved
    assert decision.allocated_margin == Decimal("201.0000")


def test_gateway_rejects_low_net_rr_before_atomic_reservation(tmp_path):
    store = SQLiteStore(tmp_path / "gateway-low-rr.sqlite3")
    store.initialize()
    book = AIStrategyBook(store)
    current = book.active("gate_testnet")
    book.save("gate_testnet", name=current["name"], sections=current["sections"],
              expected_revision=current["revision"], execution=current["execution"])
    # Simulate an old or directly edited persisted row that tries to lower the
    # PA strategy floor; the gateway must still apply the registered 2.0.
    with store._connect() as db:
        row = db.execute(
            "SELECT revision,execution_json FROM ai_strategy_instructions WHERE account_id=? ORDER BY revision DESC LIMIT 1",
            ("gate_testnet",),
        ).fetchone()
        execution = json.loads(row["execution_json"])
        execution["min_net_rr"] = 1.5
        db.execute("UPDATE ai_strategy_instructions SET execution_json=? WHERE account_id=? AND revision=?",
                   (json.dumps(execution), "gate_testnet", row["revision"]))
    gateway = object.__new__(ExecutionGateway)
    gateway.store = store
    gateway.ledger = SimpleNamespace(
        reserve_margin_only=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("low RR reached ledger")))
    intent = OrderIntent(
        intent_id="gateway-low-rr", idempotency_key="gateway-low-rr", account_id="gate_testnet",
        mode=TradingMode.TESTNET, venue="gate", instrument_id="BTCUSDT", side="LONG",
        order_type="limit", quantity=20, price=100, leverage=5,
        protection_plan=ProtectionPlan(stop_price=95, take_profit=110), risk_policy="MARGIN_ONLY",
    )
    market = {
        "price": 100, "slippage": 0.001,
        "market": {"contractSize": 1, "taker": 0.00075, "leverage_max": 20,
                   "precision": {"amount": 1, "price": 0.1},
                   "limits": {"price": {"step": 0.1}}},
    }
    with pytest.raises(GatewayError) as caught:
        gateway._reserve_margin_only_ai_intent(intent, market, datetime.now(timezone.utc))
    assert caught.value.code == "AI_NET_REWARD_RISK_TOO_LOW"


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
    template = next(item for item in TEMPLATES if item["id"] == "price_action_structure")
    book.save(account_id, name=template["name"], sections=template["sections"],
              expected_revision=0, template_id=template["id"], execution=template["execution_defaults"])

    class FakeGate:
        def __init__(self):
            self.orders = []

        def get_account_truth(self, **kwargs):
            return {
                "status": "AVAILABLE", "api_environment": mode.value, "equity": "100000",
                "available_margin": "100000", "used_margin": "0",
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
        price=100, leverage=5, protection_plan=ProtectionPlan(stop_price=95, take_profit=112),
        risk_policy="MARGIN_ONLY", control_mode=ControlMode.AUTONOMOUS,
        decision_path=DecisionPath.AI_LED,
    )
    market = {
        "symbol": "BTCUSDT", "price": 100, "fresh": True,
        "data_as_of": now.isoformat(), "received_at": now.isoformat(),
        "slippage": 0.001,
        "fee_rate": 0.0005,
        "market": {
            "contractSize": 1, "taker": None if mode == TradingMode.LIVE else 0.0005, "leverage_max": 20,
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
        reservation = db.execute(
            "SELECT amount_risk,amount_margin FROM risk_reservations WHERE account_id=? AND status='PENDING'",
            (account_id,),
        ).fetchone()
    assert "MARGIN_AND_STOP_RISK_APPROVED" in row["risk_decision_json"]
    assert Decimal(reservation["amount_risk"]) > 0
    assert Decimal(reservation["amount_risk"]) <= Decimal("150")  # 0.15% of the fresh 100,000-USDT equity


@pytest.mark.parametrize("account_id,mode,account_type", [
    ("gate_testnet", TradingMode.TESTNET, "GATE_TESTNET"),
    ("gate_live", TradingMode.LIVE, "GATE_LIVE"),
])
def test_gate_gateway_atomic_stop_risk_cap_blocks_testnet_and_live_before_order(
    tmp_path, monkeypatch, account_id, mode, account_type,
):
    import core.trading.gate_accounts as gate_accounts

    store = SQLiteStore(tmp_path / f"atomic-risk-{mode.value.lower()}.sqlite3")
    store.initialize()
    ledger = AccountLedger(store)
    ledger.create_account(account_id, mode.value, config={
        "account_type": account_type, "provider": "gate", "execution_mode": mode.value,
    })
    template = next(item for item in TEMPLATES if item["id"] == "price_action_structure")
    book = AIStrategyBook(store)
    book.save(account_id, name=template["name"], sections=template["sections"], expected_revision=0,
              template_id=template["id"], execution={**template["execution_defaults"], "max_margin_pct": 80})

    class FakeGate:
        def __init__(self):
            self.orders = []

        def get_account_truth(self, **_kwargs):
            return {
                "status": "AVAILABLE", "api_environment": mode.value, "equity": "1000",
                "available_margin": "1000", "used_margin": "0",
                "positions_status": "AVAILABLE", "pending_orders_status": "AVAILABLE",
                "positions": [], "pending_orders": [],
            }

        def place_order(self, **kwargs):
            self.orders.append(kwargs)
            return {"status": "open", "order_id": "must-not-be-used", "amount": kwargs["amount"], "filled": 0}

    fake = FakeGate()
    monkeypatch.setattr(gate_accounts, "is_managed_gate_account", lambda *_args: True)
    monkeypatch.setattr(gate_accounts, "build_gate_trader", lambda *_args: fake)
    gateway = ExecutionGateway(store, ledger=ledger)
    now = datetime.now(timezone.utc)
    intent = OrderIntent(
        intent_id=f"atomic-risk-{mode.value.lower()}", idempotency_key=f"atomic-risk-{mode.value.lower()}",
        account_id=account_id, mode=mode, venue="gate", environment=mode.value,
        instrument_id="BTCUSDT", side="LONG", order_type="limit", quantity=20,
        price=100, leverage=5, protection_plan=ProtectionPlan(stop_price=95, take_profit=113),
        risk_policy="MARGIN_ONLY", control_mode=ControlMode.AUTONOMOUS,
        decision_path=DecisionPath.AI_LED,
    )
    market = {
        "symbol": "BTCUSDT", "price": 100, "fresh": True,
        "data_as_of": now.isoformat(), "received_at": now.isoformat(),
        "slippage": 0.001, "fee_rate": 0.0005,
        "market": {
            "contractSize": 1, "taker": 0.0005, "leverage_max": 20,
            "precision": {"amount": 1, "price": 0.1},
            "limits": {"amount": {"min": 1, "max": 1000}, "price": {"step": 0.1}},
        },
    }
    with pytest.raises(GatewayError) as caught:
        gateway.submit_intent(intent, market_snapshot=market, now=now)
    assert caught.value.code == "MARGIN_OR_STOP_RISK_CAP_BLOCKED"
    assert fake.orders == []
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM risk_reservations").fetchone()[0] == 0


def test_remote_market_open_without_order_book_slippage_is_explicitly_blocked():
    market = {
        "price": 100, "slippage": None, "fee_rate": 0.0005,
        "market": {"contractSize": 1, "leverage_max": 20,
                   "precision": {"amount": 1}, "limits": {"amount": {"min": 1, "max": 100}}},
    }
    ExecutionGateway._validate_remote_market_economics(market, order_type="limit")
    with pytest.raises(GatewayError, match="Market opening requires observed order-book slippage"):
        ExecutionGateway._validate_remote_market_economics(market, order_type="market", strict_slippage=True)
