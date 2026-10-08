"""Monitor scope derives from immutable registration; no processes/models run."""
from copy import deepcopy
import pytest
from core.replay.ai_history import digest
from scripts.refresh_gemini_research_state import plan_metadata, reset_trial_observations


def registration(ids, target=None):
    plan={'templates':[{'template_id':i, 'execution':{'scan_interval_minutes':15}} for i in ids],
          'expected_pilot_decisions':192 * len(ids) * 3,
          'heldout_protocol':{'window_hours':48,'calendar_offsets_days':[14,44,74]}}
    if target is not None: plan['acceptance_win_rate_target']=target
    return {'plan':plan,'plan_sha256':digest(plan)}


def test_pa_counts_and_60_target_replace_five_style_metadata():
    m=plan_metadata(registration(['price_action_structure'],.6))
    assert m['strategy_scope']==['price_action_structure']
    assert m['pilot_total_decisions']==m['validation_decisions_per_phase']==576
    assert m['target_validation_win_rate']==.6


def test_legacy_target_remains_historical_50_not_relabelled():
    assert plan_metadata(registration(['a','b']))['target_validation_win_rate']==.5


@pytest.mark.parametrize('defect',['hash','duplicate','target'])
def test_corrupt_scope_cannot_update_monitor(defect):
    r=registration(['price_action_structure'],.6)
    if defect=='hash': r['plan']['expected_pilot_decisions']=1
    if defect=='duplicate': r=registration(['a','a'])
    if defect=='target': r=registration(['a'],.1)
    with pytest.raises(ValueError,match='STATE_'):
        plan_metadata(r)


def test_new_trial_does_not_inherit_pass_or_profit_evidence():
    state={'first_window_full_native_protocol_passed':True,
           'first_window_full_protocol_evidence':'old/proof.json',
           'first_50_model_calls':56, 'latest_partial_economics':{'wins':4},
           'latest_partial_activity':{'scans':171},
           'full_protocol_audit':'old/audit.json', 'full_execution_passed':True,
           'pilot_50_execution_protocol_passed':True,'profit_acceptance_passed':True,
           'candidate_strategy_enabled':True,'independent_validation_started':True}
    reset_trial_observations(state)
    assert not any(k.startswith(('first_window_','first_50_','latest_partial_')) for k in state)
    assert 'full_protocol_audit' not in state
    assert not any(state[k] for k in ('full_execution_passed','profit_acceptance_passed',
        'pilot_50_execution_protocol_passed','candidate_strategy_enabled','independent_validation_started'))


def test_new_trial_preserves_actual_installed_version_and_source_repair():
    state={'client_installer_experiment':'V24_INSTALLED_PA_ONLY_PUBLIC_API',
           'desktop_update_evidence':'v24/installed-proof.json','client_installer_updated':True,
           'wait_unused_ttl_source_applied':True,'verified_heldout_entry':'scripts/verified.py'}
    original=deepcopy(state)
    reset_trial_observations(state)
    for key,value in original.items(): assert state[key]==value


def test_old_invocation_and_trade_observations_do_not_follow_a_new_freeze():
    state = {'current_win_rate': .75, 'current_accepted_entries': 20,
        'partial_model_response_protocol_valid': True, 'partial_ledger_audit_valid': True,
        'invocation_exit_code_observed': 0, 'last_terminal_tool_session_id': 123,
        'next_safe_action': 'RESUME_OLD_TRIAL', 'repair_activation_receipt': 'old/activation.json',
        'source_changed_files': ['old.py'], 'installed_client_version': 'v24'}
    reset_trial_observations(state)
    assert 'current_win_rate' not in state and 'partial_ledger_audit_valid' not in state
    assert 'next_safe_action' not in state and 'invocation_exit_code_observed' not in state
    assert 'repair_activation_receipt' not in state
    assert state['installed_client_version'] == 'v24'


def test_prior_latency_profit_and_terminal_proof_do_not_relabel_next_trial():
    state = {'current_transport_failed_phase': 'READING_RESPONSE_BODY',
        'current_transport_root_cause': 'old failure', 'current_closed_net_pnl_usdt': '-10',
        'current_partial_valid_ledger_audit_passed': True,
        'terminal_checkpoint_evidence': 'old/checkpoint.json',
        'pilot_50_model_response_protocol_valid': True,
        'verified_exit_new_actual_model_execution_proven': True,
        'verified_exit_source_repair': True, 'installed_client_version': 'v24'}
    reset_trial_observations(state)
    assert not any(key.startswith('current_') for key in state)
    assert 'terminal_checkpoint_evidence' not in state
    assert 'pilot_50_model_response_protocol_valid' not in state
    assert 'verified_exit_new_actual_model_execution_proven' not in state
    assert state['verified_exit_source_repair'] is True
    assert state['installed_client_version'] == 'v24'


def test_live_refresh_clears_previous_exit_and_binds_current_activation(tmp_path, monkeypatch):
    import io
    import json
    from types import SimpleNamespace
    import scripts.refresh_gemini_research_state as module
    directory = tmp_path/'reports/gemini-year-research-v30-fixture'
    directory.mkdir(parents=True)
    r = registration(['price_action_structure'], .6)
    r['plan'].update(pilot_windows=[], source_sha256={}, relay_inference_policy={})
    r['plan_sha256'] = digest(r['plan'])
    (directory/'research-plan.json').write_text(json.dumps(r))
    (directory/'source-freeze-activation-proof.json').write_text(json.dumps({
        'plan_sha256': r['plan_sha256'], 'changed_sources': {'current.py': {}}}))
    state_path=tmp_path/'reports/research-monitor-state.json'
    state_path.write_text(json.dumps({'active_experiment':directory.relative_to(tmp_path).as_posix(),
        'research_pid':None, 'invocation_exit_code_observed':0, 'next_safe_action':'RESUME_OLD_TRIAL'}))
    monkeypatch.setattr(module,'ROOT',tmp_path)
    monkeypatch.setattr(module,'frozen_source_fingerprint',lambda: {})
    monkeypatch.setattr(module,'read_relay_policy',lambda: {})
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout=json.dumps({
        'ProcessId': 77, 'CommandLine': 'python '+directory.name})))
    monkeypatch.setattr(module,'urlopen',lambda *a,**k:io.BytesIO(json.dumps({
        'ai_session':{'state':'STOPPED','enabled':False}}).encode()))
    module.refresh(directory, session_id=88,max_new_decisions=50)
    state=json.loads(state_path.read_text())
    assert state['invocation_exit_code_observed'] is None
    assert state['execution_session_id']==88 and state['research_pid']==77
    assert state['next_safe_action']=='OBSERVE_CURRENT_FROZEN_INVOCATION_WITHOUT_RESTART_OR_SOURCE_CHANGE'
    assert state['repair_activation_receipt'].endswith('v30-fixture/source-freeze-activation-proof.json')
    assert state['source_changed_files']==['current.py']
