from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from core.replay.pa_decision_quality_v38.authorization_guard import (
    AUTHORIZATION_REVOCATION_SCHEMA_VERSION,
    REVOCATION_REGISTRY_PATH,
    AuthorizationRevocationError,
    authorization_execution_status,
    require_authorization_not_revoked,
)
from core.replay.pa_decision_quality_v38.format_pilot import (
    FORMAT_PILOT_AUTHORIZATION_ID,
    FORMAT_PILOT_SPEC,
    run_format_pilot,
)
from core.replay.pa_decision_quality_v38.gemini_runner import (
    execute_authorized_optimization,
    load_authorization,
    load_prompt_templates,
)
from core.replay.pa_decision_quality_v38.market_only import build_market_only_input


def test_consumed_campaign_authorization_is_rejected_with_an_empty_clone_ledger(
    tmp_path: Path,
    market_point_factory,
):
    root = Path(__file__).resolve().parents[2]
    authorization = load_authorization(
        root / "configs" / "research" / "authorizations" / "v38-gemini-optimization-run-20261010-v1.json",
    )
    prompts = load_prompt_templates(root)
    market_input = build_market_only_input(market_point_factory())
    ledger = tmp_path / "reports" / "v38+" / "runs" / "empty-clone-ledger.jsonl"
    provider_calls: list[str] = []

    with pytest.raises(ValueError, match="RESEARCH_AUTHORIZATION_REVOKED"):
        execute_authorized_optimization(
            [market_input],
            prompts,
            authorization=authorization,
            ledger_path=ledger,
            call_model=lambda _messages, request_id: provider_calls.append(request_id),
            max_contexts=1,
        )

    assert provider_calls == []
    assert not ledger.exists()


def test_revocation_registry_marks_both_exhausted_scopes():
    assert authorization_execution_status("V38_GEMINI_OPTIMIZATION_A1_A2_20261010_V1") == "CONSUMED_REVOKED"
    assert authorization_execution_status(FORMAT_PILOT_AUTHORIZATION_ID) == "CONSUMED_REVOKED"


def test_unlisted_authorization_id_is_not_confused_with_a_revoked_scope():
    assert authorization_execution_status("FRESH_UNLISTED_AUTHORIZATION_ID") == "NOT_REVOKED"


def test_missing_revocation_registry_fails_closed(tmp_path: Path):
    with pytest.raises(AuthorizationRevocationError, match="AUTHORIZATION_REVOCATION_REGISTRY_UNAVAILABLE"):
        require_authorization_not_revoked("FRESH_NOT_REVOKED_ID", registry_path=tmp_path / "missing.json")


def test_malformed_revocation_registry_fails_closed(tmp_path: Path):
    registry = tmp_path / "invalid.json"
    registry.write_text('{"schema_version":"unknown","revocations":[]}', encoding="utf-8")
    with pytest.raises(AuthorizationRevocationError, match="AUTHORIZATION_REVOCATION_REGISTRY_INVALID"):
        require_authorization_not_revoked("FRESH_NOT_REVOKED_ID", registry_path=registry)


def test_revocation_registry_requires_matching_authorization_file_hash(tmp_path: Path):
    registry = json.loads(REVOCATION_REGISTRY_PATH.read_text(encoding="utf-8"))
    registry["schema_version"] = AUTHORIZATION_REVOCATION_SCHEMA_VERSION
    registry["revocations"][0]["authorization_record_sha256"] = "0" * 64
    path = tmp_path / "tampered-hash.json"
    path.write_text(json.dumps(registry), encoding="utf-8")

    with pytest.raises(AuthorizationRevocationError, match="AUTHORIZATION_REVOCATION_REGISTRY_INVALID"):
        authorization_execution_status(
            "V38_GEMINI_OPTIMIZATION_A1_A2_20261010_V1",
            registry_path=path,
        )


def test_campaign_cli_rejects_consumed_authorization_before_loading_runtime(
    monkeypatch,
    capsys,
):
    import scripts.run_market_only_gemini_v38 as cli

    auth = load_authorization(
        Path(__file__).resolve().parents[2]
        / "configs"
        / "research"
        / "authorizations"
        / "v38-gemini-optimization-run-20261010-v1.json",
    )
    monkeypatch.setattr(cli, "load_authorization", lambda _path: auth)
    monkeypatch.setattr(cli, "load_prompt_templates", lambda *_: pytest.fail("PROMPT_LOAD_AFTER_REVOKED_AUTH"))
    monkeypatch.setattr(cli, "_model_caller", lambda: pytest.fail("MODEL_CLIENT_AFTER_REVOKED_AUTH"))
    monkeypatch.setattr(
        cli,
        "_provider_route_reauthorization_evidence",
        lambda **_kwargs: pytest.fail("ROUTE_GET_AFTER_REVOKED_AUTH"),
    )

    result = cli.main(
        [
            "--input",
            "unused-input.json",
            "--manifest",
            "unused-manifest.json",
            "--execute",
            "--resume-after-provider-route-change",
        ]
    )

    assert result == 2
    assert "RESEARCH_AUTHORIZATION_REVOKED" in capsys.readouterr().err


def test_format_pilot_cli_rejects_consumed_authorization_before_model_client(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    import scripts.run_market_only_gemini_v38 as campaign_cli
    import scripts.run_v38_json_format_pilot as pilot_cli

    market_input = {"fixture": True}
    monkeypatch.setattr(pilot_cli, "ROOT", tmp_path)
    monkeypatch.setattr(pilot_cli, "load_pilot_materials", lambda *_: (market_input, {}, []))
    monkeypatch.setattr(pilot_cli, "_shared_repository_root", lambda: tmp_path)
    monkeypatch.setattr(
        pilot_cli,
        "run_format_pilot",
        lambda **_kwargs: {
            "status": "DRY_RUN",
            "pilot_kind": "FORMAT_ONLY",
            "authorization_id": FORMAT_PILOT_AUTHORIZATION_ID,
            "dispatch_intent_count": 0,
            "terminal_result_count": 0,
            "quality_sample_eligible": False,
            "orders_created": 0,
        },
    )
    monkeypatch.setattr(campaign_cli, "_model_caller", lambda: pytest.fail("MODEL_CLIENT_AFTER_REVOKED_AUTH"))
    monkeypatch.setattr(
        campaign_cli,
        "_provider_route_reauthorization_evidence",
        lambda **_kwargs: pytest.fail("ROUTE_GET_AFTER_REVOKED_AUTH"),
    )

    result = pilot_cli.main(["--run"])

    assert result == 2
    assert "RESEARCH_AUTHORIZATION_REVOKED" in capsys.readouterr().out


def test_consumed_one_intent_pilot_is_rejected_with_an_empty_clone_ledger(
    tmp_path: Path,
    market_point_factory,
):
    market_input = build_market_only_input(market_point_factory())
    spec = replace(
        FORMAT_PILOT_SPEC,
        authorization_id=FORMAT_PILOT_AUTHORIZATION_ID,
        decision_id=market_input["decision_id"],
        market_input_sha256=market_input["market_input_sha256"],
    )
    prompt = {"prompt_version": spec.prompt_version}
    messages = [
        {"role": "system", "content": "fixed local fixture"},
        {"role": "user", "content": "fixed local fixture"},
    ]
    reports = tmp_path / "reports" / "v38+" / "runs"
    ledger = reports / "empty-clone-pilot-ledger.jsonl"
    provider_calls: list[str] = []

    with pytest.raises(ValueError, match="RESEARCH_AUTHORIZATION_REVOKED"):
        run_format_pilot(
            market_input=market_input,
            prompt=prompt,
            messages=messages,
            ledger_path=ledger,
            report_path=reports / "pilot-result.json",
            repository_root=tmp_path,
            execute=True,
            call_model=lambda _messages, request_id: provider_calls.append(request_id),
            spec=spec,
        )

    assert provider_calls == []
    assert not ledger.exists()
