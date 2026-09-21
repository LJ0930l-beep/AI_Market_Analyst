"""Regressions for truthful model identity and account-fact projection."""

from __future__ import annotations

import json
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.v3 import router_for as v3_router_for
from core.analysis.institutional_dashboard import _public_cycle
from core.model_routing import DEFAULT_SMART_MODEL
from core.storage import SQLiteStore
from core.trading.account_aliases import GATE_TESTNET_ACCOUNT_ID
from core.trading.ai_led_engine import (
    AICycleContext,
    AIActionOutput,
    AICycleResult,
    AILedDecisionEngine,
)
from core.trading.gate_accounts import provision_default_gate_accounts
from core.trading.gate_account_truth import GateAccountTruthService
from core.trading.ledger import AccountLedger
from core.trading.trader_capabilities import TraderCapabilityService, UNKNOWN


def _store(tmp_path, name: str = "model-truth.db") -> SQLiteStore:
    store = SQLiteStore(tmp_path / name)
    store.initialize()
    return store


def test_empty_system_blocked_cycle_has_no_synthetic_model_or_analysis():
    cycle = _public_cycle(
        {
            "cycle_id": "blocked-empty",
            "account_id": "paper-test",
            "action": "SYSTEM_BLOCKED",
            "status": "BLOCKED",
            "reason": "MODEL_UNAVAILABLE",
            "payload_json": "{}",
        }
    )

    assert cycle["action"] == "SYSTEM_BLOCKED"
    assert cycle["details"]["model"] is None
    assert cycle["details"]["model_receipt"] is None
    assert cycle["details"]["ai_analysis"] is None
    assert cycle["details"]["evidence_refs"] is None
    assert cycle["details"]["order_selection"] is None


def test_system_blocked_cycle_cannot_publish_even_a_valid_identity_as_a_decision():
    receipt = {
        "model_id": DEFAULT_SMART_MODEL,
        "actual_model_id": DEFAULT_SMART_MODEL,
        "model_identity_source": "completion_response",
        "verified_manifest_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
    }
    cycle = _public_cycle(
        {
            "cycle_id": "blocked-with-attempt",
            "account_id": "paper-test",
            "action": "SYSTEM_BLOCKED",
            "decision_origin": "SYSTEM",
            "status": "BLOCKED",
            "reason": "MODEL_RESPONSE_INVALID",
            "payload_json": json.dumps({
                "model_receipt": receipt,
                "analysis": {"confidence": 85},
                "evidence_refs": ["evidence:stale"],
                "order_selection": {"preference": "AUTO", "ttl_seconds": 900},
            }),
        }
    )
    assert cycle["details"]["model"] is None
    assert cycle["details"]["model_receipt"] is None
    assert cycle["details"]["ai_analysis"] is None
    assert cycle["details"]["evidence_refs"] is None
    assert cycle["details"]["order_selection"] is None


def test_public_cycle_accepts_identity_equivalent_alias_and_manifest_receipt():
    receipt = {
        "model_id": DEFAULT_SMART_MODEL,
        "actual_model_id": DEFAULT_SMART_MODEL,
        "model_identity_source": "completion_response",
        "verified_manifest_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
    }
    cycle = _public_cycle(
        {
            "cycle_id": "success",
            "account_id": "paper-test",
            "action": "WAIT",
            "status": "WAITING",
            "reason": "No setup.",
            "payload_json": json.dumps({"model_receipt": receipt}),
        }
    )
    assert cycle["details"]["model"] == DEFAULT_SMART_MODEL
    assert cycle["details"]["model_receipt"] == receipt


def test_request_bound_receipt_accepts_alias_matching_a_manifest_path():
    receipt = {
        "model_id": DEFAULT_SMART_MODEL,
        "actual_model_id": DEFAULT_SMART_MODEL,
        "model_identity_source": "request_bound_to_verified_manifest",
        "verified_manifest_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
    }
    cycle = _public_cycle(
        {
            "cycle_id": "request-bound-success",
            "account_id": "paper-test",
            "action": "WAIT",
            "status": "WAITING",
            "payload_json": json.dumps({"model_receipt": receipt}),
        }
    )
    assert cycle["details"]["model_receipt"] == receipt


def test_public_cycle_rejects_a_forged_model_or_manifest_identity():
    receipt = {
        "model_id": DEFAULT_SMART_MODEL,
        "actual_model_id": "Other-Bonsai-2-27B.gguf",
        "model_identity_source": "completion_response",
        "verified_manifest_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
    }
    cycle = _public_cycle(
        {
            "cycle_id": "forged",
            "account_id": "paper-test",
            "action": "WAIT",
            "status": "WAITING",
            "reason": "No setup.",
            "payload_json": json.dumps({"model_receipt": receipt}),
        }
    )
    assert cycle["details"]["model"] is None
    assert cycle["details"]["model_receipt"] is None


def test_system_blocked_persisted_cycle_does_not_claim_configured_model_or_order_policy(tmp_path):
    store = _store(tmp_path)
    ledger = AccountLedger(store)
    ledger.create_account("paper-test", mode="PAPER", initial_deposit=Decimal("1000"))
    engine = AILedDecisionEngine(
        store=store,
        execution_gateway=None,
        risk_engine=None,
        ledger=ledger,
        guardian=None,
    )
    context = AICycleContext(
        cycle_id="system-blocked",
        account_id="paper-test",
        generation=1,
        started_at="2030-01-02T12:00:00+00:00",
        expires_at="2030-01-02T12:15:00+00:00",
        allowed_instruments=(),
        model_id=DEFAULT_SMART_MODEL,
        model_call_attempted=True,
        model_inference_settings={
            "actual_model_id": DEFAULT_SMART_MODEL,
            "model_identity_source": "completion_response",
            "verified_manifest_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
        },
    )
    result = AICycleResult(
        cycle_id=context.cycle_id,
        action_output=AIActionOutput("WAIT", "", "The model was not called."),
        status="BLOCKED",
        reason="MODEL_UNAVAILABLE",
        decision_origin="SYSTEM",
        operational_state="SYSTEM_BLOCKED",
    )
    engine._persist_cycle(result, context)

    with store._connect() as db:
        payload = json.loads(
            db.execute(
                "SELECT payload_json FROM ai_led_cycles WHERE cycle_id=?",
                (context.cycle_id,),
            ).fetchone()[0]
        )
    assert payload["model"] is None
    assert payload["model_id"] is None
    assert payload["model_receipt"] is None
    assert payload["analysis"] is None
    assert payload["order_selection"] is None


def test_successful_model_cycle_preserves_verified_receipt_fields(tmp_path):
    store = _store(tmp_path, "success-receipt.db")
    ledger = AccountLedger(store)
    ledger.create_account("paper-test", mode="PAPER", initial_deposit=Decimal("1000"))
    engine = AILedDecisionEngine(
        store=store,
        execution_gateway=None,
        risk_engine=None,
        ledger=ledger,
        guardian=None,
    )
    context = AICycleContext(
        cycle_id="model-success",
        account_id="paper-test",
        generation=1,
        started_at="2030-01-02T12:00:00+00:00",
        expires_at="2030-01-02T12:15:00+00:00",
        allowed_instruments=("BTCUSDT",),
        model_id=DEFAULT_SMART_MODEL,
        model_version=r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
        model_call_attempted=True,
        model_call_completed=True,
        model_inference_settings={
            "actual_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
            "model_identity_source": "completion_response",
            "verified_manifest_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
        },
    )
    result = AICycleResult(
        cycle_id=context.cycle_id,
        action_output=AIActionOutput("WAIT", "BTCUSDT", "No setup.", order_preference="LIMIT"),
        status="WAITING",
        reason="No setup.",
        decision_origin="MODEL",
    )
    engine._persist_cycle(result, context)

    with store._connect() as db:
        row = db.execute(
            "SELECT payload_json FROM ai_led_cycles WHERE cycle_id=?",
            (context.cycle_id,),
        ).fetchone()
    payload = json.loads(row[0])
    assert payload["model"] == DEFAULT_SMART_MODEL
    assert payload["model_receipt"]["actual_model_id"].endswith("Ternary-Bonsai-2-27B-PTQ1_0.gguf")
    assert payload["model_receipt"]["verified_manifest_model_id"].endswith("Ternary-Bonsai-2-27B-PTQ1_0.gguf")
    assert payload["order_selection"]["preference"] == "LIMIT"


def test_gate_testnet_missing_remote_truth_keeps_account_values_unknown(tmp_path):
    store = _store(tmp_path, "gate-unknown.db")
    provision_default_gate_accounts(store)

    cockpit = TraderCapabilityService(store).risk_snapshot(GATE_TESTNET_ACCOUNT_ID)

    assert cockpit["account_truth_authority"] == "REMOTE_GATE_TESTNET_PRIVATE_API"
    assert cockpit["account_truth"]["status"] == "NOT_AVAILABLE"
    assert cockpit["account_truth"]["equity"] is None
    assert cockpit["ledger_snapshot"]["role"] == "REMOTE_FACTS_AUDIT_CACHE_ONLY"
    for key in ("initial_deposit", "wallet_balance", "cash", "net_equity", "reserved_risk", "daily_loss"):
        assert cockpit["ledger_snapshot"][key] is None
    assert cockpit["capacity"]["single_trade_risk_available"] == UNKNOWN
    assert cockpit["capacity"]["portfolio_risk_available"] == UNKNOWN
    assert cockpit["capacity"]["status"] == UNKNOWN
    assert cockpit["reconciliation"]["status"] == UNKNOWN
    assert cockpit["reconciliation"]["last_reconciled_at"] is None
    assert cockpit["model"]["status"] == UNKNOWN
    assert "REMOTE_ACCOUNT_TRUTH_UNAVAILABLE" in cockpit["risk"]["new_risk_block_reasons"]


def test_risk_cockpit_model_readiness_comes_from_nested_runtime_health(tmp_path):
    store = _store(tmp_path, "runtime-model-health.db")
    ledger = AccountLedger(store)
    ledger.create_account("paper-runtime", mode="PAPER", initial_deposit=Decimal("1000"))

    class Runtime:
        ai_coordinator = type(
            "Coordinator",
            (),
            {
                "status": lambda self: {
                    "state": "RUNNING",
                    "model": {
                        "status": "READY",
                        "model_id": DEFAULT_SMART_MODEL,
                        "actual_model_id": "Ternary-Bonsai-2-27B-PTQ1_0.gguf",
                        "available": True,
                        "model_available": True,
                        "checked_at": "2030-01-02T12:00:00+00:00",
                    },
                }
            },
        )()

        def status(self):
            return {"state": "RUNNING", "account_id": "paper-runtime", "execution_blocked": False}

    cockpit = TraderCapabilityService(store, runtime=Runtime()).risk_snapshot("paper-runtime")

    assert cockpit["model"]["status"] == "READY"
    assert cockpit["model"]["model_id"] == DEFAULT_SMART_MODEL
    assert cockpit["model"]["actual_model_id"] == "Ternary-Bonsai-2-27B-PTQ1_0.gguf"
    assert cockpit["model"]["runtime_state"] == "RUNNING"


def test_v3_gate_risk_summary_does_not_fall_back_to_local_equity(tmp_path):
    store = _store(tmp_path, "gate-risk-summary.db")
    provision_default_gate_accounts(store)
    app = FastAPI()
    app.include_router(v3_router_for(lambda: store))

    response = TestClient(app).get("/v3/risk/summary", params={"account_id": GATE_TESTNET_ACCOUNT_ID})

    assert response.status_code == 200
    payload = response.json()
    assert payload["cluster_pressure"]["status"] == "UNKNOWN"
    assert payload["risk"]["account_truth_status"] == "UNAVAILABLE"
    for key in ("net_equity", "cash", "allocated_margin", "max_portfolio_risk_budget", "reserved_risk", "daily_loss"):
        assert payload["risk"][key] is None
    assert payload["capacity"] == {
        "status": "UNKNOWN",
        "capacity_quantity": None,
        "reason": "ORDERBOOK_DEPTH_NOT_REPORTED",
    }
    assert payload["tca"]["status"] == "UNKNOWN"
    assert payload["tca"]["average_fill_price"] is None
    assert payload["correlation_status"] == "UNKNOWN"


def test_v3_gate_risk_summary_rejects_non_numeric_remote_account_values(tmp_path):
    store = _store(tmp_path, "gate-risk-invalid-truth.db")
    provision_default_gate_accounts(store)

    class InvalidTruth:
        def get_account_truth(self, *, include_trades=False):
            return {
                "status": "AVAILABLE",
                "source": "fixture",
                "observed_at": "2030-01-02T12:00:00+00:00",
                "equity": "not-a-number",
                "available_margin": "100",
                "used_margin": "0",
            }

    GateAccountTruthService(store).refresh(GATE_TESTNET_ACCOUNT_ID, InvalidTruth())
    app = FastAPI()
    app.include_router(v3_router_for(lambda: store))

    response = TestClient(app).get("/v3/risk/summary", params={"account_id": GATE_TESTNET_ACCOUNT_ID})

    assert response.status_code == 200
    payload = response.json()
    assert payload["cluster_pressure"]["status"] == "UNKNOWN"
    assert payload["risk"]["account_truth_status"] == "UNAVAILABLE"
    assert payload["risk"]["net_equity"] is None
