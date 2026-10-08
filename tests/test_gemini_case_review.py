"""Offline case-review provenance, full denominators and prompt budget checks."""
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
import sys

import pytest

from core.model_routing import DEFAULT_MODEL
from core.replay.ai_template_runner import frozen_templates
from core.trading.ai_session_coordinator import _estimate_tokens
from scripts import review_gemini_research_cases as review
from scripts.run_gemini_heldout_research import candidate_instructions


def fixture():
    templates = frozen_templates()
    evidence = {'plan_sha256': 'plan', 'model_id': DEFAULT_MODEL,
        'styles': [{k: t[k] for k in ('template_id', 'name', 'profile')} for t in templates],
        'windows': []}
    cases = []
    for index in range(3):
        strategies = []
        for template in templates:
            strategies.append({'template_id': template['template_id'], 'name': template['name'],
                'decision_count': 336, 'action_counts': {'WAIT': 310, 'OPEN_LONG': 26},
                'execution_counts': {'MODEL_TRANSPORT_TIMEOUT': 1}, 'roi': -.0354321234,
                'fees': 99.128129, 'funding_pnl': -1.27, 'realized_gross_pnl': -13.222,
                'unrealized_pnl': 4.61, 'closed_trade_count': 2, 'win_rate': .5,
                'profit_factor': .8, 'profit_factor_status': 'FINITE', 'max_drawdown': .08,
                'filled_entry_order_count': 3, 'accepted_entry_order_count': 4,
                'pending_order_count': 1, 'economic_eligible': True, 'halted_reason': None,
                'wait_reason_examples': [('需要进一步收盘确认，不伪造缺失数据。' * 30, 28), ('other', 3)]})
        evidence['windows'].append({'window': f'pilot-{index+1}', 'comparison_eligible': False,
                                  'errors': ['transport timeout'], 'strategies': strategies})
    for template in templates:
        for index, outcome in enumerate(('WIN', 'LOSS', 'BREAKEVEN', 'WIN', 'LOSS', 'WIN')):
            cases.append({'case_id': f'{template["template_id"]}-{index}',
                'closed_at': f'2025-10-{15+index:02}T00:00:00Z', 'template_id': template['template_id'],
                'net_outcome': outcome, 'net_pnl_usdt': '1' if outcome == 'WIN' else '-2',
                'fees_usdt': '3.000075', 'entry_model_proposals': {'entry': {'decision': {
                    'reason': '原始解释只是模型主张。' * 50, 'order_preference': 'LIMIT'}}},
                'entry_fills': [{'fee_type': 'TAKER'}],
                'exit_ledger_triggers': [{'action': 'TAKE_PROFIT' if outcome == 'WIN' else 'STOP_LOSS'}],
                'initial_plan_scenario': {'target_net_usdt': '4.222',
                    'stop_loss_including_fees_usdt': '9.75', 'net_reward_to_loss': '.43302564',
                    'entry_price_change_cost_including_latency_usdt': '.2127'}})
    casebook = {'plan_sha256': 'plan', 'case_count': len(cases), 'cases': cases}
    waits = {'plan_sha256': 'plan', 'strategies': {t['template_id']: {
        'flat_account_scans': 70, 'flat_account_open_proposals': 2, 'flat_account_waits': 68,
        'scans_with_system_position_or_pending_entry': 12,
        'missing_condition_data_mentions': {'OI': 6, 'FUNDING': 4},
        'nonessential_verbose_field': 'omit'} for t in templates}}
    return evidence, casebook, waits


def payload(messages):
    data = json.loads(messages[1]['content'])
    identities = data['template_ids']
    def decode(value):
        if type(value) is int and 0 <= value < len(identities):
            return identities[value]
        return value
    for window in data['windows']:
        column = data['strategy_metric_columns'].index('template_id')
        for row in window['strategy_metric_rows']:
            row[column] = decode(row[column])
    for style in data['styles']:
        style['template_id'] = decode(style['template_id'])
        if 'strategy_id' in data['style_profile_columns']:
            column = data['style_profile_columns'].index('strategy_id')
            style['profile_values'][column] = decode(style['profile_values'][column])
    for name in ('case_example', 'case_outcome', 'activity', 'wait_example'):
        if name+'_rows' in data:
            column = data[name+'_columns'].index('template_id')
            for row in data[name+'_rows']:
                row[column] = decode(row[column])
    return data


def test_fallback_tables_reconstruct_exact_profiles_errors_names_and_every_count():
    evidence, cases, waits = fixture()
    messages, _ = review.review_request(evidence, cases, waits)
    source = json.loads(messages[1]['content'])
    original = deepcopy(source)
    # Mix repeated and different errors, retain explicit zero and absent counts.
    source['windows'][0]['errors'] = [
        {'template_id': 0, 'as_of': '2025-10-15T00:00:00Z', 'error': 'timeout ' * 20},
        {'template_id': 1, 'as_of': '2025-10-15T01:00:00Z', 'error': 'timeout ' * 20},
        'literal diagnostic']
    source['windows'][0]['strategy_metric_rows'][0][source['strategy_metric_columns'].index('action_counts')]['HOLD'] = 0
    compact = review.compact_review_tables(source)
    texts = compact.pop('shared_text')
    def unshare(value):
        if isinstance(value, dict) and set(value) == {'t'}:
            return texts[value['t']]
        if isinstance(value, dict):
            return {k: unshare(v) for k, v in value.items()}
        if isinstance(value, list):
            return [unshare(v) for v in value]
        return value
    compact = unshare(compact)
    for s, before in zip(compact['styles'], source['styles']):
        restored = {**compact['common_style_profile'], **dict(zip(compact['style_profile_columns'], s['profile_values']))}
        assert restored == dict(zip(source['style_profile_columns'], before['profile_values']))
    names = {s['template_id']: s['name'] for s in compact['styles']}
    for w, before in zip(compact['windows'], source['windows']):
        errors = [r['literal_error'] if isinstance(r, dict) else
                  {'template_id': r[0], 'as_of': r[1], 'error': compact['error_catalog'][r[2]]}
                  for r in w['error_rows']]
        assert errors == before['errors']
        for row, old_row in zip(w['strategy_metric_rows'], before['strategy_metric_rows']):
            restored = dict(zip(compact['strategy_metric_columns'], row))
            if compact.get('metric_name_from_style'):
                restored['name'] = names[restored['template_id']]
            for key, columns in compact['nested_metric_columns'].items():
                restored[key] = {k: v for k, v in zip(columns, restored[key]) if v is not None}
            assert restored == dict(zip(source['strategy_metric_columns'], old_row))
    # No closed costs, outcomes, activity or monetary precision are transformed.
    for key in ('case_example_rows', 'case_outcome_rows', 'activity_rows', 'audited_closed_case_count'):
        assert compact[key] == source[key]
    assert original['windows'][0]['errors'] == ['transport timeout']


def receipt(messages):
    return {'model_id': DEFAULT_MODEL, 'actual_model_id': DEFAULT_MODEL,
        'verified_manifest_model_id': DEFAULT_MODEL, 'model_version': DEFAULT_MODEL,
        'model_identity_source': 'completion_response', 'prompt_version': review.VERSION,
        'input_hash': hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        'parse_status': 'valid'}


def output(evidence):
    return {'status': 'CANDIDATE_ONLY_UNVALIDATED', 'proposals': [{
        'template_id': t['template_id'], 'finding': '净收益仍待验证。',
        'candidate_instruction': '依据可核验结构与扣费空间判断机会，保持原风格和保护。',
        'validation_checks': ['独立验证扣费收益与回撤。']} for t in evidence['styles']]}


def test_all_window_stats_profiles_and_errors_survive_lossless_table_compaction():
    evidence, cases, waits = fixture()
    original = deepcopy((evidence, cases, waits))
    messages, _ = review.review_request(evidence, cases, waits)
    data = payload(messages)
    assert len(data['windows']) == 3 and len(data['styles']) == 5
    for source, window in zip(evidence['windows'], data['windows']):
        assert window['errors'] == source['errors'] and not window['comparison_eligible']
        for expected, row in zip(source['strategies'], window['strategy_metric_rows']):
            restored = dict(zip(data['strategy_metric_columns'], row))
            assert restored == {k: v for k, v in expected.items() if k != 'wait_reason_examples'}
    for source, style in zip(evidence['styles'], data['styles']):
        restored = dict(zip(data['style_profile_columns'], style['profile_values']))
        assert {k: restored[k] for k in source['profile']} == source['profile']
        assert style['template_id'] == source['template_id']
    assert (evidence, cases, waits) == original
    assert sum(_estimate_tokens(m['content']) for m in messages) + review.OUTPUT_TOKENS + 256 <= 8192
    visible = messages[0]['content'].split('JSON字段规则：\n', 1)[1]
    assert json.loads(visible) == review.review_request(evidence, cases, waits)[1]


def test_examples_are_earliest_each_result_and_do_not_replace_full_outcome_counts():
    evidence, cases, waits = fixture()
    messages, _ = review.review_request(evidence, cases, waits)
    data = payload(messages)
    examples = [dict(zip(data['case_example_columns'], row)) for row in data['case_example_rows']]
    assert len(examples) == 10 and data['audited_closed_case_count'] == 30
    assert all(cases['cases'][e['casebook_index']]['case_id'].endswith(('-0', '-1')) for e in examples)
    for style in evidence['styles']:
        outcomes = [dict(zip(data['case_outcome_columns'], row)) for row in data['case_outcome_rows']]
        counts = {c['net_outcome']: c['count'] for c in outcomes
                  if c['template_id'] == style['template_id']}
        assert counts == {'WIN': 3, 'LOSS': 2, 'BREAKEVEN': 1}
    assert all(e['entry_fee_types'] == ['TAKER'] and e['entry_order_types'] == ['LIMIT'] for e in examples)
    assert all(len(e['entry_model_claims'][0]) == 24 for e in examples)
    reasons = [dict(zip(data['wait_example_columns'], row)) for row in data['wait_example_rows']]
    assert all(item['count'] == 28 and item['window'] == 'pilot-1' and len(item['text']) == 24 for item in reasons)


@pytest.mark.parametrize('source', ['casebook', 'waits'])
def test_mismatched_provenance_is_refused(source):
    evidence, cases, waits = fixture()
    (cases if source == 'casebook' else waits)['plan_sha256'] = 'different'
    with pytest.raises(ValueError, match='PLAN_BINDING_MISMATCH'):
        review.review_request(evidence, cases, waits)


def test_oversized_request_fails_explicitly_instead_of_dropping_error_or_return_statistics():
    evidence, cases, waits = fixture()
    evidence['windows'][0]['errors'] = ['x' * 50000]
    with pytest.raises(ValueError, match='INPUT_BUDGET_EXCEEDED'):
        review.review_request(evidence, cases, waits)


def test_long_exact_decimal_case_values_and_activity_denominators_fit_with_full_statistics():
    evidence, cases, waits = fixture()
    for case in cases['cases']:
        for key in case['initial_plan_scenario']:
            case['initial_plan_scenario'][key] = '1.142768989830358635218131721'
        case['net_pnl_usdt'] = '-9.32994921750'
    messages, _ = review.review_request(evidence, cases, waits)
    data = payload(messages)
    assert sum(_estimate_tokens(m['content']) for m in messages) + review.OUTPUT_TOKENS + 256 <= 8192
    for row in data['activity_rows']:
        values = dict(zip(data['activity_columns'], row))
        source = waits['strategies'][values.pop('template_id')]
        assert values == {key: source[key] for key in values}
    assert all(row[-1] == '1.142768989830358635218131721' for row in data['case_example_rows'])


@pytest.mark.parametrize('defect', ['count', 'duplicate', 'denominator', 'unknown'])
def test_case_counts_cannot_hide_closed_outcomes(defect):
    evidence, cases, waits = fixture()
    if defect == 'count': cases['case_count'] -= 1
    if defect == 'duplicate': cases['cases'][1] = deepcopy(cases['cases'][0])
    if defect == 'denominator': evidence['windows'][0]['strategies'][0]['closed_trade_count'] += 1
    if defect == 'unknown': cases['cases'][0]['template_id'] = 'unregistered'
    with pytest.raises(ValueError, match='CASE_SET_INVALID|CLOSED_DENOMINATOR_MISMATCH'):
        review.review_request(evidence, cases, waits)


def test_verified_review_is_compatible_with_frozen_heldout_candidate_protocol():
    evidence, cases, waits = fixture()
    messages, schema = review.review_request(evidence, cases, waits)
    value, proof = output(evidence), receipt(messages)
    review.validate_review(value, proof, messages, schema, evidence)
    artifact = {'review': value, 'receipt': proof, 'request_messages': messages,
                'plan_sha256': 'plan', 'requires_validation': True}
    candidate = candidate_instructions(artifact, 'plan')
    assert set(candidate) == {t['template_id'] for t in evidence['styles']}
    assert all(t['execution']['fixed_notional_usdt'] == 2000 for t in frozen_templates(candidate))


def test_transport_sees_the_exact_budgeted_schema_messages_without_hidden_append(monkeypatch):
    from core.model_client import ModelClient
    evidence, cases, waits = fixture()
    messages, schema = review.review_request(evidence, cases, waits)
    original = deepcopy(messages)
    client = ModelClient()
    def completion(**kwargs):
        assert kwargs['messages'] == original
        assert kwargs['max_tokens'] == review.OUTPUT_TOKENS
        return {'choices': [{'message': {'content': json.dumps(output(evidence))}}]}
    monkeypatch.setattr(client, 'chat_completion', completion)
    client.structured_analysis(messages, schema=schema, max_tokens=review.OUTPUT_TOKENS,
                                model_name=DEFAULT_MODEL, allow_syntax_repair=False)
    assert messages == original


@pytest.mark.parametrize('field', ['finding', 'candidate_instruction', 'validation_checks', 'empty'])
def test_output_length_bounds_are_enforced_before_candidate_freeze(field):
    evidence, cases, waits = fixture()
    messages, schema = review.review_request(evidence, cases, waits)
    value = output(evidence)
    if field == 'finding': value['proposals'][0][field] = 'x' * 65
    elif field == 'candidate_instruction': value['proposals'][0][field] = 'x' * 121
    elif field == 'validation_checks': value['proposals'][0][field] = ['x' * 41]
    else: value['proposals'][0]['validation_checks'] = []
    with pytest.raises(ValueError, match='TEXT_BUDGET'):
        review.validate_review(value, receipt(messages), messages, schema, evidence)


@pytest.mark.parametrize('defect', ['duplicate', 'hash', 'model', 'unbounded'])
def test_review_cannot_freeze_duplicate_identity_unverified_call_or_unbounded_candidate(defect):
    evidence, cases, waits = fixture()
    messages, schema = review.review_request(evidence, cases, waits)
    value, proof = output(evidence), receipt(messages)
    if defect == 'duplicate': value['proposals'][1] = deepcopy(value['proposals'][0])
    if defect == 'hash': proof['input_hash'] = 'f' * 64
    if defect == 'model': proof['actual_model_id'] = 'other'
    if defect == 'unbounded': value['proposals'][0]['candidate_instruction'] = 'x' * 501
    with pytest.raises(ValueError): review.validate_review(value, proof, messages, schema, evidence)


@pytest.mark.parametrize('phase', ['partial', 'validation', 'untouched_test'])
def test_cli_refuses_incomplete_or_heldout_before_model_instantiation(tmp_path, monkeypatch, phase):
    import scripts.analyze_gemini_research as original
    partition = 'optimization' if phase == 'partial' else phase
    plan = {'pilot_windows': [{'id': 'fixture', 'partition': partition}]}
    from core.replay.ai_history import digest
    (tmp_path/'research-plan.json').write_text(json.dumps({'plan': plan, 'plan_sha256': digest(plan)}), encoding='utf-8')
    target = tmp_path/'fixture'
    target.mkdir()
    (target/'results.json').write_text(json.dumps({'status': 'PAUSED'}), encoding='utf-8')
    (target/'ledger-audit.json').write_text(json.dumps({'status': 'PASS'}), encoding='utf-8')
    monkeypatch.setattr(review, 'research_driver_lock', lambda _: nullcontext())
    monkeypatch.setattr(review, 'optimization_evidence', original.optimization_evidence)
    monkeypatch.setattr(review, 'OllamaProvider', lambda **_: pytest.fail('Model must not be called'))
    monkeypatch.setattr(sys, 'argv', ['review', '--directory', str(tmp_path)])
    with pytest.raises(ValueError, match='COMPLETE_AUDITED|REFUSES_VALIDATION_OR_TEST'):
        review.main()
    assert not (tmp_path/'strategy-candidates.json').exists()


def complete_fixture():
    evidence, cases, _ = fixture()
    cases['audits'] = []
    for window in evidence['windows']:
        window['comparison_eligible'] = True
        window['errors'] = []
        count = sum(s['decision_count'] for s in window['strategies'])
        cases['audits'].append({'window': window['window'], 'status': 'PASS',
            'run_status': 'COMPLETED', 'complete_execution_audit': True,
            'recorded_row_statuses': {'COMPLETED': count}, 'completed_decisions': count})
    return evidence, cases


def test_complete_execution_checks_all_registered_window_and_scan_denominators():
    evidence, cases = complete_fixture()
    review.require_complete_execution(evidence, cases)


@pytest.mark.parametrize('defect', ['partial_pass', 'error', 'ambiguity', 'halted', 'missing_window',
    'duplicate_window', 'duplicate_audit', 'scan_count', 'completed_count', 'wrong_plan', 'empty_audits'])
def test_candidate_cli_refuses_finished_but_incomplete_execution_before_model_call(tmp_path, monkeypatch, defect):
    evidence, cases = complete_fixture()
    a = cases['audits'][0]
    if defect == 'partial_pass': a['complete_execution_audit'] = False
    elif defect == 'error': a['recorded_row_statuses']['ERROR'] = 1
    elif defect == 'ambiguity': a['recorded_row_statuses']['MODEL_STARTED'] = 1
    elif defect == 'halted': evidence['windows'][0]['comparison_eligible'] = False
    elif defect == 'missing_window': cases['audits'].pop()
    elif defect == 'duplicate_window': evidence['windows'][1]['window'] = evidence['windows'][0]['window']
    elif defect == 'duplicate_audit': cases['audits'].append(deepcopy(a))
    elif defect == 'scan_count': a['recorded_row_statuses']['COMPLETED'] -= 1
    elif defect == 'completed_count': a['completed_decisions'] -= 1
    elif defect == 'wrong_plan': cases['plan_sha256'] = 'another-plan'
    else: cases['audits'] = []
    monkeypatch.setattr(review, 'research_driver_lock', lambda _: nullcontext())
    monkeypatch.setattr(review, 'optimization_evidence', lambda _: evidence)
    monkeypatch.setattr(review, 'build_directory', lambda _: cases)
    monkeypatch.setattr(review, 'OllamaProvider', lambda **_: pytest.fail('No model call allowed'))
    monkeypatch.setattr(sys, 'argv', ['review', '--directory', str(tmp_path)])
    with pytest.raises(ValueError, match='COMPLETE_EXECUTION'):
        review.main()
    assert not (tmp_path/'strategy-candidates.json').exists()
