"""Offline classification/immutability fixtures; no real model or profitability proof."""
from copy import deepcopy
import pytest
from scripts.audit_gemini_execution_protocol import prefix_issues,wait_repair_unchanged,completed_transport_proof


def scheduled():
    expected=[('fixture',str(i)) for i in range(50)]
    rows=[{'template_id':name,'as_of':when,'status':'COMPLETED'} for name,when in expected]
    return rows,expected


def completed_stream():
    return {'status':'COMPLETED','transport_trace':{'phase':'COMPLETED','transport_mode':'SSE',
        'stream_done':True,'stream_finish_reason':'stop','stream_event_count':2,
        'elapsed_ms':100,'first_stream_event_ms':10,'first_content_ms':20}}


def test_complete_stream_proof_is_distinct_from_partial_json_or_legacy_json_transport():
    assert completed_transport_proof(completed_stream())['stream_done'] is True
    assert completed_transport_proof({'status':'COMPLETED','transport_trace':{'phase':'COMPLETED'}})=={'phase':'COMPLETED'}


@pytest.mark.parametrize('key,value',[('stream_done',False),('stream_finish_reason','length'),
    ('stream_event_count',True),('stream_event_count',0),('first_content_ms',101),
    ('first_content_ms',float('nan')),('first_stream_event_ms',21)])
def test_independent_protocol_audit_rejects_incomplete_or_inconsistent_stream(key,value):
    attempt=completed_stream()
    attempt['transport_trace'][key]=value
    with pytest.raises(ValueError,match='PROTOCOL_'):
        completed_transport_proof(attempt)


def test_only_fifty_native_completed_rows_have_no_prefix_issues():
    rows,expected=scheduled()
    assert not prefix_issues(rows,expected)
    assert 'FEWER_THAN_50_NATIVE_SCANS' in prefix_issues(rows[:49],expected)


def test_error_denominator_is_preserved_and_later_rows_cannot_replace_failure():
    rows,expected=scheduled()
    rows[20]['status']='ERROR'
    rows.append({'template_id':'fixture','as_of':'50','status':'COMPLETED'})
    assert 'ERROR_IN_FIRST_50' in prefix_issues(rows,expected)


def test_nonterminal_claim_does_not_become_a_successful_scan():
    rows,expected=scheduled()
    rows[-1]['status']='MODEL_STARTED'
    assert 'NONTERMINAL_OR_HALTED_SCAN_IN_FIRST_50' in prefix_issues(rows,expected)


def test_calendar_scan_cannot_be_replaced_with_another_scan():
    rows,expected=scheduled()
    rows[-1]['as_of']='different'
    assert 'NATIVE_SCAN_PREFIX_MISMATCH' in prefix_issues(rows,expected)


def waiting():
    return {'action':'WAIT','instrument_id':'BTCUSDT','next_trigger_price':100,
        'strategy_analysis':{'missing_conditions':['volume']},
        'timeframe_analysis':{'5m':'Original evidence '*100,'15m':'unchanged'}}


def test_independent_wait_repair_check_accepts_only_the_bounded_original_explanation():
    before=waiting()
    after=deepcopy(before)
    after['timeframe_analysis']['5m']='Original evidence summarized'
    wait_repair_unchanged(before,after,{'timeframe_analysis.5m'})


@pytest.mark.parametrize('mutation',['trigger','conditions','remove','other_period','still_long'])
def test_independent_wait_repair_check_rejects_decision_key_and_unbounded_text_drift(mutation):
    before=waiting()
    after=deepcopy(before)
    after['timeframe_analysis']['5m']='Original evidence summarized'
    if mutation=='trigger': after['next_trigger_price']=101
    if mutation=='conditions': after['strategy_analysis']['missing_conditions']=['new condition']
    if mutation=='remove': after.pop('next_trigger_price')
    if mutation=='other_period': after['timeframe_analysis']['15m']='new trend'
    if mutation=='still_long': after['timeframe_analysis']['5m']=before['timeframe_analysis']['5m']
    with pytest.raises(ValueError,match='PROTOCOL_WAIT_REPAIR'):
        wait_repair_unchanged(before,after,{'timeframe_analysis.5m'})
