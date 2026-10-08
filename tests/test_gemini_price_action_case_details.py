"""Offline omission/truncation and actual candidate-request regressions."""
from copy import deepcopy
import json

import pytest

from scripts.review_gemini_price_action_cases import price_action_case_details


def fixture():
    from tests.test_gemini_case_review import fixture as baseline
    evidence, book, _ = baseline()
    identity = 'price_action_structure'
    evidence['styles'] = [s for s in evidence['styles'] if s['template_id'] == identity]
    for w in evidence['windows']:
        w['strategies'] = [s for s in w['strategies'] if s['template_id'] == identity]
    book['cases'] = [c for c in book['cases'] if c['template_id'] == identity]
    book['case_count'] = len(book['cases'])
    for i, c in enumerate(book['cases']):
        d = c['entry_model_proposals']['entry']['decision']
        d.update(reason=f'Case {i}: bullish sweep, but higher timeframe remains bearish.',
                 entry_price=4100.125, stop_price=4094.0, take_profit=4135.0,
                 requested_leverage=20, position_size_usdt=2000)
    return evidence, book


def test_short_catalog_references_preserve_literal_dollars_exact_values_and_old_at_times():
    from scripts.review_gemini_price_action_cases import compact_price_action_objects, expand_price_action_objects
    _, book = fixture()
    details = price_action_case_details(book)
    index = details['proposal_columns'].index('timeframe_analysis')
    value = {'exact': '$123.000000000000001', 'literal': '$12', 'double': '$$5',
             'empty': '$', 'time': '@0', 'unknown': None, 'zero': 0, 'false': False}
    for row in details['proposal_rows']:
        row[index] = deepcopy(value)
    encoded = compact_price_action_objects(details)
    assert 'object_reference_encoding' in encoded
    assert expand_price_action_objects(encoded) == details
    expanded = expand_price_action_objects(encoded)
    expanded['proposal_rows'][0][index]['literal'] = 'mutated'
    assert expanded['proposal_rows'][1][index]['literal'] == '$12'
    assert details['proposal_rows'][0][index] == value


@pytest.mark.parametrize('reference', ['$999999', '$01', '$-1', '$x', '$'])
def test_short_catalog_reference_invalid_indices_are_rejected(reference):
    from scripts.review_gemini_price_action_cases import compact_price_action_objects, expand_price_action_objects
    _, book = fixture()
    encoded = compact_price_action_objects(price_action_case_details(book))
    encoded['proposal_rows'][0][1][2] = reference
    with pytest.raises(ValueError, match='PA_CASE_DETAILS_OBJECT_CATALOG_REFERENCE_INVALID'):
        expand_price_action_objects(encoded)


def test_historical_object_format_literal_dollar_is_not_reinterpreted_as_reference():
    from scripts.review_gemini_price_action_cases import compact_price_action_objects, expand_price_action_objects
    _, book = fixture()
    details = price_action_case_details(book)
    encoded = compact_price_action_objects(details)
    encoded.pop('object_reference_encoding')
    # Minimal historical catalog and row marker: no short-reference metadata.
    encoded['object_shapes'] = [['literal']]
    encoded['object_catalog'] = [{'o': [0, '$12']}]
    encoded['proposal_rows'] = [[list(range(len(details['proposal_columns']))),
        [None] * len(details['proposal_columns'])]]
    encoded['proposal_rows'][0][1][2] = {'r': 0}
    encoded['position_scan_rows'] = []
    assert expand_price_action_objects(encoded)['proposal_rows'][0][2] == {'literal': '$12'}


def test_every_case_including_breakeven_and_late_loss_keeps_full_claim():
    _, book = fixture()
    original = deepcopy(book)
    details = price_action_case_details(book)
    assert book == original
    assert details['case_count'] == len(details['case_rows']) == 6
    assert {row[0] for row in details['proposal_rows']} == set(range(6))
    ri = details['proposal_columns'].index('reason')
    assert all(row[ri].endswith('remains bearish.') for row in details['proposal_rows'])
    assert details['case_rows'][2][details['case_columns'].index('net_outcome')] == 'BREAKEVEN'
    assert details['text_truncated'] is False


def test_every_actual_position_scan_keeps_full_management_claim_and_execution_distinction():
    _, book = fixture()
    for index, case in enumerate(book['cases']):
        case['position_visible_model_scans'] = [{
            'as_of': '2025-01-01T00:01:00Z',
            'decision': {'action': 'HOLD', 'reason': 'Full untruncated holding claim ' + str(index),
                'entry_condition': 'specific structural exit', 'timeframe_analysis': {'15m': 'opposite evidence'}},
            'visible_position': {'mark_price': 105, 'stop_price': 90, 'take_profit': 120, 'ownership': 'VERIFIED_SYSTEM'},
            'decision_explicitly_targets_this_position': False, 'position_execution_events': []}]
    original = deepcopy(book)
    details = price_action_case_details(book)
    assert details['position_scan_count'] == len(book['cases'])
    for index, row in enumerate(details['position_scan_rows']):
        scan = dict(zip(details['position_scan_columns'], row))
        assert scan['casebook_index'] == index
        assert scan['reason'].endswith(str(index))
        assert scan['timeframe_analysis'] == {'15m': 'opposite evidence'}
        assert scan['decision_explicitly_targets_this_position'] is False
        assert scan['position_execution_events'] == []
    details['position_scan_rows'][0][-3]['mark_price'] = 1
    assert book == original


def test_all_entry_proposals_per_case_and_exact_prices_are_preserved():
    _, book = fixture()
    p = book['cases'][0]['entry_model_proposals']
    p['second'] = deepcopy(p['entry'])
    p['second']['decision']['reason'] = 'Second entry has different evidence.'
    p['second']['decision']['entry_price'] = '4100.12500000000000000001'
    details = price_action_case_details(book)
    assert details['proposal_count'] == 7
    row = details['proposal_rows'][1]
    assert row[:2] == [0, 1]
    assert row[details['proposal_columns'].index('entry_price')] == '4100.12500000000000000001'


def test_actual_candidate_request_keeps_each_holding_scan_structure_and_time_legend():
    from scripts.review_gemini_research_cases import review_request
    evidence, book = fixture()
    case = book['cases'][0]
    case['instrument_id'] = 'ETHUSDT'
    encoding = {'keys': {'st': 'state', 'cf': 'confirmed_at'}, 'times': ['holding-time'],
                'defaults': {'source': 'archive'}}
    signal = {'bos': {'st': 'INVALIDATED', 'cf': '@0'}}
    context = {'breakout_retest': {'st': 'SUPERSEDED', 'cf': '@0'}}
    case['position_visible_model_scans'] = [{
        'as_of': 'holding-time', 'decision': {'action': 'WAIT', 'instrument_id': 'BTCUSDT',
            'reason': 'Action concerns a different instrument.'},
        'visible_position': {'instrument_id': case['instrument_id'], 'mark_price': '105.000000000000001'},
        'decision_explicitly_targets_this_position': False, 'position_execution_events': [],
        'effective_model_visible_position_facts': {'instrument_id': case['instrument_id'],
            'market_snapshot': {'mark_price': '105.000000000000001'}, 'price_action_encoding': encoding,
            'technical_context': {'timeframes': {
                '15m': {'price_action': signal, 'price_vs_ema20': 'BELOW', 'indicators': {'atr14_simple': '2.123456789'}},
                '1h': {'price_action': context, 'price_vs_ema20': 'ABOVE'}}}}}]
    original = deepcopy(book)
    messages, _ = review_request(evidence, book)
    details = json.loads(messages[1]['content'])['price_action_case_details']
    row = dict(zip(details['position_scan_columns'], details['position_scan_rows'][0]))
    assert row['instrument_id'] == 'BTCUSDT'  # The action's symbol stays distinguishable.
    assert row['visible_position']['instrument_id'] == case['instrument_id']
    assert row['visible_15m_price_action'] == signal
    assert row['visible_1h_price_action'] == context
    assert row['visible_price_action_encoding'] == encoding
    assert row['visible_15m_atr'] == '2.123456789'
    assert row['visible_15m_ema_side'] == 'BELOW' and row['visible_1h_ema_side'] == 'ABOVE'
    assert row['visible_market_snapshot']['mark_price'] == '105.000000000000001'
    assert book == original
    assert '该仓币种当轮实际最终请求' in messages[0]['content']


def test_unknown_holding_facts_are_null_and_wrong_instrument_is_refused():
    _, book = fixture()
    case = book['cases'][0]
    case['instrument_id'] = 'ETHUSDT'
    scan = {'as_of': 'holding-time', 'decision': {'action': 'HOLD'},
        'visible_position': {'instrument_id': case['instrument_id']},
        'decision_explicitly_targets_this_position': False, 'position_execution_events': []}
    case['position_visible_model_scans'] = [scan]
    details = price_action_case_details(book)
    row = dict(zip(details['position_scan_columns'], details['position_scan_rows'][0]))
    assert row['visible_15m_price_action'] is None and row['visible_1h_price_action'] is None
    assert row['visible_price_action_encoding'] is None and row['visible_market_snapshot'] is None
    scan['effective_model_visible_position_facts'] = {'instrument_id': 'OTHER'}
    with pytest.raises(ValueError, match='PA_CASE_DETAILS_POSITION_MARKET_IDENTITY_INVALID'):
        price_action_case_details(book)


def test_object_layout_and_catalog_compaction_roundtrips_nested_markers_exact_prices_and_nulls():
    from scripts.review_gemini_price_action_cases import compact_price_action_objects, expand_price_action_objects
    _, book = fixture()
    common = {'price': '4100.12500000000000000001', 'state': 'INVALIDATED',
        'confirmed_at': '2025-01-01T00:00:00Z', 'unknown': None,
        'literal_markers': {'o': [0, 'literal'], 'r': 0, 't': 1}}
    for c in book['cases']:
        c['entry_model_proposals']['entry']['decision']['timeframe_analysis'] = {'15m': deepcopy(common)}
    details = price_action_case_details(book)
    original = deepcopy(details)
    encoded = compact_price_action_objects(details)
    assert details == original
    assert encoded['object_catalog']
    assert expand_price_action_objects(encoded) == original
    assert len(json.dumps(encoded)) < len(json.dumps(original))
    encoded['object_catalog'][0] = {'o': [0]}
    assert details == original


@pytest.mark.parametrize('bad', [{'o': [True]}, {'o': [-1]}, {'o': [999]},
    {'o': [0, 'extra']}, {'r': -1}, {'r': True}, {'r': 999}, {'unexpected': 1}])
def test_bad_object_references_cannot_be_silently_decoded(bad):
    from scripts.review_gemini_price_action_cases import compact_price_action_objects, expand_price_action_objects
    _, book = fixture()
    encoded = compact_price_action_objects(price_action_case_details(book))
    encoded['proposal_rows'][0][1][2] = bad
    with pytest.raises(ValueError, match='PA_CASE_DETAILS_OBJECT_'):
        expand_price_action_objects(encoded)


def test_recursive_shared_object_reference_cycle_is_refused():
    from scripts.review_gemini_price_action_cases import compact_price_action_objects, expand_price_action_objects
    _, book = fixture()
    encoded = compact_price_action_objects(price_action_case_details(book))
    encoded['object_catalog'] = [{'r': 0}]
    encoded['proposal_rows'][0][1][2] = {'r': 0}
    with pytest.raises(ValueError, match='OBJECT_CATALOG_REFERENCE_INVALID'):
        expand_price_action_objects(encoded)


def test_fallback_compaction_resolves_shared_text_before_objects_without_losing_any_case():
    from scripts.review_gemini_price_action_cases import expand_price_action_objects
    from scripts.review_gemini_research_cases import review_request, compact_review_tables
    evidence, book = fixture()
    text = 'Repeated complete management counterevidence must survive every reference.'
    for c in book['cases']:
        c['entry_model_proposals']['entry']['decision']['timeframe_analysis'] = {'15m': text}
    messages, _ = review_request(evidence, book)
    data = json.loads(messages[1]['content'])
    compressed = compact_review_tables(data)
    def resolve_text(value):
        if isinstance(value, dict):
            if set(value) == {'t'}:
                return compressed['shared_text'][value['t']]
            return {k: resolve_text(v) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve_text(v) for v in value]
        return value
    restored = expand_price_action_objects(resolve_text(compressed['price_action_case_details']))
    assert restored == data['price_action_case_details']
    assert restored['case_count'] == 6


def test_catalog_and_row_deltas_preserve_changed_nulls_false_zero_and_exact_prices():
    from scripts.review_gemini_price_action_cases import compact_price_action_objects, expand_price_action_objects
    _, book = fixture()
    for i, case in enumerate(book['cases']):
        case['entry_model_proposals']['entry']['decision']['timeframe_analysis'] = {
            '15m': {'common': 'A long common structural evidence sentence preserved exactly.',
                'available_at': f'2025-01-01T00:0{i}:00Z', 'price': '4100.000000000000001',
                'null_or_false': None if i % 2 else False, 'zero': 0, 'literal_delta': {'d': [0, [], []]}}}
    details = price_action_case_details(book)
    encoded = compact_price_action_objects(details)
    assert any('d' in obj for obj in encoded['object_catalog'])
    assert len(encoded['proposal_rows'][1][0]) < len(details['proposal_rows'][1])
    assert expand_price_action_objects(encoded) == details
    restored = expand_price_action_objects(encoded)
    index = details['proposal_columns'].index('timeframe_analysis')
    assert restored['proposal_rows'][1][index]['15m']['null_or_false'] is None
    assert restored['proposal_rows'][0][index]['15m']['null_or_false'] is False
    assert restored['proposal_rows'][0][index]['15m']['zero'] == 0
    assert restored['proposal_rows'][0][index]['15m']['literal_delta'] == {'d': [0, [], []]}


def test_pre_delta_archived_object_layout_format_still_decodes_without_rewriting_it():
    from scripts.review_gemini_price_action_cases import compact_price_action_objects, expand_price_action_objects
    _, book = fixture()
    details = price_action_case_details(book)
    encoded = compact_price_action_objects(details)
    # Reconstruct the old full-row container, retaining literal object refs.
    for field in ('proposal_rows', 'position_scan_rows'):
        previous, rows = None, []
        for indices, values in encoded[field]:
            row = [None] * encoded['row_delta_widths'][field] if previous is None else deepcopy(previous)
            for i, value in zip(indices, values):
                row[i] = value
            rows.append(row)
            previous = row
        encoded[field] = rows
    encoded.pop('row_delta_widths')
    encoded.pop('row_delta_rule')
    original = deepcopy(encoded)
    assert expand_price_action_objects(encoded) == details
    assert encoded == original


@pytest.mark.parametrize('indices', [[0, 0], [1, 0], [-1], [999], [True]])
def test_bad_or_incomplete_row_delta_indices_are_refused(indices):
    from scripts.review_gemini_price_action_cases import compact_price_action_objects, expand_price_action_objects
    _, book = fixture()
    encoded = compact_price_action_objects(price_action_case_details(book))
    encoded['proposal_rows'][0] = [indices, [None] * len(indices)]
    with pytest.raises(ValueError, match='PA_CASE_DETAILS_DELTA_INDICES_INVALID'):
        expand_price_action_objects(encoded)


@pytest.mark.parametrize('delta', [[True, [], []], [-1, [], []], [0, [True], [None]],
    [0, [0, 0], [None, None]], [0, [999], [None]], [0, [0], []]])
def test_bad_catalog_delta_cannot_borrow_or_mutate_an_unrelated_field(delta):
    from scripts.review_gemini_price_action_cases import compact_price_action_objects, expand_price_action_objects
    _, book = fixture()
    encoded = compact_price_action_objects(price_action_case_details(book))
    encoded['object_shapes'] = [['known']]
    encoded['object_catalog'] = [{'o': [0, 'unchanged']}, {'d': delta}]
    encoded['proposal_rows'][0][1][2] = {'r': 1}
    with pytest.raises(ValueError, match='PA_CASE_DETAILS_OBJECT_DELTA_'):
        expand_price_action_objects(encoded)


def test_only_actually_visible_facts_are_projected_missing_is_not_invented():
    _, book = fixture()
    p = book['cases'][0]['entry_model_proposals']['entry']
    p['effective_model_visible_entry_facts'] = {'technical_context': {'timeframes': {
        '15m': {'price_vs_ema20': 'BELOW', 'indicators': {'atr14_simple': 19.042143}}}}}
    details = price_action_case_details(book)
    row = dict(zip(details['proposal_columns'], details['proposal_rows'][0]))
    assert row['visible_15m_ema_side'] == 'BELOW'
    assert row['visible_1h_ema_side'] is None
    assert row['visible_15m_atr'] == 19.042143
    details['proposal_rows'][0][2] = 'mutated output'
    assert p['decision']['reason'].startswith('Case 0:')


def test_original_encoded_structure_states_and_time_legend_survive_without_inference():
    _, book = fixture()
    p = book['cases'][0]['entry_model_proposals']['entry']
    encoding = {'keys': {'st': 'state', 'cf': 'confirmed_at'},
                'times': ['2025-10-15T00:30:00Z'], 'defaults': {'source': 'archive', 'status': 'READY'}}
    signal = {'bos': {'st': 'INVALIDATED', 'cf': '@0'},
              'sweep_reclaim': {'sd': 'LONG', 'cf': '@0'},
              'prior_range': {'hi': 4146.56, 'lo': 4046.01, 'xl': True}}
    context = {'breakout_retest': {'st': 'SUPERSEDED', 'cf': '@0'}}
    p['effective_model_visible_entry_facts'] = {'price_action_encoding': encoding,
        'technical_context': {'timeframes': {'15m': {'price_action': signal}, '1h': {'price_action': context}}}}
    details = price_action_case_details(book)
    row = dict(zip(details['proposal_columns'], details['proposal_rows'][0]))
    assert row['visible_price_action_encoding'] == encoding
    assert row['visible_15m_price_action'] == signal
    assert row['visible_1h_price_action'] == context
    row['visible_15m_price_action']['bos']['st'] = 'ACTIVE'
    assert signal['bos']['st'] == 'INVALIDATED'


def test_structure_not_in_actual_visible_request_is_null_not_a_model_claim():
    _, book = fixture()
    details = price_action_case_details(book)
    row = dict(zip(details['proposal_columns'], details['proposal_rows'][0]))
    assert row['visible_price_action_encoding'] is None
    assert row['visible_15m_price_action'] is None
    assert row['visible_1h_price_action'] is None


@pytest.mark.parametrize('defect', ['count', 'duplicate', 'identity', 'no_proposal', 'bad_reason'])
def test_bad_case_binding_is_refused(defect):
    _, book = fixture()
    if defect == 'count': book['case_count'] -= 1
    elif defect == 'duplicate': book['cases'][1]['case_id'] = book['cases'][0]['case_id']
    elif defect == 'identity': book['cases'][0]['template_id'] = 'other'
    elif defect == 'no_proposal': book['cases'][0]['entry_model_proposals'] = {}
    else: book['cases'][0]['entry_model_proposals']['entry']['decision']['reason'] = 12
    with pytest.raises(ValueError, match='PA_CASE_DETAILS_'):
        price_action_case_details(book)


def test_actual_candidate_messages_include_all_cases_and_untruncated_counterevidence():
    from scripts.review_gemini_research_cases import review_request
    evidence, book = fixture()
    messages, _ = review_request(evidence, book)
    data = json.loads(messages[1]['content'])
    assert data['price_action_case_details'] == price_action_case_details(book)
    assert '前24字' in messages[0]['content']


def test_input_budget_failure_cannot_silently_remove_late_cases_or_counterevidence():
    from scripts.review_gemini_research_cases import review_request
    evidence, book = fixture()
    book['cases'][-1]['entry_model_proposals']['entry']['decision']['reason'] = 'unique counterevidence ' * 10000
    with pytest.raises(ValueError, match='INPUT_BUDGET_EXCEEDED'):
        review_request(evidence, book)


def test_review_budget_is_separate_from_unchanged_legacy_or_live_scan_policy():
    from scripts.review_gemini_research_cases import review_context_budget, review_request
    evidence, book = fixture()
    messages, _ = review_request(evidence, book)
    policy = json.loads(messages[1]['content'])['review_input_policy']
    assert review_context_budget(['price_action_structure']) == 131072
    assert review_context_budget(['other']) == 8192
    assert review_context_budget(['price_action_structure', 'other']) == 8192
    assert policy['trade_scan_or_relay_policy_changed'] is False
    assert policy['provider_native_context_limit_verified'] is False


def test_all_unique_long_claims_fit_larger_bounded_review_without_truncation():
    from core.trading.ai_session_coordinator import _estimate_tokens
    from scripts.review_gemini_research_cases import review_request, OUTPUT_TOKENS
    evidence, book = fixture()
    for i, case in enumerate(book['cases']):
        case['entry_model_proposals']['entry']['decision']['reason'] = f'{i}:' + '完整反向证据及失效条件，不应截断。' * 60
    messages, _ = review_request(evidence, book)
    tokens = sum(_estimate_tokens(m['content']) for m in messages) + OUTPUT_TOKENS + 256
    assert 8192 < tokens <= 131072
    details = json.loads(messages[1]['content'])['price_action_case_details']
    column = details['proposal_columns'].index('reason')
    assert [r[column] for r in details['proposal_rows']] == [
        c['entry_model_proposals']['entry']['decision']['reason'] for c in book['cases']]


def test_one_pa_candidate_can_describe_entry_invalidation_and_management_within_existing_runner_bound():
    from scripts.review_gemini_research_cases import review_request, validate_review, review_instruction_limit
    from tests.test_gemini_case_review import output, receipt
    from core.replay.ai_template_runner import frozen_templates
    evidence, book = fixture()
    messages, schema = review_request(evidence, book)
    response = output(evidence)
    instruction = "结构仍有效时管理盈利仓；结构失效及时退出，勿扩大止损；确认入场不是触价；保留费用和活动审查。" * 4
    assert 120 < len(instruction) <= 500
    response['proposals'][0]['candidate_instruction'] = instruction
    validate_review(response, receipt(messages), messages, schema, evidence)
    baseline = frozen_templates(template_ids=['price_action_structure'])[0]
    frozen = frozen_templates({'price_action_structure': instruction}, template_ids=['price_action_structure'])[0]
    assert frozen['profile'] == baseline['profile']
    assert frozen['execution'] == baseline['execution']
    assert frozen['sections']['custom_prompt'] == baseline['sections']['custom_prompt'] + '\n研究候选：' + instruction
    assert review_instruction_limit(['price_action_structure']) == 500
    assert 'candidate_instruction≤500' in messages[0]['content']
    assert json.loads(messages[1]['content'])['review_input_policy']['candidate_instruction_max_chars'] == 500


def test_pa_candidate_over_500_characters_is_rejected_without_truncation():
    from scripts.review_gemini_research_cases import review_request, validate_review
    from tests.test_gemini_case_review import output, receipt
    evidence, book = fixture()
    messages, schema = review_request(evidence, book)
    response = output(evidence)
    response['proposals'][0]['candidate_instruction'] = '证' * 501
    original = deepcopy(response)
    with pytest.raises(ValueError):
        validate_review(response, receipt(messages), messages, schema, evidence)
    assert response == original


def test_legacy_multiple_strategy_candidates_keep_120_char_bound():
    from scripts.review_gemini_research_cases import review_request, validate_review, review_instruction_limit
    from tests.test_gemini_case_review import fixture as full_fixture, output, receipt
    evidence, book, waits = full_fixture()
    messages, schema = review_request(evidence, book, waits)
    response = output(evidence)
    response['proposals'][0]['candidate_instruction'] = '证' * 121
    with pytest.raises(ValueError):
        validate_review(response, receipt(messages), messages, schema, evidence)
    assert review_instruction_limit([s['template_id'] for s in evidence['styles']]) == 120
