from copy import deepcopy
import json
import pytest
from core.storage import SQLiteStore
from core.trading.ai_strategy_book import AIStrategyBook, DEFAULT_SECTIONS, TEMPLATES, STRATEGY_PROFILE_VERSION, _PREVIOUS_NOFX_STYLE_BRIEFS, _V7_NOFX_STYLE_BRIEFS


def test_saved_builtin_brief_upgrades_without_resetting_live_margin_or_leverage(tmp_path):
    store = SQLiteStore(tmp_path / 'strategy-upgrade.sqlite3')
    store.initialize()
    book = AIStrategyBook(store)
    template = TEMPLATES[0]
    execution = {**book.active('gate_live')['execution'], 'leverage': 37,
                 'max_notional_usdt': 500, 'fixed_notional_usdt': 500, 'max_margin_pct': 18}
    saved = book.save('gate_live', name=template['name'], sections=deepcopy(template['sections']),
                      expected_revision=0, template_id=template['id'], execution=execution)
    old_sections = dict(saved['sections'])
    old_sections['entry_standards'] = _PREVIOUS_NOFX_STYLE_BRIEFS[template['id']]
    with store._connect() as db:
        db.execute('UPDATE ai_strategy_instructions SET sections_json=? WHERE account_id=?',
                   (json.dumps(old_sections, ensure_ascii=False), 'gate_live'))
    upgraded = book.active('gate_live')
    assert upgraded['sections']['entry_standards'] == template['sections']['entry_standards']
    assert upgraded['execution']['leverage'] == 37
    assert upgraded['execution']['max_notional_usdt'] == 500
    assert upgraded['revision'] == saved['revision']


def test_v7_nofx_prompts_upgrade_all_four_styles_without_resetting_live_envelope(tmp_path):
    store = SQLiteStore(tmp_path / 'nofx-prompt-upgrade.sqlite3')
    store.initialize()
    book = AIStrategyBook(store)
    for index, template in enumerate(TEMPLATES):
        account_id = f'gate_live_{index}'
        execution = {**book.active(account_id)['execution'],
                     'leverage': 37, 'fixed_notional_usdt': 500,
                     'max_notional_usdt': 500, 'max_margin_pct': 17}
        saved = book.save(
            account_id, name=template['name'], sections=deepcopy(template['sections']),
            expected_revision=0, template_id=template['id'], execution=execution,
        )
        old_sections = dict(saved['sections'])
        old_sections['entry_standards'] = _V7_NOFX_STYLE_BRIEFS[template['id']]
        old_profile = dict(saved['profile'])
        old_profile['version'] = 'ai_strategy_pack_2026_09_v7_nofx_gate'
        with store._connect() as db:
            db.execute(
                'UPDATE ai_strategy_instructions SET sections_json=?, profile_json=? WHERE account_id=?',
                (json.dumps(old_sections, ensure_ascii=False),
                 json.dumps(old_profile, ensure_ascii=False), account_id),
            )
        upgraded = book.active(account_id)
        assert upgraded['sections']['entry_standards'] == template['sections']['entry_standards']
        assert upgraded['sections']['entry_standards'].startswith('NoFX ')
        assert upgraded['execution']['leverage'] == 37
        assert upgraded['execution']['max_notional_usdt'] == 500
        assert upgraded['execution']['max_margin_pct'] == 17
        assert upgraded['revision'] == saved['revision']
    assert [item['scan_interval_minutes'] for item in TEMPLATES] == [5, 15, 15, 15]
    assert [item['style'] for item in TEMPLATES] == ['AGGRESSIVE', 'AGGRESSIVE', 'CONSERVATIVE', 'CONSERVATIVE']


def test_nofx_prompt_upgrade_keeps_operator_authored_entry_rules(tmp_path):
    store = SQLiteStore(tmp_path / 'nofx-custom-prompt.sqlite3')
    store.initialize()
    book = AIStrategyBook(store)
    template = TEMPLATES[0]
    sections = {**template['sections'], 'entry_standards': '只按我确认的事件窗口判断。'}
    saved = book.save('gate_live', name=template['name'], sections=sections,
                      expected_revision=0, template_id=template['id'])
    old_profile = {**saved['profile'], 'version': 'ai_strategy_pack_2026_09_v7_nofx_gate'}
    with store._connect() as db:
        db.execute('UPDATE ai_strategy_instructions SET profile_json=? WHERE account_id=?',
                   (json.dumps(old_profile, ensure_ascii=False), 'gate_live'))
    assert book.active('gate_live')['sections']['entry_standards'] == sections['entry_standards']


def test_account_instructions_are_versioned_and_conflicting_edits_fail(tmp_path):
    store = SQLiteStore(tmp_path / 'strategy.sqlite3')
    store.initialize()
    book = AIStrategyBook(store)
    frozen = book.active('gate_testnet')
    sections = deepcopy(DEFAULT_SECTIONS)
    sections['custom_prompt'] = '关注真实新闻催化后的价格确认。'
    saved = book.save('gate_testnet', name='事件策略', sections=sections, expected_revision=0)
    assert saved['revision'] == 1 and saved['digest'] != frozen['digest']
    assert frozen['sections']['custom_prompt'] == TEMPLATES[2]['sections']['custom_prompt']
    assert book.active('gate_live')['revision'] == 0
    assert AIStrategyBook(store).active('gate_testnet') == saved
    with pytest.raises(ValueError, match='REVISION_CONFLICT'):
        book.save('gate_testnet', name='陈旧修改', sections=sections, expected_revision=0)
    with store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM ai_strategy_instructions').fetchone()[0] == 1


def test_template_identity_and_profile_are_persisted(tmp_path):
    store = SQLiteStore(tmp_path / 'strategy-profile.sqlite3')
    store.initialize()
    book = AIStrategyBook(store)
    active = book.active('gate_testnet')
    impulse = next(item for item in TEMPLATES if item['id'] == 'aggressive_impulse')
    saved = book.save(
        'gate_testnet',
        name=impulse['name'],
        sections=deepcopy(impulse['sections']),
        expected_revision=active['revision'],
        template_id=impulse['id'],
        execution={**active['execution'], 'scan_interval_minutes': 5, 'order_preference': 'AUTO'},
    )
    assert saved['template_id'] == 'aggressive_impulse'
    assert saved['style'] == 'AGGRESSIVE'
    assert saved['profile']['version'] == STRATEGY_PROFILE_VERSION
    # The impulse preset is the pack's single 5m strategy and carries 15m/1h
    # confirmation context.
    assert saved['profile']['signal_timeframe'] == '5m'
    assert saved['execution']['scan_interval_minutes'] == 5
    assert saved['profile']['limit_priority'] is True
    assert book.view('gate_testnet')['templates'][0]['profile']['strategy_id'] == 'aggressive_impulse'


def test_template_signal_timeframe_overrides_stale_execution_cadence(tmp_path):
    store = SQLiteStore(tmp_path / 'strategy-cadence.sqlite3')
    store.initialize()
    book = AIStrategyBook(store)
    active = book.active('gate_testnet')
    breakout = next(item for item in TEMPLATES if item['id'] == 'aggressive_breakout')
    saved = book.save(
        'gate_testnet',
        name=breakout['name'],
        sections=deepcopy(breakout['sections']),
        expected_revision=active['revision'],
        template_id=breakout['id'],
        execution={**active['execution'], 'scan_interval_minutes': 5},
    )
    assert saved['template_id'] == 'aggressive_breakout'
    assert saved['profile']['signal_timeframe'] == '15m'
    assert saved['execution']['scan_interval_minutes'] == 15


def test_nofx_runtime_is_persisted_as_active_contract_and_cleared_by_local_template_save(tmp_path):
    store = SQLiteStore(tmp_path / 'strategy-nofx-runtime.sqlite3')
    store.initialize()
    book = AIStrategyBook(store)
    current = book.active('gate_testnet')
    runtime = {
        'version': 1,
        'source': 'NOFX_IMPORT',
        'signal_timeframe': '5m',
        'context_timeframes': ['15m', '1h'],
        'candidate_sources': ['gate_active_usdt_perpetuals'],
        'excluded_symbols': ['TESTUSDT'],
        'unsupported_sources': [],
        'indicators': {
            'raw_klines': {'enabled': True},
            'ema': {'enabled': True, 'periods': [20, 50]},
            'macd': {'enabled': False},
            'rsi': {'enabled': True, 'periods': [14]},
            'atr': {'enabled': True, 'periods': [14]},
            'bollinger': {'enabled': True, 'periods': [20]},
            'volume': {'enabled': True},
            'open_interest': {'enabled': True, 'status': 'CONFIGURED'},
            'funding_rate': {'enabled': True, 'status': 'CONFIGURED'},
        },
    }
    imported = book.save(
        'gate_testnet', name='NOFX 映射策略', sections=deepcopy(current['sections']),
        expected_revision=current['revision'], template_id='aggressive_impulse',
        execution={**current['execution'], 'scan_interval_minutes': 15},
        nofx_runtime=runtime, replace_nofx_runtime=True,
    )

    assert imported['nofx_runtime']['signal_timeframe'] == '5m'
    assert imported['execution']['scan_interval_minutes'] == 5
    assert imported['profile']['context_timeframes'] == ['15m', '1h']
    assert imported['nofx_runtime']['indicators']['rsi']['periods'] == [14]

    local_template = next(item for item in TEMPLATES if item['id'] == 'conservative_pullback')
    cleared = book.save(
        'gate_testnet', name=local_template['name'], sections=deepcopy(local_template['sections']),
        expected_revision=imported['revision'], template_id=local_template['id'],
        execution=imported['execution'], nofx_runtime=None, replace_nofx_runtime=True,
    )
    assert cleared['nofx_runtime'] is None
    assert cleared['execution']['scan_interval_minutes'] == 15


def test_template_order_preference_is_authoritative_and_repairs_legacy_prose(tmp_path):
    store = SQLiteStore(tmp_path / 'strategy-order-policy.sqlite3')
    store.initialize()
    book = AIStrategyBook(store)
    active = book.active('gate_testnet')
    breakout = next(item for item in TEMPLATES if item['id'] == 'aggressive_breakout')
    # Genuinely legacy prose: a 5-minute frequency, which a 15-minute template
    # must replace with its canonical text.  Written out here instead of borrowed
    # from a live template so this stays a fixture and cannot be invalidated by
    # rewording a template's sections.
    legacy_sections = {
        **deepcopy(DEFAULT_SECTIONS),
        'frequency': '由系统每 5 分钟评估触发，使用已收盘 5m、15m、1h 数据。',
        'entry_standards': '价格到位后以市价追单进场。',
        'decision_process': '扫描候选后按当前价开单，不得犹豫。',
        'custom_prompt': '仅关注 BTCUSDT 的日内动量。',
    }
    saved = book.save(
        'gate_testnet',
        name=breakout['name'],
        sections=legacy_sections,
        expected_revision=active['revision'],
        template_id=breakout['id'],
        execution={**active['execution'], 'order_preference': 'MARKET', 'scan_interval_minutes': 5},
    )
    assert saved['execution']['order_preference'] == 'AUTO'
    assert saved['execution']['scan_interval_minutes'] == 15
    assert '每 15 分钟' in saved['sections']['frequency']
    assert '突破' in saved['sections']['entry_standards']
    # The operator's own supplement is preserved across the canonical repair.
    assert saved['sections']['custom_prompt'] == '仅关注 BTCUSDT 的日内动量。'


@pytest.mark.parametrize('bad', [{}, {**DEFAULT_SECTIONS, 'risk': '100%'}, {**DEFAULT_SECTIONS, 'role': ''}, {**DEFAULT_SECTIONS, 'role': 'x' * 2001}])
def test_invalid_instruction_sections_cannot_override_policy(tmp_path, bad):
    store = SQLiteStore(tmp_path / 'strategy.sqlite3')
    store.initialize()
    book = AIStrategyBook(store)
    with pytest.raises(ValueError, match='SECTIONS_INVALID'):
        book.save('gate_testnet', name='无效', sections=bad, expected_revision=0)
    assert book.view('gate_testnet')['fixed_policy']['leverage_source'] == 'MODEL_CHOICE_WITH_STRATEGY_AND_GATE_CEILINGS'
