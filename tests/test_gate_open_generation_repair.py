"""Bounded model-authored repairs; synthetic fixtures are not trading evidence."""
import json
from copy import deepcopy

import pytest

from core.replay import ai_template_runner as runner
from core.trading.model_schemas import gate_open_schema_repair_fields
from tests.test_ai_template_runner import FixtureProvider, history, rows


PROPOSAL = {"action": "OPEN_LONG", "instrument_id": "ETHUSDT", "reason": "fixture sweep",
            "confidence": 76, "entry_price": 100, "stop_price": 90, "take_profit": 200}
MISSING = "NOFX_GATE_OPEN_CONTRACT_MISSING:position_size_usdt,requested_leverage,order_preference"


def test_missing_contract_fields_are_identified_without_mutating_or_inventing_values():
    original = deepcopy(PROPOSAL)
    assert gate_open_schema_repair_fields(PROPOSAL, MISSING, ["ETHUSDT"]) == {
        "position_size_usdt", "requested_leverage", "order_preference", "evidence_refs"}
    assert PROPOSAL == original


@pytest.mark.parametrize("field,value", [("action", "WAIT"), ("instrument_id", "BTCUSDT"),
    ("reason", ""), ("entry_price", 0), ("stop_price", float("nan")), ("confidence", True)])
def test_invalid_identity_or_invalid_price_cannot_start_a_repair(field, value):
    assert not gate_open_schema_repair_fields({**PROPOSAL, field: value}, MISSING, ["ETHUSDT"])


@pytest.mark.parametrize("error", ["MODEL_TIMEOUT", "SMART_MODEL_MISMATCH",
    "INVALID_ACTION_SCHEMA:output.evidence_refs[0]:enum", "INVALID_ACTION_SCHEMA:output.entry_price:range"])
def test_unrelated_errors_cannot_start_gate_repair(error):
    assert not gate_open_schema_repair_fields(PROPOSAL, error, ["ETHUSDT"])


class RepairingProvider(FixtureProvider):
    def __init__(self, *, bad_ttl=False, drift=False, twice_bad=False, missing_prices=False, null_repair=False,
                 legacy_risk=False, drop_legacy_risk=False):
        super().__init__()
        self.bad_ttl, self.drift, self.twice_bad = bad_ttl, drift, twice_bad
        self.missing_prices, self.null_repair = missing_prices, null_repair
        self.legacy_risk, self.drop_legacy_risk = legacy_risk, drop_legacy_risk
        self.initial = None

    def generate_json(self, messages, **options):
        payload = json.loads(messages[1]["content"])
        repair = "previous_decision" in payload
        if repair:
            assert payload["previous_decision"] == self.initial
            assert options["schema"]["properties"]["action"]["enum"] == ["OPEN_LONG"]
            assert options["schema"]["properties"]["instrument_id"]["enum"] == ["ETHUSDT"]
            assert options["schema"]["properties"]["position_size_usdt"]["type"] == "number"
            assert options["schema"]["properties"]["requested_leverage"]["type"] == "integer"
            assert options["schema"]["properties"]["order_preference"]["enum"] == ["LIMIT", "MARKET"]
            inputs = payload["inputs"]
        else:
            inputs = payload
        refs = [r for r in inputs["evidence_refs"] if r.startswith("market_snapshot:")]
        decision = {**PROPOSAL, "evidence_refs": refs, "position_size_usdt": 10,
                    "requested_leverage": 2, "order_preference": "LIMIT", "ttl_seconds": 900}
        if self.legacy_risk:
            decision["requested_risk_fraction"] = None if repair and self.drop_legacy_risk else 0.08
        if not repair or self.twice_bad:
            for field in ("position_size_usdt", "requested_leverage", "order_preference"):
                decision.pop(field)
            if self.bad_ttl:
                decision["ttl_seconds"] = 3600
        if not repair:
            if self.missing_prices:
                decision["entry_price"] = None
                decision["stop_price"] = None
                decision["evidence_refs"] = []
            self.initial = deepcopy(decision)
        elif self.drift:
            decision["entry_price"] = 101
        elif self.null_repair:
            decision["stop_price"] = None
        self.payloads.append(payload)
        raw = json.dumps(decision)
        return decision, raw, {"model_id": self.model_id, "model_version": self.model_id,
            "actual_model_id": self.model_id, "model_identity_source": "completion_response",
            "verified_manifest_model_id": self.model_id}


@pytest.mark.parametrize("bad_ttl", [False, True])
def test_actual_coordinator_repairs_one_open_in_same_frozen_context(tmp_path, monkeypatch, bad_ttl):
    from core.model_client import model_client
    monkeypatch.setattr(model_client, "count_tokens", lambda content, **kwargs: max(1, len(content)//3))
    provider = RepairingProvider(bad_ttl=bad_ttl)
    path = tmp_path / "replay.sqlite3"
    report = runner.run_ai_template_replay(history(5), db_path=path, model_provider=provider,
        priority_guard=lambda: None, max_decisions=1)
    assert len(provider.payloads) == 2 and not report["errors"]
    row = rows(path)[0]
    decision, context = json.loads(row["decision_json"]), json.loads(row["context_json"])
    assert decision["action"] == "OPEN_LONG" and decision["entry_price"] == 100
    assert decision["position_size_usdt"] == 10 and decision["requested_leverage"] == 2
    assert decision["ttl_seconds"] == 900
    assert len(context["model_inference_settings"]["model_response_audit"]["attempts"]) == 2
    assert provider.payloads[1]["inputs"]["market_snapshots"] == provider.payloads[0]["market_snapshots"]
    assert provider.payloads[1]["inputs"]["account_truth"] == provider.payloads[0]["account_truth"]
    assert json.loads(row["result_json"])["private_exchange_calls"] == 0


@pytest.mark.parametrize("mode,error", [("drift", "INVALID_ACTION_SCHEMA:output.entry_price:range"),
                                       ("twice_bad", "INVALID_ACTION_SCHEMA:output:fields")])
def test_repair_cannot_change_price_or_retry_without_bound(tmp_path, monkeypatch, mode, error):
    from core.model_client import model_client
    monkeypatch.setattr(model_client, "count_tokens", lambda content, **kwargs: max(1, len(content)//3))
    provider = RepairingProvider(**{mode: True})
    report = runner.run_ai_template_replay(history(5), db_path=tmp_path / "replay.sqlite3",
        model_provider=provider, priority_guard=lambda: None, max_decisions=1)
    assert len(provider.payloads) == 2
    assert error in report["errors"][0]["error"]
    assert all(item["fills"] == 0 for item in report["results"])


@pytest.mark.parametrize("drop", [False, True])
def test_gate_repair_preserves_existing_legacy_risk_field_without_applying_it_as_sizing_rule(tmp_path, monkeypatch, drop):
    from core.model_client import model_client
    monkeypatch.setattr(model_client, "count_tokens", lambda content, **kwargs: max(1, len(content)//3))
    provider = RepairingProvider(legacy_risk=True, drop_legacy_risk=drop)
    path = tmp_path / "replay.sqlite3"
    report = runner.run_ai_template_replay(history(5), db_path=path, model_provider=provider,
        priority_guard=lambda: None, max_decisions=1)
    assert len(provider.payloads) == 2
    if drop:
        assert "output.requested_risk_fraction:type" in report["errors"][0]["error"]
        assert rows(path)[0]["status"] == "ERROR"
    else:
        assert not report["errors"]
        assert json.loads(rows(path)[0]["decision_json"])["requested_risk_fraction"] == 0.08


def test_native_trade_pins_use_numeric_bounds_and_do_not_fill_mutable_values():
    from core.trading.model_schemas import AI_ACTION_SCHEMA, pin_gate_repair_trade_fields, gemini_response_format
    original = deepcopy(AI_ACTION_SCHEMA)
    proposal = {**PROPOSAL, "requested_risk_fraction": 0.08, "entry_price": None}
    pinned = pin_gate_repair_trade_fields(original, proposal, {"entry_price", "position_size_usdt"})
    native = gemini_response_format(pinned)["json_schema"]["schema"]
    risk = native["properties"]["requested_risk_fraction"]
    assert risk["type"] == "number" and risk["minimum"] == risk["maximum"] == 0.08
    assert "enum" not in risk and "requested_risk_fraction" in native["required"]
    assert native["properties"]["instrument_id"]["enum"] == ["ETHUSDT"]
    assert pinned["properties"]["entry_price"] == original["properties"]["entry_price"]
    assert pinned["properties"]["position_size_usdt"] == original["properties"]["position_size_usdt"]
    assert original == AI_ACTION_SCHEMA and proposal["entry_price"] is None


@pytest.mark.parametrize("null_repair", [False, True])
def test_missing_prices_require_model_repair_not_execution_defaults(tmp_path, monkeypatch, null_repair):
    from core.model_client import model_client
    monkeypatch.setattr(model_client, "count_tokens", lambda content, **kwargs: max(1, len(content)//3))
    provider = RepairingProvider(missing_prices=True, null_repair=null_repair)
    path = tmp_path / "replay.sqlite3"
    report = runner.run_ai_template_replay(history(5), db_path=path, model_provider=provider,
        priority_guard=lambda: None, max_decisions=1)
    assert len(provider.payloads) == 2
    assert {"entry_price", "stop_price", "evidence_refs"} <= set(provider.payloads[1]["mutable_fields"])
    if null_repair:
        assert "stop_price:type" in report["errors"][0]["error"]
        assert rows(path)[0]["status"] == "ERROR"
    else:
        assert not report["errors"]
        decision = json.loads(rows(path)[0]["decision_json"])
        assert decision["entry_price"] == 100 and decision["stop_price"] == 90
        assert provider.initial["entry_price"] is None
    assert all(item["fills"] == 0 for item in report["results"])
