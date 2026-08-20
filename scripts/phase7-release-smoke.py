"""Run a bounded local release smoke measurement with explicit non-claims."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from core.backup import backup_database, read_database_counts, restore_database
from core.instruments import instrument_for
from core.scheduler import LocalResourceProbe
from core.storage import SQLiteStore


def run_smoke() -> dict[str, object]:
    started_at = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix="ai-market-analyst-p7-smoke-") as temp:
        root = Path(temp)
        source = root / "source.sqlite3"
        artifact = root / "backup-artifact"
        restored = root / "restored.sqlite3"
        store = SQLiteStore(source)
        store.initialize()
        store.save_instrument(instrument_for("AAPL"))

        backup_start = time.perf_counter()
        backup_manifest = backup_database(source, artifact)
        backup_elapsed_ms = round((time.perf_counter() - backup_start) * 1000, 3)

        restore_start = time.perf_counter()
        restore_result = restore_database(artifact, restored)
        restore_elapsed_ms = round((time.perf_counter() - restore_start) * 1000, 3)

        probe_start = time.perf_counter()
        resource = LocalResourceProbe(timeout_seconds=2.0).probe()
        probe_elapsed_ms = round((time.perf_counter() - probe_start) * 1000, 3)
        resource_payload = resource.to_dict()
        raw_details = resource_payload.get("details")
        if isinstance(raw_details, dict):
            processes = raw_details.get("processes")
            safe_processes: list[dict[str, object]] = []
            if isinstance(processes, list):
                for process in processes:
                    if not isinstance(process, dict):
                        continue
                    name = str(process.get("name", "unknown")).replace("\\", "/").rsplit("/", 1)[-1]
                    safe_processes.append({"name": name})
            resource_payload["details"] = {
                "process_count": len(safe_processes),
                "free_memory_mb": raw_details.get("free_memory_mb"),
                "minimum_free_memory_mb": raw_details.get("minimum_free_memory_mb"),
            }
        return {
            "report_version": "phase7_release_smoke_v1",
            "started_at": started_at,
            "hardware_context": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "cpu_count": os.cpu_count(),
                "gpu_measurement": "nvidia-smi read-only probe result only; no benchmark threshold",
            },
            "fixture_context": {
                "database": "temporary SQLite database",
                "seeded_instruments": ["AAPL"],
                "network": False,
                "model": False,
                "orders": False,
            },
            "backup": {"elapsed_ms": backup_elapsed_ms, "schema_version": backup_manifest["schema_version"]},
            "restore": {
                "elapsed_ms": restore_elapsed_ms,
                "restored": restore_result["restored"],
                "counts": read_database_counts(restored),
            },
            "resource_probe": {**resource_payload, "elapsed_ms": probe_elapsed_ms},
            "non_claims": [
                "These are bounded local smoke timings, not a production performance SLO.",
                "The resource result is capability evidence and does not measure model quality or concurrency.",
                "No live provider, Ollama, ComfyUI, distributed or real-order behavior is inferred.",
            ],
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local Phase 7 release smoke.")
    parser.add_argument("--output", default="docs/phase7-release-smoke.json")
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    report = run_smoke()
    output = (repo / args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
