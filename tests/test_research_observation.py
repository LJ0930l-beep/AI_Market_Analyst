import json
import pytest
from core.replay.research_observation import assess_strategy, wilson_interval, observe_directory, scan_outcome
from core.replay import ai_template_runner as runner
from tests.test_ai_template_runner import history, wait


def summary(wins=0, trades=0):
    return {"wins": wins, "closed_trade_count": trades, "realized_gross_pnl": 20,
            "fees": 2, "funding_pnl": -1, "economic_eligible": True}


def rows(n=24, action="WAIT", status="COMPLETED"):
    return [{"as_of": f"2025-01-01T{i:02}:00:00Z", "status":status,
             "decision_json": json.dumps({"action":action}), "context_json":"{}"} for i in range(n)]


def test_win_rate_is_not_model_confidence_and_zero_samples_are_unknown():
    result = assess_strategy(summary(), rows(), partition="validation", audited=True)
    assert result["win_rate"] is None and result["win_rate_wilson_95"] is None
    assert result["eligibility"] == "NOT_VERIFIED"
    assert "24_CONSECUTIVE_WAITS_REVIEW_TRIGGER_FOLLOW_THROUGH" in result["issues"]


def test_small_sample_high_win_rate_cannot_pass_validation():
    result = assess_strategy(summary(2, 2), rows(2, "OPEN_LONG"), partition="validation", audited=True)
    assert result["win_rate"] == 1 and result["eligibility"] == "NOT_VERIFIED"
    assert result["win_rate_wilson_95"][0] < 0.5


def test_optimization_winner_cannot_be_called_independent_validation():
    result = assess_strategy(summary(70, 100), rows(24, "OPEN_LONG"), partition="optimization", audited=True)
    assert result["eligibility"] == "DIAGNOSTIC_ONLY_OPTIMIZATION"
    result = assess_strategy(summary(70, 100), rows(24, "OPEN_LONG"), partition="validation", audited=True)
    assert result["eligibility"].startswith("OBSERVED_VALIDATION_TARGET_MET")
    assert assess_strategy(summary(70, 100), rows(24, "OPEN_LONG"), partition="validation")["eligibility"] == "NOT_VERIFIED"


def test_blocked_model_proposals_are_distinct_from_strategy_waiting():
    r = rows(2, status="ERROR")
    for row in r:
        row["decision_json"] = None
        row["context_json"] = json.dumps({"model_raw_response":json.dumps({"action":"OPEN_LONG"})})
    result = assess_strategy(summary(), r, partition="optimization")
    assert result["model_open_proposals_including_blocked"] == 2
    assert result["valid_open_rate"] == 0
    assert "OPEN_PROPOSALS_BLOCKED_BEFORE_EXECUTION" in result["issues"]


def test_interval_known_boundaries():
    assert wilson_interval(0,0) is None
    assert wilson_interval(50,100) == pytest.approx([0.4038315303659957, 0.5961684696340044])
    with pytest.raises(ValueError):
        wilson_interval(2,1)


def test_monitor_reads_actual_replay_checkpoint_and_keeps_zero_trades_unknown(tmp_path):
    from core.replay.ai_history import digest
    target = tmp_path / 'pilot-1'
    target.mkdir()
    runner.run_ai_template_replay(history(5), db_path=target/'results.sqlite3', model_decider=wait)
    plan = {"pilot_windows":[{"id":"pilot-1", "partition":"optimization"}], "expected_pilot_decisions":5}
    (tmp_path/'research-plan.json').write_text(json.dumps({"plan":plan,"plan_sha256":digest(plan)}), encoding='utf-8')
    report = observe_directory(tmp_path)
    assert report['processed_decisions'] == 5
    assert len(report['windows'][0]['strategies']) == 5
    assert all(s['win_rate'] is None for s in report['windows'][0]['strategies'].values())
    assert report['production_strategy_writes'] == 0


@pytest.mark.parametrize("action,error,expected", [
    ("WAIT", "INVALID_MODEL_OUTPUT_SCHEMA:output:anyOf", "WAIT_OUTPUT_CONTRACT_ERROR"),
    ("OPEN_LONG", "INVALID_MODEL_OUTPUT_SCHEMA:missing", "OPEN_OUTPUT_CONTRACT_ERROR"),
    (None, "LLMError: request timed out", "MODEL_TRANSPORT_TIMEOUT"),
    ("OPEN_SHORT", "AI_INPUT_BUDGET_EXCEEDED", "MODEL_INPUT_BUDGET_BLOCK"),
])
def test_failure_stages_are_distinct(action, error, expected):
    r=rows(1, status="ERROR")[0];r['decision_json']=None;r['error_code']=error
    r['context_json']=json.dumps({'model_raw_response':json.dumps({'action':action})})
    assert scan_outcome(r)==expected


def test_original_open_is_retained_when_later_repair_times_out():
    r=rows(1, status="ERROR")[0];r['decision_json']=None;r['error_code']='LLMError: timed out'
    r['context_json']=json.dumps({'model_raw_response':'{"provider_error":"timeout"}',
        'model_inference_settings':{'model_response_audit':{'attempts':[{'raw_response':'{"action":"OPEN_LONG"}'}]}}})
    result=assess_strategy(summary(),[r],partition='optimization')
    assert result['model_open_proposals_including_blocked']==1
    assert result['scan_outcomes']=={'MODEL_TRANSPORT_TIMEOUT':1}


def test_submitted_entry_is_not_a_fill_and_margin_rejection_is_explicit():
    r=rows(1,action='OPEN_LONG')[0];r['result_json']='{"status":"SUBMITTED"}'
    assert scan_outcome(r)=='ENTRY_SUBMITTED_NOT_FILL_PROOF'
    r['result_json']='{"status":"BLOCKED","reason":"FIXED_NOTIONAL_MARGIN_INSUFFICIENT"}'
    assert scan_outcome(r)=='EXECUTION_MARGIN_BLOCK'


def test_duration_includes_failed_calls_and_does_not_claim_only_thinking():
    r=rows(2);r[0]['wall_elapsed_seconds']=10;r[1]['wall_elapsed_seconds']=162
    result=assess_strategy(summary(),r,partition='optimization')
    assert result['scan_duration_seconds']['median']==86
    assert result['scan_duration_seconds']['maximum']==162


def test_registered_sixty_percent_target_is_enforced_without_relabeling_legacy():
    sample = rows(24, 'OPEN_LONG')
    legacy = assess_strategy(summary(295, 500), sample, partition='validation', audited=True)
    current = assess_strategy(summary(295, 500), sample, partition='validation', audited=True,
                              win_rate_target=0.6)
    assert legacy['registered_win_rate_target'] == 0.5
    assert legacy['eligibility'].startswith('OBSERVED_VALIDATION_TARGET_MET')
    assert current['registered_win_rate_target'] == 0.6
    assert current['eligibility'] == 'NOT_VERIFIED'
    assert 'OBSERVED_WIN_RATE_BELOW_60_PERCENT' in current['issues']
    exact = assess_strategy(summary(60, 100), sample, partition='validation', audited=True,
                            win_rate_target=0.6)
    assert exact['eligibility'] == 'NOT_VERIFIED'  # Wilson lower bound is below 60%.
    qualified = assess_strategy(summary(72, 100), sample, partition='validation', audited=True,
                                win_rate_target=0.6)
    assert qualified['eligibility'].startswith('OBSERVED_VALIDATION_TARGET_MET')


@pytest.mark.parametrize('target', [True, '0.6', 0.59, float('nan')])
def test_unregistered_target_is_rejected(target):
    with pytest.raises(ValueError, match='UNSUPPORTED_RESEARCH_WIN_RATE_TARGET'):
        assess_strategy(summary(), rows(1), partition='optimization', win_rate_target=target)


def test_directory_observer_uses_hashed_registered_target(tmp_path):
    from core.replay.ai_history import digest
    target = tmp_path / 'pilot-1'
    target.mkdir()
    runner.run_ai_template_replay(history(5), db_path=target/'results.sqlite3', model_decider=wait,
                                  template_ids=['price_action_structure'])
    plan = {'pilot_windows': [{'id': 'pilot-1', 'partition': 'optimization'}],
            'expected_pilot_decisions': 5, 'acceptance_win_rate_target': 0.6}
    registration = {'plan': plan, 'plan_sha256': digest(plan)}
    path = tmp_path/'research-plan.json'
    path.write_text(json.dumps(registration), encoding='utf-8')
    report = observe_directory(tmp_path)
    assert report['target_win_rate'] == 0.6
    assert report['windows'][0]['strategies']['price_action_structure']['registered_win_rate_target'] == 0.6
    plan['acceptance_win_rate_target'] = 0.5
    path.write_text(json.dumps(registration), encoding='utf-8')
    with pytest.raises(ValueError, match='RESEARCH_PLAN_HASH_MISMATCH'):
        observe_directory(tmp_path)
