"""V2 cycle plugged into the existing owned runtime and tray lifecycle."""

from datetime import datetime, timedelta, timezone

from .agent_execution import AgentDecisionService, SimulationEngine
from .market_intelligence import build_market_intelligence
from .monitoring import MonitoringRunResult, MonitoringService
from .providers.gateio_provider import GatePublicProvider
from .quant.strategies import STRATEGIES


class StrategyMonitoringService(MonitoringService):
    strategy_mode = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.gate = GatePublicProvider()
        self.provider_factory = lambda _instrument: self.gate
        self.agent = AgentDecisionService(self.store, kwargs.get("llm_provider"))

    def run(self, *, symbols=None, now=None):
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
                if not active:
                    continue
                instrument = self.store.resolve_instrument(symbol)
                market = self.gate.market(symbol)
                bars = self.gate.get_bars(instrument, "15m", 240)
                context_bars = self.gate.get_bars(instrument, "1h", 120)
                quote = self.gate.get_quote(instrument)
                fetched = datetime.now(timezone.utc)
                self.store.upsert_market_bars(
                    symbol,
                    "15m",
                    bars,
                    provider=self.gate.provider_name,
                    data_as_of=quote.timestamp,
                    now=fetched,
                )
                self.store.upsert_market_bars(
                    symbol,
                    "1h",
                    context_bars,
                    provider=self.gate.provider_name,
                    data_as_of=quote.timestamp,
                    now=fetched,
                )
                fresh = 0 <= (fetched - quote.timestamp).total_seconds() <= 120
                self.store.save_realtime_state(
                    {
                        "symbol": symbol,
                        "provider": self.gate.provider_name,
                        "price": quote.price,
                        "change_pct": quote.change_pct,
                        "change_period": "24h",
                        "data_as_of": quote.timestamp.isoformat(),
                        "freshness_status": "fresh" if fresh else "stale",
                        "stale_after_seconds": 120,
                        "timestamp_basis": "provider_or_response_received",
                    },
                    now=fetched,
                )
                closed = [
                    b for b in bars if b.timestamp + timedelta(minutes=15) <= fetched
                ]
                for b in closed:
                    SimulationEngine(self.store).advance(symbol, b)
                context = {}
                if any(s["strategy_id"] == "liquidity_sweep" for s in active):
                    context["closed_5m"] = self.gate.get_bars(instrument, "5m", 60)
                if any(s["strategy_id"] == "funding_extreme" for s in active):
                    funding = self.gate.funding_context(symbol)
                    context["funding_history"] = [
                        (
                            datetime.fromtimestamp(r["timestamp"] / 1000, timezone.utc),
                            r["fundingRate"],
                        )
                        for r in funding["history"]
                        if r.get("timestamp") and r.get("fundingRate") is not None
                    ]
                    context["oi_history"] = [
                        (
                            datetime.fromtimestamp(r["timestamp"] / 1000, timezone.utc),
                            r["openInterestAmount"],
                        )
                        for r in funding["open_interest_history"]
                        if r.get("timestamp")
                        and r.get("openInterestAmount") is not None
                    ]
                for sub in active:
                    # Re-check after network fetch: a disabled/deleted subscription cannot call the model.
                    if not any(
                        s["symbol"] == symbol and s["strategy_id"] == sub["strategy_id"]
                        for s in self.store.list_strategy_subscriptions(True)
                    ):
                        continue
                    proposal = STRATEGIES[sub["strategy_id"]](sub["params"]).evaluate(
                        symbol, bars, now=fetched, context=context
                    )
                    status = {
                        "symbol": symbol,
                        "strategy_id": sub["strategy_id"],
                        "status": "NO_TRIGGER",
                        "as_of": fetched.isoformat(),
                        "source": self.gate.provider_name,
                        "bars": len(bars),
                    }
                    if proposal:
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
                            return not (
                                getattr(self, "cancel_event", None) is not None
                                and self.cancel_event.is_set()
                            ) and any(
                                s["symbol"] == symbol
                                and s["strategy_id"] == sub["strategy_id"]
                                for s in self.store.list_strategy_subscriptions(True)
                            )

                        status = self.agent.decide(
                            proposal, market, facts, now=fetched, authorized=authorized
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
