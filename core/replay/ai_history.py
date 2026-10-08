"""Frozen public history and point-in-time readers for actual AI template replay.

Historical REST bars are identified honestly as backtest data, with an explicit
bar-close availability assumption. They are never production execution evidence.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

SCHEMA_VERSION = "ai_template_history_v1"
PUBLIC_ROOT = "https://api.gateio.ws/api/v4/futures/usdt"
FRAME_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}
BENCHMARK_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT")


def utc(value: Any) -> datetime:
    point = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if point.tzinfo is None:
        raise ValueError("REPLAY_TIMEZONE_REQUIRED")
    return point.astimezone(timezone.utc)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def manifest_hash(payload: dict) -> str:
    return digest({key: value for key, value in payload.items() if key != "manifest_sha256"})


def public_get(path: str, params: dict | None = None) -> Any:
    # Only fixed public read endpoints; no credentials or configurable hosts.
    if not (path == "/candlesticks" or path == "/funding_rate" or path.startswith("/contracts/")):
        raise ValueError("REPLAY_PUBLIC_PATH_NOT_ALLOWED")
    url = PUBLIC_ROOT + path + (("?" + urlencode(params)) if params else "")
    for attempt in range(3):
        try:
            request = Request(url, headers={"Accept": "application/json", "User-Agent": "AI-Market-Analyst-Replay/1",
                                           "X-Gate-Size-Decimal": "1"})
            with urlopen(request, timeout=25) as response:
                return json.load(response)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError("REPLAY_PUBLIC_FETCH_FAILED")


def native_symbol(symbol: str) -> str:
    if not symbol.endswith("USDT") or not symbol[:-4].isalnum():
        raise ValueError("REPLAY_USDT_SYMBOL_REQUIRED")
    return symbol[:-4] + "_USDT"


def _cached_public_json(path: Path, endpoint: str, params: dict | None, fetch,
                        *, max_age_seconds: float | None = None) -> tuple[Any, datetime, str]:
    """Retain actual fetch provenance; legacy files expose only their mtime."""
    now = datetime.now(timezone.utc)
    if path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(cached, dict) and cached.get("cache_schema") == "gate_public_replay_cache_v1":
            observed = utc(cached["fetched_at"])
            value, basis = cached["data"], "RECORDED_PUBLIC_FETCH_TIME"
        else:
            observed = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            value, basis = cached, "LEGACY_CACHE_FILE_MTIME_PROXY"
        if max_age_seconds is None or 0 <= (now - observed).total_seconds() <= max_age_seconds:
            return value, observed, basis
    value = fetch(endpoint, params) if params is not None else fetch(endpoint)
    observed = datetime.now(timezone.utc)
    path.write_text(canonical({"cache_schema": "gate_public_replay_cache_v1",
                              "fetched_at": observed.isoformat(), "data": value}), encoding="utf-8")
    return value, observed, "RECORDED_PUBLIC_FETCH_TIME"


def normalize_contract(symbol: str, raw: dict) -> dict:
    size = float(raw["quanto_multiplier"])
    tick = float(raw["order_price_round"])
    minimum, maximum = float(raw["order_size_min"]), float(raw["order_size_max"])
    leverage = float(raw["leverage_max"])
    maker, taker = float(raw.get("maker_fee_rate", 0.0002)), float(raw.get("taker_fee_rate", 0.0005))
    maintenance = float(raw["maintenance_rate"]) if raw.get("maintenance_rate") is not None else None
    risk_base = float(raw["risk_limit_base"]) if raw.get("risk_limit_base") is not None else None
    if not all(math.isfinite(v) and v > 0 for v in (size, tick, minimum, maximum, leverage)):
        raise ValueError("REPLAY_CONTRACT_RULES_INVALID")
    if ((maintenance is not None and (not math.isfinite(maintenance) or not 0 <= maintenance < 1))
            or (risk_base is not None and (not math.isfinite(risk_base) or risk_base <= 0))):
        raise ValueError("REPLAY_MAINTENANCE_RULES_INVALID")
    return {
        "instrument_id": symbol, "native_symbol": native_symbol(symbol), "contract_size": size,
        "price_round": tick, "min_size": minimum, "max_size": maximum, "amount_step": minimum,
        "enable_decimal": raw.get("enable_decimal") is True,
        "leverage_max": leverage, "maker_fee_rate": maker, "taker_fee_rate": taker,
        "maintenance_rate": maintenance, "risk_limit_base": risk_base,
        "maintenance_time_basis": "CURRENT_PUBLIC_BASE_RATE_PROXY_NOT_HISTORICAL_RISK_TIERS",
        "funding_interval": int(raw.get("funding_interval") or 28800),
        "rules_time_basis": "CURRENT_PUBLIC_RULES_PROXY_NOT_HISTORICAL_RULES",
        "public_contract_sha256": digest(raw),
        "market": {"id": native_symbol(symbol), "symbol": symbol, "contract": True, "linear": True,
                   "contractSize": size, "precision": {"amount": minimum, "price": tick},
                   "limits": {"amount": {"min": minimum, "max": maximum}, "leverage": {"max": leverage}},
                   "maker": maker, "taker": taker, "leverage_max": leverage,
                   "maintenance_rate": maintenance, "risk_limit_base": risk_base},
    }


def normalize_bar(symbol: str, frame: str, raw: dict, fetched_at: datetime, contract: dict) -> dict:
    start = datetime.fromtimestamp(int(raw["t"]), timezone.utc)
    end = start + timedelta(minutes=FRAME_MINUTES[frame])
    values = {key: float(raw[field]) for key, field in
              (("open", "o"), ("high", "h"), ("low", "l"), ("close", "c"), ("volume", "v"))}
    if (not all(math.isfinite(v) for v in values.values()) or min(values[k] for k in ("open", "high", "low", "close")) <= 0
            or values["volume"] < 0 or values["high"] < max(values["open"], values["close"], values["low"])
            or values["low"] > min(values["open"], values["close"])):
        raise ValueError("REPLAY_BAR_INVALID")
    return {
        "instrument_id": symbol, "symbol": symbol, "timeframe": frame,
        "instrument_key": f"gate:perpetual:{native_symbol(symbol)}:USDT:last",
        "venue": "gate", "market_type": "perpetual", "native_symbol": native_symbol(symbol),
        "settle_currency": "USDT", "price_type": "last", "provider": "gate",
        "bar_start": start.isoformat(), "bar_end": end.isoformat(),
        "available_at": end.isoformat(), "fetched_at": fetched_at.isoformat(),
        "availability_basis": "HISTORICAL_BAR_CLOSE_ASSUMPTION", "source": "gate_native_rest:last",
        "quality_status": "VALID", "is_closed": True, "synthetic": False,
        "raw_hash": digest(raw), "revision_id": digest(raw), "volume_unit": "contracts",
        "base_volume": values["volume"] * contract["contract_size"], **values,
        "payload_json": {"provider": "gate", "source": "gate_native_rest:last", "synthetic": False},
    }


def archived_news(database: Path | None, start: datetime, end: datetime) -> list[dict]:
    if database is None:
        return []
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute("SELECT event_id,title,summary,published_at,known_at,revision_known_at,"
            "affected_symbols_json,source,source_type,category,importance,url,credibility_score,primary_source "
            "FROM phase6_events WHERE known_at>=? AND known_at<=? ORDER BY known_at,event_id",
            ((start - timedelta(hours=48)).isoformat(), end.isoformat())).fetchall()
        news = []
        for row in rows:
            item = dict(row)
            try:
                known = max(utc(item["known_at"]), utc(item["revision_known_at"] or item["known_at"]))
                published = utc(item["published_at"])
                if known > end or published > known:
                    continue
                symbols = json.loads(item.pop("affected_symbols_json"))
            except (ValueError, TypeError):
                continue
            split = urlsplit(str(item.pop("url") or ""))
            item.update({"revision_id": f"archived:{item['event_id']}:{known.isoformat()}",
                         "known_at": known.isoformat(), "available_at": known.isoformat(),
                         "affected_symbols": symbols, "symbols": symbols,
                         "url": urlunsplit((split.scheme, split.netloc, split.path, "", "")),
                         "archive_quality": "LATEST_ARCHIVED_REVISION_ONLY"})
            news.append(item)
        return news
    finally:
        connection.close()


def freeze_public_history(output: Path, start: datetime, end: datetime, *,
                          symbols=BENCHMARK_SYMBOLS, database: Path | None = None,
                          fetch=public_get, progress=None) -> dict:
    start, end = utc(start), utc(end)
    now = datetime.now(timezone.utc)
    if (not start < end <= now or start.minute or start.second or start.microsecond
            or end.minute or end.second or end.microsecond):
        raise ValueError("REPLAY_WINDOW_INVALID_OR_NOT_CLOSED")
    if end - start > timedelta(days=31):
        raise ValueError("REPLAY_WINDOW_TOO_LARGE")
    output.mkdir(parents=True, exist_ok=True)
    cache = output / "public-cache"
    cache.mkdir(exist_ok=True)
    bars, contracts, funding, coverage = [], {}, [], {}
    for symbol in tuple(dict.fromkeys(symbols)):
        native = native_symbol(symbol)
        contract_path = cache / f"{symbol}-contract.json"
        raw_contract, rules_observed, rules_basis = _cached_public_json(
            contract_path, "/contracts/" + native, None, fetch, max_age_seconds=900)
        contract = contracts[symbol] = normalize_contract(symbol, raw_contract)
        contract.update(rules_observed_at=rules_observed.isoformat(), rules_observation_basis=rules_basis)
        for frame, minutes in FRAME_MINUTES.items():
            first = start if frame == "1m" else start - timedelta(minutes=minutes * 240)
            last = end
            cursor = int(first.timestamp())
            stop = int(last.timestamp())
            collected = {}
            while cursor < stop:
                chunk_end = min(stop, cursor + 1800 * minutes * 60)
                path = cache / f"{symbol}-{frame}-{cursor}-{chunk_end}.json"
                raw_rows, fetched_at, fetch_basis = _cached_public_json(path, "/candlesticks",
                    {"contract": native, "interval": frame, "from": cursor, "to": chunk_end - 1}, fetch)
                if not isinstance(raw_rows, list):
                    raise ValueError("REPLAY_CANDLES_RESPONSE_INVALID")
                for raw in raw_rows:
                    row = normalize_bar(symbol, frame, raw, fetched_at, contract)
                    row["fetch_time_basis"] = fetch_basis
                    if first <= utc(row["bar_start"]) and utc(row["bar_end"]) <= last:
                        collected[row["bar_start"]] = row
                cursor = chunk_end
            ordered = sorted(collected.values(), key=lambda row: row["bar_start"])
            expected = int((last - first).total_seconds() / (minutes * 60))
            continuous = all(utc(b["bar_start"]) - utc(a["bar_start"]) == timedelta(minutes=minutes)
                             for a, b in zip(ordered, ordered[1:]))
            complete = (len(ordered) == expected and continuous and bool(ordered)
                        and utc(ordered[0]["bar_start"]) == first and utc(ordered[-1]["bar_end"]) == last)
            coverage[f"{symbol}:{frame}"] = {"expected": expected, "count": len(ordered), "complete": complete}
            bars.extend(ordered)
            if progress:
                progress(f"{symbol} {frame}: {len(ordered)}/{expected} closed bars; complete={complete}")
        # Gate's `to` is exclusive. Include the payment exactly at the closing
        # valuation boundary without charging the initially empty account.
        path = cache / f"{symbol}-funding-{int(start.timestamp())}-{int(end.timestamp())+1}.json"
        raw_funding, funding_fetched, funding_basis = _cached_public_json(path, "/funding_rate",
            {"contract": native, "from": int(start.timestamp()), "to": int(end.timestamp())+1, "limit": 1000}, fetch)
        if not isinstance(raw_funding, list):
            raise ValueError("REPLAY_FUNDING_RESPONSE_INVALID")
        count = 0
        for raw in raw_funding:
            at = datetime.fromtimestamp(int(raw["t"]), timezone.utc)
            if start < at <= end:
                rate = float(raw["r"])
                if not math.isfinite(rate):
                    raise ValueError("REPLAY_FUNDING_RATE_INVALID")
                funding.append({"instrument_id": symbol, "symbol": symbol, "payment_time": at.isoformat(),
                                "timestamp": at.isoformat(), "rate": rate, "source": "gate_public_funding_history",
                                "fetched_at": funding_fetched.isoformat(), "fetch_time_basis": funding_basis,
                                "raw_hash": digest(raw)})
                count += 1
        coverage[f"{symbol}:funding"] = {"count": count, "complete": count >= int((end-start).total_seconds()) // contract["funding_interval"]}
    points, point = [], start
    while point < end:
        points.append(point.isoformat())
        point += timedelta(minutes=5)
    news = archived_news(database, start, end)
    payload = {
        "schema_version": SCHEMA_VERSION, "created_at": now.isoformat(), "symbols": list(contracts),
        "window_start": start.isoformat(), "window_end": end.isoformat(), "decision_points": points,
        "bars": bars, "contracts": contracts, "funding": sorted(funding, key=lambda row: row["payment_time"]),
        "news": news, "coverage": coverage, "complete_data": all(row["complete"] for row in coverage.values()),
        "assumptions": {"bar_availability": "HISTORICAL_BAR_CLOSE_ASSUMPTION_NOT_ARCHIVED_ARRIVAL",
            "contract_rules": "CURRENT_PUBLIC_RULES_PROXY", "order_book": "UNAVAILABLE_HISTORICALLY",
            "open_interest": "UNAVAILABLE_HISTORICALLY", "news": "ARCHIVED_PARTIAL" if news else "UNAVAILABLE_TECHNICAL_ONLY",
            "universe": "FIXED_SIX_LIQUID_USDT_CONTRACT_BENCHMARK_NOT_ENTIRE_EXCHANGE",
            "funding_mark_price": "LAST_CLOSED_1M_LAST_TRADE_PRICE_PROXY",
            "selection": "PREDEFINED_LAST_COMPLETE_UTC_DAY_NOT_SELECTED_FOR_RETURNS"},
    }
    payload["manifest_sha256"] = manifest_hash(payload)
    (output / "history.json").write_text(canonical(payload), encoding="utf-8")
    (output / "coverage.json").write_text(canonical({key: value for key, value in payload.items() if key not in {"bars", "news", "funding"}}), encoding="utf-8")
    return payload


class ReplayHistory:
    def __init__(self, payload_or_path: dict | str | Path):
        self.payload = deepcopy(payload_or_path) if isinstance(payload_or_path, dict) else json.loads(Path(payload_or_path).read_text(encoding="utf-8"))
        if self.payload.get("schema_version") != SCHEMA_VERSION or self.payload.get("manifest_sha256") != manifest_hash(self.payload):
            raise ValueError("REPLAY_MANIFEST_INTEGRITY_INVALID")
        self.symbols = tuple(self.payload["symbols"])
        self.window_start, self.window_end = utc(self.payload["window_start"]), utc(self.payload["window_end"])
        self.decision_points = [utc(at) for at in self.payload["decision_points"]]
        self.as_of = self.window_start
        self._bars = defaultdict(list)
        for row in self.payload["bars"]:
            if row.get("synthetic") is not False or row.get("is_closed") is not True:
                raise ValueError("REPLAY_SYNTHETIC_OR_UNCLOSED_BAR")
            self._bars[(row["symbol"], row["timeframe"])].append(row)
        for rows in self._bars.values():
            rows.sort(key=lambda row: (utc(row["bar_end"]), utc(row["available_at"])))

    def set_as_of(self, point: datetime):
        self.as_of = utc(point)

    def latest_bars(self, symbol: str, timeframe: str, limit=240, **filters) -> list[dict]:
        versions = {}
        for row in self._bars[(symbol, timeframe)]:
            if utc(row["bar_end"]) <= self.as_of and utc(row["available_at"]) <= self.as_of:
                versions[row["bar_start"]] = row
        return [deepcopy(versions[key]) for key in sorted(versions)][-int(limit):]

    list_market_bars = latest_bars

    def news_as_of(self, point: datetime, symbols, limit=8) -> list[dict]:
        point = utc(point)
        rows = []
        for item in self.payload["news"]:
            known, published = utc(item["known_at"]), utc(item["published_at"])
            affected = set(item.get("affected_symbols") or item.get("symbols") or [])
            if (known <= point and point - timedelta(hours=48) <= published <= point
                    and (not affected or "MARKET_WIDE" in affected or affected.intersection(symbols))):
                rows.append(deepcopy(item))
        return sorted(rows, key=lambda row: (row["known_at"], row["revision_id"]), reverse=True)[:limit]

    def market_snapshot(self, symbol: str, point: datetime) -> dict:
        previous = self.as_of
        self.set_as_of(point)
        bars = self.latest_bars(symbol, "1m", limit=1) or self.latest_bars(symbol, "5m", limit=1)
        self.as_of = previous
        if not bars:
            raise ValueError(f"REPLAY_QUOTE_UNAVAILABLE:{symbol}")
        contract = deepcopy(self.payload["contracts"][symbol])
        return {"instrument_id": symbol, "symbol": symbol, "price": bars[-1]["close"],
                "data_as_of": bars[-1]["bar_end"], "received_at": utc(point).isoformat(),
                "source": self.payload.get("assumptions", {}).get("data_venue", "gate") + "_historical_closed_bar", "fresh": True, "freshness_status": "FRESH",
                "liquidity_ok": None, "cost_evidence_status": "SIMULATED_ASSUMPTION_NO_HISTORICAL_BOOK",
                "fee_rate": contract["taker_fee_rate"], "slippage": 0.0002,
                "contract_rules": contract, "market": contract["market"],
                "research_only": True, "execution_environment": "ISOLATED_HISTORICAL_SIMULATION"}

    def bars_between(self, start: datetime, end: datetime, timeframe="1m") -> list[dict]:
        start, end = utc(start), utc(end)
        rows = [deepcopy(row) for symbol in self.symbols for row in self._bars[(symbol, timeframe)]
                if start < utc(row["bar_end"]) <= end]
        return sorted(rows, key=lambda row: (utc(row["bar_end"]), row["symbol"]))

    def select_symbols(self, point: datetime, signal_timeframe="15m", limit=3, managed_symbols=None) -> list[str]:
        self.set_as_of(point)
        ranked = []
        for symbol in self.symbols:
            rows = self.latest_bars(symbol, signal_timeframe, limit=32)
            if len(rows) < 32:
                continue
            # Dollar turnover gives a predefined liquidity benchmark, computed
            # only from the previous 32 closed bars, never from future returns.
            turnover = sum(row["base_volume"] * row["close"] for row in rows)
            ranked.append((-turnover, symbol))
        managed = list(dict.fromkeys(s for s in (managed_symbols or []) if s in self.symbols))
        selected = managed + [symbol for _, symbol in sorted(ranked) if symbol not in managed]
        return selected[:max(int(limit), len(managed))]
