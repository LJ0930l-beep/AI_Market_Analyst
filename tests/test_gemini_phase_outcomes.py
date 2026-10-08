"""Outcome arithmetic and negative acceptance cases, synthetic fixtures only."""
from datetime import datetime, timedelta, timezone
from copy import deepcopy

import pytest

from scripts.audit_gemini_phase_outcomes import summarize_strategy


BASE = datetime(2025, 1, 1, tzinfo=timezone.utc)


def trade(index, net='1', *, window='a', opened=None, closed=None):
    start = opened or BASE + timedelta(hours=index)
    return {'window': window, 'position_id': str(index), 'opened_at': start.isoformat(),
            'closed_at': (closed or start + timedelta(minutes=10)).isoformat(),
            'net_pnl': net, 'realized_gross_pnl': net, 'fees': '0', 'funding_pnl': '0'}


def account(window='a', floating='0', dd=.02):
    return {'window': window, 'metrics': {'unrealized_pnl': floating, 'max_drawdown': dd},
            'events': [{'action': 'OPEN_LONG', 'status': 'ACCEPTED', 'order_id': '1'},
                       {'action': 'OPEN_LONG', 'status': 'PARTIAL_FILL', 'order_id': '1'},
                       {'action': 'OPEN_LONG', 'status': 'FILLED', 'order_id': '1'}]}


def summarize(trades, **overrides):
    args = dict(rows=[], accounts=[account()], phase='validation', expected_windows=['a'],
                complete_audited=True, scan_interval_minutes=15, maximum_drawdown=.1)
    args.update(overrides)
    return summarize_strategy(trades, **args)


def test_pool_counts_dont_average_small_and_large_window_percentages():
    trades = [trade(0, window='a')] + [trade(i, '-1', window='b') for i in range(9)]
    result = summarize(trades, expected_windows=['a', 'b'])
    assert result['pooled_win_rate'] == .1
    assert result['closed_net_pnl_usdt'] == '-8'


def test_net_loss_after_costs_and_breakeven_count_in_win_rate():
    loss = trade(0, '-1')
    loss.update(realized_gross_pnl='2', fees='4', funding_pnl='1')
    result = summarize([loss, trade(1, '0'), trade(2, '1')])
    assert result['losses'] == result['breakeven'] == result['wins_after_fees_and_funding'] == 1
    assert result['pooled_win_rate'] == 1 / 3


def test_floating_profit_never_substitutes_for_realized_net_profit():
    result = summarize([trade(0, '-1')], accounts=[account(floating='1000')])
    assert result['unrealized_pnl_usdt_excluded'] == '1000'
    assert result['closed_net_pnl_usdt'] == '-1'
    assert 'NO_POSITIVE_COMPLETE_CLOSED_NET_PNL' in result['issues']


def test_overlapping_and_nearby_reentries_cluster_across_assets():
    trades = [trade(0, opened=BASE, closed=BASE + timedelta(minutes=30)),
              trade(1, opened=BASE + timedelta(minutes=10), closed=BASE + timedelta(minutes=40)),
              trade(2, opened=BASE + timedelta(minutes=50), closed=BASE + timedelta(minutes=60)),
              trade(3, opened=BASE + timedelta(minutes=76))]
    assert summarize(trades)['opportunity_clusters'] == 2


@pytest.mark.parametrize('mode', ['optimization', 'partial', 'error', 'drawdown', 'unspecified'])
def test_high_wins_do_not_override_other_acceptance_failures(mode):
    args = {}
    if mode == 'optimization': args['phase'] = 'optimization'
    elif mode == 'partial': args['complete_audited'] = False
    elif mode == 'error': args['rows'] = [{'status': 'ERROR', 'window': 'a', 'as_of': BASE.isoformat()}]
    elif mode == 'drawdown': args['accounts'] = [account(dd=.2)]
    elif mode == 'unspecified': args['maximum_drawdown'] = None
    assert not summarize([trade(i) for i in range(40)], **args)['numeric_screen_met']


def test_numeric_screen_still_requires_independent_regime_review():
    result = summarize([trade(i) for i in range(40)])
    assert result['numeric_screen_met']
    assert result['independence_and_regime_review_still_required']
    assert result['filled_entry_orders'] == result['accepted_entry_orders'] == 1


def test_zero_sample_remains_unknown():
    result = summarize([], accounts=[])
    assert result['pooled_win_rate'] is result['wilson_95'] is None
    assert not result['numeric_screen_met']


@pytest.mark.parametrize('failed', [True, False])
def test_finished_window_keeps_audited_returns_and_error_denominator(tmp_path, monkeypatch, failed):
    import json
    import sqlite3
    import scripts.audit_gemini_phase_outcomes as module
    from core.replay.ai_history import digest
    template = {'template_id':'fixture', 'execution':{'sizing_mode':'FIXED_NOTIONAL',
        'fixed_notional_usdt':2000, 'leverage_mode':'VENUE_LIMIT', 'scan_interval_minutes':15}}
    plan = {'templates':[template], 'pilot_windows':[
        {'id':f'pilot-{i}', 'partition':'optimization'} for i in range(1,4)]}
    (tmp_path/'research-plan.json').write_text(json.dumps({'plan':plan,'plan_sha256':digest(plan)}), encoding='utf-8')
    target = tmp_path/'pilot-1'
    target.mkdir()
    closed = trade(0, '-1')
    checkpoint = {'accounts':{'fixture':{'state':{'events':[], 'completed_trades':[closed]}}}}
    with sqlite3.connect(target/'results.sqlite3') as db:
        db.execute('CREATE TABLE ai_template_replay_runs(status,config_json,checkpoint_json)')
        db.execute('INSERT INTO ai_template_replay_runs VALUES(?,?,?)', ('COMPLETED',
            json.dumps({'templates':[template]}),json.dumps(checkpoint)))
        db.execute('CREATE TABLE ai_template_replay_decisions(template_id,status,as_of,decision_json,result_json)')
        for i, status in enumerate(['COMPLETED', 'ERROR' if failed else 'COMPLETED']):
            db.execute('INSERT INTO ai_template_replay_decisions VALUES(?,?,?,?,?)',
                ('fixture',status,(BASE+timedelta(minutes=15*i)).isoformat(),'{"action":"WAIT"}','{}'))
    def audit(snapshot, manifest, *, complete):
        assert complete is (not failed)
        return {'completed_decisions':1 if failed else 2,
            'scope':'PARTIAL_LEDGER_NOT_FINAL_RETURNS' if failed else 'COMPLETE_LEDGER',
            'accounts':{'fixture':{'unrealized_pnl':'0','max_drawdown':.01}}}
    monkeypatch.setattr(module, 'audit_snapshot', audit)
    result = module.audit_directory(tmp_path, maximum_drawdown=.1)
    first, stats = result['windows'][0], result['strategies']['fixture']
    assert first['complete_audited'] is (not failed)
    assert first['recorded_row_statuses'].get('ERROR',0) == int(failed)
    assert stats['closed_positions'] == 1 and stats['closed_net_pnl_usdt'] == '-1'
    assert stats['observed_row_statuses'].get('ERROR',0) == int(failed)
    assert ('FAILED_OR_HALTED_SCANS' in stats['issues']) is failed
    assert not result['profit_acceptance_passed'] and not stats['numeric_screen_met']


@pytest.mark.parametrize('mode', ['duplicate', 'net_tamper', 'negative_lifetime'])
def test_invalid_economic_inputs_rejected(mode):
    source = [trade(0)]
    if mode == 'duplicate': source.append(deepcopy(source[0]))
    elif mode == 'net_tamper': source[0]['net_pnl'] = '9'
    else: source[0]['closed_at'] = (BASE - timedelta(minutes=1)).isoformat()
    with pytest.raises(ValueError): summarize(source)


@pytest.mark.parametrize('limit', [True, float('nan'), float('inf'), 0, 1, -.1])
def test_invalid_drawdown_limit_rejected(limit):
    with pytest.raises(ValueError, match='INVALID_DRAWDOWN_LIMIT'):
        summarize([], maximum_drawdown=limit)


def policy_fixture(tmp_path):
    import json
    from core.replay.ai_history import digest
    from core.replay.ai_template_runner import frozen_templates
    plan = {'templates': frozen_templates(), 'pilot_windows':
            [{'id': f'fixture-{i}', 'partition':'optimization'} for i in range(3)]}
    plan_sha = digest(plan)
    (tmp_path / 'research-plan.json').write_text(json.dumps({'plan':plan, 'plan_sha256':plan_sha}), encoding='utf-8')
    policy = {'source_optimization_plan_sha256':plan_sha,
        'template_ids':[t['template_id'] for t in plan['templates']],
        'minimum_observed_closed_net_win_rate':.5, 'wilson_95_lower_bound_minimum':.5,
        'minimum_complete_closed_positions_per_strategy':30,
        'minimum_nonoverlapping_opportunity_clusters_per_strategy':30,
        'entry_notional_usdt':2000, 'maximum_window_equity_drawdown':.1}
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({'policy':policy,'policy_sha256':digest(policy)}), encoding='utf-8')
    return path, policy


def test_bound_policy_sets_drawdown_without_passing_unstarted_experiment(tmp_path):
    import json
    from scripts.audit_gemini_phase_outcomes import audit_directory
    path, _ = policy_fixture(tmp_path)
    result = audit_directory(tmp_path, acceptance_policy=path)
    assert result['acceptance_policy_sha256'] == json.loads(path.read_text())['policy_sha256']
    assert all(value['drawdown_limit'] == .1 for value in result['strategies'].values())
    assert not result['profit_acceptance_passed'] and not result['all_windows_complete_audited']


@pytest.mark.parametrize('defect', ['hash','origin','target','budget','drawdown_conflict'])
def test_policy_cannot_be_tampered_weakened_or_bound_to_another_experiment(tmp_path, defect):
    import json
    from core.replay.ai_history import digest
    from scripts.audit_gemini_phase_outcomes import audit_directory
    path, policy = policy_fixture(tmp_path)
    options = {}
    if defect == 'hash':
        artifact = {'policy':policy,'policy_sha256':'changed'}
    else:
        if defect == 'origin': policy['source_optimization_plan_sha256'] = 'other'
        elif defect == 'target': policy['minimum_observed_closed_net_win_rate'] = .4
        elif defect == 'budget': policy['entry_notional_usdt'] = 100
        elif defect == 'drawdown_conflict': options['maximum_drawdown'] = .2
        artifact = {'policy':policy,'policy_sha256':digest(policy)}
    path.write_text(json.dumps(artifact), encoding='utf-8')
    with pytest.raises(ValueError, match='OUTCOME_ACCEPTANCE_POLICY'):
        audit_directory(tmp_path, acceptance_policy=path, **options)
