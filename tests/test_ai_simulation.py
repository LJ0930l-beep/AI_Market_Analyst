from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.replay.ai_simulation import ReplayAccount


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
MARKET = {
    "instrument_id": "BTCUSDT",
    "price": 100.0,
    "contract_rules": {
        "contract_size": 1,
        "amount_step": 1,
        "price_tick": 0.01,
        "min_size": 1,
        "max_size": 10_000,
        "leverage_max": 20,
    },
}


def decision(*, action="OPEN_LONG", order_type="MARKET", quantity=1, **extra):
    result = {
        "action": action,
        "instrument_id": "BTCUSDT",
        "order_preference": order_type,
        "requested_leverage": 2,
        "quantity": quantity,
        "stop_price": 90,
        "take_profit": 120,
        "ttl_seconds": 3600,
        **extra,
    }
    if order_type == "LIMIT":
        result.setdefault("limit_price", 100)
    return result


def bar(start, *, minutes=1, open=100, high=101, low=99, close=100, volume=1000, **extra):
    return {
        "instrument_id": "BTCUSDT",
        "bar_start": start,
        "bar_end": start + timedelta(minutes=minutes),
        "open": open,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "synthetic": False,
        "contract_rules": MARKET["contract_rules"],
        **extra,
    }


def account(**kwargs):
    config = {
        "initial_equity": 1000,
        "maker_fee_rate": 0.001,
        "taker_fee_rate": 0.002,
        "slippage_bps": 10,
        "participation_rate": 1,
        "margin_cap_pct": 20,
        "account_id": "replay-test",
    }
    config.update(kwargs)
    return ReplayAccount(**config)


def test_limit_needs_later_closed_bar_and_strict_crossing():
    sim = account()
    accepted = sim.apply_decision(
        decision(order_type="LIMIT", limit_price=100), T0, {**MARKET, "price": 101}
    )
    assert accepted["status"] == "ACCEPTED"

    # A pre-submission candle cannot fill the order, and an unclosed candle is
    # ignored until a later call says its end time is observable.
    earlier = bar(T0 - timedelta(minutes=2), low=99, high=101)
    assert sim.advance([earlier], through=T0) == []
    unclosed = bar(T0, low=99, high=101)
    assert sim.advance([unclosed], through=T0 + timedelta(seconds=30)) == []

    # Merely touching the limit does not create a fill.
    touch = bar(T0, low=100, high=101)
    sim.advance([touch], through=T0 + timedelta(minutes=1))
    assert not sim.positions[accepted["position_id"]]["lots"]

    # A later strict crossing fills exactly at the limit, without price
    # improvement and with the configured maker fee.
    crossed = bar(T0 + timedelta(minutes=1), low=99.99, high=100.5, close=100.2)
    events = sim.advance([crossed], through=T0 + timedelta(minutes=2))
    fill = next(event for event in events if event.get("fill_price") is not None)
    assert fill["fill_price"] == 100
    assert fill["fee_type"] == "MAKER"
    assert fill["fee"] == pytest.approx(0.1)


def test_market_fills_next_bar_open_with_slippage_then_fees_reduce_net_win():
    sim = account()
    accepted = sim.apply_decision(decision(), T0, MARKET)
    assert accepted["status"] == "ACCEPTED"
    # A bar that began before submission cannot execute the market order.
    sim.advance([bar(T0 - timedelta(minutes=1), open=100, high=102, low=99)], through=T0)
    assert not sim.positions[accepted["position_id"]]["lots"]

    first = bar(T0, open=100, high=102, low=99, close=101)
    opened = sim.advance([first], through=T0 + timedelta(minutes=1))
    entry = next(event for event in opened if event.get("action") == "OPEN_LONG" and event.get("fill_price") is not None)
    assert entry["fill_price"] == pytest.approx(100.1)
    assert entry["fee_type"] == "TAKER"

    close_time = T0 + timedelta(minutes=1)
    close = sim.apply_decision(
        {"action": "CLOSE_POSITION", "instrument_id": "BTCUSDT", "position_id": accepted["position_id"]},
        close_time,
        {**MARKET, "price": 110},
    )
    assert close["status"] == "ACCEPTED"
    closed = sim.advance(
        [bar(close_time, open=110, high=111, low=109, close=110)],
        through=close_time + timedelta(minutes=1),
    )
    assert any(event.get("action") == "AI_REDUCE" and event.get("fill_price") == pytest.approx(109.89) for event in closed)
    summary = sim.summary({"BTCUSDT": 110})
    assert summary["closed_trade_count"] == 1
    assert summary["win_rate"] == 1
    assert summary["fees"] > 0
    assert summary["roi"] == pytest.approx((summary["ending_equity"] / 1000) - 1)


@pytest.mark.parametrize(
    ("order_type", "limit_price", "expected_entry"),
    [("MARKET", None, 100.1), ("LIMIT", 99, 99)],
)
def test_new_entry_protection_is_checked_on_the_entry_bar(order_type, limit_price, expected_entry):
    sim = account()
    opened = sim.apply_decision(
        decision(order_type=order_type, limit_price=limit_price, stop_price=95, take_profit=120),
        T0,
        MARKET,
    )
    fills = sim.advance(
        [bar(T0, open=100, high=101, low=90, close=92)],
        through=T0 + timedelta(minutes=1),
    )
    entry = next(event for event in fills if event.get("action") == "OPEN_LONG" and event.get("fill_price") is not None)
    stop = next(event for event in fills if event.get("action") == "STOP_LOSS" and event.get("fill_price") is not None)
    assert entry["fill_price"] == pytest.approx(expected_entry)
    assert stop["fill_price"] == pytest.approx(94.9)
    assert stop["position_id"] == opened["position_id"]
    assert sim.summary({"BTCUSDT": 92})["closed_trade_count"] == 1


def test_opening_without_take_profit_is_rejected():
    sim = account()
    invalid = decision()
    invalid.pop("take_profit")
    result = sim.apply_decision(invalid, T0, MARKET)
    assert result["status"] == "REJECTED"
    assert result["reason"] == "TAKE_PROFIT_REQUIRED"


def test_gross_profit_does_not_count_as_a_win_after_all_fees():
    sim = account()
    opened = sim.apply_decision(decision(), T0, MARKET)
    sim.advance([bar(T0, open=100, high=101, low=99)], through=T0 + timedelta(minutes=1))
    close_time = T0 + timedelta(minutes=1)
    sim.apply_decision(
        {"action": "CLOSE_POSITION", "instrument_id": "BTCUSDT", "position_id": opened["position_id"]},
        close_time,
        {**MARKET, "price": 100.5},
    )
    sim.advance([bar(close_time, open=100.5, high=100.6, low=100.4, close=100.5)], through=close_time + timedelta(minutes=1))
    summary = sim.summary({"BTCUSDT": 100.5})
    trade = sim.completed_trades[0]
    assert trade["realized_gross_pnl"] > 0
    assert trade["net_pnl"] < 0
    assert summary["closed_trade_count"] == 1
    assert summary["win_rate"] == 0


def test_short_side_is_adversely_slipped_and_protection_is_directional():
    sim = account()
    opened = sim.apply_decision(
        decision(action="OPEN_SHORT", stop_price=110, take_profit=80), T0, MARKET
    )
    fills = sim.advance([bar(T0, open=100, high=101, low=99)], through=T0 + timedelta(minutes=1))
    entry = next(event for event in fills if event.get("action") == "OPEN_SHORT" and event.get("fill_price") is not None)
    assert entry["fill_price"] == pytest.approx(99.9)
    truth = sim.account_truth({"BTCUSDT": 100}, T0 + timedelta(minutes=1))
    assert truth["positions"][0]["side"] == "SHORT"
    assert truth["positions"][0]["stop_price"] == 110

    close_time = T0 + timedelta(minutes=1)
    sim.apply_decision(
        {"action": "CLOSE_POSITION", "instrument_id": "BTCUSDT", "position_id": opened["position_id"]},
        close_time,
        {**MARKET, "price": 90},
    )
    sim.advance([bar(close_time, open=90, high=91, low=89, close=90)], through=close_time + timedelta(minutes=1))
    assert sim.completed_trades[0]["net_pnl"] > 0
    assert sim.summary({"BTCUSDT": 90})["win_rate"] == 1


def test_protection_update_allows_ai_to_widen_and_preserves_unspecified_target():
    sim = account()
    opened = sim.apply_decision(decision(), T0, MARKET)
    sim.advance([bar(T0)], through=T0 + timedelta(minutes=1))
    updated = sim.apply_decision(
        {"action": "UPDATE_PROTECTION", "instrument_id": "BTCUSDT", "position_id": opened["position_id"], "new_stop_price": 95},
        T0 + timedelta(minutes=1),
        MARKET,
    )
    assert updated["status"] == "ACCEPTED"
    assert updated["take_profit_preserved"] is True
    truth = sim.account_truth({"BTCUSDT": 100}, T0 + timedelta(minutes=1))
    assert truth["positions"][0]["stop_price"] == 95
    assert truth["positions"][0]["take_profit"] == 120

    widened = sim.apply_decision(
        {"action": "UPDATE_PROTECTION", "instrument_id": "BTCUSDT", "position_id": opened["position_id"], "new_stop_price": 90},
        T0 + timedelta(minutes=1),
        MARKET,
    )
    assert widened["status"] == "ACCEPTED"
    assert sim.positions[opened["position_id"]]["lots"][0]["stop_price"] == 90


@pytest.mark.parametrize("side,stop,mark", [("LONG", 105, 110), ("SHORT", 95, 90)])
def test_ai_can_protect_profit_beyond_entry_and_update_target_only(side, stop, mark):
    sim = account()
    opening = decision(action="OPEN_LONG" if side == "LONG" else "OPEN_SHORT",
                       stop_price=90 if side == "LONG" else 110,
                       take_profit=120 if side == "LONG" else 80)
    opened = sim.apply_decision(opening, T0, MARKET)
    sim.advance([bar(T0)], through=T0 + timedelta(minutes=1))
    now = T0 + timedelta(seconds=90)
    updated = sim.apply_decision({"action": "UPDATE_PROTECTION", "instrument_id": "BTCUSDT",
        "position_id": opened["position_id"], "new_stop_price": stop}, now, {**MARKET, "price": mark})
    assert updated["status"] == "ACCEPTED"
    lot = sim.positions[opened["position_id"]]["lots"][0]
    assert lot["stop_price"] == stop
    assert lot["protection_active_after"] == now.isoformat()
    target = 130 if side == "LONG" else 70
    updated = sim.apply_decision({"action": "UPDATE_PROTECTION", "instrument_id": "BTCUSDT",
        "position_id": opened["position_id"], "new_take_profit": target}, now, {**MARKET, "price": mark})
    assert updated["status"] == "ACCEPTED"
    assert lot["stop_price"] == stop and lot["take_profit"] == target


def test_gap_stop_and_same_bar_stop_target_tie_choose_adverse_stop():
    sim = account(slippage_bps=10)
    opened = sim.apply_decision(decision(), T0, MARKET)
    sim.advance([bar(T0, open=100, high=101, low=99)], through=T0 + timedelta(minutes=1))
    # Both stop and target are crossed; stop wins. The open gaps below stop,
    # so the exit is worse than the stop plus adverse slippage.
    events = sim.advance(
        [bar(T0 + timedelta(minutes=1), open=80, high=125, low=79, close=85)],
        through=T0 + timedelta(minutes=2),
    )
    exit_fill = next(event for event in events if event.get("action") == "STOP_LOSS" and event.get("fill_price") is not None)
    assert exit_fill["fill_price"] == pytest.approx(79.92)
    assert exit_fill["gap_stop"] is True
    assert not any(event.get("action") == "TAKE_PROFIT" for event in events)
    assert sim.completed_trades[0]["net_pnl"] < 0
    assert opened["position_id"] == sim.completed_trades[0]["position_id"]


def test_partial_market_fills_each_get_protection_and_triggered_remainder_persists():
    sim = account(participation_rate=0.5)
    opened = sim.apply_decision(decision(quantity=2), T0, MARKET)
    first = sim.advance([bar(T0, volume=2)], through=T0 + timedelta(minutes=1))
    assert any(event.get("status") == "PARTIAL_FILL" for event in first)
    second = sim.advance([bar(T0 + timedelta(minutes=1), volume=2)], through=T0 + timedelta(minutes=2))
    assert any(event.get("action") == "OPEN_LONG" and event.get("quantity") == 1 for event in second)
    truth = sim.account_truth({"BTCUSDT": 100}, T0 + timedelta(minutes=2))
    protection = truth["owned_protection_orders"]
    assert len(protection) == 2
    assert {item["stop_price"] for item in protection} == {90}

    gap = sim.advance(
        [bar(T0 + timedelta(minutes=2), open=80, high=81, low=79, close=80, volume=2)],
        through=T0 + timedelta(minutes=3),
    )
    assert sum(event.get("action") == "STOP_LOSS" and event.get("fill_price") is not None for event in gap) == 1
    assert any(event.get("status") == "PROTECTION_REMAINDER_PENDING" for event in gap)
    assert len(sim.account_truth({"BTCUSDT": 80}, T0 + timedelta(minutes=3))["positions"]) == 1

    next_bar = sim.advance(
        [bar(T0 + timedelta(minutes=3), open=78, high=79, low=77, close=78, volume=2)],
        through=T0 + timedelta(minutes=4),
    )
    assert any(event.get("action") == "STOP_LOSS" and event.get("fill_price") is not None for event in next_bar)
    assert sim.summary({"BTCUSDT": 78})["closed_trade_count"] == 1
    assert opened["position_id"] == sim.completed_trades[0]["position_id"]


def test_ttl_cancels_limit_and_releases_reserved_margin():
    sim = account()
    accepted = sim.apply_decision(
        decision(order_type="LIMIT", limit_price=100, ttl_seconds=30), T0, MARKET
    )
    assert accepted["status"] == "ACCEPTED"
    reserved_before = sim.account_truth({}, T0)["reserved_margin"]
    events = sim.advance(
        [bar(T0 + timedelta(minutes=1), open=101, low=101, high=102, close=101)],
        through=T0 + timedelta(minutes=2),
    )
    assert any(event["status"] == "EXPIRED" for event in events)
    truth = sim.account_truth({}, T0 + timedelta(minutes=2))
    assert truth["pending_orders"] == []
    assert truth["reserved_margin"] == 0
    assert reserved_before > 0
    assert sim.summary({})["expired_orders"] == 1
    assert accepted["order_id"] not in sim.orders


def test_margin_and_contract_limits_cap_or_reject_without_overcommitting():
    sim = account()
    capped = sim.apply_decision(
        decision(quantity=1000, requested_leverage=1), T0, MARKET
    )
    assert capped["status"] == "ACCEPTED"
    assert capped["quantity_capped"] is True
    assert capped["quantity"] <= 2
    too_leveraged = sim.apply_decision(
        decision(quantity=1, requested_leverage=50), T0, MARKET
    )
    assert too_leveraged["status"] == "REJECTED"
    assert too_leveraged["reason"] == "LEVERAGE_EXCEEDS_CONTRACT_MAX"
    invalid_protection = sim.apply_decision(
        decision(quantity=1, stop_price=105, take_profit=120), T0, MARKET
    )
    assert invalid_protection["status"] == "REJECTED"
    assert invalid_protection["reason"] == "PROTECTION_GEOMETRY_INVALID"


def test_funding_is_asof_filtered_idempotent_and_included_in_closed_trade_net():
    sim = account()
    opened = sim.apply_decision(decision(quantity=2), T0, MARKET)
    sim.advance([bar(T0, volume=1000)], through=T0 + timedelta(minutes=1))
    payment_time = T0 + timedelta(minutes=2)
    record = {
        "instrument_id": "BTCUSDT",
        "payment_time": payment_time,
        "available_at": payment_time + timedelta(minutes=5),
        "rate": 0.001,
        "source": "fixture:historical-funding",
    }
    assert sim.apply_funding([record], through=payment_time + timedelta(minutes=1)) == []
    assert sim.funding_pnl == 0
    applied = sim.apply_funding([record], through=payment_time + timedelta(minutes=5))
    assert applied[0]["funding_pnl"] == pytest.approx(-0.2)
    assert float(sim.funding_pnl) == pytest.approx(-0.2)
    assert sim.apply_funding([record], through=payment_time + timedelta(minutes=6)) == []

    close_time = payment_time + timedelta(minutes=6)
    sim.apply_decision(
        {"action": "CLOSE_POSITION", "instrument_id": "BTCUSDT", "position_id": opened["position_id"]},
        close_time,
        {**MARKET, "price": 100},
    )
    sim.advance([bar(close_time, open=100, high=101, low=99, close=100)], through=close_time + timedelta(minutes=1))
    trade = sim.completed_trades[0]
    assert float(trade["funding_pnl"]) == pytest.approx(-0.2)
    assert float(trade["net_pnl"]) == pytest.approx(float(trade["realized_gross_pnl"]) - float(trade["fees"]) - 0.2)


def test_checkpoint_round_trip_preserves_pending_orders_and_future_behavior():
    sim = account()
    sim.apply_decision(decision(order_type="LIMIT", limit_price=100), T0, MARKET)
    sim.advance([bar(T0, low=100, high=101)], through=T0 + timedelta(minutes=1))
    resumed = ReplayAccount.from_dict(sim.to_dict())
    assert resumed.account_id == sim.account_id
    assert resumed.summary({})["pending_order_count"] == 1
    next_bar = bar(T0 + timedelta(minutes=1), low=99.5, high=100.2, close=100)
    left = sim.advance([next_bar], through=T0 + timedelta(minutes=2))
    right = resumed.advance([next_bar], through=T0 + timedelta(minutes=2))
    left_fill = next(event for event in left if event.get("fill_price") is not None)
    right_fill = next(event for event in right if event.get("fill_price") is not None)
    assert left_fill == right_fill
    assert sim.summary({"BTCUSDT": 100})["ending_equity"] == resumed.summary({"BTCUSDT": 100})["ending_equity"]


def test_advance_is_idempotent_and_empty_win_rate_is_null():
    sim = account()
    once = sim.advance([bar(T0)], through=T0 + timedelta(minutes=1))
    twice = sim.advance([bar(T0)], through=T0 + timedelta(minutes=1))
    assert once == [] and twice == []
    summary = sim.summary({})
    assert summary["closed_trade_count"] == 0
    assert summary["win_rate"] is None
    assert summary["profit_factor"] is None
    assert summary["simulation"] is True


def test_filled_bar_replay_is_idempotent_after_checkpoint_restore():
    sim = account()
    submitted = sim.apply_decision(
        decision(order_type="LIMIT", limit_price=99, order_id="entry-idempotent"), T0, MARKET
    )
    assert submitted["status"] == "ACCEPTED"
    sim.advance([bar(T0)], through=T0 + timedelta(minutes=1))

    fill_bar = bar(T0 + timedelta(minutes=1), low=98.5, high=100.5)
    fills = sim.advance([fill_bar], through=T0 + timedelta(minutes=2))
    assert len([event for event in fills if event.get("order_id") == "entry-idempotent" and event.get("fill_price") is not None]) == 1

    restored = ReplayAccount.from_dict(sim.to_dict())
    assert sim.advance([fill_bar], through=T0 + timedelta(minutes=2)) == []
    assert restored.advance([fill_bar], through=T0 + timedelta(minutes=2)) == []
    assert sim.to_dict() == restored.to_dict()
    assert len(sim.positions[submitted["position_id"]]["lots"]) == 1
    assert sim.summary({})["filled_order_count"] == 1


@pytest.mark.parametrize("side,limit,opening,expected", [
    ("LONG", 101, 99, 99.1), ("SHORT", 99, 101, 100.89),
])
def test_marketable_limits_pay_taker_and_use_later_price_without_breaking_limit(side, limit, opening, expected):
    sim = account()
    accepted = sim.apply_decision(decision(action="OPEN_" + side, order_type="LIMIT", limit_price=limit,
        stop_price=90 if side == "LONG" else 110, take_profit=120 if side == "LONG" else 80), T0, MARKET)
    assert accepted["fee_type"] == "TAKER"
    assert accepted["marketability_assumption"] == "SUBMISSION_LAST_PRICE_PROXY"
    fills = sim.advance([bar(T0, open=opening, high=opening + 1, low=opening - 1, close=opening)], T0 + timedelta(minutes=1))
    fill = next(item for item in fills if item.get("action") == "OPEN_" + side and item.get("fill_price") is not None)
    assert fill["fill_price"] == pytest.approx(expected)
    assert fill["fill_price"] <= limit if side == "LONG" else fill["fill_price"] >= limit
    assert fill["fee_type"] == "TAKER"
    assert fill["fee"] == pytest.approx(expected * 0.002)


def test_marketable_limit_slippage_is_clamped_to_limit_price():
    sim = account()
    sim.apply_decision(decision(order_type="LIMIT", limit_price=100), T0, MARKET)
    events = sim.advance([bar(T0, open=100, high=101, low=99, close=100)], T0 + timedelta(minutes=1))
    entry = next(item for item in events if item.get("action") == "OPEN_LONG" and item.get("fill_price") is not None)
    assert entry["fill_price"] == 100
    assert entry["fee_type"] == "TAKER"


def test_native_take_profit_uses_market_slippage_and_taker_costs():
    sim = account()
    sim.apply_decision(decision(), T0, MARKET)
    sim.advance([bar(T0)], T0 + timedelta(minutes=1))
    events = sim.advance([bar(T0 + timedelta(minutes=1), open=119, high=121, low=118, close=120)], T0 + timedelta(minutes=2))
    target = next(item for item in events if item.get("action") == "TAKE_PROFIT" and item.get("fill_price") is not None)
    assert target["fill_price"] == pytest.approx(119.88)
    assert target["fee"] == pytest.approx(119.88 * 0.002)
    assert target["fee_type"] == "TAKER"


def test_same_side_orders_merge_one_way_position_and_keep_order_identity():
    sim = account()
    first = sim.apply_decision(decision(order_id="entry-one"), T0, MARKET)
    sim.advance([bar(T0)], T0 + timedelta(minutes=1))
    second = sim.apply_decision(decision(order_type="LIMIT", limit_price=99, order_id="entry-two"),
        T0 + timedelta(minutes=1), MARKET)
    sim.advance([bar(T0 + timedelta(minutes=1), open=100, high=101, low=98, close=100)], T0 + timedelta(minutes=2))
    assert first["position_id"] == second["position_id"]
    position = sim.positions[first["position_id"]]
    assert position["entry_order_ids"] == ["entry-one", "entry-two"]
    assert {lot["order_id"] for lot in position["lots"]} == {"entry-one", "entry-two"}
    truth = sim.account_truth({}, T0 + timedelta(minutes=2))
    assert len(truth["positions"]) == 1
    assert truth["positions"][0]["quantity"] == 2
    assert truth["positions"][0]["entry_price"] == pytest.approx((100.1 + 99) / 2)


@pytest.mark.parametrize("reverse_quantity,expected_remaining", [(1, 0), (3, 2)])
def test_opposite_resting_limit_nets_existing_position_then_opens_only_excess(reverse_quantity, expected_remaining):
    sim = account()
    first = sim.apply_decision(decision(), T0, MARKET)
    sim.advance([bar(T0)], T0 + timedelta(minutes=1))
    reverse = sim.apply_decision(decision(action="OPEN_SHORT", order_type="LIMIT", limit_price=102,
        quantity=reverse_quantity, stop_price=110, take_profit=80), T0 + timedelta(minutes=1), MARKET)
    assert reverse["status"] == "ACCEPTED"
    events = sim.advance([bar(T0 + timedelta(minutes=1), open=101, high=103, low=100, close=101)], T0 + timedelta(minutes=2))
    netted = next(item for item in events if item.get("action") == "ONE_WAY_NETTING")
    assert netted["position_id"] == first["position_id"]
    assert netted["fee_type"] == "MAKER"
    assert netted["fee"] == pytest.approx(0.102)
    entry = next(item for item in events if item.get("action") == "OPEN_SHORT" and item.get("fill_price") is not None)
    assert entry["netted_quantity"] == 1
    assert entry["opened_quantity"] == expected_remaining
    truth = sim.account_truth({}, T0 + timedelta(minutes=2))
    assert len(truth["positions"]) == (1 if expected_remaining else 0)
    if expected_remaining:
        assert truth["positions"][0]["side"] == "SHORT"
        assert truth["positions"][0]["quantity"] == expected_remaining
    assert sim.completed_trades[0]["position_id"] == first["position_id"]
    assert float(sim.total_fees) == pytest.approx(100.1 * 0.002 + 102 * 0.001 * reverse_quantity)
    assert float(sim.total_fees) == pytest.approx(sum(item.get("fee", 0) for item in sim.events))
    summary = sim.summary({})
    assert summary["fees"] == pytest.approx(summary["maker_fees"] + summary["taker_fees"])


def test_opposite_pending_orders_crossing_same_bar_never_create_hedged_positions():
    sim = account()
    sim.apply_decision(decision(order_type="LIMIT", limit_price=99), T0, MARKET)
    sim.apply_decision(decision(action="OPEN_SHORT", order_type="LIMIT", limit_price=101,
        stop_price=110, take_profit=80), T0, MARKET)
    sim.advance([bar(T0, open=100, high=102, low=98, close=100)], T0 + timedelta(minutes=1))
    assert not sim.account_truth({}, T0 + timedelta(minutes=1))["positions"]
    assert sim.summary({})["closed_trade_count"] == 1
    assert float(sim.completed_trades[0]["net_pnl"]) == pytest.approx(1.8)


def test_nonpositive_equity_halts_and_cannot_rebound_or_resume_trading():
    sim = account(initial_equity=100, margin_cap_pct=100, slippage_bps=0,
                  maker_fee_rate=0, taker_fee_rate=0)
    market = {**MARKET, "contract_rules": {**MARKET["contract_rules"], "leverage_max": 100}}
    opening = decision(quantity=90, requested_leverage=100, stop_price=1, take_profit=200)
    accepted = sim.apply_decision(opening, T0, market)
    sim.advance([bar(T0, open=100, high=101, low=100, close=100)], T0 + timedelta(minutes=1))
    assert accepted["status"] == "ACCEPTED"
    events = sim.advance([bar(T0 + timedelta(minutes=1), open=98, high=99, low=97, close=98)], T0 + timedelta(minutes=2))
    assert any(item["status"] == "HALTED" for item in events)
    assert sim.halted_reason == "UNSUPPORTED_LIQUIDATION_PATH"
    frozen = sim.to_dict()
    summary = sim.summary({"BTCUSDT": 200})
    assert summary["roi"] is None and summary["ending_equity"] is None
    assert summary["diagnostic_equity"] < 0
    assert summary["economic_eligible"] is False
    assert sim.advance([bar(T0 + timedelta(minutes=2), open=150, high=200, low=150, close=190)], T0 + timedelta(minutes=3)) == []
    assert sim.apply_funding([{"instrument_id": "BTCUSDT", "payment_time": T0 + timedelta(minutes=3), "rate": -1, "mark_price": 200}], T0 + timedelta(minutes=3)) == []
    assert sim.apply_decision(decision(), T0 + timedelta(minutes=3), market)["status"] == "HALTED"
    assert sim.account_truth({"BTCUSDT": 200}, T0 + timedelta(minutes=3))["error_code"] == "UNSUPPORTED_LIQUIDATION_PATH"
    assert sim.to_dict() == frozen
    resumed = ReplayAccount.from_dict(frozen)
    assert resumed.summary({"BTCUSDT": 200}) == summary


def test_public_maintenance_proxy_halts_before_bankruptcy_and_stop_beyond_boundary():
    sim = account(initial_equity=100, margin_cap_pct=100, slippage_bps=0,
                  maker_fee_rate=0, taker_fee_rate=0)
    market = {**MARKET, "contract_rules": {**MARKET["contract_rules"], "leverage_max": 100, "maintenance_rate": 0.005}}
    sim.apply_decision(decision(quantity=90, requested_leverage=100, stop_price=98, take_profit=200), T0, market)
    sim.advance([bar(T0, open=100, high=101, low=100, close=100)], T0 + timedelta(minutes=1))
    events = sim.advance([bar(T0 + timedelta(minutes=1), open=100, high=101, low=98, close=100)], T0 + timedelta(minutes=2))
    assert sim.halted_reason == "UNSUPPORTED_LIQUIDATION_PATH"
    assert not any(item.get("action") == "STOP_LOSS" and item.get("fill_price") for item in events)
    assert sim.summary({})["closed_trade_count"] == 0
    assert sim.summary({})["roi"] is None


def test_intrabar_maintenance_breach_cannot_be_hidden_by_a_recovered_close():
    sim = account(initial_equity=100, margin_cap_pct=100, slippage_bps=0,
                  maker_fee_rate=0, taker_fee_rate=0)
    market = {**MARKET, "contract_rules": {**MARKET["contract_rules"], "leverage_max": 100, "maintenance_rate": 0.005}}
    sim.apply_decision(decision(quantity=90, requested_leverage=100, stop_price=1, take_profit=200), T0, market)
    sim.advance([bar(T0, open=100, high=101, low=100, close=100)], T0 + timedelta(minutes=1))
    sim.advance([bar(T0 + timedelta(minutes=1), open=100, high=101, low=99.2, close=100.5)], T0 + timedelta(minutes=2))
    summary = sim.summary({"BTCUSDT": 200})
    assert summary["halted_reason"] == "UNSUPPORTED_LIQUIDATION_PATH"
    assert summary["diagnostic_equity"] > 0
    assert summary["diagnostic_equity"] <= summary["halted_diagnostic"]["maintenance_margin_proxy"]
    assert summary["roi"] is None


def test_stop_that_precedes_maintenance_boundary_can_complete_normally():
    sim = account(initial_equity=100, margin_cap_pct=100, slippage_bps=0,
                  maker_fee_rate=0, taker_fee_rate=0)
    market = {**MARKET, "contract_rules": {**MARKET["contract_rules"], "leverage_max": 100, "maintenance_rate": 0.005}}
    sim.apply_decision(decision(quantity=90, requested_leverage=100, stop_price=99.8, take_profit=200), T0, market)
    sim.advance([bar(T0, open=100, high=101, low=100, close=100)], T0 + timedelta(minutes=1))
    sim.advance([bar(T0 + timedelta(minutes=1), open=100, high=101, low=98, close=100)], T0 + timedelta(minutes=2))
    summary = sim.summary({})
    assert summary["economic_eligible"] is True
    assert summary["closed_trade_count"] == 1
    assert summary["roi"] == pytest.approx(-0.18)


def test_accepted_one_way_leverage_setting_updates_existing_lots_and_pending_entries():
    sim = account()
    first = sim.apply_decision(decision(requested_leverage=2), T0, MARKET)
    sim.advance([bar(T0)], T0 + timedelta(minutes=1))
    pending = sim.apply_decision(decision(order_type="LIMIT", limit_price=99, requested_leverage=4),
        T0 + timedelta(minutes=1), MARKET)
    existing = sim.positions[first["position_id"]]
    assert existing["leverage"] == 4
    assert existing["lots"][0]["leverage"] == 4
    assert existing["lots"][0]["entry_requested_leverage"] == 2
    assert sim.orders[pending["order_id"]]["leverage"] == 4
    truth = sim.account_truth({"BTCUSDT": 100}, T0 + timedelta(minutes=1))
    assert truth["positions"][0]["leverage"] == 4
    assert truth["positions"][0]["used_margin"] == pytest.approx(25)
    # A rejected request must not mutate the instrument leverage setting.
    sim.apply_decision(decision(requested_leverage=21), T0 + timedelta(minutes=1), MARKET)
    assert existing["lots"][0]["leverage"] == 4
    sim.apply_decision(decision(order_type="LIMIT", limit_price=98, requested_leverage=5),
        T0 + timedelta(minutes=1), MARKET)
    assert existing["lots"][0]["leverage"] == 5
    assert sim.orders[pending["order_id"]]["leverage"] == 5


def test_partial_opposite_fill_reduces_old_lots_without_opening_hedge():
    sim = account()
    first = sim.apply_decision(decision(quantity=2), T0, MARKET)
    sim.advance([bar(T0)], T0 + timedelta(minutes=1))
    reverse = sim.apply_decision(decision(action="OPEN_SHORT", order_type="LIMIT", limit_price=102,
        quantity=1, stop_price=110, take_profit=80), T0 + timedelta(minutes=1), MARKET)
    sim.advance([bar(T0 + timedelta(minutes=1), open=101, high=103, low=100, close=101)], T0 + timedelta(minutes=2))
    truth = sim.account_truth({}, T0 + timedelta(minutes=2))
    assert len(truth["positions"]) == 1
    assert truth["positions"][0]["position_id"] == first["position_id"]
    assert truth["positions"][0]["side"] == "LONG"
    assert truth["positions"][0]["quantity"] == 1
    assert not sim.positions[reverse["position_id"]]["lots"]
    assert not sim.halted_reason
    assert sim.summary({})["closed_trade_count"] == 0


def test_same_bar_take_profit_does_not_erase_an_earlier_possible_liquidation():
    sim = account(initial_equity=100, margin_cap_pct=100, slippage_bps=0,
                  maker_fee_rate=0, taker_fee_rate=0)
    market = {**MARKET, "contract_rules": {**MARKET["contract_rules"], "leverage_max": 100, "maintenance_rate": 0.005}}
    sim.apply_decision(decision(quantity=90, requested_leverage=100, stop_price=10, take_profit=120), T0, market)
    sim.advance([bar(T0, open=100, high=101, low=100, close=100)], T0 + timedelta(minutes=1))
    events = sim.advance([bar(T0 + timedelta(minutes=1), open=100, high=125, low=80, close=120)], T0 + timedelta(minutes=2))
    assert sim.halted_reason == "UNSUPPORTED_LIQUIDATION_PATH"
    assert not any(item.get("action") == "TAKE_PROFIT" and item.get("fill_price") is not None for item in events)
    assert sim.summary({})["roi"] is None
    assert sim.halted_diagnostic["cause"] == "TAKE_PROFIT_LIQUIDATION_ORDERING_AMBIGUOUS"


def test_safe_stop_first_path_precedes_same_bar_target_and_liquidation_extreme():
    sim = account(initial_equity=100, margin_cap_pct=100, slippage_bps=0,
                  maker_fee_rate=0, taker_fee_rate=0)
    market = {**MARKET, "contract_rules": {**MARKET["contract_rules"], "leverage_max": 100, "maintenance_rate": 0.005}}
    sim.apply_decision(decision(quantity=90, requested_leverage=100, stop_price=99.8, take_profit=120), T0, market)
    sim.advance([bar(T0, open=100, high=101, low=100, close=100)], T0 + timedelta(minutes=1))
    events = sim.advance([bar(T0 + timedelta(minutes=1), open=100, high=125, low=80, close=120)], T0 + timedelta(minutes=2))
    assert not sim.halted_reason
    assert sim.summary({})["roi"] == pytest.approx(-0.18)
    assert any(item.get("action") == "STOP_LOSS" for item in events)
    assert not any(item.get("action") == "TAKE_PROFIT" for item in events)
