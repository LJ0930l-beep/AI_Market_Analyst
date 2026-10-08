"""Synthetic account and decision diagnostics, not real acceptance evidence."""
from scripts.summarize_gemini_wait_causes import summarize


def row(action='WAIT', *, owned=False, account_status='AVAILABLE', missing=None, status='COMPLETED'):
    return {'status': status, 'window': 'a', 'as_of': '2025-01-01T00:00:00Z',
        'account': {'status': account_status, 'managed_positions':
                    [{'ownership':'VERIFIED_SYSTEM'}] if owned else [], 'owned_entry_orders': []},
        'decision': {'action':action, 'reason':'fixture', 'strategy_analysis':{'missing_conditions':missing or []}}}


def test_holding_scans_not_in_flat_scan_denominator():
    result = summarize([row('HOLD', owned=True)] * 8 + [row('OPEN_SHORT'), row()])
    assert result['flat_account_scans'] == 2
    assert result['flat_account_open_proposal_fraction'] == .5
    assert result['scans_with_system_position_or_pending_entry'] == 8


def test_pending_system_entry_is_managed_but_external_positions_are_not():
    pending, external = row(), row()
    pending['account']['owned_entry_orders'] = [{'ownership':'SYSTEM_ORDER_ID_MATCH', 'order_id':'a'}]
    external['account']['managed_positions'] = [{'ownership':'EXTERNAL_OR_UNVERIFIED'}]
    result = summarize([pending, external])
    assert result['flat_account_scans'] == result['scans_with_system_position_or_pending_entry'] == 1


def test_unknown_account_and_errors_do_not_appear_as_valid_flat_scans():
    result = summarize([row(account_status='UNAVAILABLE'), row(status='ERROR')])
    assert result['flat_account_scans'] == 0
    assert result['flat_account_open_proposal_fraction'] is None
    assert result['unknown_account_scans_excluded_from_flat_denominator'] == 1


def test_data_mentions_require_review_not_automatic_invalid_wait_label():
    result = summarize([row(missing=['OI与资金费率未知', '新闻未确认'])])
    assert result['missing_condition_data_mentions'] == {'OI':1, 'funding':1, 'news':1}
    assert result['data_mentions_are_review_candidates_not_proof_wait_is_wrong']
    assert result['open_proposals_are_not_order_acceptance_or_fill_proof']


def test_zero_samples_unknown_and_bounded_examples():
    assert summarize([])['flat_account_open_proposal_fraction'] is None
    assert len(summarize([row()] * 30)['wait_examples']) == 6


def test_point_and_join_are_not_open_interest_mentions():
    result = summarize([row(missing=['swing point confirmation', 'join the breakout'])])
    assert result['missing_condition_data_mentions'] == {}
    result = summarize([row(missing=['OI_change unknown'])])
    assert result['missing_condition_data_mentions'] == {'OI':1}
