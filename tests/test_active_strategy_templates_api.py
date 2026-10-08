"""Public strategy selection must match the operator's PA-only scope."""
from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.v2 import router_for
from core.storage import SQLiteStore
from core.trading.ai_strategy_book import ACTIVE_TEMPLATES, AIStrategyBook, TEMPLATES


@pytest.mark.parametrize('account', ['gate_live', 'gate_testnet'])
def test_public_templates_only_pa_without_resetting_saved_strategy(tmp_path, account):
    store = SQLiteStore(tmp_path / 'selection.sqlite3')
    store.initialize()
    book = AIStrategyBook(store)
    sections = deepcopy(ACTIVE_TEMPLATES[0]['sections'])
    sections['custom_prompt'] = 'Keep the operator PA instructions and evidence cutoff.'
    saved = book.save(account, name='Operator PA', sections=sections, expected_revision=0)
    app = FastAPI()

    def no_runtime():
        pytest.fail('Read-only selection must not access trading or the model')

    app.include_router(router_for(lambda: store, no_runtime, lambda: None))
    with TestClient(app) as client:
        response = client.get('/v2/ai-strategy', params={'account_id': account})
    assert response.status_code == 200
    payload = response.json()
    assert [t['id'] for t in payload['templates']] == ['price_action_structure']
    assert payload['active'] == saved == book.active(account)
    assert payload['active']['execution']['fixed_notional_usdt'] == 2000
    assert payload['active']['execution']['leverage_mode'] == 'VENUE_LIMIT'
    # Immutable old experiments still have their original definitions.
    assert len(TEMPLATES) == 5
