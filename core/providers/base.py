"""Provider contracts. Providers fetch data; they do not create signals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..instruments import Instrument


class ProviderError(RuntimeError):
    """Raised when an external provider cannot supply valid data."""

    def __init__(self, message: str, *, code: str = "provider_error", provider: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.provider = provider


@dataclass(frozen=True, slots=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("OHLC bar violates high/low bounds")
        if self.low <= 0 or self.close <= 0:
            raise ValueError("prices must be positive")
        if self.volume < 0:
            raise ValueError("volume must be non-negative")


@dataclass(frozen=True, slots=True)
class Quote:
    instrument: Instrument
    timestamp: datetime
    price: float
    volume: float | None = None
    change_pct: float | None = None
    high: float | None = None
    low: float | None = None

    def __post_init__(self) -> None:
        if self.price <= 0:
            raise ValueError("quote price must be positive")


class MarketProvider(Protocol):
    def get_quote(self, instrument: Instrument) -> Quote:
        ...

    def get_bars(self, instrument: Instrument, timeframe: str, limit: int = 200) -> list[Bar]:
        ...
