from __future__ import annotations

from pathlib import Path

import pytest

from scripts.validate_blocker_register import validate_register


def _write_fixture(
    root: Path, *, status: str = "FIX_IN_REVIEW", component: str = "integration", rollback: str = "Keep work isolated."
) -> None:
    blocker_id = "B-20990101-001"
    (root / "docs" / "blockers").mkdir(parents=True)
    fields = {
        "id": blocker_id,
        "severity": "S1",
        "status": status,
        "component": component,
        "mode": "RESEARCH_OFFLINE",
        "branch_sha": "1" * 40,
        "trigger_signature": "FIXTURE_BLOCKER",
        "first_seen_at_utc": "2099-01-01T00:00:00Z",
        "user_authorization": "DOCUMENTED_SCOPE_ONLY",
        "external_cost_or_order_attempted": "false",
        "remote_effect_known": "false",
        "risk_to_funds": "NONE",
        "affected_sample_or_order_ids": "fixture only",
        "immediate_stop_or_isolation": "No external requests.",
        "playbook_codes": "B22",
        "original_evidence": "A fixed offline fixture reproduced the blocker.",
        "evidence_files_and_hashes": "fixture evidence SHA-256 " + "a" * 64,
        "reproduction": "Offline fixture.",
        "root_cause_status": "CONFIRMED",
        "root_cause": "The fixture omits the required proof.",
        "temporary_mitigation": "Keep the affected gate closed pending review.",
        "safe_fallback": "Keep the gate closed.",
        "patch_pr": "none",
        "repair_method": "Add the missing offline validation and regression coverage.",
        "negative_tests": "Invalid status is rejected.",
        "unblock_criteria": "All checks pass.",
        "needs_human_decision": "false",
        "next_owner_action": "Review the offline fix.",
        "rollback": rollback,
    }
    body = "```yaml\n" + "\n".join(f"{key}: {value}" for key, value in fields.items()) + "\n```\n"
    (root / "docs" / "blockers" / f"{blocker_id}.md").write_text(body, encoding="utf-8")
    (root / "docs" / "blockers" / "index.yaml").write_text(
        "schema_version: V2_BLOCKER_INDEX_V2\nblockers:\n"
        f"  - id: {blocker_id}\n"
        "    severity: S1\n"
        f"    status: {status}\n"
        f"    component: {component}\n"
        "    trigger_signature: FIXTURE_BLOCKER\n"
        "    playbook_codes: B22\n"
        f"    file: docs/blockers/{blocker_id}.md\n"
        "    next_owner_action: Review the offline fix.\n",
        encoding="utf-8",
    )


def test_repository_blockers_follow_the_playbook_contract() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    assert validate_register(repo_root) == []


@pytest.mark.parametrize(
    ("status", "component", "rollback", "expected"),
    [
        ("RESOLVED", "integration", "Keep work isolated.", "invalid status"),
        ("FIX_IN_REVIEW", "gate_account_truth", "Keep work isolated.", "invalid component"),
        ("FIX_IN_REVIEW", "integration", "", "empty required field rollback"),
    ],
)
def test_register_rejects_out_of_contract_records(
    tmp_path: Path,
    status: str,
    component: str,
    rollback: str,
    expected: str,
) -> None:
    _write_fixture(tmp_path, status=status, component=component, rollback=rollback)
    errors = validate_register(tmp_path)
    assert any(expected in error for error in errors)


def test_register_rejects_malformed_index_lines(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    index_path = tmp_path / "docs" / "blockers" / "index.yaml"
    index_path.write_text(
        index_path.read_text(encoding="utf-8").replace("    component: integration", "    component integration"),
        encoding="utf-8",
    )

    errors = validate_register(tmp_path)

    assert any("malformed blocker index line" in error for error in errors)


def test_register_rejects_missing_required_index_fields(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    index_path = tmp_path / "docs" / "blockers" / "index.yaml"
    index_path.write_text(
        index_path.read_text(encoding="utf-8").replace("    next_owner_action: Review the offline fix.\n", ""),
        encoding="utf-8",
    )

    errors = validate_register(tmp_path)

    assert any("missing fields next_owner_action" in error for error in errors)


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("root_cause", "", "empty required field root_cause"),
        ("original_evidence", "", "empty required field original_evidence"),
        ("temporary_mitigation", "", "empty required field temporary_mitigation"),
        ("repair_method", "", "empty required field repair_method"),
        ("playbook_codes", "B00", "playbook_codes must contain only B01 through B26"),
        ("playbook_codes", "B22,B22", "playbook_codes contains duplicates"),
    ],
)
def test_register_requires_traceable_playbook_and_disposition_fields(
    tmp_path: Path,
    field: str,
    value: str,
    expected: str,
) -> None:
    _write_fixture(tmp_path)
    blocker_path = next((tmp_path / "docs" / "blockers").glob("B-*.md"))
    content = blocker_path.read_text(encoding="utf-8")
    if field == "playbook_codes":
        content = content.replace("playbook_codes: B22", f"playbook_codes: {value}")
    else:
        content = content.replace(
            f"{field}: "
            + next(line.partition(": ")[2] for line in content.splitlines() if line.startswith(f"{field}: ")),
            f"{field}: {value}",
        )
    blocker_path.write_text(content, encoding="utf-8")
    index_path = tmp_path / "docs" / "blockers" / "index.yaml"
    index_text = index_path.read_text(encoding="utf-8")
    if field == "playbook_codes":
        index_path.write_text(index_text.replace("playbook_codes: B22", f"playbook_codes: {value}"), encoding="utf-8")

    errors = validate_register(tmp_path)

    assert any(expected in error for error in errors)


def test_register_rejects_wrong_schema_version(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    index_path = tmp_path / "docs" / "blockers" / "index.yaml"
    index_path.write_text(
        index_path.read_text(encoding="utf-8").replace("V2_BLOCKER_INDEX_V2", "V2_BLOCKER_INDEX_V1"), encoding="utf-8"
    )

    errors = validate_register(tmp_path)

    assert any("schema_version must be V2_BLOCKER_INDEX_V2" in error for error in errors)


def test_register_rejects_empty_index_actions(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    index_path = tmp_path / "docs" / "blockers" / "index.yaml"
    index_path.write_text(
        index_path.read_text(encoding="utf-8").replace(
            "    next_owner_action: Review the offline fix.", "    next_owner_action:"
        ),
        encoding="utf-8",
    )

    errors = validate_register(tmp_path)

    assert any("has empty next_owner_action" in error for error in errors)


def test_register_rejects_blocker_paths_outside_register_directory(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    index_path = tmp_path / "docs" / "blockers" / "index.yaml"
    outside_path = tmp_path / "docs" / "outside.md"
    blocker_path = tmp_path / "docs" / "blockers" / "B-20990101-001.md"
    outside_path.write_text(blocker_path.read_text(encoding="utf-8"), encoding="utf-8")
    index_path.write_text(
        index_path.read_text(encoding="utf-8").replace("docs/blockers/B-20990101-001.md", "docs/outside.md"),
        encoding="utf-8",
    )

    errors = validate_register(tmp_path)

    assert any("file escapes docs/blockers" in error for error in errors)
