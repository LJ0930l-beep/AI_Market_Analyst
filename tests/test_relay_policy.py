"""No API keys or remote calls in synthetic relay drift tests."""
import json
import pytest
from core.replay.relay_policy import read_relay_policy, relay_policy_guard
from core.replay.ai_template_runner import ReplayPaused


def config(path, high=8192):
    path.write_text(json.dumps({"proxy": {"api_key": "fixture-private-value",
        "request_timeout": 120, "thinking_budget": {"control_source": "gateway",
        "flash_mode": "custom", "flash_low": -1, "flash_medium": -1,
        "flash_high": high, "flash_tiered": -1}}}), encoding="utf-8")


def test_policy_never_contains_credentials_and_calls_priority_guard(tmp_path):
    p=tmp_path/'relay.json';config(p); policy=read_relay_policy(p)
    assert 'fixture-private-value' not in json.dumps(policy)
    calls=[];relay_policy_guard(policy, lambda: calls.append(True), p)()
    assert calls == [True]


def test_drift_blocks_before_model_or_priority_call(tmp_path):
    p=tmp_path/'relay.json';config(p);policy=read_relay_policy(p);config(p, -1)
    calls=[]
    with pytest.raises(ReplayPaused, match='POLICY_DRIFT'):
        relay_policy_guard(policy, lambda: calls.append(True), p)()
    assert not calls


def test_missing_configuration_is_explicit(tmp_path):
    with pytest.raises(ValueError, match='POLICY_UNAVAILABLE'):
        read_relay_policy(tmp_path/'missing.json')
