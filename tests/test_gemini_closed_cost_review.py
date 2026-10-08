"""Synthetic accounting fixtures and candidate provenance, not market proof."""
from copy import deepcopy
import json

import pytest

from scripts.review_gemini_closed_costs import summarize_closed_costs
from scripts.review_gemini_research_cases import review_request


def case(index, gross, fees, funding='0', net=None):
    from decimal import Decimal
    n = Decimal(gross)-Decimal(fees)+Decimal(funding) if net is None else Decimal(net)
    return {'case_id':str(index),'template_id':'a','gross_pnl_usdt':gross,
        'fees_usdt':fees,'funding_pnl_usdt':funding,'net_pnl_usdt':str(n),
        'net_outcome':'WIN' if n>0 else 'LOSS' if n<0 else 'BREAKEVEN',
        'entry_fills':[{'fee_type':'TAKER'},{'fee_type':'MAKER'}],
        'exit_ledger_triggers':[{'action':'STOP_LOSS'},{'action':'STOP_LOSS'}]}


def book(cases):
    return {'case_count':len(cases),'cases':cases}


def expand_shared_text(value, shared_text):
    """Reconstruct the compact prompt table before asserting exact decimals."""
    if isinstance(value, dict):
        if set(value) == {'t'}:
            index = value['t']
            assert type(index) is int and 0 <= index < len(shared_text)
            return shared_text[index]
        return {key: expand_shared_text(item, shared_text) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_shared_text(item, shared_text) for item in value]
    return value


def restore_table_rows(payload, table_key):
    rows = expand_shared_text(payload[table_key], payload.get('shared_text', []))
    template_ids = payload.get('template_ids', [])
    for row in rows:
        if row and type(row[0]) is int:
            row[0] = template_ids[row[0]]
    return rows


def test_costs_preserve_exact_decimals_and_separate_gross_losses_fee_flips_and_zero_samples():
    source=book([case(1,'-2','3'),case(2,'1','2'),case(3,'-1','1','4')])
    before=deepcopy(source)
    result=summarize_closed_costs(source,['a','unused'])
    row=dict(zip(result['columns'],result['rows'][0]))
    assert row['gross_pnl_usdt']=='-2' and row['fees_usdt']=='6'
    assert row['funding_pnl_usdt']=='4' and row['net_pnl_usdt']=='-4'
    assert row['gross_loss_count']==2 and row['positive_before_fees_nonpositive_after_fees_count']==1
    assert row['closed_count']==3 and row['entry_fill_fee_type_counts']=={'TAKER':3,'MAKER':3}
    assert row['exit_fill_trigger_counts']=={'STOP_LOSS':6}
    assert result['rows'][1][1:5]==[0,0,0,0] and result['zero_sample_is_not_profit_evidence']
    assert source==before


@pytest.mark.parametrize('defect',['nan','bool','formula','outcome','negative_fee','duplicate','unknown','fee_type'])
def test_unbound_or_invalid_costs_are_rejected(defect):
    c=case(1,'1','2');b=book([c])
    if defect=='nan': c['gross_pnl_usdt']='NaN'
    elif defect=='bool': c['fees_usdt']=True
    elif defect=='formula': c['net_pnl_usdt']='99'
    elif defect=='outcome': c['net_outcome']='WIN'
    elif defect=='negative_fee': c['fees_usdt']='-1'
    elif defect=='duplicate': b['cases'].append(deepcopy(c));b['case_count']=2
    elif defect=='unknown': c['template_id']='other'
    elif defect=='fee_type': c['entry_fills'][0]['fee_type']='unverified'
    with pytest.raises(ValueError,match='CLOSED_COST_'):
        summarize_closed_costs(b,['a'])


def test_full_candidate_request_preserves_all_cost_rows_and_refuses_tampering():
    from tests.test_gemini_case_review import fixture
    from core.trading.ai_session_coordinator import _estimate_tokens
    from scripts.review_gemini_research_cases import OUTPUT_TOKENS
    evidence,cases,waits=fixture()
    ids=[s['template_id'] for s in evidence['styles']]
    for c in cases['cases']:
        c['gross_pnl_usdt']='4.000075' if c['net_outcome']=='WIN' else '1.000075'
        c['funding_pnl_usdt']='0'
        if c['net_outcome']=='BREAKEVEN':
            c['gross_pnl_usdt']=c['fees_usdt'];c['net_pnl_usdt']='0'
    costs=summarize_closed_costs(cases,ids)
    messages,_=review_request(evidence,cases,waits,costs)
    payload=json.loads(messages[1]['content'])
    assert len(payload['closed_cost_rows'])==5
    assert payload['closed_cost_columns']==costs['columns']
    restored = restore_table_rows(payload, 'closed_cost_rows')
    assert restored == costs['rows']
    assert sum(_estimate_tokens(m['content']) for m in messages)+OUTPUT_TOKENS+256<=8192
    costs['rows'][0][6]='0'
    with pytest.raises(ValueError,match='CLOSED_COST_BINDING'):
        review_request(evidence,cases,waits,costs)


def test_cost_review_with_long_decimal_amounts_fits_without_silent_rounding():
    from decimal import Decimal
    from tests.test_gemini_case_review import fixture
    evidence,cases,waits=fixture()
    for c in cases['cases']:
        n=Decimal('1.142768989830358635218131721') if c['net_outcome']=='WIN' else Decimal('-9.32994921750')
        if c['net_outcome']=='BREAKEVEN': n=Decimal(0)
        f=Decimal('3.000075');u=Decimal('0.00000001234567890123456789')
        c.update(net_pnl_usdt=str(n),fees_usdt=str(f),funding_pnl_usdt=str(u),gross_pnl_usdt=str(n+f-u))
    costs=summarize_closed_costs(cases,[s['template_id'] for s in evidence['styles']])
    messages,_=review_request(evidence,cases,waits,costs)
    data=json.loads(messages[1]['content'])
    restored = restore_table_rows(data, 'closed_cost_rows')
    assert restored == costs['rows']
