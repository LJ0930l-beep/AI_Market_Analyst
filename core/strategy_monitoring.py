"""V2 cycle plugged into the existing owned runtime and tray lifecycle."""

from datetime import datetime, timedelta, timezone

from .agent_execution import AgentDecisionService
from .instruments import canonical_instrument_key
from .market_intelligence import build_market_intelligence
from .monitoring import MonitoringRunResult, MonitoringService
from .providers.gateio_provider import GatePublicProvider
from .quant.strategies import STRATEGIES
from .trading.execution_gateway import ExecutionGateway


class StrategyMonitoringService(MonitoringService):
    strategy_mode = True

    def __init__(self, **kwargs):
        self.account_id = kwargs.pop("account_id", None)
        super().__init__(**kwargs)
        self.gate = GatePublicProvider()
        self._native_bootstrap_cache = {}
        self.provider_factory = lambda _instrument: self.gate
        # Strategy-driven orders share the same gateway and durable ledger as
        # manual and AI-led orders.  The legacy simulator remains available
        # only for the explicitly marked research compatibility fixtures.
        self.execution_gateway = ExecutionGateway(self.store)
        self.agent = AgentDecisionService(
            self.store,
            getattr(self.smart, "llm_provider", None),
            gateway=self.execution_gateway,
        )

    def refresh_ai_inputs(self, symbols):
        """Public evidence refresh owned by the AI cycle; no legacy decisions."""
        from .news_refresh import refresh_public_news
        result = self.run(symbols=symbols, analysis_only=True)
        refresh_public_news(self.store, symbols=symbols)
        # News/bootstrap may take time. Refresh executable quote and observed
        # depth last instead of aging the initial ticker during those reads.
        from .trading.autonomous_strategy import book_cost_evidence
        for symbol in symbols:
            try:
                quote = self.gate.get_quote(self.store.resolve_instrument(symbol))
                book = self.gate.order_book(symbol, limit=20)
                snapshot = dict(self.store.get_realtime_state(symbol) or {})
                snapshot.update(book_cost_evidence(book, quote.price))
                snapshot.update(price=quote.price, data_as_of=quote.timestamp.isoformat(), freshness_status="fresh")
                self.store.save_realtime_state(snapshot, now=datetime.now(timezone.utc))
            except Exception:
                # No cost defaults for a remote account. Missing depth is a
                # visible entry blocker, while Guardian remains independent.
                snapshot = dict(self.store.get_realtime_state(symbol) or {})
                snapshot.update(slippage=None, liquidity_ok=False, cost_evidence_status="UNAVAILABLE")
                if snapshot.get("symbol"):
                    self.store.save_realtime_state(snapshot, now=datetime.now(timezone.utc))
        return result

    def run(self, *, symbols=None, now=None, analysis_only=False):
        now = now or datetime.now(timezone.utc)
        subscriptions = self.store.list_strategy_subscriptions(True)
        symbols = set(symbols or [s["symbol"] for s in subscriptions])
        items = []
        for symbol in sorted(symbols)[: self.max_symbols]:
            if (
                getattr(self, "cancel_event", None) is not None
                and self.cancel_event.is_set()
            ):
                break
            try:
                active = [s for s in subscriptions if s["symbol"] == symbol]
                if not active and not analysis_only:
                    continue
                instrument = self.store.resolve_instrument(symbol)
                market = self.gate.market(symbol)
                quote = self.gate.get_quote(instrument)
                fetched = datetime.now(timezone.utc)
                native_symbol = str(market.get("id") or instrument.symbol).strip().upper()
                market_type = (
                    "perpetual"
                    if market.get("swap")
                    else str(market.get("type") or instrument.market_type)
                )
                settle_currency = str(
                    market.get("settle") or instrument.settle_currency
                ).strip().upper()
                identity = {
                    "venue": "gate",
                    "market_type": market_type,
                    "native_symbol": native_symbol,
                    "settle_currency": settle_currency,
                    "price_type": "last",
                    # The request symbol (BTCUSDT) and Gate's native contract
                    # id (BTC_USDT) are intentionally different.  Persist the
                    # same canonical identity that was validated by the
                    # provider instead of making SQLite reconstruct a key
                    # that cannot identify the requested symbol.
                    "instrument_key": canonical_instrument_key(
                        "gate",
                        market_type,
                        native_symbol,
                        settle_currency,
                        "last",
                    ),
                }
                bars_by_timeframe = {}

                native_bootstrap = None
                if getattr(self.gate, "uses_native_rest", False):
                    cached = self._native_bootstrap_cache.get(symbol)
                    cached_at = cached.get("fetched_at") if isinstance(cached, dict) else None
                    if isinstance(cached_at, datetime) and (fetched - cached_at).total_seconds() < 900:
                        native_bootstrap = cached.get("payload")
                    else:
                        # A native Gate bootstrap is the only production
                        # source for the institutional strategy path.  If it
                        # fails, mark data blocked instead of falling back to
                        # a short/synthetic CCXT history.
                        native_bootstrap = self.gate.bootstrap_symbol(
                            symbol,
                            instrument=instrument,
                            timeframes=("5m", "15m", "1h", "1d"),
                            min_15m=600,
                        )
                        self.store.save_gate_bootstrap(native_bootstrap, now=fetched)
                        self._native_bootstrap_cache[symbol] = {"fetched_at": fetched, "payload": native_bootstrap}

                if isinstance(native_bootstrap, dict):
                    bars_by_timeframe.update({str(key): list(value) for key, value in (native_bootstrap.get("bars") or {}).items() if isinstance(value, list)})

                def load_bars(timeframe: str, limit: int):
                    if timeframe not in bars_by_timeframe:
                        loaded = self.gate.get_bars(instrument, timeframe, limit)
                        bars_by_timeframe[timeframe] = loaded
                        self.store.upsert_market_bars(
                            symbol,
                            timeframe,
                            loaded,
                            provider=self.gate.provider_name,
                            data_as_of=quote.timestamp,
                            now=fetched,
                            **identity,
                        )
                    return bars_by_timeframe[timeframe]

                required_timeframes = {STRATEGIES[s["strategy_id"]].signal_timeframe for s in active}
                if analysis_only:
                    required_timeframes.update(("15m", "1h"))
                if any("1h" in STRATEGIES[s["strategy_id"]].context_timeframes for s in active):
                    required_timeframes.add("1h")
                if any("5m" in STRATEGIES[s["strategy_id"]].context_timeframes for s in active):
                    required_timeframes.add("5m")
                for timeframe in sorted(required_timeframes):
                    if analysis_only and timeframe in {"15m", "1h"}:
                        # Bootstrap history is cached for calibration, but the
                        # latest closed bars must be refreshed each AI cycle.
                        bars_by_timeframe.pop(timeframe, None)
                        load_bars(timeframe, 160)
                        continue
                    if isinstance(native_bootstrap, dict) and timeframe in bars_by_timeframe:
                        continue
                    load_bars(timeframe, 240 if timeframe in {"5m", "15m"} else 120)
                context_bars = bars_by_timeframe.get("1h", [])
                fresh = 0 <= (fetched - quote.timestamp).total_seconds() <= 120
                self.store.save_realtime_state(
                    {
                        "symbol": symbol,
                        "provider": self.gate.provider_name,
                        # Keep injected deterministic providers useful for
                        # isolated acceptance tests while production Gate
                        # always exposes its explicit LIVE_PUBLIC/TESTNET
                        # environment.  Do not infer a live environment from
                        # a fixture that has no routing metadata.
                        "environment": getattr(self.gate, "environment", "INJECTED_FIXTURE"),
                        "market_data_environment": getattr(
                            self.gate, "environment", "INJECTED_FIXTURE"
                        ),
                        "price": quote.price,
                        "change_pct": quote.change_pct,
                        "change_period": "24h",
                        "data_as_of": quote.timestamp.isoformat(),
                        "freshness_status": "fresh" if fresh else "stale",
                        "stale_after_seconds": 120,
                        "timestamp_basis": "provider_or_response_received",
                        "native_symbol": native_symbol,
                        "market": {
                            key: market.get(key)
                            for key in (
                                "id", "symbol", "base", "quote", "settle", "type", "swap",
                                "linear", "contractSize", "precision", "limits", "taker", "maker",
                                "mark_price", "index_price", "funding_rate", "funding_next_apply",
                            )
                            if market.get(key) is not None
                        },
                        "market_contract_evidence": {
                            "status": "OBSERVED_GATE_NATIVE_CONTRACT",
                            "source": market.get("source") or "gate_native_rest_contract",
                            "native_symbol": native_symbol,
                            "exchange_metadata": True,
                        },
                        "data_quality": {
                            "status": str((native_bootstrap or {}).get("quality", {}).get("status") or "AVAILABLE") if isinstance(native_bootstrap, dict) else "AVAILABLE",
                            "source": str((native_bootstrap or {}).get("quality", {}).get("source") or "gate_native_rest") if isinstance(native_bootstrap, dict) else "gate_ccxt_injected",
                            "closed_15m_bars": (native_bootstrap or {}).get("quality", {}).get("closed_15m_bars") if isinstance(native_bootstrap, dict) else None,
                            "mark_index_aligned": (native_bootstrap or {}).get("quality", {}).get("mark_index_aligned") if isinstance(native_bootstrap, dict) else None,
                            "synthetic": False,
                        },
                    },
                    now=fetched,
                )
                context_common = {
                    "market_type": instrument.asset_type.value,
                    "instrument": instrument,
                    "market": market,
                }
                funding_context = {}
                if any(s["strategy_id"] == "funding_extreme" for s in active):
                    try:
                        funding = (native_bootstrap or {}).get("funding") if isinstance(native_bootstrap, dict) else self.gate.funding_context(symbol)
                        if not isinstance(funding, dict):
                            raise ValueError("GATE_FUNDING_CONTEXT_UNAVAILABLE")
                        funding_context = {
                            "funding_history": [
                                {"timestamp": datetime.fromtimestamp(r["timestamp"] / 1000, timezone.utc), "funding_rate": r["fundingRate"], "unit": "decimal_fraction"}
                                for r in funding.get("history", [])
                                if r.get("timestamp") and r.get("fundingRate") is not None
                            ],
                            "oi_history": [
                                {"timestamp": datetime.fromtimestamp(r["timestamp"] / 1000, timezone.utc), "open_interest": r["openInterestAmount"], "unit": "contracts"}
                                for r in funding.get("open_interest_history", [])
                                if r.get("timestamp") and r.get("openInterestAmount") is not None
                            ],
                        }
                    except Exception:
                        # S6 must become UNSUPPORTED when its public context is
                        # unavailable; it must never receive zero-filled data.
                        funding_context = {}
                for sub in active:
                    # Re-check after network fetch: a disabled/deleted subscription cannot call the model.
                    if not any(
                        s["symbol"] == symbol and s["strategy_id"] == sub["strategy_id"]
                        for s in self.store.list_strategy_subscriptions(True)
                    ):
                        continue
                    strategy = STRATEGIES[sub["strategy_id"]](sub["params"])
                    signal_timeframe = strategy.signal_timeframe
                    signal_bars = bars_by_timeframe.get(signal_timeframe, [])
                    context = dict(context_common)
                    context.update({"timeframe": signal_timeframe, "signal_timeframe": signal_timeframe})
                    if strategy.strategy_id == "ema_trend":
                        context["hourly_closes"] = [bar.close for bar in context_bars]
                    if strategy.strategy_id == "liquidity_sweep":
                        context["closed_5m"] = bars_by_timeframe.get("5m", [])
                    if strategy.strategy_id == "funding_extreme":
                        context.update(funding_context)
                    if strategy.strategy_id == "session_vwap" and signal_bars:
                        session_open = signal_bars[0].timestamp.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                        session_close = session_open + timedelta(days=1)
                        context["session"] = {"source": "utc_continuous_crypto_session", "open": session_open, "close": session_close}
                        context["session_bars"] = [bar for bar in signal_bars if session_open <= bar.timestamp < session_close]
                    proposal = strategy.evaluate(symbol, signal_bars, now=fetched, context=context)
                    status = {
                        "symbol": symbol,
                        "strategy_id": sub["strategy_id"],
                        "status": strategy.last_status or "NO_TRIGGER",
                        "reason": strategy.last_reason,
                        "as_of": fetched.isoformat(),
                        "source": self.gate.provider_name,
                        "timeframe": signal_timeframe,
                        "bars": len(signal_bars),
                    }
                    if proposal and not analysis_only and not getattr(self, "ai_only", False):
                        facts = {
                            "freshness": "fresh" if fresh else "stale",
                            "source": self.gate.provider_name,
                            "as_of": quote.timestamp.isoformat(),
                            "price": quote.price,
                            "hourly_closes": [
                                b.close
                                for b in context_bars
                                if b.timestamp + timedelta(hours=1) <= fetched
                            ][-24:],
                            "news": build_market_intelligence(
                                self.store, as_of=fetched
                            )["news"],
                        }

                        def authorized():
                            return not getattr(self, "ai_only", False) and not (
                                getattr(self, "cancel_event", None) is not None
                                and self.cancel_event.is_set()
                            ) and any(
                                s["symbol"] == symbol
                                and s["strategy_id"] == sub["strategy_id"]
                                for s in self.store.list_strategy_subscriptions(True)
                            )

                        proposal_payload = proposal.to_dict()
                        if self.account_id:
                            proposal_payload["account_id"] = self.account_id
                        status = self.agent.decide(
                            proposal_payload, market, facts, now=fetched, authorized=authorized
                        )
                    self.store.set_scheduler_state(
                        "v2_strategy:" + symbol + ":" + sub["strategy_id"],
                        status,
                        updated_at=fetched.isoformat(),
                    )
                    items.append(status)
            except Exception as exc:
                items.append(
                    {
                        "symbol": symbol,
                        "status": "DEGRADED",
                        "error_code": type(exc).__name__,
                    }
                )
        return MonitoringRunResult(
            (
                "COMPLETED_WITH_ERRORS"
                if any(i["status"] == "DEGRADED" for i in items)
                else "COMPLETED"
            ),
            now,
            tuple(subscriptions),
            tuple(items),
            {"symbols": len(symbols), "max_symbols": self.max_symbols},
        )
