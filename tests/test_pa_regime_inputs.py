"""Isolated fixtures verify causal multi-frame input, not strategy profitability."""
from copy import deepcopy
import json

import pytest

from core.replay.ai_history import ReplayHistory
from core.replay.gemini_research import freeze_window, research_plan
from core.replay.ai_template_runner import frozen_templates
from core.storage import SQLiteStore
from core.trading.ai_strategy_book import AIStrategyBook, ACTIVE_TEMPLATES, _V10_NOFX_STYLE_BRIEFS, _V32_NOFX_STYLE_BRIEFS
from core.trading.ai_session_coordinator import AISessionCoordinator, _fit_prompt_payload, _compact_gate_system_prompt
from core.trading.autonomous_strategy import technical_context, compact_technical, build_nofx_gate_system_prompt
from tests.test_gemini_year_research import fixture_archive


def fixture_inputs(tmp_path):
    archive, window, rules = fixture_archive(tmp_path)
    plan = research_plan(archive, template_ids=['price_action_structure'], optimization_window_hours=48)
    payload = freeze_window(plan, window, rules, tmp_path/'history.json')
    history = ReplayHistory(payload)
    history.set_as_of(history.window_start)
    class Store:
        def latest_bars(self, symbol, timeframe, **kwargs):
            return history.latest_bars(symbol, timeframe, limit=240)
        def build_price_action_evidence(self, rows, timeframe, now):
            from core.replay.gemini_research import research_price_action
            return research_price_action(rows, timeframe, now)
    context = technical_context(Store(), ('ETHUSDT',), history.window_start,
        timeframes=['15m','5m','1h','4h'], include_price_action=True)
    return history, context, plan, payload


def test_real_aggregation_path_has_4h_ohlcv_and_no_forming_bar(tmp_path):
    history, context, plan, payload = fixture_inputs(tmp_path)
    rows = history.latest_bars('ETHUSDT','4h',limit=240)
    assert len(rows)==60  # Ten-day warm-up, not synthetic padding.
    assert all(r['bar_end'] <= history.window_start.isoformat() for r in rows)
    assert rows[-1]['base_volume']==240*12
    assert context['ETHUSDT']['timeframes']['4h']['status']=='READY'
    assert context['ETHUSDT']['timeframes']['4h']['price_action']['status']=='READY'
    assert plan['templates'][0]['profile']['context_timeframes']==['1h','4h']
    history.set_as_of('2025-10-01T00:15:00+00:00')
    assert history.latest_bars('ETHUSDT','4h',limit=1)[0]['bar_end']=='2025-10-01T00:00:00+00:00'


def test_actual_projection_and_budget_preserve_multi_candle_sequence(tmp_path):
    _, context, _, _ = fixture_inputs(tmp_path)
    compact = compact_technical(context,signal_timeframe='15m',entry_timeframe='5m')
    frames=compact['ETHUSDT']['timeframes']
    assert {tf:len(frame['candles']) for tf,frame in frames.items()}=={'15m':8,'5m':6,'1h':4,'4h':4}
    strategy=frozen_templates(template_ids=['price_action_structure'])[0]
    system=_compact_gate_system_prompt(build_nofx_gate_system_prompt(strategy),strategy)
    payload={'allowed_instruments':['ETHUSDT'],'active_strategy':strategy,
             'technical_context':compact,'account_truth':{'equity':1000,'positions':[]},
             'market_snapshots':{'ETHUSDT':{'price':100}},'news_coverage_status':'NO_HISTORICAL_NEWS_ARCHIVE_UNKNOWN',
             'news_revisions':[]}
    projected, meta=_fit_prompt_payload(payload,system,12288,reserve=1536,signal_timeframe='15m')
    assert projected['account_truth']==payload['account_truth']
    assert projected['technical_context']['ETHUSDT']['timeframes'].keys()==frames.keys()
    for tf,frame in projected['technical_context']['ETHUSDT']['timeframes'].items():
        assert len(frame['candles'])==len(frames[tf]['candles'])
        assert frame['last_closed_at']==frames[tf]['last_closed_at']
    assert projected['news_coverage_status']=='NO_HISTORICAL_NEWS_ARCHIVE_UNKNOWN'
    # Insufficient budget must reject, never silently regress to one candle.
    with pytest.raises(ValueError):
        _fit_prompt_payload(payload,system,1000,reserve=1536,signal_timeframe='15m',allow_symbol_deferral=False)


def test_legacy_runtime_cannot_drop_4h_from_pa_scan_contract():
    strategy=frozen_templates(template_ids=['price_action_structure'])[0]
    strategy['nofx_runtime']={'signal_timeframe':'15m','context_timeframes':['1h']}
    signal, frames, _ = AISessionCoordinator._strategy_scan_contract(strategy)
    assert signal=='15m' and frames==('5m','1h','4h')
    strategy['nofx_runtime']['signal_timeframe']='5m'
    assert AISessionCoordinator._strategy_scan_contract(strategy)[:2]==('15m',('5m','1h','4h'))


@pytest.mark.parametrize('prior_brief',[_V10_NOFX_STYLE_BRIEFS['price_action_structure'],_V32_NOFX_STYLE_BRIEFS['price_action_structure']])
def test_saved_builtin_brief_upgrades_without_overwriting_operator_text_or_db(tmp_path,prior_brief):
    store=SQLiteStore(str(tmp_path/'isolated.sqlite3'));store.initialize();book=AIStrategyBook(store)
    template=ACTIVE_TEMPLATES[0]
    sections=deepcopy(template['sections']);sections['entry_standards']=prior_brief
    with store._connect() as db:
        db.execute('INSERT INTO ai_strategy_instructions(account_id,revision,name,sections_json,updated_at,template_id,profile_json,execution_json) VALUES(?,?,?,?,?,?,?,?)',
            ('fixture',1,template['name'],json.dumps(sections),'fixture',template['id'],json.dumps(template['profile']),json.dumps(template['execution_defaults'])))
    upgraded=book.active('fixture')
    assert upgraded['sections']['entry_standards']==template['sections']['entry_standards']
    assert upgraded['profile']['context_timeframes']==['1h','4h']
    custom={**sections,'entry_standards':'Operator custom strategy. Must stay intact.'}
    assert book._sections_for_template(template,custom)==custom
    with store._connect() as db:
        assert json.loads(db.execute('SELECT sections_json FROM ai_strategy_instructions').fetchone()[0])==sections


def test_complete_regime_and_news_instructions_survive_production_prompt():
    from core.trading.ai_strategy_book import _PA_RESEARCH_SEED_INSTRUCTION
    strategy=frozen_templates(template_ids=['price_action_structure'])[0]
    prompt=_compact_gate_system_prompt(build_nofx_gate_system_prompt(strategy),strategy)
    assert strategy['sections']['entry_standards'] in prompt
    assert _PA_RESEARCH_SEED_INSTRUCTION in prompt
    for text in ['新闻','4h','1h','震荡','趋势','过渡','多根','15m是主要交易周期','5m是入场周期','跟随','不微利抢平']:
        assert text in prompt
    assert strategy['execution']['fixed_notional_usdt']==2000
    assert '主要交易周期：15m；入场周期：5m；背景周期：' in prompt
    assert strategy['execution']['scan_interval_minutes']==15


def test_entry_5m_bars_are_closed_at_actual_point_in_time(tmp_path):
    history,context,plan,payload=fixture_inputs(tmp_path)
    last=history.latest_bars('ETHUSDT','5m',limit=1)[0]
    assert last['bar_end']==history.window_start.isoformat()
    history.set_as_of('2025-10-01T00:04:00+00:00')
    assert history.latest_bars('ETHUSDT','5m',limit=1)[0]['bar_end']==last['bar_end']
    assert context['ETHUSDT']['timeframes']['5m']['status']=='READY'


def test_legacy_runtime_read_projection_keeps_main15_entry5_and_saved_margin(tmp_path):
    store=SQLiteStore(str(tmp_path/'isolated.sqlite3'));store.initialize();book=AIStrategyBook(store)
    template=ACTIVE_TEMPLATES[0]
    runtime={'signal_timeframe':'5m','context_timeframes':['1h']}
    execution={**template['execution_defaults'],'max_margin_pct':7}
    with store._connect() as db:
        db.execute('INSERT INTO ai_strategy_instructions(account_id,revision,name,sections_json,updated_at,template_id,profile_json,execution_json,nofx_runtime_json) VALUES(?,?,?,?,?,?,?,?,?)',
            ('fixture',1,template['name'],json.dumps(template['sections']),'fixture',template['id'],json.dumps(template['profile']),json.dumps(execution),json.dumps(runtime)))
    active=book.active('fixture')
    assert active['profile']['signal_timeframe']=='15m'
    assert active['profile']['entry_timeframe']=='5m'
    assert active['profile']['context_timeframes']==['1h','4h']
    assert active['execution']['scan_interval_minutes']==15
    assert active['execution']['max_margin_pct']==7
    assert AISessionCoordinator._strategy_scan_contract(active)[:2]==('15m',('5m','1h','4h'))
    with store._connect() as db:
        assert json.loads(db.execute('SELECT nofx_runtime_json FROM ai_strategy_instructions').fetchone()[0])==runtime
