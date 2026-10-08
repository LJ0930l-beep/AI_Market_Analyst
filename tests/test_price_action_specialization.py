"""Single-strategy operator contract; fixtures do not prove returns."""
from copy import deepcopy
import json
import pytest

from core.storage import SQLiteStore
from core.trading.ai_strategy_book import AIStrategyBook, ACTIVE_TEMPLATES, TEMPLATES
from core.replay.ai_template_runner import frozen_templates, run_ai_template_replay
from scripts.run_gemini_heldout_research import heldout_plan
from scripts.audit_gemini_phase_outcomes import summarize_strategy
from tests.test_ai_template_runner import history, wait
from tests.test_gemini_year_research import fixture_archive
from tests.test_gemini_phase_outcomes import trade, account
from core.replay.gemini_research import research_plan

PA = 'price_action_structure'


def book(tmp_path):
    store = SQLiteStore(str(tmp_path / 'isolated.sqlite3'))
    store.initialize()
    return AIStrategyBook(store), store


def test_only_price_action_available_and_default(tmp_path):
    strategy, _ = book(tmp_path)
    view = strategy.view('gate_live')
    assert [t['id'] for t in view['templates']] == [PA]
    assert view['active']['template_id'] == PA
    assert view['active']['execution']['fixed_notional_usdt'] == 2000
    assert view['active']['execution']['leverage_mode'] == 'VENUE_LIMIT'


@pytest.mark.parametrize('identity', [t['id'] for t in TEMPLATES if t['id'] != PA])
def test_retired_templates_cannot_be_saved(tmp_path, identity):
    strategy, _ = book(tmp_path)
    with pytest.raises(ValueError, match='TEMPLATE_RETIRED'):
        strategy.save('gate_live', name='fixture', sections=ACTIVE_TEMPLATES[0]['sections'],
                      expected_revision=0, template_id=identity)


def test_retired_saved_row_projects_pa_without_db_write_or_limit_reset(tmp_path):
    strategy, store = book(tmp_path)
    old = TEMPLATES[0]
    execution = {**old['execution_defaults'], 'max_margin_pct': 7, 'symbols': ['ETHUSDT'],
                 'universe_mode': 'CUSTOM'}
    with store._connect() as db:
        db.execute('INSERT INTO ai_strategy_instructions(account_id,revision,name,sections_json,updated_at,execution_json,template_id,style,profile_json) VALUES(?,?,?,?,?,?,?,?,?)',
                   ('gate_live', 4, old['name'], json.dumps(old['sections']), 'fixture',
                    json.dumps(execution), old['id'], old['style'], json.dumps(old['profile'])))
    result = strategy.active('gate_live')
    assert result['template_id'] == result['profile']['strategy_id'] == PA
    assert result['sections'] == ACTIVE_TEMPLATES[0]['sections']
    assert result['revision'] == 4 and result['retired_template_id'] == old['id']
    assert result['execution']['max_margin_pct'] == 7
    assert result['execution']['symbols'] == ['ETHUSDT']
    assert result['execution']['scan_interval_minutes'] == 15
    with store._connect() as db:
        assert db.execute('SELECT template_id,revision FROM ai_strategy_instructions').fetchone()[:] == (old['id'], 4)


def test_pa_save_enforces_fixed_budget_preserving_margin(tmp_path):
    strategy, _ = book(tmp_path)
    result = strategy.save('gate_live', name='PA', sections=ACTIVE_TEMPLATES[0]['sections'],
        expected_revision=0, template_id=PA,
        execution={**ACTIVE_TEMPLATES[0]['execution_defaults'], 'fixed_notional_usdt': 500,
                   'max_notional_usdt': 500, 'max_margin_pct': 8})
    assert result['execution']['fixed_notional_usdt'] == result['execution']['max_notional_usdt'] == 2000
    assert result['execution']['max_margin_pct'] == 8


def test_replay_has_only_pa_and_rejects_resume_with_different_selection(tmp_path):
    database = tmp_path / 'replay.sqlite3'
    report = run_ai_template_replay(history(), db_path=database, model_decider=wait, template_ids=[PA])
    assert len(report['results']) == 1 and report['results'][0]['template_id'] == PA
    assert report['decision_count'] == 2
    with pytest.raises(ValueError, match='MISMATCH'):
        run_ai_template_replay(history(), db_path=database, model_decider=wait, resume=True)


@pytest.mark.parametrize('selection', [[], [PA, PA], ['unknown']])
def test_bad_selection_rejected(selection):
    with pytest.raises(ValueError, match='SELECTION'):
        frozen_templates(template_ids=selection)


def test_new_calendar_plan_and_heldout_keep_single_identity(tmp_path):
    archive, _, _ = fixture_archive(tmp_path)
    plan = research_plan(archive, template_ids=[PA], optimization_window_hours=48)
    plan['acceptance_win_rate_target'] = .6
    assert plan['expected_pilot_decisions'] == 576
    assert [t['template_id'] for t in plan['templates']] == [PA]
    candidate = {PA: '单测候选，不代表收益。'}
    for phase in ('validation', 'untouched_test'):
        frozen = heldout_plan(plan, candidate, phase, 'fixture')
        assert frozen['expected_pilot_decisions'] == 576
        assert frozen['acceptance_win_rate_target'] == .6
        assert [t['template_id'] for t in frozen['templates']] == [PA]


def test_60_percent_is_a_real_gate_not_report_label():
    trades = [trade(i, '2' if i < 59 else '-1') for i in range(100)]
    metrics = summarize_strategy(trades, rows=[], accounts=[account()], phase='validation',
        expected_windows=['a'], complete_audited=True, scan_interval_minutes=15,
        maximum_drawdown=.1, win_rate_target=.6)
    assert metrics['pooled_win_rate'] == .59
    assert metrics['registered_win_rate_target'] == .6 and not metrics['numeric_screen_met']
    assert 'OBSERVED_NET_WIN_RATE_BELOW_TARGET_OR_UNDEFINED' in metrics['issues']
    assert 'WILSON_LOWER_BOUND_BELOW_60_PERCENT_OR_UNDEFINED' in metrics['issues']
