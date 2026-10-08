"""Isolated geometry fixtures, never real-market or profitability evidence."""
from copy import deepcopy
import pytest
from scripts.diagnose_pa_confirmation_semantics import retest_candle_fact, wire_digest
from core.ai.transport_diagnostics import CompletionTransportTrace


def frame(side='SHORT', candle=None):
    stamp = '2025-10-15T01:30:00Z'
    return {'last_closed_at': stamp, 'candles': [candle or [100, 104, 96, 97, 10]],
            'price_action': {'breakout_retest': {'state': 'ACTIVE', 'side': side,
                'level': 100, 'bar_at': stamp, 'confirmed_at': stamp}}}


@pytest.mark.parametrize('side,candle,closed,body', [
    ('SHORT', [100, 104, 96, 97, 10], True, 'DOWN'),
    ('LONG', [100, 104, 96, 103, 10], True, 'UP'),
    ('SHORT', [97, 104, 96, 99, 10], True, 'UP'),
    ('SHORT', [100, 104, 96, 103, 10], False, 'UP'),
])
def test_touch_close_fact_is_not_reversal_quality_or_entry_instruction(side, candle, closed, body):
    data = frame(side, candle)
    before = deepcopy(data)
    result = retest_candle_fact(data, '2025-10-15T01:30:00+00:00')
    assert result['range_touched_level'] is True
    assert result['closed_on_breakout_side'] is closed
    assert result['body_direction'] == body
    assert result['not_a_complete_setup_or_required_trade'] is True
    assert 'action' not in result
    assert data == before


def test_previous_retest_candle_not_replaced_by_current_candle_geometry():
    data = frame()
    data['price_action']['breakout_retest']['bar_at'] = '2025-10-15T01:15:00Z'
    result = retest_candle_fact(data, '2025-10-15T01:30:00+00:00')
    assert result['current_candle_is_event_candle'] is False
    assert 'ohlc' not in result


def test_future_event_not_reclassified_as_known():
    data = frame()
    data['price_action']['breakout_retest']['confirmed_at'] = '2025-10-15T01:31:00Z'
    with pytest.raises(ValueError, match='NOT_YET_KNOWN'):
        retest_candle_fact(data, '2025-10-15T01:30:00+00:00')


@pytest.mark.parametrize('value', [True, float('nan'), float('inf'), -2, '100'])
def test_invalid_event_price_cannot_become_a_confirmed_fact(value):
    data = frame()
    data['price_action']['breakout_retest']['level'] = value
    with pytest.raises(ValueError, match='NUMERIC_GEOMETRY_REQUIRED'):
        retest_candle_fact(data, '2025-10-15T01:30:00+00:00')


def test_invalid_ohlc_rejected():
    with pytest.raises(ValueError, match='OHLC_INVALID'):
        retest_candle_fact(frame(candle=[100, 98, 101, 99, 10]), '2025-10-15T01:30:00+00:00')


def test_invalidated_retest_not_selected_as_active():
    data = frame()
    data['price_action']['breakout_retest']['state'] = 'INVALIDATED'
    assert retest_candle_fact(data, '2025-10-15T01:30:00+00:00') is None


def test_chinese_actual_request_hash_matches_production_transport_representation():
    messages = [{'role': 'user', 'content': '回测已知，额外条件未必成立'}]
    trace = CompletionTransportTrace(messages, 1, 170)
    assert wire_digest(messages) == trace.data['wire_messages_sha256']


@pytest.mark.parametrize('text', ['2025-10-15T01:30:00+00:00', '2025-10-15T09:30:00+08:00'])
def test_equivalent_timestamp_encodings_refer_to_same_event_candle(text):
    data = frame()
    data['last_closed_at'] = text
    result = retest_candle_fact(data, '2025-10-15T01:30:00+00:00')
    assert result['current_candle_is_event_candle'] is True
    assert result['range_touched_level'] is True


def test_naive_event_time_not_accepted():
    data = frame()
    data['price_action']['breakout_retest']['bar_at'] = '2025-10-15T01:30:00'
    with pytest.raises(ValueError, match='AWARE_TIME_REQUIRED'):
        retest_candle_fact(data, '2025-10-15T01:30:00+00:00')
