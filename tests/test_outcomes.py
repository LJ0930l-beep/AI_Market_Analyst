import unittest
from datetime import datetime, timedelta, timezone

from core.instruments import instrument_for
from core.outcomes import OutcomeStatus, evaluate_outcome_as_of, settle_prediction
from core.providers import Bar
from core.signals import Action, SignalProposal


def make_signal(action: Action = Action.LONG) -> SignalProposal:
    generated = datetime(2026, 1, 1, tzinfo=timezone.utc)
    if action is Action.LONG:
        levels = (100.0, 100.0, 95.0, 107.5, 112.5)
    else:
        levels = (100.0, 100.0, 105.0, 92.5, 87.5)
    return SignalProposal(
        prediction_id=f"outcome-{action.value.lower()}",
        instrument=instrument_for("AAPL"),
        analysis_timeframe="1h",
        generated_at=generated,
        action=action,
        entry_low=levels[0],
        entry_high=levels[1],
        stop=levels[2],
        tp1=levels[3],
        tp2=levels[4],
        signal_validity_minutes=60,
        expected_hold_minutes=120,
        max_hold_minutes=180,
        reevaluate_at=generated + timedelta(minutes=60),
        invalidation=("stop touched",),
        raw_confidence=0.7,
        reason_codes=("test",),
        summary="test signal",
    )


class OutcomeTests(unittest.TestCase):
    def test_tp1(self):
        signal = make_signal()
        bars = [Bar(signal.generated_at + timedelta(hours=1), 100, 108, 99, 106, 100)]
        outcome = settle_prediction(signal, bars)
        self.assertEqual(outcome.status, OutcomeStatus.TP1)
        self.assertAlmostEqual(outcome.realized_r, 1.5)
        self.assertGreater(outcome.mfe_r, 1.5)

    def test_stop_wins_intrabar_conflict(self):
        signal = make_signal()
        bars = [Bar(signal.generated_at + timedelta(hours=1), 100, 108, 94, 101, 100)]
        outcome = settle_prediction(signal, bars)
        self.assertEqual(outcome.status, OutcomeStatus.STOP)
        self.assertEqual(outcome.realized_r, -1.0)

    def test_timeout_records_r_and_extremes(self):
        signal = make_signal()
        bars = [
            Bar(signal.generated_at + timedelta(hours=1), 100, 102, 98, 101, 100),
            Bar(signal.generated_at + timedelta(hours=2), 101, 103, 99, 102, 100),
            Bar(signal.generated_at + timedelta(hours=4), 102, 104, 100, 103, 100),
        ]
        outcome = settle_prediction(signal, bars)
        self.assertEqual(outcome.status, OutcomeStatus.TIMEOUT)
        self.assertTrue(outcome.timeout)
        self.assertEqual(outcome.bars_held, 3)
        self.assertGreater(outcome.realized_r, 0)

    def test_wait_is_not_actionable(self):
        generated = datetime(2026, 1, 1, tzinfo=timezone.utc)
        signal = SignalProposal(
            prediction_id="wait-1",
            instrument=instrument_for("BTCUSDT"),
            analysis_timeframe="1h",
            generated_at=generated,
            action=Action.WAIT,
            entry_low=None,
            entry_high=None,
            stop=None,
            tp1=None,
            tp2=None,
            signal_validity_minutes=60,
            expected_hold_minutes=120,
            max_hold_minutes=180,
            reevaluate_at=generated + timedelta(minutes=60),
            invalidation=("new snapshot required",),
            raw_confidence=0.5,
            reason_codes=("no-edge",),
            summary="wait",
        )
        outcome = settle_prediction(signal, [Bar(generated + timedelta(hours=1), 1, 1.1, 0.9, 1, 1)])
        self.assertEqual(outcome.status, OutcomeStatus.NOT_ACTIONABLE)

    def test_incremental_evaluator_waits_before_horizon_and_ignores_future_bars(self):
        signal = make_signal()
        trigger = Bar(signal.generated_at + timedelta(hours=1), 100, 108, 99, 106, 100)
        future = Bar(signal.generated_at + timedelta(hours=2), 100, 113, 99, 111, 100)
        self.assertIsNone(evaluate_outcome_as_of(signal, [trigger], signal.generated_at + timedelta(minutes=30)))
        self.assertEqual(
            evaluate_outcome_as_of(signal, [trigger, future], trigger.timestamp).status,
            OutcomeStatus.TP1,
        )
        self.assertIsNone(evaluate_outcome_as_of(signal, [future], signal.generated_at + timedelta(hours=1)))

    def test_incremental_evaluator_missing_weekend_bar_stays_pending_until_horizon_bar(self):
        signal = make_signal()
        pre_horizon = Bar(signal.generated_at + timedelta(hours=2), 100, 103, 99, 102, 100)
        weekend_as_of = signal.generated_at + timedelta(days=3)
        self.assertIsNone(evaluate_outcome_as_of(signal, [pre_horizon], weekend_as_of))
        horizon_bar = Bar(weekend_as_of, 100, 103, 99, 102, 100)
        outcome = evaluate_outcome_as_of(signal, [pre_horizon, horizon_bar], weekend_as_of)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.status, OutcomeStatus.TIMEOUT)
        self.assertTrue(outcome.timeout)
        self.assertGreaterEqual(outcome.settled_at, signal.max_hold_until)

    def test_incremental_short_targets_and_same_bar_stop_priority(self):
        signal = make_signal(Action.SHORT)
        tp2 = Bar(signal.generated_at + timedelta(hours=1), 100, 101, 87, 90, 100)
        self.assertEqual(evaluate_outcome_as_of(signal, [tp2], tp2.timestamp).status, OutcomeStatus.TP2)
        conflict = Bar(signal.generated_at + timedelta(hours=1), 100, 106, 90, 100, 100)
        self.assertEqual(evaluate_outcome_as_of(signal, [conflict], conflict.timestamp).status, OutcomeStatus.STOP)

    def test_incremental_wait_is_not_actionable_at_point(self):
        generated = datetime(2026, 1, 1, tzinfo=timezone.utc)
        signal = SignalProposal(
            prediction_id="wait-point",
            instrument=instrument_for("BTCUSDT"),
            analysis_timeframe="1h",
            generated_at=generated,
            action=Action.WAIT,
            entry_low=None,
            entry_high=None,
            stop=None,
            tp1=None,
            tp2=None,
            signal_validity_minutes=60,
            expected_hold_minutes=120,
            max_hold_minutes=180,
            reevaluate_at=generated + timedelta(minutes=60),
            invalidation=("new snapshot required",),
            raw_confidence=0.5,
            reason_codes=("no-edge",),
            summary="wait",
        )
        outcome = evaluate_outcome_as_of(signal, [], generated + timedelta(hours=1))
        self.assertEqual(outcome.status, OutcomeStatus.NOT_ACTIONABLE)


if __name__ == "__main__":
    unittest.main()
