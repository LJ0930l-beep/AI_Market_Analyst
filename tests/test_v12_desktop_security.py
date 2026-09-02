from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_tauri_build_command_is_repository_root_stable() -> None:
    config = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    build = config["build"]
    assert "scripts/build-tauri.ps1" in build["beforeBuildCommand"]
    assert "scripts/build-tauri.ps1" in build["beforeBuildCommand"]
    script = (ROOT / "scripts" / "build-tauri.ps1").read_text(encoding="utf-8")
    assert "$PSScriptRoot" in script
    assert "build-tauri-frontend.ps1" in script
    assert "build-sidecar.ps1" in script


def test_webview_cannot_spawn_or_rewrite_desktop_processes() -> None:
    capability = json.loads((ROOT / "src-tauri" / "capabilities" / "default.json").read_text(encoding="utf-8"))
    permissions = capability["permissions"]
    assert not any(
        permission == "shell:allow-spawn"
        or (isinstance(permission, dict) and permission.get("identifier") == "shell:allow-spawn")
        for permission in permissions
    )
    assert all(not (isinstance(permission, dict) and permission.get("allow") and any(item.get("args") is True for item in permission["allow"] if isinstance(item, dict))) for permission in permissions)
    assert "autostart:allow-enable" in permissions
    assert "autostart:allow-disable" in permissions
    assert "autostart:allow-is-enabled" in permissions

    rust = (ROOT / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
    assert '"--ownership-token"' in rust
    assert "select_sidecar_port" in rust
    assert "backend_status" in rust
    assert "foreign processes remain untouched" in rust
    assert "taskkill" in rust and '"/PID"' in rust


def test_tauri_csp_removes_inline_script_permission_and_uninstall_cleans_autostart() -> None:
    config = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    csp = config["app"]["security"]["csp"]
    assert "script-src 'unsafe-inline'" not in csp
    assert "connect-src" in csp and "127.0.0.1:*" in csp

    hook = (ROOT / "scripts" / "installer-hooks.nsh").read_text(encoding="utf-8")
    assert 'DeleteRegValue HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Run" "AI Market Analyst"' in hook
    assert 'DeleteRegValue HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\StartupApproved\\Run" "AI Market Analyst"' in hook


def test_desktop_lifecycle_keeps_monitoring_safe_when_hidden_or_degraded() -> None:
    rust = (ROOT / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
    assert '"terminal" => route_main_window(app, "/monitoring")' in rust
    assert '"settings" => route_main_window(app, "/settings")' in rust
    assert '"monitoring-toggle"' in rust and '"/monitoring/pause"' in rust and '"/monitoring/resume"' in rust
    assert '"restart-backend"' in rust and 'restart_backend(app.clone())' in rust
    assert 'if active == Some(true)' in rust and 'notify_monitoring_continues' in rust
    assert 'window.app_handle().exit(0)' in rust
    assert 'let mut misses = 0_u8' in rust and 'if misses >= 3' in rust

    runtime = (ROOT / "core" / "monitoring_runtime.py").read_text(encoding="utf-8")
    assert '"startup_no_scan"' in runtime or "resume_not_authorized" in runtime
    assert 'MONITORING_RUNTIME_CONTRACT_VERSION = "monitoring_runtime_v1"' in runtime
    assert 'queue.Queue(maxsize=128)' in runtime
