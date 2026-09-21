import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from core.instruments import instrument_for
from core.performance.calibration import apply_calibration, fit_calibration
from core.performance.metrics import aggregate_performance
from core.providers import FixtureProvider
from core.quant import build_quant_snapshot
from core.signals import Action, build_signal


def _record(index: int, action: str, realized_r: float | None, raw: float = 0.7, calibrated: float | None = None):
    generated = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=index)
    prediction = {
        "prediction_id": f"p-{index}",
        "instrument": {"symbol": "NVDA", "asset_type": "equity"},
        "analysis_timeframe": "1h",
        "generated_at": generated.isoformat(),
        "action": action,
        "raw_confidence": raw,
        "calibrated_confidence": calibrated,
        "expected_hold_minutes": 1440,
        "model_id": "Bonsai-2-27B-PTQ1_0",
        "prompt_version": "phase2-json-v6",
        "source_type": "replay",
        "parse_status": "valid",
        "context_json": json.dumps(
            {
                "quant": {"market_regime": "bull_trend"},
                "market_context": {"news_available": True},
                "news": [],
            }
        ),
    }
    outcome = None
    if realized_r is not None:
        outcome = {
            "prediction_id": f"p-{index}",
            "status": "TIMEOUT",
            "settled_at": (generated + timedelta(hours=1)).isoformat(),
            "realized_r": realized_r,
            "mfe_r": max(realized_r, 0.4),
            "mae_r": min(realized_r, -0.2),
        }
    return {"prediction": prediction, "outcome": outcome}


class PerformancePhase3Tests(unittest.TestCase):
    def test_calibration_default_provenance_uses_pinned_bonsai_model(self):
        result = fit_calibration([], scope={"source_type": "replay"})
        self.assertEqual(result.version, "cal-v1-Bonsai-2-27B-PTQ1_0")

    def test_calibration_preserves_explicit_model_scope(self):
        result = fit_calibration([], scope={"source_type": "replay", "model_id": "custom-model"})
        self.assertEqual(result.version, "cal-v1-custom-model")

    def test_metrics_include_wait_in_coverage_but_not_win_rate(self):
        records = [_record(0, "LONG", 1.0), _record(1, "SHORT", -1.0), _record(2, "LONG", 0.5), _record(3, "WAIT", None)]
        metrics = aggregate_performance(records, scope={"source_type": "replay"})
        self.assertEqual(metrics["sample_count"], 4)
        self.assertEqual(metrics["actionable_count"], 3)
        self.assertEqual(metrics["resolved_actionable"], 3)
        self.assertAlmostEqual(metrics["win_rate"], 2 / 3)
        self.assertAlmostEqual(metrics["avg_r"], 1 / 6)
        self.assertAlmostEqual(metrics["profit_factor"], 1.5)
        self.assertAlmostEqual(metrics["coverage"], 0.75)
        self.assertAlmostEqual(metrics["wait_rate"], 0.25)
        self.assertAlmostEqual(metrics["max_drawdown_r"], -1.0)
        self.assertEqual(metrics["timeout_count"], 3)

    def test_insufficient_calibration_preserves_raw_confidence(self):
        records = [_record(index, "LONG", 1.0 if index % 2 else -1.0) for index in range(12)]
        result = fit_calibration(records, scope={"source_type": "replay"}, min_sample=100)
        self.assertEqual(result.status, "INSUFFICIENT_SAMPLE")
        self.assertEqual(result.fallback, "raw_insufficient_sample")
        instrument = instrument_for("NVDA")
        bars = FixtureProvider().get_bars(instrument, "1h", 120)
        quant = build_quant_snapshot(bars, "1h", symbol=instrument.symbol)
        signal = build_signal(instrument, quant, force_action=Action.LONG)
        signal = replace(signal, raw_confidence=0.72)
        calibrated = apply_calibration(signal, result)
        self.assertAlmostEqual(calibrated.raw_confidence, 0.72)
        self.assertAlmostEqual(calibrated.calibrated_confidence, 0.72)
        self.assertEqual(calibrated.calibration_sample_size, 12)

    def test_beta_shrinkage_is_used_after_sample_gate(self):
        records = [_record(index, "LONG", 1.0 if index < 70 else -1.0) for index in range(100)]
        result = fit_calibration(records, scope={"source_type": "replay"}, min_sample=100)
        self.assertEqual(result.status, "ACTIVE")
        bucket = next(item for item in result.buckets if item.lower == 0.7)
        self.assertEqual(bucket.n, 100)
        self.assertAlmostEqual(bucket.shrunk_rate or 0.0, 75 / 110)


if __name__ == "__main__":
    unittest.main()
