"""AI-authored strategy: real PAPER ledger boundaries, isolated model fixtures."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from core.storage import SQLiteStore
from core.trading.ai_led_engine import AICycleContext, AIActionOutput, AILedDecisionEngine
from core.trading.ai_session_coordinator import AISessionCoordinator
from core.trading.autonomous_strategy import CONTRACT, technical_context, validate_entry, book_cost_evidence
from core.trading.execution_gateway import ExecutionGateway
from core.trading.ledger import AccountLedger
from core.trading.position_guardian import PositionGuardian
from core.trading.risk_engine import RiskEngine
from core.trading.session_manager import SessionManager


def bars(now, minutes=15, count=64):
    end = now.replace(minute=(now.minute // minutes) * minutes, second=0, microsecond=0)
    return [{"bar_start": (end - timedelta(minutes=minutes * (i + 1))).isoformat(),
             "bar_end": (end - timedelta(minutes=minutes * i)).isoformat(),
             "available_at": (end - timedelta(minutes=minutes * i)).isoformat(),
             "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10.0,
             "is_closed": True, "quality_status": "VALID"} for i in reversed(range(count))]


def context_and_output(now):
    refs = ("market_snapshot:BTCUSDT:abc", "technical_snapshot:BTCUSDT:abc", "news_revision:news1")
    ctx = AICycleContext(
        cycle_id="ai-news-test", account_id="news-paper", generation=1,
        started_at=now.isoformat(), expires_at=(now + timedelta(seconds=240)).isoformat(),
        allowed_instruments=("BTCUSDT",), decision_contract=CONTRACT,
        market_snapshots={"BTCUSDT": {"price": 100.0, "fresh": True, "data_as_of": now.isoformat()}},
        technical_context=technical_context(SimpleNamespace(list_market_bars=lambda s, tf, limit: bars(now, 15 if tf == "15m" else 60)), ("BTCUSDT",), now),
        evidence_refs=refs,
        news_revisions=[{"revision_id": "news1", "symbols": ["BTCUSDT"], "title": "Observed public news",
                         "source": "fixture", "published_at": (now - timedelta(hours=1)).isoformat(), "known_at": now.isoformat()}],
    )
    out = AIActionOutput(
        action="OPEN_LONG", instrument_id="BTCUSDT", reason="新闻中性，技术面符合已满足的入场区间", entry_price=100.0,
        stop_price=95.0, take_profit=118.0, requested_risk_fraction=0.002, evidence_refs=refs,
        extra_fields={"strategy_plan": {"name": "AI 区间恢复", "thesis": "当前支持恢复", "entry_conditions": ["报价位于区间"], "exit_conditions": ["触及止损或止盈"]},
                      "entry_zone": {"low": 99.8, "high": 100.2}, "confidence": 75,
                      "timeframe_analysis": {"15m": "短线恢复", "1h": "价格稳定"},
                      "news_context": {"impact": "NEUTRAL", "summary": "来源未显示方向性冲击"}, "invalidation_condition": "跌破95失效"},
    )
    return ctx, out


@pytest.mark.parametrize("mutation,reason", [
    (lambda c, o: c.news_revisions.clear(), "NEWS_EVIDENCE_UNAVAILABLE"),
    (lambda c, o: c.news_revisions[0].update(known_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()), "NEWS_EVIDENCE_UNAVAILABLE"),
    (lambda c, o: c.news_revisions[0].update(symbols=["ETHUSDT"]), "NEWS_EVIDENCE_UNAVAILABLE"),
    (lambda c, o: setattr(o, "evidence_refs", (*o.evidence_refs, "news_revision:invented")), "INVALID_EVIDENCE_REF"),
    (lambda c, o: o.extra_fields.update(confidence=50), "AI_CONFIDENCE_BELOW_POLICY"),
    (lambda c, o: o.extra_fields.update(confidence=True), "AI_CONFIDENCE_BELOW_POLICY"),
    (lambda c, o: o.extra_fields.update(strategy_plan=None), "AI_STRATEGY_PLAN_REQUIRED"),
    (lambda c, o: o.extra_fields.update(entry_zone={"low": 101.0, "high": 102.0}), "AI_ENTRY_CONDITION_NOT_MET"),
    (lambda c, o: setattr(o, "take_profit", 110.0), "AI_NET_REWARD_RISK_TOO_LOW"),
    (lambda c, o: setattr(o, "stop_price", float("nan")), "AI_ENTRY_PRICES_REQUIRED"),
    (lambda c, o: setattr(o, "requested_risk_fraction", 0.003), "RISK_LIMIT_EXCEEDED"),
    (lambda c, o: setattr(o, "requested_leverage", 10), "AI_LEVERAGE_LIMIT_EXCEEDED"),
    (lambda c, o: c.technical_context["BTCUSDT"]["timeframes"]["15m"].update(status="BLOCKED"), "TECHNICAL_EVIDENCE_UNAVAILABLE"),
])
def test_unsafe_entry_is_blocked_before_any_gateway_call(mutation, reason):
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    mutation(ctx, output)
    assert validate_entry(ctx, output, now) == reason


def test_closed_bar_context_rejects_future_unclosed_gaps_and_nonfinite():
    now = datetime.now(timezone.utc)
    rows = bars(now)
    store = SimpleNamespace(list_market_bars=lambda *a, **k: rows)
    assert technical_context(store, ("BTCUSDT",), now)["BTCUSDT"]["timeframes"]["15m"]["status"] == "READY"
    rows[-1]["close"] = float("inf")
    rows[-2]["available_at"] = (now + timedelta(days=1)).isoformat()
    rows[-3]["is_closed"] = False
    frame = technical_context(store, ("BTCUSDT",), now)["BTCUSDT"]["timeframes"]["15m"]
    assert frame["status"] == "INSUFFICIENT_OR_STALE"
    assert all(b["close"] == 100 for b in frame["bars"])
    rows = bars(now)
    rows.pop(-10)
    assert technical_context(store, ("BTCUSDT",), now)["BTCUSDT"]["timeframes"]["15m"]["status"] == "INSUFFICIENT_OR_STALE"


def test_depth_costs_are_observed_and_empty_depth_is_rejected():
    book = {"bids": [{"p": "99.9", "s": "20"}], "asks": [{"p": "100.1", "s": "30"}], "source": "test"}
    result = book_cost_evidence(book, 100)
    assert result["slippage"] == pytest.approx(.001)
    assert result["depth_contracts"]["asks"] == 30
    with pytest.raises(ValueError):
        book_cost_evidence({"bids": [], "asks": []}, 100)


@pytest.fixture
def setup(tmp_path):
    store = SQLiteStore(tmp_path / "autonomous-news.db")
    store.initialize()
    ledger = AccountLedger(store)
    ledger.create_account("news-paper", mode="PAPER", initial_deposit=Decimal("10000"))
    gateway = ExecutionGateway(store, ledger=ledger)
    guardian = PositionGuardian(store, ledger)
    coordinator = AISessionCoordinator(store=store, service=SimpleNamespace(), ledger=ledger, guardian=guardian,
                                       session_manager=SessionManager(store), execution_gateway=gateway)
    engine = AILedDecisionEngine(store, gateway, RiskEngine(ledger), ledger, guardian)
    yield store, ledger, coordinator, engine
    coordinator.close()
    ledger.close()


def test_real_paper_open_and_wait_without_registered_strategy(setup):
    store, ledger, coordinator, engine = setup
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    assert validate_entry(ctx, output, now) is None
    result = engine.execute_cycle(ctx, now=now, model_output=output)
    assert result.status == "EXECUTED", result.reason
    assert result.order_intent.candidate_id is None
    assert result.order_intent.decision_path.value == "AI_LED"
    assert len(ledger.get_open_positions("news-paper")) == 1
    with store._connect() as db:
        payload = json.loads(db.execute("SELECT payload_json FROM ai_led_cycles").fetchone()[0])
    assert payload["decision_contract"] == CONTRACT
    assert payload["strategy_plan"]["name"] == "AI 区间恢复"
    assert payload["model_output"]["stop_price"] == 95.0
    assert payload["news_revisions"][0]["revision_id"] == "news1"
    ctx.cycle_id = "next-cycle"
    ctx.news_revisions = []
    wait = AIActionOutput(action="WAIT", instrument_id="BTCUSDT", reason="暂无新的开仓依据")
    assert engine.execute_cycle(ctx, now=now, model_output=wait).status == "WAITING"
    assert len(ledger.get_open_positions("news-paper")) == 1


def test_model_prompt_freezes_technical_news_and_ai_authored_json(setup):
    store, _, coordinator, engine = setup
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)

    class Model:
        model_id = "qwen3.5:9b"

        def generate_json(self, messages, **kwargs):
            assert kwargs["model_name"] == "qwen3.5:9b"
            assert "UNTRUSTED DATA" in messages[0]["content"]
            payload = json.loads(messages[1]["content"])
            assert payload["fixed_policy"]["strategy_origin"] == "AI_AUTHORED"
            assert payload["technical_context"]["BTCUSDT"]["status"] == "READY"
            assert payload["news_revisions"][0]["revision_id"] == "news1"
            return {"action": "OPEN_LONG", "instrument_id": "BTCUSDT", "reason": output.reason,
                    "entry_price": 100, "stop_price": 95, "take_profit": 118, "requested_risk_fraction": .002,
                    "evidence_refs": [r for r in payload["evidence_refs"] if r.startswith(("market_snapshot:", "technical_snapshot:", "news_revision:"))],
                    **deepcopy(output.extra_fields)}

    coordinator.model_provider = Model()
    decoded = coordinator._model_output(ctx)
    assert decoded.extra_fields["strategy_plan"]["name"] == "AI 区间恢复"
    assert ctx.evidence_bundle_id
    assert engine.execute_cycle(ctx, now=now, model_output=decoded).status == "EXECUTED"


def test_blocked_model_open_is_audited_as_open_not_fabricated_wait(setup):
    store, ledger, _, engine = setup
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    ctx.news_revisions = []
    result = engine.execute_cycle(ctx, now=now, model_output=output)
    assert result.status == "BLOCKED"
    assert result.action_output.action == "OPEN_LONG"
    assert result.decision_origin == "MODEL"
    assert not ledger.get_open_positions("news-paper")


def test_invalid_model_attempt_is_not_reported_as_never_called(setup):
    store, _, coordinator, _ = setup
    ctx, _ = context_and_output(datetime.now(timezone.utc))
    ctx.model_call_attempted = True
    result = coordinator._blocked_cycle(ctx, "INVALID_MODEL_OUTPUT_SCHEMA")
    assert result.decision_origin == "SYSTEM"
    with store._connect() as db:
        row = db.execute("SELECT model_called, model_result FROM ai_led_cycles").fetchone()
    assert row[0] == 1
    assert row[1] == "INVALID_OR_DISCARDED"
