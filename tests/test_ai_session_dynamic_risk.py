"""Production coordinator wiring for dynamic risk, isolated from venue APIs."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from core.storage import SQLiteStore
from core.model_routing import DEFAULT_SMART_MODEL
from core.trading import ai_session_coordinator as coordinator_module
from core.trading.ai_led_engine import AICycleResult, AICycleContext, AIActionOutput
from core.trading.ai_session_coordinator import AISessionCoordinator
from core.trading.execution_gateway import ExecutionGateway, TradingMode
from core.trading.ledger import AccountLedger
from core.trading.position_guardian import PositionGuardian
from core.trading.risk_engine import RiskEngine
from core.trading.session_manager import SessionManager
from core.trading.strategy_execution import normalize_execution, size_position


@pytest.fixture
def runtime(tmp_path):
    store = SQLiteStore(tmp_path / "dynamic-risk.sqlite3")
    store.initialize()
    ledger = AccountLedger(store)
    ledger.create_account("risk-paper", mode="PAPER", initial_deposit=Decimal("10000"))
    gateway = ExecutionGateway(store, ledger=ledger)
    coordinator = AISessionCoordinator(
        store=store,
        service=SimpleNamespace(),
        session_manager=SessionManager(store),
        ledger=ledger,
        guardian=PositionGuardian(store, ledger),
        execution_gateway=gateway,
        risk_engine=RiskEngine(ledger),
        clock=lambda: datetime(2026, 9, 14, 12, tzinfo=timezone.utc),
    )
    yield store, ledger, coordinator
    coordinator.close()
    ledger.close()


def _context(coordinator, *, now, execution=None):
    return coordinator._build_context(
        cycle_id="dynamic-risk-cycle",
        now=now,
        session_id="paper-session",
        generation=1,
        account_id="risk-paper",
        mode=TradingMode.PAPER,
        venue="simulated",
        authorization=None,
        snapshots={"BTCUSDT": {"price": 100.0, "fresh": True}},
        candidates=[],
        strategy_instructions={
            "revision": 7,
            "template_id": "aggressive_impulse",
            "execution": normalize_execution(execution),
        },
    )


def _insert_exit(store, fill_id, *, at, pnl):
    with store._connect() as db:
        db.execute(
            """INSERT INTO trade_fills(
                fill_id,account_id,venue,mode,symbol,side,quantity,price,status,
                payload_json,created_at,event_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                fill_id, "risk-paper", "simulated", "PAPER", "BTCUSDT", "SELL", "1", "100",
                "FILLED", json.dumps({"reduce_only": True, "realized_pnl": pnl}),
                at.isoformat(), at.isoformat(),
            ),
        )


def test_cycle_context_contains_strategy_scoped_risk_and_atr_sizing(runtime):
    _, _, coordinator = runtime
    now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    context = _context(coordinator, now=now, execution={"atr_adaptive_sizing": True})

    risk = context.dynamic_risk
    assert risk["entry_allowed"] is True
    assert risk["strategy_revision"] == 7
    assert risk["strategy_template_id"] == "aggressive_impulse"
    assert risk["atr_adaptive_sizing"] == {
        "enabled": True,
        "status": "ENFORCED_BY_STOP_DISTANCE_RISK_SIZING",
        "source": "ai_led_engine.size_position.stop_distance_and_risk_budget",
        "sizing_contract": "ATR-informed stop distance scales quantity against fixed risk budget",
    }
    assert risk["evidence_status"] == "NO_AUTHORITATIVE_EXIT_EVIDENCE"
    assert risk["evidence_source"] == "account_trade_fills.reduce_only_realized_pnl"

    # Wider ATR-derived stop distance reduces quantity while preserving the
    # configured loss budget. This is the executable sizing contract.
    execution = normalize_execution()
    narrow = size_position(
        execution, equity=10000, available=10000, used_margin=0, entry=100,
        unit_risk=1, contract_size=1, step=0.01, risk_fraction=0.0025,
        leverage=5, fee_rate=0.0005,
    )
    wide = size_position(
        execution, equity=10000, available=10000, used_margin=0, entry=100,
        unit_risk=2, contract_size=1, step=0.01, risk_fraction=0.0025,
        leverage=5, fee_rate=0.0005,
    )
    assert narrow["quantity"] > wide["quantity"]
    assert narrow["risk_budget_usdt"] == wide["risk_budget_usdt"] == 25
    assert narrow["estimated_loss_usdt"] <= 25
    assert wide["estimated_loss_usdt"] <= 25


def test_bonsai_prompt_receives_the_same_dynamic_risk_snapshot(runtime, monkeypatch):
    _, _, coordinator = runtime
    now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    context = _context(coordinator, now=now, execution={"atr_adaptive_sizing": True})
    captured = {}

    class PromptCaptureModel:
        model_id = DEFAULT_SMART_MODEL
        model_version = "fixture-only"
        context_length = 16384
        max_tokens = 512

        def generate_json(self, messages, **_kwargs):
            captured["payload"] = json.loads(messages[1]["content"])
            artifact = r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf"
            return (
                {
                    "action": "WAIT",
                    "instrument_id": "BTCUSDT",
                    "reason": "单测：不满足开仓条件",
                    "confidence": None,
                },
                None,
                {
                    "model_id": DEFAULT_SMART_MODEL,
                    "actual_model_id": artifact,
                    "model_identity_source": "completion_response",
                    "verified_manifest_model_id": artifact,
                },
            )

    coordinator.model_provider = PromptCaptureModel()
    monkeypatch.setattr(coordinator_module, "_load_market_radar_snapshot", lambda *_args: {})

    output = coordinator._model_output(context)

    assert output.action == "WAIT"
    assert captured["payload"]["dynamic_risk"] == context.dynamic_risk
    assert captured["payload"]["dynamic_risk"]["entry_allowed"] is True
    assert captured["payload"]["dynamic_risk"]["atr_adaptive_sizing"]["enabled"] is True


def test_two_authoritative_losses_are_injected_into_context_and_block_open(runtime):
    store, _, coordinator = runtime
    now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    _insert_exit(store, "loss-recent", at=now - timedelta(minutes=10), pnl=-5)
    _insert_exit(store, "loss-prior", at=now - timedelta(minutes=20), pnl=-3)
    context = _context(coordinator, now=now)
    assert context.dynamic_risk["entry_allowed"] is False
    assert context.dynamic_risk["reasons"] == ["CONSECUTIVE_LOSS_COOLDOWN"]
    assert context.dynamic_risk["blocked_until"] == (now + timedelta(hours=1, minutes=50)).isoformat()
    assert [row["fill_id"] for row in context.dynamic_risk["evidence"]["recent_authoritative_exits"]] == ["loss-recent", "loss-prior"]

    context.model_call_attempted = True
    context.model_call_completed = True
    output = AIActionOutput(action="OPEN_LONG", instrument_id="BTCUSDT", reason="单测中的已完成模型开仓建议")
    result = coordinator._execute_model_decision(context, output, now=now)
    assert result.status == "BLOCKED"
    assert result.reason.startswith("DYNAMIC_RISK_ENTRY_BLOCKED: CONSECUTIVE_LOSS_COOLDOWN")
    with store._connect() as db:
        row = db.execute("SELECT status,reason,payload_json FROM ai_led_cycles WHERE cycle_id=?", (context.cycle_id,)).fetchone()
    assert row["status"] == "BLOCKED"
    assert row["reason"].startswith("DYNAMIC_RISK_ENTRY_BLOCKED:")
    persisted = json.loads(row["payload_json"])
    assert persisted["model_output"]["extra_fields"]["dynamic_risk"]["entry_allowed"] is False


def test_us_open_defense_blocks_only_new_entries(runtime, monkeypatch):
    _, _, coordinator = runtime
    # Monday 09:20 America/New_York, inside the configured 09:15-09:30 guard.
    now = datetime(2026, 9, 14, 13, 20, tzinfo=timezone.utc)
    context = _context(coordinator, now=now)
    assert context.dynamic_risk["entry_allowed"] is False
    assert "US_OPEN_DEFENSE" in context.dynamic_risk["reasons"]
    assert context.dynamic_risk["blocked_until"] == datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc).isoformat()

    seen = []

    class RecordingEngine:
        def __init__(self, **_kwargs):
            pass

        def execute_cycle(self, _context, *, now, model_output):
            seen.append(model_output.action)
            return AICycleResult(
                cycle_id=_context.cycle_id,
                action_output=model_output,
                status="HOLDING",
                reason="position management passed through",
            )

    monkeypatch.setattr(coordinator_module, "AILedDecisionEngine", RecordingEngine)
    close = AIActionOutput(action="CLOSE_POSITION", instrument_id="BTCUSDT", reason="关闭已持有仓位")
    result = coordinator._execute_model_decision(context, close, now=now)
    assert result.status == "HOLDING"
    assert seen == ["CLOSE_POSITION"]


def test_dynamic_risk_failure_fails_closed_only_for_open(runtime, monkeypatch):
    _, _, coordinator = runtime
    now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(
        coordinator_module,
        "evaluate_dynamic_risk",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("test-only unavailable evidence source")),
    )
    context = _context(coordinator, now=now)
    assert context.dynamic_risk["entry_allowed"] is False
    assert context.dynamic_risk["reasons"] == ["DYNAMIC_RISK_EVALUATION_UNAVAILABLE"]
    assert context.dynamic_risk["evidence_source"] == "account_trade_fills.realized_exit_payload"

    called = []

    class RecordingEngine:
        def __init__(self, **_kwargs):
            pass

        def execute_cycle(self, _context, *, now, model_output):
            called.append(model_output.action)
            return AICycleResult(
                cycle_id=_context.cycle_id,
                action_output=model_output,
                status="WAITING",
                reason="management remains available",
            )

    monkeypatch.setattr(coordinator_module, "AILedDecisionEngine", RecordingEngine)
    open_result = coordinator._execute_model_decision(
        context,
        AIActionOutput(action="OPEN_SHORT", instrument_id="BTCUSDT", reason="开仓"),
        now=now,
    )
    assert open_result.status == "BLOCKED"
    close_result = coordinator._execute_model_decision(
        context,
        AIActionOutput(action="REDUCE_POSITION", instrument_id="BTCUSDT", reason="减仓"),
        now=now,
    )
    assert close_result.status == "WAITING"
    # The blocked open is persisted as a SYSTEM WAIT; the later reduction is
    # passed through unchanged while the same lock remains active.
    assert called == ["WAIT", "REDUCE_POSITION"]
