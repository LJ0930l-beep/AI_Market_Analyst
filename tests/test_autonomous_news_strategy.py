"""AI-authored strategy: real PAPER ledger boundaries, isolated model fixtures."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from core.storage import SQLiteStore
from core.model_routing import DEFAULT_SMART_MODEL
from core.trading.ai_led_engine import AICycleContext, AIActionOutput, AILedDecisionEngine
from core.trading import ai_session_coordinator as coordinator_module
from core.trading.ai_session_coordinator import AISessionCoordinator
from core.trading.autonomous_strategy import CONTRACT, technical_context, compact_technical, validate_entry, book_cost_evidence, resolve_order_preference
from core.trading.decision_memory import list_decision_memory, record_decision_memory, update_memory_outcome
from core.trading.execution_gateway import ExecutionGateway, TradingMode
from core.trading.ledger import AccountLedger
from core.trading.order_selection import OrderSelectionInput, OrderSelectionPolicy
from core.trading.position_guardian import PositionGuardian
from core.trading.risk_engine import RiskEngine
from core.trading.session_manager import SessionManager
from core.trading.strategy_execution import normalize_execution


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
        model_id=DEFAULT_SMART_MODEL,
        model_version=r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
        model_call_attempted=True,
        model_call_completed=True,
        model_inference_settings={
            "actual_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
            "model_identity_source": "completion_response",
            "verified_manifest_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
        },
        market_snapshots={"BTCUSDT": {"price": 100.0, "bid": 99.95, "ask": 100.05, "slippage": 0.001, "liquidity_ok": True, "fresh": True, "data_as_of": now.isoformat()}},
        technical_context=technical_context(SimpleNamespace(list_market_bars=lambda s, tf, limit: bars(now, 15 if tf == "15m" else 60)), ("BTCUSDT",), now),
        evidence_refs=refs,
        news_revisions=[{"revision_id": "news1", "symbols": ["BTCUSDT"], "title": "Observed public news",
                         "source": "fixture", "published_at": (now - timedelta(hours=1)).isoformat(), "known_at": now.isoformat()}],
    )
    out = AIActionOutput(
        action="OPEN_LONG", instrument_id="BTCUSDT", reason="新闻中性，技术面符合已满足的入场区间", entry_price=100.0,
        stop_price=95.0, take_profit=118.0, requested_risk_fraction=0.002, order_preference="MARKET", evidence_refs=refs,
        extra_fields={"strategy_plan": {"name": "AI 区间恢复", "thesis": "当前支持恢复", "entry_conditions": ["报价位于区间"], "exit_conditions": ["触及止损或止盈"]},
                      "entry_zone": {"low": 99.8, "high": 100.2}, "confidence": 75,
                      "timeframe_analysis": {"15m": "短线恢复", "1h": "价格稳定"},
                      "news_context": {"impact": "NEUTRAL", "summary": "来源未显示方向性冲击"}, "invalidation_condition": "跌破95失效"},
    )
    return ctx, out


def market_radar_fixture(symbols, now):
    symbol = symbols[0] if symbols else "BTCUSDT"
    return {
        "status": "AVAILABLE", "generated_at": now.isoformat(), "symbols": list(symbols),
        "cvd": {"status": "AVAILABLE", "source": "gate_public_trades_ws_and_rest", "as_of": now.isoformat(),
                "unit": "contracts", "synthetic": False,
                "series": [{"symbol": symbol, "time": now.isoformat(), "price": 100, "buy_contracts": 12,
                            "sell_contracts": 7, "delta_contracts": 5, "cvd_contracts": 5, "trade_count": 19}]},
        "open_interest": {"status": "AVAILABLE", "source": "gate_and_binance_public_derivatives", "as_of": now.isoformat(),
                          "binance_status": "AVAILABLE", "synthetic": False,
                          "series": [{"venue": "gate", "symbol": symbol, "time": now.isoformat(), "open_interest": 1000,
                                      "price": 100, "unit": "contracts"}]},
        "derivatives_matrix": [{"symbol": symbol, "gate": {"status": "AVAILABLE", "time": now.isoformat(),
                                 "oi_change_pct": 1.2, "funding_rate_pct": 0.01},
                                "binance": {"status": "AVAILABLE", "source": "binance_futures_public_rest",
                                            "time": now.isoformat(), "oi_change_pct": 1.4, "funding_rate_pct": 0.02},
                                "crowding_score": 12, "crowding_label": "BALANCED"}],
        "liquidations": {"status": "NO_DATA", "source": "gate_futures.public_liquidates_websocket", "as_of": None,
                         "window_hours": 24, "counts": {"LONG": 0, "SHORT": 0}, "estimated_notional": {}, "recent": [], "synthetic": False},
        "onchain": {"status": "CONFIG_REQUIRED", "source": None, "as_of": None, "providers": {"arkham": {"status": "CONFIG_REQUIRED"}}, "events": [], "synthetic": False},
        "cross_market": {"status": "CONFIG_REQUIRED", "source": None, "as_of": None,
                         "message": "DXY/NQ 数据源未配置", "items": [{"symbol": "DXY", "value": None,
                                                                            "change_pct": None, "status": "SOURCE_REQUIRED"}]},
    }


@pytest.mark.parametrize("mutation,reason", [
    (lambda c, o: c.news_revisions.clear(), "NEWS_EVIDENCE_UNAVAILABLE"),
    (lambda c, o: c.news_revisions[0].update(known_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()), "NEWS_EVIDENCE_UNAVAILABLE"),
    (lambda c, o: c.news_revisions[0].update(symbols=["ETHUSDT"]), "NEWS_EVIDENCE_UNAVAILABLE"),
    (lambda c, o: setattr(o, "evidence_refs", (*o.evidence_refs, "news_revision:invented")), "INVALID_EVIDENCE_REF"),
    (lambda c, o: o.extra_fields.update(confidence=50), "AI_CONFIDENCE_BELOW_POLICY"),
    (lambda c, o: o.extra_fields.update(confidence=True), "AI_CONFIDENCE_BELOW_POLICY"),
    (lambda c, o: o.extra_fields.update(entry_zone={"low": 101.0, "high": 102.0}), "AI_ENTRY_CONDITION_NOT_MET"),
    (lambda c, o: setattr(o, "take_profit", 110.0), "AI_NET_REWARD_RISK_TOO_LOW"),
    (lambda c, o: setattr(o, "stop_price", float("nan")), "AI_ENTRY_PRICES_REQUIRED"),
    (lambda c, o: setattr(o, "requested_risk_fraction", 0.003), "RISK_LIMIT_EXCEEDED"),
    (lambda c, o: setattr(o, "requested_leverage", 101), "AI_LEVERAGE_LIMIT_EXCEEDED"),
    (lambda c, o: setattr(o, "stop_price", 99.9), "AI_STOP_DISTANCE_TOO_NARROW"),
    (lambda c, o: c.technical_context["BTCUSDT"]["timeframes"]["15m"].update(status="BLOCKED"), "TECHNICAL_EVIDENCE_UNAVAILABLE"),
])
def test_unsafe_entry_is_blocked_before_any_gateway_call(mutation, reason):
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    mutation(ctx, output)
    assert validate_entry(ctx, output, now) == reason


def test_open_accepts_model_trade_without_optional_narrative_fields():
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    for key in ("strategy_plan", "entry_zone", "news_context", "timeframe_analysis", "invalidation_condition"):
        output.extra_fields.pop(key, None)
    assert validate_entry(ctx, output, now) is None
    assert output.extra_fields["entry_zone_source"] == "MODEL_ENTRY_AND_CURRENT_QUOTE"


def test_limit_entry_without_optional_zone_or_limit_price_uses_model_price():
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    ctx.strategy_instructions = {
        "execution": {"symbols": [], "universe_mode": "ALL", "direction": "BOTH", "leverage": 3,
                      "risk_per_trade_pct": .25, "min_confidence": 70, "min_net_rr": 2,
                      "order_preference": "AUTO", "scan_interval_minutes": 15},
        "profile": {"order_preference": "AUTO", "limit_priority": True,
                    "allow_future_limit": True, "max_limit_distance_pct": .85},
    }
    output.order_preference = "LIMIT"
    output.entry_price = 99.3
    output.limit_price = None
    output.extra_fields.pop("entry_zone", None)
    assert validate_entry(ctx, output, now) is None
    assert output.limit_price == 99.3
    assert output.extra_fields["limit_price_source"] == "MODEL_ENTRY_PRICE"


def test_no_fresh_news_is_disclosed_but_does_not_alone_block_verified_technical_entry():
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    ctx.news_revisions.clear()
    ctx.evidence_refs = tuple(ref for ref in ctx.evidence_refs if not ref.startswith("news_revision:"))
    output.evidence_refs = tuple(ref for ref in output.evidence_refs if not ref.startswith("news_revision:"))
    output.extra_fields["news_context"] = {
        "impact": "UNKNOWN",
        "summary": "本轮输入中没有 48 小时内适用于该币种的已核验新闻。",
    }

    assert validate_entry(ctx, output, now) is None


def test_old_google_search_false_symbol_hit_is_not_a_mandatory_news_citation():
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    ctx.news_revisions[0].update(
        title="Crypto Profit Calculator — Calculate Trade P&L",
        url="https://news.google.com/rss/articles/example",
        symbols=["BTCUSDT"],
    )
    ctx.evidence_refs = tuple(ref for ref in ctx.evidence_refs if not ref.startswith("news_revision:"))
    output.evidence_refs = tuple(ref for ref in output.evidence_refs if not ref.startswith("news_revision:"))
    output.extra_fields["news_context"] = {"impact": "UNKNOWN", "summary": "搜索结果未证实与该币种相关。"}
    assert validate_entry(ctx, output, now) is None


def test_cited_news_with_unknown_impact_keeps_uncertainty_without_blocking_technical_entry():
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    output.extra_fields["news_context"] = {
        "impact": "UNKNOWN", "summary": "已引用这条新闻，但来源没有足够证据判断方向。",
    }
    assert validate_entry(ctx, output, now) is None


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


def test_five_minute_strategy_loads_its_signal_frame():
    now = datetime.now(timezone.utc)
    store = SimpleNamespace(list_market_bars=lambda _symbol, timeframe, limit: bars(now, 5 if timeframe == "5m" else (15 if timeframe == "15m" else 60)))
    result = technical_context(store, ("BTCUSDT",), now, interval=5)
    assert tuple(result["BTCUSDT"]["timeframes"]) == ("5m", "15m", "1h")
    assert all(frame["status"] == "READY" for frame in result["BTCUSDT"]["timeframes"].values())


def test_compact_technical_states_price_side_of_ema_without_model_arithmetic():
    raw = {"KASUSDT": {"status": "READY", "timeframes": {"5m": {
        "status": "READY", "last_closed_at": "2026-09-23T15:00:00+00:00",
        "bars": [{"open": .0392, "high": .0393, "low": .0389, "close": .03901, "volume": 100}],
        "indicators": {"ema20": .03974},
    }}}}
    frame = compact_technical(raw, signal_timeframe="5m")["KASUSDT"]["timeframes"]["5m"]
    assert frame["price_vs_ema20"] == "BELOW"
    assert frame["candles"][-1][3] == .03901


def test_nofx_indicator_snapshot_requires_gate_perpetual_last_bar_identity():
    now = datetime(2026, 9, 21, 8, 15, tzinfo=timezone.utc)
    configuration = {"enable_ema": True, "ema_periods": [20], "enable_oi": True}
    rows = bars(now, 5, count=64)
    for row in rows:
        row.update({
            "provider": "gate", "source": "gate_native_rest:last", "venue": "gate",
            "market_type": "perpetual", "price_type": "last",
        })
    store = SimpleNamespace(latest_bars=lambda _symbol, _timeframe, *, limit, **_filters: rows[-limit:])
    frame = technical_context(
        store, ("BTCUSDT",), now, interval=5, timeframes=("5m",), nofx_indicators=configuration,
    )["BTCUSDT"]["timeframes"]["5m"]
    assert frame["nofx_indicator_snapshot"]["source"] == "gate_native_rest:last"
    assert frame["nofx_indicator_snapshot"]["indicators"]["ema"]["status"] == "AVAILABLE"

    for row in rows:
        row["provider"] = "untrusted"
    blocked = technical_context(
        store, ("BTCUSDT",), now, interval=5, timeframes=("5m",), nofx_indicators=configuration,
    )["BTCUSDT"]["timeframes"]["5m"]["nofx_indicator_snapshot"]
    assert blocked["status"] == "UNAVAILABLE"
    assert blocked["reason"] == "GATE_BAR_IDENTITY_UNVERIFIED"


def test_profiled_limit_strategy_accepts_passive_future_entry():
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    ctx.strategy_instructions = {
        "execution": {"symbols": [], "universe_mode": "ALL", "direction": "BOTH", "leverage": 3, "risk_per_trade_pct": .25, "min_confidence": 70, "min_net_rr": 2, "order_preference": "LIMIT", "scan_interval_minutes": 15},
        "profile": {"allow_future_limit": True},
    }
    output.order_preference = "AUTO"
    output.entry_price = 98.0
    output.limit_price = 98.0
    output.stop_price = 94.0
    output.take_profit = 108.0
    output.extra_fields["entry_zone"] = {"low": 97.5, "high": 98.5}
    assert validate_entry(ctx, output, now) is None


def test_limit_profile_cannot_be_widened_to_market_by_model_output():
    instructions = {
        "execution": {"order_preference": "LIMIT"},
        "profile": {"order_preference": "LIMIT", "allow_market_entry": False},
    }
    assert resolve_order_preference(instructions, "MARKET") == "LIMIT"
    assert resolve_order_preference(instructions, "AUTO") == "LIMIT"


def test_auto_passive_entry_without_optional_limit_field_uses_strategy_limit_priority():
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    ctx.strategy_instructions = {
        "execution": {
            "symbols": [], "universe_mode": "ALL", "direction": "BOTH",
            "leverage": 3, "risk_per_trade_pct": .25, "min_confidence": 70,
            "min_net_rr": 2, "order_preference": "AUTO", "scan_interval_minutes": 15,
        },
        "profile": {
            "order_preference": "AUTO", "limit_priority": True,
            "allow_future_limit": True, "max_limit_distance_pct": 0.85,
        },
    }
    output.order_preference = "AUTO"
    output.entry_price = 99.83
    output.limit_price = None
    output.extra_fields["entry_zone"] = {"low": 99.8, "high": 99.9}

    assert validate_entry(ctx, output, now) is None
    assert output.order_preference == "LIMIT"
    assert output.limit_price == 99.83
    assert output.extra_fields["execution_preference_override"]["reason"] == "PROFILE_LIMIT_PRIORITY_PASSIVE_ENTRY"


@pytest.mark.parametrize("model_preference", ["AUTO", "MARKET"])
def test_auto_market_below_trigger_threshold_is_normalized_to_limit_before_selection(model_preference):
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    ctx.strategy_instructions = {
        "execution": {
            "symbols": [], "universe_mode": "ALL", "direction": "BOTH",
            "leverage": 3, "risk_per_trade_pct": .25, "min_confidence": 70,
            "min_net_rr": 2, "order_preference": "AUTO", "scan_interval_minutes": 15,
        },
        "profile": {
            "order_preference": "AUTO", "limit_priority": True,
            "allow_market_entry": True, "market_min_trigger_completion": 92,
        },
    }
    output.order_preference = model_preference
    output.entry_price = 99.95
    output.limit_price = 99.95
    output.stop_price = 95.0
    output.take_profit = 118.0
    output.extra_fields["entry_zone"] = {"low": 99.8, "high": 100.2}
    output.extra_fields["strategy_analysis"] = {"trigger_completion_pct": 65}

    assert validate_entry(ctx, output, now) is None
    assert output.order_preference == "LIMIT"
    assert output.extra_fields["execution_preference_override"] == {
        "requested_preference": model_preference,
        "effective_preference": "LIMIT",
        "reason": "MARKET_TRIGGER_COMPLETION_BELOW_THRESHOLD",
        "trigger_completion_pct": 65,
        "required_trigger_completion_pct": 92,
    }

    # Quotes and depth are deliberately healthy: without normalization the
    # downstream AUTO selector would choose MARKET at this near-quote price.
    selection = OrderSelectionPolicy.select(OrderSelectionInput(
        side="LONG", preference=resolve_order_preference(ctx.strategy_instructions, output.order_preference),
        quote=100.0, bid=99.99, ask=100.01, limit_price=output.limit_price,
        entry_zone_low=99.8, entry_zone_high=100.2,
        signal_at=now, now=now, slippage_bps=1, liquidity_ok=True,
        tick_size=.01, market_is_remote=True,
    ))
    assert selection.order_type == "limit"
    assert selection.reason_code == "LIMIT_REQUESTED"


def test_auto_below_market_threshold_without_passive_limit_is_blocked():
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    ctx.strategy_instructions = {
        "execution": {
            "symbols": [], "universe_mode": "ALL", "direction": "BOTH",
            "leverage": 3, "risk_per_trade_pct": .25, "min_confidence": 70,
            "min_net_rr": 2, "order_preference": "AUTO", "scan_interval_minutes": 15,
        },
        "profile": {
            "order_preference": "AUTO", "limit_priority": True,
            "allow_market_entry": True, "market_min_trigger_completion": 92,
        },
    }
    output.order_preference = "AUTO"
    output.extra_fields["strategy_analysis"] = {"trigger_completion_pct": 65}

    assert validate_entry(ctx, output, now) == "AI_MARKET_TRIGGER_NOT_CONFIRMED"


def test_profile_signal_timeframe_wins_over_stale_execution_interval():
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    ctx.strategy_instructions = {
        "execution": {
            "symbols": [], "universe_mode": "ALL", "direction": "BOTH",
            "leverage": 3, "risk_per_trade_pct": .25, "min_confidence": 70,
            "min_net_rr": 2, "order_preference": "MARKET", "scan_interval_minutes": 5,
        },
        "profile": {"signal_timeframe": "15m", "order_preference": "MARKET"},
    }
    output.order_preference = "MARKET"
    # The old execution row says 5m, but this profile only requires the
    # available 15m signal and 1h context frames.
    assert validate_entry(ctx, output, now) is None


def test_depth_costs_are_observed_and_empty_depth_is_rejected():
    book = {"bids": [{"p": "99.9", "s": "20"}], "asks": [{"p": "100.1", "s": "30"}], "source": "test"}
    result = book_cost_evidence(book, 100)
    assert result["slippage"] == pytest.approx(.001)
    assert result["depth_contracts"]["asks"] == 30
    with pytest.raises(ValueError):
        book_cost_evidence({"bids": [], "asks": []}, 100)


def test_model_net_rr_repair_uses_exact_frozen_limit_costs_without_changing_trade():
    from core.trading.ai_session_coordinator import _model_net_rr_repair_bounds

    context = SimpleNamespace(
        allowed_instruments=('BCHUSDT',),
        market_snapshots={'BCHUSDT': {
            'price': 338.95, 'fee_rate': 0.00075,
            'slippage': 0.0009735949255051946,
        }},
        strategy_instructions={
            'profile': {'allow_future_limit': True},
            'execution': {'min_net_rr': 1.6},
        },
    )
    model_trade = {
        'action': 'OPEN_LONG', 'instrument_id': 'BCHUSDT',
        'order_preference': 'LIMIT', 'limit_price': 338.95,
        'stop_price': 333.82, 'take_profit': 349.22,
    }
    bound = _model_net_rr_repair_bounds(context, model_trade)
    assert bound is not None
    assert bound['observed_net_rr'] == pytest.approx(1.57962, rel=1e-5)
    assert bound['required_target_bound'] == pytest.approx(349.341669, rel=1e-6)
    assert model_trade['take_profit'] == 349.22
    assert _model_net_rr_repair_bounds(context, {**model_trade, 'take_profit': 349.35}) is None


def test_net_rr_bound_detects_passive_auto_entry_without_redundant_limit_fields():
    from core.trading.ai_session_coordinator import _model_net_rr_repair_bounds

    context = SimpleNamespace(
        allowed_instruments=('BTCUSDT',),
        market_snapshots={'BTCUSDT': {'price': 101.0, 'fee_rate': .00075, 'slippage': .001}},
        strategy_instructions={
            'profile': {'allow_future_limit': True, 'limit_priority': True, 'max_limit_distance_pct': .85},
            'execution': {'min_net_rr': 1.6},
        },
    )
    trade = {'action': 'OPEN_LONG', 'instrument_id': 'BTCUSDT', 'entry_price': 100.5,
             'stop_price': 98.0, 'take_profit': 104.5}
    bound = _model_net_rr_repair_bounds(context, trade)
    assert bound is not None and bound['required_target_bound'] > trade['take_profit']
    assert _model_net_rr_repair_bounds(context, {**trade, 'entry_price': 101.5}) is None


def test_net_rr_tick_rounding_is_not_a_false_risk_rejection():
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    ctx.strategy_instructions = {
        'profile': {'signal_timeframe': '15m', 'allow_future_limit': True},
        'execution': normalize_execution({'universe_mode': 'ALL', 'symbols': [],
                                          'direction': 'BOTH', 'min_net_rr': 1.6}),
    }
    output.order_preference = 'LIMIT'
    output.limit_price = 100
    fee = 0.0005  # Explicit local PAPER contract in this fixture.
    slip = ctx.market_snapshots['BTCUSDT']['slippage']
    loss = 100 - 95 + 95 * slip + (100 + 95) * fee
    bound = (1.6 * loss + 100 * (1 + fee)) / (1 - slip - fee)
    output.take_profit = bound - 1e-10
    assert validate_entry(ctx, output, now) is None
    output.take_profit = bound - 0.01
    assert validate_entry(ctx, output, now) == 'AI_NET_REWARD_RISK_TOO_LOW'


def test_low_net_rr_open_gets_one_model_authored_target_retry(setup):
    _, _, coordinator, _ = setup
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    ctx.market_snapshots['BTCUSDT']['fee_rate'] = 0.00075
    ctx.strategy_instructions = {
        'profile': {'signal_timeframe': '15m', 'atr_stop_multiple': 1.8,
                    'major_stop_floor_pct': 0.6, 'alt_stop_floor_pct': 1.5,
                    'allow_future_limit': True, 'max_limit_distance_pct': 0.85},
        'execution': normalize_execution({'universe_mode': 'ALL', 'symbols': [],
                                          'direction': 'BOTH', 'min_net_rr': 1.6}),
    }

    class Model:
        model_id = 'Bonsai-2-27B-PTQ1_0'
        context_length = 32768
        max_tokens = 900

        def __init__(self):
            self.calls = []

        def generate_json(self, messages, **kwargs):
            self.calls.append(kwargs['prompt_version'])
            payload = json.loads(messages[1]['content'])
            if len(self.calls) == 1:
                refs = [ref for ref in payload['evidence_refs']
                        if ref.startswith(('market_snapshot:', 'technical_snapshot:', 'news_revision:'))]
                decision = {
                    'action': 'OPEN_LONG', 'instrument_id': 'BTCUSDT',
                    'reason': output.reason, 'confidence': 75,
                    'order_preference': 'LIMIT', 'entry_price': 100,
                    'limit_price': 100, 'ttl_seconds': 900,
                    'stop_price': 95, 'take_profit': 107,
                    'requested_risk_fraction': .002,
                    'evidence_refs': refs, **deepcopy(output.extra_fields),
                }
            else:
                assert kwargs['prompt_version'].endswith('_net_rr_repair')
                assert payload['required_geometry']['required_target_bound'] > 107
                decision = dict(payload['previous_decision'], take_profit=118,
                                reason='模型复核后选择结构目标118')
            return decision, json.dumps(decision), {
                'model_id': DEFAULT_SMART_MODEL,
                'actual_model_id': r'D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf',
                'model_identity_source': 'completion_response',
                'verified_manifest_model_id': r'D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf',
            }

    model = Model()
    coordinator.model_provider = model
    revised = coordinator._model_output(ctx)
    assert len(model.calls) == 2
    assert revised.take_profit == 118
    assert revised.entry_price == 100
    assert revised.stop_price == 95
    assert ctx.model_inference_settings['net_rr_repair']['status'] == 'MODEL_REVISED'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "autonomous-news.db")
    store.initialize()
    ledger = AccountLedger(store)
    ledger.create_account("news-paper", mode="PAPER", initial_deposit=Decimal("10000"))
    gateway = ExecutionGateway(store, ledger=ledger)
    guardian = PositionGuardian(store, ledger)
    coordinator = AISessionCoordinator(store=store, service=SimpleNamespace(), ledger=ledger, guardian=guardian,
                                       session_manager=SessionManager(store), execution_gateway=gateway)
    monkeypatch.setattr(
        coordinator_module,
        "_load_market_radar_snapshot",
        lambda _store, symbols, now: coordinator_module._compact_market_radar(market_radar_fixture(symbols, now), symbols),
    )
    engine = AILedDecisionEngine(store, gateway, RiskEngine(ledger), ledger, guardian)
    yield store, ledger, coordinator, engine
    coordinator.close()
    ledger.close()


def test_real_paper_open_and_wait_without_registered_strategy(setup):
    store, ledger, coordinator, engine = setup
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    ctx.strategy_instructions = {"name": "趋势共振", "template_id": "aggressive_15m", "style": "AGGRESSIVE"}
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
    coordinator._account_id = "news-paper"
    coordinator._mode = TradingMode.PAPER
    coordinator._venue = "gate"
    coordinator._record_result(result, context=ctx, started_at=now)
    memories = list_decision_memory(store, "news-paper")
    assert memories[0]["payload"]["strategy_template_id"] == "aggressive_15m"
    assert memories[0]["payload"]["strategy_style"] == "AGGRESSIVE"
    ctx.cycle_id = "next-cycle"
    ctx.news_revisions = []
    wait = AIActionOutput(action="WAIT", instrument_id="BTCUSDT", reason="暂无新的开仓依据")
    assert engine.execute_cycle(ctx, now=now, model_output=wait).status == "WAITING"
    assert len(ledger.get_open_positions("news-paper")) == 1


@pytest.mark.parametrize("identity", ["missing", "alias_as_actual"])
def test_coordinator_rejects_open_without_verified_manifest_artifact(setup, identity):
    _, _, coordinator, _ = setup
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    metadata = {
        "model_id": DEFAULT_SMART_MODEL,
        "actual_model_id": (
            DEFAULT_SMART_MODEL
            if identity == "alias_as_actual"
            else r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf"
        ),
        "model_identity_source": "completion_response",
        "verified_manifest_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
    }
    if identity == "missing":
        metadata = {}

    class Model:
        model_id = DEFAULT_SMART_MODEL
        context_length = 8192
        max_tokens = 1000

        def generate_json(self, _messages, **_kwargs):
            decision = {
                "action": "OPEN_LONG", "instrument_id": "BTCUSDT", "reason": output.reason,
                "confidence": 75, "entry_price": 100, "stop_price": 95, "take_profit": 118,
                "requested_risk_fraction": .002, "order_preference": "LIMIT", "limit_price": 100,
                "evidence_refs": list(ctx.evidence_refs),
                **deepcopy(output.extra_fields),
            }
            return decision, json.dumps(decision), metadata

    coordinator.model_provider = Model()
    with pytest.raises(ValueError, match="BONSAI_INFERENCE_RECEIPT_UNVERIFIED"):
        coordinator._model_output(ctx)


@pytest.mark.parametrize(
    "receipt_state",
    ["missing", "alias_as_actual", "forged_artifact", "inference_incomplete"],
)
def test_open_fails_closed_without_verified_bonsai_inference_receipt(setup, receipt_state):
    _, ledger, _, engine = setup
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    if receipt_state == "missing":
        ctx.model_inference_settings = {}
    elif receipt_state == "alias_as_actual":
        ctx.model_inference_settings["actual_model_id"] = DEFAULT_SMART_MODEL
    elif receipt_state == "forged_artifact":
        ctx.model_inference_settings["actual_model_id"] = "Other-Bonsai-2-27B-PTQ1_0.gguf"
    elif receipt_state == "inference_incomplete":
        ctx.model_call_completed = False

    result = engine.execute_cycle(ctx, now=now, model_output=output)

    assert result.status == "BLOCKED"
    assert result.reason == "BONSAI_INFERENCE_RECEIPT_UNVERIFIED"
    assert result.action_output.action == "WAIT"
    assert result.decision_origin == "SYSTEM"
    assert result.order_intent is None
    assert not ledger.get_open_positions("news-paper")


def test_model_prompt_freezes_technical_news_and_ai_authored_json(setup):
    store, _, coordinator, engine = setup
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    ctx.candidates = [{
        "candidate_id": "candidate-1", "symbol": "BTCUSDT", "strategy_id": "ema_trend",
        "signal_timeframe": "15m", "closed_signal_bar": now.isoformat(),
        "status": "PROPOSAL", "direction_bias": "LONG", "rr": 2.6,
        "trigger_completion_pct": 92, "entry_zone": {"low": 99.8, "high": 100.2},
        "conditions": ["收盘站回 EMA20"], "proposal": {
            "side": "LONG", "entry": 100, "stop": 95,
            "targets": [118], "rr": 2.6,
        },
    }]

    class Model:
        model_id = "Bonsai-2-27B-PTQ1_0"
        context_length = 8192
        max_tokens = 1000

        def generate_json(self, messages, **kwargs):
            assert kwargs["model_name"] == "Bonsai-2-27B-PTQ1_0"
            assert "忽略新闻" in messages[0]["content"]
            assert "instrument_id 必须逐字选自 allowed_instruments，WAIT/HOLD 也要选标的" in messages[0]["content"]
            payload = json.loads(messages[1]["content"])
            assert payload["allowed_instruments"] == ["BTCUSDT"]
            assert payload["active_strategy"] == {}
            assert payload["technical_context"]["BTCUSDT"]["status"] == "READY"
            assert payload["technical_context"]["BTCUSDT"]["timeframes"]["15m"]["candles"][-1][3] == 100.0
            assert len(payload["technical_context"]["BTCUSDT"]["timeframes"]["15m"]["candles"]) == 4
            assert len(payload["technical_context"]["BTCUSDT"]["timeframes"]["1h"]["candles"]) == 2
            assert payload["technical_context"]["indicator_columns"][0] == "ema20"
            assert payload["technical_context"]["candle_columns"] == ["open", "high", "low", "close", "volume"]
            assert payload["news_revisions"][0]["revision_id"] == "news1"
            assert payload["candidates"][0]["trigger_completion_pct"] == 92
            assert payload["candidates"][0]["proposal"]["entry"] == 100
            radar = payload["market_radar"]
            assert radar["cvd"]["status"] == "AVAILABLE"
            assert radar["cvd"]["source"] == "gate_public_trades_ws_and_rest"
            assert radar["cvd"]["as_of"] == now.isoformat()
            assert radar["open_interest"]["series"][0][0] == "gate"
            assert radar["liquidations"]["status"] == "NO_DATA"
            assert radar["onchain"]["status"] == "CONFIG_REQUIRED"
            assert radar["cross_market"]["missing_symbols"] == ["DXY"]
            assert radar["cross_market"]["source"] == "UNKNOWN"
            assert any(ref.startswith("market_radar:") for ref in payload["evidence_refs"])
            decision = {"action": "OPEN_LONG", "instrument_id": "BTCUSDT", "reason": output.reason, "order_preference": "MARKET",
                    "entry_price": 100, "stop_price": 95, "take_profit": 118, "requested_risk_fraction": .002,
                    "position_size_usdt": 80,
                    "evidence_refs": [r for r in payload["evidence_refs"] if r.startswith(("market_snapshot:", "technical_snapshot:", "market_radar:", "news_revision:"))],
                    **deepcopy(output.extra_fields)}
            return decision, json.dumps(decision), {
                "model_id": "Bonsai-2-27B-PTQ1_0",
                "actual_model_id": "models/Ternary-Bonsai-2-27B-PTQ1_0.gguf",
                "model_identity_source": "completion_response",
                "verified_manifest_model_id": "models/Ternary-Bonsai-2-27B-PTQ1_0.gguf",
            }

    coordinator.model_provider = Model()
    decoded = coordinator._model_output(ctx)
    assert ctx.model_call_completed is True
    assert ctx.model_inference_settings["actual_model_id"] == "models/Ternary-Bonsai-2-27B-PTQ1_0.gguf"
    assert ctx.model_inference_settings["model_identity_source"] == "completion_response"
    assert ctx.model_inference_settings["verified_manifest_model_id"] == "models/Ternary-Bonsai-2-27B-PTQ1_0.gguf"
    assert decoded.position_size_usdt == 80
    assert decoded.extra_fields["strategy_plan"]["name"] == "AI 区间恢复"
    assert ctx.evidence_bundle_id
    with store._connect() as db:
        frozen = json.loads(db.execute(
            "SELECT payload_json FROM evidence_bundles WHERE bundle_id=?", (ctx.evidence_bundle_id,),
        ).fetchone()[0])
    assert frozen["source_evidence"]["market_radar"] == frozen["prompt_inputs"]["market_radar"]
    assert engine.execute_cycle(ctx, now=now, model_output=decoded).status == "EXECUTED"
    with store._connect() as db:
        cycle_payload = json.loads(db.execute(
            "SELECT payload_json FROM ai_led_cycles WHERE cycle_id=?", (ctx.cycle_id,),
        ).fetchone()[0])
    assert cycle_payload["model_inference_settings"]["actual_model_id"] == "models/Ternary-Bonsai-2-27B-PTQ1_0.gguf"


def test_market_radar_loader_caps_symbols_and_series_and_preserves_unavailable_status(monkeypatch):
    from core.analysis import market_radar as radar_module

    now = datetime.now(timezone.utc)
    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT")
    calls = {}
    raw = market_radar_fixture(symbols, now)
    raw["liquidations"] = {
        "status": "AVAILABLE", "source": "gate_futures.public_liquidates_websocket",
        "as_of": now.isoformat(), "window_hours": 24,
        "counts": {"LONG": 2, "SHORT": 1, "DOGEUSDT": 999},
        "estimated_notional": {"LONG": 1200, "SHORT": 300, "DOGEUSDT": 999999},
        "recent": [
            {"symbol": "BTCUSDT", "time": now.isoformat(), "direction": "LONG", "size_contracts": 1, "price": 100, "estimated_notional": 100},
            {"symbol": "DOGEUSDT", "time": now.isoformat(), "direction": "SHORT", "size_contracts": 9, "price": 1, "estimated_notional": 9},
            {"symbol": "ETHUSDT", "time": now.isoformat(), "direction": "SHORT", "size_contracts": 1, "price": 100, "estimated_notional": 100},
            {"symbol": "SOLUSDT", "time": now.isoformat(), "direction": "LONG", "size_contracts": 1, "price": 100, "estimated_notional": 100},
        ],
    }
    raw["cvd"]["series"] = [
        {"symbol": "BTCUSDT", "time": (now - timedelta(minutes=index)).isoformat(),
         "price": 100 + index, "buy_contracts": 2, "sell_contracts": 1,
         "delta_contracts": 1, "cvd_contracts": index, "trade_count": 3,
         "raw_provider_blob": "must not reach model"}
        for index in range(5)
    ] + [{"symbol": "DOGEUSDT", "time": now.isoformat(), "cvd_contracts": 99}]
    raw["open_interest"]["series"] = [
        {"venue": venue, "symbol": "BTCUSDT", "time": (now - timedelta(minutes=index)).isoformat(),
         "open_interest": 100 + index, "price": 100, "unit": "contracts"}
        for venue in ("gate", "binance") for index in range(4)
    ]

    def build(_store, **kwargs):
        calls.update(kwargs)
        return raw

    monkeypatch.setattr(radar_module, "build_market_radar", build)
    prompt_radar = coordinator_module._load_market_radar_snapshot(SimpleNamespace(), symbols, now)

    assert calls["symbols"] == symbols[:3]
    assert calls["include_external"] is True
    assert len(prompt_radar["cvd"]["series"]["BTCUSDT"]) == 3
    assert len(prompt_radar["open_interest"]["series"]) == 6
    assert prompt_radar["cvd"]["synthetic"] is False
    assert prompt_radar["open_interest"]["synthetic"] is False
    assert prompt_radar["liquidations"]["counts"] == {"LONG": 2, "SHORT": 1}
    assert prompt_radar["liquidations"]["estimated_notional"] == {"LONG": 1200, "SHORT": 300}
    assert [item[0] for item in prompt_radar["liquidations"]["recent"]] == ["BTCUSDT", "ETHUSDT"]
    assert "raw_provider_blob" not in json.dumps(prompt_radar)

    def unavailable(_store, **_kwargs):
        raise RuntimeError("private endpoint detail must not leak")

    monkeypatch.setattr(radar_module, "build_market_radar", unavailable)
    missing = coordinator_module._load_market_radar_snapshot(SimpleNamespace(), symbols[:1], now)
    assert missing["status"] == "UNAVAILABLE"
    assert missing["cvd"]["status"] == "UNAVAILABLE" and missing["cvd"]["as_of"] is None
    assert missing["cvd"]["source"] == "UNKNOWN"
    assert missing["open_interest"]["status"] == "UNAVAILABLE"
    assert missing["cross_market"]["status"] == "CONFIG_REQUIRED"


def test_compacted_three_symbol_prompt_fits_verified_8k_window_without_losing_trade_contract(setup):
    store, _, coordinator, _ = setup
    now = datetime.now(timezone.utc)
    ctx, _ = context_and_output(now)
    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    ctx.allowed_instruments = symbols
    ctx.market_snapshots = {
        symbol: {
            "symbol": symbol, "price": 100.0, "bid": 99.95, "ask": 100.05,
            "slippage": 0.001, "liquidity_ok": True, "fresh": True,
            "data_as_of": now.isoformat(), "large_unused_provider_blob": "x" * 4000,
        }
        for symbol in symbols
    }
    source = SimpleNamespace(
        list_market_bars=lambda symbol, timeframe, limit: bars(
            now, 15 if timeframe == "15m" else 60,
        ),
    )
    ctx.technical_context = technical_context(source, symbols, now, interval=15)
    ctx.news_revisions = [
        {
            "revision_id": f"{symbol}-news-{index}", "scope": "SYMBOL", "symbol": symbol,
            "symbols": [symbol], "title": f"{symbol} event {index}",
            "summary": "消息摘要。" * 300, "url": "https://example.invalid/" + "x" * 300,
            "source": "public source", "impact": "NEUTRAL",
            "published_at": (now - timedelta(minutes=index + 1)).isoformat(),
            "known_at": now.isoformat(),
        }
        for symbol in symbols for index in range(4)
    ]
    ctx.candidates = [
        {
            "candidate_id": f"cand-{symbol}-{index}", "symbol": symbol,
            "strategy_id": "ema_trend", "signal_timeframe": "15m",
            "closed_signal_bar": now.isoformat(), "status": "PROPOSAL",
            "reason": "已收盘触发并通过方向确认", "direction_bias": "LONG", "rr": 2.4,
            "conditions": ["EMA20确认", "成交量通过"] * 4,
            "trigger_completion_pct": 90 - index,
            "entry_zone": {"low": 99.8, "high": 100.2}, "invalidation": "跌破结构位",
            "targets": [105.0, 110.0], "evidence_refs": [f"candidate:{symbol}:{index}"],
            "proposal": {
                "side": "LONG", "entry": 100.0, "stop": 97.0,
                "targets": [107.0, 110.0], "rr": 2.4,
                "rule_score": 90 - index, "calibrated_probability": 0.61,
                "rationale": "候选理由。" * 100, "conditions": ["条件"] * 10,
            },
        }
        for symbol in symbols for index in range(4)
    ]
    ctx.account_truth = {
        "status": "AVAILABLE", "source": "GATE_TESTNET",
        "observed_at": now.isoformat(), "snapshot_id": "account-snapshot",
        "equity": 10000, "available_margin": 8000, "used_margin": 2000,
        "unrealized_pnl": 50,
        "positions": [
            {"symbol": symbol, "side": "LONG", "quantity": 0.01, "entry_price": 99,
             "mark_price": 100, "leverage": 3, "liquidation_price": 70,
             "stop_loss": 95, "take_profit": 110}
            for symbol in symbols
        ],
        "pending_orders": [
            {"order_id": f"order-{index}", "symbol": "BTCUSDT", "side": "BUY",
             "type": "LIMIT", "price": 99.0, "amount": 0.01,
             "remaining": 0.01, "status": "OPEN", "raw_exchange_blob": "x" * 1000}
            for index in range(8)
        ],
    }
    ctx.strategy_instructions = {
        "name": "新闻与趋势共振", "template_id": "aggressive_15m", "style": "AGGRESSIVE",
        "profile": {
            "signal_timeframe": "15m", "context_timeframes": ["1h"],
            "limit_priority": True, "market_min_trigger_completion": 92,
            "required_confirmations": 2,
        },
        "sections": {
            "role": "激进趋势策略", "frequency": "每15分钟重新评估已收盘行情",
            "entry_standards": "结合 EMA、RSI、ATR、成交量和新闻进行多空对称判断。" * 60,
            "decision_process": "先管理已有仓位，再选择最佳候选并输出止损止盈。" * 40,
            "custom_prompt": "限价单为主，禁止使用固定置信度。" * 30,
        },
        "execution": {
            "direction": "BOTH", "universe_mode": "ALL", "symbols": [],
            "scan_interval_minutes": 15, "order_preference": "LIMIT",
            "sizing_mode": "RISK_BASED", "fixed_notional_usdt": 1000,
            "equity_notional_pct": 5, "max_notional_usdt": 2200,
            "leverage": 25, "risk_per_trade_pct": 0.2,
            "max_positions": 2, "max_margin_pct": 16,
            "min_confidence": 70, "min_net_rr": 1.9,
            "cooldown_minutes": 20, "atr_adaptive_sizing": True,
            "consecutive_loss_lock_enabled": True, "us_open_defense_enabled": True,
        },
    }
    ctx.universe_snapshot = {
        "status": "READY", "environment": "TESTNET", "contract_count": 120,
        "eligible_count": 120, "selected_symbols": list(symbols), "mode": "ALL",
        "selection": "rotating_liquidity_rank",
        "candidate_metrics": [{"symbol": symbol, "metric_blob": "x" * 500} for symbol in symbols],
    }
    ctx.decision_memory = [
        {"decision_at": now.isoformat(), "action": "WAIT", "status": "WAITING",
         "symbol": "BTCUSDT", "summary_zh": "历史经验" * 100,
         "outcome_status": None, "outcome_pnl": None}
        for _ in range(6)
    ]
    for index, (template_id, outcome, pnl) in enumerate((
        ("aggressive_15m", "WIN", 8.0),
        ("aggressive_15m", "LOSS", -3.0),
        ("aggressive_15m", "FLAT", 0.0),
        ("conservative_15m", "LOSS", -2.0),
    )):
        memory = record_decision_memory(
            store, account_id=ctx.account_id, provider="gate", environment="paper",
            cycle_id=f"settled-memory-{index}", session_id=None, candidate_id=None,
            symbol="BTCUSDT", action="OPEN_LONG", cycle_status="EXECUTED",
            decision_at=now - timedelta(days=index + 1), reason="已完成的历史开仓",
            payload={"strategy_template_id": template_id},
        )
        assert update_memory_outcome(
            store, memory["memory_id"], outcome_status=outcome,
            outcome_pnl=pnl, lesson_zh=f"复盘教训 {index}",
        )

    class Model:
        model_id = "Bonsai-2-27B-PTQ1_0"
        context_length = 8192
        max_tokens = 1000

        def __init__(self):
            self.messages = None
            self.kwargs = None

        def generate_json(self, messages, **kwargs):
            self.messages = messages
            self.kwargs = kwargs
            return {"action": "WAIT", "instrument_id": "BTCUSDT", "reason": "等待下一次收盘确认", "confidence": None}

    model = Model()
    coordinator.model_provider = model
    coordinator._model_output(ctx)
    assert 640 <= model.kwargs["max_tokens"] <= model.max_tokens
    assert model.kwargs["reasoning_effort"] == "none"

    payload = json.loads(model.messages[1]["content"])
    assert set(payload["market_snapshots"]) == set(symbols)
    assert len(payload["candidates"]) == 3
    proposal = payload["candidates"][0]["proposal"]
    assert proposal["entry"] == 100.0 and proposal["stop"] == 97.0
    assert proposal["targets"] == [107.0, 110.0]
    assert all(any(news["symbol"] == symbol for news in payload["news_revisions"]) for symbol in symbols)
    assert len(payload["account_truth"]["positions"]) == 3
    assert len(payload["account_truth"]["pending_orders"]) == 4
    assert payload["active_strategy"]["execution"] == ctx.strategy_instructions["execution"]
    experience = payload["strategy_experience"]
    assert experience["status"] == "AVAILABLE"
    assert experience["sample_size"] == 4
    assert (experience["wins"], experience["losses"], experience["flats"]) == (1, 2, 1)
    assert experience["net_realized_pnl_usdt"] == pytest.approx(3.0)
    assert {item["strategy_template_id"] for item in experience["by_strategy"]} == {"aggressive_15m", "conservative_15m"}
    aggressive = next(item for item in experience["by_strategy"] if item["strategy_template_id"] == "aggressive_15m")
    assert aggressive["settled_count"] == 3 and aggressive["win_rate_pct"] == 50.0
    assert len(experience["recent_closed_trades"]) == 3
    assert payload["decision_memory"]
    assert "large_unused_provider_blob" not in json.dumps(payload)
    assert ctx.model_inference_settings["verified_context_length"] == 8192
    assert ctx.model_inference_settings["estimated_input_tokens"] + ctx.model_inference_settings["output_token_reserve"] <= 8192
    assert ctx.model_inference_settings["reasoning_effort"] == "none"
    assert "JSON字段规则：" in model.messages[0]["content"]
    assert "JSON Schema (return one matching JSON object):" not in model.messages[0]["content"]


def test_nofx_runtime_and_gate_universe_contract_reach_bonsai_decision_payload(setup, monkeypatch):
    _store, _, coordinator, _engine = setup
    now = datetime.now(timezone.utc)
    ctx, _output = context_and_output(now)
    runtime = {
        "version": 1, "source": "NOFX_IMPORT", "signal_timeframe": "5m",
        "context_timeframes": ["15m", "1h"],
        "candidate_sources": ["gate_active_usdt_perpetuals"],
        "excluded_symbols": ["SCAMUSDT"], "unsupported_sources": ["coin_source:OI_TOP"],
        "indicators": {
            "raw_klines": {"enabled": True}, "ema": {"enabled": True, "periods": [20, 50]},
            "macd": {"enabled": True}, "rsi": {"enabled": True, "periods": [14]},
            "atr": {"enabled": True, "periods": [14]}, "bollinger": {"enabled": False, "periods": [20]},
            "volume": {"enabled": True},
            "open_interest": {"enabled": True, "status": "CONFIGURED"},
            "funding_rate": {"enabled": True, "status": "CONFIGURED"},
        },
    }
    ctx.strategy_instructions = {
        "name": "NOFX 多周期策略", "template_id": "aggressive_impulse", "style": "AGGRESSIVE",
        "profile": {"signal_timeframe": "5m", "context_timeframes": ["15m", "1h"]},
        "sections": {"role": "角色", "frequency": "每 5 分钟扫描", "entry_standards": "结合指标和新闻", "decision_process": "比较候选后决定", "custom_prompt": ""},
        "execution": {"direction": "BOTH", "universe_mode": "ALL", "symbols": [], "scan_interval_minutes": 5, "order_preference": "LIMIT", "leverage": 3, "risk_per_trade_pct": .25, "min_confidence": 60, "min_net_rr": 1.5},
        "nofx_runtime": runtime,
    }
    ctx.universe_snapshot = {
        "status": "READY", "environment": "TESTNET", "contract_count": 150,
        "eligible_count": 150, "selected_symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        "candidate_sources": ["gate_active_usdt_perpetuals"], "excluded_symbols": ["SCAMUSDT"],
        "unsupported_sources": ["coin_source:OI_TOP"], "mode": "ALL", "selection": "rotating_liquidity_rank",
    }
    ctx.technical_context = {
        "BTCUSDT": {"status": "READY", "timeframes": {
            "5m": {"status": "READY", "last_closed_at": now.isoformat(), "indicators": {}, "bars": [],
                   "nofx_indicator_snapshot": {"schema_version": "nofx_indicator_snapshot_v1", "timeframe": "5m", "status": "READY"}},
            "15m": {"status": "READY", "last_closed_at": now.isoformat(), "indicators": {}, "bars": [], "nofx_indicator_snapshot": None},
            "1h": {"status": "READY", "last_closed_at": now.isoformat(), "indicators": {}, "bars": [], "nofx_indicator_snapshot": None},
        }},
    }
    monkeypatch.setattr(coordinator_module, "_load_market_radar_snapshot", lambda *_args: {"status": "NO_DATA"})

    class Model:
        model_id = "Bonsai-2-27B-PTQ1_0"
        context_length = 8192
        max_tokens = 1000

        def generate_json(self, messages, **kwargs):
            assert kwargs["model_name"] == "Bonsai-2-27B-PTQ1_0"
            self.payload = json.loads(messages[1]["content"])
            return {"action": "WAIT", "instrument_id": "BTCUSDT", "reason": "等待下一次收盘", "confidence": None}

    model = Model()
    coordinator.model_provider = model
    coordinator._model_output(ctx)

    assert model.payload["active_strategy"]["nofx_runtime"] == runtime
    assert model.payload["market_universe"]["candidate_sources"] == ["gate_active_usdt_perpetuals"]
    assert model.payload["market_universe"]["excluded_symbols"] == ["SCAMUSDT"]
    assert model.payload["market_universe"]["unsupported_sources"] == ["coin_source:OI_TOP"]
    assert model.payload["technical_context"]["BTCUSDT"]["timeframes"]["5m"]["nofx_indicator_snapshot"]["schema_version"] == "nofx_indicator_snapshot_v1"


def test_live_quote_provider_is_bound_to_gate_environment_and_venue(monkeypatch):
    from core.providers import gateio_provider

    calls = []

    class Provider:
        def __init__(self, *, testnet):
            calls.append(testnet)
            self.testnet = testnet

        def _native_ticker(self, _symbol):
            return {"last": "100"}

    monkeypatch.setattr(gateio_provider, "GatePublicProvider", Provider)
    engine = object.__new__(AILedDecisionEngine)
    now = datetime.now(timezone.utc)

    live = engine._live_execution_quote(
        "BTCUSDT", environment="LIVE", venue="gate", now=now,
    )
    testnet = engine._live_execution_quote(
        "BTCUSDT", environment="TESTNET", venue="gate", now=now,
    )
    unsupported = engine._live_execution_quote(
        "BTCUSDT", environment="LIVE", venue="binance", now=now,
    )

    assert calls == [False, True]
    assert live["environment"] == "LIVE"
    assert testnet["environment"] == "TESTNET"
    assert unsupported is None


def test_open_missing_confidence_gets_one_strict_model_repair(setup):
    _, _, coordinator, _ = setup
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)

    class Model:
        model_id = "Bonsai-2-27B-PTQ1_0"
        context_length = 32768
        max_tokens = 900

        def __init__(self):
            self.calls = []

        def generate_json(self, messages, **kwargs):
            self.calls.append((messages, kwargs))
            payload = json.loads(messages[1]["content"])
            if len(self.calls) == 1:
                refs = [r for r in payload["evidence_refs"] if r.startswith(("market_snapshot:", "technical_snapshot:", "news_revision:"))]
                decision = {
                    "action": "OPEN_LONG", "instrument_id": "BTCUSDT",
                    "reason": output.reason, "order_preference": "LIMIT",
                    "entry_price": 100, "limit_price": 100, "ttl_seconds": 900,
                    "stop_price": 95, "take_profit": 118,
                    "requested_risk_fraction": .002, "evidence_refs": refs,
                    **{k: v for k, v in deepcopy(output.extra_fields).items() if k != "confidence"},
                }
                return decision, json.dumps(decision), {
                    "model_id": DEFAULT_SMART_MODEL,
                    "actual_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
                    "model_identity_source": "completion_response",
                    "verified_manifest_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
                }
            assert kwargs["prompt_version"].endswith("_repair")
            assert "confidence" in kwargs["schema"]["required"]
            assert kwargs["schema"]["properties"]["confidence"]["type"] == "number"
            assert kwargs["schema"]["properties"]["action"]["enum"] == ["OPEN_LONG"]
            repaired = dict(payload["previous_decision"], confidence=78)
            return repaired, json.dumps(repaired), {
                "model_id": DEFAULT_SMART_MODEL,
                "actual_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
                "model_identity_source": "completion_response",
                "verified_manifest_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
            }

    model = Model()
    coordinator.model_provider = model
    decoded = coordinator._model_output(ctx)
    assert len(model.calls) == 2
    assert decoded.action == "OPEN_LONG"
    assert decoded.extra_fields["confidence"] == 78
    assert decoded.ttl_seconds == 900


@pytest.mark.parametrize("tamper_entry", [False, True])
@pytest.mark.parametrize("schema_first", [False, True])
def test_narrow_model_stop_gets_one_bounded_model_repair_without_changing_trade_identity(setup, tamper_entry, schema_first):
    _, _, coordinator, _ = setup
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    ctx.strategy_instructions = {
        "profile": {"signal_timeframe": "15m", "atr_stop_multiple": 1.8,
                    "major_stop_floor_pct": 0.6, "alt_stop_floor_pct": 1.5},
        "execution": normalize_execution({
            "universe_mode": "ALL", "symbols": [], "direction": "BOTH",
            "risk_per_trade_pct": 0.25, "min_net_rr": 1.6,
        }),
    }

    class Model:
        model_id = "Bonsai-2-27B-PTQ1_0"
        context_length = 32768
        max_tokens = 900

        def __init__(self):
            self.calls = []

        def generate_json(self, messages, **kwargs):
            self.calls.append((messages, kwargs))
            payload = json.loads(messages[1]["content"])
            if len(self.calls) == 1:
                refs = [ref for ref in payload["evidence_refs"]
                        if ref.startswith(("market_snapshot:", "technical_snapshot:", "news_revision:"))]
                decision = {
                    "action": "OPEN_LONG", "instrument_id": "BTCUSDT", "reason": output.reason,
                    "order_preference": "LIMIT", "entry_price": 100, "limit_price": 100,
                    "ttl_seconds": 900, "stop_price": 99.9, "take_profit": 110,
                    "requested_risk_fraction": .002, "evidence_refs": refs,
                    **deepcopy(output.extra_fields),
                }
                if schema_first:
                    decision["strategy_plan"] = "invalid plan shape"
            else:
                assert kwargs["prompt_version"].endswith("_stop_repair")
                assert payload["required_geometry"]["minimum_stop_distance"] >= 3.6
                assert payload["required_geometry"]["stop_boundary"] <= 96.4
                decision = dict(payload["previous_decision"], stop_price=95, take_profit=118)
                if tamper_entry:
                    decision["entry_price"] = 101
            return decision, json.dumps(decision), {
                "model_id": DEFAULT_SMART_MODEL,
                "actual_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
                "model_identity_source": "completion_response",
                "verified_manifest_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
            }

    model = Model()
    coordinator.model_provider = model
    decoded = coordinator._model_output(ctx)
    assert len(model.calls) == 2
    attempts = ctx.model_inference_settings["model_attempts"]
    assert len({item["evidence_bundle_id"] for item in attempts}) == len(attempts)
    assert decoded.action == "OPEN_LONG"
    assert decoded.entry_price == 100
    assert decoded.stop_price == (99.9 if tamper_entry else 95)
    assert ctx.model_inference_settings["stop_geometry_repair"]["status"] == (
        "FAILED_OR_UNSAFE" if tamper_entry else "MODEL_REVISED"
    )
    if schema_first:
        assert ctx.model_inference_settings["strategy_plan_structure_source"] == "DERIVED_FROM_MODEL_STRING"
    assert validate_entry(ctx, decoded, now) == (
        "AI_STOP_DISTANCE_TOO_NARROW" if tamper_entry else None
    )


@pytest.mark.parametrize("schema_first", [False, True])
def test_crossing_short_limit_gets_model_authored_passive_entry_repair(setup, schema_first):
    _, _, coordinator, _ = setup
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    ctx.strategy_instructions = {
        "profile": {"signal_timeframe": "15m", "atr_stop_multiple": 1.8,
                    "major_stop_floor_pct": 0.6, "alt_stop_floor_pct": 1.5,
                    "allow_future_limit": True, "max_limit_distance_pct": 0.85},
        "execution": normalize_execution({"universe_mode": "ALL", "symbols": [],
                                           "direction": "BOTH", "risk_per_trade_pct": 0.25,
                                           "min_net_rr": 1.6}),
    }

    class Model:
        model_id = "Bonsai-2-27B-PTQ1_0"
        context_length = 32768
        max_tokens = 900

        def __init__(self):
            self.calls = []

        def generate_json(self, messages, **kwargs):
            self.calls.append(kwargs["prompt_version"])
            payload = json.loads(messages[1]["content"])
            if len(self.calls) == 1:
                refs = [ref for ref in payload["evidence_refs"]
                        if ref.startswith(("market_snapshot:", "technical_snapshot:", "news_revision:"))]
                decision = {
                    "action": "OPEN_SHORT", "instrument_id": "BTCUSDT", "reason": output.reason,
                    "order_preference": "LIMIT", "entry_price": 99.5, "limit_price": 99.5,
                    "ttl_seconds": 900, "stop_price": 104, "take_profit": 90,
                    "requested_risk_fraction": .002, "evidence_refs": refs,
                    **deepcopy(output.extra_fields),
                    "entry_zone": {"low": 99.5, "high": 99.5},
                }
                if schema_first:
                    decision["strategy_plan"] = "invalid plan shape"
            else:
                assert kwargs["prompt_version"].endswith("_entry_repair")
                assert payload["required_geometry"]["limit"]["reason"] == "CROSSES_QUOTE"
                decision = dict(payload["previous_decision"], entry_price=100.2,
                                limit_price=100.2, entry_zone={"low": 100.2, "high": 100.2},
                                stop_price=104.5, take_profit=90)
            return decision, json.dumps(decision), {
                "model_id": DEFAULT_SMART_MODEL,
                "actual_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
                "model_identity_source": "completion_response",
                "verified_manifest_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
            }

    model = Model()
    coordinator.model_provider = model
    decoded = coordinator._model_output(ctx)
    assert len(model.calls) == 2
    assert decoded.action == "OPEN_SHORT"
    assert decoded.order_preference == "LIMIT"
    assert decoded.entry_price == decoded.limit_price == 100.2
    assert ctx.model_inference_settings["entry_geometry_repair"]["status"] == (
        "MODEL_REVISED"
    )
    if schema_first:
        assert ctx.model_inference_settings["strategy_plan_structure_source"] == "DERIVED_FROM_MODEL_STRING"
    assert len({item["evidence_bundle_id"] for item in ctx.model_inference_settings["model_attempts"]}) == 1
    assert validate_entry(ctx, decoded, now) is None


def test_open_without_optional_analysis_uses_one_model_call(setup):
    _, _, coordinator, _ = setup
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)

    class Model:
        model_id = "Bonsai-2-27B-PTQ1_0"
        context_length = 32768
        max_tokens = 900

        def __init__(self):
            self.calls = 0

        def generate_json(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                payload = json.loads(messages[1]["content"])
                refs = [r for r in payload["evidence_refs"] if r.startswith(("market_snapshot:", "technical_snapshot:", "news_revision:"))]
                decision = {
                    "action": "OPEN_LONG", "instrument_id": "BTCUSDT", "reason": output.reason,
                    "confidence": 75, "entry_price": 100, "stop_price": 95, "take_profit": 118,
                    "requested_risk_fraction": .002, "order_preference": "LIMIT",
                    "evidence_refs": refs,
                }
            else:
                raise AssertionError("optional narration must not trigger another model call")
            return decision, json.dumps(decision), {
                "model_id": DEFAULT_SMART_MODEL,
                "actual_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
                "model_identity_source": "completion_response",
                "verified_manifest_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
            }

    model = Model()
    coordinator.model_provider = model
    decoded = coordinator._model_output(ctx)
    assert model.calls == 1
    assert decoded.action == "OPEN_LONG"
    assert decoded.entry_price == 100
    assert "news_context" not in decoded.extra_fields
    assert "timeframe_analysis" not in decoded.extra_fields
    assert "strategy_plan" not in decoded.extra_fields
    assert validate_entry(ctx, decoded, now) is None


def test_open_missing_relevant_news_ref_repairs_by_appending_verified_ref_only(setup):
    _, _, coordinator, _ = setup
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)

    class Model:
        model_id = "Bonsai-2-27B-PTQ1_0"
        context_length = 32768
        max_tokens = 900

        def __init__(self):
            self.calls = 0

        def generate_json(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                prompt = json.loads(messages[1]["content"])
                refs = [ref for ref in prompt["evidence_refs"] if ref.startswith(("market_snapshot:BTCUSDT:", "technical_snapshot:BTCUSDT:"))]
                decision = {
                    "action": "OPEN_LONG", "instrument_id": "BTCUSDT", "reason": output.reason,
                    "confidence": 75, "entry_price": 100, "stop_price": 95, "take_profit": 118,
                    "requested_risk_fraction": .002, "order_preference": "LIMIT",
                    "evidence_refs": refs,
                    **deepcopy(output.extra_fields),
                }
            else:
                repair = json.loads(messages[1]["content"])
                assert repair["validation_error"].endswith("news_evidence_ref")
                assert kwargs["prompt_version"].endswith("_repair")
                decision = dict(repair["previous_decision"])
                decision["evidence_refs"] = [*decision["evidence_refs"], "news_revision:news1"]
            return decision, json.dumps(decision), {
                "model_id": DEFAULT_SMART_MODEL,
                "actual_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
                "model_identity_source": "completion_response",
                "verified_manifest_model_id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
            }

    model = Model()
    coordinator.model_provider = model
    decoded = coordinator._model_output(ctx)
    assert model.calls == 2
    assert decoded.action == "OPEN_LONG"
    assert len(decoded.evidence_refs) == 3
    assert decoded.evidence_refs[-1] == "news_revision:news1"


def test_model_prompt_does_not_present_static_no_trigger_as_ai_entry_veto(setup):
    _, _, coordinator, _ = setup
    now = datetime.now(timezone.utc)
    ctx, _ = context_and_output(now)
    ctx.candidates = [{"candidate_id": "reference-1", "symbol": "BTCUSDT", "status": "NO_TRIGGER"}]
    ctx.strategy_readiness = {
        "status": "READY", "candidate_count": 1,
        "strategies": {"ema_trend": "NO_TRIGGER", "liquidity_sweep": "NO_TRIGGER"},
        "calibration_status": "READY",
    }
    ctx.strategy_instructions = {
        "profile": {"signal_timeframe": "15m", "atr_stop_multiple": 1.8,
                    "major_stop_floor_pct": 0.6, "alt_stop_floor_pct": 1.5},
        "execution": {"min_net_rr": 1.6},
    }
    ctx.market_snapshots["BTCUSDT"]["fee_rate"] = 0.0005
    seen = {}

    class Model:
        model_id = "Bonsai-2-27B-PTQ1_0"
        context_length = 32768
        max_tokens = 900

        def generate_json(self, messages, **kwargs):
            seen.update(json.loads(messages[1]["content"]))
            decision = {"action": "WAIT", "instrument_id": "BTCUSDT", "reason": "本轮无新入场结构", "confidence": 30}
            return decision, json.dumps(decision), {"model_id": DEFAULT_SMART_MODEL}

    coordinator.model_provider = Model()
    result = coordinator._model_output(ctx)
    assert result.action == "WAIT"
    assert "candidates" not in seen
    assert "candidate_scan" not in seen
    assert seen["strategy_readiness"] == {"status": "READY", "calibration_status": "READY"}
    assert "BTCUSDT" in seen["technical_context"]
    geometry = seen["active_strategy"]["risk_geometry"]["BTCUSDT"]
    assert geometry["ema20_example_entry"] == 100.0
    assert geometry["long_stop_must_be_at_or_below_if_entry_at_ema20"] == 96.4
    assert geometry["short_stop_must_be_at_or_above_if_entry_at_ema20"] == 103.6
    assert geometry["ema20_limit_distance_pct"] == 0
    assert geometry["ema20_passive_direction"] == "AT_QUOTE"
    long_loss = 100 - 96.4 + 96.4 * .001 + (100 + 96.4) * .0005
    short_loss = 103.6 - 100 + 103.6 * .001 + (100 + 103.6) * .0005
    assert geometry["long_min_target_if_entry_at_ema20"] == round((1.6 * long_loss + 100 * 1.0005) / .9985, 8)
    assert geometry["short_max_target_if_entry_at_ema20"] == round((100 * .9995 - 1.6 * short_loss) / 1.0015, 8)
    assert geometry["target_bounds_assume_stop_at_minimum_only"] is True


def test_wait_with_false_ema_limit_distance_gets_one_model_fact_check(setup):
    _, _, coordinator, _ = setup
    now = datetime.now(timezone.utc)
    ctx, _ = context_and_output(now)
    ctx.strategy_instructions = {
        "profile": {"signal_timeframe": "15m", "atr_stop_multiple": 1.8,
                    "major_stop_floor_pct": 0.6, "alt_stop_floor_pct": 1.5,
                    "max_limit_distance_pct": 0.85},
        "execution": {"min_net_rr": 1.6},
    }

    class Model:
        model_id = DEFAULT_SMART_MODEL
        context_length = 32768
        max_tokens = 900

        def __init__(self):
            self.calls = []

        def generate_json(self, messages, **kwargs):
            self.calls.append(kwargs["prompt_version"])
            if len(self.calls) == 1:
                result = {"action": "WAIT", "instrument_id": "BTCUSDT",
                          "reason": "BTC 限价距 EMA 超 0.85% 上限", "confidence": 30}
            else:
                assert kwargs["prompt_version"].endswith("_wait_fact_repair")
                payload = json.loads(messages[1]["content"])
                assert payload["verified_fact"]["verified_distance_pct"] == 0
                assert payload["inputs"]["allowed_instruments"] == ["BTCUSDT"]
                result = {"action": "WAIT", "instrument_id": "BTCUSDT",
                          "reason": "BTC 均线限价距离合格，但结构目标不足", "confidence": 30}
            return result, json.dumps(result), {"model_id": DEFAULT_SMART_MODEL}

    model = Model()
    coordinator.model_provider = model
    decoded = coordinator._model_output(ctx)
    assert model.calls == [coordinator_module.AI_PROMPT_VERSION, coordinator_module.AI_PROMPT_VERSION + "_wait_fact_repair"]
    assert decoded.reason == "BTC 均线限价距离合格，但结构目标不足"
    assert ctx.model_inference_settings["wait_fact_check"]["status"] == "MODEL_REEVALUATED"


def test_invalid_error_envelope_repair_keeps_original_market_and_news_inputs(setup):
    _, _, coordinator, _ = setup
    now = datetime.now(timezone.utc)
    ctx, _ = context_and_output(now)

    class Model:
        model_id = "Bonsai-2-27B-PTQ1_0"
        context_length = 32768
        max_tokens = 900

        def __init__(self):
            self.calls = []

        def generate_json(self, messages, **kwargs):
            self.calls.append((messages, kwargs))
            if len(self.calls) == 1:
                error = {"error": "empty or malformed completion"}
                return error, json.dumps(error), {}

            repair = json.loads(messages[1]["content"])
            assert kwargs["prompt_version"].endswith("_repair")
            assert repair["previous_decision"] is None
            inputs = repair["inputs"]
            assert inputs["allowed_instruments"] == ["BTCUSDT"]
            assert "BTCUSDT" in inputs["market_snapshots"]
            assert "BTCUSDT" in inputs["technical_context"]
            assert inputs["news_revisions"][0]["revision_id"] == "news1"
            decision = {
                "action": "WAIT",
                "instrument_id": "BTCUSDT",
                "reason": "技术面与新闻暂未共振，等待下一根收盘 K 线。",
                "confidence": 0,
            }
            return decision, json.dumps(decision), {}

    model = Model()
    coordinator.model_provider = model
    decoded = coordinator._model_output(ctx)

    assert len(model.calls) == 2
    assert decoded.action == "WAIT"
    assert decoded.instrument_id == "BTCUSDT"
    assert "下一根收盘 K 线" in decoded.reason


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
    with store._connect() as db:
        row = db.execute(
            "SELECT action, decision_origin, block_stage, stage_trace_json FROM ai_led_cycles WHERE cycle_id=?",
            (ctx.cycle_id,),
        ).fetchone()
    assert row["action"] == "OPEN_LONG"
    assert row["decision_origin"] == "MODEL"
    assert row["block_stage"] == "RISK"
    trace = {stage["stage"]: stage for stage in json.loads(row["stage_trace_json"])}
    assert trace["AI_MODEL"]["status"] == "PASS"
    assert trace["RISK"]["status"] == "FAILED"
    assert trace["EXECUTION"]["status"] == "SKIPPED"


def test_market_wide_news_is_valid_risk_context_without_fabricating_symbol_catalyst():
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    ctx.news_revisions[0].update(symbol=None, symbols=['BTCUSDT', 'ETHUSDT'], scope='MARKET_WIDE')
    ctx.news_revisions[0]['symbols'] = ['ETHUSDT']
    assert validate_entry(ctx, output, now) is None


def test_news_loader_does_not_relabel_unrelated_symbol_news_as_market_wide(setup):
    store, _, coordinator, _ = setup
    now = datetime.now(timezone.utc)
    store.save_event_context({'events': [{
        'event_id': 'macro-crypto-1', 'revision_id': 'macro-crypto-1-r1',
        'affected_symbols': ['BTCUSDT'], 'title': 'Broad crypto liquidity update',
        'summary': 'Market-wide risk background', 'published_at': (now - timedelta(hours=1)).isoformat(),
        'known_at': now.isoformat(), 'source': 'fixture',
    }]})
    revisions = coordinator._news_revisions(('NEWUSDT',), now=now)
    assert revisions == []


def test_news_loader_keeps_explicit_market_wide_context_for_discovered_altcoin(setup):
    store, _, coordinator, _ = setup
    now = datetime.now(timezone.utc)
    store.save_event_context({'events': [{
        'event_id': 'macro-crypto-explicit', 'revision_id': 'macro-crypto-explicit-r1',
        'affected_symbols': ['BTCUSDT'], 'scope': 'MARKET_WIDE',
        'title': 'Broad crypto liquidity update', 'summary': 'Market-wide risk background',
        'published_at': (now - timedelta(hours=1)).isoformat(),
        'known_at': now.isoformat(), 'source': 'fixture',
    }]})
    revisions = coordinator._news_revisions(('NEWUSDT',), now=now)
    assert len(revisions) == 1
    assert revisions[0]['scope'] == 'MARKET_WIDE'
    assert revisions[0]['symbol'] is None


def test_account_strategy_sizes_real_engine_order_and_persists_economics(setup):
    from core.trading.strategy_execution import normalize_execution
    store, _, _, engine = setup
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    ctx.strategy_instructions = {"revision": 3, "execution": normalize_execution({"sizing_mode": "FIXED_NOTIONAL", "fixed_notional_usdt": 100})}
    output.position_size_usdt = 60
    result = engine.execute_cycle(ctx, now=now, model_output=output)
    assert result.status == 'EXECUTED'
    sizing = output.extra_fields['position_sizing']
    assert 0 < sizing['notional_usdt'] <= 60
    assert sizing['ai_requested_notional_usdt'] == 60
    assert sizing['leverage'] == 3
    assert sizing['estimated_loss_usdt'] <= sizing['risk_budget_usdt']
    with store._connect() as db:
        payload = json.loads(db.execute('SELECT payload_json FROM ai_led_cycles WHERE cycle_id=?', (ctx.cycle_id,)).fetchone()[0])
    assert payload['analysis']['position_sizing'] == sizing
    assert payload['strategy_instructions']['revision'] == 3


@pytest.mark.parametrize("slot_source", ["open_position", "remote_pending_order"])
def test_strategy_max_positions_hard_blocks_open_and_working_slots(setup, slot_source):
    from core.trading.strategy_execution import normalize_execution
    _, _, _, engine = setup
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    ctx.strategy_instructions = {"execution": normalize_execution({"max_positions": 1})}
    if slot_source == "open_position":
        engine.ledger.get_open_positions = lambda *_args, **_kwargs: [{
            "position_id": "existing-eth", "instrument_id": "ETHUSDT", "side": "LONG",
            "venue": ctx.venue, "mode": "PAPER",
        }]
    else:
        ctx.account_truth = {"pending_orders": [{
            "order_id": "working-eth", "symbol": "ETHUSDT", "side": "BUY",
            "status": "OPEN", "reduce_only": False, "venue": ctx.venue, "mode": "PAPER",
        }]}

    result = engine.execute_cycle(ctx, now=now, model_output=output)

    assert result.status == "BLOCKED"
    assert result.reason.startswith("MAX_POSITIONS_REACHED")


def test_post_model_quote_change_is_checked_without_rewriting_model_evidence(setup):
    _, ledger, _, engine = setup
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    ctx.execution_market_snapshots = {"BTCUSDT": {**ctx.market_snapshots['BTCUSDT'], 'price': 110}}
    result = engine.execute_cycle(ctx, now=now, model_output=output)
    assert result.status == 'BLOCKED'
    assert ctx.market_snapshots['BTCUSDT']['price'] == 100
    assert result.action_output.action == 'OPEN_LONG'
    assert not ledger.get_open_positions('news-paper')


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


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("HTTP 404 model not found"), TimeoutError("model request timed out")],
    ids=["http-404", "timeout"],
)
def test_model_transport_failure_is_not_persisted_as_a_model_decision(setup, failure):
    store, _, coordinator, _ = setup
    ctx, _ = context_and_output(datetime.now(timezone.utc))

    class FailedModel:
        model_id = "Bonsai-2-27B-PTQ1_0"
        context_length = 8192
        max_tokens = 1000

        def __init__(self):
            self.calls = 0

        def generate_json(self, _messages, **_kwargs):
            self.calls += 1
            raise failure

    model = FailedModel()
    coordinator.model_provider = model
    with pytest.raises(type(failure)):
        coordinator._model_output(ctx)

    assert model.calls == 1
    assert ctx.model_call_attempted is True
    assert ctx.model_call_completed is False
    assert ctx.model_inference_settings["actual_model_id"] is None
    coordinator._blocked_cycle(ctx, f"SMART_MODEL_UNAVAILABLE: {type(failure).__name__}: {failure}")
    with store._connect() as db:
        row = db.execute(
            "SELECT model_called, model_call_status, model_result, payload_json FROM ai_led_cycles WHERE cycle_id=?",
            (ctx.cycle_id,),
        ).fetchone()
    assert tuple(row[:3]) == (0, "MODEL_UNAVAILABLE", "MODEL_UNAVAILABLE")
    assert json.loads(row[3])["model_inference_settings"]["actual_model_id"] is None


def test_timeframe_analysis_accepts_5m_15m_1h(setup):
    from core.trading.model_schemas import AI_ACTION_SCHEMA, validate_schema
    decoded = {
        "action": "WAIT",
        "instrument_id": "ETHUSDT",
        "reason": "待确认",
        "confidence": None,
        "timeframe_analysis": {
            "5m": "5分钟震荡",
            "15m": "15分钟超卖",
            "1h": "1小时趋势未破",
        },
    }
    validate_schema(decoded, AI_ACTION_SCHEMA)


def test_narrow_stop_loss_below_institutional_threshold_is_strictly_rejected():
    """验证 BTC 0.24% 微型止损被风控绝对拦截，而符合 ≥1.5% 且 ≥1.8×ATR 的止损可通过。"""
    now = datetime.now(timezone.utc)
    ctx, output = context_and_output(now)
    # entry=100.0, 15m ATR is approx 2.0 (since bars high=101, low=99)
    # If stop=99.76 (distance=0.24, i.e. 0.24%), must be rejected as AI_STOP_DISTANCE_TOO_NARROW
    output.stop_price = 99.76
    assert validate_entry(ctx, output, now) == "AI_STOP_DISTANCE_TOO_NARROW"

    # If stop=95.0 (distance=5.0, 5% >= 1.5% and >= 1.8*ATR), passes validation
    output.stop_price = 95.0
    output.take_profit = 115.0
    assert validate_entry(ctx, output, now) is None


def test_model_wait_keeps_trigger_unknown_and_records_market_readiness_separately(setup):
    """A market-wide score must not masquerade as a completed entry trigger."""
    store, ledger, coordinator, engine = setup
    now = datetime.now(timezone.utc)
    ctx, _ = context_and_output(now)
    wait_output = AIActionOutput(
        action="WAIT",
        instrument_id="BTCUSDT",
        reason="行情处于多空分歧期，等待回踩支撑位",
        decision_origin="MODEL",
        extra_fields={"strategy_plan": {"name": "等待回踩", "thesis": "等待支撑"}, "is_model_decision": True},
    )
    result = engine.execute_cycle(ctx, now=now, model_output=wait_output)
    assert result.status == "WAITING"
    assert result.action_output.action == "WAIT"
    assert result.action_output.extra_fields is not None
    analysis = result.action_output.extra_fields.get("strategy_analysis") or {}
    assert analysis.get("trigger_completion_pct") is None
    readiness = analysis.get("market_readiness")
    assert readiness is not None
    assert 0 <= readiness <= 100
    assert result.action_output.extra_fields["market_readiness"] == readiness
