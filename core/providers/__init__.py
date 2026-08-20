"""Market and news provider interfaces plus local fixtures."""

from .base import Bar, MarketProvider, ProviderError, Quote
from .binance import BinancePublicProvider
from .coingecko import CoinGeckoPublicProvider
from .fixture import FixtureNewsProvider, FixtureProvider
from .instrument_validation import (
    InstrumentValidationError,
    InstrumentValidationResult,
    InstrumentValidator,
    PublicInstrumentValidator,
)
from .news import ImpactHorizon, NewsCategory, NewsEvent, NewsProvider
from .runtime import MarketDataBundle, ProviderSnapshot, ProviderChain, build_default_provider, fetch_market_data

__all__ = [
    "Bar",
    "BinancePublicProvider",
    "CoinGeckoPublicProvider",
    "FixtureNewsProvider",
    "FixtureProvider",
    "InstrumentValidationError",
    "InstrumentValidationResult",
    "InstrumentValidator",
    "PublicInstrumentValidator",
    "MarketDataBundle",
    "MarketProvider",
    "ImpactHorizon",
    "NewsCategory",
    "NewsEvent",
    "NewsProvider",
    "ProviderError",
    "ProviderChain",
    "ProviderSnapshot",
    "Quote",
    "build_default_provider",
    "fetch_market_data",
]
