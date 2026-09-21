"""Read models for the market microstructure and intelligence surfaces.

Every number in this projection is derived from persisted exchange events or
an authenticated inbound webhook.  Missing feeds remain explicitly
unavailable; the product never fills chart gaps with synthetic values.
"""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import threading
from typing import Any, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from core.trading.institutional_schema import ensure_institutional_trader_schema


_BINANCE_CACHE: dict[str, tuple[datetime, dict[str, Any]]] = {}
_BINANCE_CACHE_LOCK = threading.RLock()
_GATE_VOLUME_TIMEFRAME = "15m"
_GATE_VOLUME_FRESHNESS = timedelta(minutes=45)
_GATE_VOLUME_LOOKBACK = timedelta(hours=12)


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        point = value
    elif value is None or value == "":
        return None
    else:
        text = str(value).strip()
        try:
            numeric = float(text)
        except (TypeError, ValueError):
            numeric = None
        if numeric is not None and math.isfinite(numeric):
            if numeric > 10_000_000_000:
                numeric /= 1000.0
            try:
                point = datetime.fromtimestamp(numeric, timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        else:
            try:
                point = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None
    if point.tzinfo is None:
        point = point.replace(tzinfo=timezone.utc)
    return point.astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _tables(db: Any) -> set[str]:
    return {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _safe_rows(db: Any, table: str, query: str, params: tuple[Any, ...] = ()) -> list[Any]:
    if table not in _tables(db):
        return []
    return list(db.execute(query, params).fetchall())


def _bucket(point: datetime, minutes: int = 5) -> datetime:
    return point.replace(minute=(point.minute // minutes) * minutes, second=0, microsecond=0)


def _normalize_symbols(symbols: Iterable[str] | None) -> list[str]:
    output: list[str] = []
    for value in symbols or ():
        symbol = "".join(ch for ch in str(value).upper() if ch.isalnum())
        if symbol.endswith("USDTUSDT"):
            symbol = symbol[:-4]
        if symbol.endswith("USDT") and symbol not in output:
            output.append(symbol)
    return output[:12]


def _infer_symbols(db: Any, requested: Iterable[str] | None) -> list[str]:
    normalized = _normalize_symbols(requested)
    if normalized:
        return normalized
    rows = _safe_rows(
        db,
        "gate_trades",
        """SELECT symbol, MAX(received_at) AS last_seen, COUNT(*) AS samples
             FROM gate_trades GROUP BY symbol ORDER BY last_seen DESC LIMIT 8""",
    )
    return _normalize_symbols(row["symbol"] for row in rows)


def _trade_radar(db: Any, symbols: list[str], *, now: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not symbols:
        return [], {"status": "NO_DATA", "source": "gate_public_trades", "as_of": None}
    marks = ",".join("?" for _ in symbols)
    rows = _safe_rows(
        db,
        "gate_trades",
        f"SELECT * FROM gate_trades WHERE symbol IN ({marks}) ORDER BY received_at DESC LIMIT 12000",
        tuple(symbols),
    )
    cutoff = now - timedelta(hours=12)
    buckets: dict[tuple[str, datetime], dict[str, float]] = defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "last": 0.0, "count": 0.0})
    latest: datetime | None = None
    for row in reversed(rows):
        point = _utc(row["event_at"]) or _utc(row["received_at"])
        if point is None or point < cutoff:
            continue
        size = abs(_number(row["size"]) or 0.0)
        price = _number(row["price"])
        side = str(row["side"] or "UNKNOWN").upper()
        if size <= 0 or price is None or side not in {"BUY", "SELL"}:
            continue
        data = buckets[(str(row["symbol"]), _bucket(point))]
        data["buy" if side == "BUY" else "sell"] += size
        data["last"] = price
        data["count"] += 1
        latest = point if latest is None or point > latest else latest
    cumulative: dict[str, float] = defaultdict(float)
    series: list[dict[str, Any]] = []
    for (symbol, point), data in sorted(buckets.items(), key=lambda item: (item[0][0], item[0][1])):
        delta = data["buy"] - data["sell"]
        cumulative[symbol] += delta
        series.append({
            "symbol": symbol,
            "time": point.isoformat(),
            "price": data["last"],
            "buy_contracts": round(data["buy"], 8),
            "sell_contracts": round(data["sell"], 8),
            "delta_contracts": round(delta, 8),
            "cvd_contracts": round(cumulative[symbol], 8),
            "trade_count": int(data["count"]),
        })
    status = "AVAILABLE" if series else "NO_DATA"
    return series, {"status": status, "source": "gate_public_trades_ws_and_rest", "as_of": latest.isoformat() if latest else None, "unit": "contracts", "synthetic": False}


def _volume_bars(db: Any, symbols: list[str], *, now: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Project recent, verified Gate last-price 15m bars from durable storage.

    Historical points may be older than the freshness window so the chart has
    context, but the latest point for each symbol must have been closed and
    observed recently. This prevents a stale cache from appearing live.
    """
    if not symbols:
        return [], {"status": "NO_DATA", "venue": "gate", "source": "gate_derivative_bars", "as_of": None, "timeframe": _GATE_VOLUME_TIMEFRAME, "unit": "contracts", "synthetic": False}
    marks = ",".join("?" for _ in symbols)
    rows = _safe_rows(
        db,
        "gate_derivative_bars",
        f"""SELECT provider, environment, symbol, native_symbol, timeframe, price_type,
                   bar_start, bar_end, available_at, received_at, open, high, low, close, volume,
                   is_closed, quality_status, payload_json
              FROM gate_derivative_bars
             WHERE symbol IN ({marks}) AND provider='gate'
               AND timeframe=? AND price_type='last' AND is_closed=1
               AND quality_status='VALID'
             ORDER BY bar_end DESC, available_at DESC, received_at DESC,
                      environment ASC, provider ASC
             LIMIT 10000""",
        (*symbols, _GATE_VOLUME_TIMEFRAME),
    )
    requested = set(symbols)
    selected: dict[tuple[str, datetime], dict[str, Any]] = {}
    for row in rows:
        symbol = str(row["symbol"]).upper()
        if symbol not in requested:
            continue
        start, end = _utc(row["bar_start"]), _utc(row["bar_end"])
        available, received = _utc(row["available_at"]), _utc(row["received_at"])
        if start is None or end is None or available is None or received is None:
            continue
        if end > now or available > now or received > now:
            continue
        if (end - start).total_seconds() != 900:
            continue
        if str(row["is_closed"]) not in {"1", "True", "true"} or str(row["quality_status"]).upper() != "VALID":
            continue
        open_price, high, low = (_number(row[name]) for name in ("open", "high", "low"))
        close, volume = _number(row["close"]), _number(row["volume"])
        if (
            any(value is None or value <= 0 for value in (open_price, high, low, close))
            or volume is None
            or volume < 0
            or low > min(open_price, close)
            or high < max(open_price, close)
        ):
            continue
        payload = _payload(row["payload_json"])
        if str(payload.get("quality_status") or "VALID").upper() != "VALID":
            continue
        if payload.get("is_closed") is not None and str(payload.get("is_closed")).lower() not in {"1", "true"}:
            continue
        # Multiple environments can persist the same Gate candle. Prefer its
        # freshest observation and emit one point per symbol/bar close.
        key = (symbol, end)
        item = {
            "venue": "gate",
            "symbol": symbol,
            "time": end.isoformat(),
            "bar_start": start.isoformat(),
            "price": close,
            "volume": volume,
            "unit": str(payload.get("volume_unit") or "contracts"),
            "timeframe": _GATE_VOLUME_TIMEFRAME,
            "source": str(payload.get("source") or "gate_derivative_bars"),
            "status": "VALID",
            "environment": str(row["environment"]),
            "available_at": available.isoformat(),
        }
        previous = selected.get(key)
        if previous is None or (item["available_at"], item["environment"]) > (previous["available_at"], previous["environment"]):
            selected[key] = item

    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    history_cutoff = now - _GATE_VOLUME_LOOKBACK
    for (symbol, end), item in selected.items():
        if end >= history_cutoff:
            by_symbol[symbol].append(item)

    series: list[dict[str, Any]] = []
    for symbol in symbols:
        points = sorted(by_symbol.get(symbol, []), key=lambda item: item["time"])
        if not points:
            continue
        latest = points[-1]
        latest_time = _utc(latest["time"])
        latest_available = _utc(latest["available_at"])
        if (
            latest_time is None
            or latest_available is None
            or now - latest_time > _GATE_VOLUME_FRESHNESS
            or now - latest_available > _GATE_VOLUME_FRESHNESS
        ):
            continue
        series.extend(points[-48:])

    latest_as_of = max((_utc(item["time"]) for item in series), default=None)
    status = "AVAILABLE" if series else "NO_DATA"
    return series, {
        "status": status,
        "venue": "gate",
        "source": "gate_derivative_bars",
        "as_of": latest_as_of.isoformat() if latest_as_of else None,
        "timeframe": _GATE_VOLUME_TIMEFRAME,
        "unit": "contracts",
        "synthetic": False,
    }


def _derivatives(db: Any, symbols: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if not symbols:
        return [], [], {"status": "NO_DATA", "source": "gate_public_rest", "as_of": None}
    marks = ",".join("?" for _ in symbols)
    oi_rows = _safe_rows(db, "gate_open_interest", f"SELECT * FROM gate_open_interest WHERE symbol IN ({marks}) ORDER BY event_at DESC LIMIT 1200", tuple(symbols))
    funding_rows = _safe_rows(db, "gate_funding_history", f"SELECT * FROM gate_funding_history WHERE symbol IN ({marks}) ORDER BY event_at DESC LIMIT 1200", tuple(symbols))
    price_rows = _safe_rows(db, "gate_derivative_bars", f"""SELECT symbol, bar_end, close FROM gate_derivative_bars
        WHERE symbol IN ({marks}) AND timeframe='15m' AND price_type='last' AND is_closed=1
        ORDER BY bar_end DESC LIMIT 1200""", tuple(symbols))
    prices: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for row in price_rows:
        point, close = _utc(row["bar_end"]), _number(row["close"])
        if point and close is not None:
            prices[str(row["symbol"])].append((point, close))
    series: list[dict[str, Any]] = []
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in reversed(oi_rows):
        point, oi = _utc(row["event_at"]), _number(row["open_interest"])
        if point is None or oi is None:
            continue
        symbol = str(row["symbol"])
        price = min(prices.get(symbol, []), key=lambda item: abs((item[0] - point).total_seconds()))[1] if prices.get(symbol) else None
        item = {"venue": "gate", "symbol": symbol, "time": point.isoformat(), "open_interest": oi, "price": price, "unit": "contracts"}
        by_symbol[symbol].append(item)
        series.append(item)
    latest_funding: dict[str, dict[str, Any]] = {}
    for row in funding_rows:
        symbol = str(row["symbol"])
        if symbol in latest_funding:
            continue
        rate = _number(row["funding_rate"])
        point = _utc(row["event_at"])
        latest_funding[symbol] = {"venue": "gate", "symbol": symbol, "funding_rate": rate, "funding_rate_pct": rate * 100 if rate is not None else None, "time": point.isoformat() if point else None}
    matrix: list[dict[str, Any]] = []
    for symbol in symbols:
        points = by_symbol.get(symbol, [])
        first, last = (points[0], points[-1]) if points else (None, None)
        oi_change = ((last["open_interest"] / first["open_interest"] - 1) * 100) if first and last and first["open_interest"] else None
        price_change = ((last["price"] / first["price"] - 1) * 100) if first and last and first.get("price") and last.get("price") else None
        funding = latest_funding.get(symbol, {})
        rate_pct = _number(funding.get("funding_rate_pct"))
        crowding = None
        crowding_label = "EVIDENCE_INSUFFICIENT"
        if rate_pct is not None and oi_change is not None:
            crowding = min(100.0, abs(rate_pct) * 1400.0 + min(45.0, abs(oi_change) * 4.0))
            crowding_label = "LONG_CROWDED" if rate_pct > 0.01 else "SHORT_CROWDED" if rate_pct < -0.01 else "BALANCED"
        matrix.append({
            "symbol": symbol,
            "gate": {"open_interest": last["open_interest"] if last else None, "oi_change_pct": round(oi_change, 3) if oi_change is not None else None, **funding},
            "binance": {"status": "NOT_CONNECTED", "reason": "binance_public_feed_not_configured"},
            "price_change_pct": round(price_change, 3) if price_change is not None else None,
            "crowding_score": round(crowding, 1) if crowding is not None else None,
            "crowding_label": crowding_label,
            "score_basis": "abs(funding_rate_pct)*1400 + min(45, abs(oi_change_pct)*4)" if crowding is not None else None,
        })
    latest = max((_utc(item.get("time")) for item in series if item.get("time")), default=None)
    status = "AVAILABLE" if series or latest_funding else "NO_DATA"
    return series, matrix, {"status": status, "source": "gate_public_derivatives_rest", "as_of": latest.isoformat() if latest else None, "synthetic": False, "binance_status": "NOT_CONNECTED"}


def _binance_json(path: str, params: dict[str, Any]) -> Any:
    url = "https://fapi.binance.com" + path + "?" + urlencode(params)
    request = Request(url, method="GET", headers={"Accept": "application/json", "User-Agent": "ai-market-analyst/2.0"})
    with urlopen(request, timeout=6.0) as response:
        if int(response.status) != 200:
            raise RuntimeError(f"BINANCE_HTTP_{response.status}")
        return json.loads(response.read().decode("utf-8"))


def _binance_symbol(symbol: str, *, now: datetime) -> dict[str, Any]:
    with _BINANCE_CACHE_LOCK:
        cached = _BINANCE_CACHE.get(symbol)
        if cached and (now - cached[0]).total_seconds() < 60:
            return dict(cached[1])
    history = _binance_json("/futures/data/openInterestHist", {"symbol": symbol, "period": "5m", "limit": 48})
    premium = _binance_json("/fapi/v1/premiumIndex", {"symbol": symbol})
    if not isinstance(history, list) or not isinstance(premium, dict):
        raise RuntimeError("BINANCE_SCHEMA_INVALID")
    series: list[dict[str, Any]] = []
    for row in history:
        if not isinstance(row, dict):
            continue
        point = _utc(row.get("timestamp"))
        oi = _number(row.get("sumOpenInterest"))
        oi_usdt = _number(row.get("sumOpenInterestValue"))
        if point is None or oi is None:
            continue
        inferred_price = oi_usdt / oi if oi_usdt is not None and oi > 0 else None
        series.append({"venue": "binance", "symbol": symbol, "time": point.isoformat(), "open_interest": oi, "open_interest_usdt": oi_usdt, "price": inferred_price, "unit": "base_asset"})
    first, last = (series[0], series[-1]) if series else (None, None)
    oi_change = ((last["open_interest"] / first["open_interest"] - 1) * 100) if first and last and first["open_interest"] else None
    funding = _number(premium.get("lastFundingRate"))
    result = {
        "status": "AVAILABLE" if series else "NO_DATA",
        "source": "binance_futures_public_rest",
        "symbol": symbol,
        "open_interest": last["open_interest"] if last else None,
        "open_interest_usdt": last.get("open_interest_usdt") if last else None,
        "oi_change_pct": round(oi_change, 3) if oi_change is not None else None,
        "funding_rate": funding,
        "funding_rate_pct": funding * 100 if funding is not None else None,
        "mark_price": _number(premium.get("markPrice")),
        "time": last.get("time") if last else None,
        "series": series,
        "synthetic": False,
    }
    with _BINANCE_CACHE_LOCK:
        _BINANCE_CACHE[symbol] = (now, dict(result))
    return result


def _binance_derivatives(symbols: list[str], *, now: datetime) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    results: dict[str, dict[str, Any]] = {}
    selected = symbols[:8]
    if not selected:
        return [], results
    with ThreadPoolExecutor(max_workers=min(4, len(selected)), thread_name_prefix="aima-binance-radar") as pool:
        futures = {pool.submit(_binance_symbol, symbol, now=now): symbol for symbol in selected}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                results[symbol] = future.result()
            except Exception as exc:
                results[symbol] = {"status": "UNAVAILABLE", "reason": type(exc).__name__, "source": "binance_futures_public_rest", "series": [], "synthetic": False}
    series = [item for symbol in selected for item in results.get(symbol, {}).get("series", [])]
    return series, results


def _liquidations(db: Any, symbols: list[str], *, now: datetime) -> dict[str, Any]:
    params: tuple[Any, ...] = ()
    clause = ""
    if symbols:
        clause = f"WHERE symbol IN ({','.join('?' for _ in symbols)})"
        params = tuple(symbols)
    rows = _safe_rows(db, "gate_liquidations", f"SELECT * FROM gate_liquidations {clause} ORDER BY received_at DESC LIMIT 3000", params)
    cutoff = now - timedelta(hours=24)
    totals = {"LONG": 0.0, "SHORT": 0.0, "UNKNOWN": 0.0}
    counts = {"LONG": 0, "SHORT": 0, "UNKNOWN": 0}
    recent: list[dict[str, Any]] = []
    latest: datetime | None = None
    for row in rows:
        point = _utc(row["event_at"]) or _utc(row["received_at"])
        if point is None or point < cutoff:
            continue
        envelope = _payload(row["payload_json"])
        raw = envelope.get("raw") if isinstance(envelope.get("raw"), dict) else envelope
        size = _number(raw.get("size", raw.get("order_size", raw.get("left"))))
        price = _number(raw.get("fill_price", raw.get("price", raw.get("mark_price"))))
        raw_side = str(raw.get("side") or raw.get("position_side") or "").upper()
        direction = "LONG" if "LONG" in raw_side else "SHORT" if "SHORT" in raw_side else ("LONG" if size is not None and size > 0 else "SHORT" if size is not None and size < 0 else "UNKNOWN")
        notional = abs(size) * price if size is not None and price is not None else None
        counts[direction] += 1
        if notional is not None:
            totals[direction] += notional
        latest = point if latest is None or point > latest else latest
        if len(recent) < 30:
            recent.append({"event_id": row["event_id"], "symbol": row["symbol"], "time": point.isoformat(), "direction": direction, "size_contracts": abs(size) if size is not None else None, "price": price, "estimated_notional": notional, "notional_basis": "abs(size)*price" if notional is not None else None})
    return {"status": "AVAILABLE" if sum(counts.values()) else "NO_DATA", "source": "gate_futures.public_liquidates_websocket", "as_of": latest.isoformat() if latest else None, "window_hours": 24, "counts": counts, "estimated_notional": {key: round(value, 4) for key, value in totals.items()}, "recent": recent, "synthetic": False}


def _onchain(db: Any) -> dict[str, Any]:
    rows = _safe_rows(db, "onchain_flow_events", "SELECT * FROM onchain_flow_events ORDER BY COALESCE(event_at,received_at) DESC LIMIT 100")
    events = [dict(row) for row in rows]
    for item in events:
        item.pop("payload_json", None)
        item.pop("raw_hash", None)
    configured = False
    try:
        import os
        configured = bool(os.environ.get("AIMA_ONCHAIN_WEBHOOK_SECRET", "").strip())
    except Exception:
        configured = False
    return {
        "status": "AVAILABLE" if events else ("WAITING_FOR_WEBHOOK" if configured else "CONFIG_REQUIRED"),
        "events": events,
        "providers": {
            "arkham": {"transport": "user_configured_alert_webhook", "status": "READY" if configured else "CONFIG_REQUIRED"},
            "whale_alert": {"transport": "provider_api_or_websocket", "status": "READY" if configured else "CONFIG_REQUIRED", "free": False},
        },
        "as_of": events[0].get("event_at") or events[0].get("received_at") if events else None,
        "synthetic": False,
    }


def build_market_radar(store: Any, *, symbols: Iterable[str] | None = None, now: datetime | None = None, include_external: bool = False) -> dict[str, Any]:
    observed = now or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    observed = observed.astimezone(timezone.utc)
    with store._connect() as db:
        ensure_institutional_trader_schema(db)
        selected = _infer_symbols(db, symbols)
        cvd, trade_status = _trade_radar(db, selected, now=observed)
        volume, volume_status = _volume_bars(db, selected, now=observed)
        oi, matrix, derivatives_status = _derivatives(db, selected)
        liquidations = _liquidations(db, selected, now=observed)
        onchain = _onchain(db)
    binance_series, binance_by_symbol = _binance_derivatives(selected, now=observed) if include_external else ([], {})
    if include_external:
        for row in matrix:
            remote = binance_by_symbol.get(str(row.get("symbol")), {"status": "UNAVAILABLE", "reason": "BINANCE_PUBLIC_DATA_UNAVAILABLE"})
            row["binance"] = {key: value for key, value in remote.items() if key != "series"}
            if row.get("crowding_score") is None and remote.get("funding_rate_pct") is not None and remote.get("oi_change_pct") is not None:
                rate_pct = float(remote["funding_rate_pct"])
                oi_change = float(remote["oi_change_pct"])
                score = min(100.0, abs(rate_pct) * 1400.0 + min(45.0, abs(oi_change) * 4.0))
                row.update(crowding_score=round(score, 1), crowding_label="LONG_CROWDED" if rate_pct > 0.01 else "SHORT_CROWDED" if rate_pct < -0.01 else "BALANCED", score_basis="binance: abs(funding_rate_pct)*1400 + min(45, abs(oi_change_pct)*4)")
        oi.extend(binance_series)
        derivatives_status["binance_status"] = "AVAILABLE" if any(item.get("status") == "AVAILABLE" for item in binance_by_symbol.values()) else "UNAVAILABLE"
        derivatives_status["source"] = "gate_and_binance_public_derivatives"
        if derivatives_status.get("status") != "AVAILABLE" and binance_series:
            derivatives_status["status"] = "AVAILABLE"
    return {
        "status": "AVAILABLE" if any(item.get("status") == "AVAILABLE" for item in (trade_status, volume_status, derivatives_status, liquidations, onchain)) else "NO_DATA",
        "generated_at": observed.isoformat(),
        "symbols": selected,
        "cvd": {**trade_status, "series": cvd},
        "volume": {**volume_status, "series": volume},
        "open_interest": {**derivatives_status, "series": oi},
        "derivatives_matrix": matrix,
        "liquidations": liquidations,
        "onchain": onchain,
        "cross_market": {
            "status": "CONFIG_REQUIRED",
            "source": None,
            "message": "DXY、US10Y 与 NQ 需要用户配置具备相应授权的实时行情源；BTC.D 尚未连接。",
            "items": [
                {"symbol": "DXY", "value": None, "change_pct": None, "status": "SOURCE_REQUIRED"},
                {"symbol": "US10Y", "value": None, "change_pct": None, "status": "SOURCE_REQUIRED"},
                {"symbol": "NQ", "value": None, "change_pct": None, "status": "SOURCE_REQUIRED"},
                {"symbol": "BTC.D", "value": None, "change_pct": None, "status": "SOURCE_REQUIRED"},
            ],
            "synthetic": False,
        },
    }


def store_onchain_webhook(store: Any, provider: str, payload: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    provider_name = str(provider or "").strip().lower()
    if provider_name not in {"arkham", "whale_alert"}:
        raise ValueError("ONCHAIN_PROVIDER_UNSUPPORTED")
    observed = now or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    received_at = observed.astimezone(timezone.utc).isoformat()
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    raw_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    provider_event_id = payload.get("id") or payload.get("alert_id") or payload.get("transaction", {}).get("hash") if isinstance(payload.get("transaction"), dict) else payload.get("id") or payload.get("alert_id")
    transaction = payload.get("transaction") if isinstance(payload.get("transaction"), dict) else {}
    asset = payload.get("symbol") or payload.get("asset") or transaction.get("symbol")
    amount = _number(payload.get("amount", transaction.get("amount")))
    amount_usd = _number(payload.get("amount_usd", payload.get("amount_usd_value", transaction.get("amount_usd"))))
    from_value = payload.get("from") if isinstance(payload.get("from"), dict) else {}
    to_value = payload.get("to") if isinstance(payload.get("to"), dict) else {}
    from_label = payload.get("from_label") or from_value.get("owner") or from_value.get("address")
    to_label = payload.get("to_label") or to_value.get("owner") or to_value.get("address")
    direction = payload.get("direction") or ("EXCHANGE_INFLOW" if "exchange" in str(to_label or "").lower() else "EXCHANGE_OUTFLOW" if "exchange" in str(from_label or "").lower() else "TRANSFER")
    event_at = _utc(payload.get("timestamp") or payload.get("time") or transaction.get("timestamp"))
    tx_hash = payload.get("hash") or payload.get("transaction_hash") or transaction.get("hash")
    event_id = f"onchain_{provider_name}_{raw_hash[:24]}"
    with store._connect() as db:
        ensure_institutional_trader_schema(db)
        cursor = db.execute(
            """INSERT OR IGNORE INTO onchain_flow_events(
                event_id,provider,provider_event_id,asset,amount,amount_usd,direction,
                from_label,to_label,transaction_hash,event_at,received_at,raw_hash,payload_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, provider_name, str(provider_event_id) if provider_event_id is not None else None, str(asset).upper() if asset else None, amount, amount_usd, str(direction).upper(), str(from_label)[:240] if from_label else None, str(to_label)[:240] if to_label else None, str(tx_hash)[:240] if tx_hash else None, event_at.isoformat() if event_at else None, received_at, raw_hash, raw),
        )
    return {"event_id": event_id, "provider": provider_name, "inserted": cursor.rowcount == 1, "received_at": received_at}


__all__ = ["build_market_radar", "store_onchain_webhook"]
