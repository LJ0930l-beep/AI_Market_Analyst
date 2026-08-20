"""Deterministic, provider-bound benchmark context.

Benchmark identity is an explicit local mapping.  This module never guesses a
provider symbol from arbitrary user input and never asks the model to calculate
relative performance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .instruments import AssetType, Instrument, TradingHours
from .providers.base import Bar, ProviderError
from .providers.runtime import MarketDataBundle, build_default_provider, fetch_market_data
from .quant.engine import QuantSnapshot


BENCHMARK_MAPPING_VERSION = "benchmark_mapping_v1"
BENCHMARK_CONTEXT_VERSION = "benchmark_context_v1"


@dataclass(frozen=True, slots=True)
class BenchmarkMetadata:
    symbol: str
    benchmark_symbol: str
    benchmark_asset_type: str
    provider: str
    provider_symbol: str
    mapping_version: str
    relation: str
    status: str
    mapping_reason: str
    metadata_labels: dict[str, str]
    updated_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "benchmark_symbol": self.benchmark_symbol,
            "benchmark_asset_type": self.benchmark_asset_type,
            "provider": self.provider,
            "provider_symbol": self.provider_symbol,
            "mapping_version": self.mapping_version,
            "relation": self.relation,
            "status": self.status,
            "mapping_reason": self.mapping_reason,
            "metadata_labels": dict(self.metadata_labels),
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkContext:
    version: str
    symbol: str
    timeframe: str
    as_of: str
    status: str
    benchmark: BenchmarkMetadata
    provider: str
    freshness: dict[str, object]
    target_return: float | None
    benchmark_return: float | None
    relative_performance: float | None
    relative_strength: float | None
    capability: dict[str, object]
    provenance: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "as_of": self.as_of,
            "status": self.status,
            "benchmark": self.benchmark.to_dict(),
            "provider": self.provider,
            "freshness": dict(self.freshness),
            "target_return": self.target_return,
            "benchmark_return": self.benchmark_return,
            "relative_performance": self.relative_performance,
            "relative_strength": self.relative_strength,
            "capability": dict(self.capability),
            "provenance": dict(self.provenance),
        }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _benchmark_instrument(symbol: str, asset_type: AssetType) -> Instrument:
    if asset_type is AssetType.CRYPTO:
        return Instrument(
            symbol=symbol,
            asset_type=AssetType.CRYPTO,
            exchange="BINANCE",
            currency="USDT",
            timezone="UTC",
            trading_hours=TradingHours.AROUND_THE_CLOCK,
            sector="Crypto benchmark",
        )
    return Instrument(
        symbol=symbol,
        asset_type=AssetType.EQUITY,
        exchange="PUBLIC",
        currency="USD",
        timezone="America/New_York",
        trading_hours=TradingHours.REGULAR,
        sector="Benchmark",
    )


def benchmark_metadata_for(instrument: Instrument, *, provider: str = "public_market") -> BenchmarkMetadata:
    """Return only mappings from the explicit Phase 6 benchmark registry."""

    if instrument.asset_type is AssetType.CRYPTO:
        benchmark_symbol = "BTCUSDT"
        relation = "crypto_market_baseline"
        reason = "explicit_public_crypto_baseline"
        labels = {"identity": "explicit", "market_scope": "crypto", "sector": "unknown"}
    elif instrument.sector == "Semiconductors":
        benchmark_symbol = "SOXX"
        relation = "sector_benchmark"
        reason = "explicit_semiconductor_sector_mapping"
        labels = {"identity": "explicit", "market_scope": "us_equity", "sector": "known"}
    elif instrument.sector == "Technology":
        benchmark_symbol = "QQQ"
        relation = "sector_benchmark"
        reason = "explicit_technology_sector_mapping"
        labels = {"identity": "explicit", "market_scope": "us_equity", "sector": "known"}
    else:
        benchmark_symbol = "SPY"
        relation = "broad_market_benchmark"
        reason = "explicit_broad_us_equity_mapping"
        labels = {"identity": "explicit", "market_scope": "us_equity", "sector": "unknown_or_broad"}
    return BenchmarkMetadata(
        symbol=instrument.symbol,
        benchmark_symbol=benchmark_symbol,
        benchmark_asset_type=instrument.asset_type.value,
        provider=provider,
        provider_symbol=benchmark_symbol,
        mapping_version=BENCHMARK_MAPPING_VERSION,
        relation=relation,
        status="mapped",
        mapping_reason=reason,
        metadata_labels=labels,
    )


def _provider_bundle(provider: object, instrument: Instrument, timeframe: str, limit: int) -> MarketDataBundle:
    try:
        if hasattr(provider, "get_bundle"):
            return provider.get_bundle(instrument, timeframe, limit)  # type: ignore[attr-defined]
        return fetch_market_data(provider, instrument, timeframe, limit)  # type: ignore[arg-type]
    except ProviderError:
        raise
    except Exception as exc:
        provider_name = str(getattr(provider, "provider_name", provider.__class__.__name__.lower()))
        raise ProviderError(str(exc), code="benchmark_provider_error", provider=provider_name) from exc


def _last_bars(bars: list[Bar] | tuple[Bar, ...], as_of: datetime) -> list[Bar]:
    cutoff = _utc(as_of)
    return sorted((bar for bar in bars if _utc(bar.timestamp) <= cutoff), key=lambda bar: _utc(bar.timestamp))


def _return(bars: list[Bar], lookback: int = 20) -> float | None:
    if len(bars) < 2:
        return None
    selected = bars[-min(lookback, len(bars)) :]
    start = selected[0].close
    end = selected[-1].close
    if start <= 0 or not math.isfinite(start) or not math.isfinite(end):
        return None
    return (end / start) - 1.0


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class BenchmarkContextService:
    """Fetch and calculate benchmark context without persistence by default."""

    def __init__(
        self,
        *,
        provider_factory: Callable[[Instrument], object] | None = None,
        store: object | None = None,
    ) -> None:
        self.provider_factory = provider_factory or build_default_provider
        self.store = store

    def metadata_for(self, instrument: Instrument, *, persist: bool = False) -> BenchmarkMetadata:
        existing = None
        if self.store is not None and hasattr(self.store, "get_benchmark_metadata"):
            existing = self.store.get_benchmark_metadata(instrument.symbol)  # type: ignore[attr-defined]
        if isinstance(existing, dict):
            labels = existing.get("metadata_labels")
            return BenchmarkMetadata(
                symbol=str(existing["symbol"]),
                benchmark_symbol=str(existing["benchmark_symbol"]),
                benchmark_asset_type=str(existing["benchmark_asset_type"]),
                provider=str(existing["provider"]),
                provider_symbol=str(existing["provider_symbol"]),
                mapping_version=str(existing["mapping_version"]),
                relation=str(existing["relation"]),
                status=str(existing["status"]),
                mapping_reason=str(existing.get("mapping_reason", "stored_mapping")),
                metadata_labels={str(key): str(value) for key, value in (labels if isinstance(labels, dict) else {}).items()},
                updated_at=str(existing.get("updated_at")) if existing.get("updated_at") else None,
            )
        metadata = benchmark_metadata_for(instrument)
        if persist and self.store is not None and hasattr(self.store, "save_benchmark_metadata"):
            self.store.save_benchmark_metadata(metadata.to_dict())  # type: ignore[attr-defined]
        return metadata

    def build(
        self,
        instrument: Instrument,
        *,
        target_bars: list[Bar] | tuple[Bar, ...],
        target_quant: QuantSnapshot | None,
        timeframe: str,
        as_of: datetime,
        target_provider: str = "unknown",
        persist_mapping: bool = False,
    ) -> BenchmarkContext:
        cutoff = _utc(as_of)
        metadata = self.metadata_for(instrument, persist=persist_mapping)
        target = _last_bars(target_bars, cutoff)
        if not target:
            return self._unavailable(metadata, instrument, timeframe, cutoff, target_provider, "target_data_unavailable")
        benchmark = _benchmark_instrument(metadata.benchmark_symbol, instrument.asset_type)
        provider_name = metadata.provider
        try:
            provider = self.provider_factory(benchmark)
            provider_name = str(getattr(provider, "provider_name", metadata.provider))
            bundle = _provider_bundle(provider, benchmark, timeframe, max(60, min(len(target_bars), 200)))
            benchmark_bars = _last_bars(bundle.bars, cutoff)
        except ProviderError as exc:
            return self._unavailable(metadata, instrument, timeframe, cutoff, provider_name, exc.code, str(exc))
        except Exception as exc:
            return self._unavailable(metadata, instrument, timeframe, cutoff, provider_name, "benchmark_provider_error", str(exc))
        if len(benchmark_bars) < 2 or len(target) < 2:
            return self._unavailable(metadata, instrument, timeframe, cutoff, provider_name, "insufficient_benchmark_history")
        target_return = _return(target)
        benchmark_return = _return(benchmark_bars)
        if target_return is None or benchmark_return is None:
            return self._unavailable(metadata, instrument, timeframe, cutoff, provider_name, "non_finite_benchmark_return")
        relative = target_return - benchmark_return
        self_baseline = instrument.asset_type is AssetType.CRYPTO and instrument.symbol == metadata.benchmark_symbol
        strength = None if self_baseline else _clamp(relative * 10.0)
        last_as_of = max(_utc(target[-1].timestamp), _utc(benchmark_bars[-1].timestamp))
        age_seconds = max(0.0, (cutoff - last_as_of).total_seconds())
        freshness = {
            "status": "fresh" if age_seconds <= 2 * self._timeframe_seconds(timeframe) else "stale",
            "age_seconds": round(age_seconds, 3),
            "target_data_as_of": _utc(target[-1].timestamp).isoformat(),
            "benchmark_data_as_of": _utc(benchmark_bars[-1].timestamp).isoformat(),
        }
        capability: dict[str, object] = {
            "relative_performance": "available",
            "relative_strength": "baseline_only" if self_baseline else "available",
            "future_bars_excluded": True,
        }
        if self_baseline:
            capability["reason"] = "crypto_benchmark_is_the_same_explicit_btc_baseline"
        if instrument.asset_type is AssetType.CRYPTO:
            capability["total_market_context"] = "unavailable"
            capability["dominance_context"] = "unavailable"
            capability["additional_crypto_context_reason"] = "public_baseline_route_does_not_claim_total_or_dominance_data"
        return BenchmarkContext(
            version=BENCHMARK_CONTEXT_VERSION,
            symbol=instrument.symbol,
            timeframe=timeframe,
            as_of=cutoff.isoformat(),
            status="available",
            benchmark=metadata,
            provider=provider_name,
            freshness=freshness,
            target_return=round(target_return, 8),
            benchmark_return=round(benchmark_return, 8),
            relative_performance=round(relative, 8),
            relative_strength=round(strength, 8) if strength is not None else None,
            capability=capability,
            provenance={
                "target_provider": target_provider,
                "benchmark_provider": provider_name,
                "mapping_version": metadata.mapping_version,
                "computed_by": "python_deterministic",
                "read_only": not persist_mapping,
            },
        )

    @staticmethod
    def _timeframe_seconds(timeframe: str) -> int:
        return {"5m": 300, "15m": 900, "1h": 3600, "4h": 14_400, "1d": 86_400}.get(timeframe.lower(), 3600)

    @staticmethod
    def _unavailable(
        metadata: BenchmarkMetadata,
        instrument: Instrument,
        timeframe: str,
        as_of: datetime,
        provider: str,
        reason: str,
        detail: str | None = None,
    ) -> BenchmarkContext:
        capability: dict[str, object] = {
            "relative_performance": "unavailable",
            "relative_strength": "unavailable",
            "reason": reason,
            "future_bars_excluded": True,
        }
        if detail:
            capability["provider_detail"] = detail
        return BenchmarkContext(
            version=BENCHMARK_CONTEXT_VERSION,
            symbol=instrument.symbol,
            timeframe=timeframe,
            as_of=_utc(as_of).isoformat(),
            status="unavailable",
            benchmark=metadata,
            provider=provider,
            freshness={"status": "unknown", "reason": reason},
            target_return=None,
            benchmark_return=None,
            relative_performance=None,
            relative_strength=None,
            capability=capability,
            provenance={"computed_by": "python_deterministic", "read_only": True},
        )


def benchmark_capabilities() -> dict[str, object]:
    return {
        "mapping_version": BENCHMARK_MAPPING_VERSION,
        "context_version": BENCHMARK_CONTEXT_VERSION,
        "equity": {"supported": ["SPY", "QQQ", "SOXX"], "provider_route": "public_equity_market"},
        "crypto": {
            "supported": ["BTCUSDT"],
            "provider_route": "public_crypto_market",
            "self_baseline": True,
            "total_market_context": "unavailable",
            "dominance_context": "unavailable",
        },
        "calculation": "python_deterministic_relative_return_and_strength",
        "llm_calculates": False,
        "read_only_get": True,
    }
