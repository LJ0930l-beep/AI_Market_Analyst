"""Closed-bar candidate scanner for the AI-led session.

This scanner evaluates the six registered strategies as a research signal
stage only.  It never calls an execution adapter and never creates an order.
The stable candidate key is the account, symbol, strategy, signal timeframe,
and closed signal-bar boundary, so retries cannot create another decision
target for the same bar.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Callable

from ..providers.base import Bar
from ..providers.gateio_provider import GatePublicProvider
from ..instruments import read_trading_bars
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


def _verified_gate_derivatives(
    facts: Any,
    *,
    expected_environment: str,
    symbol: str,
    now: datetime,
) -> dict[str, Any]:
    """Validate the Gate history envelope before exposing it to a strategy.

    The strategy may apply its own event-time freshness windows, but that is
    not a substitute for verifying who supplied the history and when the
    response was fetched. Unsupported adapters must fail closed here.
    """
    if not isinstance(facts, dict):
        raise ValueError("Gate derivatives response is not an object")
    if str(facts.get("provider") or "").strip().lower() != "gate":
        raise ValueError("Gate derivatives provider is not verified")
    if str(facts.get("environment") or "").strip().upper() != expected_environment:
        raise ValueError("Gate derivatives environment is not verified")
    if str(facts.get("data_status") or "").strip().upper() != "AVAILABLE":
        raise ValueError("Gate derivatives status is not available")
    source = str(facts.get("source") or "").strip().lower()
    if source not in {"gate_native_rest_derivatives_history", "gate_public_swap"}:
        raise ValueError("Gate derivatives source is not supported")

    as_of = _utc(facts.get("data_as_of"))
    if as_of is None:
        raise ValueError("Gate derivatives response has no verifiable fetch time")
    age = now - as_of
    if age < timedelta(seconds=-30) or age > timedelta(minutes=5):
        raise ValueError("Gate derivatives response is stale or future-dated")

    native_symbol = str(facts.get("native_symbol") or "").strip().upper()
    expected_native_symbol = f"{symbol[:-4]}_USDT" if symbol.upper().endswith("USDT") else ""
    if native_symbol and expected_native_symbol and native_symbol != expected_native_symbol:
        raise ValueError("Gate derivatives symbol does not match candidate")

    funding = facts.get("funding_history")
    oi = facts.get("oi_history")
    if not isinstance(funding, list) or not isinstance(oi, list):
        raise ValueError("Gate derivatives history shape is invalid")
    return {
        "funding_history": funding,
        "oi_history": oi,
        "derivatives_source": source,
        "derivatives_environment": expected_environment,
        "derivatives_as_of": _iso(as_of),
    }


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
    def __init__(
        self,
        store: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        derivatives_provider_factory: Callable[[bool], Any] | None = None,
    ) -> None:
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.derivatives_provider_factory = derivatives_provider_factory or (
            lambda testnet: GatePublicProvider(testnet=testnet)
        )
        with store._connect() as db:
            ensure_institutional_trader_schema(db)

    def _subscriptions(
        self,
        symbols: tuple[str, ...],
        strategy_ids: tuple[str, ...] | list[str] | None = None,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        result: dict[tuple[str, str], dict[str, Any]] = {}
        allowed_strategies = {
            str(item).strip()
            for item in (strategy_ids or STRATEGIES)
            if str(item).strip() in STRATEGIES
        }
        if not allowed_strategies:
            allowed_strategies = set(STRATEGIES)
        try:
            rows = self.store.list_strategy_subscriptions(True)
        except Exception:
            rows = []
        for row in rows:
            symbol = str(row.get("symbol") or row.get("instrument_id") or "").strip().upper()
            strategy_id = str(row.get("strategy_id") or "").strip()
            if symbol and strategy_id in allowed_strategies and (not symbols or symbol in symbols):
                result[(symbol, strategy_id)] = dict(row)
        for symbol in symbols:
            if strategy_ids is not None:
                # An autonomous AI profile explicitly owns its candidate
                # strategy set. Watchlist monitoring subscriptions may supply
                # parameters for matching rules, but must not silently remove
                # the rest of the active profile's evidence.
                for strategy_id in sorted(allowed_strategies):
                    result.setdefault(
                        (symbol, strategy_id),
                        {"symbol": symbol, "strategy_id": strategy_id, "params": {}},
                    )
            elif not any(s == symbol for s, _ in result):
                for strategy_id in sorted(allowed_strategies):
                    result[(symbol, strategy_id)] = {"symbol": symbol, "strategy_id": strategy_id, "params": {}}
        return result

    def _bars(self, symbol: str, timeframe: str, *, now: datetime, limit: int) -> list[dict[str, Any]]:
        # Restricted to the traded-price identity on purpose.  An unfiltered
        # read returns the same bar once per identity (last/mark/index/legacy),
        # and ``BaseStrategy.evaluate`` treats a repeated timestamp as
        # insufficient history -- which left every strategy permanently
        # WARMING_UP and produced no candidate at all.
        try:
            rows = read_trading_bars(self.store, symbol, timeframe, limit=limit)
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

    @staticmethod
    def _human_conditions(
        *,
        proposal: TradeProposal | None,
        status: str,
        reason: str,
        strategy_id: str,
        indicators: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """Expose observed rule facts without inventing thresholds.

        Strategy implementations own their exact thresholds.  The scanner
        therefore records the actual indicator values and the strategy's
        trigger result, while leaving a threshold blank when that threshold
        is not part of the public proposal contract.
        """

        values = indicators if isinstance(indicators, dict) else {}
        conditions: list[dict[str, Any]] = [
            {
                "name": f"{strategy_id} 规则触发",
                "current_value": "PROPOSAL" if proposal is not None else status,
                "required_value": "PROPOSAL",
                "passed": proposal is not None,
                "evidence": reason[:240],
            }
        ]
        for key in sorted(values):
            value = values.get(key)
            if isinstance(value, (dict, list, tuple)):
                continue
            conditions.append(
                {
                    "name": f"指标 {key}",
                    "current_value": value,
                    "required_value": None,
                    "passed": True if proposal is not None else None,
                    "evidence": "策略实际输出的指标快照；阈值由该策略实现解释。",
                }
            )
        return conditions[:32]

    @classmethod
    def _candidate_contract(
        cls,
        *,
        proposal: TradeProposal | None,
        status: str,
        reason: str,
        strategy_id: str,
        strategy_cls: Any,
        context: dict[str, Any],
        closed_15m_bar: str,
    ) -> dict[str, Any]:
        proposal_dict = proposal.to_dict() if isinstance(proposal, TradeProposal) else {}
        indicators = proposal_dict.get("indicators") if proposal_dict else {}
        conditions = cls._human_conditions(
            proposal=proposal,
            status=status,
            reason=reason,
            strategy_id=strategy_id,
            indicators=indicators,
        )
        evidence = list(dict.fromkeys(
            [str(item) for item in (proposal_dict.get("evidence_refs") or []) if str(item).strip()]
            + [f"market_bar:{context.get('symbol') or ''}:{context.get('signal_timeframe') or getattr(strategy_cls, 'signal_timeframe', '')}:{closed_15m_bar}"]
        ))
        entry_zone = None
        invalidation = None
        targets: list[dict[str, Any]] = []
        if proposal is not None:
            # The strategy emits an exact trigger, not a discretionary range.
            # Keep the degenerate range explicit instead of fabricating a
            # tolerance around the entry.
            entry_zone = {
                "low": proposal.entry,
                "high": proposal.entry,
                "basis": "EXACT_STRATEGY_TRIGGER",
            }
            invalidation = f"{proposal.side}：价格触及硬止损 {proposal.stop} 即失效。"
            targets = [
                {"price": price, "fraction": fraction, "label": f"TP{index}"}
                for index, (price, fraction) in enumerate(zip(proposal.targets, proposal.target_fractions), start=1)
            ]
        completion = None
        if proposal is not None:
            try:
                completion = max(0.0, min(100.0, float(proposal.rule_score) * 100.0))
            except (TypeError, ValueError):
                completion = None
        signal_time = proposal.generated_at if proposal is not None else None
        expires_at = proposal.expires_at if proposal is not None else None
        context_timeframes = list(context.get("context_timeframes") or getattr(strategy_cls, "context_timeframes", ()) or ())
        context_timeframes = [str(item) for item in context_timeframes]
        market_regime = (
            context.get("market_regime")
            or (indicators or {}).get("market_regime")
            or "UNKNOWN"
        )
        return {
            "conditions": conditions,
            "trigger_completion_pct": completion,
            "entry_zone": entry_zone,
            "invalidation": invalidation,
            "targets": targets,
            "rr": proposal.risk_reward_ratio if proposal is not None else None,
            "evidence_refs": evidence[:32],
            "signal_time": signal_time,
            "expires_at": expires_at,
            "context_timeframe": {
                "signal": str(context.get("signal_timeframe") or getattr(strategy_cls, "signal_timeframe", "UNKNOWN")),
                "context": context_timeframes,
            },
            "market_regime": str(market_regime),
            "direction_bias": proposal.side if proposal is not None else "NEUTRAL",
            "trigger_status": status,
        }

    def scan(
        self,
        *,
        account_id: str,
        provider: str,
        environment: str,
        symbols: tuple[str, ...],
        now: datetime | None = None,
        calibration_profile: dict[str, Any] | None = None,
        strategy_ids: tuple[str, ...] | list[str] | None = None,
        signal_timeframe_override: str | None = None,
        context_timeframes_override: tuple[str, ...] | list[str] | None = None,
    ) -> list[dict[str, Any]]:
        point = _utc(now or self.clock()) or datetime.now(timezone.utc)
        bounded_symbols = tuple(dict.fromkeys(str(item).strip().upper() for item in symbols if str(item).strip()))[:5]
        subscriptions = self._subscriptions(bounded_symbols, strategy_ids)
        output: list[dict[str, Any]] = []
        bar_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
        derivative_context_cache: dict[str, dict[str, Any]] = {}
        derivative_provider: Any | None = None
        derivative_provider_environment: str | None = None
        for (symbol, strategy_id), subscription in sorted(subscriptions.items()):
            strategy_cls = STRATEGIES[strategy_id]
            signal_timeframe = str(signal_timeframe_override or strategy_cls.signal_timeframe).lower()
            if signal_timeframe not in {"5m", "15m"}:
                signal_timeframe = str(strategy_cls.signal_timeframe)
            cache_key = (symbol, signal_timeframe)
            signal_rows = bar_cache.setdefault(cache_key, self._bars(symbol, signal_timeframe, now=point, limit=240))
            signal_bars = [item for item in (_bar(row, timeframe=signal_timeframe) for row in signal_rows) if item is not None]
            target = signal_bars[-1] if signal_bars else None
            # Anchor the identity to the strategy's actual signal bar.  A 5m
            # strategy must be allowed to produce a new auditable candidate on
            # every closed 5m bar rather than reusing one 15m identity.
            if target is not None:
                closed_signal_bar = _iso(target.bar_end)
            else:
                closed_signal_bar = "UNKNOWN"
            candidate_key = f"{account_id}|{symbol}|{strategy_id}|{signal_timeframe}|{closed_signal_bar}"
            candidate_id = f"candidate_{hashlib.sha256(candidate_key.encode('utf-8')).hexdigest()[:28]}"
            # Older releases keyed the same unique database tuple without the
            # timeframe component.  Reuse that durable id when it already
            # exists so a profile upgrade from 15m to 5m cannot collide with
            # the legacy UNIQUE(account, symbol, strategy, closed_bar) index.
            with self.store._connect() as db:
                existing = db.execute(
                    """SELECT candidate_id FROM ai_strategy_candidates
                       WHERE account_id=? AND symbol=? AND strategy_id=?
                         AND signal_timeframe=? AND closed_15m_bar=? LIMIT 1""",
                    (account_id, symbol, strategy_id, signal_timeframe, closed_signal_bar),
                ).fetchone()
            if existing is not None:
                candidate_id = str(existing[0])
            market_type = "crypto" if symbol.endswith("USDT") else "equity"
            context: dict[str, Any] = {
                "timeframe": signal_timeframe,
                "signal_timeframe": signal_timeframe,
                "market_type": market_type,
                "params": subscription.get("params") or {},
                "evidence_refs": [f"market_bar:{symbol}:{signal_timeframe}:{closed_signal_bar}"],
            }
            # LiquiditySweep consumes closed 5m evidence. When 5m is itself
            # the signal timeframe, reuse the exact already-filtered series
            # rather than reading or appending a second copy of those bars.
            # This keeps its closed-bar and availability checks identical to
            # the signal series and leaves context_timeframes as secondary
            # frames only.
            if signal_timeframe == "5m":
                context["closed_5m"] = signal_bars
            context_timeframes = tuple(
                dict.fromkeys(
                    str(item).lower()
                    for item in (context_timeframes_override or strategy_cls.context_timeframes)
                    if str(item).lower() in {"5m", "15m", "1h", "8h", "1d"}
                    and str(item).lower() != signal_timeframe
                )
            )
            context["context_timeframes"] = list(context_timeframes)
            if strategy_id == "funding_extreme":
                if symbol not in derivative_context_cache:
                    try:
                        if str(provider or "").strip().lower() != "gate":
                            raise ValueError("funding/OI history is only configured for Gate")
                        testnet = str(environment or "").strip().lower() in {"testnet", "testnet_public"}
                        expected_environment = "TESTNET_PUBLIC" if testnet else "LIVE_PUBLIC"
                        if derivative_provider is None or derivative_provider_environment != expected_environment:
                            derivative_provider = self.derivatives_provider_factory(testnet)
                            derivative_provider_environment = expected_environment
                        facts = derivative_provider.derivatives_history(symbol)
                        derivative_context_cache[symbol] = _verified_gate_derivatives(
                            facts,
                            expected_environment=expected_environment,
                            symbol=symbol,
                            now=point,
                        )
                    except Exception as exc:
                        derivative_context_cache[symbol] = {
                            "funding_history": [],
                            "oi_history": [],
                            "derivatives_source": "UNAVAILABLE",
                            "derivatives_environment": "UNKNOWN",
                            "derivatives_error": str(exc)[:160],
                        }
                context.update(derivative_context_cache[symbol])
            for context_timeframe in context_timeframes:
                rows = bar_cache.setdefault((symbol, context_timeframe), self._bars(symbol, context_timeframe, now=point, limit=240))
                bars = [item for item in (_bar(row, timeframe=context_timeframe) for row in rows) if item is not None]
                if context_timeframe == "5m":
                    context["closed_5m"] = bars
                elif context_timeframe == "1h":
                    context["hourly_closes"] = [item.close for item in bars]
                    context["closed_1h"] = bars
                elif context_timeframe == "1d":
                    context["daily_bars"] = bars
            try:
                strategy = strategy_cls(subscription.get("params") or {})
                # The active AI strategy profile owns the signal cadence for
                # this scan. Strategy classes provide defaults, but their
                # evaluate() contract uses the instance timeframe for bar
                # validation, expiry, ATR geometry, and the emitted proposal.
                # Without this override a valid 5m profile reads 5m bars and
                # then rejects every registered 15m-default rule as
                # UNSUPPORTED before evaluating its actual setup.
                strategy.signal_timeframe = signal_timeframe
                proposal = strategy.evaluate(symbol, signal_bars, now=point, context=context) if target is not None else None
                status = "PROPOSAL" if proposal is not None else str(strategy.last_status or "NO_TRIGGER")
                reason = strategy.last_reason or (f"没有可用的闭合 {signal_timeframe} K 线。" if target is None else "策略当前未触发。")
                if (
                    target is not None
                    and strategy_id == "funding_extreme"
                    and context.get("derivatives_source") == "UNAVAILABLE"
                ):
                    status = "UNSUPPORTED"
                    reason = "Gate funding/OI history unavailable: " + str(
                        context.get("derivatives_error") or "source or environment could not be verified"
                    )
            except Exception as exc:
                proposal = None
                status = "ERROR"
                reason = f"{type(exc).__name__}: {exc}"[:320]
            contract = self._candidate_contract(
                proposal=proposal,
                status=status,
                reason=reason,
                strategy_id=strategy_id,
                strategy_cls=strategy_cls,
                context={**context, "symbol": symbol, "signal_timeframe": signal_timeframe},
                closed_15m_bar=closed_signal_bar,
            )
            payload: dict[str, Any] = {
                "candidate_id": candidate_id,
                "account_id": account_id,
                "provider": provider,
                "environment": str(environment).lower(),
                "symbol": symbol,
                "strategy_id": strategy_id,
                "strategy_version": getattr(strategy_cls, "version", "unknown"),
                "signal_timeframe": signal_timeframe,
                # ``closed_15m_bar`` is retained for the existing database/API
                # contract; ``closed_signal_bar`` states its true semantics.
                "closed_15m_bar": closed_signal_bar,
                "closed_signal_bar": closed_signal_bar,
                "status": status,
                "reason": reason,
                "proposal": proposal.to_dict() if isinstance(proposal, TradeProposal) else None,
                **contract,
                "calibration_profile_id": (calibration_profile or {}).get("profile_id"),
                "scanned_at": _iso(point),
            }
            source_hash = _digest({"symbol": symbol, "strategy_id": strategy_id, "bar": asdict(target) if target else None, "status": status, "reason": reason, "proposal": payload.get("proposal"), "contract": contract})
            with self.store._connect() as db:
                db.execute(
                    """INSERT INTO ai_strategy_candidates(
                        candidate_id, account_id, provider, environment, symbol,
                        strategy_id, strategy_version, signal_timeframe,
                        closed_15m_bar, status, side, entry_price, stop_price,
                        take_profit, rule_score, calibrated_probability,
                        calibration_sample_size, rationale, source_hash,
                        conditions_json, trigger_completion_pct, entry_zone_json,
                        invalidation, targets_json, rr, evidence_json, signal_time,
                        expires_at, context_timeframe, market_regime, direction_bias,
                        trigger_status,
                        context_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(candidate_id) DO UPDATE SET
                        status=excluded.status, side=excluded.side,
                        entry_price=excluded.entry_price, stop_price=excluded.stop_price,
                        take_profit=excluded.take_profit, rule_score=excluded.rule_score,
                        calibrated_probability=excluded.calibrated_probability,
                        calibration_sample_size=excluded.calibration_sample_size,
                        rationale=excluded.rationale, source_hash=excluded.source_hash,
                        conditions_json=excluded.conditions_json,
                        trigger_completion_pct=excluded.trigger_completion_pct,
                        entry_zone_json=excluded.entry_zone_json,
                        invalidation=excluded.invalidation,
                        targets_json=excluded.targets_json,
                        rr=excluded.rr,
                        evidence_json=excluded.evidence_json,
                        signal_time=excluded.signal_time,
                        expires_at=excluded.expires_at,
                        context_timeframe=excluded.context_timeframe,
                        market_regime=excluded.market_regime,
                        direction_bias=excluded.direction_bias,
                        trigger_status=excluded.trigger_status,
                        context_json=excluded.context_json, updated_at=excluded.updated_at""",
                    (
                        candidate_id, account_id, str(provider or "unknown"), str(environment).lower(), symbol,
                        strategy_id, str(getattr(strategy_cls, "version", "unknown")), signal_timeframe,
                        closed_signal_bar, status, proposal.side if proposal else None,
                        proposal.entry if proposal else None, proposal.stop if proposal else None,
                        proposal.targets[0] if proposal else None, proposal.rule_score if proposal else None,
                        proposal.calibrated_probability if proposal else None,
                        proposal.calibration_sample_size if proposal else 0, reason, source_hash,
                        json.dumps(contract["conditions"], ensure_ascii=False, allow_nan=False),
                        contract["trigger_completion_pct"],
                        json.dumps(contract["entry_zone"], ensure_ascii=False, allow_nan=False) if contract["entry_zone"] is not None else None,
                        contract["invalidation"],
                        json.dumps(contract["targets"], ensure_ascii=False, allow_nan=False),
                        contract["rr"],
                        json.dumps(contract["evidence_refs"], ensure_ascii=False, allow_nan=False),
                        contract["signal_time"], contract["expires_at"],
                        json.dumps(contract["context_timeframe"], ensure_ascii=False, allow_nan=False),
                        contract["market_regime"], contract["direction_bias"], contract["trigger_status"],
                        json.dumps({**payload, "context": {key: value for key, value in context.items() if key not in {"closed_5m", "closed_1h", "daily_bars"}}}, ensure_ascii=False, default=str, allow_nan=False),
                        _iso(point), _iso(point),
                    ),
                )
            payload["pending_order"] = self.has_pending_candidate(account_id, candidate_id)
            output.append(payload)
        return output


__all__ = ["CandidateScanner"]
