"""Synthetic candidate tampering/stream/evidence regression tests, no API calls."""
from copy import deepcopy
import hashlib
import json

import pytest

from core.model_routing import DEFAULT_MODEL
from core.replay.ai_history import digest
from scripts.audit_gemini_candidate_provenance import output_binding, audit_directory
from scripts.review_gemini_research_cases import review_request, VERSION
from scripts.run_gemini_heldout_research import candidate_instructions

IDENTITY = 'price_action_structure'


def fixture():
    output = {'status': 'CANDIDATE_ONLY_UNVALIDATED', 'proposals': [{'template_id': IDENTITY,
        'finding': 'Synthetic protocol test', 'candidate_instruction': 'Confirm known structure and fees.',
        'validation_checks': ['Independent validation']} ]}
    messages = [{'role': 'system', 'content': 'Synthetic protocol test only'},
                {'role': 'user', 'content': json.dumps({'plan_sha256': 'synthetic-plan'})}]
    raw = json.dumps(output, ensure_ascii=False)
    trace = {'version': 'gemini_transport_trace_v1', 'correlation_id': 'a'*32, 'phase': 'COMPLETED',
        'wire_messages_sha256': hashlib.sha256(json.dumps({'messages': messages}, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest(), 'http_status': 200, 'transport_mode': 'SSE',
        'stream_done': True, 'stream_finish_reason': 'stop', 'stream_event_count': 5,
        'elapsed_ms': 300, 'first_stream_event_ms': 10, 'first_content_ms': 20}
    receipt = {'raw_response': raw, 'transport_trace': trace, 'model_id': DEFAULT_MODEL,
        'actual_model_id': DEFAULT_MODEL, 'verified_manifest_model_id': DEFAULT_MODEL,
        'model_version': DEFAULT_MODEL, 'model_identity_source': 'completion_response',
        'prompt_version': VERSION, 'parse_status': 'valid',
        'input_hash': hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
    return output, raw, receipt, messages


def test_original_response_complete_stream_and_wire_input_bind_without_mutation():
    data = fixture()
    before = deepcopy(data)
    output, raw, receipt, messages = data
    proof = output_binding(output, raw, receipt, messages)
    assert data == before
    assert proof['raw_response_sha256'] == hashlib.sha256(raw.encode()).hexdigest()
    assert proof['decoded_review_sha256'] == digest(output)
    assert proof['scope'].startswith('LOCAL_RECORDED')


def test_old_heldout_input_receipt_check_accepts_changed_proposal_but_new_binding_refuses():
    output, raw, receipt, messages = fixture()
    output['proposals'][0]['candidate_instruction'] = 'Changed after generation.'
    artifact = {'plan_sha256': 'synthetic-plan', 'requires_validation': True,
                'review': output, 'receipt': receipt, 'request_messages': messages}
    assert candidate_instructions(artifact, 'synthetic-plan', [IDENTITY])[IDENTITY] == 'Changed after generation.'
    with pytest.raises(ValueError, match='OUTPUT_CHANGED_FROM_MODEL_RESPONSE'):
        output_binding(output, raw, receipt, messages)


@pytest.mark.parametrize('defect', ['no_raw', 'receipt_raw', 'bad_json', 'duplicate_key', 'nonfinite',
    'missing_trace', 'bad_id', 'http_error', 'failed_phase', 'json_transport', 'stream_done',
    'finish_reason', 'event_count_bool', 'empty_events', 'bad_timing', 'wire_hash', 'changed_input'])
def test_incomplete_or_changed_candidate_is_refused(defect):
    output, raw, receipt, messages = fixture()
    trace = receipt['transport_trace']
    if defect == 'no_raw': raw = None
    elif defect == 'receipt_raw': receipt['raw_response'] = 'other'
    elif defect == 'bad_json': raw = receipt['raw_response'] = '{'
    elif defect == 'duplicate_key':
        raw = receipt['raw_response'] = raw.replace('{', '{"status":"earlier",', 1)
    elif defect == 'nonfinite': raw = receipt['raw_response'] = '{"value":NaN}'
    elif defect == 'missing_trace': del receipt['transport_trace']
    elif defect == 'bad_id': trace['correlation_id'] = 'not-a-correlation-id'
    elif defect == 'http_error': trace['http_status'] = 500
    elif defect == 'failed_phase': trace['failed_phase'] = 'READING_RESPONSE_BODY'
    elif defect == 'json_transport': trace['transport_mode'] = 'JSON'
    elif defect == 'stream_done': trace['stream_done'] = False
    elif defect == 'finish_reason': trace['stream_finish_reason'] = 'length'
    elif defect == 'event_count_bool': trace['stream_event_count'] = True
    elif defect == 'empty_events': trace['stream_event_count'] = 0
    elif defect == 'bad_timing': trace['first_content_ms'] = 400
    elif defect == 'wire_hash': trace['wire_messages_sha256'] = 'b'*64
    else: messages[0]['content'] += 'added after call'
    with pytest.raises(ValueError):
        output_binding(output, raw, receipt, messages)


def complete_directory(tmp_path, monkeypatch):
    import scripts.analyze_gemini_research as analysis
    import scripts.build_gemini_trade_casebook as builder
    from scripts.review_gemini_entry_geometry import geometry_review
    from scripts.review_gemini_closed_costs import summarize_closed_costs
    from scripts import audit_gemini_full_execution as full
    evidence = {'plan_sha256': 'synthetic-plan', 'styles': [{'template_id': IDENTITY, 'name': 'PA',
        'profile': {'signal_timeframe': '15m'}}], 'windows': [{'window': 'pilot-1', 'comparison_eligible': True,
        'errors': [], 'strategies': [{'template_id': IDENTITY, 'closed_trade_count': 0, 'decision_count': 1}]}]}
    book = {'plan_sha256': 'synthetic-plan', 'case_count': 0, 'cases': [], 'audits': [{
        'window': 'pilot-1', 'status': 'PASS', 'run_status': 'COMPLETED', 'complete_execution_audit': True,
        'recorded_row_statuses': {'COMPLETED': 1}, 'completed_decisions': 1}]}
    wait = {'plan_sha256': 'synthetic-plan', 'strategies': {IDENTITY: {'flat_account_scans': 1,
        'flat_account_open_proposals': 0, 'flat_account_waits': 1,
        'scans_with_system_position_or_pending_entry': 0, 'missing_condition_data_mentions': {}}}}
    geometry = geometry_review(book, {IDENTITY: '15m'})
    costs = summarize_closed_costs(book, [IDENTITY])
    messages, _ = review_request(evidence, book, wait, costs, geometry)
    output, raw, receipt, _ = fixture()
    receipt['input_hash'] = hashlib.sha256(json.dumps(messages,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    receipt['transport_trace']['wire_messages_sha256'] = hashlib.sha256(json.dumps(
        {'messages':messages},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    artifact = {'plan_sha256': 'synthetic-plan', 'requires_validation': True,
        'review': output, 'receipt': receipt, 'raw_response': raw, 'request_messages': messages,
        'trade_casebook_sha256': digest(book), 'wait_cause_review_sha256': digest(wait),
        'closed_cost_review_sha256': digest(costs), 'entry_geometry_review_sha256': digest(geometry),
        'response_binding': output_binding(output, raw, receipt, messages)}
    protocol={'status':full.PASS,'plan_sha256':'synthetic-plan','expected_scans':1,
        'observed_row_statuses':{'COMPLETED':1},'native_decision_model_calls_checked':1,
        'bound_model_attempts_sha256':'synthetic-bound-hash'}
    artifact['full_execution_check_sha256']=digest(protocol)
    from core.trading.ai_session_coordinator import _estimate_tokens
    from scripts.review_gemini_research_cases import review_context_budget, OUTPUT_TOKENS
    from scripts.review_gemini_price_action_cases import price_action_case_details
    artifact['review_application_context_budget'] = review_context_budget([IDENTITY])
    artifact['review_estimated_total_tokens'] = sum(_estimate_tokens(m['content']) for m in messages) + OUTPUT_TOKENS + 256
    artifact['price_action_case_details_sha256'] = digest(price_action_case_details(book))
    for filename, content in [('strategy-candidates.json', artifact), ('trade-casebook.json', book),
        ('wait-cause-review.json', wait), ('closed-cost-review.json', costs), ('entry-geometry-review.json', geometry),
        ('full-execution-check.json',protocol)]:
        (tmp_path/filename).write_text(json.dumps(content,ensure_ascii=False),encoding='utf-8')
    monkeypatch.setattr(analysis, 'optimization_evidence', lambda _: evidence)
    monkeypatch.setattr(builder, 'build_directory', lambda _: deepcopy(book))
    monkeypatch.setattr(full,'audit_directory',lambda _:deepcopy(protocol))
    return artifact, book


def test_full_auditor_checks_independent_ledger_and_every_saved_input_evidence(tmp_path, monkeypatch):
    complete_directory(tmp_path, monkeypatch)
    proof = audit_directory(tmp_path)
    assert proof['status'] == 'CANDIDATE_PROVENANCE_PASS_NOT_STRATEGY_OR_PROFIT_ACCEPTANCE'
    assert proof['model_calls'] == proof['private_exchange_calls'] == 0


@pytest.mark.parametrize('defect', ['book', 'wait', 'cost', 'geometry', 'proposal', 'binding', 'identity','full_protocol',
                                  'case_details','budget','estimated_tokens'])
def test_full_auditor_refuses_changed_saved_candidate_or_evidence(tmp_path, monkeypatch, defect):
    artifact, book = complete_directory(tmp_path, monkeypatch)
    files = {'book':'trade-casebook.json', 'wait':'wait-cause-review.json', 'cost':'closed-cost-review.json',
        'geometry':'entry-geometry-review.json', 'proposal':'strategy-candidates.json',
        'binding':'strategy-candidates.json', 'identity':'strategy-candidates.json','full_protocol':'full-execution-check.json',
        'case_details':'strategy-candidates.json','budget':'strategy-candidates.json','estimated_tokens':'strategy-candidates.json'}
    path = tmp_path/files[defect]
    content = json.loads(path.read_text(encoding='utf-8'))
    if defect == 'book': content['case_count'] = 1
    elif defect == 'wait': content['strategies'][IDENTITY]['flat_account_scans'] = 999
    elif defect == 'cost': content['rows'][0][1] = 999
    elif defect == 'geometry': content['case_count'] = 999
    elif defect == 'proposal': content['review']['proposals'][0]['candidate_instruction'] = 'Changed later'
    elif defect == 'binding': content['response_binding']['raw_response_sha256'] = 'f'*64
    elif defect=='identity': content['receipt']['actual_model_id'] = 'other-model'
    elif defect=='case_details': content['price_action_case_details_sha256'] = 'changed'
    elif defect=='budget': content['review_application_context_budget'] = 999999
    elif defect=='estimated_tokens': content['review_estimated_total_tokens'] = 0
    else: content['bound_model_attempts_sha256']='changed'
    path.write_text(json.dumps(content,ensure_ascii=False),encoding='utf-8')
    with pytest.raises(ValueError):
        audit_directory(tmp_path)
