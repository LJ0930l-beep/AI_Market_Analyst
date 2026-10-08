import hashlib
import json
from datetime import datetime, timezone

import pytest

from core.analysis.ai_strategy_validation import frozen_strategy_manifest, validate_ai_strategy_evidence
from core.storage import SQLiteStore
from core.trading.ai_strategy_book import TEMPLATES
from core.trading.ai_led_engine import AICycleContext, AICycleResult, AIActionOutput, AILedDecisionEngine
from core.trading.execution_gateway import TradingMode
from core.trading.gate_exit_attribution import strategy_config_sha256
from core.trading.gate_trade_settlement import GateTradeSettlementService
from core.trading.ledger import AccountLedger
from core.trading.strategy_execution import normalize_execution


ALIAS = "Bonsai-2-27B-PTQ1_0"
ARTIFACT = "Ternary-Bonsai-2-27B-PTQ1_0.gguf"
PROMPT = "ai-market-analyst-test-prompt-v1"
REQUEST_HASH = "a" * 64


def _store(path):
    store = SQLiteStore(path)
    store.initialize()
    with store._connect() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS ai_led_cycles (
                cycle_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, action TEXT NOT NULL,
                status TEXT NOT NULL, reason TEXT NOT NULL, latency_ms REAL,
                order_intent_id TEXT, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
                model_call_attempted INTEGER, model_call_completed INTEGER,
                strategy_template_id TEXT, strategy_revision INTEGER, strategy_config_sha256 TEXT
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS order_intents (
                intent_id TEXT PRIMARY KEY, idempotency_key TEXT, account_id TEXT NOT NULL,
                mode TEXT, instrument_id TEXT, side TEXT, order_type TEXT, quantity REAL,
                price REAL, payload_hash TEXT, status TEXT, execution_result_json TEXT,
                created_at TEXT, updated_at TEXT, venue TEXT, environment TEXT,
                reduce_only INTEGER, strategy_id TEXT, strategy_version TEXT,
                decision_path TEXT, control_mode TEXT, cycle_id TEXT
            )"""
        )
        GateTradeSettlementService(store)._ensure(db)
    return store


def _strategy(template_id="aggressive_impulse", *, revision=1, custom_leverage=None):
    template = next(item for item in TEMPLATES if item["id"] == template_id)
    execution = normalize_execution({
        **template["execution_defaults"],
        "scan_interval_minutes": template["scan_interval_minutes"],
        "order_preference": template.get("order_preference", "AUTO"),
    })
    if custom_leverage is not None:
        execution["leverage"] = custom_leverage
    result = {
        "account_id": "gate_testnet",
        "revision": revision,
        "name": template["name"],
        "template_id": template_id,
        "style": template["style"],
        "profile": template["profile"],
        "sections": template["sections"],
        "nofx_runtime": None,
        "updated_at": None,
        "execution": execution,
    }
    result["digest"] = hashlib.sha256(json.dumps(
        result, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")).hexdigest()
    return result


def _model_payload(*, action, intent_id, strategy, strategy_version, reduce_only, account="gate_testnet", symbol="BTCUSDT"):
    digest = strategy_config_sha256(strategy)
    settings = {
        "actual_model_id": ARTIFACT,
        "model_identity_source": "completion_response",
        "verified_manifest_model_id": ARTIFACT,
        "local_schema_validation": "PASS",
        "model_response_audit": {
            "attempts": [{
                "status": "COMPLETED", "prompt_version": PROMPT,
                "request_hash": REQUEST_HASH,
                "raw_response": json.dumps({"action": action, "instrument_id": symbol}),
                "validation_error": None, "raw_response_truncated": False,
            }],
        },
    }
    return {
        "cycle_id": f"cycle-{intent_id}",
        "action": action,
        "instrument_id": symbol,
        "model_output": {"action": action, "instrument_id": symbol},
        "strategy_instructions": strategy,
        "strategy_template_id": strategy["template_id"],
        "strategy_revision": strategy["revision"],
        "strategy_config_sha256": digest,
        "account_id": account,
        "venue": "gate",
        "mode": "TESTNET",
        "environment": "testnet",
        "model": ALIAS,
        "model_id": ALIAS,
        "model_version": ARTIFACT,
        "prompt_version": PROMPT,
        "model_call_prompt_version": PROMPT,
        "input_hash": REQUEST_HASH,
        "model_receipt": settings,
        "model_inference_settings": settings,
        "decision_origin": "MODEL",
        "is_model_decision": True,
        "model_called": True,
        "model_call_attempted": True,
        "model_call_completed": True,
        "model_result": action,
        "order_intent": {
            "intent_id": intent_id,
            "strategy_version": strategy_version,
            "reduce_only": reduce_only,
        },
    }


def _add_cycle(db, *, action, intent_id, strategy, strategy_version, reduce_only,
               status="EXECUTED", account="gate_testnet", symbol="BTCUSDT",
               model_call_completed=1):
    payload = _model_payload(
        action=action, intent_id=intent_id, strategy=strategy,
        strategy_version=strategy_version, reduce_only=reduce_only,
        account=account, symbol=symbol,
    )
    cycle_id = f"cycle-{intent_id}"
    db.execute(
        """INSERT INTO ai_led_cycles
           (cycle_id,account_id,action,status,reason,latency_ms,order_intent_id,
            payload_json,created_at,model_call_attempted,model_call_completed,
            strategy_template_id,strategy_revision,strategy_config_sha256,
            completed_at,environment,provider,decision_origin,model_call_status,
            model_called,model_result)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (cycle_id, account, action, status, "", 1.0, intent_id,
         json.dumps(payload, ensure_ascii=False), "2026-09-01T00:00:00+00:00",
         1, model_call_completed, strategy["template_id"], strategy["revision"],
         strategy_config_sha256(strategy), "2026-09-01T00:00:01+00:00",
         "testnet", "gate", "MODEL", "MODEL_DECISION", 1, action),
    )
    return cycle_id, payload


def _add_intent(db, *, intent_id, cycle_id, strategy, strategy_version,
                side, order_id, reduce_only, status="FILLED", decision_path="AI_LED",
                control_mode="AUTONOMOUS", account="gate_testnet", environment="testnet",
                symbol="BTC_USDT", strategy_id="ai_led", receipt_extra=None):
    receipt = {"intent_id": intent_id, "order_id": order_id, "filled_quantity": 1}
    receipt.update(receipt_extra or {})
    db.execute(
        """INSERT INTO order_intents
           (intent_id,idempotency_key,account_id,mode,instrument_id,side,order_type,
            quantity,price,payload_hash,status,execution_result_json,created_at,
            updated_at,venue,environment,reduce_only,strategy_id,strategy_version,
            decision_path,control_mode,cycle_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (intent_id, f"key-{intent_id}", account,
         "TESTNET" if environment == "testnet" else "LIVE", symbol,
         side, "market", 1.0, 100.0, "hash", status, json.dumps(receipt),
         "2026-09-01T00:00:00+00:00", "2026-09-01T00:00:01+00:00",
         "gate", environment, int(reduce_only), strategy_id, strategy_version,
         decision_path, control_mode, cycle_id),
    )
    return receipt


def _native_trade(db, *, trade_id, order_id, side, account="gate_testnet",
                  environment="testnet", symbol="BTCUSDT", venue="gate"):
    db.execute(
        """INSERT INTO gate_remote_trade_evidence
           (evidence_id,account_id,environment,venue,trade_id,contract,canonical_symbol,
            order_id,event_at_ms,side,quantity,price,fee_amount,fee_currency,fee_source,
            point_fee,raw_hash,raw_json,first_seen_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"ev-{account}-{trade_id}", account, environment, venue, trade_id,
         "BTC_USDT", symbol, order_id, 1_800_000_000_000, side, "1", "100",
         "-0.5", "USDT", "GATE_NATIVE_MY_TRADE", "0", hashlib.sha256(trade_id.encode()).hexdigest(),
         "{}", "2026-10-01T00:00:00+00:00"),
    )


def _native_order(db, order_id, *, size="-1", contract="BTC_USDT", reduce_only=True,
                  status="finished", left="0", account="gate_testnet", environment="testnet"):
    raw = {
        "id": str(order_id), "contract": contract, "size": size,
        "is_reduce_only": reduce_only, "status": status, "left": left,
    }
    db.execute(
        """INSERT INTO gate_remote_order_evidence
           (evidence_id,account_id,environment,order_id,contract,canonical_symbol,
            is_reduce_only,is_close,observed_at,raw_hash,raw_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (f"order-ev-{account}-{order_id}", account, environment, str(order_id),
         contract, "BTCUSDT", int(bool(reduce_only)), 0,
         "2026-10-01T00:00:00+00:00", hashlib.sha256(str(order_id).encode()).hexdigest(),
         json.dumps(raw)),
    )


def _add_attribution(db, *, trade_id, episode_id, role, basis, account="gate_testnet", environment="testnet"):
    db.execute(
        """INSERT INTO gate_trade_episode_attributions
           (attribution_id,account_id,environment,trade_id,episode_id,economic_role,
            attribution_status,basis,evidence_json,observed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (f"attr-{account}-{trade_id}-{role}", account, environment, trade_id,
         episode_id, role, "VERIFIED", basis, "{}", "2026-10-01T00:00:00+00:00"),
    )


def _seed_episode(store, *, number=1, exit_actor="AI_REDUCE", strategy=None,
                  close_count=1, entry_status="CANCELED", model_call_completed=1,
                  account="gate_testnet", environment="testnet", symbol="BTC_USDT",
                  duplicate_settlement=False, close_actor_overrides=None):
    strategy = strategy or _strategy()
    episode_id = f"episode-{number}"
    entry_order_id = f"{1000 + number}"
    entry_intent_id = f"intent-entry-{number}"
    entry_cycle_id = f"cycle-{entry_intent_id}"
    strategy_version = str(strategy["revision"])
    entry_trade_id = f"entry-trade-{number}"
    exit_trade_ids = [f"exit-trade-{number}-{idx}" for idx in range(close_count)]
    exit_order_ids = [f"{2000 + number * 10 + idx}" for idx in range(close_count)]
    actors = close_actor_overrides or [exit_actor] * close_count
    with store._connect() as db:
        _add_cycle(db, action="OPEN_LONG", intent_id=entry_intent_id,
                   strategy=strategy, strategy_version=strategy_version,
                   reduce_only=False, status="SUBMITTED", account=account,
                   symbol=symbol.replace("_", ""), model_call_completed=model_call_completed)
        entry_receipt_extra = {}
        if exit_actor == "PROTECTION":
            parent_id = str(9000 + number)
            child_id = exit_order_ids[0]
            entry_receipt_extra = {
                "protection_orders": [{"leg": "stop_loss", "order_id": parent_id}],
                "protection_terminal_observations": [{
                    "leg": "stop_loss", "protection_order_id": parent_id,
                    "parent_order_id": entry_order_id,
                    "observed_at": "2026-10-01T00:00:00+00:00",
                    "account_id": account, "environment": environment,
                    "symbol": symbol.replace("_", ""),
                    "native_readback": {
                        "status": "FINISHED", "finish_as": "succeeded",
                        "trade_id": child_id, "me_order_id": entry_order_id,
                        "triggered_order_id": child_id,
                        "raw": {
                            "id": parent_id, "id_string": parent_id,
                            "status": "FINISHED", "finish_as": "succeeded",
                            "trade_id": child_id, "me_order_id": entry_order_id,
                            "initial": {
                                "contract": symbol, "size": "-1",
                                "reduce_only": True, "text": "t-ai-sl",
                            },
                        },
                    },
                }],
            }
        _add_intent(
            db, intent_id=entry_intent_id, cycle_id=entry_cycle_id,
            strategy=strategy, strategy_version=strategy_version, side="LONG",
            order_id=entry_order_id, reduce_only=False, status=entry_status,
            account=account, environment=environment, symbol=symbol,
            strategy_id="ai_led", receipt_extra=entry_receipt_extra,
        )
        db.execute(
            """INSERT INTO gate_accounting_episodes
               (episode_id,account_id,environment,contract,canonical_symbol,
                entry_order_id,entry_intent_id,cycle_id,side,strategy_id,strategy_version,
                identity_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (episode_id, account, environment, symbol, symbol.replace("_", ""),
             entry_order_id, entry_intent_id, entry_cycle_id, "BUY", "ai_led",
             strategy_version, json.dumps({"cycle_id": entry_cycle_id}),
             "2026-09-01T00:00:00+00:00"),
        )
        _native_trade(db, trade_id=entry_trade_id, order_id=entry_order_id,
                      side="BUY", account=account, environment=environment,
                      symbol=symbol.replace("_", ""))
        _add_attribution(db, trade_id=entry_trade_id, episode_id=episode_id,
                         role="ENTRY", basis="EXACT_ENTRY_ORDER_ID",
                         account=account, environment=environment)
        total = "9.5"
        position_close_id = f"position-close-{number}"
        db.execute(
            """INSERT INTO gate_position_close_evidence
               (evidence_id,account_id,environment,contract,canonical_symbol,
                side,closed_at_ms,accum_size,pnl,pnl_pnl,pnl_fee,pnl_fund,pnl_dividend,
                raw_hash,raw_json,first_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (position_close_id, account, environment, symbol, symbol.replace("_", ""),
             "LONG", 1_800_000_000_000 + number, "1", total, "10", "-1", "0.5", "0",
             hashlib.sha256(position_close_id.encode()).hexdigest(), "{}",
             "2026-10-01T00:00:00+00:00"),
        )
        for idx, (trade_id, order_id, actor) in enumerate(zip(exit_trade_ids, exit_order_ids, actors)):
            _native_trade(db, trade_id=trade_id, order_id=order_id, side="SELL",
                          account=account, environment=environment,
                          symbol=symbol.replace("_", ""))
            basis = "SYSTEM_REDUCE_ONLY_ORDER_ID" if actor == "AI_REDUCE" else "EXCLUSIVE_FLAT_BASELINE_NATIVE_CLOSE_SIZE_POSITION_CLOSE"
            _add_attribution(db, trade_id=trade_id, episode_id=episode_id,
                             role="CLOSE", basis=basis,
                             account=account, environment=environment)
            if actor == "AI_REDUCE":
                close_intent_id = f"intent-close-{number}-{idx}"
                close_cycle_id, _ = _add_cycle(
                    db, action="CLOSE_POSITION", intent_id=close_intent_id,
                    strategy=strategy, strategy_version=strategy_version,
                    reduce_only=True, account=account,
                    symbol=symbol.replace("_", ""),
                )
                _add_intent(
                    db, intent_id=close_intent_id, cycle_id=close_cycle_id,
                    strategy=strategy, strategy_version=strategy_version, side="SELL",
                    order_id=order_id, reduce_only=True, account=account,
                    environment=environment, symbol=symbol,
                )
                _native_order(db, order_id, account=account, environment=environment)
            elif actor == "PROTECTION":
                _native_order(db, order_id, account=account, environment=environment)
        settlement = {
            "status": "SETTLED_FULL_COST", "account_id": account,
            "environment": environment.upper(), "accounting_episode_id": episode_id,
            "contract": symbol, "side": "LONG", "entry_order_id": entry_order_id,
            "entry_intent_id": entry_intent_id, "entry_trade_ids": [entry_trade_id],
            "exit_trade_ids": exit_trade_ids,
            "position_close_evidence_id": position_close_id,
            "settlement_currency": "USDT", "contract_multiplier": "1",
            "gross_realized": "10", "fee_effect": "-1", "funding_effect": "0.5",
            "dividend_effect": "0", "total_pnl": total,
            "fee_status": "VERIFIED_GATE_NATIVE_POSITION_CLOSE_AND_MY_TRADES",
            "funding_status": "VERIFIED_GATE_NATIVE_POSITION_CLOSE.pnl_fund",
            "trade_history_coverage": "COMPLETE",
            "position_close_history_coverage": "COMPLETE",
            "first_native_fill_at_ms": 1_800_000_000_000,
            "closed_at_ms": 1_800_000_000_000 + number,
        }
        settlement_json = json.dumps(settlement)
        db.execute(
            """INSERT INTO gate_episode_settlements
               (settlement_id,episode_id,status,settlement_currency,gross_realized,
                fee_effect,funding_effect,dividend_effect,total_pnl,fee_status,
                funding_status,pnl_source,settlement_json,settled_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"settlement-{number}", episode_id, "SETTLED_FULL_COST", "USDT",
             "10", "-1", "0.5", "0", total,
             "VERIFIED_GATE_NATIVE_POSITION_CLOSE_AND_MY_TRADES",
             "VERIFIED_GATE_NATIVE_POSITION_CLOSE.pnl_fund",
             "DECIMAL_TRADE_CASHFLOWS_RECONCILED_TO_GATE_POSITION_CLOSE",
             settlement_json, "2026-10-01T00:00:00+00:00"),
        )
        if duplicate_settlement:
            db.execute(
                """INSERT INTO gate_episode_settlements
                   (settlement_id,episode_id,status,settlement_currency,gross_realized,
                    fee_effect,funding_effect,dividend_effect,total_pnl,fee_status,
                    funding_status,pnl_source,settlement_json,settled_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"settlement-{number}-duplicate", episode_id, "SETTLED_FULL_COST", "USDT",
                 "10", "-1", "0.5", "0", total,
                 "VERIFIED_GATE_NATIVE_POSITION_CLOSE_AND_MY_TRADES",
                 "VERIFIED_GATE_NATIVE_POSITION_CLOSE.pnl_fund",
                 "DECIMAL_TRADE_CASHFLOWS_RECONCILED_TO_GATE_POSITION_CLOSE",
                 settlement_json, "2026-10-02T00:00:00+00:00"),
            )
    return episode_id


def test_manifest_is_frozen_and_empty_evidence_cannot_be_ranked(tmp_path):
    manifest = frozen_strategy_manifest()
    assert len(manifest["strategies"]) == 5
    assert len({item["sha256"] for item in manifest["strategies"]}) == 5
    assert [item["scan_interval_minutes"] for item in manifest["strategies"]] == [5, 15, 15, 15, 15]
    assert manifest["strategies"][-1]["id"] == "price_action_structure"
    assert manifest["strategies"][-1]["profile_version"] == "price_action_structure_v1"
    assert manifest == frozen_strategy_manifest()

    store = _store(tmp_path / "empty.sqlite3")
    report = validate_ai_strategy_evidence(store, "gate_testnet", environment="testnet")
    assert report["forward_comparison"]["status"] == "EVIDENCE_INSUFFICIENT"
    assert report["default_strategy_recommendation"] is None
    assert all(row["settled_trades"] == 0 for row in report["forward_comparison"]["results"])
    assert report["historical_replay"]["status"] == "NOT_RUN"


def test_cycle_persistence_stores_explicit_model_completion_and_strategy_config_digest(tmp_path):
    store = _store(tmp_path / "cycle-provenance.sqlite3")
    ledger = AccountLedger(store)
    engine = AILedDecisionEngine(
        store=store, execution_gateway=None, risk_engine=None,
        ledger=ledger, guardian=None,
    )
    strategy = _strategy(revision=6)
    context = AICycleContext(
        cycle_id="durable-provenance-cycle", account_id="gate_testnet",
        generation=1, started_at="2026-09-01T00:00:00+00:00",
        expires_at="2026-09-01T00:15:00+00:00", allowed_instruments=("BTCUSDT",),
        mode=TradingMode.TESTNET, venue="gate", environment="testnet",
        model_call_attempted=True, model_call_completed=True,
        strategy_instructions=strategy,
    )
    result = AICycleResult(
        cycle_id=context.cycle_id,
        action_output=AIActionOutput("OPEN_LONG", "BTCUSDT", "verified model action"),
        status="SUBMITTED", reason="accepted", decision_origin="MODEL",
    )
    engine._persist_cycle(result, context)

    with store._connect() as db:
        row = db.execute(
            "SELECT * FROM ai_led_cycles WHERE cycle_id=?", (context.cycle_id,)
        ).fetchone()
    assert row["model_call_attempted"] == 1
    assert row["model_call_completed"] == 1
    assert row["strategy_template_id"] == strategy["template_id"]
    assert row["strategy_revision"] == strategy["revision"]
    assert row["strategy_config_sha256"] == strategy_config_sha256(strategy)
    payload = json.loads(row["payload_json"])
    assert payload["model_call_completed"] is True
    assert payload["strategy_config_sha256"] == row["strategy_config_sha256"]


def test_ai_reduce_exit_qualifies_and_manual_close_stays_financial_only(tmp_path):
    store = _store(tmp_path / "manual-and-ai.sqlite3")
    _seed_episode(store, number=1, exit_actor="AI_REDUCE")
    _seed_episode(store, number=2, exit_actor="MANUAL")

    report = validate_ai_strategy_evidence(store, "gate_testnet", environment="testnet")
    aggressive = next(row for row in report["forward_comparison"]["results"]
                      if row["id"] == "aggressive_impulse")

    assert report["financial_settled"]["settled_trades"] == 2
    assert report["financial_settled"]["net_pnl_usdt"] == 19.0
    assert aggressive["settled_trades"] == 1
    assert aggressive["net_pnl_usdt"] == 9.5
    cohort = aggressive["config_cohorts"][0]
    assert cohort["financial_settled_trades"] == 2
    assert cohort["autonomous_qualified_trades"] == 1
    assert cohort["actor_types"] == {"AI_REDUCE": 1}
    assert cohort["actor_exclusions"]["FINANCIAL_CLOSE_SIZE_ONLY_ACTOR_UNPROVEN"] == 1
    assert report["default_strategy_recommendation"] is None


def test_succeeded_parent_child_protection_exit_qualifies(tmp_path):
    store = _store(tmp_path / "protection.sqlite3")
    _seed_episode(store, number=3, exit_actor="PROTECTION")
    report = validate_ai_strategy_evidence(store, "gate_testnet", "testnet")
    result = next(row for row in report["forward_comparison"]["results"]
                  if row["id"] == "aggressive_impulse")
    assert result["settled_trades"] == 1
    assert result["config_cohorts"][0]["actor_types"] == {"NATIVE_PROTECTION": 1}


def test_duplicate_episode_settlement_is_counted_once(tmp_path):
    store = _store(tmp_path / "dedupe.sqlite3")
    _seed_episode(store, number=4, duplicate_settlement=True)
    report = validate_ai_strategy_evidence(store, "gate_testnet", "testnet")
    assert report["financial_settled"]["settled_trades"] == 1
    assert report["financial_settled"]["net_pnl_usdt"] == 9.5


def test_custom_config_cohorts_are_visible_but_never_aggregated(tmp_path):
    store = _store(tmp_path / "cohorts.sqlite3")
    _seed_episode(store, number=5, strategy=_strategy(revision=2), exit_actor="AI_REDUCE")
    _seed_episode(store, number=6, strategy=_strategy(revision=3, custom_leverage=12), exit_actor="AI_REDUCE")
    report = validate_ai_strategy_evidence(store, "gate_testnet", "testnet")
    result = next(row for row in report["forward_comparison"]["results"]
                  if row["id"] == "aggressive_impulse")
    assert len(result["config_cohorts"]) == 2
    assert result["settled_trades"] is None
    assert result["net_pnl_usdt"] is None
    assert {row["qualification_status"] for row in result["config_cohorts"]} == {
        "ACTOR_QUALIFIED", "TEMPLATE_CONFIG_MISMATCH",
    }


@pytest.mark.parametrize("mutate,reason", [
    (lambda strategy, completed: (strategy, 0), "MODEL_CALL_NOT_COMPLETED"),
])
def test_missing_completed_model_call_cannot_enter_autonomous_count(tmp_path, mutate, reason):
    store = _store(tmp_path / "missing-model-call.sqlite3")
    strategy, completed = mutate(_strategy(), 1)
    _seed_episode(store, number=7, strategy=strategy, model_call_completed=completed)
    report = validate_ai_strategy_evidence(store, "gate_testnet", "testnet")
    result = next(row for row in report["forward_comparison"]["results"]
                  if row["id"] == "aggressive_impulse")
    assert report["financial_settled"]["settled_trades"] == 1
    assert result["settled_trades"] == 0
    assert report["exclusions"]["autonomous_qualification"][reason] == 1


@pytest.mark.parametrize("column,value,expected_reason", [
    ("account_id", "other-account", "NATIVE_EXIT_FILL_MISSING_OR_AMBIGUOUS"),
    ("environment", "live", "NATIVE_EXIT_FILL_MISSING_OR_AMBIGUOUS"),
    ("canonical_symbol", "ETHUSDT", "NATIVE_EXIT_FILL_SCOPE_OR_DIRECTION_MISMATCH"),
])
def test_foreign_account_environment_and_symbol_exit_fills_are_not_attributed(tmp_path, column, value, expected_reason):
    store = _store(tmp_path / f"foreign-evidence-{column}.sqlite3")
    _seed_episode(store, number=8, exit_actor="AI_REDUCE")
    with store._connect() as db:
        assert column in {"account_id", "environment", "canonical_symbol"}
        db.execute(f"UPDATE gate_remote_trade_evidence SET {column}=? WHERE trade_id='exit-trade-8-0'", (value,))
    report = validate_ai_strategy_evidence(store, "gate_testnet", "testnet")
    result = next(row for row in report["forward_comparison"]["results"]
                  if row["id"] == "aggressive_impulse")
    assert report["financial_settled"]["settled_trades"] == 0
    assert result["settled_trades"] == 0
    assert report["exclusions"]["settlement"][expected_reason] == 1


@pytest.mark.parametrize("association", ["zero", "absent"])
def test_synthetic_gate_native_shape_with_unassociated_parent_proves_child_actor(tmp_path, association):
    """A succeeded fixture derived from the observed Gate field shape, not a real trigger claim."""
    store = _store(tmp_path / f"protection-native-shape-{association}.sqlite3")
    _seed_episode(store, number=15, exit_actor="PROTECTION")
    parent_id = "2106070841958596608"
    with store._connect() as db:
        intent_id = "intent-entry-15"
        row = db.execute(
            "SELECT execution_result_json FROM order_intents WHERE intent_id=?", (intent_id,)
        ).fetchone()
        receipt = json.loads(row[0])
        receipt["protection_orders"][0]["order_id"] = parent_id
        observation = receipt["protection_terminal_observations"][0]
        observation["protection_order_id"] = parent_id
        observation["finish_time"] = "2026-10-01T00:00:01+00:00"
        child_id = "2150"
        raw = {
            "id": int(parent_id), "id_string": parent_id,
            "status": "FINISHED", "finish_as": "succeeded",
            "finish_time": "2026-10-01T00:00:01+00:00",
            "trade_id": int(child_id), "trade_id_string": child_id,
            "initial": {
                "contract": "BTC_USDT", "size": "-1",
                "is_reduce_only": True,
                # The observed source text is truncated and has no leg suffix.
                "text": "intent_ai_verify_protected_e",
            },
        }
        if association == "zero":
            raw.update({"me_order_id": 0, "me_order_id_string": "0"})
        native_readback = {
            "id": int(parent_id), "id_string": parent_id,
            "status": "FINISHED", "finish_as": "succeeded",
            "finish_time": raw["finish_time"],
            "trade_id": int(child_id), "trade_id_string": child_id,
            "triggered_order_id": child_id,
            "raw": raw,
        }
        if association == "zero":
            native_readback.update({"me_order_id": 0, "me_order_id_string": "0"})
            observation["triggered_order_id"] = child_id
        observation["native_readback"] = native_readback
        db.execute(
            "UPDATE order_intents SET execution_result_json=? WHERE intent_id=?",
            (json.dumps(receipt), intent_id),
        )

    report = validate_ai_strategy_evidence(store, "gate_testnet", "testnet")
    result = next(row for row in report["forward_comparison"]["results"]
                  if row["id"] == "aggressive_impulse")
    cohort = result["config_cohorts"][0]
    assert report["financial_settled"]["settled_trades"] == 1
    assert result["settled_trades"] == 1
    assert cohort["actor_types"] == {"NATIVE_PROTECTION": 1}


@pytest.mark.parametrize("mutation", [
    "wrong_child_order_id",
    "mismatched_long_child_id",
    "canceled_parent_leg",
    "me_order_id_is_entry_fill_id",
    "mismatched_associated_entry_id",
    "me_order_id_used_as_child",
    "wrong_leg",
    "conflicting_parent_id",
    "conflicting_child_id_string",
    "contradictory_text_leg",
])
def test_protection_actor_requires_succeeded_owned_parent_exact_child_and_entry_order_link(tmp_path, mutation):
    store = _store(tmp_path / f"protection-negative-{mutation}.sqlite3")
    _seed_episode(store, number=13, exit_actor="PROTECTION")
    with store._connect() as db:
        intent_id = "intent-entry-13"
        row = db.execute(
            "SELECT execution_result_json FROM order_intents WHERE intent_id=?", (intent_id,)
        ).fetchone()
        receipt = json.loads(row[0])
        observation = receipt["protection_terminal_observations"][0]
        readback = observation["native_readback"]
        raw = readback["raw"]
        if mutation == "wrong_child_order_id":
            readback["trade_id"] = raw["trade_id"] = "99999"
            readback["triggered_order_id"] = "99999"
        elif mutation == "mismatched_long_child_id":
            readback["trade_id"] = raw["trade_id"] = "30000000000000000000"
            readback["triggered_order_id"] = "30000000000000000000"
        elif mutation == "canceled_parent_leg":
            readback["finish_as"] = raw["finish_as"] = "canceled"
            readback.pop("triggered_order_id", None)
        elif mutation == "me_order_id_is_entry_fill_id":
            readback["me_order_id"] = raw["me_order_id"] = "entry-trade-13"
        elif mutation == "mismatched_associated_entry_id":
            readback["me_order_id"] = raw["me_order_id"] = "7777"
            readback["me_order_id_string"] = raw["me_order_id_string"] = "7777"
        elif mutation == "me_order_id_used_as_child":
            entry_order_id = "1013"
            readback.pop("trade_id", None)
            raw.pop("trade_id", None)
            readback.pop("trade_id_string", None)
            raw.pop("trade_id_string", None)
            readback["me_order_id"] = raw["me_order_id"] = entry_order_id
            readback["me_order_id_string"] = raw["me_order_id_string"] = entry_order_id
            readback["triggered_order_id"] = entry_order_id
        elif mutation == "wrong_leg":
            observation["leg"] = "take_profit"
        elif mutation == "conflicting_parent_id":
            raw["id_string"] = str(int(raw["id"]) + 1)
        elif mutation == "conflicting_child_id_string":
            raw["trade_id_string"] = str(int(raw["trade_id"]) + 1)
        elif mutation == "contradictory_text_leg":
            raw["initial"]["text"] = "owned-parent-tp"
        db.execute(
            "UPDATE order_intents SET execution_result_json=? WHERE intent_id=?",
            (json.dumps(receipt), intent_id),
        )
    report = validate_ai_strategy_evidence(store, "gate_testnet", "testnet")
    result = next(row for row in report["forward_comparison"]["results"]
                  if row["id"] == "aggressive_impulse")
    assert report["financial_settled"]["settled_trades"] == 1
    assert result["settled_trades"] == 0
    assert report["exclusions"]["autonomous_qualification"]["FINANCIAL_CLOSE_SIZE_ONLY_ACTOR_UNPROVEN"] == 1


def test_native_child_order_must_match_contract_side_and_reduce_only(tmp_path):
    store = _store(tmp_path / "protection-child-mismatch.sqlite3")
    _seed_episode(store, number=14, exit_actor="PROTECTION")
    with store._connect() as db:
        raw = json.dumps({
            "id": "2140", "contract": "ETH_USDT", "size": "-1",
            "is_reduce_only": False, "status": "finished", "left": "0",
        })
        db.execute(
            "UPDATE gate_remote_order_evidence SET contract='ETH_USDT',canonical_symbol='ETHUSDT',is_reduce_only=0,raw_json=? WHERE order_id='2140'",
            (raw,),
        )
    report = validate_ai_strategy_evidence(store, "gate_testnet", "testnet")
    result = next(row for row in report["forward_comparison"]["results"]
                  if row["id"] == "aggressive_impulse")
    assert result["settled_trades"] == 0


@pytest.mark.parametrize(("native_contract", "expected_financial_count"), [
    ("BTC_USDT", 1),
    ("ETH_USDT", 0),
])
def test_position_close_contract_compares_canonical_gate_symbol(tmp_path, native_contract, expected_financial_count):
    store = _store(tmp_path / f"position-close-contract-{native_contract}.sqlite3")
    _seed_episode(store, number=16, exit_actor="AI_REDUCE")
    with store._connect() as db:
        db.execute(
            "UPDATE gate_accounting_episodes SET contract='BTCUSDT' WHERE episode_id='episode-16'"
        )
        settlement_row = db.execute(
            "SELECT settlement_json FROM gate_episode_settlements WHERE episode_id='episode-16'"
        ).fetchone()
        settlement = json.loads(settlement_row[0])
        settlement["contract"] = "BTCUSDT"
        db.execute(
            "UPDATE gate_episode_settlements SET settlement_json=? WHERE episode_id='episode-16'",
            (json.dumps(settlement),),
        )
        db.execute(
            "UPDATE gate_position_close_evidence SET contract=? WHERE evidence_id='position-close-16'",
            (native_contract,),
        )

    report = validate_ai_strategy_evidence(store, "gate_testnet", "testnet")
    assert report["financial_settled"]["settled_trades"] == expected_financial_count
    if expected_financial_count:
        result = next(row for row in report["forward_comparison"]["results"]
                      if row["id"] == "aggressive_impulse")
        assert result["settled_trades"] == 1
    else:
        assert report["exclusions"]["settlement"]["NATIVE_POSITION_CLOSE_COMPONENTS_MISMATCH"] == 1


def test_mixed_ai_and_manual_close_fills_are_excluded(tmp_path):
    store = _store(tmp_path / "mixed-close.sqlite3")
    _seed_episode(store, number=9, close_count=2,
                  close_actor_overrides=["AI_REDUCE", "MANUAL"])
    report = validate_ai_strategy_evidence(store, "gate_testnet", "testnet")
    result = next(row for row in report["forward_comparison"]["results"]
                  if row["id"] == "aggressive_impulse")
    assert report["financial_settled"]["settled_trades"] == 1
    assert result["settled_trades"] == 0
    assert report["exclusions"]["autonomous_qualification"]["MIXED_AUTONOMOUS_AND_UNPROVEN_EXIT_SOURCES"] == 1


def test_incomplete_cost_settlement_is_excluded_from_both_bases(tmp_path):
    store = _store(tmp_path / "missing-cost.sqlite3")
    _seed_episode(store, number=10, exit_actor="AI_REDUCE")
    with store._connect() as db:
        row = db.execute("SELECT settlement_json FROM gate_episode_settlements WHERE episode_id='episode-10'").fetchone()
        settlement = json.loads(row[0])
        settlement["funding_status"] = "UNKNOWN"
        settlement["trade_history_coverage"] = "INCOMPLETE"
        db.execute(
            "UPDATE gate_episode_settlements SET settlement_json=?,funding_status='UNKNOWN' WHERE episode_id='episode-10'",
            (json.dumps(settlement),),
        )
    report = validate_ai_strategy_evidence(store, "gate_testnet", "testnet")
    assert report["financial_settled"]["settled_trades"] == 0
    assert report["exclusions"]["settlement"]["SETTLEMENT_COST_OR_COVERAGE_UNVERIFIED"] == 1


def test_environment_must_be_explicit_when_account_contains_live_and_testnet(tmp_path):
    store = _store(tmp_path / "ambiguous-env.sqlite3")
    _seed_episode(store, number=11, exit_actor="AI_REDUCE", environment="testnet")
    _seed_episode(store, number=12, exit_actor="AI_REDUCE", environment="live")
    report = validate_ai_strategy_evidence(store, "gate_testnet")
    assert report["environment"] is None
    assert report["exclusions"]["scope_error"] == "ACCOUNT_ENVIRONMENT_SCOPE_REQUIRED"
    assert report["financial_settled"]["settled_trades"] == 0
