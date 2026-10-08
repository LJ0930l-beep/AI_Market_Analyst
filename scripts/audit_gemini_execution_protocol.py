"""Independently audit the first 50 native scans; never certify profit/live fills."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime,timedelta,timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.replay.ai_history import digest,utc
from core.replay.ai_template_runner import frozen_source_fingerprint
from core.replay.relay_policy import read_relay_policy
from scripts.audit_gemini_phase_outcomes import audit_snapshot
from scripts.audit_fixed_entry_budget import audit_entry


def prefix_issues(rows,expected):
    """No error/ambiguous row may be removed to manufacture fifty successes."""
    issues=[]
    if len(rows)<50: issues.append('FEWER_THAN_50_NATIVE_SCANS')
    prefix=rows[:50]
    if [(r['template_id'],r['as_of']) for r in prefix]!=expected[:len(prefix)]:
        issues.append('NATIVE_SCAN_PREFIX_MISMATCH')
    if any(r['status']=='ERROR' for r in prefix): issues.append('ERROR_IN_FIRST_50')
    if any(r['status'] not in {'COMPLETED','ERROR'} for r in prefix):
        issues.append('NONTERMINAL_OR_HALTED_SCAN_IN_FIRST_50')
    return issues


def completed_transport_proof(attempt):
    trace=attempt.get('transport_trace') or {}
    if attempt.get('status')!='COMPLETED' or trace.get('phase')!='COMPLETED':
        raise ValueError('PROTOCOL_NONCOMPLETED_MODEL_ATTEMPT')
    if trace.get('transport_mode') not in (None, 'JSON', 'SSE'):
        raise ValueError('PROTOCOL_UNKNOWN_TRANSPORT')
    if trace.get('transport_mode')=='SSE':
        count=trace.get('stream_event_count')
        if (trace.get('stream_done') is not True or trace.get('stream_finish_reason')!='stop'
                or type(count) is not int or not 1<=count<=65536):
            raise ValueError('PROTOCOL_INCOMPLETE_STREAM')
        elapsed=trace.get('elapsed_ms')
        for key in ('elapsed_ms','first_stream_event_ms','first_content_ms'):
            value=trace.get(key)
            if type(value) not in (int,float) or not math.isfinite(value) or value<0:
                raise ValueError('PROTOCOL_STREAM_TIMING')
        if not 0<=trace['first_stream_event_ms']<=trace['first_content_ms']<=elapsed:
            raise ValueError('PROTOCOL_STREAM_TIMING')
    return trace


def protocol_scope(plan):
    """Read the registered experiment size; never reuse the old five-strategy count."""
    scans = plan.get('expected_pilot_decisions')
    if type(scans) is not int or scans < 50:
        raise ValueError('PROTOCOL_REGISTERED_SCAN_COUNT_INVALID')
    return {'scope': 'FIRST_50_MODEL_PROTOCOL_NOT_FULL_RUN_PROFIT_OR_GATE_ACCEPTANCE',
            'planned_full_scans': scans, 'full_run_still_required': True}


def wait_repair_unchanged(before,after,mutable):
    """Separate invariant implementation, rather than trusting coordinator output."""
    valid_periods={'5m','15m','1h','4h','1d'}
    if len(mutable)!=1: raise ValueError('PROTOCOL_WAIT_REPAIR_SCOPE')
    path=next(iter(mutable))
    if not path.startswith('timeframe_analysis.') or path.split('.',1)[1] not in valid_periods:
        raise ValueError('PROTOCOL_WAIT_REPAIR_SCOPE')
    period=path.split('.',1)[1]
    prior=before.get('timeframe_analysis',{})
    revised=after.get('timeframe_analysis',{})
    if not isinstance(prior.get(period),str) or len(prior[period])<=800:
        raise ValueError('PROTOCOL_WAIT_REPAIR_NOT_OVERLONG')
    if set(before)!=set(after) or set(prior)!=set(revised):
        raise ValueError('PROTOCOL_WAIT_REPAIR_KEY_DRIFT')
    if any(before[k]!=after[k] for k in before if k!='timeframe_analysis'):
        raise ValueError('PROTOCOL_WAIT_REPAIR_DECISION_DRIFT')
    if any(prior[k]!=revised[k] for k in prior if k!=period):
        raise ValueError('PROTOCOL_WAIT_REPAIR_OTHER_PERIOD_DRIFT')
    if not isinstance(revised.get(period),str) or len(revised[period])>800:
        raise ValueError('PROTOCOL_WAIT_REPAIR_STILL_OVERLONG')


def audit_directory(directory):
    directory=Path(directory).resolve()
    registration=json.loads((directory/'research-plan.json').read_text(encoding='utf-8'))
    plan=registration['plan']
    if registration['plan_sha256']!=digest(plan): raise ValueError('PROTOCOL_PLAN_HASH')
    if plan['source_sha256']!=frozen_source_fingerprint(): raise ValueError('PROTOCOL_SOURCE_DRIFT')
    if plan['relay_inference_policy']!=read_relay_policy(): raise ValueError('PROTOCOL_RELAY_DRIFT')
    window=plan['pilot_windows'][0]
    target=(directory/window['id']).resolve()
    if target.parent!=directory: raise ValueError('PROTOCOL_WINDOW_OUTSIDE_EXPERIMENT')
    database=target/'results.sqlite3'
    if not database.exists(): return {'status':'PENDING','issues':['NO_CHECKPOINT_YET']}
    with tempfile.TemporaryDirectory(prefix='gemini-protocol-audit-') as temporary:
        snapshot=Path(temporary)/'snapshot.sqlite3'
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as source:
            source.execute('PRAGMA query_only=ON')
            with closing(sqlite3.connect(snapshot)) as copied: source.backup(copied)
        with closing(sqlite3.connect(snapshot.as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            rows=[dict(row) for row in db.execute('SELECT * FROM ai_template_replay_decisions ORDER BY rowid')]
            config=json.loads(db.execute('SELECT config_json FROM ai_template_replay_runs').fetchone()[0])
            expected=[]
            when=utc(window['start'])
            while when<utc(window['end']) and len(expected)<50:
                for template in config['templates']:
                    if when.minute%int(template['execution']['scan_interval_minutes'])==0:
                        expected.append((template['template_id'],when.isoformat()))
                when+=timedelta(minutes=5)
            issues=prefix_issues(rows,expected)
            prefix=rows[:50]
            recorded=Counter(r['status'] for r in prefix)
            result={'status':'PENDING' if len(rows)<50 or recorded['MODEL_STARTED'] or recorded['MODEL_DONE'] else 'FAIL',
                'observed_at':datetime.now(timezone.utc).isoformat(),
                **protocol_scope(plan),
                'plan_sha256':registration['plan_sha256'],'first_50_scan_keys_sha256':digest([r['scan_key'] for r in prefix]),
                'observed_total_rows':len(rows),'observed_total_row_statuses':dict(Counter(r['status'] for r in rows)),
                'first_50_row_statuses':dict(recorded),
                'issues':issues,'profit_acceptance_passed':False,'private_exchange_execution_proven':False,
                'complete_profit_acceptance_still_required':True}
            if issues: return result
            audit=audit_snapshot(snapshot,target/'history.json',complete=False)
            if audit['status']!='PASS': raise ValueError('PROTOCOL_LEDGER_AUDIT_FAILED')
            actions,execution_statuses,trace_phases=Counter(),Counter(),Counter()
            accepted=[]
            template_map={t['template_id']:t['execution'] for t in config['templates']}
            wait_repair_count=0
            for row in prefix:
                context=json.loads(row['context_json'])
                decision=json.loads(row['decision_json'])
                execution=json.loads(row['result_json'])
                actions[decision['action']]+=1
                execution_statuses[execution['status']]+=1
                if execution.get('private_exchange_calls')!=0: raise ValueError('PROTOCOL_PRIVATE_CALLS')
                attempts=context['model_inference_settings']['model_response_audit']['attempts']
                if not attempts: raise ValueError('PROTOCOL_NO_MODEL_RESPONSE_AUDIT')
                for attempt in attempts:
                    trace=completed_transport_proof(attempt)
                    trace_phases[trace['phase']]+=1
                settings=context['model_inference_settings']
                for attempt in settings.get('model_attempts',[]):
                    if attempt['prompt_version']!=context.get('model_call_prompt_version'): continue
                    bundle=json.loads(db.execute('SELECT payload_json FROM evidence_bundles WHERE bundle_id=?',
                        (attempt['evidence_bundle_id'],)).fetchone()[0])
                    wrapper=json.loads(bundle['prompt_request']['messages'][1]['content'])
                    if (wrapper.get('previous_decision') or {}).get('action')=='WAIT':
                        from scripts.verify_ai_template_replay import audit_ttl_wire_value
                        final=audit_ttl_wire_value(json.loads(context['model_raw_response']))
                        # Only documented unused WAIT zero placeholders may be projected.
                        for key in ('stop_price','take_profit','take_profit_1','take_profit_2'):
                            if type(final.get(key)) in (int,float) and final[key]==0: final[key]=None
                        wait_repair_unchanged(wrapper['previous_decision'],final,set(wrapper['mutable_fields']))
                        wait_repair_count+=1
                for event in execution.get('events',[]):
                    if event.get('status')=='ACCEPTED' and event.get('action') in {'OPEN_LONG','OPEN_SHORT'}:
                        accepted.append(audit_entry(decision,event,
                            execution['execution_market_snapshots'][event['instrument_id']],template_map[row['template_id']]))
            result.update(status='FIRST_50_EXECUTION_PROTOCOL_PASS_NOT_PROFIT_ACCEPTANCE' if accepted else
                'FIRST_50_VALID_MODEL_PROTOCOL_BUT_NO_ACCEPTED_OPEN_EVIDENCE',
                first_50_completed=50,partial_independent_audit_completed_rows=audit['completed_decisions'],
                partial_independent_audit_sha256=digest(audit),valid_actions=dict(actions),
                execution_statuses=dict(execution_statuses),transport_attempt_phases=dict(trace_phases),
                wait_text_repairs_independently_checked=wait_repair_count,
                simulation_entries_sizing_checked=len(accepted),source_freeze_unchanged=True,
                protocol_checks_passed=True,simulation_execution_evidence_present=bool(accepted))
            return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True)
    args=parser.parse_args()
    report=audit_directory(args.directory)
    (args.directory/'50-decision-execution-check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True))


if __name__=='__main__': main()
