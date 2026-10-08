from datetime import datetime, timezone
from types import SimpleNamespace

from core.storage import SQLiteStore
from core.trading import ai_led_engine as engine_module
from core.trading.ai_led_engine import AICycleContext, AICycleResult, AIActionOutput, AILedDecisionEngine


def test_cycle_is_committed_before_trace_opens_its_own_writer(tmp_path, monkeypatch):
    store = SQLiteStore(str(tmp_path / "cycles.sqlite3"))
    store.initialize()
    engine = AILedDecisionEngine(store=store, execution_gateway=SimpleNamespace(), risk_engine=SimpleNamespace(),
                                ledger=SimpleNamespace(), guardian=SimpleNamespace())
    now = datetime.now(timezone.utc).isoformat()
    context = AICycleContext(cycle_id="tx-test", account_id="unit-only", generation=1,
                             started_at=now, expires_at=now, allowed_instruments=("ETHUSDT",))
    output = AIActionOutput(action="WAIT", instrument_id="ETHUSDT", reason="unit fixture")
    result = AICycleResult(cycle_id=context.cycle_id, action_output=output, status="WAITING", reason=output.reason)
    observed = []
    original = engine_module.persist_stage_trace

    def trace_writer(store, **kwargs):
        # A distinct connection can see the cycle only after the outer writer
        # committed. The trace's own writes must also persist normally.
        with store._connect() as db:
            row = db.execute("SELECT cycle_id FROM ai_led_cycles WHERE cycle_id=?", (kwargs["cycle_id"],)).fetchone()
            observed.append(row is not None)
        original(store, **kwargs)

    monkeypatch.setattr(engine_module, "persist_stage_trace", trace_writer)
    engine._persist_cycle(result, context)
    assert observed == [True]
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM ai_cycle_stages WHERE cycle_id='tx-test'").fetchone()[0] > 0


def test_position_projection_preserves_protection_time_and_source():
    from core.trading.execution_gateway import ExecutionGateway
    rows = ExecutionGateway._gate_remote_positions_for_risk({"source": "AI_REPLAY_SIMULATION", "positions": [{
        "symbol": "ETHUSDT", "side": "LONG", "contracts": 10, "entry_price": 100,
        "stop_price": 95, "take_profit": 110, "opened_at": "2026-10-02T01:00:00Z",
        "position_id": "sim-p1", "contract_size": 0.01, "leverage": 10,
    }]}, account_id="unit-only")
    assert rows[0]["stop_price"] == "95"
    assert rows[0]["take_profit"] == 110
    assert rows[0]["opened_at"] == "2026-10-02T01:00:00Z"
    assert rows[0]["source"] == "AI_REPLAY_SIMULATION"
