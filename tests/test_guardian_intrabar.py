from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.storage import SQLiteStore
from core.providers.base import Bar
from core.trading.ledger import AccountLedger
from core.trading.position_guardian import PositionGuardian
from core.monitoring_runtime import MonitoringRuntime


def setup_position(tmp_path, side="LONG", trailing=None):
    store = SQLiteStore(tmp_path / "intrabar.db")
    store.initialize()
    ledger = AccountLedger(store)
    ledger.create_account("a", mode="PAPER", initial_deposit=Decimal("1000"))
    ledger.record_trade_fill(
        account_id="a", instrument_id="BTCUSDT", side=side,
        quantity=Decimal("1"), price=Decimal("100"), fee=Decimal("0"),
        mode="PAPER", venue="simulated", order_id="seed", trade_id="seed",
        stop_price=90 if side == "LONG" else 110, protection_status="ACTIVE",
        protection_contract={"trailing_protection": trailing} if trailing else None,
    )
    guardian = PositionGuardian(store, ledger)
    guardian.set_account_scope("a")
    return ledger, guardian


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_same_candle_new_extreme_triggers_stop_after_restart(tmp_path, side):
    ledger, guardian = setup_position(tmp_path, side)
    now = datetime.now(timezone.utc)
    first = Bar(now, 100, 102, 99, 101, 1)
    adverse = Bar(now, 100, 102, 80, 85, 2) if side == "LONG" else Bar(now, 100, 120, 99, 115, 2)
    try:
        assert guardian.process_bar("BTCUSDT", first) == []
        # A fresh instance must retain deduplication without dropping revisions.
        guardian = PositionGuardian(guardian.store, ledger)
        guardian.set_account_scope("a")
        result = guardian.process_bar("BTCUSDT", adverse)
        assert len(result) == 1 and result[0]["reason"] == "STOP"
        assert ledger.get_open_positions("a") == []
        assert guardian.process_bar("BTCUSDT", adverse) == []
    finally:
        ledger.close()


def test_trailing_does_not_reuse_old_low_but_observes_new_close(tmp_path):
    ledger, guardian = setup_position(tmp_path, trailing={"type": "DISTANCE", "value": 3})
    now = datetime.now(timezone.utc)
    first = Bar(now, 100, 110, 95, 109, 1)
    try:
        assert guardian.process_bar("BTCUSDT", first) == []
        assert guardian.process_bar("BTCUSDT", first) == []
        # Changed volume is not permission to fill against the pre-tightening low.
        assert guardian.process_bar("BTCUSDT", Bar(now, 100, 110, 95, 109, 2)) == []
        assert guardian.process_bar("BTCUSDT", Bar(now - timedelta(minutes=15), 100, 110, 80, 85, 2)) == []
        result = guardian.process_bar("BTCUSDT", Bar(now, 100, 110, 95, 106, 3))
        assert len(result) == 1
        assert result[0]["price"] == pytest.approx(106 * .999)
    finally:
        ledger.close()


def test_runtime_unclosed_candle_revision_reaches_protection(tmp_path):
    ledger, guardian = setup_position(tmp_path)
    runtime = MonitoringRuntime(
        store=guardian.store, ledger=ledger, guardian=guardian,
        service=SimpleNamespace(strategy_mode=False, max_symbols=5),
    )
    now = datetime.now(timezone.utc)
    try:
        # Exercise the actual stream callback without starting any network workers.
        runtime._on_stream_bar("BTCUSDT", Bar(now, 100, 102, 99, 101, 1), False)
        assert len(ledger.get_open_positions("a")) == 1
        runtime._on_stream_bar("BTCUSDT", Bar(now, 100, 102, 80, 85, 2), False)
        assert ledger.get_open_positions("a") == []
    finally:
        runtime.ai_coordinator.close()
        ledger.close()


def test_older_revision_cannot_trigger_new_trailing_stop(tmp_path):
    ledger, guardian = setup_position(tmp_path, trailing={"type": "DISTANCE", "value": 3})
    now = datetime.now(timezone.utc)
    try:
        guardian.process_bar("BTCUSDT", Bar(now, 100, 110, 95, 109, 10))
        # Lower cumulative volume/high identifies a delayed revision even though
        # its close would cross the newly raised stop.
        assert guardian.process_bar("BTCUSDT", Bar(now, 100, 108, 95, 100, 5)) == []
        assert len(ledger.get_open_positions("a")) == 1
    finally:
        ledger.close()
