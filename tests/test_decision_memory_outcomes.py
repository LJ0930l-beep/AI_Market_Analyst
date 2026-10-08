"""Outcome reconciliation must turn closed positions into usable memory.

Regression guard for the second broken link: ``update_memory_outcome`` had no
caller, so ``memory_for_prompt`` returned a permanently null ``outcome_status``
and the model could never tell a winning setup from a losing one.
"""
from datetime import datetime, timedelta, timezone
import json

import pytest

from core.storage import SQLiteStore
from core.trading.decision_memory import (
    list_decision_memory,
    memory_for_prompt,
    reconcile_decision_outcomes,
    record_decision_memory,
)

ACCOUNT = "paper_test"


def _store(tmp_path, name):
    store = SQLiteStore(tmp_path / name)
    store.initialize()
    return store


def _fill(store, *, fill_id, cycle_id, position_id, side, quantity, price, fee=0.0,
          contract_size=1.0, fee_source="REMOTE_ADAPTER", account_id=ACCOUNT, mode="PAPER"):
    with store._connect() as db:
        db.execute(
            """INSERT INTO trade_fills
               (fill_id, account_id, venue, mode, order_id, trade_id, position_id, symbol,
                side, quantity, price, fee, fee_amount, fee_currency, contract_size,
                status, payload_json, created_at, event_at, cycle_id, fee_source)
               VALUES (?, ?, 'simulated', ?, ?, ?, ?, 'BTCUSDT', ?, ?, ?, ?, ?, 'USDT', ?,
                       'RECORDED', '{}', ?, ?, ?, ?)""",
            (
                fill_id, account_id, mode, f"order_{fill_id}", f"trade_{fill_id}", position_id,
                side, str(quantity), str(price), str(fee), str(fee), str(contract_size),
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
                cycle_id, fee_source,
            ),
        )


def _entry_memory(store, *, cycle_id, action="OPEN_LONG", symbol="BTCUSDT"):
    return record_decision_memory(
        store,
        account_id=ACCOUNT,
        provider="simulated",
        environment="paper",
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


def test_long_term_memory_keeps_older_entries_and_settled_outcome_on_upsert(tmp_path):
    store = _store(tmp_path, "durable.sqlite3")
    oldest = _entry_memory(store, cycle_id="cycle_oldest")
    for index in range(24):
        _entry_memory(store, cycle_id=f"cycle_new_{index}")
    with store._connect() as db:
        count = db.execute("SELECT COUNT(*) FROM ai_decision_memory WHERE account_id=?", (ACCOUNT,)).fetchone()[0]
        preserved = db.execute("SELECT memory_id FROM ai_decision_memory WHERE cycle_id='cycle_oldest'").fetchone()
    assert count == 25 and preserved[0] == oldest["memory_id"]
    assert len(list_decision_memory(store, ACCOUNT)) == 20
    _fill(store, fill_id="old_entry", cycle_id="cycle_oldest", position_id="pos_oldest",
          side="BUY", quantity=1, price=100)
    _fill(store, fill_id="old_exit", cycle_id="cycle_oldest", position_id="pos_oldest",
          side="SELL", quantity=1, price=105)
    result = reconcile_decision_outcomes(store, ACCOUNT)
    assert any(item["memory_id"] == oldest["memory_id"] for item in result["resolved"])
    _entry_memory(store, cycle_id="cycle_oldest")
    with store._connect() as db:
        row = db.execute("SELECT outcome_status, outcome_pnl FROM ai_decision_memory WHERE memory_id=?",
                         (oldest["memory_id"],)).fetchone()
    assert row["outcome_status"] == "WIN" and row["outcome_pnl"] == pytest.approx(5)


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


def test_missing_exchange_fee_does_not_become_a_false_net_result(tmp_path):
    store = _store(tmp_path, "unknown-fee.sqlite3")
    memory = _entry_memory(store, cycle_id="cycle_fee_unknown")
    _fill(store, fill_id="unknown_entry", cycle_id="cycle_fee_unknown", position_id="pos_fee_unknown",
          side="BUY", quantity=1, price=100, fee_source="REMOTE_ADAPTER_FEE_UNKNOWN")
    _fill(store, fill_id="known_exit", cycle_id="cycle_fee_unknown", position_id="pos_fee_unknown",
          side="SELL", quantity=1, price=110)

    result = reconcile_decision_outcomes(store, ACCOUNT)

    assert result["resolved"] == []
    assert _outcomes(store)[memory["memory_id"]]["outcome_status"] is None


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


def test_verified_gate_lesson_survives_twenty_four_newer_wait_rows_and_8k_compaction(tmp_path):
    from core.trading.ai_session_coordinator import _compact_decision_experience, _fit_prompt_payload
    from core.trading.gate_trade_settlement import GateTradeSettlementService

    store = _store(tmp_path, "verified-lesson.sqlite3")
    service = GateTradeSettlementService(store)
    opened_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    closed_at_ms = int(datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp() * 1000)
    memory = record_decision_memory(
        store, account_id="gate_live", provider="gate", environment="live",
        cycle_id="cycle-native-lesson", session_id="session-native", candidate_id=None,
        symbol="BTCUSDT", action="OPEN_LONG", cycle_status="EXECUTED",
        decision_at=opened_at, reason="原生证据已核验的入场",
        payload={"strategy_template_id": "trend_15m", "strategy_id": "ema_trend_v2"},
    )
    episode = {
        "episode_id": "episode-native-lesson", "account_id": "gate_live", "environment": "live",
        "contract": "BTC_USDT", "canonical_symbol": "BTCUSDT", "entry_order_id": "45678",
        "entry_intent_id": "intent_ai_native_lesson", "cycle_id": "cycle-native-lesson",
        "side": "LONG", "strategy_id": "ema_trend_v2", "strategy_version": "v2",
    }
    settlement = {
        "status": "SETTLED_FULL_COST", "account_id": "gate_live", "environment": "live",
        "accounting_episode_id": episode["episode_id"], "entry_order_id": episode["entry_order_id"],
        "entry_intent_id": episode["entry_intent_id"], "total_pnl": "5.25",
        "settlement_currency": "USDT", "fee_status": "VERIFIED", "fee_effect": "-0.75",
        "fee_currency": "USDT", "fee_sources": ["native_trade"],
        "funding_status": "VERIFIED", "funding_effect": "0.10",
        "exit_trade_ids": ["native-exit-1"], "position_close_evidence_id": "position-close-1",
        "closed_at_ms": closed_at_ms, "pnl_source": "NATIVE_TRADE_AND_POSITION_CLOSE",
    }
    with store._connect() as db:
        service._ensure(db)
        db.execute(
            """INSERT INTO gate_accounting_episodes
               (episode_id,account_id,environment,contract,canonical_symbol,entry_order_id,entry_intent_id,
                cycle_id,memory_id,side,strategy_id,strategy_version,identity_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,NULL,?,?,?,?,?)""",
            (episode["episode_id"], episode["account_id"], episode["environment"], episode["contract"],
             episode["canonical_symbol"], episode["entry_order_id"], episode["entry_intent_id"],
             episode["cycle_id"], episode["side"], episode["strategy_id"], episode["strategy_version"],
             "{}", opened_at.isoformat()),
        )
        db.execute(
            """INSERT INTO gate_episode_settlements
               (settlement_id,episode_id,status,settlement_currency,total_pnl,fee_effect,fee_status,
                funding_effect,funding_status,pnl_source,settlement_json,settled_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("settlement-native-lesson", episode["episode_id"], settlement["status"], "USDT",
             settlement["total_pnl"], settlement["fee_effect"], settlement["fee_status"],
             settlement["funding_effect"], settlement["funding_status"], settlement["pnl_source"],
             json.dumps(settlement), "2026-09-02T00:00:00+00:00"),
        )
    resolved = service._resolve_memory(episode, settlement)
    assert resolved and resolved["updated"] is True
    assert resolved["memory_id"] == memory["memory_id"]

    for index in range(24):
        record_decision_memory(
            store, account_id="gate_live", provider="gate", environment="live",
            cycle_id=f"cycle-wait-{index}", session_id="session-native", candidate_id=None,
            symbol="BTCUSDT", action="WAIT", cycle_status="WAITING",
            decision_at=datetime(2026, 9, 3, tzinfo=timezone.utc) + timedelta(minutes=index),
            reason="本轮没有可执行入场条件",
        )

    retrieved = memory_for_prompt(store, "gate_live")
    lessons = [row for row in retrieved if row.get("memory_role") == "VERIFIED_SETTLED_LESSON"]
    assert len(lessons) == 1
    assert lessons[0]["outcome_status"] == "WIN" and lessons[0]["outcome_pnl"] == pytest.approx(5.25)
    assert lessons[0]["strategy_id"] == "ema_trend_v2"
    recent_decisions, experience = _compact_decision_experience(retrieved)
    assert recent_decisions[0]["memory_role"] == "VERIFIED_SETTLED_LESSON"
    assert recent_decisions[0]["outcome_evidence"]["accounting_episode_id"] == episode["episode_id"]
    assert experience["recent_closed_trades"][0]["strategy_template_id"] == "trend_15m"
    full_evidence = experience["recent_closed_trades"][0]["outcome_evidence"]
    assert full_evidence["fee_effect"] == "-0.75" and full_evidence["funding_effect"] == "0.10"
    assert full_evidence["exit_trade_ids"] == ["native-exit-1"]

    projected, metadata = _fit_prompt_payload(
        {"decision_memory": recent_decisions, "strategy_experience": experience,
         "market_radar": {"optional_history": "x" * 20_000}},
        "system", context_length=8192, reserve=1024,
    )
    assert metadata["compacted"] is True and "keep_current_radar_facts_only" in metadata["steps"]
    compact_memory = projected["decision_memory"][0]
    assert compact_memory["memory_role"] == "VERIFIED_SETTLED_LESSON"
    assert compact_memory["strategy_id"] == "ema_trend_v2"
    assert compact_memory["outcome_evidence"]["accounting_episode_id"] == episode["episode_id"]
    assert projected["strategy_experience"]["recent_closed_trades"][0]["outcome_evidence"]["fee_effect"] == "-0.75"


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


def test_gate_memory_is_not_settled_from_local_fill_mirror(tmp_path):
    store = _store(tmp_path, "gate-local-mirror.sqlite3")
    memory = record_decision_memory(
        store,
        account_id="gate_testnet",
        provider="gate",
        environment="testnet",
        cycle_id="cycle_gate_unverified",
        session_id="sess_gate",
        candidate_id=None,
        symbol="BTCUSDT",
        action="OPEN_LONG",
        cycle_status="EXECUTED",
        decision_at=datetime.now(timezone.utc),
        reason="test Gate entry",
        decision_origin="MODEL",
    )
    _fill(store, fill_id="gate_e1", cycle_id="cycle_gate_unverified", position_id="local_pos",
          side="BUY", quantity=1, price=100, account_id="gate_testnet", mode="TESTNET")
    _fill(store, fill_id="gate_x1", cycle_id="cycle_gate_unverified", position_id="local_pos",
          side="SELL", quantity=1, price=110, account_id="gate_testnet", mode="TESTNET")

    result = reconcile_decision_outcomes(store, "gate_testnet")

    assert result == {"considered": 0, "pending": 0, "resolved": []}
    row = {item["memory_id"]: item for item in list_decision_memory(store, "gate_testnet")}[
        memory["memory_id"]
    ]
    assert row["outcome_status"] is None
    assert row["outcome_pnl"] is None
