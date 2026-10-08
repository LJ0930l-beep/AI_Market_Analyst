"""Input comparison fixtures must not imply real funding feed availability."""
import json

import pytest

from core.replay.ai_history import digest
from scripts.audit_gemini_funding_visibility import audit_directory, inspect_scan, visible_rate_fields


def test_cost_totals_unknown_and_boolean_rate_values_are_not_visible_market_rates():
    fields = visible_rate_fields({'market_snapshots':{'BTCUSDT':{'funding_pnl':0,'fundingRate':None}},
        'market_radar':{'funding_rate_pct':True,'status':'UNAVAILABLE'},
        'performance':{'fundingRate':.01}})
    assert fields == []


def test_zero_rate_is_valid_and_named_matrix_columns_are_examined_without_inferring_oi():
    fields = visible_rate_fields({'market_radar':{'derivatives_matrix_columns':'symbol,gate_funding_pct,gate_oi_pct,binance_funding_pct',
        'derivatives_matrix':[['BTCUSDT',None,10,0]]}})
    assert len(fields) == 1 and fields[0]['value'] == '0'
    assert fields[0]['path'][-1] == 'binance_funding_pct'


def test_available_archive_not_in_effective_prompt_is_distinct_from_no_known_rate():
    inputs={'allowed_instruments':['BTCUSDT'], 'market_snapshots':{'BTCUSDT':{'price':100}}}
    history={'assumptions':{'data_venue':'binance'},'funding':[{'instrument_id':'BTCUSDT',
        'payment_time':'2025-10-15T00:00:00Z','rate':.0001,'source':'binance_official_funding_archive'}]}
    gap=inspect_scan(inputs,history,'2025-10-15T00:05:00Z',{'action':'WAIT'})
    assert gap['wait_with_archive_rate_but_no_visible_rate_field']
    assert not inspect_scan(inputs,history,'2025-10-15T00:00:30Z',{'action':'WAIT'})['archive_rate_present_but_no_numeric_rate_field']
    assert not inspect_scan(inputs,history,'2025-10-15T00:05:00Z',{'action':'OPEN_LONG'})['wait_with_archive_rate_but_no_visible_rate_field']


@pytest.mark.parametrize('partition',['validation','untouched_test'])
def test_observer_cannot_read_heldout_data_to_prepare_strategy_changes(tmp_path,partition):
    plan={'pilot_windows':[{'partition':partition,'id':'never-read'}]}
    (tmp_path/'research-plan.json').write_text(json.dumps({'plan':plan,'plan_sha256':digest(plan)}),encoding='utf-8')
    with pytest.raises(ValueError,match='OPTIMIZATION_ONLY'): audit_directory(tmp_path)
