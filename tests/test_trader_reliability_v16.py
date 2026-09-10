"""v1.6 failure-first regressions for temporal protection and attribution.

These tests enter the durable SQLite/PAPER boundaries used by production.  No
exchange adapter, private credential, or real account is involved.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

from core.analysis.ai_trade_analytics import analyze_ai_trading_ledger
from core.monitoring_runtime import MonitoringRuntime
from core.providers.base import Bar
from core.storage import SQLiteStore
from core.trading.ledger import AccountLedger
from core.trading.position_guardian import PositionGuardian
import pytest


def _paper_position(
    store: SQLiteStore,
    *,
    account_id: str = "v16-a",
    protection_contract: dict | None = None,
    side: str = "BUY",
):
    ledger = AccountLedger(store)
    ledger.create_account(
        account_id,
        mode="PAPER",
        initial_deposit=Decimal("1000"),
        config={"venue": "simulated"},
    )
    ledger.record_trade_fill(
        account_id=account_id,
        instrument_id="BTCUSDT",
        side=side,
        quantity=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("0"),
        mode="PAPER",
        venue="simulated",
        order_id=f"{account_id}-entry",
        trade_id=f"{account_id}-entry-fill",
        position_id=f"{account_id}-position",
        stop_price=90 if side == "BUY" else 110,
        protection_status="ACTIVE",
        protection_contract=protection_contract,
    )
    guardian = PositionGuardian(store, ledger)
    guardian.set_account_scope(account_id)
    return ledger, guardian


class _OnePassStop:
    """Stop event that lets the real Guardian loop execute exactly once."""

    def __init__(self) -> None:
        self.waited = False

    def is_set(self) -> bool:
        return self.waited

    def wait(self, _timeout: float) -> bool:
        self.waited = True
        return True


def test_v16_future_unclosed_bar_end_does_not_advance_deadline(tmp_path):
    store = SQLiteStore(tmp_path / "v16-future-deadline.db")
    store.initialize()
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    ledger, guardian = _paper_position(
        store,
        protection_contract={"time_exit_at": deadline.isoformat()},
    )
    try:
        now = datetime.now(timezone.utc)
        future_bar = Bar(
            now - timedelta(minutes=1),
            100,
            105,
            95,
            101,
            1,
            bar_end=now + timedelta(minutes=14),
            event_at=now,
            sequence=1,
            is_closed=False,
        )

        assert guardian.process_bar("BTCUSDT", future_bar) == []
        assert len(ledger.get_open_positions("v16-a", venue="simulated", mode="PAPER")) == 1
    finally:
        ledger.close()


def test_v16_stream_callback_keeps_wall_clock_deadline_semantics(tmp_path):
    store = SQLiteStore(tmp_path / "v16-runtime-future-deadline.db")
    store.initialize()
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    ledger, guardian = _paper_position(
        store,
        protection_contract={"time_exit_at": deadline.isoformat()},
    )
    runtime = MonitoringRuntime(
        store=store,
        ledger=ledger,
        guardian=guardian,
        service=SimpleNamespace(strategy_mode=False, max_symbols=5),
    )
    try:
        now = datetime.now(timezone.utc)
        runtime._on_stream_bar(
            "BTCUSDT",
            Bar(
                now - timedelta(minutes=1),
                100,
                105,
                95,
                101,
                1,
                bar_end=now + timedelta(minutes=14),
                event_at=now + timedelta(minutes=10),
                sequence=1,
                is_closed=True,
            ),
            False,
        )
        assert len(ledger.get_open_positions("v16-a", venue="simulated", mode="PAPER")) == 1
    finally:
        runtime.ai_coordinator.close()
        ledger.close()


def test_v16_crossing_activation_bar_later_new_extreme_triggers_stop(tmp_path):
    store = SQLiteStore(tmp_path / "v16-crossing-revision.db")
    store.initialize()
    ledger, guardian = _paper_position(store)
    try:
        with store._connect() as db:
            payload = json.loads(
                db.execute(
                    "SELECT payload_json FROM simulated_positions WHERE position_id=?",
                    ("v16-a-position",),
                ).fetchone()[0]
            )
        activation = max(
            datetime.fromisoformat(payload["entry_filled_at"]),
            datetime.fromisoformat(payload["protection_effective_at"]),
        )
        bar_start = activation - timedelta(minutes=10)
        first_event = datetime.now(timezone.utc)
        first = Bar(
            bar_start,
            100,
            105,
            95,
            101,
            1,
            event_at=first_event,
            sequence=1,
        )
        second = Bar(
            bar_start,
            100,
            105,
            85,
            101,
            2,
            event_at=datetime.now(timezone.utc),
            sequence=2,
        )

        assert guardian.process_bar("BTCUSDT", first) == []
        result = guardian.process_bar("BTCUSDT", second)

        assert len(result) == 1
        assert result[0]["reason"] == "STOP"
        assert ledger.get_open_positions("v16-a", venue="simulated", mode="PAPER") == []
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("side", "first_high", "second_high"),
    [("SELL", 105, 115)],
)
def test_v16_crossing_activation_bar_protects_short_on_later_new_extreme(tmp_path, side, first_high, second_high):
    store = SQLiteStore(tmp_path / "v16-crossing-short.db")
    store.initialize()
    ledger, guardian = _paper_position(store, side=side)
    try:
        with store._connect() as db:
            payload = json.loads(
                db.execute(
                    "SELECT payload_json FROM simulated_positions WHERE position_id=?",
                    ("v16-a-position",),
                ).fetchone()[0]
            )
        activation = max(
            datetime.fromisoformat(payload["entry_filled_at"]),
            datetime.fromisoformat(payload["protection_effective_at"]),
        )
        bar_start = activation - timedelta(minutes=10)
        first_event = datetime.now(timezone.utc)
        first = Bar(bar_start, 100, first_high, 95, 101, 1, event_at=first_event, sequence=1)
        second = Bar(
            bar_start,
            100,
            second_high,
            95,
            101,
            2,
            event_at=datetime.now(timezone.utc),
            sequence=2,
        )
        assert guardian.process_bar("BTCUSDT", first) == []
        result = guardian.process_bar("BTCUSDT", second)
        assert len(result) == 1 and result[0]["reason"] == "STOP"
        assert ledger.get_open_positions("v16-a", venue="simulated", mode="PAPER") == []
    finally:
        ledger.close()


def test_v16_guardian_loop_checks_durable_overdue_position_without_feed_and_recovers(tmp_path):
    store = SQLiteStore(tmp_path / "v16-empty-feed-loop.db")
    store.initialize()
    deadline = datetime.now(timezone.utc) - timedelta(seconds=5)
    ledger, guardian = _paper_position(
        store,
        protection_contract={"time_exit_at": deadline.isoformat()},
    )
    try:
        guardian._stop_event = _OnePassStop()
        guardian._run_guardian_loop()

        pending = ledger.get_open_positions("v16-a", venue="simulated", mode="PAPER")
        assert len(pending) == 1
        assert pending[0]["protection_status"] == "DEGRADED"
        assert pending[0]["protection_last_reason"] == "TIME_EXIT"
        assert guardian.check_health() is False
        with store._connect() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM trade_fills WHERE account_id=? AND side='SELL'",
                ("v16-a",),
            ).fetchone()[0] == 0

        recovery_bar = Bar(
            datetime.now(timezone.utc),
            100,
            101,
            99,
            100,
            1,
            event_at=datetime.now(timezone.utc),
            sequence=1,
        )
        recovered = guardian.process_bar("BTCUSDT", recovery_bar)
        assert len(recovered) == 1
        assert recovered[0]["reason"] == "TIME_EXIT"
        assert ledger.get_open_positions("v16-a", venue="simulated", mode="PAPER") == []

        # Re-delivery is a read/reconcile no-op after the durable close.
        assert guardian.process_bar("BTCUSDT", recovery_bar) == []
        with store._connect() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM trade_fills WHERE account_id=? AND side='SELL'",
                ("v16-a",),
            ).fetchone()[0] == 1
    finally:
        ledger.close()


def test_v16_price_only_event_is_a_quote_not_a_missing_ohlc_crash(tmp_path):
    store = SQLiteStore(tmp_path / "v16-price-only.db")
    store.initialize()
    ledger, guardian = _paper_position(store)
    try:
        point = datetime.now(timezone.utc)
        result = guardian.process_bar(
            "BTCUSDT",
            SimpleNamespace(
                timestamp=point,
                event_at=point,
                sequence=1,
                price=85.0,
            ),
        )
        assert len(result) == 1 and result[0]["reason"] == "STOP"
        assert ledger.get_open_positions("v16-a", venue="simulated", mode="PAPER") == []
    finally:
        ledger.close()


def test_v16_window_filter_keeps_full_lifecycle_attribution(tmp_path):
    # The v15 test is the actual API -> runtime -> plan -> gateway -> ledger
    # fixture.  Reuse that production entrance so this regression does not
    # manufacture rows by calling the projection's internals.
    v15 = runpy.run_path(str(Path(__file__).with_name("test_trader_reliability_v15.py")))
    v15["test_v15_strategy_runtime_fill_is_visible_once_in_ai_analysis_projection"](tmp_path)
    db_path = tmp_path / "v15-ai-analysis-projection.db"
    store = SQLiteStore(db_path)
    store.initialize()
    try:
        with store._connect() as db:
            events = [
                datetime.fromisoformat(row[0])
                for row in db.execute(
                    "SELECT event_at FROM trade_fills WHERE account_id=? ORDER BY event_at, rowid",
                    ("v15-analysis-a",),
                ).fetchall()
            ]
        assert len(events) == 2
        cutoff = events[0] + (events[1] - events[0]) / 2

        window = analyze_ai_trading_ledger(
            store,
            "v15-analysis-a",
            from_at=cutoff,
        )
        exit_records = [item for item in window["execution_records"] if item["economic_role"] == "EXIT"]
        assert len(exit_records) == 1
        assert exit_records[0]["decision_path"] == "STRATEGY_DRIVEN"
        position = next(item for item in window["execution_positions"] if item["position_id"] == exit_records[0]["position_id"])
        assert position["attribution"] == "STRATEGY_DRIVEN"
        assert window["execution_summary"]["fill_count"] == 1
        assert window["execution_summary"]["entry_fill_count"] == 0
        assert window["execution_summary"]["exit_fill_count"] == 1
        assert window["performance_by_attribution"]["STRATEGY_DRIVEN"]["realized_pnl_usdt"] == exit_records[0]["realized_pnl_usdt"]
    finally:
        # SQLiteStore's operation-scoped connections are closed by the
        # context manager; this is intentionally a read-only projection.
        pass


def test_v16_window_pnl_does_not_include_earlier_exit_on_same_position(tmp_path):
    v15 = runpy.run_path(str(Path(__file__).with_name("test_trader_reliability_v15.py")))
    v15["test_v15_strategy_runtime_fill_is_visible_once_in_ai_analysis_projection"](tmp_path)
    store = SQLiteStore(tmp_path / "v15-ai-analysis-projection.db")
    store.initialize()
    ledger = AccountLedger(store)
    try:
        with store._connect() as db:
            fills = db.execute(
                "SELECT position_id, event_at FROM trade_fills WHERE account_id=? ORDER BY event_at, rowid",
                ("v15-analysis-a",),
            ).fetchall()
        entry_at = datetime.fromisoformat(fills[0][1])
        later_exit_at = datetime.fromisoformat(fills[1][1])
        prior_exit_at = entry_at + (later_exit_at - entry_at) / 3
        ledger.record_trade_fill(
            account_id="v15-analysis-a",
            instrument_id="BTCUSDT",
            side="SELL",
            quantity=Decimal("0.25"),
            price=Decimal("99"),
            fee=Decimal("0"),
            mode="PAPER",
            venue="simulated",
            order_id="v16-prior-exit",
            trade_id="v16-prior-exit-fill",
            position_id=fills[0][0],
            reduce_only=True,
            event_at=prior_exit_at,
        )

        cutoff = prior_exit_at + (later_exit_at - prior_exit_at) / 2
        window = analyze_ai_trading_ledger(store, "v15-analysis-a", from_at=cutoff)
        exit_records = [item for item in window["execution_records"] if item["economic_role"] == "EXIT"]
        assert len(exit_records) == 1
        assert window["execution_summary"]["realized_pnl_usdt"] == exit_records[0]["realized_pnl_usdt"]
        position_realized = next(
            item["realized_pnl"]
            for item in window["execution_positions"]
            if item["position_id"] == fills[0][0]
        )
        assert position_realized != window["execution_summary"]["realized_pnl_usdt"]
    finally:
        ledger.close()
