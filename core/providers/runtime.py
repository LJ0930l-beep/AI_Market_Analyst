"""Runtime provider routing and auditable data freshness metadata."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from ..instruments import AssetType, Instrument
from .base import Bar, MarketProvider, ProviderError, Quote
from .binance import BinancePublicProvider
from .coingecko import CoinGeckoPublicProvider
from .fixture import FixtureProvider
from .yfinance import YFinanceProvider


@dataclass(frozen=True, slots=True)
class ProviderSnapshot:
    provider: str
    fetched_at: datetime
    data_as_of: datetime
    stale: bool
    error_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "fetched_at": self.fetched_at.astimezone(timezone.utc).isoformat(),
            "data_as_of": self.data_as_of.astimezone(timezone.utc).isoformat(),
            "stale": self.stale,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class MarketDataBundle:
    quote: Quote
    bars: tuple[Bar, ...]
    snapshot: ProviderSnapshot

    @property
    def data_as_of(self) -> datetime:
        return self.snapshot.data_as_of


def fetch_market_data(provider: MarketProvider, instrument: Instrument, timeframe: str, limit: int = 200) -> MarketDataBundle:
    """Call a provider through the stable Phase 1 interface and attach metadata."""

    fetched_at = datetime.now(timezone.utc)
    provider_name = str(getattr(provider, "provider_name", provider.__class__.__name__.lower()))
    try:
        quote = provider.get_quote(instrument)
        bars = tuple(provider.get_bars(instrument, timeframe, limit))
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(str(exc), code="provider_error", provider=provider_name) from exc
    if not bars:
        raise ProviderError("provider returned no bars", code="empty_data", provider=provider_name)
    data_as_of = max(bars[-1].timestamp, quote.timestamp)
    return MarketDataBundle(
        quote=quote,
        bars=bars,
        snapshot=ProviderSnapshot(
            provider=provider_name,
            fetched_at=fetched_at,
            data_as_of=data_as_of,
            stale=bool(getattr(provider, "stale", False)),
        ),
    )


class ProviderChain:
    """Try providers in order and preserve an honest error/fallback trail."""

    def __init__(self, providers: list[MarketProvider]) -> None:
        if not providers:
            raise ValueError("ProviderChain requires at least one provider")
        self.providers = providers
        self.provider_name = "provider_chain"

    def get_bundle(self, instrument: Instrument, timeframe: str, limit: int = 200) -> MarketDataBundle:
        errors: list[ProviderError] = []
        for provider in self.providers:
            try:
                bundle = fetch_market_data(provider, instrument, timeframe, limit)
                if errors:
                    fallback_codes = ",".join(error.code for error in errors)
                    bundle = MarketDataBundle(
                        quote=bundle.quote,
                        bars=bundle.bars,
                        snapshot=ProviderSnapshot(
                            provider=bundle.snapshot.provider,
                            fetched_at=bundle.snapshot.fetched_at,
                            data_as_of=bundle.snapshot.data_as_of,
                            stale=bundle.snapshot.stale,
                            error_code=f"fallback_after:{fallback_codes}",
                        ),
                    )
                return bundle
            except ProviderError as exc:
                errors.append(exc)
        detail = "; ".join(f"{e.provider or 'unknown'}:{e.code}" for e in errors)
        last = errors[-1] if errors else None
        raise ProviderError(f"all providers failed ({detail})", code="all_providers_failed", provider="provider_chain") from last

    def get_quote(self, instrument: Instrument) -> Quote:
        return self.get_bundle(instrument, "1h", limit=2).quote

    def get_bars(self, instrument: Instrument, timeframe: str, limit: int = 200) -> list[Bar]:
        return list(self.get_bundle(instrument, timeframe, limit).bars)


def build_default_provider(instrument: Instrument) -> MarketProvider | ProviderChain:
    """Build the configured real/fixture route without introducing credentials."""

    mode = os.environ.get("MARKET_DATA_MODE", "real").strip().lower()
    if mode == "fixture":
        return FixtureProvider()
    if mode not in {"real", "auto"}:
        raise ValueError("MARKET_DATA_MODE must be real, auto, or fixture")
    # A fixture is a deterministic test dependency, never an implicit live
    # fallback.  Release/live smoke must fail visibly when public data is
    # unavailable; tests can opt in with ALLOW_FIXTURE_FALLBACK=1.
    allow_fixture_fallback = os.environ.get("ALLOW_FIXTURE_FALLBACK", "0").strip() == "1" and os.environ.get("DISABLE_FIXTURE_FALLBACK", "0") != "1"
    if instrument.asset_type is AssetType.EQUITY:
        providers: list[MarketProvider] = [YFinanceProvider()]
        if allow_fixture_fallback:
            providers.append(FixtureProvider())
        return ProviderChain(providers)
    providers = [BinancePublicProvider(), CoinGeckoPublicProvider()]
    if allow_fixture_fallback:
        providers.append(FixtureProvider())
    return ProviderChain(providers)
