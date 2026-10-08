"""Synthetic arithmetic fixtures, never real trading acceptance."""
from copy import deepcopy
from decimal import Decimal
import pytest
from scripts.review_gemini_ten_trade_quality import ten_trade_quality


def book(nets, window='pilot-1'):
    return {'case_count':len(nets),'cases':[{'case_id':str(i),'template_id':'price_action_structure',
        'window':window,'closed_at':f'2025-10-15T00:{i:02d}:00Z','net_pnl_usdt':str(n)}
        for i,n in enumerate(nets)]}


def test_six_tiny_wins_can_still_lose_and_must_not_be_profit_acceptance():
    b=book(['.01']*6+['-2']*4); original=deepcopy(b)
    result=ten_trade_quality(b); row=result['rows'][0]
    assert b==original
    assert row['blocks_with_at_least_six_net_wins']==1
    assert Decimal(row['aggregate_net_pnl_usdt'])==Decimal('-7.94')
    assert row['average_net_win_usdt']=='.01' or Decimal(row['average_net_win_usdt'])==Decimal('.01')
    assert not result['future_six_wins_in_every_ten_guaranteed']


@pytest.mark.parametrize('nets',[[],[1]*9,[0]*9])
def test_insufficient_sample_never_vacuously_passes(nets):
    result=ten_trade_quality(book(nets))
    for row in result['rows']:
        assert row['all_observed_ten_sequences_meet_six_wins'] is None
        assert row['nonoverlapping_ten_blocks']==0


def test_zero_net_is_not_win_and_all_overlapping_sequences_are_retained():
    row=ten_trade_quality(book([1]*6+[0]*4+[-2]))['rows'][0]
    assert row['consecutive_ten_sequences']==2
    assert row['sequences_with_at_least_six_net_wins']==1
    assert row['minimum_net_wins_in_ten']==5
    assert row['nonoverlapping_ten_blocks']==1


def test_cannot_bridge_separate_reset_account_windows():
    b=book([1]*10)
    for case in b['cases'][5:]: case['window']='pilot-2'
    assert all(r['consecutive_ten_sequences']==0 for r in ten_trade_quality(b)['rows'])


@pytest.mark.parametrize('defect',['NaN','Infinity','duplicate','naive_time','count'])
def test_corrupt_book_rejected(defect):
    b=book([1]*10)
    if defect in ('NaN','Infinity'): b['cases'][0]['net_pnl_usdt']=defect
    elif defect=='duplicate': b['cases'][0]['case_id']=b['cases'][1]['case_id']
    elif defect=='naive_time': b['cases'][0]['closed_at']='2025-10-15T00:00:00'
    else: b['case_count']=9
    with pytest.raises(ValueError,match='TEN_TRADE_'): ten_trade_quality(b)


def test_legacy_unidentified_windows_are_unavailable_not_pooled():
    result=ten_trade_quality(book([1]*10,window=None))
    assert result['window_identity_unavailable_cases']==10 and result['rows']==[]


def test_real_candidate_prompt_binds_user_profit_quality_goal():
    from tests.test_gemini_case_review import fixture
    from scripts.review_gemini_research_cases import review_request
    import json
    evidence,casebook,waits=fixture()
    identity='price_action_structure'
    evidence['styles']=[s for s in evidence['styles'] if s['template_id']==identity]
    for window in evidence['windows']:
        window['strategies']=[s for s in window['strategies'] if s['template_id']==identity]
    casebook['cases']=[c for c in casebook['cases'] if c['template_id']==identity]
    casebook['case_count']=len(casebook['cases'])
    messages,_=review_request(evidence,casebook)
    payload=json.loads(messages[1]['content'])
    assert payload['operator_objective']['future_guarantee'] is False
    assert payload['operator_objective']['closed_net_win_target']=='AT_LEAST_SIX_PROFITABLE_COMPLETE_CLOSES_PER_TEN'
    assert '结构失效及时退出' in messages[0]['content']
    assert '赚一点就平' in messages[0]['content']
    assert payload['ten_trade_quality']['case_count']==casebook['case_count']
