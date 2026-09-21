"""Independent Bonsai 2 27B public-market scanner.

This service is deliberately analysis-only: it reads public Gate market data
and public RSS news, requests a strict JSON analysis from Bonsai, and persists
the model receipt.  It never imports a strategy scanner, TradingAuthorization,
session manager, execution gateway, or order model.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
import time
import uuid
from typing import Any, Iterable

from core.instruments import instrument_for
from core.model_routing import DEFAULT_SMART_MODEL, is_bonsai_model_identity, is_verified_bonsai_receipt
from core.news_engine import NewsEngine, RSSNewsProvider
from core.providers.gateio_provider import GatePublicProvider


QWEN_MARKET_SCAN_INTERVAL_SECONDS = 300
QWEN_MARKET_SCAN_VERSION = "qwen_market_scan_v1"
DEFAULT_SCAN_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

QWEN_MARKET_SCAN_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["market_summary", "overall_bias", "confidence", "risk_flags", "symbol_analysis"],
    "properties": {
        "market_summary": {"type": "string", "maxLength": 2000},
        "overall_bias": {"type": "string", "enum": ["BULLISH", "BEARISH", "NEUTRAL", "MIXED", "UNKNOWN"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 100},
        "risk_flags": {"type": "array", "maxItems": 20, "items": {"type": "string", "maxLength": 240}},
        "symbol_analysis": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["symbol", "bias", "summary", "key_levels"],
                "properties": {
                    "symbol": {"type": "string", "maxLength": 40},
                    "bias": {"type": "string", "enum": ["BULLISH", "BEARISH", "NEUTRAL", "MIXED", "UNKNOWN"]},
                    "summary": {"type": "string", "maxLength": 1000},
                    "key_levels": {"type": "array", "maxItems": 12, "items": {"type": "number"}},
                },
            },
        },
    },
}


def _iso(value: datetime | None = None) -> str:
    point = value or datetime.now(timezone.utc)
    if point.tzinfo is None:
        point = point.replace(tzinfo=timezone.utc)
    return point.astimezone(timezone.utc).isoformat()


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _symbols(values: Iterable[str] | None) -> tuple[str, ...]:
    ordered: list[str] = []
    for raw in values or DEFAULT_SCAN_SYMBOLS:
        symbol = str(raw or "").strip().upper()
        if symbol and symbol not in ordered:
            ordered.append(symbol)
    return tuple(ordered[:5]) or DEFAULT_SCAN_SYMBOLS


class QwenMarketScanner:
    """Five-minute Bonsai scan loop with durable, non-executable receipts."""

    def __init__(
        self,
        store: Any,
        model_provider: Any,
        *,
        market_provider: Any | None = None,
        news_provider: Any | None = None,
        interval_seconds: float = QWEN_MARKET_SCAN_INTERVAL_SECONDS,
    ) -> None:
        self.store = store
        self.model_provider = model_provider
        self.market_provider = market_provider or GatePublicProvider()
        self.news_provider = news_provider or RSSNewsProvider()
        self.interval_seconds = max(60.0, float(interval_seconds))
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last: dict[str, Any] | None = None
        self._next_run_at: str | None = None
        self._ensure_table()
        self._load_last()

    def _ensure_table(self) -> None:
        if not hasattr(self.store, "_connect"):
            return
        with self.store._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS qwen_market_scans (
                    scan_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    model_digest TEXT,
                    input_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )

    def _load_last(self) -> None:
        if not hasattr(self.store, "_connect"):
            return
        try:
            with self.store._connect() as db:
                row = db.execute(
                    "SELECT scan_id, status, model_id, model_digest, input_hash, payload_json, created_at FROM qwen_market_scans ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                if row:
                    try:
                        payload = json.loads(row["payload_json"])
                    except Exception:
                        payload = {}
                    self._last = {
                        "scan_id": row["scan_id"],
                        "status": row["status"],
                        "model_id": row["model_id"],
                        "model_digest": row["model_digest"],
                        "input_hash": row["input_hash"],
                        "payload": payload,
                        "created_at": row["created_at"],
                    }
        except Exception:
            pass

    def _health(self) -> dict[str, Any]:
        provider = self.model_provider
        if provider is None or not callable(getattr(provider, "health", None)):
            return {"available": False, "model_available": False, "reason_code": "SMART_MODEL_UNAVAILABLE"}
        try:
            try:
                result = provider.health(model_name=DEFAULT_SMART_MODEL)
            except TypeError:
                result = provider.health()
        except Exception as exc:
            return {"available": False, "model_available": False, "reason_code": "SMART_MODEL_UNAVAILABLE", "error": type(exc).__name__}
        data = dict(result) if isinstance(result, dict) else {}
        actual_model = data.get("actual_model_id")
        identity_valid = is_bonsai_model_identity(actual_model)
        model_available = (
            data.get("model_id") == DEFAULT_SMART_MODEL
            and data.get("model_available") is True
            and identity_valid
            and data.get("model_identity_source") == "verified_manifest"
        )
        return {**data, "available": bool(data.get("available")) and model_available, "model_available": model_available}

    def _market(self, symbol: str) -> dict[str, Any]:
        instrument = instrument_for(symbol)
        quote = self.market_provider.get_quote(instrument)
        bars = self.market_provider.get_bars(instrument, "5m", limit=24)
        latest = bars[-1] if bars else None
        return {
            "symbol": symbol,
            "price": float(quote.price),
            "observed_at": _iso(quote.timestamp),
            "provider": str(getattr(self.market_provider, "provider_name", "gate_public")),
            "environment": str(getattr(self.market_provider, "environment", "LIVE_PUBLIC")),
            "five_minute_bars": [
                {
                    "start": _iso(bar.timestamp),
                    "end": _iso(getattr(bar, "bar_end", None) or bar.timestamp),
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": float(bar.volume),
                    "closed": bool(getattr(bar, "is_closed", False)),
                }
                for bar in bars[-12:]
            ],
            "latest_bar_closed": bool(getattr(latest, "is_closed", False)) if latest is not None else False,
        }

    def _news(self, symbol: str) -> dict[str, Any]:
        instrument = instrument_for(symbol)
        result = NewsEngine(self.news_provider).collect(instrument, limit=8)
        return {
            "available": bool(result.available),
            "provider": result.provider,
            "error_code": result.error_code,
            "events": [event.to_dict() for event in result.events],
        }

    def _persist(self, result: dict[str, Any]) -> None:
        if not hasattr(self.store, "_connect"):
            return
        with self.store._connect() as db:
            db.execute(
                """INSERT INTO qwen_market_scans
                   (scan_id, status, model_id, model_digest, input_hash, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    result["scan_id"], result["status"], result["model_id"], result.get("model_digest"),
                    result["input_hash"], json.dumps(result, ensure_ascii=False, allow_nan=False), result["created_at"],
                ),
            )

    def run_once(self, symbols: Iterable[str] | None = None) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        selected = _symbols(symbols)
        scan_id = f"qwen_scan_{uuid.uuid4().hex[:16]}"
        payload: dict[str, Any] = {
            "schema_version": QWEN_MARKET_SCAN_VERSION,
            "decision_origin": "MODEL_ANALYSIS",
            "execution": "NOT_REQUESTED",
            "order_created": False,
            "symbols": list(selected),
        }
        health = self._health()
        payload["model_health"] = health
        try:
            payload["market"] = [self._market(symbol) for symbol in selected]
            payload["news"] = {symbol: self._news(symbol) for symbol in selected}
        except Exception as exc:
            payload["data_error"] = type(exc).__name__
            health_available = bool(health.get("available"))
            status = "MARKET_DATA_UNAVAILABLE" if health_available else "MODEL_UNAVAILABLE"
            result = {
                "scan_id": scan_id, "status": status, "model_id": DEFAULT_SMART_MODEL,
                "model_digest": health.get("weight_digest"), "input_hash": _digest(payload),
                "payload": payload, "created_at": _iso(started), "completed_at": _iso(),
            }
            self._persist(result)
            with self._lock:
                self._last = result
            return result

        input_hash = _digest(payload)
        if not health.get("available") or not callable(getattr(self.model_provider, "generate_json", None)):
            result = {
                "scan_id": scan_id, "status": "MODEL_UNAVAILABLE", "model_id": DEFAULT_SMART_MODEL,
                "model_digest": health.get("weight_digest"), "input_hash": input_hash,
                "payload": payload, "created_at": _iso(started), "completed_at": _iso(),
            }
            self._persist(result)
            with self._lock:
                self._last = result
            return result

        messages = [
            {"role": "system", "content": "你是市场分析模型。只返回符合 JSON Schema 的 JSON。输出分析，不得下单、不得建议绕过风控、不得虚构新闻。"},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
        ]
        try:
            answer, _raw, metadata = self.model_provider.generate_json(
                messages,
                model_name=DEFAULT_SMART_MODEL,
                prompt_version=QWEN_MARKET_SCAN_VERSION,
                input_hash=input_hash,
                temperature=0.0,
                schema=QWEN_MARKET_SCAN_SCHEMA,
            )
            if not is_verified_bonsai_receipt(metadata):
                raise ValueError("MODEL_RECEIPT_INVALID")
            if not isinstance(answer, dict):
                raise ValueError("INVALID_MODEL_JSON")
            payload["analysis"] = answer
            payload["model_receipt"] = {
                key: metadata.get(key)
                for key in (
                    "model_id", "model_version", "actual_model_id", "model_identity_source",
                    "verified_manifest_model_id", "latency_ms", "schema_enforcement", "parse_status",
                )
            }
            result = {
                "scan_id": scan_id, "status": "COMPLETED", "model_id": DEFAULT_SMART_MODEL,
                "model_digest": health.get("weight_digest"), "input_hash": input_hash,
                "payload": payload, "created_at": _iso(started), "completed_at": _iso(),
            }
        except Exception as exc:
            payload["model_error"] = type(exc).__name__
            result = {
                "scan_id": scan_id, "status": "MODEL_CALL_FAILED", "model_id": DEFAULT_SMART_MODEL,
                "model_digest": health.get("weight_digest"), "input_hash": input_hash,
                "payload": payload, "created_at": _iso(started), "completed_at": _iso(),
            }
        self._persist(result)
        with self._lock:
            self._last = result
        return result

    def _loop(self, symbols: tuple[str, ...]) -> None:
        while not self._stop_event.is_set():
            self.run_once(symbols)
            with self._lock:
                self._next_run_at = _iso(datetime.now(timezone.utc) + timedelta(seconds=self.interval_seconds))
            if self._stop_event.wait(self.interval_seconds):
                break
        with self._lock:
            self._next_run_at = None

    def start(self, symbols: Iterable[str] | None = None) -> dict[str, Any]:
        selected = _symbols(symbols)
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status()
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, args=(selected,), name="aima-qwen-market-scan", daemon=True)
            self._thread.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        self._stop_event.set()
        with self._lock:
            worker = self._thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2.0)
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._last is None:
                self._load_last()
            running = self._thread is not None and self._thread.is_alive()
            return {"running": running, "interval_seconds": self.interval_seconds, "next_run_at": self._next_run_at, "latest": self._last}

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        if not hasattr(self.store, "_connect"):
            return []
        with self.store._connect() as db:
            rows = db.execute("SELECT payload_json FROM qwen_market_scans ORDER BY created_at DESC LIMIT ?", (max(1, min(int(limit), 100)),)).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                records.append(payload)
        return records
