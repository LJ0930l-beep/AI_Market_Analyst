"""Persist currently observed process/checkpoint identity, never trading policy."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from urllib.request import urlopen
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.replay.ai_template_runner import frozen_source_fingerprint
from core.replay.relay_policy import read_relay_policy
from core.replay.ai_history import digest


def plan_metadata(registration):
    """Derive scope/counts from the frozen plan rather than previous status."""
    plan = registration['plan']
    if digest(plan) != registration['plan_sha256']:
        raise ValueError('STATE_PLAN_HASH_INVALID')
    identities = [t['template_id'] for t in plan['templates']]
    if not identities or len(set(identities)) != len(identities):
        raise ValueError('STATE_TEMPLATE_IDENTITIES_INVALID')
    expected = plan['expected_pilot_decisions']
    if type(expected) is not int or expected <= 0:
        raise ValueError('STATE_EXPECTED_SCAN_COUNT_INVALID')
    protocol = plan['heldout_protocol']
    hours = protocol['window_hours']
    windows = len(protocol['calendar_offsets_days'])
    scans = sum(int(hours * 60 / t['execution']['scan_interval_minutes'])
                for t in plan['templates']) * windows
    target = plan.get('acceptance_win_rate_target', .5)
    if target not in (.5, .6):
        raise ValueError('STATE_ACCEPTANCE_TARGET_INVALID')
    return {'strategy_scope': identities, 'pilot_total_decisions': expected,
            'validation_decisions_per_phase': scans, 'heldout_window_hours': hours,
            'target_validation_win_rate': target}


def reset_trial_observations(state):
    """Drop prior-trial results without relabelling installed runtime evidence."""
    for key in list(state):
        if key.startswith(('first_50_', 'first_window_', 'latest_partial_', 'current_')) or key in (
            'entry_geometry_budget_preview', 'entry_geometry_review', 'full_protocol_audit',
            'candidate_provenance_actual_partial_refusal', 'current_win_rate', 'current_valid_actions',
            'current_accepted_entries', 'current_filled_entries', 'current_complete_closed_positions',
            'current_native_model_calls_checked', 'partial_model_response_protocol_valid',
            'partial_ledger_audit_valid', 'comparison_eligible', 'automatic_resume_ready',
            'invocation_exit_code_observed', 'last_terminal_tool_session_id', 'next_safe_action',
            'current_error_stage', 'error_cause', 'error_calls_retried', 'formal_candidate_generated',
            'repair_activation_receipt', 'source_changed_files', 'source_backup',
            'terminal_checkpoint_evidence', 'pilot_50_model_response_protocol_valid',
            'verified_exit_new_actual_model_execution_proven'):
            state.pop(key)
    state.update(pilot_50_execution_protocol_passed=False,full_execution_passed=False,
                 profit_acceptance_passed=False,independent_validation_started=False,
                 candidate_strategy_enabled=False,execution_protocol_audit=None)


def refresh(directory,session_id=None,max_new_decisions=None):
    directory=Path(directory).resolve()
    assert directory.is_relative_to((ROOT/'reports').resolve()), 'STATE_EXPERIMENT_OUTSIDE_REPORTS'
    registration=json.loads((directory/'research-plan.json').read_text(encoding='utf-8'))
    plan=registration['plan']
    metadata = plan_metadata(registration)
    query="Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -and $_.CommandLine -match 'run_gemini_(year|heldout)_research' } | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    result=subprocess.run(['powershell','-NoProfile','-Command',query],check=True,capture_output=True,text=True)
    observed=json.loads(result.stdout) if result.stdout.strip() else []
    if isinstance(observed,dict): observed=[observed]
    matches=[p for p in observed if directory.name in p['CommandLine']]
    if len(matches)>1: raise ValueError('STATE_MULTIPLE_RESEARCH_PROCESSES')
    process=matches[0] if matches else None
    path=ROOT/'reports/research-monitor-state.json'
    state=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    same_experiment=Path(state.get('active_experiment','')).name==directory.name
    old_pid=state.get('research_pid')
    old_session=state.get('execution_session_id')
    windows=[]
    for window in plan['pilot_windows']:
        database=directory/window['id']/'results.sqlite3'
        if not database.exists(): continue
        with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            counts=dict(db.execute('SELECT status,COUNT(*) FROM ai_template_replay_decisions GROUP BY status'))
            run=db.execute('SELECT status,checkpoint_json FROM ai_template_replay_runs').fetchone()
            checkpoint=json.loads(run[1])
            windows.append({'window':window['id'],'run_state':run[0],
                            'checkpoint_decisions':checkpoint['decision_count'],'row_statuses':counts})
    runtime_error = None
    try:
        with urlopen('http://127.0.0.1:18765/v2/ai-session/status',timeout=35) as response:
            ai=json.load(response)['ai_session']
    except (URLError, TimeoutError, OSError) as error:
        ai = {'state': 'UNAVAILABLE', 'enabled': None}
        runtime_error = type(error).__name__  # No URL credentials or response body.
    if not same_experiment:
        state['previous_experiment']=state.get('active_experiment')
        reset_trial_observations(state)
    state.update(metadata)
    state.update(active_experiment=directory.relative_to(ROOT).as_posix(),
        research_pid=process['ProcessId'] if process else None,
        process_command_line=process['CommandLine'] if process else None,
        execution_session_id=(session_id if session_id is not None else old_session
            if same_experiment and old_pid==process['ProcessId'] else None) if process else None,
        process_must_be_revalidated=True,last_verified_utc=datetime.now(timezone.utc).isoformat(),
        processed_decisions=sum(w['checkpoint_decisions'] for w in windows),
        valid_terminal_decisions=sum(w['row_statuses'].get('COMPLETED',0) for w in windows),
        current_error_count=sum(w['row_statuses'].get('ERROR',0) for w in windows),windows=windows,
        source_freeze_verified=plan['source_sha256']==frozen_source_fingerprint(),
        relay_policy_verified=plan['relay_inference_policy']==read_relay_policy(),
        client_ai_state=ai.get('state'),client_ai_enabled=ai.get('enabled'),
        client_status_error=runtime_error,
        acceptance_policy=(directory/'acceptance-policy.json').relative_to(ROOT).as_posix())
    if max_new_decisions is not None: state['current_invocation_max_decisions']=max_new_decisions
    if process:
        state['invocation_exit_code_observed'] = None
    state['next_safe_action'] = ('OBSERVE_CURRENT_FROZEN_INVOCATION_WITHOUT_RESTART_OR_SOURCE_CHANGE'
        if process else 'AUDIT_CURRENT_TERMINAL_CHECKPOINT_BEFORE_CONTINUATION_OR_REPAIR')
    activation = directory/'source-freeze-activation-proof.json'
    if activation.exists():
        receipt = json.loads(activation.read_text(encoding='utf-8'))
        if receipt.get('plan_sha256') != registration['plan_sha256']:
            raise ValueError('STATE_ACTIVATION_PLAN_HASH_MISMATCH')
        state['repair_activation_receipt'] = activation.relative_to(ROOT).as_posix()
        state['source_changed_files'] = list(receipt.get('changed_sources') or {})
    prefix=directory.name.split('-')[3].upper()
    state['phase']=prefix+('_FROZEN_INVOCATION_RUNNING' if process else '_INVOCATION_TERMINAL_REQUIRES_AUDIT')
    path.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
    return {k:state[k] for k in ('phase','research_pid','execution_session_id','processed_decisions',
        'valid_terminal_decisions','current_error_count','source_freeze_verified','relay_policy_verified',
        'client_ai_state','client_ai_enabled')}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--session-id',type=int)
    parser.add_argument('--max-new-decisions',type=int)
    args=parser.parse_args()
    print(json.dumps(refresh(args.directory,args.session_id,args.max_new_decisions)))


if __name__=='__main__': main()
