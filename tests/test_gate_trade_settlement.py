"""Public, synthetic guards for evidence-first Gate accounting."""
from datetime import datetime, timedelta, timezone
import json

from core.analysis.ai_trade_analytics import analyze_ai_trading_ledger, build_performance_context
from core.storage import SQLiteStore
from core.trading.execution_gateway import ExecutionGateway
from core.trading.gate_account_truth import GateAccountTruthService
from core.trading.gate_trade_settlement import GateTradeSettlementService
from core.trading.ledger import AccountLedger


def _store(tmp_path, name="gate-settlement.sqlite3"):
    store = SQLiteStore(tmp_path / name)
    store.initialize()
    return store


def _intent(store, *, intent_id, order_id, symbol, created_at, side="LONG", status="FILLED", receipt=None):
    # ExecutionGateway owns this production schema. Keep the literal LONG and
    # FILLED receipt shape used by persisted Gate intents in the fixture.
    ExecutionGateway(store)
    now = datetime.now(timezone.utc).isoformat()
    with store._connect() as db:
        db.execute(
            """INSERT INTO order_intents
               (intent_id, idempotency_key, account_id, mode, instrument_id, side,
                order_type, quantity, payload_hash, status, execution_result_json,
                created_at, updated_at, venue, environment, reduce_only, cycle_id)
               VALUES (?, ?, 'gate_live', 'LIVE', ?, ?, 'market', 1, 'fixture', ?, ?, ?, ?,
                       'gate', 'LIVE', 0, ?)""",
            (intent_id, f"key-{intent_id}", symbol, side, status,
             json.dumps(receipt or {"order_id": order_id, "filled": 1}),
             created_at, now, f"cycle-{intent_id}"),
        )


def test_terminal_zero_fill_skips_only_when_no_fill_evidence_exists(tmp_path):
    store = _store(tmp_path)
    old = "2026-09-01T00:00:00+00:00"
    _intent(store, intent_id="zero", order_id="o-zero", symbol="BTCUSDT", created_at=old,
            status="CANCELED", receipt={"order_id": "o-zero", "filled": 0})
    _intent(store, intent_id="evidence", order_id="o-evidence", symbol="ETHUSDT", created_at=old,
            status="CANCELED", receipt={
                "order_id": "o-evidence", "filled": 0,
                "execution_evidence": {"filled": 1},
            })

    targets = GateTradeSettlementService(store)._pending_entry_targets("gate_live", "live", limit=10)

    assert [target["canonical_symbol"] for target in targets] == ["ETHUSDT"]


def test_sync_target_rotation_reaches_new_symbol_after_old_unresolved_targets(tmp_path):
    store = _store(tmp_path)
    service = GateTradeSettlementService(store)
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    for index, symbol in enumerate(("OLD1USDT", "OLD2USDT", "OLD3USDT", "NEWUSDT")):
        created = (base + timedelta(minutes=index)).isoformat()
        _intent(store, intent_id=f"intent-{index}", order_id=f"order-{index}", symbol=symbol,
                created_at=created)

    first = service._pending_entry_targets("gate_live", "live", limit=3)
    for target in first:
        service._mark_sync_target("gate_live", "live", target["canonical_symbol"], "DEGRADED")

    second = service._pending_entry_targets("gate_live", "live", limit=3)

    assert "NEWUSDT" in {target["canonical_symbol"] for target in second}


def test_malformed_native_page_row_downgrades_coverage_and_is_retained(tmp_path):
    store = _store(tmp_path)
    service = GateTradeSettlementService(store)

    result = service.replay_observed_evidence(
        "gate_live",
        "live",
        native_trades=[None],
        trade_coverage={
            "contract": "BTC_USDT", "from_ms": 1_700_000_000_000,
            "to_ms": 1_700_000_001_000, "complete": True,
            "page_count": 1, "page_size": 100,
        },
        native_position_closes=[],
        position_close_coverage={
            "contract": "BTC_USDT", "from_ms": 1_700_000_000_000,
            "to_ms": 1_700_000_001_000, "complete": True,
            "page_count": 1, "page_size": 100,
        },
        position_snapshots=[],
        contract_metadata={"BTC_USDT": {"settle": "USDT"}},
    )

    assert result["status"] == "DEGRADED"
    assert result["trade_coverage"]["complete"] is False
    assert result["counts"]["invalid_trade_rows"] == 1
    with store._connect() as db:
        rejected = db.execute(
            "SELECT evidence_kind, reason FROM gate_remote_evidence_rejections"
        ).fetchone()
    assert rejected["evidence_kind"] == "TRADE"
    assert rejected["reason"] == "NATIVE_TRADE_ID_OR_CONTRACT_MISSING"


def _replay_native_lifecycle_with_first_open_seconds(tmp_path, first_open_seconds):
    store = _store(tmp_path)
    service = GateTradeSettlementService(store)
    entry_fill_ms = 1_790_961_391_900
    exit_fill_ms = entry_fill_ms + 2_100
    close_at_ms = 1_790_961_395_000
    baseline_ms = entry_fill_ms - 10_000
    intent_created_ms = entry_fill_ms - 5_000
    baseline_at = datetime.fromtimestamp(baseline_ms / 1000, tz=timezone.utc)
    intent_created_at = datetime.fromtimestamp(intent_created_ms / 1000, tz=timezone.utc).isoformat()
    service.record_position_snapshot(
        "gate_live", "LIVE", positions=[], observed_at=baseline_at,
        positions_status="AVAILABLE",
    )
    _intent(
        store, intent_id="intent-rounded-open-time", order_id="entry-order",
        symbol="BTC_USDT", created_at=intent_created_at,
        receipt={"order_id": "entry-order", "filled": 1},
    )
    trades = [
        {
            "trade_id": "native-entry", "contract": "BTC_USDT", "order_id": "entry-order",
            "size": "1", "close_size": "0", "price": "100", "fee": "-0.1",
            "fee_currency": "USDT", "point_fee": "0", "create_time_ms": entry_fill_ms,
        },
        {
            "trade_id": "native-exit", "contract": "BTC_USDT", "order_id": "exit-order",
            "size": "-1", "close_size": "-1", "price": "102", "fee": "-0.101",
            "fee_currency": "USDT", "point_fee": "0", "create_time_ms": exit_fill_ms,
        },
    ]
    closes = [
        {
            "voucher_id": f"voucher-{index}", "contract": "BTC_USDT", "side": "long",
            "first_open_time": seconds, "time": close_at_ms // 1000,
            "accum_size": "1", "pnl": "1.799", "pnl_pnl": "2",
            "pnl_fee": "-0.201", "pnl_fund": "0", "pnl_dividend": "0",
        }
        for index, seconds in enumerate(first_open_seconds)
    ]
    coverage = {
        "contract": "BTC_USDT", "from_ms": baseline_ms, "to_ms": close_at_ms,
        "complete": True, "page_count": 1, "page_size": 100,
    }
    return service.replay_observed_evidence(
        "gate_live", "LIVE", native_trades=trades, trade_coverage=coverage,
        native_position_closes=closes, position_close_coverage=coverage,
        position_snapshots=[], contract_metadata={
            "BTC_USDT": {"contract_size": 1, "size_step": 1, "settle": "USDT", "pnl_precision": 8},
        },
    )


def test_second_rounded_position_open_time_can_match_millisecond_entry_fill(tmp_path):
    # The fill occurred at .900 seconds; Gate's integer-second lifecycle time
    # rounded upward by 100 ms but still identifies the unique same position.
    result = _replay_native_lifecycle_with_first_open_seconds(tmp_path, [1_790_961_392])

    assert result["episodes"][0]["status"] == "SETTLED_FULL_COST"
    assert result["episodes"][0]["settlement"]["gross_realized"] == "2"
    assert result["episodes"][0]["settlement"]["total_pnl"] == "1.799"


def test_neighbor_second_position_open_candidates_remain_ambiguous(tmp_path):
    # Floor and ceil whole-second candidates both fall within the documented
    # precision window; the exact-lifecycle cardinality guard must still fail closed.
    result = _replay_native_lifecycle_with_first_open_seconds(
        tmp_path, [1_790_961_391, 1_790_961_392],
    )

    assert result["episodes"][0]["status"] == "UNVERIFIED"
    assert result["episodes"][0]["reason"] == "POSITION_CLOSE_LIFECYCLE_MATCH_AMBIGUOUS"


def _seed_bundle_with_legacy_snapshot(store, *, packet_overrides=None):
    observed_at = "2026-09-30T10:00:00+00:00"
    native = GateAccountTruthService(store)._record(
        "gate_live",
        {
            "status": "AVAILABLE", "api_environment": "LIVE", "observed_at": observed_at,
            "equity": 1000, "available_margin": 900, "used_margin": 100,
            "positions": [], "pending_orders": [], "fills": [],
            "source": "Gate.io v4 LIVE Private API",
        },
        created_at=observed_at,
    )
    truth = {
        "account_id": "gate_live", "api_environment": "LIVE", "status": "AVAILABLE",
        "snapshot_id": native["snapshot_id"], "observed_at": observed_at, "positions": [],
    }
    truth.update(packet_overrides or {})
    with store._connect() as db:
        db.execute(
            """CREATE TABLE ai_led_cycles
               (cycle_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, payload_json TEXT NOT NULL)"""
        )
        db.execute(
            "INSERT INTO ai_led_cycles(cycle_id, account_id, payload_json) VALUES ('cycle-baseline', 'gate_live', ?)",
            (json.dumps({"evidence_bundle_id": "bundle-baseline"}),),
        )
        db.execute(
            """INSERT INTO evidence_bundles
               (bundle_id, as_of, expires_at, frozen_at, input_hash, payload_json, missing_json, status)
               VALUES ('bundle-baseline', ?, ?, ?, 'hash', ?, '[]', 'FROZEN')""",
            (observed_at, observed_at, observed_at,
             json.dumps({"source_evidence": {"account_truth": truth}})),
        )
    target = {
        "intents": [{"cycle_id": "cycle-baseline", "created_at": "2026-10-01T00:00:00+00:00"}]
    }
    return GateTradeSettlementService(store)._import_pre_entry_bundle_snapshots(
        "gate_live", "live", target
    )


def test_legacy_bundle_baseline_requires_exact_native_snapshot_identity(tmp_path):
    valid_store = _store(tmp_path, "legacy-valid.sqlite3")
    assert _seed_bundle_with_legacy_snapshot(valid_store) == 1
    with valid_store._connect() as db:
        row = db.execute(
            "SELECT positions_status, positions_json FROM gate_position_snapshot_evidence"
        ).fetchone()
    assert row["positions_status"] == "AVAILABLE"
    assert json.loads(row["positions_json"]) == []

    unavailable_store = _store(tmp_path, "legacy-unavailable.sqlite3")
    assert _seed_bundle_with_legacy_snapshot(
        unavailable_store, packet_overrides={"positions_status": "UNAVAILABLE"}
    ) == 0

    cross_environment_store = _store(tmp_path, "legacy-cross-env.sqlite3")
    assert _seed_bundle_with_legacy_snapshot(
        cross_environment_store, packet_overrides={"api_environment": "TESTNET"}
    ) == 0


def test_sync_persists_each_history_page_before_advancing_cursor(tmp_path):
    store = _store(tmp_path, "page-crash.sqlite3")
    created_at = "2026-09-30T00:00:00+00:00"
    _intent(store, intent_id="page-intent", order_id="page-order", symbol="BTCUSDT",
            created_at=created_at)

    class Trader:
        def __init__(self):
            self.trade_offsets = []

        def get_market_metadata(self, _symbol):
            return {
                "native_symbol": "BTC_USDT", "quanto_multiplier": 1,
                "size_step": 1, "settle": "USDT",
            }

        def get_trade_history_page(self, _symbol, *, from_ms, to_ms, limit, offset):
            self.trade_offsets.append(offset)
            if offset == 0:
                return {
                    "records": [{
                        "trade_id": "native-entry", "contract": "BTC_USDT",
                        "order_id": "page-order", "size": "1", "close_size": "0",
                        "price": "100", "fee": "-0.1", "fee_currency": "USDT",
                        "point_fee": "0", "create_time": "2026-09-30T00:01:00+00:00",
                    }],
                    "has_more": True, "next_offset": 1,
                }
            return {"records": [], "has_more": False, "next_offset": offset}

        def get_position_close_history_page(self, _symbol, *, from_ms, to_ms, limit, offset):
            return {"records": [], "has_more": False, "next_offset": offset}

    trader = Trader()
    crash_service = GateTradeSettlementService(
        store, clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc)
    )

    def crash_before_cursor(*_args, **_kwargs):
        raise SystemExit("simulated process stop after evidence commit")

    crash_service._save_history_cursor = crash_before_cursor
    try:
        crash_service.sync_from_gate_provider(
            "gate_live", "live", trader, max_symbols=1,
            max_pages_per_stream=1, page_size=1,
        )
    except SystemExit:
        pass
    else:
        raise AssertionError("fixture must stop at the page/cursor boundary")

    with store._connect() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM gate_remote_trade_evidence WHERE trade_id='native-entry'"
        ).fetchone()[0]
        cursor = db.execute(
            "SELECT next_offset, status FROM gate_history_sync_cursors WHERE evidence_kind='TRADES'"
        ).fetchone()
    assert count == 1
    assert cursor["next_offset"] == 0

    recovered = GateTradeSettlementService(
        store, clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc)
    )
    recovered.sync_from_gate_provider(
        "gate_live", "live", trader, max_symbols=1,
        max_pages_per_stream=1, page_size=1,
    )
    recovered.sync_from_gate_provider(
        "gate_live", "live", trader, max_symbols=1,
        max_pages_per_stream=1, page_size=1,
    )

    with store._connect() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM gate_remote_trade_evidence WHERE trade_id='native-entry'"
        ).fetchone()[0]
        cursor = db.execute(
            "SELECT next_offset, status FROM gate_history_sync_cursors WHERE evidence_kind='TRADES'"
        ).fetchone()
    assert trader.trade_offsets[:2] == [0, 0]
    assert 1 in trader.trade_offsets
    assert count == 1
    assert cursor["status"] == "COMPLETE"


def _seed_verified_episodes_and_account_truth(store):
    AccountLedger(store).create_account(
        "gate_live",
        mode="LIVE",
        initial_deposit="10000",
        config={"account_type": "GATE_LIVE", "venue": "gate"},
    )
    now = datetime.now(timezone.utc)
    truth = GateAccountTruthService(store)
    for observed_at, equity in ((now - timedelta(minutes=2), 1000), (now, 1300)):
        timestamp = observed_at.isoformat()
        truth._record(
            "gate_live",
            {
                "status": "AVAILABLE", "api_environment": "LIVE", "observed_at": timestamp,
                "equity": equity, "available_margin": 900, "used_margin": 100,
                "realized_pnl": 50, "unrealized_pnl": 0, "positions": [],
                "pending_orders": [], "fills": [], "source": "synthetic_fixture",
            },
            created_at=timestamp,
        )

    service = GateTradeSettlementService(store)
    with store._connect() as db:
        service._ensure(db)
        for index, (closed_ms, pnl, settled_at) in enumerate((
            (1_700_000_000_000, "2", "2026-10-02T00:00:00+00:00"),
            (1_700_000_100_000, "8", "2026-10-01T00:00:00+00:00"),
        )):
            episode_id = f"episode-{index}"
            db.execute(
                """INSERT INTO gate_accounting_episodes
                   (episode_id, account_id, environment, contract, canonical_symbol,
                    entry_order_id, entry_intent_id, cycle_id, side, identity_json, created_at)
                   VALUES (?, 'gate_live', 'live', 'BTC_USDT', 'BTCUSDT', ?, ?, ?, 'BUY', '{}', ?)""",
                (episode_id, f"entry-order-{index}", f"entry-intent-{index}", f"cycle-{index}", settled_at),
            )
            settlement = {
                "status": "SETTLED_FULL_COST", "total_pnl": pnl,
                "closed_at_ms": closed_ms, "first_native_fill_at_ms": closed_ms - 60_000,
                "settlement_currency": "USDT", "fee_status": "VERIFIED",
                "funding_status": "VERIFIED", "side": "LONG",
            }
            db.execute(
                """INSERT INTO gate_episode_settlements
                   (settlement_id, episode_id, status, settlement_currency, total_pnl,
                    fee_status, funding_status, pnl_source, settlement_json, settled_at)
                   VALUES (?, ?, 'SETTLED_FULL_COST', 'USDT', ?, 'VERIFIED', 'VERIFIED',
                           'SYNTHETIC_NATIVE_FIXTURE', ?, ?)""",
                (f"settlement-{index}", episode_id, pnl, json.dumps(settlement), settled_at),
            )


def test_verified_system_pnl_is_separate_from_account_equity_change_and_curve_uses_close_time(tmp_path):
    store = _store(tmp_path)
    _seed_verified_episodes_and_account_truth(store)

    analysis = analyze_ai_trading_ledger(store, "gate_live", venue="gate", mode="LIVE")
    context = build_performance_context(store, "gate_live", venue="gate", mode="LIVE")

    assert analysis["account"]["net_pnl_usdt"] == 10
    assert analysis["account"]["strategy_net_pnl_usdt"] == 10
    assert analysis["account"]["account_equity_change_usdt"] == 300
    assert [point["accounting_episode_id"] for point in analysis["strategy_equity_curve"]] == [
        "episode-0", "episode-1",
    ]
    assert context["net_pnl_usdt"] == 10
    assert context["strategy_pnl_basis"] == "GATE_VERIFIED_SYSTEM_EPISODES"
    assert context["account_equity_change_usdt"] == 300
