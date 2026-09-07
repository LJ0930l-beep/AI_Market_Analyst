"""Canonical instrument definitions.

The rest of the system receives an :class:`Instrument`, never a provider-specific
symbol string. This keeps stock/Crypto differences at the provider boundary.
"""

from __future__ import annotations

import re
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
    contract_size: float = 1.0
    tick_size: float = 0.01
    step_size: float = 1.0
    contract_type: str = "spot"
    capabilities: tuple[str, ...] = ("trade", "market_data")

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not symbol or any(ch in symbol for ch in " /\\"):
            raise ValueError(f"invalid instrument symbol: {self.symbol!r}")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "exchange", self.exchange.strip().upper())
        object.__setattr__(self, "currency", self.currency.strip().upper())
        object.__setattr__(self, "contract_type", str(self.contract_type).strip().lower())

    @property
    def instrument_id(self) -> str:
        """Composite Instrument ID conforming to spec.md Section 4 & 19."""
        venue = self.exchange.lower()
        asset = self.asset_type.value
        sym = self.symbol
        return f"{asset}:{venue}:{sym}:{self.contract_type}:{self.currency.lower()}"

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


class CandidateParseError(ValueError):
    """Raised when a V1 public instrument candidate is not structurally safe."""

    code = "INVALID_INSTRUMENT_CANDIDATE"


@dataclass(frozen=True, slots=True)
class InstrumentCandidate:
    """A parsed expansion candidate before any provider compatibility probe."""

    instrument: Instrument
    registry_source: str
    metadata_status: str
    metadata_labels: dict[str, str]

    @property
    def is_canonical(self) -> bool:
        return self.registry_source == "canonical"


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
    if normalized in {"SOL", "SOLUSDT"}:
        return Instrument(
            symbol="SOLUSDT",
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


def _candidate_error(message: str) -> CandidateParseError:
    return CandidateParseError(message)


def parse_instrument_candidate(symbol: object, asset_type: object) -> InstrumentCandidate:
    """Parse a safe public equity or Binance-compatible USDT candidate.

    Equity candidates accept an ASCII ticker with at most one class separator
    (``BRK.B`` or ``BF-B``), while crypto accepts a base alias (``SOL``) or a
    full ``SOLUSDT`` symbol.  No whitespace or path syntax is normalized away.
    Provider availability and listing status are deliberately checked later by
    the injected validator.
    """

    if not isinstance(asset_type, str) or not asset_type or asset_type != asset_type.strip() or any(char.isspace() for char in asset_type):
        raise _candidate_error("asset_type must be exactly equity or crypto")
    normalized_type = asset_type.lower()
    if normalized_type not in {AssetType.EQUITY.value, AssetType.CRYPTO.value}:
        raise _candidate_error("asset_type must be exactly equity or crypto")
    if not isinstance(symbol, str) or not symbol:
        raise _candidate_error("symbol must be a non-empty string")
    if symbol != symbol.strip() or any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 for char in symbol
    ):
        raise _candidate_error("symbol must not contain whitespace or control characters")
    if any(char in symbol for char in "/\\"):
        raise _candidate_error("symbol must not contain path separators")

    normalized = symbol.upper()
    if len(normalized) > 16:
        raise _candidate_error("symbol is too long")

    try:
        # SOL was introduced as an explicit V1.2 expansion symbol.  Keep its
        # V1.1 registration semantics (inferred Binance metadata) so older
        # imported registries do not change meaning on reopen.
        canonical = None if normalized in {"SOL", "SOLUSDT"} else instrument_for(normalized)
    except ValueError:
        canonical = None
    if canonical is not None:
        if canonical.asset_type.value != normalized_type:
            raise _candidate_error("symbol and asset_type do not identify the same instrument")
        return InstrumentCandidate(
            instrument=canonical,
            registry_source="canonical",
            metadata_status="canonical",
            metadata_labels={
                "exchange": "known",
                "sector": "known",
                "currency": "known",
                "timezone": "known",
                "trading_hours": "known",
            },
        )

    if normalized_type == AssetType.EQUITY.value:
        if normalized.endswith("USDT"):
            raise _candidate_error("USDT symbols must be registered as crypto")
        if len(normalized) > 10 or not re.fullmatch(r"[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)?", normalized):
            raise _candidate_error("equity symbol must use safe public ticker syntax")
        instrument = Instrument(
            symbol=normalized,
            asset_type=AssetType.EQUITY,
            exchange="UNKNOWN",
            currency="USD",
            timezone="America/New_York",
            trading_hours=TradingHours.REGULAR,
            sector=None,
        )
        return InstrumentCandidate(
            instrument=instrument,
            registry_source="registered",
            metadata_status="inferred",
            metadata_labels={
                "exchange": "unknown",
                "sector": "unknown",
                "currency": "inferred",
                "timezone": "inferred",
                "trading_hours": "inferred",
            },
        )

    if normalized == "USDT":
        raise _candidate_error("USDT is not an unambiguous crypto base symbol")
    full_symbol = normalized if normalized.endswith("USDT") else f"{normalized}USDT"
    base = full_symbol[:-4]
    if (
        len(full_symbol) > 16
        or base == "USDT"
        or base.endswith("USDT")
        or not re.fullmatch(r"[A-Z0-9]{2,12}", base)
        or not re.search(r"[A-Z]", base)
    ):
        raise _candidate_error("crypto symbol must be a safe Binance-compatible USDT spot symbol")
    instrument = Instrument(
        symbol=full_symbol,
        asset_type=AssetType.CRYPTO,
        exchange="BINANCE",
        currency="USDT",
        timezone="UTC",
        trading_hours=TradingHours.AROUND_THE_CLOCK,
        sector=None,
    )
    return InstrumentCandidate(
        instrument=instrument,
        registry_source="registered",
        metadata_status="inferred",
        metadata_labels={
            "exchange": "inferred",
            "sector": "unknown",
            "currency": "inferred",
            "timezone": "inferred",
            "trading_hours": "inferred",
        },
    )


def instrument_from_payload(payload: dict[str, object]) -> Instrument:
    """Rehydrate a stored, allowlisted Instrument payload."""

    try:
        return Instrument(
            symbol=str(payload["symbol"]),
            asset_type=AssetType(str(payload["asset_type"])),
            exchange=str(payload["exchange"]),
            currency=str(payload["currency"]),
            timezone=str(payload["timezone"]),
            trading_hours=TradingHours(str(payload["trading_hours"])),
            sector=payload.get("sector") if isinstance(payload.get("sector"), str) else None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("stored instrument payload is invalid") from exc


def phase1_universe() -> tuple[Instrument, ...]:
    return tuple(instrument_for(symbol) for symbol in ("AAPL", "NVDA", "TSLA", "AMD", "BTCUSDT", "ETHUSDT", "SOLUSDT"))
