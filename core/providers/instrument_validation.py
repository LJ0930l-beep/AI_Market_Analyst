"""Public-data compatibility validation for explicitly registered instruments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol

from ..instruments import AssetType, InstrumentCandidate
from .base import MarketProvider, ProviderError
from .binance import BinancePublicProvider
from .yfinance import YFinanceProvider


class InstrumentValidationError(RuntimeError):
    """A provider validation result that must not be persisted."""

    def __init__(self, code: str, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.provider = provider


@dataclass(frozen=True, slots=True)
class InstrumentValidationResult:
    provider: str
    validated_at: datetime
    data_as_of: datetime
    mode: str = "public_probe"

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "validated",
            "mode": self.mode,
            "provider": self.provider,
            "validated_at": self.validated_at.astimezone(timezone.utc).isoformat(),
            "data_as_of": self.data_as_of.astimezone(timezone.utc).isoformat(),
        }


class InstrumentValidator(Protocol):
    def validate(self, candidate: InstrumentCandidate) -> InstrumentValidationResult:
        ...


ProviderFactory = Callable[[], MarketProvider]


class PublicInstrumentValidator:
    """Probe one public quote endpoint with bounded timeout and retry policy.

    Equity validation uses Yahoo chart data and crypto validation uses Binance
    public spot ticker data. This service intentionally does not use
    ``ProviderChain`` or ``FixtureProvider``: a stale fixture cannot prove
    public-provider compatibility.
    """

    _UNSUPPORTED_CODES = frozenset({
        "empty_data",
        "invalid_symbol",
        "symbol_not_found",
        "unsupported_asset",
        "unsupported_symbol",
    })

    def __init__(
        self,
        *,
        equity_provider_factory: ProviderFactory | None = None,
        crypto_provider_factory: ProviderFactory | None = None,
        timeout: float = 4.0,
        retries: int = 1,
    ) -> None:
        self.timeout = min(max(float(timeout), 0.2), 5.0)
        self.retries = max(0, min(int(retries), 2))
        self._equity_provider_factory = equity_provider_factory or (
            lambda: YFinanceProvider(timeout=self.timeout, retries=self.retries, public_chart_only=True)
        )
        self._crypto_provider_factory = crypto_provider_factory or (
            lambda: BinancePublicProvider(timeout=self.timeout, retries=self.retries)
        )

    def _provider_factory(self, candidate: InstrumentCandidate) -> ProviderFactory:
        return (
            self._equity_provider_factory
            if candidate.instrument.asset_type is AssetType.EQUITY
            else self._crypto_provider_factory
        )

    @staticmethod
    def _provider_name(provider: MarketProvider) -> str:
        return str(getattr(provider, "provider_name", provider.__class__.__name__.lower()))

    def validate(self, candidate: InstrumentCandidate) -> InstrumentValidationResult:
        provider = self._provider_factory(candidate)()
        provider_name = self._provider_name(provider)
        if bool(getattr(provider, "stale", False)) or provider_name.lower().startswith("fixture"):
            raise InstrumentValidationError(
                "PROVIDER_UNAVAILABLE",
                "provider compatibility validation cannot use stale fixture data",
                provider=provider_name,
            )
        try:
            quote = provider.get_quote(candidate.instrument)
        except ProviderError as exc:
            code = "INSTRUMENT_UNSUPPORTED" if exc.code in self._UNSUPPORTED_CODES else "PROVIDER_UNAVAILABLE"
            raise InstrumentValidationError(code, str(exc), provider=exc.provider or provider_name) from exc
        except Exception as exc:  # pragma: no cover - network/provider dependent
            raise InstrumentValidationError("PROVIDER_UNAVAILABLE", str(exc), provider=provider_name) from exc

        if quote.instrument.symbol != candidate.instrument.symbol or quote.price <= 0:
            raise InstrumentValidationError(
                "PROVIDER_UNAVAILABLE",
                "provider returned an unusable quote",
                provider=provider_name,
            )
        data_as_of = quote.timestamp
        if data_as_of.tzinfo is None:
            data_as_of = data_as_of.replace(tzinfo=timezone.utc)
        return InstrumentValidationResult(
            provider=provider_name,
            validated_at=datetime.now(timezone.utc),
            data_as_of=data_as_of.astimezone(timezone.utc),
        )
