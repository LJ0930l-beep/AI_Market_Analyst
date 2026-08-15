import unittest
from datetime import datetime, timezone

from core.instruments import instrument_for
from core.providers import (
    Bar,
    FixtureProvider,
    ProviderChain,
    ProviderError,
    Quote,
    fetch_market_data,
)
from core.providers.binance import BinancePublicProvider
from core.providers.yfinance import YFinanceProvider


class _FailingProvider:
    provider_name = "failing"

    def get_quote(self, instrument):
        raise ProviderError("offline", code="network_error", provider=self.provider_name)

    def get_bars(self, instrument, timeframe, limit=200):
        raise ProviderError("offline", code="network_error", provider=self.provider_name)


class _MockBinance(BinancePublicProvider):
    def __init__(self):
        super().__init__(base_url="https://example.invalid", timeout=0.01, retries=0)

    def _request(self, path, params):
        if path.endswith("klines"):
            return [
                [1704067200000, "42000", "42500", "41800", "42300", "12.5"],
                [1704070800000, "42300", "42600", "42100", "42400", "10.0"],
            ]
        return {"symbol": params["symbol"], "price": "42400"}


class _MockYahoo(YFinanceProvider):
    def __init__(self):
        super().__init__(timeout=0.01, retries=0)

    def _ticker(self, instrument):
        raise ProviderError("optional package absent", code="dependency_missing", provider=self.provider_name)

    def _request_chart(self, instrument, timeframe, limit):
        return {
            "chart": {
                "result": [{
                    "timestamp": [1704067200, 1704070800],
                    "indicators": {"quote": [{
                        "open": [100, 101], "high": [102, 103], "low": [99, 100], "close": [101, 102], "volume": [1000, 1100]
                    }]},
                }]
            }
        }


class ProviderPhase2Tests(unittest.TestCase):
    def test_crypto_instrument_exposes_base_and_quote(self):
        instrument = instrument_for("BTCUSDT")
        self.assertEqual(instrument.base, "BTC")
        self.assertEqual(instrument.quote_currency, "USDT")
        self.assertEqual(instrument.quote, "USDT")

    def test_binance_maps_public_payload_to_utc_contracts(self):
        provider = _MockBinance()
        instrument = instrument_for("BTCUSDT")
        bars = provider.get_bars(instrument, "1h", 2)
        quote = provider.get_quote(instrument)
        self.assertIsInstance(bars[0], Bar)
        self.assertIsInstance(quote, Quote)
        self.assertEqual(bars[-1].timestamp.tzinfo, timezone.utc)
        self.assertEqual(quote.timestamp.tzinfo, timezone.utc)
        self.assertEqual(quote.price, 42400.0)

    def test_yahoo_public_chart_fallback_maps_without_yfinance_package(self):
        provider = _MockYahoo()
        bars = provider.get_bars(instrument_for("AAPL"), "1h", 2)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[-1].close, 102.0)
        self.assertEqual(bars[-1].timestamp.tzinfo, timezone.utc)

    def test_fixture_metadata_is_explicitly_stale(self):
        instrument = instrument_for("AAPL")
        bundle = fetch_market_data(FixtureProvider(), instrument, "1h", 120)
        self.assertEqual(bundle.snapshot.provider, "fixture")
        self.assertTrue(bundle.snapshot.stale)
        self.assertLessEqual(bundle.snapshot.data_as_of, bundle.snapshot.fetched_at)

    def test_provider_chain_preserves_honest_fixture_fallback(self):
        instrument = instrument_for("ETHUSDT")
        bundle = ProviderChain([_FailingProvider(), FixtureProvider()]).get_bundle(instrument, "1h", 120)
        self.assertEqual(bundle.snapshot.provider, "fixture")
        self.assertTrue(bundle.snapshot.stale)
        self.assertEqual(bundle.quote.instrument.symbol, "ETHUSDT")


if __name__ == "__main__":
    unittest.main()
