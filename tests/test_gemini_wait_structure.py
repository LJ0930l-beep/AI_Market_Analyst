"""Isolated unit records are not real market or profitability evidence."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
from scripts.review_gemini_wait_structure import describe_wait,structure_state,candidate_summary,visible_markets,MARKET_COLUMNS
from scripts import review_gemini_research_cases as review
from tests.test_gemini_case_review import fixture

def visible():
    return {'allowed_instruments':['BTCUSDT'],
        'account_truth':{'status':'AVAILABLE','managed_positions':[],'owned_entry_orders':[]},
        'technical_context':{'BTCUSDT':{'timeframes':{
            '15m':{'price_action':{'bos':{'state':'ACTIVE'},'breakout_retest':{'state':'ACTIVE'}}},
            '1h':{'price_action':{'bos':{'state':'INVALIDATED'},'breakout_retest':None}}}}}}
def decision():
    return {'instrument_id':'BTCUSDT','strategy_analysis':{'missing_conditions':['等待二次确认，趋势冲突']}}
def report():
    return {'schema_version':'actual_wait_structure_review_v1','plan_sha256':'plan',
        'windows':[{'window':'pilot-1','row_statuses':{'COMPLETED':1,'ERROR':2}}],
        'wait_records_checked':1,'group_columns':['window','template_id','account_scope',
            '15m_bos','15m_retest','1h_bos','1h_retest','count'],
        'group_rows':[['pilot-1','price_action_structure','SYSTEM_FLAT','ACTIVE','ACTIVE','INVALIDATED','ABSENT',1]],
        'mention_columns':['window','template_id','label','count'],
        'mention_rows':[['pilot-1','price_action_structure','confirmation',1],
            ['pilot-1','price_action_structure','trend_conflict',1]],
        'mentions_overlap_not_additive':True,'active_retest_does_not_prove_full_setup_or_required_entry':True,
        'account_scope_definition':'SYSTEM_OWNED_POSITION_OR_ENTRY_ORDER_NOT_ALL_ACCOUNT_EXPOSURE',
        'records':[{'window':'pilot-1','template_id':'price_action_structure','scan_key':'scan',
            'account_scope':'SYSTEM_FLAT','visible_structure_states':['ACTIVE','ACTIVE','INVALIDATED','ABSENT'],
            'model_declared_missing_conditions':['等待二次确认，趋势冲突']}]}

def market_report():
    value=report();record=value['records'][0]
    record.update(instrument_id='BTCUSDT',allowed_instruments=['BTCUSDT','ETHUSDT'],
        visible_market_states=[['BTCUSDT','READY','ACTIVE','ACTIVE','READY','INVALIDATED','ABSENT'],
                               ['ETHUSDT','ABSENT','ABSENT','ABSENT','ABSENT','ABSENT','ABSENT']])
    value.update(market_group_columns=MARKET_COLUMNS,
        market_coverage_does_not_prove_model_comparison_or_trade_opportunity=True,
        market_group_rows=[[record['window'],record['template_id'],m[0],m[0]==record['instrument_id'],*m[1:],1]
                           for m in record['visible_market_states']])
    return value


def test_all_allowed_markets_include_missing_context_and_ignore_metadata_columns():
    value=visible();value['allowed_instruments'].append('ETHUSDT')
    value['technical_context']['candle_columns']=['open','close']
    rows=visible_markets(value)
    assert len(rows)==2 and rows[0][0]=='BTCUSDT'
    assert rows[1]==['ETHUSDT',*(['ABSENT']*6)]
    assert rows[0][1]=='UNKNOWN'  # no fabricated READY when source omits status


@pytest.mark.parametrize('change', ['duplicate','wrong_market_type','wrong_frames','wrong_frame','wrong_summary'])
def test_market_input_shapes_not_silently_dropped(change):
    value=visible()
    if change=='duplicate':value['allowed_instruments']=['BTCUSDT','BTCUSDT']
    if change=='wrong_market_type':value['technical_context']['BTCUSDT']=[]
    if change=='wrong_frames':value['technical_context']['BTCUSDT']['timeframes']=[]
    if change=='wrong_frame':value['technical_context']['BTCUSDT']['timeframes']['15m']=[]
    if change=='wrong_summary':value['technical_context']['BTCUSDT']['timeframes']['15m']['price_action']=[]
    with pytest.raises(ValueError,match='WAIT_STRUCTURE_'):visible_markets(value)


def test_market_summary_counts_instrument_snapshots_not_unique_scans():
    value=market_report();summary=candidate_summary(value,'plan')
    assert summary['wait_records_checked']==1
    assert sum(r[-1] for r in summary['market_group_rows'])==2
    assert summary['market_group_rows'][0][3] is True
    assert summary['market_group_rows'][1][3] is False
    assert summary['market_coverage_does_not_prove_model_comparison_or_trade_opportunity'] is True
    assert summary['missing_condition_fact_rows'][0][-2:] == ['等待二次确认，趋势冲突',1]
    assert summary['missing_condition_frequencies_overlap_not_additive'] is True


def test_distinct_exact_conditions_retained_per_scan_without_duplicate_inflation():
    value=market_report();record=value['records'][0]
    record['model_declared_missing_conditions']=['等待二次确认，趋势冲突','等待二次确认，趋势冲突','确认回踩收盘']
    # mention_rows are scan-level label flags, unaffected by repeated text.
    summary=candidate_summary(value,'plan')
    facts=summary['missing_condition_fact_rows']
    assert len(facts)==2
    assert {r[-2] for r in facts} == set(record['model_declared_missing_conditions'])
    assert all(r[-1]==1 for r in facts)


@pytest.mark.parametrize('change', ['allowed','missing_market','duplicate_market','selected','count_bool','selected_int','state','caveat','columns'])
def test_changed_market_binding_and_claims_rejected(change):
    value=market_report();record=value['records'][0]
    if change=='allowed':record['allowed_instruments'].append('SOLUSDT')
    if change=='missing_market':record['visible_market_states'].pop()
    if change=='duplicate_market':record['visible_market_states'][1][0]='BTCUSDT'
    if change=='selected':record['instrument_id']='SOLUSDT'
    if change=='count_bool':value['market_group_rows'][0][-1]=True
    if change=='selected_int':value['market_group_rows'][0][3]=1
    if change=='state':value['market_group_rows'][1][5]='ACTIVE'
    if change=='caveat':value.pop('market_coverage_does_not_prove_model_comparison_or_trade_opportunity')
    if change=='columns':value['market_group_columns']=[]
    with pytest.raises(ValueError,match='WAIT_STRUCTURE_MARKET_'):candidate_summary(value,'plan')


def test_model_receives_both_selected_and_unselected_market_facts():
    evidence,book,waits=fixture();messages,_=review.review_request(evidence,book,waits,wait_structure=market_report())
    payload=json.loads(messages[1]['content'])['actual_visible_wait_structure']
    assert len(payload['market_group_rows'])==2
    assert payload['market_group_rows'][1][2]=='ETHUSDT'
    assert '数据可见不证明模型完成跨币种比较' in messages[0]['content']


def test_independent_candidate_auditor_rebuilds_market_coverage(tmp_path,monkeypatch):
    from scripts.audit_gemini_candidate_provenance import audit_directory
    candidate_directory_with_structure(tmp_path,monkeypatch,include_market=True)
    assert audit_directory(tmp_path)['status']=='CANDIDATE_PROVENANCE_PASS_NOT_STRATEGY_OR_PROFIT_ACCEPTANCE'


def test_market_report_tamper_is_not_accepted_by_provenance(tmp_path,monkeypatch):
    from scripts.audit_gemini_candidate_provenance import audit_directory
    candidate_directory_with_structure(tmp_path,monkeypatch,include_market=True)
    path=tmp_path/'wait-structure-review.json';value=json.loads(path.read_text(encoding='utf-8'))
    value['market_group_rows'][1][5]='ACTIVE'
    path.write_text(json.dumps(value),encoding='utf-8')
    with pytest.raises(ValueError):audit_directory(tmp_path)

def test_active_basic_retest_is_only_a_fact_and_account_is_flat():
    scope,states,labels=describe_wait(decision(),visible())
    assert scope=='SYSTEM_FLAT' and states==['ACTIVE','ACTIVE','INVALIDATED','ABSENT']
    assert labels==['confirmation','trend_conflict']

@pytest.mark.parametrize('kind',['position','entry'])
def test_verified_system_management_separated_from_flat(kind):
    value=visible()
    if kind=='position':value['account_truth']['managed_positions']=[{'ownership':'VERIFIED_SYSTEM'}]
    else:value['account_truth']['owned_entry_orders']=[{'ownership':'SYSTEM_ORDER_ID_MATCH','order_id':'order'}]
    assert describe_wait(decision(),value)[0]=='SYSTEM_MANAGED'

def test_missing_account_not_inferred_flat():
    value=visible();value['account_truth']['status']='UNAVAILABLE'
    assert describe_wait(decision(),value)[0]=='UNKNOWN_ACCOUNT'

def test_unverified_external_position_not_system_management():
    value=visible();value['account_truth']['managed_positions']=[{'ownership':'EXTERNAL'}]
    assert describe_wait(decision(),value)[0]=='SYSTEM_FLAT'

def test_outside_actual_visible_coin_rejected():
    value=decision();value['instrument_id']='ETHUSDT'
    with pytest.raises(ValueError,match='VISIBLE_INSTRUMENT'):describe_wait(value,visible())

@pytest.mark.parametrize('value',[0,True,[], 'ACTIVE'])
def test_malformed_event_not_treated_as_absent(value):
    with pytest.raises(ValueError,match='EVENT_TYPE'):structure_state(value)

def test_gap_nonstring_is_rejected():
    value=decision();value['strategy_analysis']['missing_conditions']=[1]
    with pytest.raises(ValueError,match='GAP_LIST'):describe_wait(value,visible())

def test_group_missingness_overlap_and_error_denominator_preserved():
    summary=candidate_summary(report(),'plan')
    assert summary['group_rows'][0][6]=='ABSENT'
    assert summary['windows'][0]['row_statuses']['ERROR']==2
    assert summary['mentions_overlap_not_additive'] is True
    assert len(summary['mention_rows'])==2 and summary['wait_records_checked']==1

@pytest.mark.parametrize('change,match',[
    ('plan','PLAN_MISMATCH'),('count','DENOMINATOR'),('group','GROUPS_CHANGED'),
    ('mention','MENTIONS_CHANGED'),('caveat','CAUSAL_CAVEAT')])
def test_changed_candidate_facts_or_removed_caveat_rejected(change,match):
    value=report()
    if change=='plan':value['plan_sha256']='other'
    if change=='count':value['group_rows'][0][-1]=True
    if change=='group':value['group_rows'][0][4]='INVALIDATED'
    if change=='mention':value['mention_rows'][0][-1]=2
    if change=='caveat':value['active_retest_does_not_prove_full_setup_or_required_entry']=False
    with pytest.raises(ValueError,match=match):candidate_summary(value,'plan')

def test_actual_wait_rows_are_bound_into_model_request_without_causal_claim():
    evidence,book,waits=fixture();value=report()
    messages,_=review.review_request(evidence,book,waits,wait_structure=value)
    payload=json.loads(messages[1]['content'])
    assert 'wait_structure_review_sha256' in payload
    assert payload['actual_visible_wait_structure']['wait_records_checked']==1
    assert 'ACTIVE基础回踩不等于完整机会' in messages[0]['content']

def candidate_directory_with_structure(tmp_path,monkeypatch,include_market=False):
    import hashlib
    from core.replay.ai_history import digest
    from core.trading.ai_session_coordinator import _estimate_tokens
    from scripts import analyze_gemini_research as analysis
    from scripts import review_gemini_wait_structure as observer
    from scripts.audit_gemini_candidate_provenance import output_binding
    from tests.test_gemini_candidate_provenance import complete_directory
    artifact,book=complete_directory(tmp_path,monkeypatch)
    structure=market_report() if include_market else report();structure['plan_sha256']='synthetic-plan'
    monkeypatch.setattr(observer,'review_directory',lambda _:deepcopy(structure))
    waits=json.loads((tmp_path/'wait-cause-review.json').read_text(encoding='utf-8'))
    costs=json.loads((tmp_path/'closed-cost-review.json').read_text(encoding='utf-8'))
    geometry=json.loads((tmp_path/'entry-geometry-review.json').read_text(encoding='utf-8'))
    messages,_=review.review_request(analysis.optimization_evidence(tmp_path),book,waits,costs,geometry,structure)
    artifact['request_messages']=messages
    artifact['wait_structure_review_sha256']=digest(structure)
    artifact['wait_structure_script_sha256']=hashlib.sha256(Path(observer.__file__).read_bytes()).hexdigest()
    artifact['review_estimated_total_tokens']=sum(_estimate_tokens(m['content']) for m in messages)+review.OUTPUT_TOKENS+256
    receipt=artifact['receipt'];receipt['input_hash']=hashlib.sha256(json.dumps(messages,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    receipt['transport_trace']['wire_messages_sha256']=hashlib.sha256(json.dumps({'messages':messages},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    artifact['response_binding']=output_binding(artifact['review'],artifact['raw_response'],receipt,messages)
    (tmp_path/'wait-structure-review.json').write_text(json.dumps(structure),encoding='utf-8')
    (tmp_path/'strategy-candidates.json').write_text(json.dumps(artifact),encoding='utf-8')

def test_candidate_auditor_rebuilds_actual_wait_structure_input(tmp_path,monkeypatch):
    from scripts.audit_gemini_candidate_provenance import audit_directory
    candidate_directory_with_structure(tmp_path,monkeypatch)
    assert audit_directory(tmp_path)['status']=='CANDIDATE_PROVENANCE_PASS_NOT_STRATEGY_OR_PROFIT_ACCEPTANCE'

@pytest.mark.parametrize('defect',['saved_structure','artifact_hash','script_hash','remove_binding'])
def test_wait_structure_candidate_tampering_rejected(tmp_path,monkeypatch,defect):
    from scripts.audit_gemini_candidate_provenance import audit_directory
    candidate_directory_with_structure(tmp_path,monkeypatch)
    path=tmp_path/('wait-structure-review.json' if defect=='saved_structure' else 'strategy-candidates.json')
    value=json.loads(path.read_text(encoding='utf-8'))
    if defect=='saved_structure':value['group_rows'][0][4]='INVALIDATED'
    elif defect=='artifact_hash':value['wait_structure_review_sha256']='changed'
    elif defect=='script_hash':value['wait_structure_script_sha256']='changed'
    else:value.pop('wait_structure_review_sha256')
    path.write_text(json.dumps(value),encoding='utf-8')
    with pytest.raises(ValueError):audit_directory(tmp_path)

@pytest.mark.parametrize('defect',['columns','overlap'])
def test_relabelled_timeframe_or_additive_gap_claim_rejected(defect):
    value=report()
    if defect=='columns':value['group_columns'][3]='1h_bos'
    else:value['mentions_overlap_not_additive']=False
    with pytest.raises(ValueError):candidate_summary(value,'plan')
