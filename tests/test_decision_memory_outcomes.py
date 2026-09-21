"""Outcome reconciliation must turn closed positions into usable memory.

Regression guard for the second broken link: ``update_memory_outcome`` had no
caller, so ``memory_for_prompt`` returned a permanently null ``outcome_status``
and the model could never tell a winning setup from a losing one.
"""
from datetime import datetime, timezone

import pytest

from core.storage import SQLiteStore
from core.trading.decision_memory import (
    list_decision_memory,
    memory_for_prompt,
    reconcile_decision_outcomes,
    record_decision_memory,
)

ACCOUNT = "gate_testnet"


def _store(tmp_path, name):
    store = SQLiteStore(tmp_path / name)
    store.initialize()
    return store


def _fill(store, *, fill_id, cycle_id, position_id, side, quantity, price, fee=0.0,
          contract_size=1.0):
    with store._connect() as db:
        db.execute(
            """INSERT INTO trade_fills
               (fill_id, account_id, venue, mode, order_id, trade_id, position_id, symbol,
                side, quantity, price, fee, fee_amount, fee_currency, contract_size,
                status, payload_json, created_at, event_at, cycle_id)
               VALUES (?, ?, 'gate', 'TESTNET', ?, ?, ?, 'BTCUSDT', ?, ?, ?, ?, ?, 'USDT', ?,
                       'RECORDED', '{}', ?, ?, ?)""",
            (
                fill_id, ACCOUNT, f"order_{fill_id}", f"trade_{fill_id}", position_id,
                side, str(quantity), str(price), str(fee), str(fee), str(contract_size),
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
                cycle_id,
            ),
        )


def _entry_memory(store, *, cycle_id, action="OPEN_LONG", symbol="BTCUSDT"):
    return record_decision_memory(
        store,
        account_id=ACCOUNT,
        provider="gate",
        environment="testnet",
        cycle_id=cycle_id,
        session_id="sess_1",
        candidate_id=None,
        symbol=symbol,
        action=action,
        cycle_status="EXECUTED",
        decision_at=datetime.now(timezone.utc),
        reason="test entry",
        decision_origin="MODEL",
    )


def _outcomes(store):
    return {item["memory_id"]: item for item in list_decision_memory(store, ACCOUNT)}


def test_closed_long_win_is_recorded_with_net_pnl(tmp_path):
    store = _store(tmp_path, "win.sqlite3")
    memory = _entry_memory(store, cycle_id="cycle_win")
    _fill(store, fill_id="e1", cycle_id="cycle_win", position_id="pos_win",
          side="BUY", quantity=1, price=100, fee=1.0)
    _fill(store, fill_id="x1", cycle_id="cycle_win", position_id="pos_win",
          side="SELL", quantity=1, price=110, fee=1.0)

    result = reconcile_decision_outcomes(store, ACCOUNT)

    assert result["considered"] == 1 and result["pending"] == 0
    row = _outcomes(store)[memory["memory_id"]]
    assert row["outcome_status"] == "WIN"
    assert row["outcome_pnl"] == pytest.approx(8.0)
    assert "净盈亏" in row["lesson_zh"]
    evidence = row["payload"]["outcome_evidence"]
    assert evidence["basis"] == "LOCAL_FILL_MIRROR_NET_OF_FEES"
    assert evidence["position_id"] == "pos_win"
    assert evidence["gross_realized"] == pytest.approx(10.0)
    assert evidence["fees"] == pytest.approx(2.0)


def test_closed_long_loss_is_recorded(tmp_path):
    store = _store(tmp_path, "loss.sqlite3")
    memory = _entry_memory(store, cycle_id="cycle_loss")
    _fill(store, fill_id="e2", cycle_id="cycle_loss", position_id="pos_loss",
          side="BUY", quantity=1, price=100)
    _fill(store, fill_id="x2", cycle_id="cycle_loss", position_id="pos_loss",
          side="SELL", quantity=1, price=90)

    reconcile_decision_outcomes(store, ACCOUNT)

    row = _outcomes(store)[memory["memory_id"]]
    assert row["outcome_status"] == "LOSS"
    assert row["outcome_pnl"] == pytest.approx(-10.0)


def test_short_win_uses_the_same_direction_agnostic_formula(tmp_path):
    store = _store(tmp_path, "short.sqlite3")
    memory = _entry_memory(store, cycle_id="cycle_short", action="OPEN_SHORT")
    _fill(store, fill_id="e3", cycle_id="cycle_short", position_id="pos_short",
          side="SELL", quantity=2, price=200)
    _fill(store, fill_id="x3", cycle_id="cycle_short", position_id="pos_short",
          side="BUY", quantity=2, price=180)

    reconcile_decision_outcomes(store, ACCOUNT)

    row = _outcomes(store)[memory["memory_id"]]
    assert row["outcome_status"] == "WIN"
    assert row["outcome_pnl"] == pytest.approx(40.0)


def test_open_position_is_left_pending(tmp_path):
    store = _store(tmp_path, "open.sqlite3")
    memory = _entry_memory(store, cycle_id="cycle_open")
    _fill(store, fill_id="e4", cycle_id="cycle_open", position_id="pos_open",
          side="BUY", quantity=1, price=100)

    result = reconcile_decision_outcomes(store, ACCOUNT)

    assert result["considered"] == 1 and result["pending"] == 1 and result["resolved"] == []
    assert _outcomes(store)[memory["memory_id"]]["outcome_status"] is None


def test_partially_closed_position_is_left_pending(tmp_path):
    store = _store(tmp_path, "partial.sqlite3")
    memory = _entry_memory(store, cycle_id="cycle_partial")
    _fill(store, fill_id="e5", cycle_id="cycle_partial", position_id="pos_partial",
          side="BUY", quantity=2, price=100)
    _fill(store, fill_id="x5", cycle_id="cycle_partial", position_id="pos_partial",
          side="SELL", quantity=1, price=110)

    result = reconcile_decision_outcomes(store, ACCOUNT)

    assert result["pending"] == 1
    assert _outcomes(store)[memory["memory_id"]]["outcome_status"] is None


def test_wait_decision_is_never_reconciled(tmp_path):
    store = _store(tmp_path, "wait.sqlite3")
    memory = _entry_memory(store, cycle_id="cycle_wait", action="WAIT")
    _fill(store, fill_id="e6", cycle_id="cycle_wait", position_id="pos_wait",
          side="BUY", quantity=1, price=100)
    _fill(store, fill_id="x6", cycle_id="cycle_wait", position_id="pos_wait",
          side="SELL", quantity=1, price=120)

    result = reconcile_decision_outcomes(store, ACCOUNT)

    assert result["considered"] == 0
    assert _outcomes(store)[memory["memory_id"]]["outcome_status"] is None


def test_entry_without_position_is_left_pending(tmp_path):
    store = _store(tmp_path, "nopos.sqlite3")
    memory = _entry_memory(store, cycle_id="cycle_nopos")

    result = reconcile_decision_outcomes(store, ACCOUNT)

    assert result["pending"] == 1
    assert _outcomes(store)[memory["memory_id"]]["outcome_status"] is None


def test_reconciliation_is_idempotent(tmp_path):
    store = _store(tmp_path, "idem.sqlite3")
    _entry_memory(store, cycle_id="cycle_idem")
    _fill(store, fill_id="e7", cycle_id="cycle_idem", position_id="pos_idem",
          side="BUY", quantity=1, price=100)
    _fill(store, fill_id="x7", cycle_id="cycle_idem", position_id="pos_idem",
          side="SELL", quantity=1, price=105)

    first = reconcile_decision_outcomes(store, ACCOUNT)
    second = reconcile_decision_outcomes(store, ACCOUNT)

    assert len(first["resolved"]) == 1
    assert second == {"considered": 0, "pending": 0, "resolved": []}


def test_resolved_outcome_reaches_the_prompt_memory(tmp_path):
    store = _store(tmp_path, "prompt.sqlite3")
    _entry_memory(store, cycle_id="cycle_prompt")
    _fill(store, fill_id="e8", cycle_id="cycle_prompt", position_id="pos_prompt",
          side="BUY", quantity=1, price=100)
    _fill(store, fill_id="x8", cycle_id="cycle_prompt", position_id="pos_prompt",
          side="SELL", quantity=1, price=97)

    reconcile_decision_outcomes(store, ACCOUNT)

    prompt_rows = memory_for_prompt(store, ACCOUNT)
    assert len(prompt_rows) == 1
    assert prompt_rows[0]["outcome_status"] == "LOSS"
    assert prompt_rows[0]["outcome_pnl"] == pytest.approx(-3.0)


def test_flat_result_inside_the_fee_band_is_recorded_as_flat(tmp_path):
    store = _store(tmp_path, "flat.sqlite3")
    memory = _entry_memory(store, cycle_id="cycle_flat")
    _fill(store, fill_id="e9", cycle_id="cycle_flat", position_id="pos_flat",
          side="BUY", quantity=1, price=100)
    _fill(store, fill_id="x9", cycle_id="cycle_flat", position_id="pos_flat",
          side="SELL", quantity=1, price=100)

    reconcile_decision_outcomes(store, ACCOUNT)

    assert _outcomes(store)[memory["memory_id"]]["outcome_status"] == "FLAT"


def test_flat_price_after_fees_is_a_loss_of_exactly_the_fees(tmp_path):
    """Paying fees to exit flat is a real loss, not a wash."""
    store = _store(tmp_path, "flatfee.sqlite3")
    memory = _entry_memory(store, cycle_id="cycle_flatfee")
    _fill(store, fill_id="e11", cycle_id="cycle_flatfee", position_id="pos_flatfee",
          side="BUY", quantity=1, price=100, fee=0.5)
    _fill(store, fill_id="x11", cycle_id="cycle_flatfee", position_id="pos_flatfee",
          side="SELL", quantity=1, price=100, fee=0.5)

    reconcile_decision_outcomes(store, ACCOUNT)

    row = _outcomes(store)[memory["memory_id"]]
    assert row["outcome_status"] == "LOSS"
    assert row["outcome_pnl"] == pytest.approx(-1.0)


def test_contract_size_scales_the_notional(tmp_path):
    store = _store(tmp_path, "contract.sqlite3")
    memory = _entry_memory(store, cycle_id="cycle_contract")
    _fill(store, fill_id="e10", cycle_id="cycle_contract", position_id="pos_contract",
          side="BUY", quantity=1, price=100, contract_size=10)
    _fill(store, fill_id="x10", cycle_id="cycle_contract", position_id="pos_contract",
          side="SELL", quantity=1, price=101, contract_size=10)

    reconcile_decision_outcomes(store, ACCOUNT)

    row = _outcomes(store)[memory["memory_id"]]
    assert row["outcome_status"] == "WIN"
    assert row["outcome_pnl"] == pytest.approx(10.0)
