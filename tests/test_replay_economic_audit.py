"""Independent cost checks on isolated simulator fixtures, never real trading."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from core.replay.ai_simulation import ReplayAccount
from scripts.verify_ai_template_replay import audit_account


def closed_fixture():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rules = {"contract_size": 1, "amount_step": 1, "price_tick": .01,
             "min_size": 1, "max_size": 10000, "leverage_max": 20}
    account = ReplayAccount(initial_equity=1000, account_id="COST_AUDIT_TEST_FIXTURE",
        maker_fee_rate=.001, taker_fee_rate=.002, slippage_bps=0, participation_rate=1)
    account.apply_decision({"action": "OPEN_LONG", "instrument_id": "BTCUSDT",
        "order_preference": "MARKET", "requested_leverage": 2, "quantity": 1,
        "stop_price": 90, "take_profit": 100.1}, start,
        {"instrument_id": "BTCUSDT", "price": 100, "contract_rules": rules})
    for i, high in enumerate((100.05, 100.2)):
        account.advance([{"instrument_id": "BTCUSDT", "bar_start": start + timedelta(minutes=i),
            "bar_end": start + timedelta(minutes=i + 1), "open": 100, "high": high,
            "low": 99.99, "close": 100, "volume": 1000, "synthetic": False,
            "contract_rules": rules}], through=start + timedelta(minutes=i + 1))
    return account.to_dict()


def test_recomputes_configured_fill_costs_and_net_loss():
    result = audit_account(closed_fixture())
    assert result["fees"] == pytest.approx(.4002)
    assert result["realized_gross_pnl"] == pytest.approx(.1)
    assert result["roi"] == pytest.approx(-.0003002)
    assert result["win_rate"] == 0


def test_consistently_erasing_fees_cannot_turn_loss_into_profit():
    modified = deepcopy(closed_fixture())
    state = modified["state"]
    state["total_fees"] = 0
    for position in state["positions"].values():
        for lot in position["lots"]:
            lot["entry_fee"] = lot["exit_fees"] = 0
            for exit_row in lot["exits"]:
                exit_row["fee"] = 0
    for event in state["events"]:
        if "fee" in event:
            event["fee"] = 0
        if "fee_paid_total" in event:
            event["fee_paid_total"] = 0
    for trade in state["completed_trades"]:
        trade["fees"] = 0
        trade["net_pnl"] = trade["realized_gross_pnl"]
    for row in state["equity_history"]:
        row["equity"] = 1000.1
    with pytest.raises(ValueError, match="FILL_FEE_FORMULA"):
        audit_account(modified)


def test_market_entry_cannot_be_relabelled_maker_to_reduce_costs():
    modified = closed_fixture()
    for event in modified["state"]["events"]:
        if event.get("status") == "ACCEPTED":
            event["fee_type"] = "MAKER"
    with pytest.raises(ValueError, match="ACCEPTED_ORDER_FEE_TYPE"):
        audit_account(modified)
