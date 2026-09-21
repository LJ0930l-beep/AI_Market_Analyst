from datetime import datetime, timedelta, timezone
import json

from fastapi.testclient import TestClient
from apps.api.main import create_app
import core.analysis.market_radar as radar_module
from core.analysis.market_radar import build_market_radar, store_onchain_webhook
from core.storage import SQLiteStore
from core.trading.dynamic_risk_policy import evaluate_dynamic_risk
from core.trading.institutional_schema import ensure_institutional_trader_schema
from core.trading.strategy_execution import normalize_execution


def test_market_radar_projects_only_persisted_gate_and_webhook_events(tmp_path):
    store = SQLiteStore(tmp_path / "radar.sqlite3")
    store.initialize()
    now = datetime(2026, 9, 14, 2, 30, tzinfo=timezone.utc)
    for index, side in enumerate(("BUY", "SELL", "BUY")):
        point = now - timedelta(minutes=10 - index * 5)
        store.save_gate_trade("BTCUSDT", "BTC_USDT", {"trade_id": str(index), "event_at": point.isoformat(), "side": side, "price": 100 + index, "size": 2 + index}, now=point)
    store.save_gate_open_interest("BTCUSDT", "BTC_USDT", [
        {"timestamp": int((now - timedelta(minutes=15)).timestamp() * 1000), "openInterestAmount": 1000},
        {"timestamp": int(now.timestamp() * 1000), "openInterestAmount": 1100},
    ], now=now)
    store.save_gate_funding("BTCUSDT", "BTC_USDT", {"history": [{"timestamp": int(now.timestamp() * 1000), "fundingRate": 0.0002}]}, now=now)
    store.save_gate_liquidation("BTCUSDT", "BTC_USDT", {"event_at": now.isoformat(), "raw": {"side": "long", "size": "2", "fill_price": "100"}}, now=now)
    stored = store_onchain_webhook(store, "arkham", {"id": "alert-1", "symbol": "USDT", "amount": 500000, "amount_usd": 500000, "from_label": "wallet", "to_label": "exchange", "timestamp": now.isoformat()}, now=now)

    result = build_market_radar(store, symbols=["BTCUSDT"], now=now)
    assert stored["inserted"] is True
    assert result["status"] == "AVAILABLE"
    assert result["cvd"]["source"] == "gate_public_trades_ws_and_rest"
    assert result["cvd"]["series"][-1]["cvd_contracts"] == 3
    assert result["derivatives_matrix"][0]["gate"]["oi_change_pct"] == 10
    assert result["liquidations"]["counts"]["LONG"] == 1
    assert result["liquidations"]["estimated_notional"]["LONG"] == 200
    assert result["onchain"]["events"][0]["direction"] == "EXCHANGE_INFLOW"
    assert result["cross_market"]["synthetic"] is False
    assert all(item["value"] is None for item in result["cross_market"]["items"])


def _gate_bar(start, *, minutes=15, close=100.0, volume=10.0, source="gate_native_rest:last", **overrides):
    end = start + timedelta(minutes=minutes)
    values = {
        "timestamp": start,
        "bar_end": end,
        "open": close - 1,
        "high": close + 1,
        "low": close - 2,
        "close": close,
        "volume": volume,
        "is_closed": True,
        "available_at": end,
        "source": source,
        "volume_unit": "contracts",
        "revision_id": f"{start.isoformat()}-{source}",
    }
    values.update(overrides)
    return values


def test_market_radar_projects_persisted_gate_last_15m_volume_and_deduplicates_environments(tmp_path):
    store = SQLiteStore(tmp_path / "gate-volume-radar.sqlite3")
    store.initialize()
    now = datetime(2026, 9, 21, 2, 30, tzinfo=timezone.utc)
    bars = [
        _gate_bar(now - timedelta(minutes=60), close=98, volume=8),
        _gate_bar(now - timedelta(minutes=45), close=99, volume=9),
        _gate_bar(now - timedelta(minutes=30), close=100, volume=10),
    ]
    bootstrap = {
        "provider": "gate",
        "environment": "TESTNET_PUBLIC",
        "symbol": "BTCUSDT",
        "native_symbol": "BTC_USDT",
        "quote": {"timestamp": now.isoformat()},
        "bars": {"5m": [_gate_bar(now - timedelta(minutes=10), minutes=5, close=98, volume=500)], "15m": bars},
        # These are different price streams and must never be labelled as last.
        "mark_bars": [_gate_bar(bars[-1]["timestamp"], close=1000, volume=9000, source="gate_native_rest:mark")],
        "index_bars": [_gate_bar(bars[-1]["timestamp"], close=2000, volume=8000, source="gate_native_rest:index")],
        "quality": {"status": "READY", "synthetic": False},
    }
    store.save_gate_bootstrap(bootstrap, now=now)
    second_environment = {**bootstrap, "environment": "LIVE_PUBLIC"}
    store.save_gate_bootstrap(second_environment, now=now)

    with store._connect() as db:
        rows = db.execute(
            "SELECT timeframe, price_type, environment, volume FROM gate_derivative_bars WHERE symbol='BTCUSDT' ORDER BY timeframe, price_type"
        ).fetchall()
    assert any(row["timeframe"] == "5m" and row["price_type"] == "last" for row in rows)
    assert {row["environment"] for row in rows if row["timeframe"] == "15m" and row["price_type"] == "last"} == {"TESTNET_PUBLIC", "LIVE_PUBLIC"}

    result = build_market_radar(store, symbols=["BTCUSDT"], now=now)
    volume = result["volume"]
    assert volume["status"] == "AVAILABLE"
    assert volume["source"] == "gate_derivative_bars"
    assert volume["timeframe"] == "15m"
    assert volume["unit"] == "contracts"
    assert volume["as_of"] == (now - timedelta(minutes=15)).isoformat()
    assert [item["volume"] for item in volume["series"]] == [8, 9, 10]
    assert [item["price"] for item in volume["series"]] == [98, 99, 100]
    assert all(item["venue"] == "gate" and item["status"] == "VALID" for item in volume["series"])
    assert all(item["source"] == "gate_native_rest:last" for item in volume["series"])
    assert len({(item["symbol"], item["time"]) for item in volume["series"]}) == 3
    assert all(item["timeframe"] == "15m" for item in volume["series"])


def _insert_derivative_bar(db, *, symbol, start, end, now, environment="TESTNET_PUBLIC", timeframe="15m", price_type="last", is_closed=1, quality="VALID", close=100, volume=10, available_at=None):
    row_id = f"fixture-{symbol}-{environment}-{start.isoformat()}-{timeframe}-{price_type}"
    payload = {
        "source": "gate_native_rest:last",
        "volume_unit": "contracts",
        "quality_status": quality,
        "is_closed": bool(is_closed),
    }
    db.execute(
        """INSERT INTO gate_derivative_bars(
               row_id,provider,environment,symbol,native_symbol,timeframe,price_type,
               bar_start,bar_end,event_time,available_at,received_at,revision_id,
               open,high,low,close,volume,is_closed,raw_hash,quality_status,payload_json
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row_id, "gate", environment, symbol, "BTC_USDT", timeframe, price_type,
            start.isoformat(), end.isoformat(), start.isoformat(),
            (available_at or now).isoformat(), now.isoformat(), row_id,
            close - 1, close + 1, close - 2, close, volume, is_closed,
            row_id, quality, json.dumps(payload),
        ),
    )


def test_market_radar_volume_rejects_unclosed_invalid_future_and_unrequested_samples(tmp_path):
    store = SQLiteStore(tmp_path / "gate-volume-quality.sqlite3")
    store.initialize()
    now = datetime(2026, 9, 21, 2, 30, tzinfo=timezone.utc)
    valid_start = now - timedelta(minutes=45)
    with store._connect() as db:
        ensure_institutional_trader_schema(db)
        _insert_derivative_bar(db, symbol="BTCUSDT", start=valid_start, end=valid_start + timedelta(minutes=15), now=now, close=98, volume=8)
        _insert_derivative_bar(db, symbol="BTCUSDT", start=valid_start - timedelta(minutes=15), end=valid_start, now=now, close=97, volume=-1)
        _insert_derivative_bar(db, symbol="BTCUSDT", start=now - timedelta(minutes=30), end=now - timedelta(minutes=15), now=now, is_closed=0, close=99, volume=900)
        _insert_derivative_bar(db, symbol="BTCUSDT", start=now - timedelta(minutes=15), end=now, now=now, quality="INVALID", close=100, volume=901)
        _insert_derivative_bar(db, symbol="BTCUSDT", start=now, end=now + timedelta(minutes=15), now=now, close=101, volume=902)
        _insert_derivative_bar(db, symbol="ETHUSDT", start=valid_start, end=valid_start + timedelta(minutes=15), now=now, close=2000, volume=999)

    result = build_market_radar(store, symbols=["BTCUSDT"], now=now)
    assert result["volume"]["status"] == "AVAILABLE"
    assert len(result["volume"]["series"]) == 1
    assert result["volume"]["series"][0]["symbol"] == "BTCUSDT"
    assert result["volume"]["series"][0]["volume"] == 8


def test_market_radar_volume_returns_no_data_when_all_gate_bars_are_stale(tmp_path):
    store = SQLiteStore(tmp_path / "gate-volume-stale.sqlite3")
    store.initialize()
    now = datetime(2026, 9, 21, 2, 30, tzinfo=timezone.utc)
    end = now - timedelta(hours=2)
    with store._connect() as db:
        ensure_institutional_trader_schema(db)
        _insert_derivative_bar(db, symbol="BTCUSDT", start=end - timedelta(minutes=15), end=end, now=now, close=98, volume=8, available_at=now - timedelta(hours=2))

    result = build_market_radar(store, symbols=["BTCUSDT"], now=now)
    assert result["volume"] == {
        "status": "NO_DATA",
        "venue": "gate",
        "source": "gate_derivative_bars",
        "as_of": None,
        "timeframe": "15m",
        "unit": "contracts",
        "synthetic": False,
        "series": [],
    }


def test_market_radar_uses_real_binance_derivative_payload_without_synthetic_fallback(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "binance-radar.sqlite3")
    store.initialize()
    now = datetime(2026, 9, 14, 2, 30, tzinfo=timezone.utc)
    radar_module._BINANCE_CACHE.clear()

    def fake_binance(path, params):
        assert params["symbol"] == "BTCUSDT"
        if path.endswith("openInterestHist"):
            return [
                {"timestamp": int((now - timedelta(minutes=5)).timestamp() * 1000), "sumOpenInterest": "100", "sumOpenInterestValue": "10000"},
                {"timestamp": int(now.timestamp() * 1000), "sumOpenInterest": "110", "sumOpenInterestValue": "11550"},
            ]
        return {"symbol": "BTCUSDT", "markPrice": "105", "lastFundingRate": "0.0002"}

    monkeypatch.setattr(radar_module, "_binance_json", fake_binance)
    result = build_market_radar(store, symbols=["BTCUSDT"], now=now, include_external=True)
    row = result["derivatives_matrix"][0]
    assert result["open_interest"]["binance_status"] == "AVAILABLE"
    assert [item["open_interest"] for item in result["open_interest"]["series"]] == [100, 110]
    assert row["binance"]["oi_change_pct"] == 10
    assert row["binance"]["funding_rate_pct"] == 0.02
    assert row["crowding_label"] == "LONG_CROWDED"


def test_market_radar_api_and_authenticated_onchain_webhook(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "radar-api.sqlite3")
    store.initialize()
    monkeypatch.setenv("AIMA_ONCHAIN_WEBHOOK_SECRET", "radar-secret")
    client = TestClient(
        create_app(store=store, llm_provider=None, market_hydration_enabled=False),
        headers={"Host": "localhost:8000", "Origin": "http://localhost:5173"},
    )
    denied = client.post("/v2/onchain/webhooks/arkham", json={"id": "denied"})
    assert denied.status_code == 403
    accepted = client.post(
        "/v2/onchain/webhooks/arkham",
        headers={"X-AIMA-Webhook-Secret": "radar-secret"},
        json={"id": "api-alert-1", "symbol": "USDT", "amount": 42, "from_label": "wallet", "to_label": "exchange"},
    )
    assert accepted.status_code == 200
    radar = client.get("/v2/market-radar?symbols=BTCUSDT&include_external=false")
    assert radar.status_code == 200
    payload = radar.json()
    assert payload["symbols"] == ["BTCUSDT"]
    assert payload["onchain"]["events"][0]["event_id"].startswith("onchain_arkham_")


def test_two_authoritative_losses_trigger_two_hour_entry_lock(tmp_path):
    store = SQLiteStore(tmp_path / "risk.sqlite3")
    store.initialize()
    now = datetime(2026, 9, 14, 2, 30, tzinfo=timezone.utc)
    with store._connect() as db:
        for index, loss in enumerate((-5.0, -3.0)):
            point = now - timedelta(minutes=10 + index)
            db.execute(
                """INSERT INTO trade_fills(fill_id,account_id,venue,mode,symbol,side,quantity,price,status,payload_json,created_at,event_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"loss-{index}", "gate_testnet", "gate", "TESTNET", "BTCUSDT", "SELL", "1", "100", "FILLED", json.dumps({"reduce_only": True, "realized_pnl": loss}), point.isoformat(), point.isoformat()),
            )
    result = evaluate_dynamic_risk(store, "gate_testnet", normalize_execution(), now=now)
    assert result["status"] == "BLOCKED"
    assert result["entry_allowed"] is False
    assert result["reasons"] == ["CONSECUTIVE_LOSS_COOLDOWN"]
    assert result["blocked_until"] == (now - timedelta(minutes=10) + timedelta(hours=2)).isoformat()


def test_unverified_or_missing_pnl_does_not_create_loss_lock(tmp_path):
    store = SQLiteStore(tmp_path / "risk-empty.sqlite3")
    store.initialize()
    result = evaluate_dynamic_risk(store, "gate_testnet", normalize_execution(), now=datetime(2026, 9, 13, 12, tzinfo=timezone.utc))
    assert result["status"] == "READY"
    assert result["entry_allowed"] is True
