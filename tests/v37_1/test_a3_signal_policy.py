from __future__ import annotations

import json

import pytest

from core.replay.pa_decision_quality_v37.a3_policy import load_a3_signal_policy


def test_a3_signal_freshness_contract_is_frozen_and_explicit():
    policy, digest = load_a3_signal_policy()

    assert policy["rule_id"] == "FAILED_BREAKOUT_20X15M_5M_REENTRY_FRESH_V2"
    assert policy["provenance"]["frozen_before_new_outcome_analysis"] is True
    assert policy["signal"]["confirmation_time"] == "reentry_5m_bar_end_after_full_bar_close"
    assert policy["signal"]["signal_available_time"] == "reentry_5m_bar_available_at"
    assert policy["signal"]["maximum_age_seconds"] == 600
    assert "greater_than_or_equal_to" in policy["signal"]["expiration_condition"]
    assert policy["execution"]["maximum_quote_age_seconds"] == 60
    assert policy["execution"]["long_entry_side"] == "best_ask"
    assert policy["execution"]["short_entry_side"] == "best_bid"
    assert policy["execution"]["historical_confirmation_close_is_executable_entry"] is False
    assert policy["execution"]["missing_or_invalid_quote"] == "LEAVE_ENTRY_PRICE_NULL_AND_BLOCK_ECONOMICS"
    assert len(digest) == 64


def test_a3_policy_content_cannot_change_without_a_new_frozen_version(tmp_path):
    policy, _ = load_a3_signal_policy()
    policy["signal"]["maximum_age_seconds"] = 601
    mutated_path = tmp_path / "mutated-policy.json"
    mutated_path.write_text(json.dumps(policy), encoding="utf-8")

    with pytest.raises(ValueError, match="V37_A3_POLICY_HASH_MISMATCH"):
        load_a3_signal_policy(mutated_path)
