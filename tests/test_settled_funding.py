"""Offline causality/unit/provenance checks; fixtures are not market evidence."""
from copy import deepcopy

import pytest

from core.replay.settled_funding import settled_funding_as_of


def record(at='2025-10-15T00:00:00Z', rate='-0.00005425', **extra):
    return {'instrument_id': 'BTCUSDT', 'payment_time': at, 'rate': rate,
            'source': 'binance_official_funding_archive', **extra}


def view(records, at='2025-10-15T00:05:00Z', **kwargs):
    return settled_funding_as_of(records, ['BTCUSDT', 'ETHUSDT'], at, venue='binance', **kwargs)


def test_settled_rate_units_and_binance_identity_are_explicit_without_fabricated_oi():
    records = [record()]
    original = deepcopy(records)
    result = view(records)
    btc = result['rates']['BTCUSDT']
    assert btc['rate_fraction'] == '-0.00005425' and btc['rate_percent'] == '-0.00542500'
    assert btc['venue'] == 'binance' and not btc['live_or_next_predicted_rate']
    assert result['rates']['ETHUSDT']['rate_fraction'] is None
    assert result['open_interest_status'] == 'UNAVAILABLE_NOT_INFERRED'
    assert records == original


def test_future_rate_and_future_malformed_value_cannot_change_earlier_facts():
    baseline = view([record()])
    future = record('2025-10-15T08:00:00Z', 'bad future value', source='unknown')
    assert view([record(), future]) == baseline


def test_explicit_publication_delay_and_default_research_lag_are_honoured():
    delayed = record(available_at='2025-10-15T00:07:00Z')
    assert view([delayed])['rates']['BTCUSDT']['rate_fraction'] is None
    assert view([record()], at='2025-10-15T00:00:30Z')['rates']['BTCUSDT']['rate_fraction'] is None
    assert view([record()], at='2025-10-15T00:01:00Z')['rates']['BTCUSDT']['rate_fraction'] is not None
    assert view([delayed], at='2025-10-15T00:07:00Z')['rates']['BTCUSDT']['availability_basis'] == 'EXPLICIT_RECORD_METADATA'


def test_latest_known_record_selected_independent_of_input_order():
    records = [record(), record('2025-10-15T08:00:00Z', '0.00003973')]
    assert view(records, at='2025-10-15T08:05:00Z') == view(list(reversed(records)), at='2025-10-15T08:05:00Z')
    assert view(records, at='2025-10-15T08:05:00Z')['rates']['BTCUSDT']['rate_fraction'] == '0.00003973'


@pytest.mark.parametrize('rate', ['NaN', 'Infinity', 'bad', True, None])
def test_invalid_known_rates_fail_explicitly(rate):
    with pytest.raises(ValueError, match='RATE_INVALID'): view([record(rate=rate)])


def test_same_timestamp_conflict_and_wrong_venue_cannot_be_silently_resolved():
    with pytest.raises(ValueError, match='CONFLICTING'): view([record(), record(rate='0.001')])
    with pytest.raises(ValueError, match='SOURCE_VENUE'): view([record(source='gate_public_funding_history')])
    with pytest.raises(ValueError, match='AVAILABILITY_BEFORE'): view([record(available_at='2025-10-14T23:59:00Z')])


@pytest.mark.parametrize('lag', [-1, 1.5, True])
def test_invalid_availability_policy_cannot_run(lag):
    with pytest.raises(ValueError, match='POLICY_INVALID'): view([], default_publication_lag_seconds=lag)
