"""Evidence-backed settlement for Gate perpetual fills.

This module deliberately keeps remote settlement evidence separate from the
local ``trade_fills`` mirror.  Gate trade IDs are immutable venue facts; local
aggregate fills are useful for execution diagnostics but do not contain enough
information to settle remote fees, external exposure, or a net-position
lifecycle.

The public ``replay_observed_evidence`` method is also the offline acceptance
entrypoint.  It accepts native Gate records and writes only to the supplied
store, so callers can replay private records into an isolated temporary SQLite
database without credentials or an exchange connection.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
import uuid
from itertools import islice
from typing import Any, Iterable, Mapping

from .decision_memory import FLAT_BAND_USDT, update_memory_outcome
from .institutional_schema import ensure_institutional_trader_schema

logger = logging.getLogger(__name__)

_REMOTE_MODES = {"LIVE", "TESTNET"}
_ZERO = Decimal("0")
_MAX_EVIDENCE_ITEMS = 5000


def verified_fully_settled_entry_order_ids(
    store: Any,
    account_id: str,
    environment: str,
) -> set[str] | None:
    """Return exact Gate entry IDs backed by a complete, scope-matched settlement.

    ``None`` signals that settlement history could not be verified. Callers
    must not treat a missing or malformed history table as proof that no old
    entries were settled.
    """
    account = str(account_id or "")
    env = str(environment or "").upper()
    if not account or env not in _REMOTE_MODES or not callable(getattr(store, "_connect", None)):
        return None
    try:
        with store._connect() as db:
            tables = {
                str(row[0]) for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?, ?)",
                    ("gate_accounting_episodes", "gate_episode_settlements"),
                ).fetchall()
            }
            if tables != {"gate_accounting_episodes", "gate_episode_settlements"}:
                return None
            rows = db.execute(
                """SELECT e.episode_id,e.entry_order_id,e.entry_intent_id,s.status,s.settlement_json
                   FROM gate_accounting_episodes e
                   JOIN gate_episode_settlements s ON s.episode_id=e.episode_id
                   WHERE e.account_id=? AND UPPER(e.environment)=? AND s.status='SETTLED_FULL_COST'
                   ORDER BY s.settled_at DESC,e.episode_id""",
                (account, env),
            ).fetchall()
        settled: set[str] = set()
        for row in rows:
            order_id = str(row["entry_order_id"] or "").strip()
            intent_id = str(row["entry_intent_id"] or "").strip()
            episode_id = str(row["episode_id"] or "").strip()
            try:
                evidence = json.loads(row["settlement_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            if (
                not order_id.isdigit()
                or not intent_id.startswith("intent_ai_")
                or not episode_id
                or not isinstance(evidence, dict)
                or str(evidence.get("status") or "").upper() != "SETTLED_FULL_COST"
                or str(evidence.get("account_id") or "") != account
                or str(evidence.get("environment") or "").upper() != env
                or str(evidence.get("accounting_episode_id") or "") != episode_id
                or str(evidence.get("entry_order_id") or "") != order_id
                or str(evidence.get("entry_intent_id") or "") != intent_id
            ):
                return None
            settled.add(order_id)
        return settled
    except Exception:
        logger.warning("Could not verify fully settled Gate entry IDs", exc_info=True)
        return None


def _utc_iso(value: Any = None) -> str:
    if isinstance(value, datetime):
        point = value
    elif isinstance(value, str):
        try:
            point = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            point = datetime.now(timezone.utc)
    else:
        point = datetime.now(timezone.utc)
    if point.tzinfo is None:
        point = point.replace(tzinfo=timezone.utc)
    return point.astimezone(timezone.utc).isoformat()


def _decimal(value: Any, *, positive: bool = False, nonnegative: bool = False) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not result.is_finite():
        return None
    if positive and result <= 0:
        return None
    if nonnegative and result < 0:
        return None
    return result


def _dtext(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _number(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return _utc_iso(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return str(value)
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    encoded = value if isinstance(value, str) else _canonical_json(value)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _scope(account_id: str, environment: str) -> tuple[str, str]:
    account = str(account_id or "").strip()
    env = str(environment or "").strip().lower()
    if not account:
        raise ValueError("ACCOUNT_ID_REQUIRED")
    if env not in {"live", "testnet"}:
        raise ValueError("GATE_REMOTE_ENVIRONMENT_REQUIRED")
    return account, env


def _canonical_symbol(value: Any) -> str:
    text = str(value or "").upper().strip().replace(":USDT", "")
    return "".join(char for char in text if char.isalnum())


def _timestamp_ms(value: Any, *, field_name: str = "") -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        point = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(point.timestamp() * 1000)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            point = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                number = Decimal(text)
            except InvalidOperation:
                return None
        else:
            if point.tzinfo is None:
                point = point.replace(tzinfo=timezone.utc)
            return int(point.timestamp() * 1000)
    else:
        number = _decimal(value)
        if number is None:
            return None
    name = field_name.lower()
    if "_us" in name or "micro" in name or abs(number) >= Decimal("1e14"):
        return int(number / Decimal(1000))
    if "_ms" in name or "milli" in name or abs(number) >= Decimal("1e12"):
        return int(number)
    return int(number * Decimal(1000))


def _position_open_matches_entry_fill(first_open_ms: int, first_fill_ms: int) -> bool:
    """Match exact or second-rounded Gate position-open timestamps.

    Native trade fills retain millisecond time while Gate position-close
    history can expose ``first_open_time`` as whole seconds. That field may
    round either down or up, so a whole-second value within one second is a
    valid representation of the fill time. Higher-precision close timestamps
    retain the original same-second requirement.
    """
    if first_open_ms // 1000 == first_fill_ms // 1000:
        return True
    return (
        first_open_ms % 1000 == 0
        and abs(first_open_ms - first_fill_ms) <= 1000
    )


def _unwrap_native(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    info = record.get("info")
    return dict(info) if isinstance(info, dict) else dict(record)


def _metadata_for(metadata: Mapping[str, Any], contract: str, symbol: str) -> dict[str, Any]:
    value = metadata.get(contract) or metadata.get(symbol) or metadata.get(_canonical_symbol(contract)) or {}
    if not value:
        canonical = _canonical_symbol(contract or symbol)
        value = next(
            (candidate for key, candidate in metadata.items() if _canonical_symbol(key) == canonical),
            {},
        )
    return dict(value) if isinstance(value, Mapping) else {}


def _coverage_values(value: Mapping[str, Any] | None) -> dict[str, Any]:
    data = dict(value or {})
    start = _timestamp_ms(data.get("from_ms", data.get("start_ms", data.get("from"))), field_name="from_ms")
    end = _timestamp_ms(data.get("to_ms", data.get("end_ms", data.get("to"))), field_name="to_ms")
    requested_complete = data.get("complete") is True or str(data.get("status") or "").upper() in {"COMPLETE", "AVAILABLE"}
    explicit_scope = data.get("contract") or data.get("canonical_symbol") or data.get("symbol")
    global_unfiltered = data.get("global_unfiltered") is True
    if explicit_scope:
        canonical_symbol = _canonical_symbol(explicit_scope) or "*"
    elif global_unfiltered:
        canonical_symbol = "*"
    else:
        canonical_symbol = "*"
        data["reason"] = data.get("reason") or "COVERAGE_SCOPE_NOT_EXPLICIT"
    complete = requested_complete and (bool(explicit_scope) or global_unfiltered)
    try:
        page_count = max(0, int(data.get("page_count", data.get("pages_fetched", 0)) or 0))
    except (TypeError, ValueError):
        page_count = 0
    try:
        page_size = max(0, int(data.get("page_size", 0) or 0))
    except (TypeError, ValueError):
        page_size = 0
    return {
        "from_ms": start,
        "to_ms": end,
        "complete": bool(complete and start is not None and end is not None and end >= start),
        "canonical_symbol": canonical_symbol,
        "page_count": page_count,
        "page_size": page_size,
        "reason": str(data.get("reason") or ("COMPLETE" if complete else "COVERAGE_INCOMPLETE"))[:160],
        "source": str(data.get("source") or "GATE_PRIVATE_HISTORY")[:80],
        "observed_at": _utc_iso(data.get("observed_at")),
        "raw": data,
    }


def _create_tables(db: Any) -> None:
    ensure_institutional_trader_schema(db)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS gate_remote_trade_evidence (
            evidence_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            venue TEXT NOT NULL DEFAULT 'gate',
            trade_id TEXT NOT NULL,
            contract TEXT,
            canonical_symbol TEXT NOT NULL,
            order_id TEXT,
            event_at_ms INTEGER,
            side TEXT,
            quantity TEXT,
            price TEXT,
            contract_multiplier TEXT,
            multiplier_source TEXT,
            fee_amount TEXT,
            fee_currency TEXT,
            fee_currency_source TEXT,
            fee_source TEXT,
            point_fee TEXT,
            close_size TEXT,
            native_size TEXT,
            native_role TEXT,
            raw_hash TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            UNIQUE(account_id, environment, trade_id)
        );
        CREATE INDEX IF NOT EXISTS idx_gate_remote_trade_scope_time
            ON gate_remote_trade_evidence(account_id, environment, canonical_symbol, event_at_ms, trade_id);

        CREATE TABLE IF NOT EXISTS gate_remote_order_evidence (
            evidence_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            order_id TEXT NOT NULL,
            contract TEXT,
            canonical_symbol TEXT NOT NULL,
            is_reduce_only INTEGER,
            is_close INTEGER,
            observed_at TEXT NOT NULL,
            raw_hash TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            UNIQUE(account_id, environment, order_id, raw_hash)
        );

        CREATE TABLE IF NOT EXISTS gate_position_close_evidence (
            evidence_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            contract TEXT NOT NULL,
            canonical_symbol TEXT NOT NULL,
            voucher_id TEXT,
            side TEXT,
            first_open_time_ms INTEGER,
            closed_at_ms INTEGER,
            accum_size TEXT,
            pnl TEXT,
            pnl_pnl TEXT,
            pnl_fee TEXT,
            pnl_fund TEXT,
            pnl_dividend TEXT,
            raw_hash TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            UNIQUE(account_id, environment, evidence_id)
        );
        CREATE INDEX IF NOT EXISTS idx_gate_position_close_scope
            ON gate_position_close_evidence(account_id, environment, canonical_symbol, first_open_time_ms, closed_at_ms);

        CREATE TABLE IF NOT EXISTS gate_position_snapshot_evidence (
            snapshot_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            observed_at_ms INTEGER NOT NULL,
            positions_status TEXT NOT NULL,
            positions_json TEXT NOT NULL,
            raw_hash TEXT NOT NULL,
            received_at TEXT NOT NULL,
            UNIQUE(account_id, environment, observed_at_ms, raw_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_gate_position_snapshot_scope_time
            ON gate_position_snapshot_evidence(account_id, environment, observed_at_ms);

        CREATE TABLE IF NOT EXISTS gate_history_coverage_evidence (
            coverage_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            evidence_kind TEXT NOT NULL,
            canonical_symbol TEXT NOT NULL,
            from_ms INTEGER,
            to_ms INTEGER,
            complete INTEGER NOT NULL,
            page_count INTEGER NOT NULL,
            page_size INTEGER NOT NULL,
            reason TEXT NOT NULL,
            source TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            raw_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_gate_history_coverage_scope
            ON gate_history_coverage_evidence(account_id, environment, evidence_kind, canonical_symbol, complete, from_ms, to_ms);

        CREATE TABLE IF NOT EXISTS gate_accounting_episodes (
            episode_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            contract TEXT NOT NULL,
            canonical_symbol TEXT NOT NULL,
            entry_order_id TEXT NOT NULL,
            entry_intent_id TEXT NOT NULL,
            cycle_id TEXT,
            memory_id TEXT,
            side TEXT NOT NULL,
            strategy_id TEXT,
            strategy_version TEXT,
            identity_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(account_id, environment, entry_order_id)
        );

        CREATE TABLE IF NOT EXISTS gate_trade_episode_attributions (
            attribution_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            trade_id TEXT NOT NULL,
            episode_id TEXT,
            economic_role TEXT NOT NULL,
            attribution_status TEXT NOT NULL,
            basis TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            UNIQUE(account_id, environment, trade_id, attribution_id)
        );

        CREATE TABLE IF NOT EXISTS gate_episode_assessments (
            assessment_id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT,
            evidence_json TEXT NOT NULL,
            assessed_at TEXT NOT NULL,
            UNIQUE(episode_id, assessment_id)
        );

        CREATE TABLE IF NOT EXISTS gate_episode_settlements (
            settlement_id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL,
            status TEXT NOT NULL,
            settlement_currency TEXT,
            gross_realized TEXT,
            fee_effect TEXT,
            funding_effect TEXT,
            dividend_effect TEXT,
            total_pnl TEXT,
            fee_status TEXT NOT NULL,
            funding_status TEXT NOT NULL,
            pnl_source TEXT NOT NULL,
            settlement_json TEXT NOT NULL,
            settled_at TEXT NOT NULL,
            UNIQUE(episode_id, settlement_id)
        );

        CREATE TABLE IF NOT EXISTS gate_episode_memory_links (
            episode_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            cycle_id TEXT,
            memory_id TEXT NOT NULL,
            basis TEXT NOT NULL,
            linked_at TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            UNIQUE(account_id, environment, memory_id)
        );

        CREATE TABLE IF NOT EXISTS gate_episode_memory_link_attempts (
            episode_id TEXT PRIMARY KEY,
            attempted_at TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_status TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS gate_remote_evidence_conflicts (
            conflict_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            evidence_kind TEXT NOT NULL,
            evidence_identity TEXT NOT NULL,
            canonical_symbol TEXT NOT NULL DEFAULT '*',
            prior_hash TEXT NOT NULL,
            incoming_hash TEXT NOT NULL,
            incoming_json TEXT NOT NULL,
            observed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS gate_remote_evidence_rejections (
            rejection_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            evidence_kind TEXT NOT NULL,
            canonical_symbol TEXT NOT NULL DEFAULT '*',
            row_index INTEGER NOT NULL,
            reason TEXT NOT NULL,
            raw_hash TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            observed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS gate_history_sync_cursors (
            account_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            evidence_kind TEXT NOT NULL,
            canonical_symbol TEXT NOT NULL,
            from_ms INTEGER NOT NULL,
            to_ms INTEGER NOT NULL,
            next_offset INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(account_id, environment, evidence_kind, canonical_symbol, from_ms, to_ms)
        );

        CREATE TABLE IF NOT EXISTS gate_history_sync_targets (
            account_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            canonical_symbol TEXT NOT NULL,
            last_attempted_at TEXT NOT NULL,
            last_status TEXT NOT NULL,
            last_error TEXT,
            PRIMARY KEY(account_id, environment, canonical_symbol)
        );
        """
    )
    rejection_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(gate_remote_evidence_rejections)").fetchall()
    }
    if "canonical_symbol" not in rejection_columns:
        db.execute("ALTER TABLE gate_remote_evidence_rejections ADD COLUMN canonical_symbol TEXT NOT NULL DEFAULT '*'")


def _native_contract(raw: Mapping[str, Any]) -> str:
    value = raw.get("contract") or raw.get("symbol") or raw.get("currency_pair") or ""
    return str(value).strip()


def _native_trade_fields(record: Any, metadata: Mapping[str, Any]) -> dict[str, Any]:
    outer = dict(record) if isinstance(record, dict) else {}
    raw = _unwrap_native(outer)
    contract = _native_contract(raw) or _native_contract(outer)
    symbol = _canonical_symbol(contract or outer.get("symbol"))
    trade_id = str(raw.get("trade_id") or raw.get("id") or outer.get("id") or "").strip()
    order_id = str(raw.get("order_id") or raw.get("order") or outer.get("order_id") or outer.get("order") or "").strip()
    size = _decimal(raw.get("size"))
    amount = _decimal(raw.get("amount", outer.get("amount")), positive=True)
    quantity = abs(size) if size is not None and size != 0 else amount
    side_value = str(raw.get("side") or outer.get("side") or "").upper()
    if side_value not in {"BUY", "SELL"} and size is not None and size != 0:
        side_value = "BUY" if size > 0 else "SELL"
    if side_value not in {"BUY", "SELL"}:
        side_value = None
    price = _decimal(raw.get("price", outer.get("price")), positive=True)
    time_value = raw.get("create_time_ms")
    time_field = "create_time_ms"
    if time_value is None:
        time_value = raw.get("create_time", outer.get("timestamp", outer.get("time")))
        time_field = "create_time"
    event_at_ms = _timestamp_ms(time_value, field_name=time_field)
    contract_meta = dict(metadata)
    multiplier = _decimal(
        contract_meta.get("contract_size", contract_meta.get("contractSize", contract_meta.get("quanto_multiplier"))),
        positive=True,
    )
    multiplier_source = None
    if multiplier is not None:
        multiplier_source = str(
            contract_meta.get("contract_size_source")
            or contract_meta.get("multiplier_source")
            or ("GATE_CONTRACT_QUANTO_MULTIPLIER" if contract_meta.get("quanto_multiplier") is not None else "GATE_MARKET_CONTRACT_SIZE")
        )
    fee_raw = raw.get("fee")
    fee_source = "GATE_NATIVE_MY_TRADE" if "fee" in raw and fee_raw is not None else None
    if fee_raw is None and isinstance(outer.get("fee"), dict):
        fee_raw = outer["fee"].get("cost")
        fee_source = "CCXT_NORMALIZED_NATIVE_TRADE" if fee_raw is not None else fee_source
    fee_amount = _decimal(fee_raw)
    fee_currency = raw.get("fee_currency")
    fee_currency_source = "GATE_NATIVE_TRADE" if fee_currency else None
    if not fee_currency and isinstance(outer.get("fee"), dict):
        fee_currency = outer["fee"].get("currency")
        fee_currency_source = "CCXT_NORMALIZED_NATIVE_TRADE" if fee_currency else None
    if not fee_currency and contract_meta.get("settle"):
        fee_currency = contract_meta.get("settle")
        fee_currency_source = "GATE_CONTRACT_SETTLEMENT_METADATA"
    point_fee = _decimal(raw.get("point_fee"))
    close_size = _decimal(raw.get("close_size"))
    return {
        "raw": raw,
        "raw_record": outer,
        "contract": contract,
        "canonical_symbol": symbol,
        "trade_id": trade_id,
        "order_id": order_id or None,
        "event_at_ms": event_at_ms,
        "side": side_value,
        "quantity": quantity,
        "price": price,
        "contract_multiplier": multiplier,
        "multiplier_source": multiplier_source,
        "fee_amount": fee_amount,
        "fee_currency": str(fee_currency).upper() if fee_currency else None,
        "fee_currency_source": fee_currency_source,
        "fee_source": fee_source,
        "point_fee": point_fee,
        "close_size": close_size,
        "native_size": size,
        "native_role": str(raw.get("role") or "").upper() or None,
    }


def _native_order_fields(record: Any) -> dict[str, Any]:
    outer = dict(record) if isinstance(record, dict) else {}
    raw = _unwrap_native(outer)
    order_id = str(raw.get("id") or raw.get("order_id") or outer.get("id") or outer.get("order_id") or "").strip()
    contract = _native_contract(raw) or _native_contract(outer)
    reduce_value = raw.get("is_reduce_only", raw.get("reduce_only", raw.get("reduceOnly", outer.get("reduce_only", outer.get("reduceOnly")))))
    close_value = raw.get("is_close", raw.get("close", raw.get("isClose", outer.get("is_close", outer.get("close")))))
    def parse_bool(value: Any) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        clean = str(value).strip().lower()
        if clean in {"true", "1", "yes", "y"}:
            return True
        if clean in {"false", "0", "no", "n", ""}:
            return False
        return None
    is_reduce = parse_bool(reduce_value)
    is_close = parse_bool(close_value)
    return {"raw": raw, "order_id": order_id, "contract": contract, "canonical_symbol": _canonical_symbol(contract or outer.get("symbol")), "is_reduce_only": is_reduce, "is_close": is_close}


def _native_close_fields(record: Any) -> dict[str, Any]:
    outer = dict(record) if isinstance(record, dict) else {}
    raw = _unwrap_native(outer)
    contract = _native_contract(raw) or _native_contract(outer)
    time_value = raw.get("time_us")
    time_field = "time_us"
    if time_value is None:
        time_value = raw.get("time")
        time_field = "time"
    return {
        "raw": raw,
        "contract": contract,
        "canonical_symbol": _canonical_symbol(contract),
        "voucher_id": str(raw.get("voucher_id") or "").strip() or None,
        "side": str(raw.get("side") or "").lower() or None,
        "first_open_time_ms": _timestamp_ms(raw.get("first_open_time"), field_name="first_open_time"),
        "closed_at_ms": _timestamp_ms(time_value, field_name=time_field),
        "accum_size": _decimal(raw.get("accum_size"), positive=True),
        "pnl": _decimal(raw.get("pnl")),
        "pnl_pnl": _decimal(raw.get("pnl_pnl")),
        "pnl_fee": _decimal(raw.get("pnl_fee")),
        "pnl_fund": _decimal(raw.get("pnl_fund")),
        "pnl_dividend": _decimal(raw.get("pnl_dividend")),
    }


def _take_bounded(values: Iterable[Any] | None, limit: int) -> tuple[list[Any], bool]:
    iterator = iter(values or ())
    captured = list(islice(iterator, limit + 1))
    return captured[:limit], len(captured) > limit


class GateTradeSettlementService:
    """Persist and reconcile immutable Gate evidence into owned trade episodes."""

    def __init__(self, store: Any, *, clock: Any = None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> str:
        return _utc_iso(self.clock())

    def _ensure(self, db: Any) -> None:
        _create_tables(db)

    def record_position_snapshot(
        self,
        account_id: str,
        environment: str,
        *,
        positions: Iterable[Mapping[str, Any]],
        observed_at: Any,
        positions_status: str = "AVAILABLE",
        raw_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        account, env = _scope(account_id, environment)
        rows = [dict(item) for item in positions if isinstance(item, Mapping)]
        status = str(positions_status or "UNKNOWN").upper()
        time_iso = _utc_iso(observed_at)
        time_ms = _timestamp_ms(time_iso)
        if time_ms is None:
            raise ValueError("POSITION_SNAPSHOT_TIME_REQUIRED")
        payload = {"observed_at": time_iso, "positions_status": status, "positions": rows, "raw_snapshot": dict(raw_snapshot or {})}
        raw_json = _canonical_json(payload)
        raw_hash = _digest(raw_json)
        snapshot_id = _digest(f"{account}|{env}|{time_ms}|{raw_hash}")
        with self.store._connect() as db:
            self._ensure(db)
            self._insert_position_snapshot(db, account, env, time_ms, status, rows, raw_hash, snapshot_id)
        return {"snapshot_id": snapshot_id, "observed_at": time_iso, "positions_status": status, "recorded": True}

    def _insert_position_snapshot(self, db: Any, account: str, env: str, time_ms: int, status: str, rows: list[dict[str, Any]], raw_hash: str, snapshot_id: str) -> None:
        db.execute(
            """INSERT OR IGNORE INTO gate_position_snapshot_evidence
               (snapshot_id, account_id, environment, observed_at_ms, positions_status,
                positions_json, raw_hash, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (snapshot_id, account, env, time_ms, status, _canonical_json(rows), raw_hash, self._now()),
        )

    def _pending_entry_targets(self, account: str, env: str, *, limit: int) -> list[dict[str, Any]]:
        targets: dict[str, dict[str, Any]] = {}
        with self.store._connect() as db:
            self._ensure(db)
            for intent in self._intent_rows(db, account, env):
                if bool(intent.get("reduce_only")) or not intent.get("remote_order_id"):
                    continue
                if self._proven_terminal_zero_fill(db, account, env, intent):
                    continue
                symbol = str(intent.get("instrument_id") or "").strip()
                canonical = _canonical_symbol(symbol)
                if not symbol or not canonical:
                    continue
                episode_id = _digest(
                    f"gate_episode|{account}|{env}|{intent['remote_order_id']}|{intent.get('intent_id')}"
                )
                settled = db.execute(
                    """SELECT 1 FROM gate_episode_settlements
                       WHERE episode_id=? AND status='SETTLED_FULL_COST' LIMIT 1""",
                    (episode_id,),
                ).fetchone()
                if settled:
                    continue
                current = targets.get(canonical)
                created_at = str(intent.get("created_at") or "")
                if current is None or (created_at and created_at < current["created_at"]):
                    targets[canonical] = {"symbol": symbol, "canonical_symbol": canonical, "created_at": created_at, "cycle_id": intent.get("cycle_id"), "intents": []}
                targets[canonical]["intents"].append({"cycle_id": intent.get("cycle_id"), "created_at": created_at})
            last_attempts = {
                str(row["canonical_symbol"]): str(row["last_attempted_at"] or "")
                for row in db.execute(
                    """SELECT canonical_symbol, last_attempted_at FROM gate_history_sync_targets
                       WHERE account_id=? AND environment=?""",
                    (account, env),
                ).fetchall()
            }
        for canonical, target in targets.items():
            target["last_attempted_at"] = last_attempts.get(canonical, "")
        return sorted(
            targets.values(),
            key=lambda item: (item["last_attempted_at"], item["created_at"], item["canonical_symbol"]),
        )[:max(1, min(int(limit), 16))]

    def _proven_terminal_zero_fill(self, db: Any, account: str, env: str, intent: Mapping[str, Any]) -> bool:
        if str(intent.get("status") or "").upper() not in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
            return False
        order_id = str(intent.get("remote_order_id") or "")
        if not order_id:
            return False
        for table, query, params in (
            (
                "trade_fills",
                "SELECT quantity FROM trade_fills WHERE account_id=? AND order_id=?",
                (account, order_id),
            ),
            (
                "gate_remote_trade_evidence",
                "SELECT quantity FROM gate_remote_trade_evidence WHERE account_id=? AND environment=? AND order_id=?",
                (account, env, order_id),
            ),
        ):
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if exists:
                rows = db.execute(query, params).fetchall()
                if rows:
                    # A persisted fill/evidence row, even one whose quantity
                    # is malformed, prevents us from proving terminal zero.
                    return False
        receipt = intent.get("receipt") if isinstance(intent.get("receipt"), Mapping) else {}
        raw_evidence = receipt.get("execution_evidence")
        evidence = raw_evidence if isinstance(raw_evidence, Mapping) else {}
        keys = ("filled", "filled_amount", "cumulative_filled", "executed_quantity")
        known = [
            parsed
            for parsed in (_decimal(source.get(key), nonnegative=True) for source in (receipt, evidence) for key in keys)
            if parsed is not None
        ]
        return bool(known) and all(value == _ZERO for value in known)

    def _mark_sync_target(self, account: str, env: str, symbol: str, status: str, error: str | None = None) -> None:
        with self.store._connect() as db:
            self._ensure(db)
            db.execute(
                """INSERT INTO gate_history_sync_targets
                   (account_id, environment, canonical_symbol, last_attempted_at, last_status, last_error)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(account_id, environment, canonical_symbol) DO UPDATE SET
                     last_attempted_at=excluded.last_attempted_at,
                     last_status=excluded.last_status, last_error=excluded.last_error""",
                (account, env, symbol, self._now(), str(status)[:40], str(error)[:100] if error else None),
            )

    def _import_pre_entry_bundle_snapshots(self, account: str, env: str, target: Mapping[str, Any]) -> int:
        intents = [item for item in (target.get("intents") or []) if isinstance(item, Mapping) and item.get("cycle_id")]
        intents.sort(key=lambda item: str(item.get("created_at") or ""))
        candidates: list[dict[str, Any]] = []
        with self.store._connect() as db:
            self._ensure(db)
            cycle_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_led_cycles'").fetchone()
            bundle_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_bundles'").fetchone()
            if not cycle_table or not bundle_table:
                return 0
            for intent in intents[:32]:
                cycle_id = str(intent.get("cycle_id") or "")
                intent_ms = _timestamp_ms(intent.get("created_at"))
                if not cycle_id or intent_ms is None:
                    continue
                cycle = db.execute(
                    "SELECT account_id, payload_json FROM ai_led_cycles WHERE cycle_id=?",
                    (cycle_id,),
                ).fetchone()
                if cycle is None or str(cycle["account_id"] or "") != account:
                    continue
                try:
                    cycle_payload = json.loads(cycle["payload_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                bundle_id = str(cycle_payload.get("evidence_bundle_id") or "")
                if not bundle_id:
                    continue
                bundle_row = db.execute("SELECT payload_json FROM evidence_bundles WHERE bundle_id=?", (bundle_id,)).fetchone()
                if bundle_row is None:
                    continue
                try:
                    bundle_payload = json.loads(bundle_row["payload_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                source = bundle_payload.get("source_evidence") if isinstance(bundle_payload.get("source_evidence"), Mapping) else {}
                truth = source.get("account_truth") if isinstance(source.get("account_truth"), Mapping) else {}
                observed_at = truth.get("observed_at")
                observed_ms = _timestamp_ms(observed_at)
                positions = truth.get("positions")
                declared_account = str(truth.get("account_id") or "")
                declared_environment = str(truth.get("api_environment") or truth.get("environment") or "").strip().lower()
                expected_environment = "live" if env == "live" else "testnet"
                positions_status = str(truth.get("positions_status") or "").upper()
                snapshot_positions = positions
                if "positions_status" not in truth:
                    # Older frozen packets predate this explicit component
                    # status. Recover it only from the exact persisted Gate
                    # private snapshot they reference, with matching scope,
                    # timestamp, and position payload. Missing metadata alone
                    # is never treated as proof of a flat account.
                    snapshot_id = str(truth.get("snapshot_id") or "")
                    snapshot_table = db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gate_remote_account_snapshots'"
                    ).fetchone()
                    legacy = None
                    if snapshot_id and snapshot_table:
                        legacy = db.execute(
                            """SELECT provider, environment, observed_at, status, source, positions_json
                               FROM gate_remote_account_snapshots
                               WHERE snapshot_id=? AND account_id=? AND environment=?""",
                            (snapshot_id, account, expected_environment),
                        ).fetchone()
                    if legacy is not None:
                        source_name = str(legacy["source"] or "").lower()
                        try:
                            stored_positions = json.loads(legacy["positions_json"] or "[]")
                        except (TypeError, ValueError, json.JSONDecodeError):
                            stored_positions = None
                        exact_legacy_snapshot = (
                            str(legacy["provider"] or "").lower() == "gate"
                            and str(legacy["environment"] or "").lower() == expected_environment
                            and str(legacy["status"] or "").upper() == "AVAILABLE"
                            and "gate.io" in source_name and "private api" in source_name
                            and _timestamp_ms(legacy["observed_at"]) == observed_ms
                            and isinstance(stored_positions, list)
                            and _canonical_json(stored_positions) == _canonical_json(positions)
                        )
                        if exact_legacy_snapshot:
                            positions_status = "AVAILABLE"
                            snapshot_positions = stored_positions
                if (
                    observed_ms is None
                    or observed_ms > intent_ms
                    or str(truth.get("status") or "").upper() != "AVAILABLE"
                    or positions_status != "AVAILABLE"
                    or declared_account != account
                    or declared_environment != expected_environment
                    or not isinstance(positions, list)
                ):
                    continue
                candidates.append({
                    "observed_at": observed_at,
                    "positions_status": positions_status,
                    "positions": snapshot_positions,
                    "evidence_bundle_id": bundle_id,
                    "cycle_id": cycle_id,
                    "intent_created_at": intent.get("created_at"),
                    "source": "FROZEN_PRE_ENTRY_EVIDENCE_BUNDLE",
                })
        inserted = 0
        for snapshot in candidates:
            self.record_position_snapshot(
                account,
                env,
                positions=snapshot["positions"],
                observed_at=snapshot["observed_at"],
                positions_status=snapshot["positions_status"],
                raw_snapshot=snapshot,
            )
            inserted += 1
        return inserted

    def _history_start_ms(self, account: str, env: str, symbol: str, intent_created_at: Any) -> int | None:
        intent_ms = _timestamp_ms(intent_created_at)
        if intent_ms is None:
            return None
        with self.store._connect() as db:
            self._ensure(db)
            row = db.execute(
                """SELECT observed_at_ms FROM gate_position_snapshot_evidence
                   WHERE account_id=? AND environment=? AND positions_status='AVAILABLE'
                     AND observed_at_ms<=?
                   ORDER BY observed_at_ms DESC LIMIT 1""",
                (account, env, intent_ms),
            ).fetchone()
            return int(row["observed_at_ms"]) if row else intent_ms

    def _begin_history_window(self, account: str, env: str, kind: str, symbol: str, start_ms: int, end_ms: int) -> dict[str, Any]:
        with self.store._connect() as db:
            self._ensure(db)
            pending = db.execute(
                """SELECT from_ms, to_ms, next_offset, status FROM gate_history_sync_cursors
                   WHERE account_id=? AND environment=? AND evidence_kind=? AND canonical_symbol=?
                     AND status IN ('IN_PROGRESS','INCOMPLETE','FAILED')
                   ORDER BY updated_at LIMIT 1""",
                (account, env, kind, symbol),
            ).fetchone()
            if pending is not None:
                return {"from_ms": int(pending["from_ms"]), "to_ms": int(pending["to_ms"]), "offset": int(pending["next_offset"]), "page_count": 0}
            latest = db.execute(
                """SELECT MAX(to_ms) AS highwater FROM gate_history_coverage_evidence
                   WHERE account_id=? AND environment=? AND evidence_kind=?
                     AND canonical_symbol=? AND complete=1 AND to_ms IS NOT NULL""",
                (account, env, kind, symbol),
            ).fetchone()
            highwater = latest["highwater"] if latest else None
            window_start = max(start_ms, int(highwater) - 1000) if highwater is not None else start_ms
            cursor = int(db.execute(
                """SELECT COALESCE(MAX(next_offset),0) FROM gate_history_sync_cursors
                   WHERE account_id=? AND environment=? AND evidence_kind=? AND canonical_symbol=?
                     AND from_ms=? AND to_ms=?""",
                (account, env, kind, symbol, window_start, end_ms),
            ).fetchone()[0])
            db.execute(
                """INSERT OR IGNORE INTO gate_history_sync_cursors
                   (account_id, environment, evidence_kind, canonical_symbol, from_ms, to_ms,
                    next_offset, status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'IN_PROGRESS', ?)""",
                (account, env, kind, symbol, window_start, end_ms, cursor, self._now()),
            )
            return {"from_ms": window_start, "to_ms": end_ms, "offset": cursor, "page_count": 0}

    def _save_history_cursor(self, account: str, env: str, kind: str, symbol: str, window: Mapping[str, Any], *, offset: int, status: str) -> None:
        with self.store._connect() as db:
            self._ensure(db)
            db.execute(
                """INSERT INTO gate_history_sync_cursors
                   (account_id, environment, evidence_kind, canonical_symbol, from_ms, to_ms,
                    next_offset, status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(account_id, environment, evidence_kind, canonical_symbol, from_ms, to_ms)
                   DO UPDATE SET next_offset=excluded.next_offset, status=excluded.status, updated_at=excluded.updated_at""",
                (account, env, kind, symbol, int(window["from_ms"]), int(window["to_ms"]), int(offset), status, self._now()),
            )

    def retry_pending_memory_links(self, account_id: str, environment: str, *, limit: int = 20) -> dict[str, Any]:
        """Retry local AI-memory writes without making any exchange request."""
        account, env = _scope(account_id, environment)
        pending: list[tuple[dict[str, Any], dict[str, Any]]] = []
        with self.store._connect() as db:
            self._ensure(db)
            rows = db.execute(
                """SELECT e.*, s.settlement_json, a.attempted_at
                   FROM gate_accounting_episodes e
                   JOIN gate_episode_settlements s ON s.episode_id=e.episode_id
                   LEFT JOIN gate_episode_memory_links l ON l.episode_id=e.episode_id
                   LEFT JOIN gate_episode_memory_link_attempts a ON a.episode_id=e.episode_id
                   WHERE e.account_id=? AND e.environment=? AND s.status='SETTLED_FULL_COST'
                     AND l.episode_id IS NULL
                   ORDER BY COALESCE(a.attempted_at,''), s.settled_at, e.episode_id
                   LIMIT ?""",
                (account, env, max(1, min(int(limit), 100))),
            ).fetchall()
            for row in rows:
                try:
                    settlement = json.loads(row["settlement_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                pending.append((dict(row), settlement))
        resolved: list[dict[str, Any]] = []
        failed = 0
        for episode, settlement in pending:
            try:
                result = self._resolve_memory(episode, settlement)
            except Exception:
                logger.exception("Gate settlement memory retry failed for episode %s", episode.get("episode_id"))
                self._record_memory_link_attempt(str(episode["episode_id"]), "WRITE_FAILED")
                failed += 1
                continue
            if result and result.get("updated"):
                resolved.append(result)
            elif result is None:
                failed += 1
        return {"attempted": len(pending), "resolved": resolved, "pending": max(0, len(pending) - len(resolved)), "failed": failed}

    def sync_from_gate_provider(
        self,
        account_id: str,
        environment: str,
        trader: Any,
        *,
        position_snapshot: Mapping[str, Any] | None = None,
        max_symbols: int = 3,
        max_pages_per_stream: int = 2,
        page_size: int = 100,
        max_order_readbacks: int = 8,
    ) -> dict[str, Any]:
        """Run a bounded incremental sync only for persisted system entry intents.

        A call makes at most ``max_symbols * max_pages_per_stream * 2`` history
        page reads and ``max_order_readbacks`` exact order readbacks.  Incomplete
        pages keep their durable offset and are retried on a later cycle.
        """
        account, env = _scope(account_id, environment)
        memory_retry = self.retry_pending_memory_links(account, env, limit=20)
        if position_snapshot is not None:
            self.record_position_snapshot(
                account,
                env,
                positions=position_snapshot.get("positions") or (),
                observed_at=position_snapshot.get("observed_at"),
                positions_status=str(position_snapshot.get("positions_status") or "UNKNOWN"),
                raw_snapshot=position_snapshot,
            )
        targets = self._pending_entry_targets(account, env, limit=max_symbols)
        if not targets:
            return {"status": "IDLE_NO_UNSETTLED_SYSTEM_ENTRY", "account_id": account, "environment": env, "targets": 0, "memory_retry": memory_retry, "errors": []}
        if not callable(getattr(trader, "get_trade_history_page", None)) or not callable(getattr(trader, "get_position_close_history_page", None)):
            return {"status": "UNSUPPORTED", "account_id": account, "environment": env, "targets": len(targets), "memory_retry": memory_retry, "errors": ["GATE_PAGINATED_HISTORY_UNSUPPORTED"]}
        page_size = max(1, min(int(page_size), 100))
        page_budget = max(1, min(int(max_pages_per_stream), 5))
        readback_budget = max(0, min(int(max_order_readbacks), 24))
        errors: list[str] = []
        target_results: list[dict[str, Any]] = []
        readbacks_left = readback_budget
        for target in targets:
            requested_symbol = str(target["symbol"])
            self._mark_sync_target(account, env, str(target["canonical_symbol"]), "STARTED")
            imported_snapshots = self._import_pre_entry_bundle_snapshots(account, env, target)
            try:
                metadata = trader.get_market_metadata(requested_symbol)
                contract = str(metadata.get("native_symbol") or "").strip()
                if not contract:
                    raise ValueError("GATE_NATIVE_CONTRACT_METADATA_MISSING")
                meta = {
                    **dict(metadata),
                    "contract_size": metadata.get("quanto_multiplier", metadata.get("contract_size", metadata.get("contractSize"))),
                    "quanto_multiplier": metadata.get("quanto_multiplier", metadata.get("contractSize")),
                    "size_step": metadata.get("size_step", (metadata.get("limits") or {}).get("amount", {}).get("step")),
                    # The settlement currency is a contract fact.  Never
                    # manufacture USDT when metadata omitted it.
                    "settle": metadata.get("settle"),
                }
            except Exception as exc:
                code = str(getattr(exc, "code", "") or type(exc).__name__)[:100]
                errors.append(f"{requested_symbol}:MARKET_METADATA:{code}")
                self._mark_sync_target(account, env, str(target["canonical_symbol"]), "FAILED", code)
                target_results.append({"symbol": requested_symbol, "status": "DEGRADED", "error": code})
                continue
            start_ms = self._history_start_ms(account, env, _canonical_symbol(contract), target.get("created_at"))
            if start_ms is None:
                errors.append(f"{contract}:INTENT_TIME_UNAVAILABLE")
                continue
            end_ms = _timestamp_ms(self.clock()) or int(datetime.now(timezone.utc).timestamp() * 1000)
            native_trades: list[dict[str, Any]] = []
            native_closes: list[dict[str, Any]] = []
            native_orders: list[dict[str, Any]] = []
            stream_coverages: dict[str, dict[str, Any]] = {}
            stream_specs = (
                ("TRADES", "get_trade_history_page", native_trades, "GATE_NATIVE_FUTURES_MY_TRADES"),
                ("POSITION_CLOSES", "get_position_close_history_page", native_closes, "GATE_NATIVE_FUTURES_POSITION_CLOSES"),
            )
            for kind, method_name, collected, source in stream_specs:
                window = self._begin_history_window(account, env, kind, _canonical_symbol(contract), start_ms, end_ms)
                offset = int(window["offset"])
                page_count = (offset + page_size - 1) // page_size if offset else 0
                complete = False
                reason = "PAGE_BUDGET_EXCEEDED"
                try:
                    for _ in range(page_budget):
                        page = getattr(trader, method_name)(
                            requested_symbol,
                            from_ms=int(window["from_ms"]),
                            to_ms=int(window["to_ms"]),
                            limit=page_size,
                            offset=offset,
                        )
                        if not isinstance(page, Mapping) or not isinstance(page.get("records"), list) or "has_more" not in page:
                            raise ValueError("GATE_HISTORY_PAGE_SCHEMA_INVALID")
                        # Keep malformed raw rows so replay records a rejection
                        # and downgrades coverage; filtering them here could
                        # turn a corrupt page into apparently complete history.
                        raw_rows = page["records"]
                        rows = [dict(record) if isinstance(record, Mapping) else record for record in raw_rows]
                        new_offset = int(page.get("next_offset", offset + len(page["records"])))
                        has_more = bool(page["has_more"])
                        malformed_page = any(not isinstance(record, Mapping) for record in raw_rows)
                        page_count += 1
                        current_coverage = {
                            "contract": contract,
                            "from_ms": int(window["from_ms"]),
                            "to_ms": int(window["to_ms"]),
                            "complete": not has_more and not malformed_page,
                            "page_count": page_count,
                            "page_size": page_size,
                            "reason": (
                                "MALFORMED_HISTORY_PAGE_ROW" if malformed_page else
                                "PAGE_NOT_FINAL" if has_more else "COMPLETE"
                            ),
                            "source": source,
                        }
                        other_kind = "POSITION_CLOSES" if kind == "TRADES" else "TRADES"
                        other_coverage = {
                            "contract": contract,
                            "from_ms": int(window["from_ms"]),
                            "to_ms": int(window["to_ms"]),
                            "complete": False,
                            "page_count": 0,
                            "page_size": page_size,
                            "reason": "OTHER_HISTORY_STREAM_PENDING",
                            "source": "GATE_PRIVATE_HISTORY_SYNC",
                        }
                        trade_cov_page = current_coverage if kind == "TRADES" else other_coverage
                        close_cov_page = current_coverage if kind == "POSITION_CLOSES" else other_coverage
                        page_result = self.replay_observed_evidence(
                            account,
                            env,
                            native_trades=rows if kind == "TRADES" else (),
                            trade_coverage=trade_cov_page,
                            native_position_closes=rows if kind == "POSITION_CLOSES" else (),
                            position_close_coverage=close_cov_page,
                            position_snapshots=[position_snapshot] if isinstance(position_snapshot, Mapping) else [],
                            contract_metadata={contract: meta},
                            native_orders=(),
                        )
                        collected.extend(rows)
                        if malformed_page:
                            reason = "MALFORMED_HISTORY_PAGE_ROW"
                            errors.append(f"{contract}:{kind}:{reason}")
                            break
                        if has_more and new_offset <= offset:
                            reason = "HISTORY_NEXT_OFFSET_NOT_ADVANCING"
                            errors.append(f"{contract}:{kind}:{reason}")
                            break
                        offset = new_offset
                        if not has_more:
                            coverage_key = "trade_coverage" if kind == "TRADES" else "position_close_coverage"
                            recorded_coverage = page_result.get(coverage_key) or {}
                            complete = bool(recorded_coverage.get("complete"))
                            reason = str(recorded_coverage.get("reason") or "COMPLETE")
                            cursor_status = "COMPLETE" if complete else "FAILED"
                            self._save_history_cursor(account, env, kind, _canonical_symbol(contract), window, offset=0, status=cursor_status)
                            if not complete:
                                errors.append(f"{contract}:{kind}:{reason}")
                            break
                        self._save_history_cursor(account, env, kind, _canonical_symbol(contract), window, offset=offset, status="INCOMPLETE")
                    if not complete:
                        if reason in {"PAGE_BUDGET_EXCEEDED", "MALFORMED_HISTORY_PAGE_ROW", "HISTORY_NEXT_OFFSET_NOT_ADVANCING"}:
                            self._save_history_cursor(account, env, kind, _canonical_symbol(contract), window, offset=offset, status="INCOMPLETE")
                except Exception as exc:
                    code = str(getattr(exc, "code", "") or type(exc).__name__)[:100]
                    reason = f"HISTORY_FETCH_FAILED:{code}"
                    self._save_history_cursor(account, env, kind, _canonical_symbol(contract), window, offset=offset, status="FAILED")
                    errors.append(f"{contract}:{kind}:{code}")
                stream_coverages[kind] = {
                    "contract": contract,
                    "from_ms": int(window["from_ms"]),
                    "to_ms": int(window["to_ms"]),
                    "complete": complete,
                    "page_count": page_count,
                    "page_size": page_size,
                    "reason": reason,
                    "source": source,
                }
            if readbacks_left and callable(getattr(trader, "get_order_readback", None)):
                unique_order_ids = list(dict.fromkeys(
                    str(row.get("order_id") or row.get("order") or "")
                    for row in native_trades if isinstance(row, Mapping)
                ))
                unique_order_ids = [order_id for order_id in unique_order_ids if order_id]
                for order_id in unique_order_ids[:readbacks_left]:
                    try:
                        native_orders.append(trader.get_order_readback(order_id, requested_symbol))
                    except Exception as exc:
                        code = str(getattr(exc, "code", "") or type(exc).__name__)[:100]
                        errors.append(f"{contract}:ORDER_READBACK:{code}")
                    finally:
                        readbacks_left -= 1
                        if readbacks_left <= 0:
                            break
            snapshot_list = [position_snapshot] if isinstance(position_snapshot, Mapping) else []
            coverage = self.replay_observed_evidence(
                account,
                env,
                native_trades=native_trades,
                trade_coverage=stream_coverages["TRADES"],
                native_position_closes=native_closes,
                position_close_coverage=stream_coverages["POSITION_CLOSES"],
                position_snapshots=snapshot_list,
                contract_metadata={contract: meta},
                native_orders=native_orders,
            )
            target_status = "COMPLETE" if coverage["status"] == "AVAILABLE" else "DEGRADED"
            target_error = None if target_status == "COMPLETE" else ";".join(
                [str(coverage.get("trade_coverage", {}).get("reason") or ""), str(coverage.get("position_close_coverage", {}).get("reason") or "")]
            ).strip(";")
            self._mark_sync_target(account, env, _canonical_symbol(contract), target_status, target_error)
            target_results.append({"symbol": requested_symbol, "contract": contract, "status": coverage["status"], "episodes": coverage["episodes"], "coverage": {"trades": coverage["trade_coverage"], "position_closes": coverage["position_close_coverage"]}})
        all_complete = bool(target_results) and all(item.get("status") == "AVAILABLE" for item in target_results)
        return {
            "status": "AVAILABLE" if all_complete and not errors else "DEGRADED",
            "account_id": account,
            "environment": env,
            "targets": len(targets),
            "target_results": target_results,
            "memory_retry": memory_retry,
            "errors": errors,
            "limits": {"max_symbols": max_symbols, "max_pages_per_stream": page_budget, "page_size": page_size, "max_order_readbacks": readback_budget},
        }

    def replay_observed_evidence(
        self,
        account_id: str,
        environment: str,
        *,
        native_trades: Iterable[Mapping[str, Any]],
        trade_coverage: Mapping[str, Any],
        native_position_closes: Iterable[Mapping[str, Any]],
        position_close_coverage: Mapping[str, Any],
        position_snapshots: Iterable[Mapping[str, Any]],
        contract_metadata: Mapping[str, Mapping[str, Any]],
        native_orders: Iterable[Mapping[str, Any]] = (),
        max_episodes: int = 20,
    ) -> dict[str, Any]:
        """Replay native private records; no network, credential, or live DB access occurs here.

        The supplied store is the only write target.  ``trade_coverage`` and
        ``position_close_coverage`` must describe the full requested time
        windows, not merely the returned rows.  A page-budget result is saved
        as incomplete evidence and can never settle an episode.
        """

        account, env = _scope(account_id, environment)
        trades, trade_truncated = _take_bounded(native_trades, _MAX_EVIDENCE_ITEMS)
        closes, close_truncated = _take_bounded(native_position_closes, _MAX_EVIDENCE_ITEMS)
        orders, order_truncated = _take_bounded(native_orders, _MAX_EVIDENCE_ITEMS)
        snapshots, snapshot_truncated = _take_bounded(position_snapshots, _MAX_EVIDENCE_ITEMS)
        if not isinstance(contract_metadata, Mapping):
            contract_metadata = {}
        normalized_trades: list[dict[str, Any]] = []
        invalid_trades: list[tuple[int, str, str, str]] = []
        for index, record in enumerate(trades):
            contract = _native_contract(_unwrap_native(record))
            fields = _native_trade_fields(record, _metadata_for(contract_metadata, contract, _canonical_symbol(contract)))
            if not fields["trade_id"] or not fields["contract"] or not fields["canonical_symbol"]:
                invalid_trades.append((index, "NATIVE_TRADE_ID_OR_CONTRACT_MISSING", _canonical_json(record), _canonical_symbol(contract) or "*"))
            else:
                normalized_trades.append(fields)
        normalized_closes: list[dict[str, Any]] = []
        invalid_closes: list[tuple[int, str, str, str]] = []
        for index, record in enumerate(closes):
            fields = _native_close_fields(record)
            if not fields["contract"] or fields["first_open_time_ms"] is None or fields["closed_at_ms"] is None:
                invalid_closes.append((index, "NATIVE_POSITION_CLOSE_IDENTITY_OR_TIME_MISSING", _canonical_json(record), fields["canonical_symbol"] or "*"))
            else:
                normalized_closes.append(fields)
        trade_cov = _coverage_values(trade_coverage)
        close_cov = _coverage_values(position_close_coverage)
        if trade_truncated or invalid_trades:
            trade_cov["complete"] = False
            trade_cov["reason"] = "TRADE_EVIDENCE_BUDGET_EXCEEDED" if trade_truncated else "INVALID_NATIVE_TRADE_ROW"
        if close_truncated or invalid_closes:
            close_cov["complete"] = False
            close_cov["reason"] = "POSITION_CLOSE_EVIDENCE_BUDGET_EXCEEDED" if close_truncated else "INVALID_NATIVE_POSITION_CLOSE_ROW"
        if order_truncated or snapshot_truncated:
            # Missing order/snapshot evidence can invalidate ownership and the
            # flat baseline, so neither history stream can support settlement.
            trade_cov["complete"] = False
            close_cov["complete"] = False
            reason = "ORDER_OR_SNAPSHOT_EVIDENCE_BUDGET_EXCEEDED"
            trade_cov["reason"] = reason
            close_cov["reason"] = reason
        trade_cov["raw"] = {**dict(trade_cov["raw"]), "complete": trade_cov["complete"], "reason": trade_cov["reason"]}
        close_cov["raw"] = {**dict(close_cov["raw"]), "complete": close_cov["complete"], "reason": close_cov["reason"]}
        counts = {
            "trades_inserted": 0, "orders_inserted": 0, "position_closes_inserted": 0,
            "snapshots_inserted": 0, "evidence_conflicts": 0,
            "invalid_trade_rows": len(invalid_trades), "invalid_position_close_rows": len(invalid_closes),
            "input_truncated": int(trade_truncated) + int(close_truncated) + int(order_truncated) + int(snapshot_truncated),
        }
        with self.store._connect() as db:
            self._ensure(db)
            for kind, coverage, symbol in (
                ("TRADES", trade_cov, trade_cov["canonical_symbol"]),
                ("POSITION_CLOSES", close_cov, close_cov["canonical_symbol"]),
            ):
                raw_json = _canonical_json(coverage["raw"])
                coverage_id = _digest(f"{account}|{env}|{kind}|{coverage['from_ms']}|{coverage['to_ms']}|{raw_json}")
                db.execute(
                    """INSERT OR IGNORE INTO gate_history_coverage_evidence
                       (coverage_id, account_id, environment, evidence_kind, canonical_symbol,
                        from_ms, to_ms, complete, page_count, page_size, reason, source, observed_at, raw_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (coverage_id, account, env, kind, symbol, coverage["from_ms"], coverage["to_ms"],
                     int(coverage["complete"]), coverage["page_count"], coverage["page_size"], coverage["reason"],
                     coverage["source"], coverage["observed_at"], raw_json),
                )
            for kind, invalid_rows in (("TRADE", invalid_trades), ("POSITION_CLOSE", invalid_closes)):
                for index, reason, raw_json, rejected_symbol in invalid_rows:
                    raw_hash = _digest(raw_json)
                    reject_id = _digest(f"{account}|{env}|{kind}|{index}|{raw_hash}")
                    db.execute(
                        """INSERT OR IGNORE INTO gate_remote_evidence_rejections
                           (rejection_id, account_id, environment, evidence_kind, canonical_symbol,
                            row_index, reason, raw_hash, raw_json, observed_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (reject_id, account, env, kind, rejected_symbol, index, reason, raw_hash, raw_json, self._now()),
                    )
            for snapshot in snapshots:
                if not isinstance(snapshot, Mapping):
                    continue
                rows = [dict(item) for item in (snapshot.get("positions") or []) if isinstance(item, Mapping)]
                observed_at = _utc_iso(snapshot.get("observed_at"))
                time_ms = _timestamp_ms(observed_at)
                if time_ms is None:
                    continue
                status = str(snapshot.get("positions_status") or snapshot.get("status") or "UNKNOWN").upper()
                raw_json = _canonical_json({"observed_at": observed_at, "positions_status": status, "positions": rows, "raw_snapshot": dict(snapshot)})
                raw_hash = _digest(raw_json)
                snapshot_id = _digest(f"{account}|{env}|{time_ms}|{raw_hash}")
                before = db.total_changes
                self._insert_position_snapshot(db, account, env, time_ms, status, rows, raw_hash, snapshot_id)
                counts["snapshots_inserted"] += int(db.total_changes > before)
            for fields in normalized_trades:
                raw_json = _canonical_json(fields["raw"])
                raw_hash = _digest(raw_json)
                evidence_id = _digest(f"{account}|{env}|{fields['trade_id']}")
                cursor = db.execute(
                    """INSERT OR IGNORE INTO gate_remote_trade_evidence
                       (evidence_id, account_id, environment, venue, trade_id, contract, canonical_symbol,
                        order_id, event_at_ms, side, quantity, price, contract_multiplier, multiplier_source,
                        fee_amount, fee_currency, fee_currency_source, fee_source, point_fee, close_size,
                        native_size, native_role, raw_hash, raw_json, first_seen_at)
                       VALUES (?, ?, ?, 'gate', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (evidence_id, account, env, fields["trade_id"], fields["contract"], fields["canonical_symbol"],
                     fields["order_id"], fields["event_at_ms"], fields["side"], _dtext(fields["quantity"]),
                     _dtext(fields["price"]), _dtext(fields["contract_multiplier"]), fields["multiplier_source"],
                     _dtext(fields["fee_amount"]), fields["fee_currency"], fields["fee_currency_source"],
                     fields["fee_source"], _dtext(fields["point_fee"]), _dtext(fields["close_size"]),
                     _dtext(fields["native_size"]), fields["native_role"], raw_hash, raw_json, self._now()),
                )
                counts["trades_inserted"] += max(0, cursor.rowcount)
                self._check_immutable_conflict(db, account, env, "TRADE", fields["trade_id"], fields["canonical_symbol"], raw_hash, raw_json, counts)
            for record in orders:
                fields = _native_order_fields(record)
                if not fields["order_id"]:
                    continue
                raw_json = _canonical_json(fields["raw"])
                raw_hash = _digest(raw_json)
                evidence_id = _digest(f"{account}|{env}|{fields['order_id']}|{raw_hash}")
                cursor = db.execute(
                    """INSERT OR IGNORE INTO gate_remote_order_evidence
                       (evidence_id, account_id, environment, order_id, contract, canonical_symbol,
                        is_reduce_only, is_close, observed_at, raw_hash, raw_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (evidence_id, account, env, fields["order_id"], fields["contract"], fields["canonical_symbol"],
                     None if fields["is_reduce_only"] is None else int(fields["is_reduce_only"]),
                     None if fields["is_close"] is None else int(fields["is_close"]), self._now(), raw_hash, raw_json),
                )
                counts["orders_inserted"] += max(0, cursor.rowcount)
            for fields in normalized_closes:
                raw_json = _canonical_json(fields["raw"])
                raw_hash = _digest(raw_json)
                identity = fields["voucher_id"] or "|".join(
                    str(fields[key]) for key in ("contract", "side", "first_open_time_ms", "closed_at_ms", "accum_size")
                )
                evidence_id = _digest(f"{account}|{env}|{identity}")
                cursor = db.execute(
                    """INSERT OR IGNORE INTO gate_position_close_evidence
                       (evidence_id, account_id, environment, contract, canonical_symbol, voucher_id, side,
                        first_open_time_ms, closed_at_ms, accum_size, pnl, pnl_pnl, pnl_fee, pnl_fund,
                        pnl_dividend, raw_hash, raw_json, first_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (evidence_id, account, env, fields["contract"], fields["canonical_symbol"], fields["voucher_id"],
                     fields["side"], fields["first_open_time_ms"], fields["closed_at_ms"], _dtext(fields["accum_size"]),
                     _dtext(fields["pnl"]), _dtext(fields["pnl_pnl"]), _dtext(fields["pnl_fee"]),
                     _dtext(fields["pnl_fund"]), _dtext(fields["pnl_dividend"]), raw_hash, raw_json, self._now()),
                )
                counts["position_closes_inserted"] += max(0, cursor.rowcount)
                self._check_immutable_conflict(db, account, env, "POSITION_CLOSE", identity, fields["canonical_symbol"], raw_hash, raw_json, counts)
        episodes = self._reconcile(account, env, contract_metadata, max_episodes=max_episodes)
        return {
            "status": "AVAILABLE" if trade_cov["complete"] and close_cov["complete"] else "DEGRADED",
            "account_id": account,
            "environment": env,
            "trade_coverage": {key: trade_cov[key] for key in ("canonical_symbol", "from_ms", "to_ms", "complete", "page_count", "page_size", "reason")},
            "position_close_coverage": {key: close_cov[key] for key in ("canonical_symbol", "from_ms", "to_ms", "complete", "page_count", "page_size", "reason")},
            "counts": counts,
            "ingested_trades": len(normalized_trades),
            "episodes": episodes,
            "resolved_memory": [item["memory"] for item in episodes if item.get("memory")],
        }

    def _check_immutable_conflict(self, db: Any, account: str, env: str, kind: str, identity: str, symbol: str, incoming_hash: str, incoming_json: str, counts: dict[str, int]) -> None:
        if kind == "TRADE":
            row = db.execute(
                "SELECT raw_hash FROM gate_remote_trade_evidence WHERE account_id=? AND environment=? AND trade_id=?",
                (account, env, identity),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT raw_hash FROM gate_position_close_evidence WHERE account_id=? AND environment=? AND evidence_id=?",
                (account, env, _digest(f"{account}|{env}|{identity}")),
            ).fetchone()
        if row is None or str(row["raw_hash"]) == incoming_hash:
            return
        conflict_id = _digest(f"{account}|{env}|{kind}|{identity}|{row['raw_hash']}|{incoming_hash}")
        db.execute(
            """INSERT OR IGNORE INTO gate_remote_evidence_conflicts
               (conflict_id, account_id, environment, evidence_kind, evidence_identity, canonical_symbol,
                prior_hash, incoming_hash, incoming_json, observed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (conflict_id, account, env, kind, identity, symbol, str(row["raw_hash"]), incoming_hash, incoming_json, self._now()),
        )
        counts["evidence_conflicts"] += 1

    def _intent_rows(self, db: Any, account: str, env: str) -> list[dict[str, Any]]:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_intents'").fetchone()
        if exists is None:
            return []
        rows = db.execute(
            """SELECT * FROM order_intents WHERE account_id=? AND lower(COALESCE(environment, mode, ''))=?
                 AND lower(COALESCE(venue, provider, 'gate'))='gate' AND upper(COALESCE(mode,'')) IN ('LIVE','TESTNET')
               ORDER BY created_at, intent_id""",
            (account, env),
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                receipt = json.loads(item.get("execution_result_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                receipt = {}
            if not isinstance(receipt, dict):
                receipt = {}
            evidence = receipt.get("execution_evidence") if isinstance(receipt.get("execution_evidence"), dict) else {}
            order_id = receipt.get("order_id") or receipt.get("id") or evidence.get("remote_order_id")
            if order_id and not str(order_id).lower().startswith("ord_unknown_"):
                item["remote_order_id"] = str(order_id)
            else:
                item["remote_order_id"] = None
            item["receipt"] = receipt
            item["environment_clean"] = str(item.get("environment") or item.get("mode") or "").lower()
            output.append(item)
        return output

    def _episode_identity(self, db: Any, account: str, env: str, intent: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        order_id = intent.get("remote_order_id")
        intent_id = str(intent.get("intent_id") or "")
        if not order_id or not intent_id:
            return None, "REMOTE_ORDER_ID_UNAVAILABLE"
        if bool(intent.get("reduce_only")):
            return None, "NOT_AN_ENTRY_INTENT"
        symbol = _canonical_symbol(intent.get("instrument_id"))
        contract = str(intent.get("instrument_id") or "").strip()
        side = str(intent.get("side") or "").upper()
        side = {"LONG": "BUY", "SHORT": "SELL"}.get(side, side)
        if side not in {"BUY", "SELL"}:
            return None, "ENTRY_SIDE_UNVERIFIED"
        episode_id = _digest(f"gate_episode|{account}|{env}|{order_id}|{intent_id}")
        memory_id = None
        cycle_id = str(intent.get("cycle_id") or "") or None
        if cycle_id:
            memory_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_decision_memory'").fetchone()
            if memory_table:
                row = db.execute(
                    """SELECT memory_id, action, symbol FROM ai_decision_memory
                       WHERE account_id=? AND cycle_id=? AND lower(environment)=?
                       ORDER BY decision_at DESC, memory_id LIMIT 10""",
                    (account, cycle_id, env),
                ).fetchall()
                expected_action = "OPEN_LONG" if side == "BUY" else "OPEN_SHORT"
                matches = [
                    item for item in row
                    if str(item["action"] or "").upper() == expected_action
                    and _canonical_symbol(item["symbol"]) == symbol
                ]
                if len(matches) == 1:
                    memory_id = str(matches[0]["memory_id"])
        identity = {
            "episode_id": episode_id,
            "account_id": account,
            "environment": env,
            "contract": contract,
            "canonical_symbol": symbol,
            "entry_order_id": str(order_id),
            "entry_intent_id": intent_id,
            "cycle_id": cycle_id,
            "memory_id": memory_id,
            "side": side,
            "strategy_id": intent.get("strategy_id"),
            "strategy_version": intent.get("strategy_version"),
            "intent_created_at": intent.get("created_at"),
        }
        identity_json = _canonical_json(identity)
        db.execute(
            """INSERT OR IGNORE INTO gate_accounting_episodes
               (episode_id, account_id, environment, contract, canonical_symbol, entry_order_id,
                entry_intent_id, cycle_id, memory_id, side, strategy_id, strategy_version,
                identity_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (episode_id, account, env, contract, symbol, str(order_id), intent_id, cycle_id, memory_id,
             side, intent.get("strategy_id"), intent.get("strategy_version"), identity_json, self._now()),
        )
        existing = db.execute(
            "SELECT * FROM gate_accounting_episodes WHERE account_id=? AND environment=? AND entry_order_id=?",
            (account, env, str(order_id)),
        ).fetchone()
        if existing is None:
            return None, "EPISODE_IDENTITY_NOT_PERSISTED"
        if str(existing["entry_intent_id"]) != intent_id:
            return dict(existing), "ENTRY_ORDER_ID_COLLISION"
        return dict(existing), None

    def _coverage_covers(self, db: Any, account: str, env: str, kind: str, symbol: str, start_ms: int, end_ms: int) -> bool:
        rows = db.execute(
            """SELECT from_ms, to_ms FROM gate_history_coverage_evidence
               WHERE account_id=? AND environment=? AND evidence_kind=? AND complete=1
                 AND canonical_symbol IN (?, '*') AND from_ms IS NOT NULL AND to_ms IS NOT NULL
               ORDER BY from_ms, to_ms""",
            (account, env, kind, symbol),
        ).fetchall()
        cursor = start_ms
        for row in rows:
            left, right = int(row["from_ms"]), int(row["to_ms"])
            if right < cursor:
                continue
            if left > cursor:
                return False
            cursor = max(cursor, right + 1)
            if cursor > end_ms:
                return True
        return cursor > end_ms

    def _flat_baseline(self, db: Any, account: str, env: str, symbol: str, intent_created_at: Any, first_fill_ms: int) -> dict[str, Any] | None:
        intent_ms = _timestamp_ms(intent_created_at)
        rows = db.execute(
            """SELECT snapshot_id, observed_at_ms, positions_status, positions_json
               FROM gate_position_snapshot_evidence
               WHERE account_id=? AND environment=? AND observed_at_ms<=?
                 AND (? IS NULL OR observed_at_ms<=?)
               ORDER BY observed_at_ms DESC""",
            (account, env, first_fill_ms, intent_ms, intent_ms),
        ).fetchall()
        for row in rows:
            if str(row["positions_status"]).upper() != "AVAILABLE":
                continue
            try:
                positions = json.loads(row["positions_json"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            exposure = False
            if isinstance(positions, list):
                for position in positions:
                    if not isinstance(position, dict):
                        continue
                    if _canonical_symbol(position.get("contract") or position.get("symbol") or position.get("instrument_id")) != symbol:
                        continue
                    size = _decimal(position.get("size", position.get("contracts", position.get("quantity", position.get("positionAmt")))))
                    if size is None or size != 0:
                        exposure = True
                        break
            if not exposure:
                return {"snapshot_id": str(row["snapshot_id"]), "observed_at_ms": int(row["observed_at_ms"])}
        return None

    def _trade_rows(self, db: Any, account: str, env: str, symbol: str) -> list[dict[str, Any]]:
        rows = db.execute(
            """SELECT * FROM gate_remote_trade_evidence
               WHERE account_id=? AND environment=? AND canonical_symbol=?
               ORDER BY event_at_ms, trade_id""",
            (account, env, symbol),
        ).fetchall()
        return [dict(row) for row in rows]

    def _order_facts(self, db: Any, account: str, env: str, symbol: str) -> dict[str, dict[str, Any]]:
        rows = db.execute(
            """SELECT * FROM gate_remote_order_evidence
               WHERE account_id=? AND environment=? AND canonical_symbol=? ORDER BY observed_at""",
            (account, env, symbol),
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = result.setdefault(str(row["order_id"]), {"is_reduce_only": None, "is_close": None})
            if row["is_reduce_only"] is not None:
                item["is_reduce_only"] = bool(row["is_reduce_only"])
            if row["is_close"] is not None:
                item["is_close"] = bool(row["is_close"])
        return result

    def _position_close_rows(self, db: Any, account: str, env: str, symbol: str) -> list[dict[str, Any]]:
        rows = db.execute(
            """SELECT * FROM gate_position_close_evidence
               WHERE account_id=? AND environment=? AND canonical_symbol=?
               ORDER BY first_open_time_ms, closed_at_ms, evidence_id""",
            (account, env, symbol),
        ).fetchall()
        return [dict(row) for row in rows]

    def _record_assessment(self, db: Any, episode_id: str, status: str, reason: str | None, evidence: Mapping[str, Any]) -> None:
        payload = dict(evidence)
        raw_json = _canonical_json(payload)
        assessment_id = _digest(f"{episode_id}|{status}|{reason}|{raw_json}")
        db.execute(
            """INSERT OR IGNORE INTO gate_episode_assessments
               (assessment_id, episode_id, status, reason, evidence_json, assessed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (assessment_id, episode_id, status, reason, raw_json, self._now()),
        )

    def _record_attribution(self, db: Any, account: str, env: str, trade_id: str, episode_id: str | None, role: str, status: str, basis: str, evidence: Mapping[str, Any]) -> None:
        payload = _canonical_json(dict(evidence))
        attribution_id = _digest(f"{account}|{env}|{trade_id}|{episode_id}|{role}|{status}|{basis}|{payload}")
        db.execute(
            """INSERT OR IGNORE INTO gate_trade_episode_attributions
               (attribution_id, account_id, environment, trade_id, episode_id, economic_role,
                attribution_status, basis, evidence_json, observed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (attribution_id, account, env, trade_id, episode_id, role, status, basis, payload, self._now()),
        )

    def _save_settlement(self, db: Any, episode_id: str, settlement: Mapping[str, Any]) -> None:
        payload = _canonical_json(dict(settlement))
        settlement_id = _digest(f"{episode_id}|{payload}")
        db.execute(
            """INSERT OR IGNORE INTO gate_episode_settlements
               (settlement_id, episode_id, status, settlement_currency, gross_realized,
                fee_effect, funding_effect, dividend_effect, total_pnl, fee_status, funding_status,
                pnl_source, settlement_json, settled_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (settlement_id, episode_id, settlement.get("status"), settlement.get("settlement_currency"),
             settlement.get("gross_realized"), settlement.get("fee_effect"), settlement.get("funding_effect"),
             settlement.get("dividend_effect"), settlement.get("total_pnl"), settlement.get("fee_status"),
             settlement.get("funding_status"), settlement.get("pnl_source"), payload, self._now()),
        )

    def _resolve_memory(self, episode: Mapping[str, Any], settlement: Mapping[str, Any]) -> dict[str, Any] | None:
        if settlement.get("status") != "SETTLED_FULL_COST":
            return None
        pnl = _decimal(settlement.get("total_pnl"))
        if pnl is None:
            return None
        value = float(pnl)
        status = "WIN" if pnl > Decimal(str(FLAT_BAND_USDT)) else "LOSS" if pnl < -Decimal(str(FLAT_BAND_USDT)) else "FLAT"
        episode_id = str(episode["episode_id"])
        account_id = str(episode["account_id"])
        environment = str(episode["environment"])
        cycle_id = str(episode.get("cycle_id") or "")
        expected_action = "OPEN_LONG" if str(episode.get("side") or "").upper() in {"BUY", "LONG"} else "OPEN_SHORT"
        evidence = {
            "basis": "GATE_NATIVE_TRADE_AND_POSITION_CLOSE_LIFECYCLE",
            "accounting_episode_id": episode_id,
            "entry_intent_id": episode["entry_intent_id"],
            "entry_order_id": episode["entry_order_id"],
            "environment": environment,
            **dict(settlement),
        }
        memory_id: str | None = None
        with self.store._connect() as db:
            self._ensure(db)
            link = db.execute(
                "SELECT memory_id FROM gate_episode_memory_links WHERE episode_id=?",
                (episode_id,),
            ).fetchone()
            memory_id = str(link["memory_id"]) if link else (str(episode.get("memory_id") or "") or None)
            if not memory_id and cycle_id:
                rows = db.execute(
                    """SELECT memory_id, action, symbol FROM ai_decision_memory
                       WHERE account_id=? AND cycle_id=? AND lower(environment)=?
                       ORDER BY decision_at DESC, memory_id LIMIT 10""",
                    (account_id, cycle_id, environment),
                ).fetchall()
                candidates = [
                    row for row in rows
                    if str(row["action"] or "").upper() == expected_action
                    and _canonical_symbol(row["symbol"]) == str(episode.get("canonical_symbol") or "")
                ]
                if len(candidates) == 1:
                    memory_id = str(candidates[0]["memory_id"])
            if not memory_id:
                db.execute(
                    """INSERT INTO gate_episode_memory_link_attempts
                       (episode_id, attempted_at, attempt_count, last_status)
                       VALUES (?, ?, 1, 'WAITING_FOR_DECISION_MEMORY')
                       ON CONFLICT(episode_id) DO UPDATE SET
                         attempted_at=excluded.attempted_at,
                         attempt_count=gate_episode_memory_link_attempts.attempt_count+1,
                         last_status=excluded.last_status""",
                    (episode_id, self._now()),
                )
                return {"memory_id": None, "status": "WAITING_FOR_DECISION_MEMORY", "updated": False}
            row = db.execute(
                """SELECT memory_id, cycle_id, environment, symbol, action, outcome_status,
                          outcome_pnl, payload_json
                   FROM ai_decision_memory WHERE memory_id=? AND account_id=?""",
                (memory_id, account_id),
            ).fetchone()
            existing_link = db.execute(
                "SELECT memory_id FROM gate_episode_memory_links WHERE episode_id=?",
                (episode_id,),
            ).fetchone()
        if row is None:
            return None
        if (
            str(row["cycle_id"] or "") != cycle_id
            or str(row["environment"] or "").lower() != environment
            or str(row["action"] or "").upper() != expected_action
            or _canonical_symbol(row["symbol"]) != str(episode.get("canonical_symbol") or "")
        ):
            self._record_memory_link_attempt(episode_id, "DECISION_MEMORY_IDENTITY_MISMATCH")
            return None
        if existing_link and str(existing_link["memory_id"]) != memory_id:
            self._record_memory_link_attempt(episode_id, "DECISION_MEMORY_LINK_CONFLICT")
            return None
        if str(row["outcome_status"] or "").upper() not in {"", status}:
            self._record_memory_link_attempt(episode_id, "DECISION_MEMORY_OUTCOME_CONFLICT")
            return None
        existing_pnl = _decimal(row["outcome_pnl"])
        if existing_pnl is not None and existing_pnl != pnl:
            self._record_memory_link_attempt(episode_id, "DECISION_MEMORY_PNL_CONFLICT")
            return None
        written = update_memory_outcome(
            self.store,
            str(memory_id),
            outcome_status=status,
            outcome_pnl=value,
            lesson_zh=(
                f"{episode.get('canonical_symbol') or episode.get('contract')} 已完成可核验平仓："
                f"全成本收益 {value:+.8f} {settlement.get('settlement_currency')}。"
            ),
            evidence=evidence,
        )
        if not written:
            self._record_memory_link_attempt(episode_id, "DECISION_MEMORY_WRITE_FAILED")
            return None
        with self.store._connect() as db:
            self._ensure(db)
            db.execute(
                """INSERT OR IGNORE INTO gate_episode_memory_links
                   (episode_id, account_id, environment, cycle_id, memory_id, basis, linked_at, evidence_json)
                   VALUES (?, ?, ?, ?, ?, 'GATE_NATIVE_SETTLEMENT', ?, ?)""",
                (episode_id, account_id, environment, cycle_id or None, memory_id, self._now(), _canonical_json(evidence)),
            )
            linked = db.execute(
                "SELECT memory_id FROM gate_episode_memory_links WHERE episode_id=?",
                (episode_id,),
            ).fetchone()
            db.execute(
                """INSERT INTO gate_episode_memory_link_attempts
                   (episode_id, attempted_at, attempt_count, last_status)
                   VALUES (?, ?, 1, 'LINKED')
                   ON CONFLICT(episode_id) DO UPDATE SET
                     attempted_at=excluded.attempted_at,
                     attempt_count=gate_episode_memory_link_attempts.attempt_count+1,
                     last_status=excluded.last_status""",
                (episode_id, self._now()),
            )
        if not linked or str(linked["memory_id"]) != memory_id:
            return None
        return {"memory_id": memory_id, "outcome_status": status, "outcome_pnl": value, "updated": True}

    def _record_memory_link_attempt(self, episode_id: str, status: str) -> None:
        with self.store._connect() as db:
            self._ensure(db)
            db.execute(
                """INSERT INTO gate_episode_memory_link_attempts
                   (episode_id, attempted_at, attempt_count, last_status)
                   VALUES (?, ?, 1, ?)
                   ON CONFLICT(episode_id) DO UPDATE SET
                     attempted_at=excluded.attempted_at,
                     attempt_count=gate_episode_memory_link_attempts.attempt_count+1,
                     last_status=excluded.last_status""",
                (episode_id, self._now(), status[:80]),
            )

    def _reconcile(self, account: str, env: str, metadata: Mapping[str, Mapping[str, Any]], *, max_episodes: int) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        memory_updates: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        with self.store._connect() as db:
            self._ensure(db)
            intents = self._intent_rows(db, account, env)
            entry_intents = [item for item in intents if not bool(item.get("reduce_only"))]
            candidates: list[tuple[tuple[int, str, str], dict[str, Any], dict[str, Any], str | None]] = []
            memory_retries: list[tuple[tuple[str, str], dict[str, Any], dict[str, Any]]] = []
            for intent in entry_intents:
                episode, identity_error = self._episode_identity(db, account, env, intent)
                if episode is None:
                    continue
                settled = db.execute(
                    """SELECT settlement_json, settled_at FROM gate_episode_settlements
                       WHERE episode_id=? AND status='SETTLED_FULL_COST'
                       ORDER BY settled_at DESC, settlement_id DESC LIMIT 1""",
                    (episode["episode_id"],),
                ).fetchone()
                if settled:
                    linked = db.execute(
                        "SELECT 1 FROM gate_episode_memory_links WHERE episode_id=? LIMIT 1",
                        (episode["episode_id"],),
                    ).fetchone()
                    if not linked:
                        attempt = db.execute(
                            "SELECT attempted_at FROM gate_episode_memory_link_attempts WHERE episode_id=?",
                            (episode["episode_id"],),
                        ).fetchone()
                        retry_at = str(attempt["attempted_at"] or "") if attempt else ""
                        memory_retries.append(((retry_at, str(settled["settled_at"] or "")), episode, json.loads(settled["settlement_json"])))
                    continue
                latest = db.execute(
                    "SELECT status, assessed_at FROM gate_episode_assessments WHERE episode_id=? ORDER BY assessed_at DESC, assessment_id DESC LIMIT 1",
                    (episode["episode_id"],),
                ).fetchone()
                priority = (0, "", str(intent.get("created_at") or "")) if latest is None else (1, str(latest["assessed_at"] or ""), str(intent.get("created_at") or ""))
                candidates.append((priority, intent, episode, identity_error))
            candidates.sort(key=lambda item: item[0])
            for _priority, intent, episode, identity_error in candidates[:max(1, min(int(max_episodes), 100))]:
                status, reason, settlement, evidence = self._assess_episode(db, account, env, intent, episode, metadata, entry_intents, intents, identity_error)
                if settlement:
                    self._save_settlement(db, str(episode["episode_id"]), settlement)
                    memory_updates.append((len(output), episode, settlement))
                else:
                    pass
                self._record_assessment(db, str(episode["episode_id"]), status, reason, evidence)
                output.append({
                    "episode_id": str(episode["episode_id"]),
                    "account_id": account,
                    "environment": env,
                    "symbol": episode.get("canonical_symbol"),
                    "entry_order_id": episode.get("entry_order_id"),
                    "entry_intent_id": episode.get("entry_intent_id"),
                    "cycle_id": episode.get("cycle_id"),
                    "memory_id": episode.get("memory_id"),
                    "status": status,
                    "reason": reason,
                    "settlement": settlement,
                    "memory": None,
                })
            memory_retries.sort(key=lambda item: item[0])
            for _priority, episode, settlement in memory_retries[:max(1, min(int(max_episodes), 100))]:
                try:
                    settlement = json.loads(_canonical_json(settlement))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                memory_updates.append((len(output), episode, settlement))
                output.append({
                    "episode_id": str(episode["episode_id"]),
                    "account_id": account,
                    "environment": env,
                    "symbol": episode.get("canonical_symbol"),
                    "entry_order_id": episode.get("entry_order_id"),
                    "entry_intent_id": episode.get("entry_intent_id"),
                    "cycle_id": episode.get("cycle_id"),
                    "memory_id": episode.get("memory_id"),
                    "status": "SETTLED_FULL_COST",
                    "reason": None,
                    "settlement": settlement,
                    "memory": None,
                    "memory_link_status": "RETRY_PENDING",
                })
        for index, episode, settlement in memory_updates:
            try:
                resolved = self._resolve_memory(episode, settlement)
            except Exception:
                logger.exception("Gate settlement memory write failed for episode %s", episode.get("episode_id"))
                resolved = None
                output[index]["memory_link_status"] = "WRITE_FAILED"
            output[index]["memory"] = resolved
            if output[index].get("memory_link_status") != "WRITE_FAILED":
                output[index]["memory_link_status"] = (
                    "LINKED" if resolved and resolved.get("updated") else
                    str(resolved.get("status")) if resolved and resolved.get("status") else
                    "PENDING_OR_CONFLICT"
                )
        return output

    def _assess_episode(
        self,
        db: Any,
        account: str,
        env: str,
        intent: Mapping[str, Any],
        episode: Mapping[str, Any],
        metadata: Mapping[str, Mapping[str, Any]],
        entry_intents: list[dict[str, Any]],
        all_intents: list[dict[str, Any]],
        identity_error: str | None,
    ) -> tuple[str, str | None, dict[str, Any] | None, dict[str, Any]]:
        symbol = str(episode.get("canonical_symbol") or "")
        contract = str(episode.get("contract") or "")
        trades = self._trade_rows(db, account, env, symbol)
        order_facts = self._order_facts(db, account, env, symbol)
        close_rows = self._position_close_rows(db, account, env, symbol)
        conflicts = db.execute(
            """SELECT 1 FROM gate_remote_evidence_conflicts WHERE account_id=? AND environment=?
               AND evidence_kind IN ('TRADE','POSITION_CLOSE') AND canonical_symbol=? LIMIT 1""",
            (account, env, symbol),
        ).fetchone()
        evidence: dict[str, Any] = {
            "entry_order_id": episode["entry_order_id"],
            "entry_intent_id": episode["entry_intent_id"],
            "contract": contract,
            "scope": {"account_id": account, "environment": env},
            "trade_ids": [],
            "position_close_ids": [],
        }
        def unresolved(reason: str, status: str = "UNVERIFIED"):
            evidence["reason"] = reason
            return status, reason, None, evidence

        if identity_error:
            return unresolved(identity_error)
        if conflicts:
            return unresolved("IMMUTABLE_REMOTE_EVIDENCE_CONFLICT")
        rejected = db.execute(
            """SELECT 1 FROM gate_remote_evidence_rejections
               WHERE account_id=? AND environment=? AND evidence_kind IN ('TRADE','POSITION_CLOSE')
                 AND canonical_symbol IN (?, '*') LIMIT 1""",
            (account, env, symbol),
        ).fetchone()
        if rejected:
            return unresolved("MALFORMED_NATIVE_EVIDENCE_ROW")
        entry_order_id = str(episode.get("entry_order_id") or "")
        entry_side = str(episode.get("side") or "").upper()
        entry_trades = [row for row in trades if str(row.get("order_id") or "") == entry_order_id]
        entry_trades = [row for row in entry_trades if row.get("event_at_ms") is not None]
        if not entry_trades:
            return unresolved("AWAITING_NATIVE_ENTRY_FILL", "AWAITING_ENTRY_FILL")
        native_contracts = {str(row.get("contract") or "") for row in entry_trades if row.get("contract")}
        if len(native_contracts) > 1:
            return unresolved("ENTRY_ORDER_HAS_MULTIPLE_NATIVE_CONTRACTS")
        if native_contracts:
            contract = next(iter(native_contracts))
        meta = _metadata_for(metadata, contract, symbol)
        multiplier = _decimal(meta.get("contract_size", meta.get("contractSize", meta.get("quanto_multiplier"))), positive=True)
        size_step = _decimal(meta.get("size_step", meta.get("order_size_step", meta.get("quanto_size_step"))), positive=True)
        settlement_currency = str(meta.get("settle") or meta.get("settlement_currency") or "").upper() or None
        precision_value = meta.get("pnl_precision", 8)
        try:
            precision = max(0, min(int(precision_value), 18))
        except (TypeError, ValueError):
            precision = 8
        tolerance = Decimal(1).scaleb(-precision)
        if any(not row.get("quantity") or not row.get("price") or not row.get("side") for row in entry_trades):
            return unresolved("ENTRY_FILL_FIELDS_INCOMPLETE")
        if any(str(row.get("side")).upper() != entry_side for row in entry_trades):
            return unresolved("ENTRY_FILL_SIDE_MISMATCH")
        if multiplier is None or size_step is None or not settlement_currency:
            return unresolved("CONTRACT_MULTIPLIER_STEP_OR_SETTLEMENT_CURRENCY_UNKNOWN")
        first_fill_ms = min(int(row["event_at_ms"]) for row in entry_trades)
        entry_quantity = sum((_decimal(row.get("quantity"), positive=True) or _ZERO for row in entry_trades), _ZERO)
        if entry_quantity <= 0 or entry_quantity % size_step != 0:
            return unresolved("ENTRY_QUANTITY_NOT_ON_CONTRACT_STEP")
        for row in entry_trades:
            if _decimal(row.get("contract_multiplier"), positive=True) != multiplier:
                return unresolved("ENTRY_CONTRACT_MULTIPLIER_MISMATCH")
        intent_side = str(intent.get("side") or "").upper()
        intent_side = {"LONG": "BUY", "SHORT": "SELL"}.get(intent_side, intent_side)
        if _canonical_symbol(intent.get("instrument_id")) != symbol or intent_side != entry_side:
            return unresolved("ENTRY_INTENT_CONTRACT_MISMATCH")
        action_memory = None
        if episode.get("memory_id"):
            action_memory = db.execute(
                "SELECT action FROM ai_decision_memory WHERE memory_id=? AND account_id=?",
                (episode["memory_id"], account),
            ).fetchone()
        if action_memory is not None:
            expected_side = "BUY" if str(action_memory["action"]).upper() == "OPEN_LONG" else "SELL"
            if expected_side != entry_side:
                return unresolved("AI_MEMORY_ACTION_ENTRY_SIDE_MISMATCH")
        baseline = self._flat_baseline(db, account, env, symbol, intent.get("created_at"), first_fill_ms)
        if baseline is None:
            return unresolved("NO_VERIFIED_FLAT_PRE_ENTRY_SNAPSHOT")
        evidence["baseline_snapshot_id"] = baseline["snapshot_id"]
        evidence["first_native_fill_at_ms"] = first_fill_ms
        evidence["entry_quantity"] = _dtext(entry_quantity)
        evidence["entry_trade_ids"] = [str(row["trade_id"]) for row in entry_trades]
        entry_ids = {str(row["trade_id"]) for row in entry_trades}

        expected_position_side = "long" if entry_side == "BUY" else "short"
        exact_closes = [
            row for row in close_rows
            if str(row.get("contract") or "") == contract
            and row.get("first_open_time_ms") is not None
            and row.get("accum_size") is not None
            and _position_open_matches_entry_fill(int(row["first_open_time_ms"]), first_fill_ms)
            and str(row.get("side") or "").lower() == expected_position_side
            and _decimal(row.get("accum_size")) == entry_quantity
        ]
        if len(exact_closes) > 1:
            return unresolved("POSITION_CLOSE_LIFECYCLE_MATCH_AMBIGUOUS")
        lifecycle_close_at_ms = int(exact_closes[0]["closed_at_ms"] or 0) if exact_closes else None

        # Any additional same-contract entry/mixed trade in the open interval
        # makes lot attribution ambiguous.  We do not infer FIFO or netting.
        close_candidates: list[dict[str, Any]] = []
        system_reduction_ids = {
            str(row.get("remote_order_id"))
            for row in all_intents
            if bool(row.get("reduce_only")) and row.get("remote_order_id")
            and _canonical_symbol(row.get("instrument_id")) == symbol
        }
        relevant_rows = [
            row for row in trades
            if row.get("event_at_ms") is not None
            and baseline["observed_at_ms"] <= int(row["event_at_ms"])
            and (lifecycle_close_at_ms is None or int(row["event_at_ms"]) <= lifecycle_close_at_ms)
        ]
        for row in relevant_rows:
            trade_id = str(row["trade_id"])
            order_id = str(row.get("order_id") or "")
            event_ms = int(row["event_at_ms"])
            if trade_id in entry_ids:
                self._record_attribution(db, account, env, trade_id, str(episode["episode_id"]), "ENTRY", "VERIFIED", "EXACT_ENTRY_ORDER_ID", {"order_id": order_id, "intent_id": episode["entry_intent_id"]})
                continue
            if event_ms < first_fill_ms:
                return unresolved("SAME_CONTRACT_TRADE_AFTER_FLAT_SNAPSHOT_BEFORE_ENTRY")
            if order_id == entry_order_id:
                return unresolved("ENTRY_ORDER_CONTAINS_CLOSE_OR_MIXED_SIZE")
            native_size = _decimal(row.get("native_size"))
            close_size = _decimal(row.get("close_size"))
            if native_size is not None and native_size == 0:
                return unresolved("ZERO_NATIVE_TRADE_SIZE")
            pure_native_close = bool(
                native_size is not None and close_size is not None and close_size != 0
                and (native_size > 0) == (close_size > 0)
                and abs(native_size) <= abs(close_size)
            )
            mixed_native = bool(
                native_size is not None and close_size is not None and close_size != 0
                and (native_size > 0) == (close_size > 0)
                and abs(native_size) > abs(close_size)
            )
            facts = order_facts.get(order_id, {})
            marked_close = facts.get("is_reduce_only") is True or facts.get("is_close") is True
            if mixed_native:
                return unresolved("MIXED_CLOSE_AND_REVERSE_ENTRY_SIZE")
            if not (pure_native_close or marked_close or order_id in system_reduction_ids):
                return unresolved("FOREIGN_OR_UNCLASSIFIED_SAME_CONTRACT_TRADE")
            if not row.get("quantity") or not row.get("price") or not row.get("side"):
                return unresolved("CLOSING_FILL_FIELDS_INCOMPLETE")
            expected_close_side = "SELL" if entry_side == "BUY" else "BUY"
            if str(row.get("side")).upper() != expected_close_side:
                return unresolved("CLOSING_FILL_DIRECTION_UNVERIFIED")
            close_candidates.append(row)

        closed_quantity = sum((_decimal(row.get("quantity"), positive=True) or _ZERO for row in close_candidates), _ZERO)
        evidence["close_trade_ids"] = [str(row["trade_id"]) for row in close_candidates]
        evidence["closed_quantity"] = _dtext(closed_quantity)
        if closed_quantity < entry_quantity:
            status = "PARTIALLY_CLOSED" if closed_quantity > 0 else "OPEN"
            reason = "CONTRACT_STEP_QUANTITY_REMAINS_OPEN"
            for row in close_candidates:
                self._record_attribution(db, account, env, str(row["trade_id"]), str(episode["episode_id"]), "CLOSE", "VERIFIED_PARTIAL", "NATIVE_CLOSE_SIZE_OR_ORDER_READBACK", {"order_id": row.get("order_id")})
            evidence["reason"] = reason
            return status, reason, None, evidence
        if closed_quantity > entry_quantity:
            return unresolved("CLOSE_QUANTITY_EXCEEDS_ENTRY_QUANTITY")
        if closed_quantity % size_step != 0:
            return unresolved("CLOSE_QUANTITY_NOT_ON_CONTRACT_STEP")
        if not exact_closes:
            # A full trade-fills-derived result may be shown as an intermediate
            # observation, but never as a verified complete-cost settlement.
            return unresolved("EXACT_POSITION_CLOSE_LIFECYCLE_NOT_FOUND", "CLOSED_AWAITING_POSITION_CLOSE_EVIDENCE")
        if len(exact_closes) != 1:
            return unresolved("POSITION_CLOSE_LIFECYCLE_MATCH_AMBIGUOUS")
        close_evidence = exact_closes[0]
        close_at_ms = int(close_evidence["closed_at_ms"] or 0)
        evidence["position_close_evidence_id"] = str(close_evidence["evidence_id"])
        evidence["position_close_voucher_id"] = close_evidence.get("voucher_id")
        evidence["position_close_trade_pnl"] = close_evidence.get("pnl")
        evidence["position_close_components"] = {
            "pnl_pnl": close_evidence.get("pnl_pnl"), "pnl_fee": close_evidence.get("pnl_fee"),
            "pnl_fund": close_evidence.get("pnl_fund"), "pnl_dividend": close_evidence.get("pnl_dividend"),
        }
        if close_at_ms <= 0 or close_at_ms < max(int(row["event_at_ms"]) for row in close_candidates):
            return unresolved("POSITION_CLOSE_TIMESTAMP_PRECEDES_EXIT_FILL")
        if not self._coverage_covers(db, account, env, "TRADES", symbol, baseline["observed_at_ms"], close_at_ms):
            return unresolved("TRADE_HISTORY_COVERAGE_INCOMPLETE")
        if not self._coverage_covers(db, account, env, "POSITION_CLOSES", symbol, baseline["observed_at_ms"], close_at_ms):
            return unresolved("POSITION_CLOSE_HISTORY_COVERAGE_INCOMPLETE")

        entry_cash_flow = _ZERO
        for row in entry_trades:
            qty, price = _decimal(row.get("quantity")), _decimal(row.get("price"))
            if qty is None or price is None:
                return unresolved("ENTRY_ECONOMICS_UNAVAILABLE")
            entry_cash_flow += (qty * multiplier * price) * (Decimal(-1) if entry_side == "BUY" else Decimal(1))
        exit_cash_flow = _ZERO
        for row in close_candidates:
            qty, price = _decimal(row.get("quantity")), _decimal(row.get("price"))
            if qty is None or price is None:
                return unresolved("EXIT_ECONOMICS_UNAVAILABLE")
            close_side = str(row.get("side")).upper()
            exit_cash_flow += (qty * multiplier * price) * (Decimal(1) if close_side == "SELL" else Decimal(-1))
        gross = entry_cash_flow + exit_cash_flow
        position_gross = _decimal(close_evidence.get("pnl_pnl"))
        if position_gross is None:
            return unresolved("POSITION_CLOSE_GROSS_PNL_UNKNOWN")
        if abs(gross - position_gross) > tolerance:
            evidence["calculated_gross_realized"] = _dtext(gross)
            return unresolved("TRADE_PNL_DOES_NOT_MATCH_POSITION_CLOSE")

        fee_effect = _decimal(close_evidence.get("pnl_fee"))
        funding_effect = _decimal(close_evidence.get("pnl_fund"))
        dividend_effect = _decimal(close_evidence.get("pnl_dividend"))
        reported_total = _decimal(close_evidence.get("pnl"))
        if fee_effect is None:
            return unresolved("POSITION_CLOSE_FEE_UNKNOWN")
        if funding_effect is None:
            return unresolved("POSITION_CLOSE_FUNDING_NOT_VERIFIED")
        if dividend_effect is None:
            return unresolved("POSITION_CLOSE_DIVIDEND_COMPONENT_NOT_VERIFIED")
        if reported_total is None:
            return unresolved("POSITION_CLOSE_TOTAL_PNL_UNKNOWN")
        trade_fee_sum = _ZERO
        fee_rows = [*entry_trades, *close_candidates]
        if any(_decimal(row.get("fee_amount")) is None for row in fee_rows):
            return unresolved("NATIVE_TRADE_FEE_AMOUNT_UNKNOWN")
        fee_currencies = {str(row.get("fee_currency") or "").upper() for row in fee_rows}
        if "" in fee_currencies or fee_currencies != {settlement_currency}:
            return unresolved("NATIVE_TRADE_FEE_CURRENCY_UNKNOWN_OR_MISMATCH")
        for row in fee_rows:
            trade_fee_sum += _decimal(row.get("fee_amount")) or _ZERO
            point_fee = _decimal(row.get("point_fee"))
            if point_fee is None:
                return unresolved("NATIVE_POINT_FEE_UNKNOWN")
            if point_fee != 0:
                return unresolved("POINT_FEE_NEEDS_SETTLEMENT_CURRENCY_CONVERSION")
        if abs(trade_fee_sum - fee_effect) <= tolerance:
            fee_sign_convention = "NATIVE_FEE_IS_SIGNED_PNL_EFFECT"
        elif abs(-trade_fee_sum - fee_effect) <= tolerance:
            fee_sign_convention = "NATIVE_FEE_IS_SIGNED_CHARGED_AMOUNT"
        else:
            evidence["sum_native_trade_fee"] = _dtext(trade_fee_sum)
            return unresolved("TRADE_FEES_DO_NOT_MATCH_POSITION_CLOSE_FEE")
        calculated_total = gross + fee_effect + funding_effect + dividend_effect
        if abs(calculated_total - reported_total) > tolerance:
            evidence["calculated_total_pnl"] = _dtext(calculated_total)
            return unresolved("POSITION_CLOSE_COMPONENTS_DO_NOT_MATCH_TOTAL_PNL")
        if not size_step or entry_quantity % size_step != 0 or closed_quantity != entry_quantity:
            return unresolved("FULL_CLOSE_CONTRACT_STEP_NOT_PROVEN")

        for row in close_candidates:
            order_id = str(row.get("order_id") or "")
            attribution_basis = "SYSTEM_REDUCE_ONLY_ORDER_ID" if order_id in system_reduction_ids else "EXCLUSIVE_FLAT_BASELINE_NATIVE_CLOSE_SIZE_POSITION_CLOSE"
            self._record_attribution(
                db,
                account, env, str(row["trade_id"]), str(episode["episode_id"]), "CLOSE", "VERIFIED",
                attribution_basis,
                {"order_id": order_id, "close_size": row.get("close_size"), "position_close_evidence_id": close_evidence["evidence_id"]},
            )
        settlement = {
            "status": "SETTLED_FULL_COST",
            "account_id": account,
            "environment": env,
            "accounting_episode_id": str(episode["episode_id"]),
            "contract": contract,
            "side": "LONG" if entry_side == "BUY" else "SHORT",
            "entry_order_id": entry_order_id,
            "entry_intent_id": episode["entry_intent_id"],
            "entry_trade_ids": [str(row["trade_id"]) for row in entry_trades],
            "exit_trade_ids": [str(row["trade_id"]) for row in close_candidates],
            "position_close_evidence_id": str(close_evidence["evidence_id"]),
            "position_close_voucher_id": close_evidence.get("voucher_id"),
            "settlement_currency": settlement_currency,
            "contract_multiplier": _dtext(multiplier),
            "multiplier_source": str(meta.get("contract_size_source") or meta.get("multiplier_source") or "GATE_CONTRACT_METADATA"),
            "quantity_step": _dtext(size_step),
            "entry_quantity": _dtext(entry_quantity),
            "closed_quantity": _dtext(closed_quantity),
            "gross_realized": _dtext(gross),
            "fee_effect": _dtext(fee_effect),
            "fees_abs": _dtext(abs(fee_effect)),
            "fee_status": "VERIFIED_GATE_NATIVE_POSITION_CLOSE_AND_MY_TRADES",
            "fee_currency": settlement_currency,
            "fee_sources": ["GATE_NATIVE_MY_TRADE", "GATE_NATIVE_POSITION_CLOSE.pnl_fee"],
            "native_fee_sign_convention": fee_sign_convention,
            "funding_effect": _dtext(funding_effect),
            "funding_status": "VERIFIED_GATE_NATIVE_POSITION_CLOSE.pnl_fund",
            "dividend_effect": _dtext(dividend_effect),
            "total_pnl": _dtext(calculated_total),
            "reported_position_close_pnl": _dtext(reported_total),
            "pnl_precision": precision,
            "pnl_source": "DECIMAL_TRADE_CASHFLOWS_RECONCILED_TO_GATE_POSITION_CLOSE",
            "trade_history_coverage": "COMPLETE",
            "position_close_history_coverage": "COMPLETE",
            "baseline_snapshot_id": baseline["snapshot_id"],
            "first_native_fill_at_ms": first_fill_ms,
            "closed_at_ms": close_at_ms,
        }
        evidence["settlement"] = settlement
        return "SETTLED_FULL_COST", None, settlement, evidence


__all__ = ["GateTradeSettlementService"]
