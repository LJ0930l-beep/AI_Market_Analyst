"""Run a disposable, real-provider V1.2.1 smoke against the packaged sidecar.

This probe deliberately records capability failures instead of replacing live
responses with fixtures.  It owns only the child process it starts and never
stops Ollama or another unrelated process.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINARY = REPO_ROOT / "src-tauri" / "binaries" / "ai-market-analyst-backend-x86_64-pc-windows-msvc.exe"
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "v1.2.1-live-smoke.json"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request(base_url: str, path: str, *, method: str = "GET", payload: object | None = None, timeout: float = 15.0) -> dict[str, object]:
    started = perf_counter()
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        f"{base_url}{path}",
        data=body,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            decoded = json.loads(raw)
            return {
                "ok": True,
                "status_code": int(response.status),
                "elapsed_ms": round((perf_counter() - started) * 1000, 3),
                "payload": decoded,
            }
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        return {
            "ok": False,
            "status_code": int(exc.code),
            "elapsed_ms": round((perf_counter() - started) * 1000, 3),
            "error": detail or str(exc),
        }
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "status_code": None,
            "elapsed_ms": round((perf_counter() - started) * 1000, 3),
            "error": str(exc),
        }


def _payload(result: dict[str, object]) -> dict[str, object]:
    value = result.get("payload")
    return value if isinstance(value, dict) else {}


def _wait_for_sidecar(base_url: str, *, timeout_seconds: float = 30.0) -> dict[str, object]:
    deadline = perf_counter() + timeout_seconds
    last: dict[str, object] = {"ok": False, "error": "not attempted"}
    while perf_counter() < deadline:
        last = _request(base_url, "/health", timeout=2.0)
        if bool(last.get("ok")):
            return last
        time.sleep(0.25)
    return last


def _endpoint_summary(result: dict[str, object], *, fields: tuple[str, ...] = ()) -> dict[str, object]:
    summary: dict[str, object] = {
        "ok": bool(result.get("ok")),
        "status_code": result.get("status_code"),
        "elapsed_ms": result.get("elapsed_ms"),
    }
    if not result.get("ok"):
        summary["error"] = result.get("error")
        return summary
    payload = _payload(result)
    for field in fields:
        if field in payload:
            summary[field] = payload[field]
    return summary


def _model_smoke() -> dict[str, object]:
    requested_model = "qwen3.5:9b"
    result: dict[str, object] = {"requested_model": requested_model, "checked_at": _utc_now()}
    try:
        from core.ai.ollama import OllamaProvider

        provider = OllamaProvider(model_name=requested_model, timeout=60.0, retries=0, max_tokens=160)
        health = provider.health()
        result["health"] = {
            key: value
            for key, value in health.items()
            if key in {"provider", "available", "model_id", "model_available", "models", "context_length", "quantization", "think", "error_code"}
        }
        if health.get("available") is not True or health.get("model_available") is not True:
            result["status"] = "unavailable"
            return result
        messages = [
            {"role": "system", "content": "Return one small JSON object only. This is a local model connectivity smoke probe, not a trading request."},
            {
                "role": "user",
                "content": json.dumps(
                    {"task": "connectivity_smoke", "required_keys": ["status", "model"], "status": "ok", "model": requested_model},
                    sort_keys=True,
                ),
            },
        ]
        parsed, _raw, metadata = provider.generate_json(
            messages,
            model_name=requested_model,
            prompt_version="v12_live_smoke_v1",
            input_hash="v12-live-smoke-qwen3.5-9b",
        )
        result.update(
            {
                "status": "passed",
                "structured_json": isinstance(parsed, dict),
                "returned_keys": sorted(str(key) for key in parsed),
                "metadata": {key: metadata.get(key) for key in ("model_id", "model_version", "prompt_version", "latency_ms", "parse_status", "output_chars")},
            }
        )
    except Exception as exc:  # pragma: no cover - depends on the local Ollama runtime
        result["status"] = "failed"
        result["error"] = type(exc).__name__
        result["error_code"] = str(getattr(exc, "code", "MODEL_SMOKE_FAILED"))
    return result


def _websocket_smoke(base_url: str) -> dict[str, object]:
    result: dict[str, object] = {"url": f"{base_url.replace('http://', 'ws://', 1)}/market/realtime/BTCUSDT/stream?timeframe=15m"}
    try:
        import websocket  # type: ignore[import-not-found]

        socket = websocket.create_connection(
            result["url"],
            timeout=8.0,
            http_proxy_host=None,
            http_no_proxy=["127.0.0.1"],
        )
        message_types: list[str] = []
        sample: dict[str, object] = {}
        deadline = perf_counter() + 20.0
        try:
            while perf_counter() < deadline:
                socket.settimeout(max(0.5, min(5.0, deadline - perf_counter())))
                raw = socket.recv()
                if not raw:
                    break
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    continue
                message_type = str(payload.get("type") or "unknown")
                if message_type not in message_types:
                    message_types.append(message_type)
                if message_type == "bar" and isinstance(payload.get("bar"), dict):
                    bar = payload["bar"]
                    sample = {key: bar.get(key) for key in ("timestamp", "open", "high", "low", "close", "volume", "is_closed") if key in bar}
                    break
        finally:
            socket.close()
        result.update(
            {
                "status": "passed" if sample else "partial" if message_types else "failed",
                "message_types": message_types,
                "bar_received": bool(sample),
                "sample_bar": sample,
            }
        )
    except Exception as exc:  # pragma: no cover - depends on public WebSocket/network availability
        result.update({"status": "failed", "error": type(exc).__name__, "error_code": str(getattr(exc, "code", "WEBSOCKET_SMOKE_FAILED"))})
    return result


def _hydration_smoke(base_url: str) -> dict[str, object]:
    """Wait for the sidecar-owned first-start public cache refresh."""

    last: dict[str, object] = {"ok": False, "error": "not attempted"}
    deadline = perf_counter() + 75.0
    while perf_counter() < deadline:
        last = _request(base_url, "/hydration/status", timeout=15.0)
        payload = _payload(last)
        if isinstance(payload.get("last_durable_run"), dict):
            break
        time.sleep(1.0)
    summary = _endpoint_summary(
        last,
        fields=(
            "contract_version", "enabled", "state", "worker_alive", "market_symbols",
            "news_symbols", "last_refresh_at", "last_success_at", "last_error",
            "cache", "provider_calls", "domain_writes", "last_durable_run",
        ),
    )
    payload = _payload(last)
    summary["status"] = "passed" if last.get("ok") and payload.get("enabled") is True and isinstance(payload.get("last_durable_run"), dict) else "failed"
    return summary


def _monitoring_smoke(base_url: str) -> dict[str, object]:
    """Exercise the sidecar-owned lifecycle against real bars and the real Smart tier.

    The first cycle is deliberately produced by ``monitoring/start`` rather
    than the legacy one-shot endpoint.  The one-shot endpoint is used only
    after the worker has been paused, to prove that the same closed bar is
    still idempotent and was not double-processed.
    """

    result: dict[str, object] = {
        "status": "failed",
        "policy_updates": {},
        "runtime": {},
        "run": {},
        "smart_analysis_saved": False,
    }
    policy = {
        "contract_version": "monitoring_policy_v1",
        "enabled": True,
        "primary_timeframe": "15m",
        "context_timeframe": "1h",
        "trigger_types": ["REGIME_CHANGE", "BREAKOUT", "BREAKDOWN", "VOLUME_EXPANSION", "VOLATILITY_EXPANSION", "LEVEL_PROXIMITY", "SIGNAL_INVALIDATION"],
        "min_trigger_score": 0.0,
        "ai_min_confidence": 0.0,
        "cooldown_minutes": 60,
        "quiet_hours": {"enabled": False},
        "notify": {"desktop": False, "sound": False},
    }
    for symbol in SYMBOLS:
        updated = _request(base_url, "/monitoring", method="PUT", payload={**policy, "instrument_id": symbol}, timeout=15.0)
        result["policy_updates"][symbol] = _endpoint_summary(updated, fields=("contract_version", "instrument_id", "enabled", "primary_timeframe"))

    runtime: dict[str, object] = {}
    initial = _request(base_url, "/monitoring/status", timeout=15.0)
    runtime["initial"] = _endpoint_summary(initial, fields=("contract_version", "state", "active", "worker_alive", "run_count", "resume_eligible", "active_symbols"))
    initial_payload = _payload(initial)
    startup_no_scan = bool(initial.get("ok")) and initial_payload.get("state") == "stopped" and int(initial_payload.get("run_count", 0) or 0) == 0
    runtime["startup_no_scan"] = startup_no_scan
    if not startup_no_scan:
        result["runtime"] = runtime
        return result

    started = _request(base_url, "/monitoring/start", method="POST", payload={}, timeout=15.0)
    runtime["start"] = _endpoint_summary(started, fields=("contract_version", "state", "active", "worker_alive", "run_count", "active_symbols", "stream"))
    if not started.get("ok"):
        result["runtime"] = runtime
        return result

    first_cycle: dict[str, object] = {}
    deadline = perf_counter() + 300.0
    while perf_counter() < deadline:
        first_cycle = _request(base_url, "/monitoring/status", timeout=15.0)
        first_payload = _payload(first_cycle)
        if bool(first_cycle.get("ok")) and bool(first_payload.get("active")) and int(first_payload.get("run_count", 0) or 0) >= 1 and first_payload.get("last_cycle_status"):
            break
        time.sleep(2.0)
    runtime["first_cycle"] = _endpoint_summary(first_cycle, fields=("contract_version", "state", "active", "worker_alive", "run_count", "last_cycle_at", "last_cycle_status", "consecutive_failures", "stream", "resource"))
    first_payload = _payload(first_cycle)
    first_cycle_passed = bool(first_cycle.get("ok")) and bool(first_payload.get("active")) and first_payload.get("last_cycle_status") == "COMPLETED" and int(first_payload.get("run_count", 0) or 0) >= 1
    runtime["first_cycle_passed"] = first_cycle_passed
    if not first_cycle_passed:
        _request(base_url, "/monitoring/stop", method="POST", payload={}, timeout=15.0)
        result["runtime"] = runtime
        return result

    paused = _request(base_url, "/monitoring/pause", method="POST", payload={}, timeout=30.0)
    paused_payload = _payload(paused)
    runtime["pause"] = _endpoint_summary(paused, fields=("contract_version", "state", "active", "worker_alive", "run_count", "active_symbols", "stream"))
    paused_count = int(paused_payload.get("run_count", 0) or 0)
    time.sleep(2.0)
    paused_check = _request(base_url, "/monitoring/status", timeout=15.0)
    paused_check_payload = _payload(paused_check)
    runtime["pause_check"] = _endpoint_summary(paused_check, fields=("contract_version", "state", "active", "worker_alive", "run_count", "active_symbols"))
    runtime["pause_stable"] = bool(paused.get("ok")) and paused_payload.get("state") == "paused" and not bool(paused_payload.get("active")) and bool(paused_check.get("ok")) and paused_check_payload.get("state") == "paused" and int(paused_check_payload.get("run_count", 0) or 0) == paused_count

    resumed = _request(base_url, "/monitoring/resume", method="POST", payload={}, timeout=15.0)
    runtime["resume"] = _endpoint_summary(resumed, fields=("contract_version", "state", "active", "worker_alive", "run_count", "active_symbols", "stream"))
    resumed_cycle: dict[str, object] = {}
    resume_deadline = perf_counter() + 180.0
    while perf_counter() < resume_deadline:
        resumed_cycle = _request(base_url, "/monitoring/status", timeout=15.0)
        resumed_payload = _payload(resumed_cycle)
        if bool(resumed_cycle.get("ok")) and bool(resumed_payload.get("active")) and int(resumed_payload.get("run_count", 0) or 0) > paused_count and resumed_payload.get("last_cycle_status"):
            break
        time.sleep(2.0)
    runtime["resume_cycle"] = _endpoint_summary(resumed_cycle, fields=("contract_version", "state", "active", "worker_alive", "run_count", "last_cycle_at", "last_cycle_status", "consecutive_failures", "stream"))
    resumed_payload = _payload(resumed_cycle)
    runtime["resume_cycle_passed"] = bool(resumed_cycle.get("ok")) and bool(resumed_payload.get("active")) and int(resumed_payload.get("run_count", 0) or 0) > paused_count and resumed_payload.get("last_cycle_status") == "COMPLETED"

    paused_after_resume = _request(base_url, "/monitoring/pause", method="POST", payload={}, timeout=30.0)
    runtime["pause_after_resume"] = _endpoint_summary(paused_after_resume, fields=("contract_version", "state", "active", "worker_alive", "run_count", "active_symbols"))
    stopped = _request(base_url, "/monitoring/stop", method="POST", payload={}, timeout=30.0)
    runtime["stop"] = _endpoint_summary(stopped, fields=("contract_version", "state", "active", "worker_alive", "run_count", "resume_eligible", "active_symbols"))
    stopped_payload = _payload(stopped)
    runtime["lifecycle_passed"] = bool(runtime.get("startup_no_scan")) and first_cycle_passed and bool(runtime.get("pause_stable")) and bool(runtime.get("resume_cycle_passed")) and bool(paused_after_resume.get("ok")) and _payload(paused_after_resume).get("state") == "paused" and bool(stopped.get("ok")) and stopped_payload.get("state") == "stopped" and not bool(stopped_payload.get("active"))
    result["runtime"] = runtime

    # Exercise the old explicit one-cycle endpoint only while the resident
    # worker is stopped.  It should observe no new closed bar after the
    # background cycle has already claimed the current bar.
    run = _request(base_url, "/monitoring/run", method="POST", payload={"symbols": list(SYMBOLS)}, timeout=240.0)
    result["run"] = _endpoint_summary(run, fields=("contract_version", "status", "as_of", "resource", "capabilities"))
    if not run.get("ok"):
        return result
    run_payload = _payload(run)
    items = run_payload.get("items") if isinstance(run_payload.get("items"), list) else []
    result["item_statuses"] = {str(item.get("symbol")): item.get("status") for item in items if isinstance(item, dict)}
    result["events"] = {
        str(item.get("symbol")): [
            {
                "status": event.get("status"),
                "trigger_type": event.get("trigger_type"),
                "analysis_status": event.get("analysis_status"),
                "prediction_id": event.get("prediction_id"),
                "smart_fallback": event.get("smart_fallback"),
                "error": event.get("error"),
            }
            for event in (item.get("events") if isinstance(item.get("events"), list) else [])
            if isinstance(event, dict)
        ]
        for item in items
        if isinstance(item, dict)
    }
    trigger_events = _request(base_url, "/triggers?limit=50", timeout=15.0)
    result["triggers"] = _endpoint_summary(trigger_events, fields=("policy_version", "dedupe", "cooldown_owner"))
    trigger_payload = _payload(trigger_events)
    trigger_rows = trigger_payload.get("events") if isinstance(trigger_payload.get("events"), list) else []
    result["trigger_event_count"] = len(trigger_rows)
    analyses = _request(base_url, "/monitoring/opportunities?limit=50", timeout=15.0)
    result["opportunities"] = _endpoint_summary(analyses, fields=("contract_version", "validator", "model_tier"))
    analyses_payload = _payload(analyses)
    analysis_rows = analyses_payload.get("analyses") if isinstance(analyses_payload.get("analyses"), list) else []
    result["opportunity_count"] = len(analysis_rows)
    result["analysis_rows"] = [
        {
            "symbol": row.get("symbol") or row.get("instrument_id"),
            "model_id": row.get("model_id"),
            "validator_status": row.get("validator_status"),
            "trigger_event_id": row.get("trigger_event_id"),
            "bias": row.get("bias"),
            "confidence": row.get("confidence"),
            "raw_model_response": str(row.get("raw_model_response") or "")[:1200],
        }
        for row in analysis_rows[:20]
        if isinstance(row, dict)
    ]
    result["smart_analysis_saved"] = any(
        isinstance(row, dict) and str(row.get("model_id", "")).endswith(":9b") and row.get("validator_status") == "VALID"
        for row in analysis_rows
    )
    repeat = _request(base_url, "/monitoring/run", method="POST", payload={"symbols": list(SYMBOLS)}, timeout=240.0)
    result["repeat_run"] = _endpoint_summary(repeat, fields=("contract_version", "status", "as_of", "resource"))
    repeat_payload = _payload(repeat)
    repeat_items = repeat_payload.get("items") if isinstance(repeat_payload.get("items"), list) else []
    result["repeat_item_statuses"] = {str(item.get("symbol")): item.get("status") for item in repeat_items if isinstance(item, dict)}
    result["bar_close_exactly_once"] = bool(repeat.get("ok")) and bool(repeat_items) and all(
        isinstance(item, dict) and item.get("status") == "NO_NEW_CLOSED_BAR" for item in repeat_items
    )
    result["status"] = "passed" if bool(runtime.get("lifecycle_passed")) and result["smart_analysis_saved"] and result["bar_close_exactly_once"] else "failed_required_runtime_smart_or_exactly_once"
    return result


def run(binary: Path, output: Path, *, port: int) -> int:
    started_at = _utc_now()
    temp_root = Path(tempfile.mkdtemp(prefix="aima-v12-live-smoke-"))
    app_data = temp_root / "appdata"
    stdout_path = temp_root / "sidecar.stdout.log"
    stderr_path = temp_root / "sidecar.stderr.log"
    base_url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment.pop("DATABASE_PATH", None)
    environment.update(
        {
            "AIMA_DATA_ROOT": str(app_data),
            "ALLOW_FIXTURE_FALLBACK": "0",
            "MARKET_DATA_MODE": "real",
            "NEWS_MODE": "real",
            "LLM_MODE": "ollama",
            "FAST_MODEL": "qwen3.5:4b",
            "SMART_MODEL": "qwen3.5:9b",
            "OLLAMA_MODEL": "qwen3.5:4b",
        }
    )
    process: subprocess.Popen[bytes] | None = None
    evidence: dict[str, object] = {
        "contract": "v121_live_smoke_v1",
        "started_at": started_at,
        "finished_at": None,
        "fixture_fallback": False,
        "binary": str(binary),
        "owned_child_only": True,
        "app_data_is_disposable": True,
        "symbols": list(SYMBOLS),
        "sidecar": {},
        "realtime": {},
        "charts": {},
        "monitoring": {},
        "hydration": {},
        "news": {},
        "model_9b": _model_smoke(),
    }
    try:
        if not binary.is_file():
            evidence["sidecar"] = {"status": "binary_missing", "error": str(binary)}
            return_code = 2
            return return_code
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                [str(binary), "--host", "127.0.0.1", "--port", str(port)],
                cwd=str(REPO_ROOT),
                env=environment,
                stdout=stdout,
                stderr=stderr,
            )
        health = _wait_for_sidecar(base_url)
        evidence["sidecar"] = _endpoint_summary(health, fields=("status", "ready", "phase", "api_version", "contract_version", "instance_id", "pid", "launcher_pid", "port", "ownership_verified"))
        if not health.get("ok"):
            evidence["sidecar"]["stderr_tail"] = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            return_code = 1
            return return_code
        release = _request(base_url, "/health/release")
        evidence["release"] = _endpoint_summary(release, fields=("version", "api_version", "phase", "database", "capabilities"))
        providers = _request(base_url, "/health/providers")
        evidence["provider_health"] = _endpoint_summary(providers, fields=("providers", "capabilities"))
        model_health = _request(base_url, "/health/model")
        evidence["model_health"] = _endpoint_summary(model_health, fields=("provider", "model", "models", "capabilities"))
        evidence["websocket"] = _websocket_smoke(base_url)

        for symbol in SYMBOLS:
            realtime = _request(base_url, f"/market/realtime/{symbol}", timeout=30.0)
            payload = _payload(realtime)
            evidence["realtime"][symbol] = _endpoint_summary(realtime, fields=("contract_version", "symbol", "data_as_of", "fetched_at", "provider", "freshness", "capabilities"))
            if realtime.get("ok"):
                evidence["realtime"][symbol]["bar_count"] = len(payload.get("bars", [])) if isinstance(payload.get("bars"), list) else 0
                evidence["realtime"][symbol]["quote_present"] = isinstance(payload.get("quote"), dict)
                provider = payload.get("provider") if isinstance(payload.get("provider"), dict) else {}
                evidence["realtime"][symbol]["provider_name"] = provider.get("provider")
                evidence["realtime"][symbol]["stale"] = provider.get("stale")
            for timeframe in ("15m", "1h"):
                chart = _request(base_url, f"/chart/{symbol}/bars?timeframe={timeframe}&limit=120", timeout=30.0)
                chart_payload = _payload(chart)
                key = f"{symbol}/{timeframe}"
                evidence["charts"][key] = _endpoint_summary(chart, fields=("contract_version", "symbol", "timeframe", "data_as_of", "provider", "tradingview"))
                if chart.get("ok"):
                    evidence["charts"][key]["bar_count"] = len(chart_payload.get("bars", [])) if isinstance(chart_payload.get("bars"), list) else 0
                    provider = chart_payload.get("provider") if isinstance(chart_payload.get("provider"), dict) else {}
                    evidence["charts"][key]["provider_name"] = provider.get("provider")
                    evidence["charts"][key]["stale"] = provider.get("stale")

        evidence["hydration"] = _hydration_smoke(base_url)

        evidence["monitoring"] = _monitoring_smoke(base_url)

        news = _request(base_url, "/news/BTCUSDT?locale=en", timeout=30.0)
        news_payload = _payload(news)
        evidence["news"] = _endpoint_summary(news, fields=("contract_version", "symbol", "locale", "provider", "available", "error_code", "capabilities"))
        events = news_payload.get("events") if isinstance(news_payload.get("events"), list) else []
        evidence["news"]["event_count"] = len(events)
        if events and isinstance(events[0], dict):
            news_id = str(events[0].get("id") or events[0].get("event_id") or "")
            evidence["news"]["sample_event"] = {key: events[0].get(key) for key in ("id", "event_id", "source", "title", "published_at") if key in events[0]}
            if news_id:
                translation = _request(base_url, f"/news/translate/{news_id}", method="POST", payload={}, timeout=90.0)
                translation_payload = _payload(translation)
                evidence["news"]["translation"] = _endpoint_summary(translation, fields=("contract_version", "news_id", "locale", "source_language", "model_id", "prompt_version", "numeric_guard_passed", "status", "evidence"))
                if translation.get("ok"):
                    evidence["news"]["translation"]["translated_title_present"] = bool(translation_payload.get("translated_title_zh"))
        required_failures: list[str] = []
        sidecar_evidence = evidence.get("sidecar", {})
        if not bool(sidecar_evidence.get("ok")) or sidecar_evidence.get("api_version") != "1.2.1" or sidecar_evidence.get("contract_version") != "desktop_backend_v1" or int(sidecar_evidence.get("pid", 0) or 0) <= 0 or int(sidecar_evidence.get("launcher_pid", 0) or 0) <= 0:
            required_failures.append("sidecar_ready")
        if not bool(evidence.get("release", {}).get("ok")) or evidence.get("release", {}).get("api_version") != "1.2.1" or evidence.get("release", {}).get("database", {}).get("schema_version") != 13:
            required_failures.append("release_schema_13")
        if evidence.get("hydration", {}).get("status") != "passed":
            required_failures.append("sidecar_public_hydration")
        if evidence.get("model_9b", {}).get("status") != "passed":
            required_failures.append("real_9b")
        if evidence.get("websocket", {}).get("status") != "passed":
            required_failures.append("public_websocket")
        for symbol in SYMBOLS:
            realtime_evidence = evidence.get("realtime", {}).get(symbol, {})
            if not realtime_evidence.get("ok") or realtime_evidence.get("provider_name") != "binance_public" or realtime_evidence.get("stale") is True:
                required_failures.append(f"live_realtime:{symbol}")
            for timeframe in ("15m", "1h"):
                chart_evidence = evidence.get("charts", {}).get(f"{symbol}/{timeframe}", {})
                if not chart_evidence.get("ok") or int(chart_evidence.get("bar_count", 0) or 0) < 60 or chart_evidence.get("provider_name") != "binance_public" or chart_evidence.get("stale") is True:
                    required_failures.append(f"live_chart:{symbol}/{timeframe}")
        monitoring_evidence = evidence.get("monitoring", {})
        if monitoring_evidence.get("status") != "passed":
            required_failures.append("smart_monitoring_and_exactly_once")
        news_evidence = evidence.get("news", {})
        translation_evidence = news_evidence.get("translation", {}) if isinstance(news_evidence, dict) else {}
        if not news_evidence.get("ok") or news_evidence.get("available") is not True or int(news_evidence.get("event_count", 0) or 0) < 1:
            required_failures.append("english_news_event")
        if not translation_evidence.get("ok") or translation_evidence.get("status") != "translated" or translation_evidence.get("numeric_guard_passed") is not True:
            required_failures.append("4b_translation_numeric_guard")
        evidence["required_failures"] = required_failures
        evidence["status"] = "passed" if not required_failures else "failed"
        return_code = 0 if not required_failures else 1
    finally:
        if process is not None:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if sys.platform == "win32":
                    # PyInstaller one-file uses a bootstrap parent and a
                    # same-owned worker.  taskkill is scoped to this exact
                    # child PID and its descendants; it does not search by
                    # process name or terminate Ollama/other applications.
                    subprocess.run(
                        ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            evidence["sidecar"]["owned_pid"] = process.pid
            evidence["sidecar"]["exit_code"] = process.returncode
        evidence["finished_at"] = _utc_now()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        shutil.rmtree(temp_root, ignore_errors=True)
    return return_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a real-provider V1.2.1 packaged sidecar smoke")
    parser.add_argument("--binary", type=Path, default=DEFAULT_BINARY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--port", type=int, default=18_766)
    args = parser.parse_args()
    return run(args.binary.resolve(), args.output.resolve(), port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
