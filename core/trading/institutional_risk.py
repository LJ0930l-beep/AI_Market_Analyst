"""Evidence-aware portfolio and execution risk primitives.

These helpers are deliberately independent from the order gateway.  They
read authoritative fills/marks supplied by a caller and return conservative
UNKNOWN/NOT_CONFIGURED states whenever correlation, depth, or fee evidence is
missing.  Decimal is used for all monetary arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import hashlib
import json
from typing import Any, Iterable, Mapping


UNKNOWN = "UNKNOWN"


def _decimal(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return result if result.is_finite() else default


@dataclass(frozen=True)
class RiskCluster:
    cluster_id: str
    name: str
    instruments: tuple[str, ...]
    max_fraction: Decimal
    source: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "name": self.name,
            "instruments": list(self.instruments),
            "max_fraction": str(self.max_fraction),
            "source": self.source,
            "status": self.status,
        }


@dataclass(frozen=True)
class ExposureSnapshot:
    instrument_key: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    contract_size: Decimal
    notional: Decimal
    risk_amount: Decimal | None
    cluster: RiskCluster
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_key": self.instrument_key,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": str(self.quantity),
            "price": str(self.price),
            "contract_size": str(self.contract_size),
            "notional": str(self.notional),
            "risk_amount": str(self.risk_amount) if self.risk_amount is not None else None,
            "cluster": self.cluster.to_dict(),
            "status": self.status,
        }


@dataclass(frozen=True)
class TCAResult:
    filled_quantity: Decimal
    notional: Decimal
    average_fill_price: Decimal | None
    implementation_shortfall: Decimal | None
    fees: Decimal | None
    slippage_cost: Decimal | None
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "filled_quantity": str(self.filled_quantity),
            "notional": str(self.notional),
            "average_fill_price": str(self.average_fill_price) if self.average_fill_price is not None else None,
            "implementation_shortfall": str(self.implementation_shortfall) if self.implementation_shortfall is not None else None,
            "fees": str(self.fees) if self.fees is not None else None,
            "slippage_cost": str(self.slippage_cost) if self.slippage_cost is not None else None,
            "status": self.status,
        }


def conservative_cluster(
    instrument_key: str,
    *,
    metadata: Mapping[str, Any] | None = None,
    default_max_fraction: Decimal = Decimal("0.005"),
) -> RiskCluster:
    """Resolve an explicit cluster, otherwise isolate the instrument."""

    clean_key = str(instrument_key or "").strip()
    metadata = metadata or {}
    raw_id = str(metadata.get("cluster_id") or "").strip()
    raw_name = str(metadata.get("cluster_name") or "").strip()
    if raw_id and raw_name:
        instruments = tuple(sorted({str(item).strip() for item in metadata.get("instruments", (clean_key,)) if str(item).strip()}))
        limit = _decimal(metadata.get("max_fraction"), default_max_fraction) or default_max_fraction
        if limit <= 0:
            limit = default_max_fraction
        return RiskCluster(raw_id, raw_name, instruments or (clean_key,), limit, "EXPLICIT_CATALOG", "RESOLVED")
    digest = hashlib.sha256(clean_key.encode("utf-8")).hexdigest()[:16] if clean_key else "unknown"
    return RiskCluster(
        cluster_id=f"unknown:{digest}",
        name="UNKNOWN_CLUSTER",
        instruments=(clean_key,) if clean_key else (),
        max_fraction=default_max_fraction,
        source="CONSERVATIVE_FALLBACK",
        status="FALLBACK_CONSERVATIVE",
    )


def calculate_exposure(
    quantity: Any,
    price: Any,
    *,
    contract_size: Any = Decimal("1"),
    stop_price: Any | None = None,
) -> tuple[Decimal, Decimal | None, str]:
    qty = _decimal(quantity)
    mark = _decimal(price)
    contract = _decimal(contract_size)
    if qty is None or mark is None or contract is None or qty < 0 or mark <= 0 or contract <= 0:
        return Decimal("0"), None, "UNKNOWN"
    notional = qty * mark * contract
    stop = _decimal(stop_price)
    risk = abs(mark - stop) * qty * contract if stop is not None and stop > 0 else None
    return notional, risk, "CALCULATED" if risk is not None else "RISK_STOP_UNKNOWN"


def build_exposure_snapshots(
    positions: Iterable[Mapping[str, Any]],
    *,
    cluster_metadata: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[ExposureSnapshot]:
    result: list[ExposureSnapshot] = []
    for position in positions:
        key = str(position.get("instrument_key") or position.get("symbol") or position.get("instrument_id") or "").strip()
        symbol = str(position.get("symbol") or position.get("instrument_id") or key).upper()
        qty = _decimal(position.get("remaining_contracts", position.get("quantity")))
        price = _decimal(position.get("mark_price", position.get("price", position.get("entry_price", position.get("entry")))))
        contract = _decimal(position.get("contract_size"), Decimal("1"))
        if qty is None or price is None or contract is None:
            result.append(ExposureSnapshot(key, symbol, str(position.get("side") or "UNKNOWN").upper(), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), None, conservative_cluster(key, metadata=(cluster_metadata or {}).get(key)), "UNKNOWN"))
            continue
        notional, risk, calc_status = calculate_exposure(
            qty,
            price,
            contract_size=contract,
            stop_price=position.get("stop_price", position.get("stop_loss", position.get("stop"))),
        )
        result.append(
            ExposureSnapshot(
                key,
                symbol,
                str(position.get("side") or "UNKNOWN").upper(),
                qty,
                price,
                contract,
                notional,
                risk,
                conservative_cluster(key, metadata=(cluster_metadata or {}).get(key)),
                calc_status,
            )
        )
    return result


def cluster_pressure(exposures: Iterable[ExposureSnapshot], *, equity: Any, max_fraction: Any = Decimal("0.005")) -> dict[str, Any]:
    equity_dec = _decimal(equity)
    limit_fraction = _decimal(max_fraction)
    items = list(exposures)
    if equity_dec is None or equity_dec <= 0 or limit_fraction is None or limit_fraction <= 0:
        return {"status": "UNKNOWN", "clusters": {}, "reason": "equity_or_limit_missing"}
    grouped: dict[str, Decimal] = {}
    missing_risk = False
    for item in items:
        if item.risk_amount is None:
            missing_risk = True
            continue
        grouped[item.cluster.cluster_id] = grouped.get(item.cluster.cluster_id, Decimal("0")) + item.risk_amount
    clusters: dict[str, Any] = {}
    for cluster_id, notional in sorted(grouped.items()):
        fraction = notional / equity_dec
        clusters[cluster_id] = {
            "risk_amount": str(notional),
            "fraction": str(fraction),
            "limit_fraction": str(limit_fraction),
            "status": "OVER_LIMIT" if fraction > limit_fraction else "WITHIN_LIMIT",
        }
    fallback = missing_risk or any(item.cluster.status != "RESOLVED" for item in items)
    return {"status": "CONSERVATIVE_FALLBACK" if fallback else "CALCULATED", "clusters": clusters, "basis": "STOP_RISK_AMOUNT"}


def estimate_paper_capacity(
    orderbook: Mapping[str, Any] | None,
    *,
    price: Any,
    participation_rate: Any = Decimal("0.10"),
) -> dict[str, Any]:
    """Estimate capacity only from explicit paper order-book depth."""

    if not isinstance(orderbook, Mapping):
        return {"status": "NOT_CONFIGURED", "capacity_quantity": None, "reason": "orderbook_depth_missing"}
    side_rows = orderbook.get("asks") or orderbook.get("bids")
    if not isinstance(side_rows, list) or not side_rows:
        return {"status": "NOT_CONFIGURED", "capacity_quantity": None, "reason": "orderbook_depth_missing"}
    rate = _decimal(participation_rate)
    mark = _decimal(price)
    if rate is None or mark is None or rate <= 0 or rate > 1 or mark <= 0:
        return {"status": "UNKNOWN", "capacity_quantity": None, "reason": "invalid_capacity_inputs"}
    depth = Decimal("0")
    for row in side_rows:
        if isinstance(row, Mapping):
            qty = _decimal(row.get("quantity", row.get("amount", row.get("size"))))
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            qty = _decimal(row[1])
        else:
            qty = None
        if qty is not None and qty > 0:
            depth += qty
    capacity = (depth * rate).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    return {"status": "CALCULATED_PAPER_DEPTH", "capacity_quantity": str(capacity), "depth_quantity": str(depth), "participation_rate": str(rate)}


def compute_tca(
    *,
    arrival_price: Any,
    side: str,
    fills: Iterable[Mapping[str, Any]],
    requested_quantity: Any | None = None,
) -> TCAResult:
    arrival = _decimal(arrival_price)
    sign = Decimal("1") if str(side).upper() in {"BUY", "LONG"} else Decimal("-1") if str(side).upper() in {"SELL", "SHORT"} else None
    filled = Decimal("0")
    notional = Decimal("0")
    fees: Decimal | None = Decimal("0")
    slippage: Decimal | None = Decimal("0")
    complete = True
    for fill in fills:
        qty = _decimal(fill.get("quantity", fill.get("filled_quantity")))
        price = _decimal(fill.get("price", fill.get("average_price")))
        if qty is None or price is None or qty < 0 or price <= 0:
            complete = False
            continue
        filled += qty
        notional += qty * price
        fee = _decimal(fill.get("fee", fill.get("fee_amount")))
        slip = _decimal(fill.get("slippage_cost", fill.get("slippage_fee")))
        if fee is None:
            fees = None
        elif fees is not None:
            fees += fee
        if slip is None:
            slippage = None
        elif slippage is not None:
            slippage += slip
    average = notional / filled if filled > 0 else None
    shortfall = sign * (average - arrival) * filled if sign is not None and arrival is not None and average is not None else None
    requested = _decimal(requested_quantity)
    if requested is not None and requested > filled:
        complete = False
    status = "CALCULATED" if complete and average is not None and fees is not None and slippage is not None else "PARTIAL_OR_COST_UNKNOWN"
    return TCAResult(filled, notional, average, shortfall, fees, slippage, status)


def json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, allow_nan=False))


__all__ = [
    "ExposureSnapshot",
    "RiskCluster",
    "TCAResult",
    "build_exposure_snapshots",
    "calculate_exposure",
    "cluster_pressure",
    "compute_tca",
    "conservative_cluster",
    "estimate_paper_capacity",
]
