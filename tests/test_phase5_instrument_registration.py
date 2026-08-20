import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from core.analysis_service import AnalysisService
from core.instruments import (
    AssetType,
    CandidateParseError,
    instrument_for,
    parse_instrument_candidate,
)
from core.providers import (
    FixtureNewsProvider,
    FixtureProvider,
    InstrumentValidationError,
    ProviderError,
    PublicInstrumentValidator,
    Quote,
)
from core.storage import SQLiteStore


class _ProbeProvider:
    stale = False

    def __init__(self, provider_name: str, error: ProviderError | None = None, *, stale: bool = False):
        self.provider_name = provider_name
        self.error = error
        self.stale = stale

    def get_quote(self, instrument):
        if self.error is not None:
            raise self.error
        return Quote(
            instrument=instrument,
            timestamp=datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc),
            price=100.0,
        )


def _validator(
    *,
    equity_provider_factory=None,
    crypto_provider_factory=None,
) -> PublicInstrumentValidator:
    return PublicInstrumentValidator(
        equity_provider_factory=equity_provider_factory or (lambda: _ProbeProvider("yahoo_test")),
        crypto_provider_factory=crypto_provider_factory or (lambda: _ProbeProvider("binance_test")),
        timeout=99,
        retries=99,
    )


class InstrumentCandidateTests(unittest.TestCase):
    def test_canonical_mapping_remains_stable_and_expansion_metadata_is_explicit(self):
        canonical = instrument_for(" btc ")
        self.assertEqual(canonical.symbol, "BTCUSDT")
        self.assertEqual(canonical.exchange, "PUBLIC")
        self.assertEqual(canonical.sector, "Crypto")

        equity = parse_instrument_candidate("msft", "equity")
        self.assertEqual(equity.instrument.symbol, "MSFT")
        self.assertEqual(equity.instrument.currency, "USD")
        self.assertEqual(equity.instrument.trading_hours.value, "regular")
        self.assertEqual(equity.instrument.exchange, "UNKNOWN")
        self.assertIsNone(equity.instrument.sector)
        self.assertEqual(equity.metadata_status, "inferred")
        self.assertEqual(equity.metadata_labels["exchange"], "unknown")
        self.assertEqual(equity.metadata_labels["trading_hours"], "inferred")

        crypto_alias = parse_instrument_candidate("sol", "crypto")
        crypto_full = parse_instrument_candidate("SOLUSDT", "crypto")
        self.assertEqual(crypto_alias.instrument.symbol, "SOLUSDT")
        self.assertEqual(crypto_full.instrument.symbol, "SOLUSDT")
        self.assertEqual(crypto_alias.instrument.exchange, "BINANCE")
        self.assertEqual(crypto_alias.instrument.currency, "USDT")

    def test_candidate_parser_rejects_unsafe_or_ambiguous_values(self):
        invalid = (
            (" MSFT", "equity"),
            ("MSFT ", "equity"),
            ("BTC/USDT", "crypto"),
            ("..", "equity"),
            ("A" * 17, "equity"),
            ("SOLUSDT", "equity"),
            ("USDT", "crypto"),
            ("NOPE", "forex"),
            ("MSFT", " equity"),
            ("MSFT\u0000", "equity"),
        )
        for symbol, asset_type in invalid:
            with self.subTest(symbol=symbol, asset_type=asset_type):
                with self.assertRaises(CandidateParseError):
                    parse_instrument_candidate(symbol, asset_type)

    def test_public_validator_distinguishes_success_transient_unsupported_and_fixture(self):
        equity = parse_instrument_candidate("MSFT", "equity")
        crypto = parse_instrument_candidate("SOL", "crypto")
        equity_result = _validator().validate(equity)
        crypto_result = _validator().validate(crypto)
        self.assertEqual(equity_result.provider, "yahoo_test")
        self.assertEqual(crypto_result.provider, "binance_test")
        self.assertEqual(equity_result.mode, "public_probe")

        transient = _validator(
            equity_provider_factory=lambda: _ProbeProvider(
                "yahoo_test",
                ProviderError("offline", code="network_error", provider="yahoo_test"),
            ),
        )
        with self.assertRaises(InstrumentValidationError) as transient_error:
            transient.validate(equity)
        self.assertEqual(transient_error.exception.code, "PROVIDER_UNAVAILABLE")

        unsupported = _validator(
            equity_provider_factory=lambda: _ProbeProvider(
                "yahoo_test",
                ProviderError("not listed", code="unsupported_symbol", provider="yahoo_test"),
            ),
        )
        with self.assertRaises(InstrumentValidationError) as unsupported_error:
            unsupported.validate(equity)
        self.assertEqual(unsupported_error.exception.code, "INSTRUMENT_UNSUPPORTED")

        fixture = _validator(
            equity_provider_factory=lambda: _ProbeProvider("fixture", stale=True),
        )
        with self.assertRaises(InstrumentValidationError) as fixture_error:
            fixture.validate(equity)
        self.assertEqual(fixture_error.exception.code, "PROVIDER_UNAVAILABLE")


class InstrumentRegistrationAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "registered.sqlite3"
        self.store = SQLiteStore(self.path)
        self.store.initialize()
        self.validator = _validator()
        analysis_service = AnalysisService(
            market_provider_factory=lambda _instrument: FixtureProvider(),
            news_provider=FixtureNewsProvider(),
            llm_provider=None,
            store=self.store,
        )
        self.app = create_app(
            store=self.store,
            analysis_service=analysis_service,
            snapshot_service=analysis_service,
            news_provider=FixtureNewsProvider(),
            llm_provider=None,
            instrument_validator=self.validator,
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        self.temp.cleanup()

    def test_registration_union_duplicate_watchlist_crud_and_failure_non_persistence(self):
        arbitrary = self.client.post("/watchlist", json={"symbol": "MSFT"})
        self.assertEqual(arbitrary.status_code, 400)
        self.assertEqual(arbitrary.json()["error"]["code"], "INVALID_SYMBOL")

        registered = self.client.post("/instruments/register", json={"symbol": "msft", "asset_type": "equity"})
        self.assertEqual(registered.status_code, 200)
        payload = registered.json()
        self.assertFalse(payload["idempotent"])
        self.assertEqual(payload["instrument"]["symbol"], "MSFT")
        self.assertEqual(payload["instrument"]["registry_source"], "registered")
        self.assertEqual(payload["instrument"]["metadata_status"], "inferred")
        self.assertEqual(payload["instrument"]["metadata_labels"]["sector"], "unknown")
        self.assertEqual(payload["validation"]["provider"], "yahoo_test")
        self.assertIsNotNone(self.store.get_instrument("MSFT"))
        self.assertIn("MSFT", [item.symbol for item in self.store.list_instruments()])

        invalid_payload = self.client.post(
            "/instruments/register",
            json={"symbol": "ORCL", "asset_type": "equity", "provider": "fixture"},
        )
        self.assertEqual(invalid_payload.status_code, 400)
        self.assertEqual(invalid_payload.json()["error"]["code"], "INVALID_INSTRUMENT_REGISTRATION_PAYLOAD")
        self.assertIsNone(self.store.get_instrument("ORCL"))

        duplicate = self.client.post("/instruments/register", json={"symbol": "MSFT", "asset_type": "equity"})
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json()["idempotent"])
        self.assertEqual(duplicate.json()["validation"]["status"], "already_registered")

        instruments = self.client.get("/instruments")
        self.assertEqual(instruments.status_code, 200)
        symbols = [item["symbol"] for item in instruments.json()]
        self.assertEqual(symbols[:6], ["AAPL", "NVDA", "TSLA", "AMD", "BTCUSDT", "ETHUSDT"])
        self.assertEqual(symbols.count("MSFT"), 1)

        saved = self.client.post("/watchlist", json={"symbol": "MSFT"})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["instrument"]["symbol"], "MSFT")
        self.assertEqual(self.client.put("/watchlist/MSFT", json={"symbol": "MSFT"}).status_code, 200)
        self.assertEqual(self.client.get("/watchlist").json()[0]["symbol"], "MSFT")
        self.assertEqual(self.client.delete("/watchlist/MSFT").json(), {"symbol": "MSFT", "deleted": True})

        unsupported_validator = _validator(
            equity_provider_factory=lambda: _ProbeProvider(
                "yahoo_test",
                ProviderError("not listed", code="unsupported_symbol", provider="yahoo_test"),
            ),
        )
        unsupported_client = TestClient(create_app(store=self.store, instrument_validator=unsupported_validator))
        rejected = unsupported_client.post("/instruments/register", json={"symbol": "ORCL", "asset_type": "equity"})
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(rejected.json()["error"]["code"], "INSTRUMENT_UNSUPPORTED")
        self.assertIsNone(self.store.get_instrument("ORCL"))

        transient_validator = _validator(
            equity_provider_factory=lambda: _ProbeProvider(
                "yahoo_test",
                ProviderError("offline", code="network_error", provider="yahoo_test"),
            ),
        )
        transient_client = TestClient(create_app(store=self.store, instrument_validator=transient_validator))
        unavailable = transient_client.post("/instruments/register", json={"symbol": "INTC", "asset_type": "equity"})
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.json()["error"]["code"], "PROVIDER_UNAVAILABLE")
        self.assertIsNone(self.store.get_instrument("INTC"))

    def test_registered_symbols_resolve_after_restart_for_detail_and_wait_analysis(self):
        registered = self.client.post("/instruments/register", json={"symbol": "SOL", "asset_type": "crypto"})
        self.assertEqual(registered.status_code, 200)
        self.assertEqual(registered.json()["instrument"]["symbol"], "SOLUSDT")

        restarted = SQLiteStore(self.path)
        restarted.initialize()
        self.assertEqual(restarted.get_instrument("SOLUSDT").symbol, "SOLUSDT")
        restarted_record = restarted.get_instrument_record("SOLUSDT")
        self.assertEqual(restarted_record["metadata_labels"]["sector"], "unknown")
        self.assertIn("SOLUSDT", [item.symbol for item in restarted.list_instruments()])
        restarted_service = AnalysisService(
            market_provider_factory=lambda _instrument: FixtureProvider(),
            news_provider=FixtureNewsProvider(),
            llm_provider=None,
            store=restarted,
        )
        restarted_client = TestClient(create_app(
            store=restarted,
            analysis_service=restarted_service,
            snapshot_service=restarted_service,
            news_provider=FixtureNewsProvider(),
            llm_provider=None,
            instrument_validator=self.validator,
        ))

        saved = restarted_client.post("/watchlist", json={"symbol": "SOLUSDT"})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["instrument"]["symbol"], "SOLUSDT")
        self.assertEqual(restarted_client.get("/watchlist").json()[0]["symbol"], "SOLUSDT")
        snapshot = restarted_client.get("/instruments/SOLUSDT/snapshot", params={"limit": 60})
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.json()["symbol"], "SOLUSDT")
        news = restarted_client.get("/instruments/SOLUSDT/news")
        self.assertEqual(news.status_code, 200)
        analysis = restarted_client.post("/analysis/SOLUSDT", json={"limit": 60})
        self.assertEqual(analysis.status_code, 200)
        self.assertEqual(analysis.json()["instrument"]["symbol"], "SOLUSDT")
        self.assertEqual(analysis.json()["signal"]["action"], "WAIT")
        self.assertEqual(restarted.counts()["predictions"], 1)

        invalid = restarted_client.get("/instruments/NOT_REGISTERED/snapshot")
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["error"]["code"], "INVALID_SYMBOL")


if __name__ == "__main__":
    unittest.main()
