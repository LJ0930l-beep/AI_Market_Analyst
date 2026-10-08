from datetime import datetime, timedelta, timezone

import pytest

from core.storage import SQLiteStore


def _bar(start: datetime, *, source: str, close: float = 100.0) -> dict:
    end = start + timedelta(minutes=15)
    return {
        "timestamp": start,
        "bar_end": end,
        "open": close - 0.5,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": 12.0,
        "is_closed": True,
        "available_at": end,
        "source": source,
        "volume_unit": "contracts",
        "revision_id": f"{start.isoformat()}-{source}",
    }


def _bootstrap(now: datetime, *, synthetic_marker: object = False) -> dict:
    bars = [
        _bar(now - timedelta(minutes=30), source="gate_native_rest:last", close=99.0),
        _bar(now - timedelta(minutes=15), source="gate_native_rest:last", close=100.0),
    ]
    mark = [_bar(now - timedelta(minutes=15), source="gate_native_rest:mark", close=100.5)]
    index = [_bar(now - timedelta(minutes=15), source="gate_native_rest:index", close=100.25)]
    quality = {"status": "READY", "closed_15m_bars": len(bars)}
    if synthetic_marker is not _MISSING:
        quality["synthetic"] = synthetic_marker
    event_ms = int((now - timedelta(minutes=1)).timestamp() * 1000)
    return {
        "provider": "gate",
        "environment": "TESTNET_PUBLIC",
        "symbol": "BTCUSDT",
        "native_symbol": "BTC_USDT",
        "quote": {"timestamp": now.isoformat(), "last": 100.0},
        "bars": {"15m": bars},
        "mark_bars": mark,
        "index_bars": index,
        "funding": {
            "history": [{"timestamp": event_ms, "fundingRate": 0.0002}],
            "open_interest_history": [
                {"timestamp": event_ms, "openInterestAmount": 1234.0},
            ],
        },
        "order_book": {
            "sequence": "1001",
            "event_at": now.isoformat(),
            "source": "gate_native_rest_order_book",
            "quality_status": "VALID",
            "bids": [[99.9, 2.0]],
            "asks": [[100.1, 2.0]],
        },
        "trades": [{
            "trade_id": "trade-1",
            "event_at": now.isoformat(),
            "side": "buy",
            "price": 100.0,
            "size": 1.5,
        }],
        "liquidations": [{
            "event_at": now.isoformat(),
            "side": "long",
            "size": 1.0,
            "price": 99.0,
        }],
        "quality": quality,
    }


_MISSING = object()


def _evidence_counts(store: SQLiteStore) -> dict[str, int]:
    with store._connect() as db:
        return {
            "bootstrap_runs": db.execute(
                "SELECT COUNT(*) FROM gate_bootstrap_runs WHERE symbol='BTCUSDT'"
            ).fetchone()[0],
            "gate_identity_bars": db.execute(
                "SELECT COUNT(*) FROM market_bar_versions WHERE symbol='BTCUSDT' AND venue='gate'"
            ).fetchone()[0],
            "derivative_bars": db.execute(
                "SELECT COUNT(*) FROM gate_derivative_bars WHERE symbol='BTCUSDT'"
            ).fetchone()[0],
            "funding": db.execute(
                "SELECT COUNT(*) FROM gate_funding_history WHERE symbol='BTCUSDT'"
            ).fetchone()[0],
            "open_interest": db.execute(
                "SELECT COUNT(*) FROM gate_open_interest WHERE symbol='BTCUSDT'"
            ).fetchone()[0],
            "order_books": db.execute(
                "SELECT COUNT(*) FROM gate_order_book_snapshots WHERE symbol='BTCUSDT'"
            ).fetchone()[0],
            "trades": db.execute(
                "SELECT COUNT(*) FROM gate_trades WHERE symbol='BTCUSDT'"
            ).fetchone()[0],
            "liquidations": db.execute(
                "SELECT COUNT(*) FROM gate_liquidations WHERE symbol='BTCUSDT'"
            ).fetchone()[0],
            "realtime_quotes": db.execute(
                "SELECT COUNT(*) FROM market_realtime_state WHERE symbol='BTCUSDT'"
            ).fetchone()[0],
        }


def _initialized_store(tmp_path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "synthetic-gate-bootstrap.sqlite3")
    store.initialize()
    return store


def test_synthetic_bootstrap_is_rejected_before_any_gate_evidence_is_written(tmp_path):
    store = _initialized_store(tmp_path)
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    synthetic = _bootstrap(now, synthetic_marker=True)

    with pytest.raises(ValueError, match="GATE_BOOTSTRAP_SYNTHETIC_DATA_REJECTED"):
        store.save_gate_bootstrap(synthetic, now=now)

    assert _evidence_counts(store) == {
        "bootstrap_runs": 0,
        "gate_identity_bars": 0,
        "derivative_bars": 0,
        "funding": 0,
        "open_interest": 0,
        "order_books": 0,
        "trades": 0,
        "liquidations": 0,
        "realtime_quotes": 0,
    }

    # Synthetic and genuine copies share the stable source-hash inputs. Since
    # the rejected copy writes no audit row, it cannot reserve the genuine
    # bootstrap ID or poison later native evidence.
    native = _bootstrap(now, synthetic_marker=False)
    result = store.save_gate_bootstrap(native, now=now)
    assert result["synthetic"] is False
    assert _evidence_counts(store)["bootstrap_runs"] == 1


@pytest.mark.parametrize("malformed_flag", ["false", 0, 1, None])
def test_malformed_synthetic_flag_is_rejected_before_any_gate_evidence_is_written(
    tmp_path, malformed_flag,
):
    store = _initialized_store(tmp_path)
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="GATE_BOOTSTRAP_SYNTHETIC_FLAG_INVALID"):
        store.save_gate_bootstrap(_bootstrap(now, synthetic_marker=malformed_flag), now=now)

    assert all(value == 0 for value in _evidence_counts(store).values())


def test_native_bootstrap_false_or_missing_synthetic_flag_remains_idempotent(tmp_path):
    store = _initialized_store(tmp_path)
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    native = _bootstrap(now, synthetic_marker=True)
    native["quality"]["synthetic"] = False

    first = store.save_gate_bootstrap(native, now=now)
    after_first = _evidence_counts(store)

    missing_flag_replay = _bootstrap(now, synthetic_marker=_MISSING)
    second = store.save_gate_bootstrap(missing_flag_replay, now=now)
    after_second = _evidence_counts(store)

    assert first["bootstrap_id"] == second["bootstrap_id"]
    assert first["synthetic"] is False and second["synthetic"] is False
    assert after_first == after_second
    assert after_first == {
        "bootstrap_runs": 1,
        "gate_identity_bars": 4,
        "derivative_bars": 4,
        "funding": 1,
        "open_interest": 1,
        "order_books": 1,
        "trades": 1,
        "liquidations": 1,
        "realtime_quotes": 0,
    }
