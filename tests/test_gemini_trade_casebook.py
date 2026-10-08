"""Synthetic cases prove accounting/linking, not actual strategy returns."""
from copy import deepcopy
from decimal import Decimal
import json
import sqlite3

import pytest

from scripts.build_gemini_trade_casebook import build_case, build_directory, visible_market_facts
from core.replay.ai_history import digest


def fixture(*, side='LONG', fee_type='TAKER'):
    action = 'OPEN_LONG' if side=='LONG' else 'OPEN_SHORT'
    stop, target = (90, 120) if side=='LONG' else (110, 80)
    trade = {'position_id':'p', 'instrument_id':'ETHUSDT','side':side,
        'opened_at':'2025-01-01T00:00:00Z','closed_at':'2025-01-01T00:05:00Z',
        'net_pnl':'17','realized_gross_pnl':'20','fees':'3','funding_pnl':'0'}
    accepted = {'position_id':'p','action':action,'status':'ACCEPTED','order_id':'entry',
                'side':side,'stop_price':stop,'take_profit':target}
    filled = {'position_id':'p','action':action,'status':'FILLED','order_id':'entry','opened_quantity':10,
        'netted_quantity':0,'quantity':10,'fill_price':100,'fee':1,'fee_type':fee_type}
    exit = {'position_id':'p','action':'TAKE_PROFIT','status':'FILLED','order_id':'exit',
            'gross_pnl':20,'fill_price':target,'fee':2,'quantity':10}
    row = {'status':'COMPLETED','scan_key':'key','as_of':'2025-01-01T00:00:00Z',
        'decision':{'action':action,'instrument_id':'ETHUSDT','entry_price':99 if side=='LONG' else 101,
                    'order_preference':'LIMIT'},'context':{'evidence_bundle_id':'bundle','input_hash':'input'},
        'response_sha256':'response','result':{'events':[accepted],
            'execution_market_snapshots':{'ETHUSDT':{'market':{'contractSize':.1,'taker':.01}}}}}
    return trade, [accepted,filled,exit], [row]


@pytest.mark.parametrize('side', ['LONG','SHORT'])
def test_long_and_short_signed_cost_and_fee_scenario_are_distinct_from_actual_return(side):
    original = fixture(side=side)
    result = build_case(*original)
    scenario = result['initial_plan_scenario']
    assert result['net_pnl_usdt'] == '17' and result['net_outcome'] == 'WIN'
    assert scenario['entry_price_change_cost_including_latency_usdt'] == '1.0'
    assert scenario['entry_price_change_is_not_pure_exchange_slippage']
    expected_reward = '17.80' if side=='LONG' else '18.20'
    assert Decimal(scenario['target_net_usdt']) == Decimal(expected_reward)
    assert result['model_reasons_are_claims_not_verified_profit_causes']
    assert not result['gate_verified']
    assert original == fixture(side=side)


def test_limit_does_not_imply_maker_fee():
    result = build_case(*fixture())
    assert result['entry_model_proposals']['entry']['decision']['order_preference'] == 'LIMIT'
    assert result['entry_fills'][0]['fee_type'] == 'TAKER'


def observation_row(*, target='p', ownership='VERIFIED_SYSTEM', visible='p'):
    return {'status': 'COMPLETED', 'scan_key': 'managed-scan', 'as_of': '2025-01-01T00:01:00Z',
        'decision': {'action': 'HOLD', 'instrument_id': 'ETHUSDT', 'position_id': target,
                     'reason': 'complete synthetic holding claim', 'entry_condition': 'synthetic structural invalidation'},
        'context': {'evidence_bundle_id': 'managed-bundle'}, 'response_sha256': 'response',
        'effective_request_hash': 'actual-final-request',
        'effective_managed_positions': [{'position_id': visible, 'instrument_id': 'ETHUSDT',
             'side': 'LONG', 'ownership': ownership, 'mark_price': 105, 'stop_price': 90, 'take_profit': 120}],
        'result': {'events': []}}


def test_management_evidence_links_actual_visible_position_and_keeps_full_claim():
    trade, events, rows = fixture()
    original = deepcopy(rows)
    held = observation_row()
    rows.append(held)
    case = build_case(trade, events, rows)
    scans = case['position_visible_model_scans']
    assert len(scans) == 1 and scans[0]['decision'] == held['decision']
    assert scans[0]['effective_model_request_hash'] == 'actual-final-request'
    assert scans[0]['decision_explicitly_targets_this_position'] is True
    assert scans[0]['visible_position']['stop_price'] == 90
    assert rows == original + [held]


@pytest.mark.parametrize('ownership,visible', [('EXTERNAL_OR_UNVERIFIED', 'p'), ('VERIFIED_SYSTEM', 'other')])
def test_time_overlap_or_external_position_does_not_fabricate_management_binding(ownership, visible):
    trade, events, rows = fixture()
    rows.append(observation_row(ownership=ownership, visible=visible))
    assert build_case(trade, events, rows)['position_visible_model_scans'] == []


def test_position_visibility_is_not_implicitly_a_targeted_hold_or_update():
    trade, events, rows = fixture()
    row = observation_row(target='other')
    row['decision'].update(action='UPDATE_PROTECTION', new_stop_price=98)
    row['result']['events'] = [{'position_id': 'other', 'action': 'UPDATE_PROTECTION', 'status': 'ACCEPTED'}]
    rows.append(row)
    scan = build_case(trade, events, rows)['position_visible_model_scans'][0]
    assert scan['decision_explicitly_targets_this_position'] is False
    assert scan['position_execution_events'] == []


def test_holding_market_facts_belong_to_visible_position_even_if_action_targets_btc():
    trade, events, rows = fixture()
    row = observation_row(target='btc-position')
    row['decision']['instrument_id'] = 'BTCUSDT'
    inputs = {'market_snapshots': {'ETHUSDT': {'mark_price': 105}, 'BTCUSDT': {'mark_price': 50000}},
        'technical_context': {'price_action_encoding': {'times': ['holding-time']},
            'ETHUSDT': {'timeframes': {'15m': {'price_action': {'bos': {'st': 'INVALIDATED'}}}}},
            'BTCUSDT': {'timeframes': {'15m': {'price_action': {'bos': {'st': 'ACTIVE'}}}}}}}
    row['effective_market_facts_by_instrument'] = {
        symbol: visible_market_facts(inputs, symbol) for symbol in ('ETHUSDT', 'BTCUSDT')}
    rows.append(row)
    scan = build_case(trade, events, rows)['position_visible_model_scans'][0]
    facts = scan['effective_model_visible_position_facts']
    assert facts['instrument_id'] == 'ETHUSDT'
    assert facts['market_snapshot'] == {'mark_price': 105}
    assert facts['technical_context']['timeframes']['15m']['price_action']['bos']['st'] == 'INVALIDATED'
    assert scan['decision_explicitly_targets_this_position'] is False
    facts['technical_context']['timeframes']['15m']['price_action']['bos']['st'] = 'MUTATED'
    assert inputs['technical_context']['ETHUSDT']['timeframes']['15m']['price_action']['bos']['st'] == 'INVALIDATED'
    assert row['effective_market_facts_by_instrument']['ETHUSDT']['technical_context']['timeframes']['15m']['price_action']['bos']['st'] == 'INVALIDATED'


def test_unknown_holding_structure_is_null_not_copied_from_entry_or_another_symbol():
    trade, events, rows = fixture()
    rows[0]['effective_entry_facts'] = {'instrument_id': 'ETHUSDT', 'technical_context': {'entry': 'known'}}
    row = observation_row()
    row['effective_market_facts_by_instrument'] = {'ETHUSDT': visible_market_facts(
        {'technical_context': {'BTCUSDT': {'future': 'unrelated'}}}, 'ETHUSDT')}
    rows.append(row)
    facts = build_case(trade, events, rows)['position_visible_model_scans'][0]['effective_model_visible_position_facts']
    assert facts['technical_context'] is None
    assert facts['market_snapshot'] is None
    assert facts['price_action_encoding'] is None


def test_mismatched_position_market_projection_is_refused():
    trade, events, rows = fixture()
    row = observation_row()
    row['effective_market_facts_by_instrument'] = {'ETHUSDT': {'instrument_id': 'BTCUSDT'}}
    rows.append(row)
    with pytest.raises(ValueError, match='CASE_VISIBLE_POSITION_MARKET_IDENTITY_INVALID'):
        build_case(trade, events, rows)


def test_directory_uses_final_repair_inputs_for_holding_structure(tmp_path, monkeypatch):
    """Synthetic audit fixture exercises projection wiring, not model/profit PASS."""
    import scripts.build_gemini_trade_casebook as module
    trade, events, entry_rows = fixture()
    holding = observation_row(target='other')
    holding['decision']['instrument_id'] = 'BTCUSDT'
    entry = entry_rows[0]
    final_inputs = {'account_truth': {'managed_positions': holding['effective_managed_positions']},
        'market_snapshots': {'ETHUSDT': {'mark_price': 105}, 'BTCUSDT': {'mark_price': 50000}},
        'technical_context': {'price_action_encoding': {'times': ['final-holding-time']},
            'ETHUSDT': {'timeframes': {'15m': {'price_action': {'bos': {'st': 'INVALIDATED'}}}}}}}
    initial_inputs = deepcopy(final_inputs)
    initial_inputs['technical_context']['ETHUSDT']['timeframes']['15m']['price_action']['bos']['st'] = 'ACTIVE'
    plan = {'templates': [], 'pilot_windows': [{'id': 'pilot-1', 'partition': 'optimization'}]}
    (tmp_path/'research-plan.json').write_text(json.dumps({'plan': plan, 'plan_sha256': digest(plan)}), encoding='utf-8')
    target = tmp_path/'pilot-1'
    target.mkdir()
    with sqlite3.connect(target/'results.sqlite3') as db:
        db.execute('CREATE TABLE ai_template_replay_runs(run_id,status,config_json,checkpoint_json)')
        db.execute('INSERT INTO ai_template_replay_runs VALUES(?,?,?,?)', ('synthetic', 'COMPLETED',
            json.dumps({'templates': []}), json.dumps({'accounts': {'price_action_structure': {'state': {
                'completed_trades': [trade], 'events': events}}}})))
        db.execute('CREATE TABLE ai_template_replay_decisions(status,scan_key,as_of,template_id,decision_json,context_json,result_json,response_sha256)')
        for row in (entry, holding):
            db.execute('INSERT INTO ai_template_replay_decisions VALUES(?,?,?,?,?,?,?,?)', (
                row['status'], row['scan_key'], row['as_of'], 'price_action_structure',
                json.dumps(row['decision']), json.dumps(row['context']), json.dumps(row['result']), row['response_sha256']))
        db.execute('CREATE TABLE evidence_bundles(bundle_id,payload_json)')
        db.executemany('INSERT INTO evidence_bundles VALUES(?,?)', [
            ('bundle', json.dumps({'initial_inputs': {}})),
            ('managed-bundle', json.dumps({'initial_inputs': initial_inputs}))])
    monkeypatch.setattr(module, 'audit_snapshot', lambda *args, **kwargs: {
        'scope': 'SYNTHETIC_AUDIT_FIXTURE', 'status': 'PASS', 'completed_decisions': 2})
    def effective_request(ctx, original_bundle, load_bundle):
        assert original_bundle == load_bundle(ctx['evidence_bundle_id'])
        is_holding = ctx['evidence_bundle_id'] == 'managed-bundle'
        if is_holding:
            assert original_bundle['initial_inputs'] == initial_inputs
        wrapper = {'previous_decision': holding['decision'], 'inputs': final_inputs} if is_holding else {}
        return {'request_hash': 'final-holding-hash' if is_holding else 'entry-hash',
            'messages': [{'role': 'system', 'content': ''}, {'role': 'user', 'content': json.dumps(wrapper)}]}
    monkeypatch.setattr(module, 'audit_effective_request', effective_request)
    result = build_directory(tmp_path)
    scan = result['cases'][0]['position_visible_model_scans'][0]
    assert result['casebook_schema_version'] == 3
    assert scan['effective_model_request_hash'] == 'final-holding-hash'
    facts = scan['effective_model_visible_position_facts']
    assert facts['instrument_id'] == 'ETHUSDT'
    assert facts['price_action_encoding']['times'] == ['final-holding-time']
    assert facts['technical_context']['timeframes']['15m']['price_action']['bos']['st'] == 'INVALIDATED'


@pytest.mark.parametrize('defect', ['duplicate_position', 'instrument', 'side', 'duplicate_scan'])
def test_ambiguous_management_position_binding_is_rejected(defect):
    trade, events, rows = fixture()
    row = observation_row()
    if defect == 'duplicate_position': row['effective_managed_positions'] *= 2
    elif defect == 'instrument': row['effective_managed_positions'][0]['instrument_id'] = 'BTCUSDT'
    elif defect == 'side': row['effective_managed_positions'][0]['side'] = 'SHORT'
    rows.append(row)
    if defect == 'duplicate_scan': rows.append(deepcopy(row))
    with pytest.raises(ValueError, match='CASE_VISIBLE_POSITION_BINDING_INVALID'):
        build_case(trade, events, rows)


def test_partial_entry_keeps_all_fills_without_inventing_a_single_entry_scenario():
    trade, events, rows = fixture()
    partial = deepcopy(events[1])
    events[1].update(status='PARTIAL_FILL', opened_quantity=5, quantity=5)
    partial.update(opened_quantity=5, quantity=5)
    events.insert(2,partial)
    result = build_case(trade,events,rows)
    assert len(result['entry_fills']) == 2
    assert result['initial_plan_scenario'] is None


@pytest.mark.parametrize('defect', ['net','missing_proposal','duplicate_proposal','instrument','action','missing_exit'])
def test_unbound_or_tampered_case_is_not_exported(defect):
    trade, events, rows = fixture()
    if defect=='net': trade['net_pnl']='25'
    elif defect=='missing_proposal': rows=[]
    elif defect=='duplicate_proposal': rows.append(deepcopy(rows[0]))
    elif defect=='instrument': rows[0]['decision']['instrument_id']='BTCUSDT'
    elif defect=='action': rows[0]['decision']['action']='OPEN_SHORT'
    elif defect=='missing_exit': events.pop()
    with pytest.raises(ValueError,match='CASE_'): build_case(trade,events,rows)


def test_gross_gain_after_fees_can_be_a_net_loss():
    trade, events, rows = fixture()
    trade.update(realized_gross_pnl='1', fees='3', net_pnl='-2')
    assert build_case(trade,events,rows)['net_outcome'] == 'LOSS'


@pytest.mark.parametrize('phase',['validation','untouched_test'])
def test_casebook_cannot_use_heldout_returns_to_tune(tmp_path,phase):
    plan={'templates':[], 'pilot_windows':[{'id':'do-not-read','partition':phase}]}
    (tmp_path/'research-plan.json').write_text(json.dumps({'plan':plan,'plan_sha256':digest(plan)}),encoding='utf-8')
    with pytest.raises(ValueError,match='NO_HELDOUT_TUNING'): build_directory(tmp_path)


@pytest.mark.parametrize('failed', [True, False])
def test_finished_schedule_with_failed_call_retains_denominator_and_never_claims_complete_execution(tmp_path,monkeypatch,failed):
    import scripts.build_gemini_trade_casebook as module
    plan={'templates':[], 'pilot_windows':[{'id':'pilot-1','partition':'optimization'}]}
    (tmp_path/'research-plan.json').write_text(json.dumps({'plan':plan,'plan_sha256':digest(plan)}),encoding='utf-8')
    target=tmp_path/'pilot-1'
    target.mkdir()
    db=sqlite3.connect(target/'results.sqlite3')
    db.execute('CREATE TABLE ai_template_replay_runs(run_id,status,config_json,checkpoint_json)')
    db.execute('INSERT INTO ai_template_replay_runs VALUES(?,?,?,?)', ('fixture','COMPLETED',
        json.dumps({'templates':[]}), json.dumps({'accounts':{}})))
    db.execute('CREATE TABLE ai_template_replay_decisions(status,decision_json,context_json,result_json)')
    db.execute('INSERT INTO ai_template_replay_decisions VALUES(?,?,?,?)', ('COMPLETED','{}','{}','{}'))
    db.execute('INSERT INTO ai_template_replay_decisions VALUES(?,?,?,?)',
               ('ERROR' if failed else 'COMPLETED','{}','{}','{}'))
    db.commit()
    db.close()
    def audit(snapshot,manifest,*,complete):
        assert complete is (not failed)
        return {'scope':'PARTIAL_LEDGER_NOT_FINAL_RETURNS' if failed else 'COMPLETE_LEDGER',
                'status':'PASS','completed_decisions':1 if failed else 2}
    monkeypatch.setattr(module,'audit_snapshot',audit)
    result=build_directory(tmp_path)
    assert result['audits'][0]['recorded_row_statuses'].get('ERROR',0) == int(failed)
    assert result['audits'][0]['complete_execution_audit'] is (not failed)
    assert result['case_count']==0 and not result['profit_acceptance_passed']
