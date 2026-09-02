"""Sidecar-owned public market/news cache hydration.

This worker is deliberately separate from both ``MonitoringRuntime`` and the
analysis scheduler.  It may fetch public read-only evidence for the dashboard,
but it never enables a policy, calls Ollama, creates a Prediction, or creates
an alert.  The dashboard GET remains cache-only; this module is the only
background writer for the first-start public data cache.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

from .events import EventIntelligenceService
from .instruments import AssetType, Instrument, TradingHours, instrument_for
from .news_engine import RSSNewsProvider
from .providers import ProviderChain, ProviderError, build_default_provider, fetch_market_data
from .providers.yfinance import YFinanceProvider
from .storage import SQLiteStore


HYDRATION_CONTRACT_VERSION = "public_hydration_v1"
DEFAULT_PUBLIC_MARKET_SET = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "NVDA", "NASDAQ", "VIX", "US10Y")
DEFAULT_NEWS_SET = ("BTCUSDT", "NVDA")


def _utc(value: datetime | None = None) -> datetime:
    point = value or datetime.now(timezone.utc)
    if point.tzinfo is None:
        point = point.replace(tzinfo=timezone.utc)
    return point.astimezone(timezone.utc)


def _macro_instrument(symbol: str) -> Instrument:
    return Instrument(
        symbol=symbol,
        asset_type=AssetType.EQUITY,
        exchange="PUBLIC",
        currency="USD",
        timezone="America/New_York",
        trading_hours=TradingHours.REGULAR,
        sector="Macro",
    )


def instrument_for_public(symbol: str) -> Instrument:
    normalized = symbol.strip().upper()
    if normalized in {"NASDAQ", "VIX", "US10Y"}:
        return _macro_instrument(normalized)
    return instrument_for(normalized)


class MarketHydrationRuntime:
    """A single bounded public-data refresh worker per sidecar process."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        enabled: bool = True,
        clock: Callable[[], datetime] | None = None,
        interval_seconds: float = 300.0,
        initial_delay_seconds: float = 0.2,
        market_symbols: tuple[str, ...] = DEFAULT_PUBLIC_MARKET_SET,
        news_symbols: tuple[str, ...] = DEFAULT_NEWS_SET,
    ) -> None:
        self.store = store
        self.enabled = bool(enabled)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.interval_seconds = max(30.0, min(float(interval_seconds), 3600.0))
        self.initial_delay_seconds = max(0.0, min(float(initial_delay_seconds), 30.0))
        self.market_symbols = tuple(dict.fromkeys(symbol.strip().upper() for symbol in market_symbols if symbol.strip()))[:20]
        self.news_symbols = tuple(dict.fromkeys(symbol.strip().upper() for symbol in news_symbols if symbol.strip()))[:4]
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        now = _utc(self.clock()).isoformat()
        self._status: dict[str, object] = {
            "contract_version": HYDRATION_CONTRACT_VERSION,
            "enabled": self.enabled,
            "state": "disabled" if not self.enabled else "idle",
            "worker_alive": False,
            "market_symbols": list(self.market_symbols),
            "news_symbols": list(self.news_symbols),
            "sources": {"market": "pending", "news": "pending"},
            "last_refresh_at": None,
            "last_success_at": None,
            "last_run_id": None,
            "run_count": 0,
            "next_refresh_at": None,
            "last_error": None,
            "last_errors": [],
            "cache": {"symbols": [], "fresh_symbols": 0, "stale_symbols": 0},
            "provider_calls": 0,
            "domain_writes": 0,
            "started_at": now,
        }

    @property
    def worker_alive(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def status(self) -> dict[str, object]:
        with self._lock:
            result = dict(self._status)
            result["worker_alive"] = self.worker_alive
            result["sources"] = dict(self._status.get("sources") or {})
            result["cache"] = dict(self._status.get("cache") or {})
            result["last_errors"] = list(self._status.get("last_errors") or [])
            return result

    def start(self) -> dict[str, object]:
        with self._lock:
            if not self.enabled:
                self._status["state"] = "disabled"
                return self.status()
            if self._worker and self._worker.is_alive():
                return self.status()
            self._seed_default_watchlist()
            self._stop_event.clear()
            self._wake_event.clear()
            self._status["state"] = "starting"
            self._worker = threading.Thread(target=self._worker_loop, name="aima-public-hydration", daemon=True)
            self._worker.start()
            return self.status()

    def _seed_default_watchlist(self) -> None:
        """Create deletable public membership once; never create policies."""

        try:
            seeded = self.store.get_app_setting("market_hydration.defaults_seeded").get("value") is True
            if seeded:
                return
            if self.store.list_watchlist_entries():
                self.store.upsert_app_setting("market_hydration.defaults_seeded", True)
                return
            for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "NVDA"):
                instrument = instrument_for_public(symbol)
                self.store.save_instrument(instrument)
                self.store.upsert_watchlist_entry(symbol)
            self.store.upsert_app_setting("market_hydration.defaults_seeded", True)
        except Exception:
            # Hydration remains useful even if a legacy database is temporarily
            # read-only. The cache/status error will explain the limitation.
            return

    def stop(self) -> dict[str, object]:
        self._stop_event.set()
        self._wake_event.set()
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=5.0)
        with self._lock:
            self._status["state"] = "stopped" if self.enabled else "disabled"
            self._status["worker_alive"] = False
        return self.status()

    def refresh_now(self) -> dict[str, object]:
        """Run one bounded public refresh; used by the explicit retry action."""

        if not self.enabled:
            with self._lock:
                self._status["state"] = "disabled"
            return self.status()
        self._refresh_once()
        return self.status()

    def _set(self, **values: object) -> None:
        with self._lock:
            self._status.update(values)
            self._status["worker_alive"] = self.worker_alive

    def _save_market(self, symbol: str, instrument: Instrument, *, now: datetime) -> tuple[bool, str | None]:
        try:
            provider = YFinanceProvider(public_chart_only=True) if instrument.asset_type is AssetType.EQUITY else build_default_provider(instrument)
            timeframe = "15m" if instrument.asset_type is AssetType.CRYPTO else "1d"
            if isinstance(provider, ProviderChain):
                bundle = provider.get_bundle(instrument, timeframe, 240)
            else:
                # Dashboard hydration intentionally uses a provider boundary,
                # never AnalysisService. Macro/equity symbols use the public
                # Yahoo chart endpoint only and do not require yfinance.
                bundle = fetch_market_data(provider, instrument, timeframe, 240)
            bars = list(bundle.bars)
            if not bars:
                raise ProviderError("provider returned no bars", code="empty_data", provider=bundle.snapshot.provider)
            self.store.save_instrument(instrument)
            self.store.upsert_market_bars(
                symbol,
                timeframe,
                bars,
                provider=bundle.snapshot.provider,
                data_as_of=bundle.snapshot.data_as_of,
                now=now,
            )
            change_pct = bundle.quote.change_pct
            if change_pct is None and len(bars) > 1 and bars[-2].close:
                change_pct = ((bundle.quote.price / bars[-2].close) - 1.0) * 100.0
            age = max(0.0, (now - bundle.quote.timestamp.astimezone(timezone.utc)).total_seconds())
            self.store.save_realtime_state(
                {
                    "contract_version": "public_market_cache_v1",
                    "symbol": symbol,
                    "provider": bundle.snapshot.provider,
                    "price": bundle.quote.price,
                    "change_pct": change_pct,
                    "volume": bundle.quote.volume,
                    "last_trade_at": bundle.quote.timestamp.astimezone(timezone.utc).isoformat(),
                    "data_as_of": bundle.snapshot.data_as_of.astimezone(timezone.utc).isoformat(),
                    "freshness_status": "stale" if bundle.snapshot.stale or age > 3600 else "fresh",
                    "stale_after_seconds": 3600,
                    "reconnect_count": 0,
                    "last_error": bundle.snapshot.error_code,
                    "age_seconds": round(age, 3),
                    "cache_scope": "dashboard_public_hydration",
                },
                now=now,
            )
            self.store.save_provider_snapshot(symbol, bundle.snapshot)
            self.store.save_snapshot(symbol, timeframe, now.isoformat(), {
                "contract_version": HYDRATION_CONTRACT_VERSION,
                "quote": {"price": bundle.quote.price, "change_pct": change_pct},
                "provider": bundle.snapshot.to_dict(),
                "bars": len(bars),
            })
            return True, bundle.snapshot.provider
        except Exception as exc:
            provider = str(getattr(exc, "provider", None) or "unknown")
            code = str(getattr(exc, "code", "provider_error"))
            try:
                self.store.save_realtime_state(
                    {
                        "symbol": symbol,
                        "provider": provider,
                        "freshness_status": "degraded",
                        "stale_after_seconds": 3600,
                        "last_error": code,
                        "cache_scope": "dashboard_public_hydration",
                    },
                    now=now,
                )
            except Exception:
                pass
            return False, f"{provider}:{code}"

    def _save_news(self, symbol: str, *, now: datetime) -> tuple[bool, str | None]:
        try:
            instrument = instrument_for_public(symbol)
            timeout = max(1.0, min(float(os.environ.get("NEWS_PROVIDER_TIMEOUT_SEC", "5")), 15.0))
            result = EventIntelligenceService(RSSNewsProvider(timeout=timeout, retries=0)).collect(instrument, as_of=now, limit=12)
            self.store.save_event_context(result.to_dict())
            return result.available, result.provider if result.available else result.error_code
        except Exception as exc:
            return False, str(getattr(exc, "code", None) or "news_provider_error")

    def _refresh_once(self) -> None:
        now = _utc(self.clock())
        run_id = f"hydration-{uuid4().hex}"
        self._set(state="refreshing", last_refresh_at=now.isoformat(), last_error=None, last_errors=[], last_run_id=run_id)
        market_errors: list[str] = []
        sources: dict[str, object] = {}
        success_count = 0
        for symbol in self.market_symbols:
            if self._stop_event.is_set():
                break
            try:
                instrument = instrument_for_public(symbol)
            except ValueError as exc:
                market_errors.append(f"{symbol}:invalid_instrument")
                continue
            ok, detail = self._save_market(symbol, instrument, now=now)
            if ok:
                success_count += 1
                sources[symbol] = detail or "public"
            elif detail:
                market_errors.append(f"{symbol}:{detail}")
        news_errors: list[str] = []
        news_success = 0
        for symbol in self.news_symbols:
            if self._stop_event.is_set():
                break
            ok, detail = self._save_news(symbol, now=now)
            if ok:
                news_success += 1
                sources[f"news:{symbol}"] = detail or "public_rss"
            elif detail:
                news_errors.append(f"{symbol}:{detail}")
        errors = market_errors + news_errors
        cached = self.store.list_realtime_states()
        fresh = sum(1 for item in cached if str(item.get("freshness_status")) == "fresh")
        with self._lock:
            self._status.update(
                {
                    "state": "degraded" if errors and success_count == 0 else "ready",
                    "last_success_at": now.isoformat() if success_count or news_success else self._status.get("last_success_at"),
                    "next_refresh_at": _utc(now + timedelta(seconds=self.interval_seconds)).isoformat(),
                    "last_error": errors[0] if errors else None,
                    "last_errors": errors[:12],
                    "sources": {"market": sources, "news": {key: value for key, value in sources.items() if str(key).startswith("news:")}},
                    "provider_calls": int(self._status.get("provider_calls") or 0) + len(self.market_symbols) + len(self.news_symbols),
                    "domain_writes": int(self._status.get("domain_writes") or 0),
                    "cache": {"symbols": [str(item.get("symbol")) for item in cached], "fresh_symbols": fresh, "stale_symbols": max(0, len(cached) - fresh)},
                    "run_count": int(self._status.get("run_count") or 0) + 1,
                }
            )
            final_status = str(self._status.get("state"))
            providers = dict(self._status.get("sources") or {})
            cache = dict(self._status.get("cache") or {})
        try:
            self.store.save_public_hydration_run(
                run_id=run_id,
                started_at=now.isoformat(),
                finished_at=_utc(self.clock()).isoformat(),
                status=final_status,
                providers=providers,
                errors=errors,
                cache=cache,
            )
        except Exception:
            pass

    def _worker_loop(self) -> None:
        if self.initial_delay_seconds > 0 and self._stop_event.wait(self.initial_delay_seconds):
            return
        failure_count = 0
        try:
            while not self._stop_event.is_set():
                try:
                    self._refresh_once()
                    failure_count = 0
                    with self._lock:
                        self._status["next_refresh_at"] = _utc(self.clock() + timedelta(seconds=self.interval_seconds)).isoformat()
                    self._wake_event.wait(self.interval_seconds)
                except Exception as exc:
                    failure_count += 1
                    delay = min(120.0, max(1.0, 2.0 ** min(failure_count - 1, 6)))
                    self._set(
                        state="degraded",
                        last_error="public_hydration_worker_error",
                        last_errors=["public_hydration_worker_error"],
                        next_refresh_at=_utc(self.clock() + timedelta(seconds=delay)).isoformat(),
                    )
                    if self._stop_event.wait(delay):
                        break
                finally:
                    self._wake_event.clear()
        finally:
            with self._lock:
                self._status["worker_alive"] = False
                if self._status.get("state") not in {"degraded", "disabled"}:
                    self._status["state"] = "stopped"


__all__ = [
    "DEFAULT_NEWS_SET",
    "DEFAULT_PUBLIC_MARKET_SET",
    "HYDRATION_CONTRACT_VERSION",
    "MarketHydrationRuntime",
    "instrument_for_public",
]
