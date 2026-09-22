"""get_market_metadata must carry the venue's leverage ceiling through.

The gateway reads it via venue_leverage_limit() and fail-closes without it, so
dropping limits.leverage made every manual open impossible even though Gate
reports leverage_max natively.
"""

from core.trading.gate_live_client import GateLiveTrader
from core.trading.strategy_execution import venue_leverage_limit

GATE_MARKET = {
    "id": "BTC_USDT",
    "symbol": "BTC/USDT:USDT",
    "swap": True,
    "linear": True,
    "settle": "USDT",
    "contractSize": 0.0001,
    "precision": {"amount": 1.0, "price": 0.1},
    "taker": 0.0005,
    "maker": 0.0002,
    "active": True,
    "limits": {
        "amount": {"min": 1.0, "max": 1000000.0},
        "leverage": {"min": 1.0, "max": 125.0},
        "price": {"min": 84819.0, "max": 88281.0},
    },
    "info": {"order_size_min": "1", "order_size_max": "1000000", "leverage_min": "1", "leverage_max": "125"},
}


class StubExchange:
    def load_markets(self):
        return {"BTC/USDT:USDT": GATE_MARKET}


class StubTrader(GateLiveTrader):
    def __init__(self):
        pass

    def _get_exchange(self):
        return StubExchange()

    def _exchange_symbol(self, _exchange, symbol):
        return "BTC/USDT:USDT"


def test_metadata_exposes_the_leverage_ceiling_the_gateway_requires():
    metadata = StubTrader().get_market_metadata("BTCUSDT")

    assert metadata["leverage_max"] == 125.0
    assert metadata["limits"]["leverage"] == {"min": 1.0, "max": 125.0}
    # The exact accessor the execution gateway calls before sizing an open.
    assert venue_leverage_limit(metadata) == 125


def test_amount_and_price_sizing_facts_survive_alongside_it():
    metadata = StubTrader().get_market_metadata("BTCUSDT")
    amount = metadata["limits"]["amount"]
    assert amount == {"step": 1.0, "min": 1.0, "max": 1000000.0}
    assert metadata["contractSize"] == 0.0001
    assert metadata["taker"] == 0.0005


def test_missing_venue_ceiling_stays_unknown_rather_than_being_invented():
    market = {key: value for key, value in GATE_MARKET.items()}
    market["limits"] = {"amount": {"min": 1.0, "max": 1000000.0}}
    market["info"] = {"order_size_min": "1", "order_size_max": "1000000"}

    class BareExchange(StubExchange):
        def load_markets(self):
            return {"BTC/USDT:USDT": market}

    class BareTrader(StubTrader):
        def _get_exchange(self):
            return BareExchange()

    metadata = BareTrader().get_market_metadata("BTCUSDT")
    assert venue_leverage_limit(metadata) is None
