"""Closed-15m candidate scanner for the AI-led session.

This scanner evaluates the six registered strategies as a research signal
stage only.  It never calls an execution adapter and never creates an order.
The stable candidate key is the account, symbol, strategy, and closed 15m bar
boundary, so retries cannot create another decision target for the same bar.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Callable

from ..providers.base import Bar
from ..quant.strategies import STRATEGIES, TradeProposal
from .institutional_schema import ensure_institutional_trader_schema


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        point = value
    elif value:
        try:
            point = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    return point.replace(tzinfo=timezone.utc) if point.tzinfo is None else point.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _bar(row: dict[str, Any], *, timeframe: str) -> Bar | None:
    timestamp = _utc(row.get("bar_start") or row.get("timestamp"))
    bar_end = _utc(row.get("bar_end"))
    if timestamp is None:
        return None
    try:
        values = [float(row[name]) for name in ("open", "high", "low", "close", "volume")]
    except (KeyError, TypeError, ValueError):
        return None
    return Bar(
        timestamp,
        *values,
        bar_end=bar_end or timestamp + timedelta(minutes=5 if timeframe == "5m" else 15 if timeframe == "15m" else 60),
        is_closed=bool(row.get("is_closed")),
        event_time=_utc(row.get("event_time")) or timestamp,
        available_at=_utc(row.get("available_at") or row.get("data_as_of")),
        fetched_at=_utc(row.get("fetched_at") or row.get("received_at")),
        revision_id=str(row.get("revision_id") or "") or None,
        source=str(row.get("source") or row.get("provider") or "") or None,
    )


class CandidateScanner:
    def __init__(self, store: Any, *, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with store._connect() as db:
            ensure_institutional_trader_schema(db)

    def _subscriptions(self, symbols: tuple[str, ...]) -> dict[tuple[str, str], dict[str, Any]]:
        result: dict[tuple[str, str], dict[str, Any]] = {}
        try:
            rows = self.store.list_strategy_subscriptions(True)
        except Exception:
            rows = []
        for row in rows:
            symbol = str(row.get("symbol") or row.get("instrument_id") or "").strip().upper()
            strategy_id = str(row.get("strategy_id") or "").strip()
            if symbol and strategy_id in STRATEGIES and (not symbols or symbol in symbols):
                result[(symbol, strategy_id)] = dict(row)
        if result:
            return result
        # An explicit AI account can scan its authorized symbols even if the
        # older UI has not yet created strategy subscription rows.  Defaults
        # are still deterministic and all six registry strategies are visible
        # in the candidate audit.
        for symbol in symbols:
            for strategy_id in STRATEGIES:
                result[(symbol, strategy_id)] = {"symbol": symbol, "strategy_id": strategy_id, "params": {}}
        return result

    def _bars(self, symbol: str, timeframe: str, *, now: datetime, limit: int) -> list[dict[str, Any]]:
        try:
            rows = self.store.list_market_bars(symbol, timeframe, limit=limit)
        except Exception:
            return []
        result = []
        for raw in rows:
            row = dict(raw)
            bar_end = _utc(row.get("bar_end"))
            available = _utc(row.get("available_at") or row.get("data_as_of"))
            if (
                not row.get("is_closed")
                or bar_end is None
                or bar_end > now
                or available is None
                or available > now
                or str(row.get("quality_status") or "VALID").upper() == "LEGACY_UNVERIFIED"
            ):
                continue
            result.append(row)
        return result

    def has_pending_candidate(self, account_id: str, candidate_id: str) -> bool:
        with self.store._connect() as db:
            row = db.execute(
                """SELECT 1 FROM order_intents WHERE account_id=? AND candidate_id=?
                   AND status NOT IN ('FILLED','CANCELED','REJECTED','EXPIRED') LIMIT 1""",
                (account_id, candidate_id),
            ).fetchone()
        return row is not None

    def scan(
        self,
        *,
        account_id: str,
        provider: str,
        environment: str,
        symbols: tuple[str, ...],
        now: datetime | None = None,
        calibration_profile: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        point = _utc(now or self.clock()) or datetime.now(timezone.utc)
        bounded_symbols = tuple(dict.fromkeys(str(item).strip().upper() for item in symbols if str(item).strip()))[:5]
        subscriptions = self._subscriptions(bounded_symbols)
        output: list[dict[str, Any]] = []
        bar_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for (symbol, strategy_id), subscription in sorted(subscriptions.items()):
            strategy_cls = STRATEGIES[strategy_id]
            signal_timeframe = str(strategy_cls.signal_timeframe)
            cache_key = (symbol, signal_timeframe)
            signal_rows = bar_cache.setdefault(cache_key, self._bars(symbol, signal_timeframe, now=point, limit=240))
            signal_bars = [item for item in (_bar(row, timeframe=signal_timeframe) for row in signal_rows) if item is not None]
            target = signal_bars[-1] if signal_bars else None
            # The candidate identity is anchored to a closed 15m bar even for
            # the two 5m signal strategies.  A 5m signal can therefore create
            # at most one auditable candidate in the containing closed 15m
            # window, while the strategy's own signal timeframe remains in the
            # payload.
            if target is not None:
                target_15m = target.timestamp.replace(minute=(target.timestamp.minute // 15) * 15, second=0, microsecond=0)
                closed_15m_bar = _iso(target_15m + timedelta(minutes=15))
            else:
                closed_15m_bar = "UNKNOWN"
            candidate_key = f"{account_id}|{symbol}|{strategy_id}|{closed_15m_bar}"
            candidate_id = f"candidate_{hashlib.sha256(candidate_key.encode('utf-8')).hexdigest()[:28]}"
            market_type = "crypto" if symbol.endswith("USDT") else "equity"
            context: dict[str, Any] = {
                "timeframe": signal_timeframe,
                "signal_timeframe": signal_timeframe,
                "market_type": market_type,
                "params": subscription.get("params") or {},
                "evidence_refs": [f"market_bar:{symbol}:{signal_timeframe}:{closed_15m_bar}"],
            }
            for context_timeframe in strategy_cls.context_timeframes:
                rows = bar_cache.setdefault((symbol, context_timeframe), self._bars(symbol, context_timeframe, now=point, limit=240))
                bars = [item for item in (_bar(row, timeframe=context_timeframe) for row in rows) if item is not None]
                if context_timeframe == "5m":
                    context["closed_5m"] = bars
                elif context_timeframe == "1h":
                    context["hourly_closes"] = [item.close for item in bars]
                    context["closed_1h"] = bars
                elif context_timeframe == "8h":
                    context["funding_history"] = []
                    context["oi_history"] = []
                elif context_timeframe == "1d":
                    context["daily_bars"] = bars
            try:
                strategy = strategy_cls(subscription.get("params") or {})
                proposal = strategy.evaluate(symbol, signal_bars, now=point, context=context) if target is not None else None
                status = "PROPOSAL" if proposal is not None else str(strategy.last_status or "NO_TRIGGER")
                reason = strategy.last_reason or ("没有可用的闭合 15m K 线。" if target is None else "策略当前未触发。")
            except Exception as exc:
                proposal = None
                status = "ERROR"
                reason = f"{type(exc).__name__}: {exc}"[:320]
            payload: dict[str, Any] = {
                "candidate_id": candidate_id,
                "account_id": account_id,
                "provider": provider,
                "environment": str(environment).lower(),
                "symbol": symbol,
                "strategy_id": strategy_id,
                "strategy_version": getattr(strategy_cls, "version", "unknown"),
                "signal_timeframe": signal_timeframe,
                "closed_15m_bar": closed_15m_bar,
                "status": status,
                "reason": reason,
                "proposal": proposal.to_dict() if isinstance(proposal, TradeProposal) else None,
                "calibration_profile_id": (calibration_profile or {}).get("profile_id"),
                "scanned_at": _iso(point),
            }
            source_hash = _digest({"symbol": symbol, "strategy_id": strategy_id, "bar": asdict(target) if target else None, "status": status, "reason": reason})
            with self.store._connect() as db:
                db.execute(
                    """INSERT INTO ai_strategy_candidates(
                        candidate_id, account_id, provider, environment, symbol,
                        strategy_id, strategy_version, signal_timeframe,
                        closed_15m_bar, status, side, entry_price, stop_price,
                        take_profit, rule_score, calibrated_probability,
                        calibration_sample_size, rationale, source_hash,
                        context_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(candidate_id) DO UPDATE SET
                        status=excluded.status, side=excluded.side,
                        entry_price=excluded.entry_price, stop_price=excluded.stop_price,
                        take_profit=excluded.take_profit, rule_score=excluded.rule_score,
                        calibrated_probability=excluded.calibrated_probability,
                        calibration_sample_size=excluded.calibration_sample_size,
                        rationale=excluded.rationale, source_hash=excluded.source_hash,
                        context_json=excluded.context_json, updated_at=excluded.updated_at""",
                    (
                        candidate_id, account_id, str(provider or "unknown"), str(environment).lower(), symbol,
                        strategy_id, str(getattr(strategy_cls, "version", "unknown")), signal_timeframe,
                        closed_15m_bar, status, proposal.side if proposal else None,
                        proposal.entry if proposal else None, proposal.stop if proposal else None,
                        proposal.targets[0] if proposal else None, proposal.rule_score if proposal else None,
                        proposal.calibrated_probability if proposal else None,
                        proposal.calibration_sample_size if proposal else 0, reason, source_hash,
                        json.dumps({**payload, "context": {key: value for key, value in context.items() if key not in {"closed_5m", "closed_1h", "daily_bars"}}}, ensure_ascii=False, default=str, allow_nan=False),
                        _iso(point), _iso(point),
                    ),
                )
            payload["pending_order"] = self.has_pending_candidate(account_id, candidate_id)
            output.append(payload)
        return output


__all__ = ["CandidateScanner"]
