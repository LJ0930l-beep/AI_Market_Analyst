"""Evidence manifest generator and consistency verifier (AT30, RT24).

Produces evidence/manifest.json conforming to specification Section 18 & repair-plan v1.2:
- Test code versions, commit reference, date, platform, modes.
- Cryptographic SHA-256 hashes of core implementation files and tests.
- Explicit AT01-AT42 and RT01-RT26 status mapping defaulting to NOT_RUN.
- True report-driven transitions: only verified test reports promote status to PASS.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
import xml.etree.ElementTree as ET


SPEC_RELEASE_CODE = "V2R1"
SPEC_VERSION = "1.2"
SPEC_TITLE = "AI_Market_Analyst_V2R1_真实交易链路修复实施方案_v1.2"


def collect_source_state(
    workspace_root: str | Path = ".",
    *,
    exclude_paths: Optional[Iterable[str | Path]] = None,
) -> Dict[str, Any]:
    """Capture the exact dirty source set used by an evidence bundle."""
    root = Path(workspace_root).resolve()
    excluded = {
        Path(item).as_posix().lstrip("./")
        for item in (exclude_paths or [])
    }
    state: Dict[str, Any] = {"status": "NOT_AVAILABLE", "git_head": None, "dirty": False, "dirty_paths": [], "dirty_file_sha256": {}}
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=False, capture_output=True, text=True,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z"], cwd=root, check=False, capture_output=True,
        )
        if head.returncode != 0 or status.returncode != 0:
            return state
        raw_entries = status.stdout.decode("utf-8", errors="replace").split("\x00") if isinstance(status.stdout, bytes) else []
        paths: list[str] = []
        for entry in raw_entries:
            if len(entry) < 4:
                continue
            path = entry[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            normalized_path = path.replace("\\", "/").lstrip("./")
            if normalized_path in excluded:
                continue
            if path and path not in paths:
                paths.append(path)
        hashes: Dict[str, str] = {}
        for rel in paths:
            candidate = root / rel
            if candidate.is_file():
                hashes[rel] = compute_file_sha256(candidate)
        digest_input = "\n".join(f"{path}:{hashes.get(path, 'FILE_NOT_FOUND')}" for path in paths)
        state.update({
            "status": "AVAILABLE",
            "git_head": head.stdout.strip(),
            "dirty": bool(paths),
            "dirty_paths": paths,
            "dirty_file_sha256": hashes,
            "dirty_tree_sha256": hashlib.sha256(digest_input.encode("utf-8")).hexdigest(),
        })
    except (OSError, ValueError):
        return state
    return state


def compute_file_sha256(file_path: str | Path) -> str:
    """Compute SHA-256 hex digest of a given file."""
    path = Path(file_path)
    if not path.is_file():
        return "FILE_NOT_FOUND"
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            sha256.update(chunk)
    return sha256.hexdigest()


def parse_junit_report(report_path: Path) -> Dict[str, Dict[str, Any]]:
    """Parse a pytest JUnit XML report to extract passed test cases and timing."""
    if not report_path.is_file():
        return {}
    results = {}
    try:
        tree = ET.parse(report_path)
        root = tree.getroot()
        for testcase in root.iter("testcase"):
            name = testcase.attrib.get("name", "")
            time_sec = float(testcase.attrib.get("time", "0"))
            has_failure = testcase.find("failure") is not None or testcase.find("error") is not None
            has_skipped = testcase.find("skipped") is not None
            if has_failure:
                outcome = "FAIL"
            elif has_skipped:
                outcome = "SKIPPED"
            else:
                outcome = "PASS"

            # Match AT codes (e.g. test_at01 -> AT01) or RT codes (e.g. test_rt01 -> RT01)
            at_m = re.search(r"at([0-9]{2})", name, re.IGNORECASE)
            if at_m:
                code = f"AT{int(at_m.group(1)):02d}"
                results[code] = {
                    "status": outcome,
                    "test_name": name,
                    "duration_seconds": time_sec,
                }

            rt_m = re.search(r"rt([0-9]{2})", name, re.IGNORECASE)
            if rt_m:
                code = f"RT{int(rt_m.group(1)):02d}"
                results[code] = {
                    "status": outcome,
                    "test_name": name,
                    "duration_seconds": time_sec,
                }
    except Exception:
        pass
    return results


def generate_evidence_manifest(
    workspace_root: str | Path = ".",
    output_relative_path: str = "evidence/manifest.json",
    report_file: Optional[str | Path] = None,
    report_files: Optional[List[str | Path]] = None,
    custom_at_statuses: Optional[Dict[str, str]] = None,
    custom_rt_statuses: Optional[Dict[str, str]] = None,
    build_evidence: Optional[Dict[str, Any]] = None,
    runtime_evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate the definitive evidence manifest.
    
    Defaults all items to NOT_RUN. Only verified test report outcomes or explicit report dicts
    advance items to PASS.
    """
    root = Path(workspace_root)
    evidence_path = root / output_relative_path
    evidence_path.parent.mkdir(parents=True, exist_ok=True)

    critical_files = [
        "core/trading/execution_gateway.py",
        "core/trading/authorization.py",
        "core/trading/position_guardian.py",
        "core/trading/ledger.py",
        "core/trading/risk_engine.py",
        "core/trading/trade_plan_contract.py",
        "core/trading/trader_capabilities.py",
        "core/trading/session_manager.py",
        "core/trading/testnet_capabilities.py",
        "core/trading/ai_led_engine.py",
        "core/trading/ai_session_coordinator.py",
        "core/monitoring_runtime.py",
        "core/v2_store.py",
        "core/analysis/strategy_evaluator.py",
        "core/news_revision.py",
        "core/data_migration.py",
        "core/diagnostics.py",
        "core/sidecar_lifecycle.py",
        "core/trading/stress_runner.py",
        "apps/api/v2.py",
        "tests/test_spec_a01_a12.py",
        "tests/test_spec_at01_at07.py",
        "tests/test_spec_at08_at14.py",
        "tests/test_spec_at15_at23.py",
        "tests/test_spec_at31_at35.py",
        "tests/test_spec_at36_at40.py",
        "tests/test_spec_at24_at30.py",
        "tests/test_spec_rt01_rt16.py",
        "tests/test_spec_rt17_rt26.py",
        "tests/test_repair_v12_luna.py",
        "tests/test_trader_reliability_v13.py",
        "tests/test_trader_reliability_v14.py",
        "web/src/components/AITraderPanel.tsx",
        "web/src/components/AuthorizationWizardModal.tsx",
        "web/src/pages/V2WorkspacePage.tsx",
        "web/src/api/client.ts",
    ]

    hashes: Dict[str, str] = {}
    for rel in critical_files:
        fp = root / rel
        if fp.exists():
            hashes[rel] = compute_file_sha256(fp)

    # Base acceptance matrix AT01-AT42 and RT01-RT26
    # All items default strictly to NOT_RUN (RT24)
    at_matrix: Dict[str, Dict[str, Any]] = {}
    for i in range(1, 43):
        code = f"AT{i:02d}"
        at_matrix[code] = {
            "status": "WAITING_USER_AUTHORIZATION" if code in ("AT41", "AT42") else "NOT_RUN",
            "scope": f"Milestone verification for {code}",
        }

    rt_matrix: Dict[str, Dict[str, Any]] = {}
    for i in range(1, 27):
        code = f"RT{i:02d}"
        rt_matrix[code] = {
            "status": "WAITING_USER_AUTHORIZATION" if code in ("RT23", "RT25") else "NOT_RUN",
            "scope": f"Specialized real trading repair verification for {code}",
        }

    # Load one or more real JUnit reports if provided.  External TESTNET and
    # clean-install rows remain authorization-gated regardless of a local XML
    # file.  A mock RT17 trace is evidence of the mock harness only, never of
    # a live Qwen 9B provider.
    report_meta = None
    report_evidence: list[Dict[str, Any]] = []
    requested_reports: list[str | Path] = []
    if report_file is not None:
        requested_reports.append(report_file)
    requested_reports.extend(report_files or [])
    seen_report_paths: set[str] = set()
    for requested in requested_reports:
        rf_path = root / requested if not Path(requested).is_absolute() else Path(requested)
        path_key = str(rf_path.resolve())
        if path_key in seen_report_paths or not rf_path.is_file():
            continue
        seen_report_paths.add(path_key)
        report_hash = compute_file_sha256(rf_path)
        current_meta = {
            "report_file": str(rf_path.relative_to(root) if rf_path.is_relative_to(root) else rf_path),
            "report_sha256": report_hash,
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        report_evidence.append(current_meta)
        if report_meta is None:
            report_meta = current_meta
        parsed = parse_junit_report(rf_path)
        for code, pdata in parsed.items():
            evidence = {**current_meta, **pdata}
            if code in at_matrix and code not in ("AT41", "AT42"):
                at_matrix[code]["status"] = pdata["status"]
                at_matrix[code]["evidence"] = evidence
            elif code in rt_matrix and code not in ("RT23", "RT25"):
                status = pdata["status"]
                if code == "RT17" and status == "PASS":
                    status = "PASS_MOCK_ONLY"
                    evidence["verification_tier"] = "MOCK_PROVIDER_TRACE"
                    evidence["real_qwen_e2e"] = False
                rt_matrix[code]["status"] = status
                rt_matrix[code]["evidence"] = evidence

    source_state = collect_source_state(root, exclude_paths=[output_relative_path])

    # Custom programmatic status overrides (from test runs or runner)
    if custom_at_statuses:
        for code, status in custom_at_statuses.items():
            if code in at_matrix:
                at_matrix[code]["status"] = status
    if custom_rt_statuses:
        for code, status in custom_rt_statuses.items():
            if code in rt_matrix:
                rt_matrix[code]["status"] = status

    manifest = {
        "manifest_schema_version": "1.2",
        "release_code": SPEC_RELEASE_CODE,
        "spec_version": SPEC_VERSION,
        "spec_title": SPEC_TITLE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platform": {
            "os": platform.system(),
            "os_release": platform.release(),
            "os_version": platform.version(),
            "architecture": platform.machine(),
            "python": sys.version,
        },
        "supported_trading_modes": ["RESEARCH", "PAPER", "TESTNET", "LIVE_LOCKED"],
        "control_modes": ["ASSISTED", "AUTONOMOUS"],
        "decision_paths": ["STRATEGY_DRIVEN", "AI_LED"],
        "file_sha256": hashes,
        "source_state": source_state,
        "acceptance_matrix": at_matrix,
        "rt_matrix": rt_matrix,
        "report_metadata": report_meta,
        "report_evidence": report_evidence,
        "build_evidence": build_evidence or {
            "status": "NOT_RUN",
            "reason": "No build evidence supplied to manifest generator",
        },
        "runtime_evidence": runtime_evidence or {
            "status": "NOT_RUN",
            "reason": "No runtime evidence supplied to manifest generator",
        },
        "verification_commands": [
            "pytest tests/test_spec_a01_a12.py tests/test_spec_at01_at07.py tests/test_spec_at08_at14.py tests/test_spec_at15_at23.py tests/test_spec_at31_at35.py -v",
            "pytest tests/test_spec_at24_at30.py tests/test_spec_at36_at40.py -v",
            "pytest tests/test_spec_rt01_rt16.py tests/test_spec_rt17_rt26.py -v",
            "cd web && npm test -- --run",
        ],
    }

    with open(evidence_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    return manifest
