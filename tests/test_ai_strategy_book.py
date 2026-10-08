"""PA-only operator contract; historical retirement coverage is separate."""
from copy import deepcopy
import pytest
from core.storage import SQLiteStore
from core.trading.ai_strategy_book import AIStrategyBook, ACTIVE_TEMPLATES
from core.trading.autonomous_strategy import build_strategy_system_prompt


def make_book(tmp_path):
    store = SQLiteStore(tmp_path / 'strategy.sqlite3')
    store.initialize()
    return AIStrategyBook(store), store


def test_account_revision_and_isolation(tmp_path):
    book, store = make_book(tmp_path)
    initial = book.active('gate_testnet')
    sections = deepcopy(initial['sections'])
    sections['custom_prompt'] = 'Use confirmed structure; never invent missing news.'
    saved = book.save('gate_testnet', name='PA', sections=sections, expected_revision=0)
    assert saved['revision'] == 1 and saved['digest'] != initial['digest']
    assert book.active('gate_live')['revision'] == 0
    assert AIStrategyBook(store).active('gate_testnet') == saved
    with pytest.raises(ValueError, match='REVISION_CONFLICT'):
        book.save('gate_testnet', name='stale', sections=sections, expected_revision=0)
    with store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM ai_strategy_instructions').fetchone()[0] == 1


def test_operator_pa_text_survives_reload(tmp_path):
    book, store = make_book(tmp_path)
    sections = deepcopy(ACTIVE_TEMPLATES[0]['sections'])
    sections['entry_standards'] = 'Custom PA: compare confirmed structure and counterevidence.'
    saved = book.save('gate_live', name='Custom PA', sections=sections, expected_revision=0)
    assert AIStrategyBook(store).active('gate_live')['sections'] == saved['sections'] == sections


def test_cadence_and_order_mode_repair_stale_execution(tmp_path):
    book, _ = make_book(tmp_path)
    pa = ACTIVE_TEMPLATES[0]
    saved = book.save('gate_live', name=pa['name'], sections=pa['sections'], expected_revision=0,
                     execution={**pa['execution_defaults'], 'scan_interval_minutes': 5, 'order_preference': 'MARKET'})
    assert saved['execution']['scan_interval_minutes'] == 15
    assert saved['execution']['order_preference'] == 'AUTO'
    assert saved['profile']['strategy_id'] == 'price_action_structure'


def test_prompt_preserves_time_and_gateway_contract(tmp_path):
    book, _ = make_book(tmp_path)
    prompt = build_strategy_system_prompt(book.active('gate_live'), nofx_gate=True)
    for term in ('as_of', 'bar_at', 'confirmed_at', 'available_at', 'UPDATE_PROTECTION', 'CANCEL_ORDER', '2000'):
        assert term in prompt


@pytest.mark.parametrize('sections', [{}, {'role': 'bad'}])
def test_invalid_sections_never_persist(tmp_path, sections):
    book, store = make_book(tmp_path)
    with pytest.raises(ValueError, match='SECTIONS_INVALID'):
        book.save('gate_live', name='fixture', sections=sections, expected_revision=0)
    with store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM ai_strategy_instructions').fetchone()[0] == 0


def test_unknown_template_not_silently_selected(tmp_path):
    book, _ = make_book(tmp_path)
    with pytest.raises(ValueError, match='TEMPLATE_INVALID'):
        book.save('gate_live', name='fixture', sections=ACTIVE_TEMPLATES[0]['sections'],
                  template_id='unknown', expected_revision=0)
