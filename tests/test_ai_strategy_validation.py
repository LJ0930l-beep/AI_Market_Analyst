from core.analysis.ai_strategy_validation import frozen_strategy_manifest, validate_ai_strategy_evidence
from core.storage import SQLiteStore


def test_four_frozen_strategies_cannot_be_ranked_without_settled_evidence(tmp_path):
    manifest = frozen_strategy_manifest()
    assert len(manifest["strategies"]) == 4
    assert len({item["sha256"] for item in manifest["strategies"]}) == 4
    assert [item["scan_interval_minutes"] for item in manifest["strategies"]] == [5, 15, 15, 15]
    assert manifest == frozen_strategy_manifest()

    store = SQLiteStore(tmp_path / "validation.sqlite3")
    store.initialize()
    with store._connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS ai_led_cycles(cycle_id TEXT, account_id TEXT, action TEXT, status TEXT, model_called INTEGER, payload_json TEXT, created_at TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS ai_decision_memory(memory_id TEXT, account_id TEXT, cycle_id TEXT, decision_at TEXT, outcome_status TEXT, outcome_pnl REAL)")
        db.execute("CREATE TABLE IF NOT EXISTS order_intents(intent_id TEXT, account_id TEXT, cycle_id TEXT, strategy_id TEXT, strategy_version TEXT, reduce_only INTEGER)")
    report = validate_ai_strategy_evidence(store, "gate_testnet")
    assert report["forward_comparison"]["status"] == "EVIDENCE_INSUFFICIENT"
    assert report["default_strategy_recommendation"] is None
    assert all(row["settled_trades"] == 0 for row in report["forward_comparison"]["results"])
    assert report["historical_replay"]["status"] == "NOT_RUN"
