"""Produce reproducible local Phase 7 dependency, license and secret evidence.

Optional tools are reported as ``unavailable`` rather than being treated as a
clean result.  The report intentionally stores summaries, not raw dependency
audit payloads or source snippets.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----")
SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|private[_-]?key)\s*[:=]\s*['\"][^'\"]{16,}['\"]"
)


def _run(command: list[str], *, cwd: Path, timeout: int = 120) -> dict[str, Any]:
    if shutil.which(command[0]) is None and not Path(command[0]).exists():
        return {"status": "unavailable", "command": command, "exit_code": None, "message": f"{command[0]} not found"}
    try:
        completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "command": command, "exit_code": None}
    output = (completed.stdout or "").strip()
    error = (completed.stderr or "").strip()
    return {
        "status": "pass" if completed.returncode == 0 else "fail",
        "command": command,
        "exit_code": completed.returncode,
        "stdout_tail": output[-500:] if output else None,
        "stderr_tail": error[-500:] if error else None,
    }


def _npm_command() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"


def _npm_audit(repo: Path, *, production: bool) -> dict[str, Any]:
    command = [_npm_command(), "audit", "--audit-level=high", "--json"]
    if production:
        command.insert(2, "--omit=dev")
    if shutil.which(command[0]) is None:
        return {"status": "unavailable", "command": command, "exit_code": None, "message": "npm not found"}
    try:
        completed = subprocess.run(command, cwd=repo / "web", capture_output=True, text=True, timeout=180, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "command": command, "exit_code": None}
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    vulnerabilities = payload.get("metadata", {}).get("vulnerabilities", {}) if isinstance(payload, dict) else {}
    return {
        "status": "pass" if completed.returncode == 0 else "fail",
        "command": command,
        "exit_code": completed.returncode,
        "vulnerabilities": vulnerabilities if isinstance(vulnerabilities, dict) else {},
        "packages": payload.get("metadata", {}).get("dependencies", {}) if isinstance(payload, dict) else {},
        "stderr_tail": (completed.stderr or "").strip()[-500:] or None,
    }


def _tracked_secret_scan(repo: Path) -> dict[str, Any]:
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=repo, capture_output=True, check=True).stdout.split(b"\0")
    candidates = {Path(os.fsdecode(raw_path)) for raw_path in listed if raw_path}
    # Include new source files before they are committed; avoid node_modules,
    # generated build output and user databases by construction.
    for relative_root in ("core", "apps", "scripts", "tests", "web/src", "web/e2e", "docs"):
        root = repo / relative_root
        if root.is_dir():
            candidates.update(path.relative_to(repo) for path in root.rglob("*") if path.is_file())
    findings: list[dict[str, str]] = []
    skipped_binary = 0
    for relative in sorted(candidates):
        if any(part in {"node_modules", "data", ".git"} for part in relative.parts):
            continue
        path = repo / relative
        try:
            content = path.read_bytes()
        except OSError:
            continue
        if b"\0" in content:
            skipped_binary += 1
            continue
        text = content.decode("utf-8", errors="ignore")
        if PRIVATE_KEY_PATTERN.search(text):
            findings.append({"path": relative.as_posix(), "category": "private_key_marker"})
        if SECRET_ASSIGNMENT_PATTERN.search(text) and "os.environ" not in text:
            findings.append({"path": relative.as_posix(), "category": "literal_secret_assignment"})
    return {
        "status": "pass" if not findings else "review_required",
        "patterns": ["private_key_marker", "literal_secret_assignment"],
        "finding_count": len(findings),
        "findings": findings,
        "binary_files_skipped": skipped_binary,
    }


def _python_license_inventory(repo: Path) -> dict[str, Any]:
    metadata_file = repo / "pyproject.toml"
    project = tomllib.loads(metadata_file.read_text(encoding="utf-8"))
    declared: list[str] = []
    optional = project.get("project", {}).get("optional-dependencies", {})
    if isinstance(optional, dict):
        for requirements in optional.values():
            if isinstance(requirements, list):
                declared.extend(str(item).split("[", 1)[0].split(">", 1)[0].split("=", 1)[0].split("<", 1)[0].strip() for item in requirements)
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for name in sorted(set(item for item in declared if item)):
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            missing.append(name)
            continue
        license_name = distribution.metadata.get("License") or None
        if not license_name or license_name == "UNKNOWN":
            classifiers = [item for item in distribution.metadata.get_all("Classifier", []) if item.startswith("License ::")]
            license_name = classifiers[0] if classifiers else None
        rows.append({"name": distribution.metadata.get("Name", name), "version": distribution.version, "license": license_name})
    return {
        "declared_optional_dependencies": sorted(set(item for item in declared if item)),
        "installed": rows,
        "not_installed": missing,
        "status": "review_required" if missing or any(not item["license"] for item in rows) else "inventory_complete",
        "compatibility": "No legal compatibility conclusion is inferred; review the listed licenses before redistribution.",
    }


def _npm_license_inventory(repo: Path) -> dict[str, Any]:
    lock = json.loads((repo / "web" / "package-lock.json").read_text(encoding="utf-8"))
    packages = lock.get("packages", {})
    rows: list[dict[str, Any]] = []
    unknown: list[str] = []
    if isinstance(packages, dict):
        for package_key in sorted(key for key in packages if key):
            package_path = repo / "web" / package_key
            metadata_path = package_path / "package.json"
            license_name: Any = None
            if metadata_path.is_file():
                try:
                    package_json = json.loads(metadata_path.read_text(encoding="utf-8"))
                    license_name = package_json.get("license") or package_json.get("licenses")
                except (OSError, UnicodeError, json.JSONDecodeError):
                    license_name = None
            if not license_name:
                unknown.append(package_key)
            rows.append({"package": package_key, "license": license_name})
    return {
        "package_count": len(rows),
        "unknown_license_count": len(unknown),
        "unknown_packages": unknown,
        "status": "review_required" if unknown else "inventory_complete",
        "compatibility": "No legal compatibility conclusion is inferred; review the listed licenses before redistribution.",
    }


def build_report(repo: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    pip_check = _run([sys.executable, "-m", "pip", "check"], cwd=repo)
    pip_check["command"] = ["python", "-m", "pip", "check"]
    security = {
        "report_version": "phase7_security_audit_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "tracked and current scoped source plus installed dependency audit commands; no data databases or node_modules source scan",
        "python_pip_check": pip_check,
        "python_pip_audit": _run(["pip-audit", "--format", "json"], cwd=repo),
        "npm_audit_full": _npm_audit(repo, production=False),
        "npm_audit_production": _npm_audit(repo, production=True),
        "tracked_secret_scan": _tracked_secret_scan(repo),
        "configuration_boundary": {
            "default_bind": "127.0.0.1",
            "scheduler_default_enabled": False,
            "external_notifications": False,
            "broker_or_real_order": False,
            "private_keys": False,
            "telemetry": False,
            "timeouts_retries_bounded": True,
        },
    }
    licenses = {
        "report_version": "phase7_license_inventory_v1",
        "generated_at": security["generated_at"],
        "python": _python_license_inventory(repo),
        "npm": _npm_license_inventory(repo),
        "legal_note": "This is an inventory and compatibility review input, not legal advice or a license approval.",
    }
    return security, licenses


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write Phase 7 security and license audit summaries.")
    parser.add_argument("--output-dir", default="docs", help="directory for the two JSON artifacts")
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    output_dir = (repo / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    security, licenses = build_report(repo)
    (output_dir / "phase7-security-audit.json").write_text(json.dumps(security, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "phase7-license-audit.json").write_text(json.dumps(licenses, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    failed = any(
        item.get("status") == "fail"
        for item in (security["python_pip_check"], security["npm_audit_full"], security["npm_audit_production"])
    )
    print(json.dumps({"security": security, "licenses": licenses}, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
