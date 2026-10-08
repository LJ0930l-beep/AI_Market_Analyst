"""Independent acceptance of bounded PA comparisons and historical feed injection."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.trading import ai_session_coordinator as coordinator
from core.trading.ai_strategy_book import TEMPLATES
from core.trading.autonomous_strategy import NOFX_GATE_STRATEGY_FOCUS, build_strategy_system_prompt
from tests.prompt_evidence_helpers import decode_price_action
from tests.test_model_context_budget import _add_production_named_technical_evidence, _production_sized_prompt_payload


def _comparison_facts(pa: dict) -> dict:
    result = {key: pa[key] for key in ("status", "as_of", "source", "closed_bar_count", "prior_range") if key in pa}
    for name in ("bos", "sweep_reclaim", "breakout_retest"):
        event = pa.get(name)
        result[name] = ({key: event[key] for key in ("side", "state", "level", "bar_at", "confirmed_at", "invalidated_at") if key in event}
                        if isinstance(event, dict) else event)
    result["confirmed_swings"] = [{key: swing[key] for key in ("side", "price", "confirmed_at") if key in swing}
                                  for swing in pa.get("confirmed_swings", [])]
    return result


def _three_pa_payload() -> dict:
    payload = _production_sized_prompt_payload()
    _add_production_named_technical_evidence(payload)
    payload["active_strategy"] = {"template_id": "price_action_structure", "revision": 6, "execution": {"leverage": 100, "max_margin_pct": 18}}
    payload["evidence_refs"] = [f"{kind}:{symbol}-snapshot" for symbol in payload["allowed_instruments"]
                                for kind in ("market_snapshot", "technical_snapshot")]
    for index, symbol in enumerate(payload["allowed_instruments"]):
        frames = payload["technical_context"][symbol]["timeframes"]
        frames.pop("5m", None)
        for timeframe, frame in frames.items():
            frame["price_action"] = {
                "status": "READY", "as_of": frame["last_closed_at"], "source": "gate_native_rest:last", "closed_bar_count": 239,
                "confirmed_swings": [{"side": "HIGH", "price": 61250 + index, "pivot_at": "2026-09-21T11:15:00Z",
                                      "confirmed_at": "2026-09-21T11:45:00Z", "pivot_available_at": "2026-09-21T11:16:00Z"}],
                "prior_range": {"lookback_bars": 20, "high": 61300 + index, "low": 60700 + index, "excludes_latest_close": True},
                "bos": {"side": "LONG", "state": "ACTIVE", "level": 61250 + index,
                        "bar_at": "2026-09-21T12:00:00Z", "confirmed_at": "2026-09-21T12:01:00Z",
                        "pivot_at": "2026-09-21T11:15:00Z", "pivot_confirmed_at": "2026-09-21T11:45:00Z"},
                "sweep_reclaim": {"side": "SHORT", "state": "INVALIDATED", "level": 61300 + index,
                                  "bar_at": "2026-09-21T11:30:00Z", "confirmed_at": "2026-09-21T11:31:00Z",
                                  "invalidated_at": "2026-09-21T12:01:00Z", "pivot_at": "2026-09-21T11:00:00Z"},
                "breakout_retest": None,
            }
        payload["market_snapshots"][symbol]["contract_rules"] = {"order_size_min": 1, "quanto_multiplier": 0.0001, "leverage_max": 100}
        payload["market_snapshots"][symbol] = coordinator._compact_market_snapshot(payload["market_snapshots"][symbol])
    payload["account_truth"].update({
        "equity": 10000, "available_margin": 7500, "used_margin": 2500,
        "managed_positions": [{"position_id": "owned-position", "symbol": payload["allowed_instruments"][0],
                               "side": "LONG", "quantity": 1, "entry_price": 61000, "stop_price": 60500,
                               "take_profit": 62000, "ownership": "VERIFIED_SYSTEM"}],
        "owned_entry_orders": [{"order_id": "owned-entry", "symbol": payload["allowed_instruments"][0], "price": 60000,
                                "amount": 1, "remaining": 1, "ownership": "SYSTEM_ORDER_ID_MATCH"}],
        "owned_protection_orders": [{"order_id": "owned-stop", "trigger_price": 60500, "reduce_only": True}],
        "owned_reduction_orders": [{"order_id": "owned-reduce", "price": 62000, "remaining": 1, "reduce_only": True}],
    })
    return payload


def test_bounded_three_symbol_comparison_keeps_known_time_and_execution_facts() -> None:
    payload = _three_pa_payload()
    original = copy.deepcopy(payload)
    # A deterministic test counter makes the budget reproducible. Live acceptance
    # separately uses /tokenize; this unit test makes no network/inference request.
    counter = lambda content: (len(content) + 1) // 2
    projected, metadata = coordinator._fit_prompt_payload(payload, "系统规则" * 1100, 8192, reserve=1024,
        signal_timeframe="15m", token_counter=counter)
    assert projected["allowed_instruments"] == original["allowed_instruments"]
    assert metadata["visible_symbols"] == metadata["selected_symbols"] == original["allowed_instruments"]
    assert metadata["deferred_symbols"] == []
    assert metadata["visibility_reason"] == "ALL_SELECTED_SYMBOLS_VISIBLE"
    assert metadata["estimated_input_tokens"] + 1280 <= 8192
    assert any("price_action_comparison" in step for step in metadata["steps"])
    for symbol in projected["allowed_instruments"]:
        for timeframe in ("15m", "1h"):
            frame = projected["technical_context"][symbol]["timeframes"][timeframe]
            before = original["technical_context"][symbol]["timeframes"][timeframe]
            assert frame["status"] == "READY"
            assert frame["last_closed_at"] == before["last_closed_at"]
            assert frame["candles"][-1] == before["candles"][-1]
            actual_pa = decode_price_action(projected["technical_context"], frame["price_action"])
            assert _comparison_facts(actual_pa) == _comparison_facts(before["price_action"])
        for field in ("price", "bid", "ask", "fee_rate", "slippage", "contract_rules"):
            assert projected["market_snapshots"][symbol].get(field) == original["market_snapshots"][symbol].get(field)
    assert projected["evidence_refs"] == original["evidence_refs"]
    assert projected["account_truth"] == original["account_truth"]
    assert projected["active_strategy"] == original["active_strategy"]
    assert payload == original


def test_pruning_unused_price_action_time_cells_preserves_decoded_facts() -> None:
    technical = {"price_action_encoding": {"keys": {"c": "confirmed_at", "unused": "pivot_at"},
                    "times": ["unused-old", "known", "unused-new"], "defaults": {"status": "READY"},
                    "rule": "omitted fields inherit defaults; @N is times[N]."},
                 "BTCUSDT": {"timeframes": {"15m": {"price_action": {"as_of": "@1", "bos": {"c": "@1", "state": "ACTIVE"}}}}}}
    before = decode_price_action(technical, technical["BTCUSDT"]["timeframes"]["15m"]["price_action"])
    assert coordinator._prune_price_action_time_table(technical)
    assert technical["price_action_encoding"]["times"] == ["known"]
    assert technical["price_action_encoding"]["keys"] == {"c": "confirmed_at"}
    assert decode_price_action(technical, technical["BTCUSDT"]["timeframes"]["15m"]["price_action"]) == before
    technical["BTCUSDT"]["timeframes"]["15m"]["price_action"]["as_of"] = "@99"
    original = copy.deepcopy(technical)
    assert not coordinator._prune_price_action_time_table(technical)
    assert technical == original


def test_latency_target_cannot_sacrifice_symbols_that_fit_verified_window() -> None:
    payload = _three_pa_payload()
    counter = lambda content: (len(content) + 1) // 2
    with pytest.raises(ValueError, match="AI_INPUT_BUDGET_EXCEEDED"):
        coordinator._fit_prompt_payload(payload, "系统规则" * 1100, 7000, reserve=1024,
            signal_timeframe="15m", token_counter=counter, allow_symbol_deferral=False)
    fitted, metadata = coordinator._fit_prompt_payload(payload, "系统规则" * 1100, 8192, reserve=1024,
        signal_timeframe="15m", token_counter=counter)
    assert fitted["allowed_instruments"] == payload["allowed_instruments"]
    assert metadata["deferred_symbols"] == []


def test_verified_duplicate_lessons_keep_union_but_do_not_merge_conflicting_outcomes() -> None:
    trade = {"cycle_id": "closed-cycle", "memory_role": "VERIFIED_SETTLED_LESSON", "outcome_pnl": 2,
             "summary_zh": "交易结算摘要", "outcome_evidence": {"episode_id": "episode", "net_pnl_usdt": 2}}
    memory = {**copy.deepcopy(trade), "summary_zh": "另一个有用观察", "lesson_zh": "结构触发后检查成本"}
    conflict = {**copy.deepcopy(memory), "outcome_pnl": -2}
    payload = {"strategy_experience": {"recent_closed_trades": [trade]}, "decision_memory": [memory, conflict]}
    assert coordinator._deduplicate_settled_prompt_lessons(payload)
    assert payload["decision_memory"] == [conflict]
    assert trade["summary_zh"] == "交易结算摘要"
    assert trade["decision_summary_zh"] == "另一个有用观察"
    assert trade["lesson_zh"] == memory["lesson_zh"]
    assert trade["outcome_evidence"] == memory["outcome_evidence"]


def test_historical_radar_injection_is_copied_without_loading_current_feeds(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("historical input must not load current market feeds")
    monkeypatch.setattr(coordinator, "_load_market_radar_snapshot", forbidden)
    historical = {"status": "UNAVAILABLE", "as_of": "2025-01-01T00:00:00Z", "cross_market": {"status": "UNAVAILABLE"}}
    context = SimpleNamespace(allowed_instruments=("BTCUSDT",), market_radar_snapshot=historical)
    actual = coordinator._market_radar_for_cycle(object(), context, datetime(2025, 1, 1, tzinfo=timezone.utc))
    assert actual == historical
    actual["cross_market"]["status"] = "MUTATED"
    assert historical["cross_market"]["status"] == "UNAVAILABLE"


def test_production_radar_without_injection_keeps_existing_loader_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    expected = {"status": "AVAILABLE"}
    def loader(*args):
        calls.append(args)
        return expected
    monkeypatch.setattr(coordinator, "_load_market_radar_snapshot", loader)
    store = object()
    context = SimpleNamespace(allowed_instruments=("BTCUSDT",))
    at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert coordinator._market_radar_for_cycle(store, context, at) is expected
    assert calls == [(store, context.allowed_instruments, at)]


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda template: template["id"])
@pytest.mark.parametrize("cap_mode", ["PERCENT", "FIXED_USDT"])
def test_compact_system_keeps_five_strategy_focus_and_execution_boundaries(template: dict, cap_mode: str) -> None:
    instructions = {"template_id": template["id"], "name": template["name"], "profile": copy.deepcopy(template["profile"]),
                    "sections": copy.deepcopy(template["sections"]), "execution": {**template["execution_defaults"],
                    "margin_cap_mode": cap_mode, "max_margin_usdt": 123.45, "leverage": 100}}
    # This legacy authorization-bound prompt test explicitly opts into that
    # mode; built-in research defaults now use the actual venue leverage cap.
    instructions["execution"]["leverage_mode"] = "STRATEGY_LIMIT"
    system = build_strategy_system_prompt(instructions, nofx_gate=True)
    compact = coordinator._compact_gate_system_prompt(system, instructions)
    assert len(compact) < len(system)
    for key in ("entry_standards", "custom_prompt"):
        assert instructions["sections"][key].strip()[:900] in compact
    frequency = instructions["sections"]["frequency"].strip()[:900]
    if frequency != f"每 {template['scan_interval_minutes']} 分钟扫描一次；先管理已有持仓，再比较允许标的。":
        assert frequency in compact
    if template["id"] != "price_action_structure":
        assert NOFX_GATE_STRATEGY_FOCUS[template["id"]] in compact
    else:
        for anchor in ("swing", "BOS", "扫", "回测", "as_of", "bar_at", "confirmed_at", "prior_range"):
            assert anchor in compact
    for anchor in ("allowed_instruments", "instrument_id", "WAIT/HOLD", "OPEN_LONG/OPEN_SHORT", "reason", "confidence",
                   "entry_price", "stop_price", "take_profit", "position_size_usdt", "requested_leverage", "order_preference",
                   "evidence_refs", "LIMIT", "MARKET", "account_id", "mode", "account_truth", "contract_rules.leverage_max",
                   "用户授权100倍与Gate合约上限中的较低值", "包含全部远端持仓及未成交委托预占", "先管理已验证", "ownership=VERIFIED_SYSTEM",
                   "EXTERNAL_OR_UNVERIFIED", "禁止对其平仓、减仓或改保护", "CANCEL_ORDER", "Gate order_id", "撤单回读确认后下一轮",
                   "禁止管理外部订单", "始终要有止损和止盈", "UPDATE_PROTECTION", "position_id", "new_stop_price/new_take_profit",
                   "变更以 Gate 回执为准", "不凭固定分数强制等待", "没有新闻不等于没有技术机会", "现在提交 LIMIT",
                   "next_trigger_price", "entry_condition", "只能逐字选自输入", "全仓", "OPEN提案不是成交"):
        assert anchor in compact
    cap = "123.45 USDT" if cap_mode == "FIXED_USDT" else f"账户权益的 {instructions['execution']['max_margin_pct']}%"
    assert cap in compact
    assert f"扫描频率：{template['scan_interval_minutes']} 分钟" in compact


def test_unknown_system_contract_is_never_compacted() -> None:
    custom = "自定义策略焦点：等待当期成交量证据；禁止删除这段自定义条件。"
    assert coordinator._compact_gate_system_prompt(custom, {"template_id": "custom"}) == custom


def test_isolated_sft_sink_bypasses_production_collector_and_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.trading import fin_dataset_collector
    def forbidden(*_args, **_kwargs):
        raise AssertionError("isolated replay must not write production training data")
    monkeypatch.setattr(coordinator, "record_sft_sample", forbidden)
    monkeypatch.setattr(fin_dataset_collector, "app_data_paths", forbidden)
    instance = object.__new__(coordinator.AISessionCoordinator)
    captured = []
    instance.sft_sample_sink = lambda **sample: captured.append(sample)
    sample = {"cycle_id": "replay", "system_prompt": "system", "user_prompt": "{}", "model_output": {"action": "WAIT"}, "mode": "PAPER"}
    instance._record_sft_sample(**sample)
    assert captured == [sample]


def test_default_sft_sink_preserves_existing_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = []
    monkeypatch.setattr(coordinator, "record_sft_sample", lambda **sample: captured.append(sample))
    instance = object.__new__(coordinator.AISessionCoordinator)
    sample = {"cycle_id": "normal", "model_output": {"action": "WAIT"}}
    instance._record_sft_sample(**sample)
    instance.sft_sample_sink = None
    instance._record_sft_sample(**sample)
    assert captured == [sample, sample]
