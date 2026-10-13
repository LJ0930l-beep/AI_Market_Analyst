"""Validate the blocker register against the V2.0 playbook without extra dependencies."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ALLOWED = {
    "severity": {"S0", "S1", "S2", "S3"},
    "status": {"OPEN", "ISOLATED", "FIX_IN_REVIEW", "VERIFIED", "BLOCKED_WITH_EVIDENCE"},
    "component": {"model", "data", "risk", "gate", "ui", "ci", "integration", "compliance"},
    "mode": {"RESEARCH_OFFLINE", "REPLAY", "PAPER", "TESTNET", "SHADOW", "LIVE"},
    "user_authorization": {"NONE", "DOCUMENTED_SCOPE_ONLY"},
    "risk_to_funds": {"NONE", "UNKNOWN", "POTENTIAL", "CONFIRMED"},
    "root_cause_status": {"CONFIRMED", "HYPOTHESIS", "UNKNOWN"},
}
REQUIRED = {
    "id", "severity", "status", "component", "mode", "branch_sha", "trigger_signature",
    "first_seen_at_utc", "user_authorization", "external_cost_or_order_attempted", "remote_effect_known",
    "risk_to_funds", "affected_sample_or_order_ids", "immediate_stop_or_isolation", "evidence_files_and_hashes",
    "reproduction", "root_cause_status", "safe_fallback", "patch_pr", "negative_tests", "unblock_criteria",
    "needs_human_decision", "next_owner_action", "rollback",
}
INDEX_REQUIRED = {"id", "severity", "status", "component", "file", "trigger_signature", "next_owner_action"}
BOOLEAN_FIELDS = {"external_cost_or_order_attempted", "remote_effect_known", "needs_human_decision"}
FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s+(.*))?$")
INDEX_ITEM_RE = re.compile(r"^  - id:\s*(\S+)\s*$")
INDEX_FIELD_RE = re.compile(r"^    ([A-Za-z_][A-Za-z0-9_-]*):(?:\s+(.*))?$")


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_flat_fields(lines: list[str], field_re: re.Pattern[str], errors: list[str], label: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = field_re.match(line)
        if not match:
            errors.append(f"{label}:{line_number}: expected a flat key/value field")
            continue
        key, raw_value = match.groups()
        if key in fields:
            errors.append(f"{label}:{line_number}: duplicate field {key}")
            continue
        fields[key] = _scalar(raw_value or "")
    return fields


def _parse_index(path: Path, errors: list[str]) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    records: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    found_blockers = False
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not found_blockers:
            if line.strip() == "blockers:":
                found_blockers = True
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item_match = INDEX_ITEM_RE.match(line)
        if item_match:
            if current is not None:
                records.append(current)
            current = {"id": item_match.group(1)}
            continue
        if current is None or not line.strip():
            errors.append(f"{path}:{line_number}: expected a blocker entry")
            continue
        field_match = INDEX_FIELD_RE.match(line)
        if field_match:
            key, raw_value = field_match.groups()
            if key in current:
                errors.append(f"{path}:{line_number}: duplicate index field {key}")
                continue
            current[key] = _scalar(raw_value or "")
        else:
            errors.append(f"{path}:{line_number}: malformed blocker index line")
    if not found_blockers:
        errors.append(f"{path}: missing blockers: section")
    if current is not None:
        records.append(current)
    return records


def _parse_blocker(path: Path, errors: list[str]) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"(?ms)^```yaml\s*\n(.*?)^```\s*$", text)
    if not match:
        errors.append(f"{path}: missing fenced YAML blocker record")
        return {}
    return _parse_flat_fields(match.group(1).splitlines(), FIELD_RE, errors, str(path))


def validate_register(repo_root: Path) -> list[str]:
    """Return contract violations for index entries and their Markdown records."""
    root = repo_root / "docs" / "blockers"
    errors: list[str] = []
    index_path = root / "index.yaml"
    records = _parse_index(index_path, errors)
    if not records:
        return errors + [f"{index_path}: no blocker records found"]

    ids: set[str] = set()
    listed_files: set[Path] = set()
    blocker_root = root.resolve()
    for record in records:
        blocker_id = record.get("id", "")
        if blocker_id in ids:
            errors.append(f"{index_path}: duplicate blocker id {blocker_id}")
        ids.add(blocker_id)
        missing_index_fields = sorted(INDEX_REQUIRED - record.keys())
        if missing_index_fields:
            errors.append(f"{index_path}: {blocker_id} missing fields {', '.join(missing_index_fields)}")
        for field in sorted(INDEX_REQUIRED):
            if field in record and not record[field].strip():
                errors.append(f"{index_path}: {blocker_id} has empty {field}")
        if record.get("severity") not in ALLOWED["severity"]:
            errors.append(f"{index_path}: {blocker_id} has invalid severity={record.get('severity')!r}")
        for field in ("status", "component"):
            if record.get(field) not in ALLOWED[field]:
                errors.append(f"{index_path}: {blocker_id} has invalid {field}={record.get(field)!r}")
        relative_file = record.get("file", "")
        if not relative_file:
            errors.append(f"{index_path}: {blocker_id} has no file reference")
            continue
        blocker_path = (repo_root / relative_file).resolve()
        try:
            blocker_path.relative_to(blocker_root)
        except ValueError:
            errors.append(f"{index_path}: {blocker_id} file escapes docs/blockers: {relative_file}")
            continue
        if blocker_path.parent != blocker_root:
            errors.append(f"{index_path}: {blocker_id} file must be directly under docs/blockers: {relative_file}")
            continue
        if blocker_path.stem != blocker_id and not blocker_path.stem.startswith(f"{blocker_id}-"):
            errors.append(f"{index_path}: {blocker_id} file name does not match its ID: {relative_file}")
        listed_files.add(blocker_path)
        if not blocker_path.is_file():
            errors.append(f"{index_path}: {blocker_id} references missing file {relative_file}")
            continue
        fields = _parse_blocker(blocker_path, errors)
        missing = sorted(REQUIRED - fields.keys())
        if missing:
            errors.append(f"{blocker_path}: missing required fields {', '.join(missing)}")
        for key in REQUIRED:
            if key in fields and not fields[key].strip():
                errors.append(f"{blocker_path}: empty required field {key}")
        for field, allowed in ALLOWED.items():
            if fields.get(field) not in allowed:
                errors.append(f"{blocker_path}: invalid {field}={fields.get(field)!r}")
        for field in sorted(BOOLEAN_FIELDS):
            if fields.get(field) not in {"true", "false"}:
                errors.append(f"{blocker_path}: {field} must be true or false")
        for field in ("id", "severity", "status", "component", "trigger_signature"):
            if fields.get(field) != record.get(field):
                errors.append(
                    f"{blocker_path}: {field} mismatch with index "
                    f"({fields.get(field)!r} != {record.get(field)!r})"
                )

    for blocker_path in root.glob("B-*.md"):
        if blocker_path.resolve() not in listed_files:
            errors.append(f"{blocker_path}: blocker file is not listed in index.yaml")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    errors = validate_register(args.repo_root.resolve())
    if errors:
        print("Blocker register validation FAILED:")
        for error in errors:
            print(f"- {error}")
        return 1
    index = args.repo_root / "docs" / "blockers" / "index.yaml"
    print(f"Blocker register valid: {len(_parse_index(index, []))} records conform to the V2.0 playbook.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
