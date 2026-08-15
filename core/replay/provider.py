"""Point-in-time market/news adapters used by the Phase 3 replay runner."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from ..instruments import Instrument
from ..news_engine import NewsProvider
from ..providers.base import Bar, MarketProvider, ProviderError, Quote
from ..providers.runtime import ProviderChain, build_default_provider


@dataclass(frozen=True, slots=True)
class HistoricalBars:
    symbol: str
    timeframe: str
    provider: str
    bars: tuple[Bar, ...]

    @property
    def data_start(self) -> datetime:
        return self.bars[0].timestamp

    @property
    def data_end(self) -> datetime:
        return self.bars[-1].timestamp


class ReplayProvider(MarketProvider):
    """Expose only bars at or before one replay ``as_of`` timestamp."""

    provider_name = "replay"
    stale = False

    def __init__(self, instrument: Instrument, bars: Iterable[Bar], as_of: datetime, *, underlying_provider: str) -> None:
        self.instrument = instrument
        self.bars = tuple(sorted(bars, key=lambda bar: bar.timestamp))
        self.as_of = as_of.astimezone(timezone.utc)
        self.underlying_provider = underlying_provider

    def _eligible(self) -> list[Bar]:
        eligible = [bar for bar in self.bars if bar.timestamp <= self.as_of]
        if len(eligible) < 60:
            raise ProviderError(
                f"replay has only {len(eligible)} bars before {self.as_of.isoformat()}",
                code="insufficient_history",
                provider=self.provider_name,
            )
        return eligible

    def get_bars(self, instrument: Instrument, timeframe: str, limit: int = 200) -> list[Bar]:
        if instrument.symbol != self.instrument.symbol:
            raise ProviderError("replay instrument mismatch", code="instrument_mismatch", provider=self.provider_name)
        return self._eligible()[-max(1, min(int(limit), 1000)) :]

    def get_quote(self, instrument: Instrument) -> Quote:
        if instrument.symbol != self.instrument.symbol:
            raise ProviderError("replay instrument mismatch", code="instrument_mismatch", provider=self.provider_name)
        eligible = self._eligible()
        last = eligible[-1]
        previous = eligible[-2].close if len(eligible) > 1 else None
        return Quote(
            instrument,
            last.timestamp,
            last.close,
            last.volume,
            ((last.close / previous) - 1.0) * 100.0 if previous else None,
            last.high,
            last.low,
        )


class ReplayNewsProvider(NewsProvider):
    """Explicit technical-only replay provider when point-in-time news is absent."""

    provider_name = "replay_news_unavailable"
    history_available = False

    def get_events(self, instrument: Instrument, limit: int = 20):
        # Empty is intentional: the replay context records the capability flag
        # instead of inventing neutral historical news.
        return []


def fetch_historical_bars(instrument: Instrument, timeframe: str, limit: int = 1000) -> HistoricalBars:
    """Fetch one bounded historical cache while preserving the real provider name."""

    provider = build_default_provider(instrument)
    candidates = provider.providers if isinstance(provider, ProviderChain) else [provider]
    errors: list[str] = []
    for candidate in candidates:
        name = str(getattr(candidate, "provider_name", candidate.__class__.__name__.lower()))
        try:
            bars = tuple(sorted(candidate.get_bars(instrument, timeframe, limit), key=lambda bar: bar.timestamp))
            if len(bars) < 60:
                raise ProviderError("historical provider returned too few bars", code="insufficient_history", provider=name)
            return HistoricalBars(instrument.symbol, timeframe, name, bars)
        except Exception as exc:
            errors.append(f"{name}:{getattr(exc, 'code', 'provider_error')}")
    raise ProviderError(
        f"historical providers failed ({'; '.join(errors)})",
        code="replay_history_unavailable",
        provider="replay",
    )


def build_as_of_points(
    bars: Iterable[Bar],
    *,
    timeframe: str,
    count: int,
    seed: int,
    min_history: int = 120,
) -> list[datetime]:
    """Choose deterministic, non-overlapping-enforced candidate timestamps."""

    ordered = sorted(bars, key=lambda bar: bar.timestamp)
    if count <= 0:
        return []
    duration_hours = {"5m": 5 / 60, "15m": 15 / 60, "1h": 1, "4h": 4, "1d": 24}.get(timeframe.lower())
    if duration_hours is None:
        raise ValueError(f"unsupported replay timeframe: {timeframe}")
    reserve = max(2, int(72 / duration_hours) + 2)
    candidates = list(range(min_history, max(min_history, len(ordered) - reserve)))
    if len(candidates) < count:
        raise ValueError(f"not enough replay points for {timeframe}: requested {count}, available {len(candidates)}")
    selected = sorted(random.Random(seed).sample(candidates, count))
    return [ordered[index].timestamp.astimezone(timezone.utc) for index in selected]


def future_bars(bars: Iterable[Bar], as_of: datetime) -> list[Bar]:
    cutoff = as_of.astimezone(timezone.utc)
    return [bar for bar in sorted(bars, key=lambda item: item.timestamp) if bar.timestamp > cutoff]
