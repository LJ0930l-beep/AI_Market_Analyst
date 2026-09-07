from datetime import datetime, timedelta, timezone
import pytest
from core.agent_execution import (
    AgentDecisionService,
    MacroGuard,
    SimulationEngine,
    risk_plan,
)
from core.quant.strategies import (
    EMATrend,
    BollingerSqueeze,
    LiquiditySweep,
    FundingExtreme,
    TradeProposal,
)
from core.providers.base import Bar
from core.storage import SQLiteStore
from core.instruments import instrument_for

NOW = datetime.now(timezone.utc)
MARKET = {
    "active": True,
    "linear": True,
    "settle": "USDT",
    "contractSize": 0.01,
    "precision": {"amount": 1, "price": 0.1},
    "limits": {"amount": {"min": 1, "max": 1000000}},
}


def proposal():
    return TradeProposal(
        "ema_trend",
        "2.0.0",
        "BTCUSDT",
        "LONG",
        100,
        95,
        (110, 115),
        (0.5, 0.5),
        NOW.isoformat(),
        (NOW + timedelta(minutes=15)).isoformat(),
        (NOW - timedelta(minutes=15)).isoformat(),
        "test rules",
    )


@pytest.fixture
def store(tmp_path):
    s = SQLiteStore(tmp_path / "v2.db")
    s.initialize()
    return s


def test_subscription_default_delete_and_migration(store):
    store.save_instrument(instrument_for("BTCUSDT"))
    store.upsert_watchlist_entry("BTCUSDT")
    store.set_strategy_subscription("BTCUSDT", "ema_trend", False, {})
    assert not store.list_strategy_subscriptions(True)
    store.set_strategy_subscription("BTCUSDT", "ema_trend", True, {})
    assert len(store.list_strategy_subscriptions(True)) == 1
    store.delete_watchlist_entry("BTCUSDT")
    store.upsert_watchlist_entry("BTCUSDT")
    assert not store.list_strategy_subscriptions(True)
    store.initialize()
    assert store.schema_version() == 14
    with pytest.raises(ValueError):
        store.set_strategy_subscription(
            "BTCUSDT", "ema_trend", True, {"volume_ratio": float("nan")}
        )


def test_risk_and_atomic_protection_partial_timeout(store):
    p = proposal().to_dict()
    plan = risk_plan(p, MARKET, now=NOW)
    assert plan["risk_amount"] <= 100
    sim = SimulationEngine(store)
    assert sim.open("fail", p, plan, fault="protection")["filled_contracts"] == 0
    assert (
        sim.open("partial", p, plan, fault="partial")["filled_contracts"]
        == plan["contracts"] * 0.5
    )
    with pytest.raises(TimeoutError):
        sim.open("lost", p, plan, fault="timeout")
    assert sim.open("lost", p, plan)["protected"]
    assert len(store.v2_records("simulated_positions")) == 3
    with pytest.raises(ValueError):
        risk_plan(p, MARKET, now=NOW + timedelta(hours=1))
    with pytest.raises(ValueError):
        risk_plan(p, MARKET, now=NOW, risk_fraction=0.021)
    with pytest.raises(ValueError):
        risk_plan(p, MARKET, now=NOW, exposure=20000)


def test_stop_first_no_lookahead_and_duplicate_settlement(store):
    p = proposal().to_dict()
    plan = risk_plan(p, MARKET, now=NOW)
    sim = SimulationEngine(store)
    sim.open("both", p, plan)
    sim.advance("BTCUSDT", Bar(NOW - timedelta(hours=1), 100, 116, 94, 100, 10))
    assert store.v2_records("simulated_positions")[0]["status"] == "OPEN"
    bar = Bar(NOW + timedelta(minutes=15), 100, 116, 94, 100, 10)
    sim.advance("BTCUSDT", bar)
    sim.advance("BTCUSDT", bar)
    assert store.v2_records("simulated_positions")[0]["status"] == "CLOSED"
    assert len(store.v2_records("simulation_events")) == 2
    assert store.v2_records("simulation_events")[0]["type"] == "STOP"


def test_macro_expiry_missing_and_direction():
    assert MacroGuard([]).check("LONG", NOW) == "MACRO_UNAVAILABLE"
    event = {
        "source_url": "https://example.test/unit-test-only",
        "actual": "2",
        "forecast": "1",
        "known_at": NOW.isoformat(),
        "directive_expires_at": (NOW + timedelta(hours=1)).isoformat(),
        "directive": "FORBID_LONG",
    }
    assert MacroGuard([event]).check("LONG", NOW) == "BLOCKED_BY_MACRO_CIRCUIT_BREAKER"
    assert MacroGuard([event]).check("SHORT", NOW) is None
    assert (
        MacroGuard([event]).check("SHORT", NOW + timedelta(hours=2))
        == "MACRO_UNAVAILABLE"
    )


def test_model_invalid_unavailable_and_deduplication(store):
    class Invalid:
        def generate_json(self, *_, **__):
            return {"decision": "EXECUTE_TRADE"}

    service = AgentDecisionService(store, Invalid())
    assert (
        service.decide(proposal(), MARKET, {"freshness": "fresh"}, now=NOW)["reason"]
        == "INVALID_MODEL_JSON"
    )
    assert (
        service.decide(proposal(), MARKET, {"freshness": "fresh"}, now=NOW)["status"]
        == "DUPLICATE"
    )
    assert not store.v2_records("simulated_positions")


def test_strategy_no_trigger_and_future_bars():
    now = NOW.replace(minute=0, second=0, microsecond=0)
    bars = [
        Bar(now - timedelta(minutes=15 * (80 - i)), 100, 101, 99, 100, 100)
        for i in range(80)
    ]
    for strategy in (
        EMATrend(),
        BollingerSqueeze(),
        LiquiditySweep(),
        FundingExtreme(),
    ):
        assert strategy.evaluate("BTCUSDT", bars, now=now) is None
        assert (
            strategy.evaluate(
                "BTCUSDT", bars + [Bar(now, 100, 200, 50, 180, 10000)], now=now
            )
            is None
        )
    bars[-1] = Bar(bars[-1].timestamp, 100, 106, 99, 105, 200)
    assert EMATrend().evaluate("BTCUSDT", bars, now=now).side == "LONG"
    assert BollingerSqueeze().evaluate("BTCUSDT", bars, now=now).side == "LONG"
    context = {
        "funding_history": [
            (now - timedelta(hours=8 * (22 - i)), 0.0001) for i in range(22)
        ]
        + [(now, 0.001)],
        "oi_history": [(now - timedelta(hours=1), 100), (now, 110)],
    }
    assert (
        FundingExtreme().evaluate("BTCUSDT", bars, now=now, context=context).side
        == "SHORT"
    )


def test_liquidity_positive_and_missing_confirmation():
    now = NOW.replace(minute=0, second=0, microsecond=0)
    bars = [
        Bar(now - timedelta(minutes=15 * (80 - i)), 100, 101, 99, 100, 100)
        for i in range(80)
    ]
    bars[-1] = Bar(bars[-1].timestamp, 100, 101, 96, 100.5, 200)
    confirmation = [
        Bar(now - timedelta(minutes=10), 100, 100.1, 98, 99, 50),
        Bar(now - timedelta(minutes=5), 99, 101, 98.5, 100.5, 100),
    ]
    s = LiquiditySweep()
    assert (
        s.evaluate("BTCUSDT", bars, now=now, context={"closed_5m": confirmation}).side
        == "LONG"
    )
    assert (
        s.evaluate("BTCUSDT", bars, now=now, context={"closed_5m": confirmation[:1]})
        is None
    )


def test_simulation_approved_model_permission_and_revocation(store):
    class Approve:
        def generate_json(self, *_, **__):
            return {
                "decision": "EXECUTE_TRADE",
                "summary": "Unit-test-only approval",
                "counterevidence": [],
            }

    store.upsert_app_setting("simulation.allow_unknown_macro", True)
    service = AgentDecisionService(store, Approve())
    result = service.decide(
        proposal(), MARKET, {"freshness": "fresh", "as_of": NOW.isoformat()}, now=NOW
    )
    assert result["status"] == "SIMULATED"
    assert result["execution"]["protected"]
    assert store.list_alerts(limit=10)[0]["symbol"] == "BTCUSDT"


def test_runtime_default_no_scan_and_subscription_membership(store):
    from core.strategy_monitoring import StrategyMonitoringService
    from core.monitoring_runtime import MonitoringRuntime

    service = StrategyMonitoringService(store=store, llm_provider=None)
    runtime = MonitoringRuntime(store=store, service=service)
    assert not runtime.status()["active"]
    assert runtime._enabled_symbols() == ()
    store.save_instrument(instrument_for("BTCUSDT"))
    store.upsert_watchlist_entry("BTCUSDT")
    store.set_strategy_subscription("BTCUSDT", "ema_trend", True, {})
    assert runtime._enabled_symbols() == ("BTCUSDT",)
    store.delete_watchlist_entry("BTCUSDT")
    assert runtime._enabled_symbols() == ()
