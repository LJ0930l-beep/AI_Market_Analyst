"""Isolated unit fixtures for frozen Gate-format execution rules, not market evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.replay.ai_history import ReplayHistory, manifest_hash, normalize_bar, normalize_contract
from core.replay.ai_simulation import ReplayAccount
from core.replay.ai_template_runner import _advance_account


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def contract(symbol="BTCUSDT"):
    return normalize_contract(symbol, {
        "quanto_multiplier": "0.0001" if symbol == "BTCUSDT" else "0.01",
        "order_price_round": "0.1" if symbol == "BTCUSDT" else "0.01",
        "order_size_min": "1" if symbol == "BTCUSDT" else "0.1",
        "order_size_max": "1000000", "leverage_max": "100",
        "enable_decimal": symbol == "ETHUSDT",
    })


def history(symbol="BTCUSDT", volumes=(1330563,), *, prices=None):
    """Synthetic values in the actual normalized format; no HTTP/model/DB access."""
    rules = contract(symbol)
    bars = []
    for index, volume in enumerate(volumes):
        price = prices[index] if prices else 100
        bars.append(normalize_bar(symbol, "1m", {
            "t": int((START + timedelta(minutes=index)).timestamp()),
            "o": str(price), "h": str(price + 1), "l": str(price - 1),
            "c": str(price), "v": str(volume),
        }, START + timedelta(days=1), rules))
    payload = {
        "schema_version": "ai_template_history_v1", "symbols": [symbol],
        "window_start": START.isoformat(),
        "window_end": (START + timedelta(minutes=len(bars))).isoformat(),
        "decision_points": [START.isoformat()], "bars": bars,
        "contracts": {symbol: rules}, "funding": [], "news": [],
        "complete_data": True,
        "assumptions": {"fixture": "UNIT_TEST_ONLY_NOT_REAL_MARKET_DATA"},
    }
    payload["manifest_sha256"] = manifest_hash(payload)
    return ReplayHistory(payload)


class RecordingAccount(ReplayAccount):
    def __init__(self):
        super().__init__(account_id="contract-capacity-unit-fixture")
        self.execution_bars = []

    def advance(self, bars, through):
        self.execution_bars.extend(deepcopy(bars))
        return super().advance(bars, through)


def market(data, price=100):
    symbol = data.symbols[0]
    return {"instrument_id": symbol, "price": price,
            "contract_rules": deepcopy(data.payload["contracts"][symbol])}


def open_position(account, data, quantity):
    event = account.apply_decision({
        "action": "OPEN_LONG", "instrument_id": data.symbols[0],
        "order_preference": "MARKET", "requested_leverage": 2,
        "quantity": quantity, "stop_price": 90, "take_profit": 120,
        "ttl_seconds": 3600,
    }, START, market(data))
    assert event["status"] == "ACCEPTED"
    assert event["quantity"] == quantity
    return event


def advance(account, data, first_minute, last_minute):
    _advance_account(account, data, START + timedelta(minutes=first_minute),
                     START + timedelta(minutes=last_minute))


def remaining(account, position_id):
    return sum((lot["remaining_qty"] for lot in account.positions[position_id]["lots"]), Decimal(0))


def test_btc_normalized_base_volume_uses_frozen_multiplier_without_mutating_history(monkeypatch):
    data = history()
    original = deepcopy(data.payload)
    monkeypatch.setattr(data, "market_snapshot", lambda *a: pytest.fail("must use frozen payload rules"))
    sim = RecordingAccount()
    raw = data.payload["bars"][0]
    assert raw["volume"] == 1330563 and raw["base_volume"] == pytest.approx(133.0563)
    assert raw["volume_unit"] == "contracts"
    assert sim._bar_capacity(raw, {}) == Decimal("1.330")

    advance(sim, data, 0, 1)

    execution = sim.execution_bars[0]
    assert sim._bar_capacity(execution, execution["contract_rules"]) == Decimal("13305")
    assert execution["contract_rules"]["contract_size"] == .0001
    assert execution["contract_rules"]["amount_step"] == 1
    assert execution["contract_rules"]["price_tick"] == .1
    assert execution["market"] == original["contracts"]["BTCUSDT"]["market"]
    assert all(execution[key] == raw[key] for key in raw)
    assert data.payload == original
    assert manifest_hash(data.payload) == original["manifest_sha256"]

    execution["contract_rules"]["market"]["precision"]["amount"] = 999
    assert data.payload == original  # The attached rules are also independent copies.


@pytest.mark.parametrize("quantity,entry_minutes,expected_first", [(100, 1, 100), (20000, 2, 13305)])
def test_btc_complete_and_partial_entries_and_exits_share_correct_capacity(quantity, entry_minutes, expected_first):
    data = history(volumes=(1330563, 1330563, 1000000, 1000000), prices=(100, 100, 101, 101))
    sim = RecordingAccount()
    opened = open_position(sim, data, quantity)

    advance(sim, data, 0, 1)
    entry = next(e for e in sim.events if e.get("fill_price") is not None and e["action"] == "OPEN_LONG")
    assert entry["quantity"] == expected_first
    assert entry["status"] == ("FILLED" if quantity == 100 else "PARTIAL_FILL")
    assert remaining(sim, opened["position_id"]) == expected_first
    if entry_minutes == 2:
        advance(sim, data, 1, 2)
    assert remaining(sim, opened["position_id"]) == quantity
    assert opened["order_id"] not in sim.orders

    # Both cases start the exit at minute 2; no order/position state is reset.
    if entry_minutes == 1:
        advance(sim, data, 1, 2)
    exited = sim.apply_decision({"action": "CLOSE_POSITION", "instrument_id": "BTCUSDT",
        "position_id": opened["position_id"]}, START + timedelta(minutes=2), market(data, 101))
    assert exited["status"] == "ACCEPTED"
    advance(sim, data, 2, 3)
    assert remaining(sim, opened["position_id"]) == (0 if quantity == 100 else 10000)
    advance(sim, data, 3, 4)
    assert remaining(sim, opened["position_id"]) == 0
    assert sim.summary({"BTCUSDT": 101})["closed_trade_count"] == 1
    exit_fills = [e for e in sim.events if e.get("fill_price") is not None and e["action"] == "AI_REDUCE"]
    assert sum(e["quantity"] for e in exit_fills) == quantity
    assert sim.total_fees > 0


def test_eth_decimal_step_rounds_partial_entries_and_exits_in_contract_units():
    data = history("ETHUSDT", volumes=(12549, 12549, 12549, 12549), prices=(100, 100, 101, 101))
    sim = RecordingAccount()
    opened = open_position(sim, data, 126.7)
    advance(sim, data, 0, 1)
    entry = next(e for e in sim.events if e.get("fill_price") is not None and e["action"] == "OPEN_LONG")
    assert entry["quantity"] == 125.4
    assert entry["status"] == "PARTIAL_FILL"
    execution = sim.execution_bars[0]
    assert sim._bar_capacity(execution, execution["contract_rules"]) == Decimal("125.4")
    advance(sim, data, 1, 2)
    assert remaining(sim, opened["position_id"]) == Decimal("126.7")

    sim.apply_decision({"action": "CLOSE_POSITION", "instrument_id": "ETHUSDT",
        "position_id": opened["position_id"]}, START + timedelta(minutes=2), market(data, 101))
    advance(sim, data, 2, 3)
    assert remaining(sim, opened["position_id"]) == Decimal("1.3")
    advance(sim, data, 3, 4)
    assert remaining(sim, opened["position_id"]) == 0
    assert sim.summary({"ETHUSDT": 101})["closed_trade_count"] == 1


@pytest.mark.parametrize("invalid", ["all_contracts", "missing_symbol", "not_object", "instrument", "native", "market_symbol", "market_id", "bar_symbol"])
def test_missing_or_wrong_symbol_rules_are_rejected_before_any_account_mutation(invalid):
    data = history(volumes=(1330563, 1330563))
    rules = data.payload["contracts"]["BTCUSDT"]
    if invalid == "all_contracts":
        data.payload.pop("contracts")
    elif invalid == "missing_symbol":
        data.payload["contracts"] = {"ETHUSDT": rules}
    elif invalid == "not_object":
        data.payload["contracts"]["BTCUSDT"] = None
    elif invalid == "instrument":
        rules["instrument_id"] = "ETHUSDT"
    elif invalid == "native":
        rules["native_symbol"] = "ETH_USDT"
    elif invalid == "market_symbol":
        rules["market"]["symbol"] = "ETHUSDT"
    elif invalid == "market_id":
        rules["market"]["id"] = "ETH_USDT"
    else:
        data._bars[("BTCUSDT", "1m")][1]["symbol"] = "ETHUSDT"
    sim = RecordingAccount()
    before = sim.to_dict()
    with pytest.raises(ValueError, match="REPLAY_EXECUTION_(CONTRACT|BAR)"):
        advance(sim, data, 0, 2)
    assert sim.execution_bars == []
    assert sim.to_dict() == before


@pytest.mark.parametrize("field", ["contract_size", "amount_step", "price_round", "min_size", "max_size", "leverage_max"])
@pytest.mark.parametrize("value", [None, 0, -1, float("nan"), float("inf"), True])
def test_incomplete_or_invalid_frozen_rules_cannot_use_simulator_defaults(field, value):
    data = history()
    data.payload["contracts"]["BTCUSDT"][field] = value
    sim = RecordingAccount()
    with pytest.raises(ValueError, match=f"REPLAY_EXECUTION_CONTRACT_INVALID:BTCUSDT:{field}"):
        advance(sim, data, 0, 1)
    assert sim.execution_bars == []


def test_frozen_rules_replace_stale_execution_metadata_and_flat_tick():
    data = history()
    raw = data.payload["bars"][0]
    raw["market"] = {"contract_size": 1, "amount_step": .001, "price_tick": 5}
    raw["contract_rules"] = {"contract_size": 1, "amount_step": .001, "price_tick": 5}
    raw["price_tick"] = 10
    original = deepcopy(data.payload)
    sim = RecordingAccount()
    advance(sim, data, 0, 1)
    execution = sim.execution_bars[0]
    assert execution["contract_rules"]["price_tick"] == .1
    assert sim._bar_capacity(execution, execution["contract_rules"]) == Decimal("13305")
    assert data.payload == original
