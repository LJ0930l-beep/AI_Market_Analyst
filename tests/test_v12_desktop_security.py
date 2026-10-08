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
    assert "Never search" in rust and "by port or" in rust
    assert "CommandChild.kill" in rust
    assert "taskkill" not in rust and '"/PID"' not in rust


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


def test_owned_sidecar_recovery_is_bounded_single_flight_and_generation_fenced() -> None:
    rust = (ROOT / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
    assert "const MAX_AUTOMATIC_RECOVERY_ATTEMPTS: u8 = 3;" in rust
    assert "fn automatic_recovery_delay(attempt: u8) -> Option<Duration>" in rust
    assert "automatic_recovery_in_progress.compare_exchange" in rust
    assert '"recovering"' in rust and "manual restart required" in rust
    assert "automatic_recovery_may_continue" in rust
    assert "shutdown_requested" in rust and "manual_restart_in_progress" in rust
    assert "fn registered_sidecar_generation_is_current(" in rust
    assert "slot.is_some()" in rust  # A new spawn must never overwrite a registered child.

    # Status mutation, generation validation and event publication share the
    # status lock, so an old health waiter cannot publish ready after stop.
    transition = rust.split("fn emit_backend_status_for_generation(", 1)[1].split("fn owns_sidecar_generation(", 1)[0]
    assert transition.index("state.status.lock()") < transition.index("state.generation.load")
    assert transition.index("state.generation.load") < transition.index("publish_backend_status_locked")
    assert "status_transition_is_authorized" in transition
    authorization = rust.split("fn status_transition_is_authorized(", 1)[1].split("fn configured_sidecar_port(", 1)[0]
    assert "shutdown_requested" in authorization and "manual_restart_in_progress" in authorization

    ready_check = rust.index('if !emit_backend_status_for_generation_owner(app, generation, "ready", port, Some(pid), None, start_kind.manual_owner())')
    assert rust.rfind("owns_sidecar_pid_generation(app, generation, pid", 0, ready_check) >= 0
    assert '"/PID"' not in rust and "taskkill" not in rust


def test_sidecar_watchdogs_cover_startup_window_and_require_owned_process_tree() -> None:
    rust = (ROOT / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
    process = (ROOT / "src-tauri" / "src" / "sidecar_process.rs").read_text(encoding="utf-8")
    start = rust.split("fn start_owned_sidecar(", 1)[1].split("fn automatic_recovery_is_current(", 1)[0]
    assert start.index("start_owned_sidecar_watchers(") < start.index("wait_for_sidecar(")
    watcher = rust.split("fn start_owned_sidecar_watchers(", 1)[1].split("fn start_owned_sidecar(", 1)[0]
    assert "CommandEvent::Terminated" in watcher
    assert "launcher_is_alive()" in watcher and "Duration::from_millis(250)" in watcher

    health = rust.split("fn sidecar_is_healthy(", 1)[1].split("fn http_json(", 1)[0]
    assert health.index("launcher_is_alive()") < health.index("sidecar_health_evidence") < health.index("verify_health_worker")
    assert "ownership_failure_blocked" in start
    assert "prior sidecar worker ownership is unresolved" in start

    assert "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in process
    assert "AssignProcessToJobObject" in process and "TerminateJobObject" in process
    assert "QueryInformationJobObject" in process and "active_process_count() == Some(0)" in process
    assert "job_object_termination_waits_for_a_real_windows_child" in process
    assert "OwnedProcessTree::new(pid, std::process::id())" in rust
    assert "parent_pid(pid) != Some(expected_parent)" in process
    assert "path.eq_ignore_ascii_case(expected)" in process
    assert "prepare_owned_tree_for_termination" in rust
    assert "automatic restart is blocked" in rust
    assert "taskkill" not in rust + process and '"/PID"' not in rust + process


def test_sidecar_restart_does_not_issue_monitoring_resume_or_start_ai_session() -> None:
    rust = (ROOT / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
    restart = rust.split("fn restart_backend(", 1)[1].split("fn backend_status(", 1)[0]
    assert 'SidecarStartKind::ManualRestart' in restart
    assert '"/monitoring/resume"' not in restart
    assert "ai-session" not in restart

    doc = (ROOT / "docs" / "desktop-sidecar-recovery.md").read_text(encoding="utf-8")
    assert "STOPPED" in doc
    assert "1, 2, then 4 seconds" in doc
    assert "foreign" in doc.lower()
