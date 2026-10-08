"""Verify accepted repair requests without treating fixtures as real trades."""
from copy import deepcopy
import json
import sqlite3

import pytest

from core.replay.ai_template_runner import run_ai_template_replay
from scripts.verify_ai_template_replay import audit_effective_request, digest, _frozen_projection
from tests.test_ai_template_runner import history, rows
from tests.test_gate_open_generation_repair import RepairingProvider


def repair_fixture(tmp_path, monkeypatch, provider=None):
    from core.model_client import model_client
    monkeypatch.setattr(model_client, "count_tokens", lambda text, **kw: max(1, len(text) // 3))
    path = tmp_path / "audit.sqlite3"
    run_ai_template_replay(history(5), db_path=path, model_provider=provider or RepairingProvider(),
                           priority_guard=lambda: None, max_decisions=1)
    context = json.loads(rows(path)[0]["context_json"])
    with sqlite3.connect(path) as db:
        bundles = {key: json.loads(value) for key, value in db.execute("SELECT bundle_id,payload_json FROM evidence_bundles")}
    return context, bundles[context["evidence_bundle_id"]], bundles


def test_original_and_repair_hashes_are_verified_separately(tmp_path, monkeypatch):
    ctx, original, bundles = repair_fixture(tmp_path, monkeypatch)
    request = audit_effective_request(ctx, original, bundles.__getitem__)
    assert request["response_schema_sha256"] == ctx["model_inference_settings"]["response_schema_sha256"]
    assert request["response_schema_sha256"] != original["prompt_request"]["response_schema_sha256"]


def test_modified_trade_is_rejected_even_if_its_schema_is_valid(tmp_path, monkeypatch):
    ctx, original, bundles = repair_fixture(tmp_path, monkeypatch)
    changed = json.loads(ctx["model_raw_response"])
    changed["entry_price"] = 101
    ctx["model_raw_response"] = json.dumps(changed)
    with pytest.raises(ValueError, match="REPAIR_CHANGED_TRADE"):
        audit_effective_request(ctx, original, bundles.__getitem__)


def test_missing_price_repair_is_independently_auditable(tmp_path, monkeypatch):
    ctx, original, bundles = repair_fixture(tmp_path, monkeypatch, RepairingProvider(missing_prices=True))
    request = audit_effective_request(ctx, original, bundles.__getitem__)
    assert request["local_validation_schema"]["properties"]["stop_price"]["type"] == "number"


def test_independent_auditor_rejects_dropped_original_risk_field(tmp_path, monkeypatch):
    ctx, original, bundles = repair_fixture(tmp_path, monkeypatch, RepairingProvider(legacy_risk=True))
    request = audit_effective_request(ctx, original, bundles.__getitem__)
    assert request["local_validation_schema"]["properties"]["requested_risk_fraction"]["minimum"] == 0.08
    changed = json.loads(ctx["model_raw_response"])
    changed["requested_risk_fraction"] = None
    ctx["model_raw_response"] = json.dumps(changed)
    with pytest.raises(ValueError, match="REPAIR_CHANGED_TRADE"):
        audit_effective_request(ctx, original, bundles.__getitem__)


def test_rehashed_wrapper_cannot_make_existing_prices_mutable(tmp_path, monkeypatch):
    ctx, original, bundles = repair_fixture(tmp_path, monkeypatch)
    attempt = ctx["model_inference_settings"]["model_attempts"][-1]
    request = bundles[attempt["evidence_bundle_id"]]["prompt_request"]
    wrapper = json.loads(request["messages"][1]["content"])
    wrapper["mutable_fields"].append("entry_price")
    request["messages"][1]["content"] = json.dumps(wrapper)
    request["request_hash"] = attempt["request_hash"] = digest({"messages": request["messages"]})
    with pytest.raises(ValueError, match="REPAIR_CHANGED_TRADE"):
        audit_effective_request(ctx, original, bundles.__getitem__)


def test_modified_repair_facts_are_rejected_even_with_rehashed_request(tmp_path, monkeypatch):
    ctx, original, bundles = repair_fixture(tmp_path, monkeypatch)
    attempt = ctx["model_inference_settings"]["model_attempts"][-1]
    request = bundles[attempt["evidence_bundle_id"]]["prompt_request"]
    wrapper = json.loads(request["messages"][1]["content"])
    wrapper["inputs"]["account_truth"]["equity"] = 999999
    request["messages"][1]["content"] = json.dumps(wrapper)
    request["request_hash"] = attempt["request_hash"] = digest({"messages": request["messages"]})
    with pytest.raises(ValueError, match="REPAIR_CHANGED_FROZEN_FACTS"):
        audit_effective_request(ctx, original, bundles.__getitem__)


def test_only_old_candles_allow_exact_six_significant_digit_projection():
    old = {"candles": [[113086.3, 113086.4], [112978.8, 112740.0]]}
    compact = {"candles": [[113086.0, 113086.0], [112978.8, 112740.0]]}
    assert _frozen_projection(compact, old)
    compact["candles"][-1][1] = 112740.1
    assert not _frozen_projection(compact, old)
