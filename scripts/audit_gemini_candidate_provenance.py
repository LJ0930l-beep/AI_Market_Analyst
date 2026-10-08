"""Independent candidate output/input/evidence binding; no model or trade calls."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.replay.ai_history import digest
from core.ai.transport_diagnostics import safe_transport_trace
from scripts.audit_gemini_execution_protocol import completed_transport_proof


def output_binding(output, raw, receipt, messages):
    if not isinstance(raw, str) or not raw or receipt.get('raw_response') != raw:
        raise ValueError('CANDIDATE_RAW_RECEIPT_BINDING_INVALID')

    def unique_object(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('CANDIDATE_DUPLICATE_RESPONSE_KEY')
            value[key] = item
        return value

    def nonfinite(_):
        raise ValueError('CANDIDATE_NONFINITE_RESPONSE')

    try:
        parsed = json.loads(raw, object_pairs_hook=unique_object, parse_constant=nonfinite)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError('CANDIDATE_RAW_JSON_INVALID') from exc
    if parsed != output:
        raise ValueError('CANDIDATE_OUTPUT_CHANGED_FROM_MODEL_RESPONSE')
    safe_trace = safe_transport_trace(receipt.get('transport_trace'))
    if safe_trace is None or safe_trace.get('http_status') != 200 or 'failed_phase' in safe_trace:
        raise ValueError('CANDIDATE_TRANSPORT_RECEIPT_INVALID')
    trace = completed_transport_proof({'status': 'COMPLETED', 'transport_trace': safe_trace})
    expected_wire = hashlib.sha256(json.dumps({'messages': messages}, sort_keys=True,
                                              separators=(',', ':')).encode()).hexdigest()
    if trace.get('transport_mode') != 'SSE' or trace.get('wire_messages_sha256') != expected_wire:
        raise ValueError('CANDIDATE_WIRE_INPUT_OR_STREAM_BINDING_INVALID')
    return {'scope': 'LOCAL_RECORDED_RESPONSE_INPUT_BINDING_NOT_PROVIDER_SIGNATURE_OR_PROFIT_PROOF',
        'response_serialization_basis': 'PROVIDER_SERIALIZED_DECODED_JSON_NOT_RAW_SSE_BYTES',
        'raw_response_sha256': hashlib.sha256(raw.encode()).hexdigest(), 'decoded_review_sha256': digest(output),
        'wire_messages_sha256': expected_wire, 'stream_done': True, 'stream_finish_reason': 'stop',
        'stream_event_count': trace['stream_event_count'], 'correlation_id': trace.get('correlation_id')}


def audit_directory(directory):
    # Lazy import prevents a cycle with the candidate-generation preflight.
    from scripts.review_gemini_research_cases import (require_complete_execution, validate_review,
        review_request, review_context_budget, OUTPUT_TOKENS)
    from core.trading.ai_session_coordinator import _estimate_tokens
    from scripts.review_gemini_price_action_cases import price_action_case_details
    from scripts.analyze_gemini_research import optimization_evidence
    from scripts.review_gemini_closed_costs import summarize_closed_costs
    from scripts.review_gemini_entry_geometry import geometry_review
    from scripts.build_gemini_trade_casebook import build_directory
    from scripts.audit_gemini_full_execution import audit_directory as audit_full_protocol, PASS as FULL_PROTOCOL_PASS

    directory = Path(directory).resolve()
    evidence = optimization_evidence(directory)  # Refuses partial and heldout.
    artifact = json.loads((directory / 'strategy-candidates.json').read_text(encoding='utf-8'))
    book = json.loads((directory / 'trade-casebook.json').read_text(encoding='utf-8'))
    wait = json.loads((directory / 'wait-cause-review.json').read_text(encoding='utf-8'))
    costs = json.loads((directory / 'closed-cost-review.json').read_text(encoding='utf-8'))
    require_complete_execution(evidence, book)
    saved_protocol = json.loads((directory/'full-execution-check.json').read_text(encoding='utf-8'))
    if (saved_protocol.get('status') != FULL_PROTOCOL_PASS
            or artifact.get('full_execution_check_sha256') != digest(saved_protocol)):
        raise ValueError('CANDIDATE_FULL_PROTOCOL_BINDING_INVALID')
    actual_protocol = audit_full_protocol(directory)
    if (actual_protocol['status'] != FULL_PROTOCOL_PASS or any(saved_protocol.get(k) != actual_protocol.get(k)
        for k in ('plan_sha256','expected_scans','observed_row_statuses','native_decision_model_calls_checked',
                  'bound_model_attempts_sha256'))):
        raise ValueError('CANDIDATE_FULL_NATIVE_PROTOCOL_CHANGED')
    independent_book = build_directory(directory)
    if any(book.get(k) != independent_book.get(k) for k in ('cases', 'audits', 'case_count', 'plan_sha256')):
        raise ValueError('CANDIDATE_CASEBOOK_CHANGED_FROM_INDEPENDENT_LEDGER')
    ids = [t['template_id'] for t in evidence['styles']]
    if costs != summarize_closed_costs(book, ids):
        raise ValueError('CANDIDATE_CLOSED_COST_EVIDENCE_CHANGED')
    geometry = None
    if ids == ['price_action_structure']:
        geometry = json.loads((directory / 'entry-geometry-review.json').read_text(encoding='utf-8'))
        expected = geometry_review(book, {t['template_id']:t['profile']['signal_timeframe'] for t in evidence['styles']})
        if geometry != expected:
            raise ValueError('CANDIDATE_GEOMETRY_EVIDENCE_CHANGED')
    expected_hashes = {'plan_sha256': evidence['plan_sha256'], 'trade_casebook_sha256': digest(book),
        'wait_cause_review_sha256': digest(wait), 'closed_cost_review_sha256': digest(costs)}
    if geometry is not None:
        expected_hashes['entry_geometry_review_sha256'] = digest(geometry)
        expected_hashes['price_action_case_details_sha256'] = digest(price_action_case_details(book))
    wait_structure = None
    if 'wait_structure_review_sha256' in artifact:
        from scripts.review_gemini_wait_structure import review_directory as review_wait_structure
        wait_structure = json.loads((directory/'wait-structure-review.json').read_text(encoding='utf-8'))
        if wait_structure != review_wait_structure(directory):
            raise ValueError('CANDIDATE_ACTUAL_WAIT_STRUCTURE_EVIDENCE_CHANGED')
        expected_hashes['wait_structure_review_sha256'] = digest(wait_structure)
        expected_hashes['wait_structure_script_sha256'] = hashlib.sha256(
            Path(__file__).with_name('review_gemini_wait_structure.py').read_bytes()).hexdigest()
    if any(artifact.get(k) != v for k, v in expected_hashes.items()):
        raise ValueError('CANDIDATE_EVIDENCE_HASH_BINDING_INVALID')
    messages, schema = review_request(evidence, book, wait, costs, geometry, wait_structure)
    if artifact['request_messages'] != messages:
        raise ValueError('CANDIDATE_VISIBLE_REQUEST_CHANGED')
    if ids == ['price_action_structure']:
        total = sum(_estimate_tokens(m['content']) for m in messages) + OUTPUT_TOKENS + 256
        if (artifact.get('review_application_context_budget') != review_context_budget(ids)
                or artifact.get('review_estimated_total_tokens') != total
                or total > review_context_budget(ids)):
            raise ValueError('CANDIDATE_REVIEW_BUDGET_BINDING_INVALID')
    validate_review(artifact['review'], artifact['receipt'], messages, schema, evidence)
    binding = output_binding(artifact['review'], artifact.get('raw_response'), artifact['receipt'], messages)
    if artifact.get('response_binding') != binding or artifact.get('requires_validation') is not True:
        raise ValueError('CANDIDATE_STORED_RESPONSE_BINDING_INVALID')
    return {'status': 'CANDIDATE_PROVENANCE_PASS_NOT_STRATEGY_OR_PROFIT_ACCEPTANCE',
        'candidate_artifact_sha256': digest(artifact), **expected_hashes, 'response_binding': binding,
        'private_exchange_calls': 0, 'model_calls': 0, 'production_strategy_writes': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    result = audit_directory(args.directory)
    (args.directory/'candidate-provenance-audit.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
