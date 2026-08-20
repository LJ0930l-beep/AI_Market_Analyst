import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from core.instruments import instrument_for, parse_instrument_candidate
from core.providers import FixtureProvider
from core.quant import build_quant_snapshot
from core.radar import OpportunityScoreConfig, build_radar
from core.signals import Action, build_signal
from core.storage import SQLiteStore


NOW = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)


def _calibration(*, status: str = "ACTIVE", sample_count: int = 100) -> dict[str, object]:
    return {
        "calibration_id": "cal-radar-v1",
        "version": "phase3-calibration-v1",
        "status": status,
        "sample_count": sample_count,
        "params": {"min_sample": 100, "alpha": 5.0, "beta": 5.0},
        "scope": {"source_type": "live"},
        "buckets": [
            {"lower": 0.50, "upper": 0.60, "n": 5, "shrunk_rate": 0.52},
            {"lower": 0.60, "upper": 0.70, "n": 5, "shrunk_rate": 0.64},
            {"lower": 0.70, "upper": 0.80, "n": 5, "shrunk_rate": 0.75},
            {"lower": 0.80, "upper": 0.90, "n": 5, "shrunk_rate": 0.82},
            {"lower": 0.90, "upper": 1.00, "n": 5, "shrunk_rate": 0.90},
        ],
    }


def _record(
    symbol: str = "AAPL",
    *,
    action: str = "LONG",
    raw_confidence: float = 0.80,
    generated_at: datetime = NOW - timedelta(hours=1),
    data_as_of: datetime | None = NOW - timedelta(hours=1),
    signal_valid_until: datetime | None = NOW + timedelta(hours=1),
    regime: str = "bull_trend",
    news_available: bool = True,
    news: list[dict[str, object]] | None = None,
    time_policy: dict[str, object] | None = None,
    risk_events: list[dict[str, object]] | None = None,
    provider_snapshot: bool = True,
    target: float = 120.0,
) -> dict[str, object]:
    context: dict[str, object] = {
        "quant": {"market_regime": regime},
        "news": news if news is not None else [{"importance": 10}],
        "market_context": {"news_available": news_available},
    }
    if time_policy is not None:
        context["time_policy"] = time_policy
    if risk_events is not None:
        context["risk_events"] = risk_events
    if provider_snapshot:
        context["provider_snapshot"] = {
            "provider": "public-test",
            "data_as_of": (data_as_of or generated_at).isoformat(),
            "stale": False,
            "error_code": None,
        }
    prediction: dict[str, object] = {
        "prediction_id": f"prediction-{symbol}",
        "symbol": symbol,
        "action": action,
        "generated_at": generated_at.isoformat(),
        "data_as_of": data_as_of.isoformat() if data_as_of else None,
        "signal_valid_until": signal_valid_until.isoformat() if signal_valid_until else None,
        "raw_confidence": raw_confidence,
        "parse_status": "baseline",
        "entry_low": 99.0 if action != "WAIT" else None,
        "entry_high": 101.0 if action != "WAIT" else None,
        "stop": 90.0 if action == "LONG" else 110.0 if action == "SHORT" else None,
        "tp1": target if action == "LONG" else 80.0 if action == "SHORT" else None,
        "tp2": 130.0 if action == "LONG" else 70.0 if action == "SHORT" else None,
        "context_json": json.dumps(context),
    }
    return {"prediction": prediction, "outcome": {"status": "TP1", "realized_r": 1.5, "settled_at": NOW.isoformat()}}


def _watchlist(*symbols: str) -> list[dict[str, str]]:
    return [{"symbol": symbol, "added_at": NOW.isoformat(), "updated_at": NOW.isoformat()} for symbol in symbols]


def _instruments(*symbols: str) -> dict[str, object]:
    return {symbol: instrument_for(symbol) for symbol in symbols}


class RadarScoringTests(unittest.TestCase):
    def test_versioned_config_components_and_contributions_are_deterministic(self):
        result = build_radar(
            _watchlist("AAPL"),
            instruments=_instruments("AAPL"),
            predictions={"AAPL": _record()},
            calibration=_calibration(),
            now=NOW,
        )
        entry = result["entries"][0]
        self.assertEqual(result["scoring_version"], "opportunity_v1")
        self.assertEqual(result["config"]["version"], "opportunity_v1")
        self.assertAlmostEqual(sum(result["config"]["weights"].values()), 1.0)
        self.assertEqual(entry["category"], "STRONG_OPPORTUNITY")
        self.assertTrue(entry["ranking_eligible"])
        self.assertAlmostEqual(entry["inputs"]["calibrated_confidence"], 0.82)
        self.assertAlmostEqual(entry["inputs"]["risk_reward"], 2.0)
        self.assertAlmostEqual(entry["inputs"]["freshness"]["score"], entry["components"]["freshness"]["score"])
        self.assertAlmostEqual(entry["components"]["risk_reward"]["score"], 1 / 3, places=6)
        contributions = [component["contribution"] for component in entry["components"].values()]
        self.assertAlmostEqual(sum(contributions), entry["score"])
        self.assertEqual(entry["provenance"]["writes"], False)
        self.assertEqual(entry["provenance"]["calibration_scope"], {"source_type": "live"})
        self.assertEqual(entry["outcome"]["status"], "TP1")

    def test_risk_reward_normalization_has_explicit_boundaries(self):
        low = build_radar(
            _watchlist("AAPL"), instruments=_instruments("AAPL"),
            predictions={"AAPL": _record(target=115.0)}, calibration=_calibration(), now=NOW,
        )["entries"][0]
        high = build_radar(
            _watchlist("AAPL"), instruments=_instruments("AAPL"),
            predictions={"AAPL": _record(target=130.0)}, calibration=_calibration(), now=NOW,
        )["entries"][0]
        self.assertAlmostEqual(low["inputs"]["risk_reward"], 1.5)
        self.assertEqual(low["components"]["risk_reward"]["score"], 0.0)
        self.assertAlmostEqual(high["inputs"]["risk_reward"], 3.0)
        self.assertEqual(high["components"]["risk_reward"]["score"], 1.0)

    def test_calibration_gate_never_ranks_from_raw_confidence_only(self):
        result = build_radar(
            _watchlist("AAPL"), instruments=_instruments("AAPL"),
            predictions={"AAPL": _record(raw_confidence=0.99)},
            calibration=_calibration(status="INSUFFICIENT_SAMPLE", sample_count=9), now=NOW,
        )
        entry = result["entries"][0]
        self.assertEqual(entry["category"], "NOT_RANKED")
        self.assertFalse(entry["ranking_eligible"])
        self.assertIsNone(entry["score"])
        self.assertEqual(entry["inputs"]["raw_confidence"], 0.99)
        self.assertIsNone(entry["inputs"]["calibrated_confidence"])
        self.assertIn("calibration_not_eligible", entry["missing_reasons"])

    def test_event_risk_context_has_priority_over_empty_or_unavailable_news(self):
        explicit = build_radar(
            _watchlist("AAPL"), instruments=_instruments("AAPL"),
            predictions={"AAPL": _record(news=[], news_available=True, time_policy={"event_risk": True})},
            calibration=_calibration(), now=NOW,
        )["entries"][0]
        event_component = explicit["components"]["news_event_risk"]
        self.assertEqual(event_component["score"], 0.0)
        self.assertEqual(event_component["input"], "high-impact")
        self.assertEqual(event_component["provenance"], "context.time_policy.event_risk")
        self.assertIn("explicitly true", event_component["reason"])

        risk_event = build_radar(
            _watchlist("AAPL"), instruments=_instruments("AAPL"),
            predictions={"AAPL": _record(news=[], news_available=False, time_policy={"event_risk": False}, risk_events=[{"importance": 90}])},
            calibration=_calibration(), now=NOW,
        )["entries"][0]
        event_component = risk_event["components"]["news_event_risk"]
        self.assertEqual(event_component["score"], 0.0)
        self.assertEqual(event_component["input"], "high-impact")
        self.assertEqual(event_component["provenance"], "context.risk_events")
        self.assertIn("risk_events", event_component["reason"])

    def test_score_config_rejects_invalid_weight_and_threshold_definitions(self):
        with self.assertRaises(ValueError):
            OpportunityScoreConfig(weights=(("calibrated_confidence", 1.0),))
        with self.assertRaises(ValueError):
            OpportunityScoreConfig(strong_threshold=0.40, watch_threshold=0.45)

    def test_calibration_scope_and_missing_validity_block_actionable_ranking(self):
        mismatched = _calibration()
        mismatched["scope"] = {"source_type": "replay"}
        scoped = build_radar(
            _watchlist("AAPL"), instruments=_instruments("AAPL"),
            predictions={"AAPL": _record()}, calibration=mismatched, now=NOW,
        )["entries"][0]
        self.assertFalse(scoped["ranking_eligible"])
        self.assertIn("calibration_not_eligible", scoped["missing_reasons"])

        no_validity = build_radar(
            _watchlist("AAPL"), instruments=_instruments("AAPL"),
            predictions={"AAPL": _record(signal_valid_until=None)}, calibration=_calibration(), now=NOW,
        )["entries"][0]
        self.assertEqual(no_validity["category"], "AVOID")
        self.assertIn("signal_validity_missing", no_validity["missing_reasons"])

    def test_wait_expired_invalid_and_awaiting_analysis_are_not_ranked(self):
        entries = _watchlist("AAPL", "NVDA", "TSLA", "AMD")
        result = build_radar(
            entries,
            instruments=_instruments("AAPL", "NVDA", "TSLA", "AMD"),
            predictions={
                "AAPL": _record(action="WAIT"),
                "NVDA": _record(signal_valid_until=NOW - timedelta(minutes=1)),
                "TSLA": {"prediction": {**_record()["prediction"], "parse_status": "repair_failed"}},
            },
            calibration=_calibration(),
            now=NOW,
        )
        by_symbol = {entry["symbol"]: entry for entry in result["entries"]}
        self.assertEqual(by_symbol["AAPL"]["category"], "WAIT")
        self.assertEqual(by_symbol["NVDA"]["category"], "AVOID")
        self.assertEqual(by_symbol["TSLA"]["category"], "AVOID")
        self.assertEqual(by_symbol["AMD"]["status"], "awaiting_analysis")
        self.assertFalse(any(entry["ranking_eligible"] for entry in result["entries"]))

    def test_missing_context_is_explicitly_degraded_and_filtering_is_deterministic(self):
        records = {
            "AAPL": _record(),
            "NVDA": _record(symbol="NVDA"),
            "BTCUSDT": _record(symbol="BTCUSDT", regime="bear_trend"),
        }
        result = build_radar(
            _watchlist("NVDA", "AAPL", "BTCUSDT"),
            instruments=_instruments("AAPL", "NVDA", "BTCUSDT"),
            predictions=records,
            calibration=_calibration(),
            now=NOW,
        )
        self.assertEqual([entry["symbol"] for entry in result["entries"]], ["AAPL", "NVDA", "BTCUSDT"])
        filtered = build_radar(
            _watchlist("AAPL", "BTCUSDT"),
            instruments=_instruments("AAPL", "BTCUSDT"),
            predictions={"BTCUSDT": {"prediction": {**records["BTCUSDT"]["prediction"], "context_json": None}}},
            calibration=_calibration(), asset_type="crypto", now=NOW,
        )
        self.assertEqual([entry["symbol"] for entry in filtered["entries"]], ["BTCUSDT"])
        self.assertFalse(filtered["entries"][0]["ranking_eligible"])
        self.assertIn("data_quality_unknown", filtered["entries"][0]["missing_reasons"])


class RadarApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "radar.sqlite3"
        self.store = SQLiteStore(self.path)
        self.store.initialize()
        self.store.upsert_watchlist_entry("AAPL")
        candidate = parse_instrument_candidate("MSFT", "equity")
        self.store.save_instrument(
            candidate.instrument,
            registry_source="registered",
            metadata_status=candidate.metadata_status,
            metadata_labels=candidate.metadata_labels,
            validation_provider="public-test",
            validated_at=NOW.isoformat(),
        )
        self.store.upsert_watchlist_entry("MSFT")
        self.client = TestClient(create_app(store=self.store))

    def tearDown(self):
        self.temp.cleanup()

    def test_get_radar_reads_watchlist_registered_symbol_and_does_not_write(self):
        before = self.store.counts()
        response = self.client.get("/radar")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["scoring_version"], "opportunity_v1")
        self.assertEqual([entry["symbol"] for entry in payload["entries"]], ["AAPL", "MSFT"])
        self.assertEqual(payload["entries"][1]["status"], "awaiting_analysis")
        self.assertEqual(payload["entries"][1]["instrument"]["symbol"], "MSFT")
        self.assertEqual(self.store.counts(), before)

    def test_get_radar_filters_and_rejects_unknown_filter_without_side_effect(self):
        before = self.store.counts()
        filtered = self.client.get("/radar", params={"asset_type": "crypto"})
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(filtered.json()["entries"], [])
        invalid = self.client.get("/radar", params={"category": "mystery"})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["error"]["code"], "INVALID_RADAR_CATEGORY")
        self.assertEqual(self.store.counts(), before)

    def test_latest_prediction_helper_is_newest_and_restart_safe(self):
        instrument = instrument_for("AAPL")
        quant = build_quant_snapshot(FixtureProvider().get_bars(instrument, "1h", 120), "1h", symbol="AAPL")
        for prediction_id, generated_at in (("old", NOW - timedelta(hours=2)), ("new", NOW - timedelta(hours=1))):
            signal = build_signal(
                instrument,
                quant,
                timeframe="1h",
                generated_at=generated_at,
                prediction_id=prediction_id,
                force_action=Action.LONG,
                data_as_of=generated_at,
            )
            self.store.save_prediction(signal)
        latest = self.store.list_latest_prediction_records(["AAPL"])
        self.assertEqual(latest["AAPL"]["prediction"]["prediction_id"], "new")
        reopened = SQLiteStore(self.path)
        reopened.initialize()
        self.assertEqual(reopened.list_latest_prediction_records(["AAPL"])["AAPL"]["prediction"]["prediction_id"], "new")


if __name__ == "__main__":
    unittest.main()
