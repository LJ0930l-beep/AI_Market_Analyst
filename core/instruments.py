"""Canonical instrument definitions.

The rest of the system receives an :class:`Instrument`, never a provider-specific
symbol string. This keeps stock/Crypto differences at the provider boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AssetType(StrEnum):
    EQUITY = "equity"
    CRYPTO = "crypto"


class TradingHours(StrEnum):
    REGULAR = "regular"
    AROUND_THE_CLOCK = "24x7"


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    asset_type: AssetType
    exchange: str
    currency: str
    timezone: str
    trading_hours: TradingHours
    sector: str | None = None

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not symbol or any(ch in symbol for ch in " /\\"):
            raise ValueError(f"invalid instrument symbol: {self.symbol!r}")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "exchange", self.exchange.strip().upper())
        object.__setattr__(self, "currency", self.currency.strip().upper())

    @property
    def base(self) -> str:
        """Base asset for crypto symbols such as BTCUSDT."""

        if self.asset_type is AssetType.CRYPTO and self.symbol.endswith(self.currency):
            return self.symbol[: -len(self.currency)]
        return self.symbol

    @property
    def quote_currency(self) -> str:
        return self.currency

    @property
    def quote(self) -> str:
        return self.currency


def instrument_for(symbol: str) -> Instrument:
    """Return the Phase 1 instrument mapping for supported smoke-test symbols."""

    normalized = symbol.strip().upper()
    if normalized in {"BTC", "BTCUSDT"}:
        return Instrument(
            symbol="BTCUSDT",
            asset_type=AssetType.CRYPTO,
            exchange="PUBLIC",
            currency="USDT",
            timezone="UTC",
            trading_hours=TradingHours.AROUND_THE_CLOCK,
            sector="Crypto",
        )
    if normalized in {"ETH", "ETHUSDT"}:
        return Instrument(
            symbol="ETHUSDT",
            asset_type=AssetType.CRYPTO,
            exchange="PUBLIC",
            currency="USDT",
            timezone="UTC",
            trading_hours=TradingHours.AROUND_THE_CLOCK,
            sector="Crypto",
        )
    if normalized in {"AAPL", "NVDA", "TSLA", "AMD"}:
        sector = {
            "AAPL": "Technology",
            "NVDA": "Semiconductors",
            "TSLA": "Automotive",
            "AMD": "Semiconductors",
        }[normalized]
        return Instrument(
            symbol=normalized,
            asset_type=AssetType.EQUITY,
            exchange="NASDAQ",
            currency="USD",
            timezone="America/New_York",
            trading_hours=TradingHours.REGULAR,
            sector=sector,
        )
    raise ValueError(f"unsupported Phase 1 instrument: {symbol!r}")


def phase1_universe() -> tuple[Instrument, ...]:
    return tuple(instrument_for(symbol) for symbol in ("AAPL", "NVDA", "TSLA", "AMD", "BTCUSDT", "ETHUSDT"))
