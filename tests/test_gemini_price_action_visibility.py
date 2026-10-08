"""Synthetic input inventory, no trading or provider calls."""
from copy import deepcopy
import json

import pytest

from core.replay.ai_history import digest
from scripts.audit_gemini_price_action_visibility import inspect_inputs, audit_directory


def inputs(swings, candles):
    return {'allowed_instruments':['ETHUSDT'], 'technical_context': {
        'price_action_encoding': {'keys': {'sd':'side','p':'price','st':'state'}, 'defaults': {'status':'READY'}},
        'ETHUSDT': {'timeframes': {'15m': {'candles': candles, 'price_action': {
            'confirmed_swings': swings, 'bos': {'st':'INVALIDATED'}, 'breakout_retest': {'st':'SUPERSEDED'}}}}}}}


def test_encoded_actual_limited_history_keeps_states_without_inventing_active_or_direction():
    value = inputs([{'sd':'HIGH','p':110},{'sd':'LOW','p':90}], [[100,110,90,105,5]])
    original = deepcopy(value)
    row = inspect_inputs(value)[0]
    assert value == original
    assert row['only_one_high_low_and_at_most_one_candle'] is True
    assert row['two_highs_and_two_lows_visible'] is False
    assert row['event_states'] == {'bos':'INVALIDATED','sweep_reclaim':None,'breakout_retest':'SUPERSEDED'}
    assert row['price_action_status'] == 'READY'


def test_native_two_swing_pairs_are_counted_without_declaring_a_trend():
    value = inputs([{'side':'HIGH','price':110},{'side':'HIGH','price':120},
                    {'side':'LOW','price':90},{'side':'LOW','price':95}], [])
    row = inspect_inputs(value)[0]
    assert row['two_highs_and_two_lows_visible'] is True
    assert row['only_one_high_low_and_at_most_one_candle'] is False
    assert 'trend' not in row


def test_longer_visible_candle_history_is_not_classified_as_one_candle_projection():
    value = inputs([{'sd':'HIGH','p':110},{'sd':'LOW','p':90}], [[1],[2]])
    assert inspect_inputs(value)[0]['only_one_high_low_and_at_most_one_candle'] is False


def test_missing_structure_frame_bars_or_price_is_not_fabricated_from_defaults():
    value = inputs([{'sd':'HIGH','p':None},{'sd':'LOW','p':90}], None)
    rows = inspect_inputs(value)
    assert rows[0]['explicit_confirmed_highs'] == 0
    assert rows[0]['visible_candle_rows'] is None
    assert rows[1]['price_action_present'] is False
    assert rows[1]['price_action_status'] is None
    assert rows[1]['visible_candle_rows'] is None


def test_no_selected_symbols_is_empty_not_full_coverage():
    assert inspect_inputs({'allowed_instruments':[]}) == []


@pytest.mark.parametrize('partition',['validation','untouched_test'])
def test_readonly_observer_cannot_use_heldout_to_guide_optimization(tmp_path, partition):
    plan = {'pilot_windows':[{'id':'never-read','partition':partition}]}
    (tmp_path/'research-plan.json').write_text(json.dumps({'plan':plan,'plan_sha256':digest(plan)}),encoding='utf-8')
    with pytest.raises(ValueError, match='OPTIMIZATION_ONLY'):
        audit_directory(tmp_path)
