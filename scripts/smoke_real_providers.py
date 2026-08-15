#!/usr/bin/env python3
"""Attempt the six-symbol Phase 2 real-data smoke test and report honest failures."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.ai import OllamaProvider
from core.analysis_service import AnalysisError, AnalysisService
from core.instruments import instrument_for, phase1_universe
from core.news_engine import RSSNewsProvider


def _run_command(args: list[str], *, env: dict[str, str] | None = None, timeout: float = 5.0) -> dict[str, object]:
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env, check=False)
        return {
            "command": " ".join(args),
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
    except Exception as exc:  # pragma: no cover - host dependent
        return {"command": " ".join(args), "returncode": None, "stdout": "", "stderr": str(exc), "error": type(exc).__name__}


def _nvidia_smi() -> dict[str, object]:
    query = "name,memory.total,memory.used,utilization.gpu,driver_version"
    result = _run_command(["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"], timeout=5)
    rows: list[dict[str, object]] = []
    for line in str(result.get("stdout") or "").splitlines():
        fields = [item.strip() for item in line.split(",")]
        if len(fields) != 5:
            continue
        try:
            rows.append(
                {
                    "name": fields[0],
                    "memory_total_mib": int(float(fields[1])),
                    "memory_used_mib": int(float(fields[2])),
                    "gpu_utilization_pct": int(float(fields[3])),
                    "driver_version": fields[4],
                }
            )
        except ValueError:
            rows.append({"raw": fields})
    return {"command": result, "gpus": rows}


def _ollama_executable() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return str(candidate) if candidate.exists() else None


def _ollama_cli_env() -> dict[str, str]:
    env = os.environ.copy()
    host = env.get("OLLAMA_BASE_URL") or env.get("OLLAMA_HOST")
    if host:
        env["OLLAMA_HOST"] = host.removeprefix("http://").removeprefix("https://")
    return env


def _ollama_ps() -> dict[str, object]:
    executable = _ollama_executable()
    if not executable:
        return {"available": False, "error": "ollama executable not found"}
    return {"available": True, "result": _run_command([executable, "ps"], env=_ollama_cli_env(), timeout=5)}


def _json_request(base_url: str, path: str, payload: dict[str, object] | None = None, timeout: float = 5.0) -> dict[str, object]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        method="POST" if payload is not None else "GET",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        parsed = json.loads(response.read().decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"Ollama {path} response must be an object")
    return parsed


def _ollama_inventory(base_url: str, model_name: str) -> dict[str, object]:
    try:
        tags = _json_request(base_url, "/api/tags", timeout=10)
        models = tags.get("models", [])
        selected = next((item for item in models if isinstance(item, dict) and item.get("name") == model_name), None)
        show = _json_request(base_url, "/api/show", {"name": model_name}, timeout=10) if selected else None
        details = (selected or {}).get("details", {}) if isinstance(selected, dict) else {}
        if not isinstance(details, dict):
            details = {}
        model_info = (show or {}).get("details", {}) if isinstance(show, dict) else {}
        if not isinstance(model_info, dict):
            model_info = {}
        return {
            "available": True,
            "model": model_name,
            "registered": selected is not None,
            "tag": selected,
            "show": show,
            "size_bytes": selected.get("size") if isinstance(selected, dict) else None,
            "size_gib": round(float(selected["size"]) / (1024**3), 3) if isinstance(selected, dict) and selected.get("size") else None,
            "quantization_level": details.get("quantization_level") or model_info.get("quantization_level"),
            "parameter_size": details.get("parameter_size") or model_info.get("parameter_size"),
            "format": details.get("format") or model_info.get("format"),
            "family": details.get("family") or model_info.get("family"),
        }
    except Exception as exc:  # pragma: no cover - network dependent
        return {"available": False, "model": model_name, "registered": False, "error": str(exc), "error_type": type(exc).__name__}


def _memory_bytes() -> int | None:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_uint32), ("memory_load", ctypes.c_uint32), ("total", ctypes.c_uint64), ("available", ctypes.c_uint64), ("total_page", ctypes.c_uint64), ("available_page", ctypes.c_uint64), ("total_virtual", ctypes.c_uint64), ("available_virtual", ctypes.c_uint64), ("available_extended", ctypes.c_uint64)]

    try:
        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.total)
    except Exception:  # pragma: no cover - non-Windows fallback
        return None
    return None


def hardware_snapshot(base_url: str) -> dict[str, object]:
    cpu = platform.processor() or platform.machine()
    powershell = shutil.which("powershell.exe")
    if powershell:
        cpu_result = _run_command([powershell, "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name)"], timeout=5)
        cpu = str(cpu_result.get("stdout") or cpu)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "os": platform.platform(),
        "python": platform.python_version(),
        "cpu": cpu,
        "logical_processors": os.cpu_count(),
        "ram_total_bytes": _memory_bytes(),
        "nvidia_smi": _nvidia_smi(),
        "ollama_ps": _ollama_ps(),
        "ollama_base_url": base_url,
    }


class RuntimeSampler:
    def __init__(self, base_url: str, interval_seconds: float = 2.0) -> None:
        self.base_url = base_url
        self.interval_seconds = interval_seconds
        self.samples: list[dict[str, object]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="phase2-runtime-sampler", daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.samples.append(hardware_snapshot(self.base_url))
            self._stop.wait(self.interval_seconds)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> list[dict[str, object]]:
        self._stop.set()
        self._thread.join(timeout=10)
        return list(self.samples)


def _strict_record_checks(record: dict[str, object]) -> list[str]:
    errors: list[str] = []
    provider = record.get("provider") if isinstance(record.get("provider"), dict) else {}
    if provider.get("provider") in {"fixture", "fixture_provider"}:
        errors.append("fixture market provider used")
    if not record.get("news_available"):
        errors.append("real news unavailable")
    model = record.get("model") if isinstance(record.get("model"), dict) else {}
    if not model.get("available"):
        errors.append(f"model unavailable: {model.get('error_code', 'unknown')}")
    signal = record.get("signal") if isinstance(record.get("signal"), dict) else {}
    if signal.get("action") not in {"LONG", "SHORT", "WAIT"}:
        errors.append("invalid signal action")
    if signal.get("input_hash") != record.get("input_hash"):
        errors.append("signal input_hash does not match context")
    if not signal.get("raw_model_response"):
        errors.append("missing raw model response")
    if signal.get("parse_status") not in {"valid", "repair_valid"}:
        errors.append("model parse status is not valid")
    if not isinstance(signal.get("latency_ms"), (int, float)) or signal.get("latency_ms", 0) <= 0:
        errors.append("missing model latency")
    if signal.get("action") == "WAIT" and any(signal.get(field) is not None for field in ("entry_low", "entry_high", "stop", "tp1", "tp2")):
        errors.append("WAIT contains actionable levels")
    if signal.get("action") in {"LONG", "SHORT"} and any(signal.get(field) is None for field in ("entry_low", "entry_high", "stop", "tp1", "tp2")):
        errors.append("actionable signal is missing levels")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="data/phase2-real-smoke.json")
    parser.add_argument("--strict", action="store_true", help="return non-zero if any symbol cannot complete")
    parser.add_argument("--require-model", action="store_true", help="also fail if Ollama/Qwen is unavailable")
    parser.add_argument("--require-schema-coverage", action="store_true", help="require observed LONG, SHORT, and WAIT outputs")
    args = parser.parse_args(argv)
    os.environ["MARKET_DATA_MODE"] = "real"
    os.environ.setdefault("NEWS_MODE", "real")
    os.environ["DISABLE_FIXTURE_FALLBACK"] = "1"
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    model_name = os.environ.get("OLLAMA_MODEL", "qwen3.5:4b")
    service = AnalysisService(news_provider=RSSNewsProvider(timeout=15, retries=1), llm_provider=OllamaProvider(base_url=base_url, model_name=model_name, timeout=180, retries=0))
    records: list[dict[str, object]] = []
    failures = 0
    health = service.llm_provider.health()  # type: ignore[union-attr]
    inventory = _ollama_inventory(base_url, model_name)
    sampler = RuntimeSampler(base_url)
    sampler.start()
    for instrument in phase1_universe():
        started = time.perf_counter()
        before = hardware_snapshot(base_url)
        try:
            result = service.analyze(instrument, timeframe="1h", limit=120)
            if args.require_model and not result.model_status.get("available"):
                failures += 1
            record = {
                "symbol": instrument.symbol,
                "provider": result.bundle.snapshot.to_dict(),
                "quote": result.bundle.quote.__dict__ if hasattr(result.bundle.quote, "__dict__") else {"price": result.bundle.quote.price, "timestamp": result.bundle.quote.timestamp.isoformat(), "change_pct": result.bundle.quote.change_pct, "high": result.bundle.quote.high, "low": result.bundle.quote.low},
                "data_as_of": result.data_as_of.isoformat(),
                "input_hash": result.context.input_hash(),
                "quant": result.context.quant.to_dict(),
                "news": result.news.to_dict(),
                "news_count": len(result.news.events),
                "news_available": result.news.available,
                "model": result.model_status,
                "signal": result.signal.to_dict(),
                "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "hardware_before": before,
                "hardware_after": hardware_snapshot(base_url),
            }
            record["strict_check_errors"] = _strict_record_checks(record)
            if args.strict and record["strict_check_errors"]:
                failures += len(record["strict_check_errors"])
            records.append(record)
        except (ValueError, AnalysisError) as exc:
            failures += 1
            records.append({"symbol": instrument.symbol, "error_code": getattr(exc, "code", "analysis_error"), "error": str(exc), "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3), "hardware_before": before, "hardware_after": hardware_snapshot(base_url)})
    samples = sampler.stop()
    actions = sorted({str(record.get("signal", {}).get("action")) for record in records if isinstance(record.get("signal"), dict) and record.get("signal", {}).get("action")})
    missing_actions = sorted({"LONG", "SHORT", "WAIT"} - set(actions))
    schema_coverage = {"observed_actions": actions, "missing_actions": missing_actions, "pass": not missing_actions}
    if args.require_schema_coverage and missing_actions:
        failures += len(missing_actions)
    report = {
        "phase": 2,
        "mode": "real",
        "ollama": {"health": health, "inventory": inventory, "ps_after": _ollama_ps()},
        "hardware": hardware_snapshot(base_url),
        "runtime_samples": samples,
        "schema_coverage": schema_coverage,
        "records": records,
        "failures": failures,
        "strict": bool(args.strict),
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 2 if (args.strict or args.require_model) and failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
