import json
from datetime import datetime, timezone

from core.storage import SQLiteStore
from core.trading.ai_session_coordinator import (
    _calibration_prompt_evidence, _compact_market_snapshot, _owned_gate_order_ids, _select_prompt_news,
)
from core.trading.execution_gateway import ExecutionGateway, TradingMode


def test_calibration_statistics_do_not_override_active_strategy_style():
    projected = _calibration_prompt_evidence({
        "status": "READY", "sample_size": 500,
        "profile": {"entry_style": "CONSERVATIVE_PULLBACK",
                    "order_preference": "MARKET", "max_concurrent_positions": 1,
                    "replay": {"mean_return": 0.01, "sample_size": 500}},
    })
    assert projected["profile"] == {"replay": {"mean_return": 0.01, "sample_size": 500}}
    assert "entry_style" not in json.dumps(projected)


def test_news_uses_one_revision_per_same_headline_event():
    rows = [
        {"revision_id": "a", "scope": "SYMBOL", "symbol": "BTCUSDT", "title": "BTC ETF inflows rise", "source": "source-a"},
        {"revision_id": "b", "scope": "SYMBOL", "symbol": "BTCUSDT", "title": " BTC  ETF inflows rise ", "source": "source-b"},
        {"revision_id": "c", "scope": "MARKET_WIDE", "title": "Fed holds rates", "source": "source-c"},
    ]
    selected = _select_prompt_news(rows, ("BTCUSDT",), limit=4)
    assert [item["revision_id"] for item in selected] == ["a", "c"]


def test_ai_receives_verified_contract_leverage_ceiling_without_raw_market_dump():
    compact = _compact_market_snapshot({
        "price": 100,
        "market": {"leverage_max": 50, "contractSize": 0.01, "taker": 0.0005,
                   "limits": {"amount": {"min": 1}}, "precision": {"amount": 1, "price": 0.1},
                   "raw": {"untrusted": "ignore"}},
    })
    assert compact["contract_rules"] == {"leverage_max": 50, "contractSize": 0.01,
                                         "min_contracts": 1, "contract_step": 1, "price_tick": 0.1}
    assert compact["fee_rate"] == 0.0005
    assert "raw" not in json.dumps(compact)


def test_gate_pending_order_ownership_requires_same_account_and_native_id(tmp_path):
    store = SQLiteStore(tmp_path / "ownership.sqlite3")
    store.initialize()
    ExecutionGateway(store)
    now = datetime.now(timezone.utc).isoformat()
    with store._connect() as db:
        db.execute(
            """INSERT INTO order_intents
               (intent_id,idempotency_key,account_id,mode,instrument_id,side,order_type,
                quantity,payload_hash,status,execution_result_json,created_at,updated_at,venue,reduce_only)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("intent_ai_owned", "intent_ai_owned", "gate_live", "LIVE", "ETHUSDT",
             "LONG", "limit", 1, "hash", "FILLED", json.dumps({
                 "order_id": "111", "protection_orders": [{"leg": "stop_loss", "order_id": "222"}]
             }), now, now, "gate", 0),
        )
    assert _owned_gate_order_ids(store, "gate_live", TradingMode.LIVE) == {"111", "222"}
    assert _owned_gate_order_ids(store, "gate_testnet", TradingMode.TESTNET) == set()
