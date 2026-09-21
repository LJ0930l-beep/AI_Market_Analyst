"""Canonical instrument definitions.

The rest of the system receives an :class:`Instrument`, never a provider-specific
symbol string. This keeps stock/Crypto differences at the provider boundary.
"""

from __future__ import annotations

import re
import math
from dataclasses import dataclass
from enum import StrEnum


_IDENTITY_PART_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def canonical_instrument_key(
    venue: object,
    market_type: object,
    native_symbol: object,
    settle_currency: object,
    price_type: object,
) -> str:
    """Build the lossless market identity used by market-data and research.

    The legacy application used ``symbol`` as its primary key.  That is not
    sufficient for a spot/perpetual pair (or for last/mark/index prices), so
    the v3 identity is a deliberately boring, deterministic five-part key.
    Components are validated instead of being escaped implicitly; callers
    must pass the provider's native symbol exactly as a data identity field.
    """

    values = (
        str(venue or "").strip().lower(),
        str(market_type or "").strip().lower(),
        str(native_symbol or "").strip().upper(),
        str(settle_currency or "").strip().upper(),
        str(price_type or "").strip().lower(),
    )
    if any(not value for value in values):
        raise ValueError("instrument identity requires venue, market_type, native_symbol, settle_currency, and price_type")
    if any(not _IDENTITY_PART_RE.fullmatch(value) for value in values):
        raise ValueError("instrument identity contains unsupported characters")
    return ":".join(values)


# The trading path must evaluate exactly one bar identity.
#
# Gate persists the same 15m bar under several identities: the traded price
# (``last``), the mark price (``mark``) and the index price (``index``).  A
# pre-v3 mirror in the ``market_bars`` table adds a ``legacy`` identity on top.
# A read that omits the identity filters therefore returns two or three rows
# for one timestamp.
#
# That is not a cosmetic duplication.  ``core/quant/strategies.py`` rejects any
# series whose timestamps repeat, so an unfiltered read silently degenerates
# into a permanent ``WARMING_UP`` and no strategy candidate is ever produced --
# which in turn starves the AI cycle of anything to open.
#
# ``last`` is the traded-price series.  It is the only one that carries real
# volume, and it is what the autonomous strategy and the market radar already
# read, so pinning every trading reader to it keeps the whole path consistent.
TRADING_BAR_VENUE = "gate"
TRADING_BAR_MARKET_TYPE = "perpetual"
TRADING_BAR_PRICE_TYPE = "last"


def trading_bar_filters() -> dict[str, str]:
    """Identity filters restricting a bar read to the traded-price series."""

    return {
        "venue": TRADING_BAR_VENUE,
        "market_type": TRADING_BAR_MARKET_TYPE,
        "price_type": TRADING_BAR_PRICE_TYPE,
    }


def _dominant_identity_rows(rows: list[dict]) -> list[dict]:
    """Collapse a mixed read down to the single best-populated identity.

    Interleaving identities is what produced duplicate ``bar_start`` values and
    pinned every strategy at ``WARMING_UP``.  Keeping exactly one keeps the
    series unambiguous; which one wins matters less than not mixing them, and
    callers only reach this path when the Gate series is absent entirely.
    """

    if not rows:
        return []
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("instrument_key") or "")
        counts[key] = counts.get(key, 0) + 1
    if len(counts) <= 1:
        return rows
    dominant = max(counts, key=lambda key: (counts[key], key))
    return [row for row in rows if str(row.get("instrument_key") or "") == dominant]


def read_trading_bars(store: object, symbol: str, timeframe: str, *, limit: int) -> list[dict]:
    """Read one symbol/timeframe as a single, unambiguous bar identity.

    The Gate ``last`` series is preferred because it is the traded price with
    real volume.  Paper and simulated accounts have no Gate writer at all --
    ``upsert_market_bars`` without explicit identity arguments stores
    everything under ``legacy:unknown:<symbol>:UNKNOWN:last`` -- so when the
    Gate series is empty the read collapses to the single best-populated
    identity instead of returning nothing.  A plain unfiltered read is *not*
    an acceptable fallback: it interleaves identities and re-creates the
    duplicate-timestamp bug this function exists to prevent.

    Stores that predate the identity filters -- and minimal read-only test
    doubles that only implement ``list_market_bars(symbol, timeframe, limit=)``
    -- cannot accept the keyword filters, so that compatibility case is
    resolved here once rather than at every call site.
    """

    reader = getattr(store, "list_market_bars", None)
    if not callable(reader):
        return []
    try:
        rows = list(reader(symbol, timeframe, limit=limit, **trading_bar_filters()))
    except TypeError:
        return _dominant_identity_rows(list(reader(symbol, timeframe, limit=limit)))
    if rows:
        return rows
    try:
        fallback = list(reader(symbol, timeframe, limit=limit))
    except TypeError:
        return []
    return _dominant_identity_rows(fallback)


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
    price_type: str = "last"

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not symbol or any(ch in symbol for ch in " /\\"):
            raise ValueError(f"invalid instrument symbol: {self.symbol!r}")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "exchange", self.exchange.strip().upper())
        object.__setattr__(self, "currency", self.currency.strip().upper())
        object.__setattr__(self, "contract_type", str(self.contract_type).strip().lower())
        price_type = str(self.price_type).strip().lower()
        if price_type not in {"last", "mark", "index", "closed"}:
            raise ValueError("price_type must be one of last, mark, index, closed")
        object.__setattr__(self, "price_type", price_type)
        for name in ("contract_size", "tick_size", "step_size"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError(f"{name} must be a finite positive number")

    @property
    def instrument_id(self) -> str:
        """Composite Instrument ID conforming to spec.md Section 4 & 19."""
        venue = self.exchange.lower()
        asset = self.asset_type.value
        sym = self.symbol
        return f"{asset}:{venue}:{sym}:{self.contract_type}:{self.currency.lower()}"

    @property
    def market_type(self) -> str:
        """Canonical venue market type (spot vs perpetual is not inferred from symbol)."""

        if self.contract_type in {"perp", "swap", "future", "futures"}:
            return "perpetual"
        return self.contract_type or ("equity" if self.asset_type is AssetType.EQUITY else "spot")

    @property
    def native_symbol(self) -> str:
        return self.symbol

    @property
    def venue(self) -> str:
        return self.exchange

    @property
    def settle_currency(self) -> str:
        return self.currency

    @property
    def instrument_key(self) -> str:
        return canonical_instrument_key(
            self.exchange,
            self.market_type,
            self.symbol,
            self.currency,
            self.price_type,
        )

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


def market_type_for_symbol(symbol: str) -> str:
    """Return the ``supported_markets`` token for a watchlist symbol.

    Symbols outside the Phase 1 map (for example a freshly added crypto pair)
    are treated as crypto.  The trading path only routes Gate perpetuals, so
    defaulting to crypto refuses the equity-only strategies instead of
    silently subscribing them to a market whose rules they cannot evaluate.
    """

    try:
        return instrument_for(symbol).asset_type.value
    except ValueError:
        return AssetType.CRYPTO.value


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
            contract_size=float(payload.get("contract_size", 1.0)),
            tick_size=float(payload.get("tick_size", 0.01)),
            step_size=float(payload.get("step_size", 1.0)),
            contract_type=str(payload.get("contract_type", "spot")),
            capabilities=tuple(str(item) for item in payload.get("capabilities", ("trade", "market_data")) if str(item).strip()),
            price_type=str(payload.get("price_type", "last")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("stored instrument payload is invalid") from exc


def phase1_universe() -> tuple[Instrument, ...]:
    return tuple(instrument_for(symbol) for symbol in ("AAPL", "NVDA", "TSLA", "AMD", "BTCUSDT", "ETHUSDT", "SOLUSDT"))
