import pytest

from scripts.audit_gemini_execution_protocol import protocol_scope


@pytest.mark.parametrize('scans', [576, 1008])
def test_protocol_scope_uses_actual_registered_schedule(scans):
    result = protocol_scope({'expected_pilot_decisions': scans})
    assert result['planned_full_scans'] == scans
    assert '1008' not in result['scope'] and result['full_run_still_required']
    assert 'full_1008_still_required' not in result


@pytest.mark.parametrize('value', [None, True, 0, 49, '576'])
def test_unknown_scope_cannot_be_assumed(value):
    with pytest.raises(ValueError, match='REGISTERED_SCAN_COUNT_INVALID'):
        protocol_scope({'expected_pilot_decisions': value})
