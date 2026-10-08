from copy import deepcopy

import pytest

from core.trading.model_schemas import AI_ACTION_SCHEMA, gemini_response_format, validate_schema


def test_native_ttl_guidance_keeps_contract_bounds_and_does_not_mutate_base():
    original = deepcopy(AI_ACTION_SCHEMA)
    projected = gemini_response_format(AI_ACTION_SCHEMA)['json_schema']['schema']
    ttl = projected['properties']['ttl_seconds']
    assert 'minimum' not in ttl and 'maximum' not in ttl
    assert ttl['type'] == 'object' and ttl['required'] == ['d3', 'd2', 'd1', 'd0']
    assert ttl['properties']['d3']['enum'] == ['0', '1']
    assert '60至1800' in ttl['description'] and 'WAIT/HOLD' in ttl['description']
    assert 'ttl_seconds' not in projected['required']
    assert AI_ACTION_SCHEMA == original
    for value in (None, 60, 777, 1800):
        validate_schema(value, AI_ACTION_SCHEMA['properties']['ttl_seconds'])


@pytest.mark.parametrize('value', [0, 59, 1801, 777.5])
def test_local_ttl_contract_remains_strict(value):
    with pytest.raises(ValueError):
        validate_schema(value, AI_ACTION_SCHEMA['properties']['ttl_seconds'])
