"""Audit every scheduled scan/model attempt, separately from profitability."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.replay.ai_history import digest, utc
from core.replay.ai_template_runner import frozen_source_fingerprint
from core.replay.relay_policy import read_relay_policy
from core.model_routing import DEFAULT_MODEL
from core.ai.transport_diagnostics import safe_transport_trace
from scripts.audit_gemini_execution_protocol import completed_transport_proof
from scripts.audit_gemini_phase_outcomes import audit_snapshot

PASS = 'FULL_NATIVE_EXECUTION_PROTOCOL_PASS_NOT_PROFIT_OR_GATE_ACCEPTANCE'


def schedule(window, templates):
    expected = []
    when, end = utc(window['start']), utc(window['end'])
    if when >= end or when.second or when.microsecond or when.minute % 5:
        raise ValueError('FULL_PROTOCOL_WINDOW_SCHEDULE_INVALID')
    for template in templates:
        interval = template['execution']['scan_interval_minutes']
        if type(interval) is not int or interval <= 0 or interval % 5:
            raise ValueError('FULL_PROTOCOL_INTERVAL_INVALID')
    while when < end:
        for template in templates:
            if when.minute % template['execution']['scan_interval_minutes'] == 0:
                expected.append((template['template_id'], when.isoformat()))
        when += timedelta(minutes=5)
    return expected


def audit_model_attempts(context, requests, seen):
    settings = context.get('model_inference_settings') or {}
    attempts = (settings.get('model_response_audit') or {}).get('attempts')
    if (context.get('model_call_completed') is not True or context.get('model_id') != DEFAULT_MODEL
            or context.get('model_version') != DEFAULT_MODEL or not isinstance(attempts, list) or not attempts):
        raise ValueError('FULL_PROTOCOL_MODEL_IDENTITY_OR_ATTEMPTS_MISSING')
    bindings = []
    for attempt in attempts:
        trace = safe_transport_trace(attempt.get('transport_trace'))
        if trace is None or trace.get('http_status') != 200 or 'failed_phase' in trace:
            raise ValueError('FULL_PROTOCOL_TRANSPORT_RECEIPT_INVALID')
        completed_transport_proof({**attempt, 'transport_trace': trace})
        if trace.get('transport_mode') != 'SSE':
            raise ValueError('FULL_PROTOCOL_COMPLETE_SSE_REQUIRED')
        correlation = trace['correlation_id']
        if correlation in seen:
            raise ValueError('FULL_PROTOCOL_REUSED_TRANSPORT_CORRELATION')
        seen.add(correlation)
        request_hash = attempt.get('request_hash')
        request = requests.get(request_hash)
        if request is None:
            raise ValueError('FULL_PROTOCOL_BOUND_REQUEST_MISSING')
        actual_wire = hashlib.sha256(json.dumps({'messages': request['messages']}, sort_keys=True,
                                                separators=(',', ':')).encode()).hexdigest()
        if actual_wire != request_hash or trace['wire_messages_sha256'] != actual_wire:
            raise ValueError('FULL_PROTOCOL_WIRE_REQUEST_CHANGED')
        raw = attempt.get('raw_response')
        if (not isinstance(raw, str) or not raw or attempt.get('raw_response_truncated') is not False
                or type(attempt.get('raw_response_chars')) is not int or len(raw) != attempt['raw_response_chars']):
            raise ValueError('FULL_PROTOCOL_RAW_RESPONSE_MISSING_OR_TRUNCATED')
        bindings.append({'request_hash': request_hash, 'correlation_id': correlation,
            'raw_response_sha256': hashlib.sha256(raw.encode()).hexdigest(),
            'stream_events': trace['stream_event_count']})
    return bindings


def audit_registered_input_budget(context, plan):
    expected = plan.get('application_input_budget')
    if expected is None:
        return  # Preserved legacy experiments did not register this field.
    actual = (context.get('model_inference_settings') or {}).get('context_length')
    if type(expected) is not int or expected <= 0 or type(actual) is not int or actual != expected:
        raise ValueError('FULL_PROTOCOL_REGISTERED_INPUT_BUDGET_CHANGED')


def audit_directory(directory):
    directory = Path(directory).resolve()
    registration = json.loads((directory/'research-plan.json').read_text(encoding='utf-8'))
    plan = registration['plan']
    if registration['plan_sha256'] != digest(plan):
        raise ValueError('FULL_PROTOCOL_PLAN_HASH_INVALID')
    if plan['source_sha256'] != frozen_source_fingerprint():
        raise ValueError('FULL_PROTOCOL_SOURCE_CHANGED')
    if plan['relay_inference_policy'] != read_relay_policy():
        raise ValueError('FULL_PROTOCOL_RELAY_CHANGED')
    schedules = [schedule(w, plan['templates']) for w in plan['pilot_windows']]
    expected_count = sum(len(s) for s in schedules)
    if expected_count != plan['expected_pilot_decisions']:
        raise ValueError('FULL_PROTOCOL_REGISTERED_COUNT_INVALID')
    windows, bindings, seen = [], [], set()
    all_statuses = Counter()
    issues = []
    for window, expected in zip(plan['pilot_windows'], schedules):
        target = (directory/window['id']).resolve()
        if target.parent != directory:
            raise ValueError('FULL_PROTOCOL_WINDOW_PATH_INVALID')
        database = target/'results.sqlite3'
        if not database.exists():
            windows.append({'window':window['id'], 'expected_scans':len(expected), 'observed_scans':0,
                            'status':'NOT_STARTED', 'complete_protocol_audit':False})
            continue
        with tempfile.TemporaryDirectory(prefix='gemini-full-protocol-') as scratch:
            snapshot = Path(scratch)/'snapshot.sqlite3'
            with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as source:
                with closing(sqlite3.connect(snapshot)) as dest:
                    source.backup(dest)
            with closing(sqlite3.connect(snapshot.as_uri()+'?mode=ro',uri=True)) as db:
                db.row_factory = sqlite3.Row
                run = dict(db.execute('SELECT * FROM ai_template_replay_runs').fetchone())
                config = json.loads(run['config_json'])
                if config['templates'] != plan['templates']:
                    raise ValueError('FULL_PROTOCOL_FROZEN_TEMPLATE_CHANGED')
                rows = [dict(r) for r in db.execute('SELECT * FROM ai_template_replay_decisions ORDER BY rowid')]
                requests = {}
                for item in db.execute('SELECT payload_json FROM evidence_bundles'):
                    request = json.loads(item[0]).get('prompt_request')
                    if request:
                        key = request['request_hash']
                        if key in requests and requests[key]['messages'] != request['messages']:
                            raise ValueError('FULL_PROTOCOL_REQUEST_HASH_COLLISION')
                        requests[key] = request
            actual = [(r['template_id'],r['as_of']) for r in rows]
            if actual != expected[:len(actual)]:
                raise ValueError('FULL_PROTOCOL_NATIVE_SCHEDULE_CHANGED')
            counts = Counter(r['status'] for r in rows)
            all_statuses.update(counts)
            if counts['ERROR']:
                issues.append({'window':window['id'], 'issue':'ERROR_SCANS', 'count':counts['ERROR']})
            complete = (run['status']=='COMPLETED' and actual==expected and set(counts)=={'COMPLETED'})
            ledger = audit_snapshot(snapshot,target/'history.json',complete=complete)
            own = []
            for row in rows:
                if row['status'] != 'COMPLETED':
                    continue
                context = json.loads(row['context_json'])
                audit_registered_input_budget(context, plan)
                own.extend(audit_model_attempts(context,requests,seen))
                if json.loads(row['result_json']).get('private_exchange_calls') != 0:
                    raise ValueError('FULL_PROTOCOL_PRIVATE_ORDER_CALLS')
            bindings.extend(own)
            windows.append({'window':window['id'], 'expected_scans':len(expected), 'observed_scans':len(rows),
                'run_state':run['status'], 'row_statuses':dict(counts), 'model_calls_checked':len(own),
                'scan_keys_sha256':digest([r['scan_key'] for r in rows]),
                'native_schedule_sha256':digest(expected), 'bound_model_attempts_sha256':digest(own),
                'ledger_audit_status':ledger['status'], 'ledger_audit_scope':ledger['scope'],
                'complete_protocol_audit':complete, 'status':'PASS' if complete else 'PARTIAL'})
    complete = bool(windows) and all(w['complete_protocol_audit'] for w in windows)
    return {'status':PASS if complete and not issues else 'FAIL' if issues else 'PARTIAL_NATIVE_PROTOCOL_NOT_FULL_PASS',
        'observed_at':datetime.now(timezone.utc).isoformat(), 'plan_sha256':registration['plan_sha256'],
        'expected_scans':expected_count, 'observed_row_statuses':dict(all_statuses), 'windows':windows,
        'native_decision_model_calls_checked':len(bindings), 'bound_model_attempts_sha256':digest(bindings),
        'issues':issues, 'source_freeze_unchanged':True, 'relay_policy_unchanged':True,
        'profit_acceptance_passed':False, 'private_exchange_execution_proven':False,
        'scope':'ALL_SCHEDULED_SCANS_AND_COMPLETE_MODEL_RESPONSES_NOT_PROFIT_OR_GATE_FILL_ACCEPTANCE'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True)
    args = parser.parse_args()
    report = audit_directory(args.directory)
    (args.directory/'full-execution-check.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
