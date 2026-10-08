from copy import deepcopy

import pytest

from scripts.prepare_pa_cost_prompt import proposed_seed, replaced_messages


def test_only_heading_shortened_preserving_original_policy():
    original = '价格行为 / Gemini High · 15m 价格结构：触价不是完整条件，固定2000，结构失效退出。'
    addition = '费用两腿与退出滑点分别核算，不缩止损凑RR。'
    result = proposed_seed({'current_frozen_seed':original,
                            'proposed_additional_research_cost_explanation':addition})
    assert result == '15m价格行为：触价不是完整条件，固定2000，结构失效退出。' + addition


def test_complete_user_schema_accounts_prices_and_trade_values_unchanged():
    messages = [{'role':'system','content':'prefix ORIGINAL suffix'},
                {'role':'user','content':'{"inputs":{"account":1,"entry":100},"schema":{"strict":true}}'}]
    before = deepcopy(messages)
    actual = replaced_messages(messages, 'ORIGINAL', 'NEXT COST EXPLANATION')
    assert actual[1:] == messages[1:]
    assert messages == before


@pytest.mark.parametrize('system', ['no old text', 'OLD OLD'])
def test_missing_or_duplicate_seed_not_silently_rewritten(system):
    with pytest.raises(ValueError, match='ONE_SYSTEM_SEED_REQUIRED'):
        replaced_messages([{'role':'system','content':system}], 'OLD', 'NEW')


def test_overlong_research_seed_rejected():
    with pytest.raises(ValueError, match='LENGTH_EXCEEDED'):
        proposed_seed({'current_frozen_seed':'价格行为 / Gemini High · 15m 价格结构：' + '验'*500,
                       'proposed_additional_research_cost_explanation':'additional'})
