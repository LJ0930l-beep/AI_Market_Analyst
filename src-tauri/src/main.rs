#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;
use std::thread;
use std::time::Duration;

use serde::Serialize;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, WindowEvent};
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;
use uuid::Uuid;

mod sidecar_process;
use sidecar_process::OwnedProcessTree;

const DEFAULT_SIDECAR_PORT: u16 = 18_765;
const MAX_PORT_PROBES: u16 = 64;
const MAX_AUTOMATIC_RECOVERY_ATTEMPTS: u8 = 3;
const AUTOMATIC_RECOVERY_BASE_DELAY_SECONDS: u64 = 1;
const API_VERSION: &str = "2.0.0";
const DESKTOP_CONTRACT_VERSION: &str = "desktop_backend_v1";
const BACKEND_EVENT: &str = "aima://backend-state";
const CLOSE_TO_TRAY_EVENT: &str = "aima://monitoring-close-to-tray";

#[derive(Debug, Clone, Serialize)]
struct SidecarStatus {
    state: String,
    port: u16,
    base_url: String,
    pid: Option<u32>,
    instance_id: Option<String>,
    contract_version: String,
    ownership_verified: bool,
    restart_count: u32,
    last_error: Option<String>,
}

#[derive(Clone)]
struct SessionIdentity {
    instance_id: String,
    ownership_token: String,
}

struct OwnedSidecar {
    child: Mutex<Option<CommandChild>>,
    ownership: Mutex<Option<std::sync::Arc<OwnedProcessTree>>>,
    status: Mutex<SidecarStatus>,
    session: Mutex<Option<SessionIdentity>>,
    operation_lock: Mutex<()>,
    generation: AtomicU64,
    cleanup_in_progress: AtomicBool,
    ownership_failure_blocked: AtomicBool,
    automatic_recovery_in_progress: AtomicBool,
    manual_restart_in_progress: AtomicBool,
    shutdown_requested: AtomicBool,
    close_notice_sent: AtomicBool,
}

impl OwnedSidecar {
    fn new() -> Self {
        Self {
            child: Mutex::new(None),
            ownership: Mutex::new(None),
            status: Mutex::new(SidecarStatus {
                state: "starting".to_string(),
                port: 0,
                base_url: String::new(),
                pid: None,
                instance_id: None,
                contract_version: DESKTOP_CONTRACT_VERSION.to_string(),
                ownership_verified: false,
                restart_count: 0,
                last_error: None,
            }),
            session: Mutex::new(None),
            operation_lock: Mutex::new(()),
            generation: AtomicU64::new(0),
            cleanup_in_progress: AtomicBool::new(false),
            ownership_failure_blocked: AtomicBool::new(false),
            automatic_recovery_in_progress: AtomicBool::new(false),
            manual_restart_in_progress: AtomicBool::new(false),
            shutdown_requested: AtomicBool::new(false),
            close_notice_sent: AtomicBool::new(false),
        }
    }
}

fn automatic_recovery_delay(attempt: u8) -> Option<Duration> {
    if attempt == 0 || attempt > MAX_AUTOMATIC_RECOVERY_ATTEMPTS {
        return None;
    }
    Some(Duration::from_secs(
        AUTOMATIC_RECOVERY_BASE_DELAY_SECONDS.saturating_mul(1_u64 << (attempt - 1)),
    ))
}

fn automatic_recovery_may_continue(
    expected_generation: u64,
    current_generation: u64,
    manual_restart_in_progress: bool,
    shutdown_requested: bool,
) -> bool {
    expected_generation == current_generation
        && !manual_restart_in_progress
        && !shutdown_requested
}

fn status_transition_is_authorized(
    expected_generation: u64,
    current_generation: u64,
    manual_owner: bool,
    manual_restart_in_progress: bool,
    shutdown_requested: bool,
) -> bool {
    expected_generation == current_generation
        && !shutdown_requested
        && (manual_owner || !manual_restart_in_progress)
}

fn configured_sidecar_port() -> Result<Option<u16>, String> {
    match std::env::var("AIMA_SIDECAR_PORT") {
        Ok(value) => {
            let port = value
                .parse::<u16>()
                .map_err(|_| "AIMA_SIDECAR_PORT must be an integer".to_string())?;
            if !(1024..=65_535).contains(&port) {
                return Err("AIMA_SIDECAR_PORT must be between 1024 and 65535".to_string());
            }
            Ok(Some(port))
        }
        Err(std::env::VarError::NotPresent) => Ok(None),
        Err(std::env::VarError::NotUnicode(_)) => Err("AIMA_SIDECAR_PORT is not valid Unicode".to_string()),
    }
}

fn select_sidecar_port() -> Result<u16, String> {
    let start = configured_sidecar_port()?.unwrap_or(DEFAULT_SIDECAR_PORT);
    for offset in 0..MAX_PORT_PROBES {
        let candidate = start.saturating_add(offset);
        if candidate < 1024 {
            continue;
        }
        if TcpListener::bind(("127.0.0.1", candidate)).is_ok() {
            return Ok(candidate);
        }
    }
    Err(format!("no available loopback port in {start}..{}; foreign listeners were not touched", start.saturating_add(MAX_PORT_PROBES - 1)))
}

#[derive(Clone, Copy)]
struct SidecarHealthEvidence {
    backend_pid: u32,
    launcher_pid: u32,
}

fn health_launcher_matches(expected_launcher: u32, backend_pid: u32, reported_launcher: u32) -> bool {
    #[cfg(target_os = "windows")]
    { let _ = backend_pid; reported_launcher == expected_launcher }
    #[cfg(not(target_os = "windows"))]
    { reported_launcher == expected_launcher || backend_pid == expected_launcher }
}

fn sidecar_health_evidence(
    port: u16,
    identity: &SessionIdentity,
    expected_launcher_pid: u32,
) -> Result<SidecarHealthEvidence, String> {
    let Ok(mut stream) = TcpStream::connect_timeout(
        &std::net::SocketAddr::from(([127, 0, 0, 1], port)),
        Duration::from_millis(800),
    ) else {
        return Err("backend did not accept a loopback connection".to_string());
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let request = format!(
        "GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nX-AIMA-Ownership-Token: {}\r\nConnection: close\r\n\r\n",
        identity.ownership_token
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return Err("backend health request failed".to_string());
    }
    let mut response = String::new();
    if stream.read_to_string(&mut response).is_err() {
        return Err("backend health response failed".to_string());
    }
    if !(response.starts_with("HTTP/1.1 200") || response.starts_with("HTTP/1.0 200")) {
        return Err("backend health returned a non-success status".to_string());
    }
    let body = response.split("\r\n\r\n").nth(1).ok_or_else(|| "backend health response had no body".to_string())?;
    let payload: serde_json::Value = serde_json::from_str(body).map_err(|_| "backend health response was not JSON".to_string())?;
    let backend_pid = payload.get("pid").and_then(serde_json::Value::as_u64).and_then(|value| u32::try_from(value).ok());
    let launcher_pid = payload.get("launcher_pid").and_then(serde_json::Value::as_u64).and_then(|value| u32::try_from(value).ok());
    let pid_matches = match (backend_pid, launcher_pid) {
        (Some(backend), Some(launcher)) => health_launcher_matches(expected_launcher_pid, backend, launcher),
        _ => false,
    };
    let matches = payload.get("status").and_then(serde_json::Value::as_str) == Some("ok")
        && payload.get("ready").and_then(serde_json::Value::as_bool) == Some(true)
        && payload.get("product").and_then(serde_json::Value::as_str) == Some("AI Market Analyst")
        && payload.get("api_version").and_then(serde_json::Value::as_str) == Some(API_VERSION)
        && payload.get("contract_version").and_then(serde_json::Value::as_str) == Some(DESKTOP_CONTRACT_VERSION)
        && payload.get("instance_id").and_then(serde_json::Value::as_str) == Some(identity.instance_id.as_str())
        && backend_pid.is_some_and(|value| value > 0)
        && launcher_pid.is_some_and(|value| value > 0)
        && pid_matches
        && payload.get("port").and_then(serde_json::Value::as_u64) == Some(port as u64)
        && payload.get("ownership_verified").and_then(serde_json::Value::as_bool) == Some(true);
    if !matches {
        return Err("backend health contract or ownership identity mismatch".to_string());
    }
    Ok(SidecarHealthEvidence {
        backend_pid: backend_pid.expect("validated above"),
        launcher_pid: launcher_pid.expect("validated above"),
    })
}

fn sidecar_is_healthy(
    port: u16,
    identity: &SessionIdentity,
    pid: u32,
    ownership: &OwnedProcessTree,
) -> Result<(), String> {
    if ownership.pid() != pid || !ownership.launcher_is_alive() {
        return Err("Tauri-owned launcher process handle is no longer alive".to_string());
    }
    let evidence = sidecar_health_evidence(port, identity, pid)?;
    if evidence.launcher_pid != pid {
        return Err("health response launcher PID did not match the owned child handle".to_string());
    }
    ownership.verify_health_worker(evidence.backend_pid)?;
    if !ownership.launcher_is_alive() {
        return Err("Tauri-owned launcher exited during health ownership verification".to_string());
    }
    Ok(())
}

fn http_json(port: u16, method: &str, path: &str, ownership_token: Option<&str>) -> Option<serde_json::Value> {
    let mut stream = TcpStream::connect_timeout(
        &std::net::SocketAddr::from(([127, 0, 0, 1], port)),
        Duration::from_millis(800),
    )
    .ok()?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let token_header = ownership_token
        .map(|token| format!("X-AIMA-Ownership-Token: {token}\r\n"))
        .unwrap_or_default();
    let request = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n{token_header}Connection: close\r\nContent-Length: 0\r\n\r\n"
    );
    stream.write_all(request.as_bytes()).ok()?;
    let mut response = String::new();
    stream.read_to_string(&mut response).ok()?;
    let body = response.split("\r\n\r\n").nth(1)?;
    serde_json::from_str(body).ok()
}

fn sidecar_generation_can_start(app: &AppHandle, generation: u64, manual_owner: bool) -> bool {
    let Some(state) = app.try_state::<OwnedSidecar>() else {
        return false;
    };
    state.generation.load(Ordering::SeqCst) == generation
        && !state.shutdown_requested.load(Ordering::SeqCst)
        && (manual_owner || !state.manual_restart_in_progress.load(Ordering::SeqCst))
}

fn wait_for_sidecar(
    app: &AppHandle,
    generation: u64,
    port: u16,
    identity: &SessionIdentity,
    pid: u32,
    ownership: &OwnedProcessTree,
    manual_owner: bool,
) -> Result<(), String> {
    let mut last_error = "backend is not ready".to_string();
    for _ in 0..120 {
        if !sidecar_generation_can_start(app, generation, manual_owner) {
            return Err("sidecar startup was superseded by a stop or newer lifecycle action".to_string());
        }
        match sidecar_is_healthy(port, identity, pid, ownership) {
            Ok(()) => return Ok(()),
            Err(error) => last_error = error,
        }
        thread::sleep(Duration::from_millis(250));
    }
    Err(format!("owned backend sidecar did not become ready: {last_error}"))
}

fn owned_backend_ready(app: &AppHandle) -> bool {
    sidecar_status(app)
        .map(|status| status.state == "ready")
        .unwrap_or(false)
}

fn current_port(app: &AppHandle) -> Option<u16> {
    sidecar_status(app).and_then(|status| (status.port > 0).then_some(status.port))
}

fn current_token(app: &AppHandle) -> Option<String> {
    app.try_state::<OwnedSidecar>().and_then(|state| state.session.lock().ok().and_then(|session| session.as_ref().map(|item| item.ownership_token.clone())))
}

fn close_to_tray_enabled(app: &AppHandle) -> bool {
    if !owned_backend_ready(app) {
        return false;
    }
    let Some(port) = current_port(app) else { return false; };
    let token = current_token(app);
    http_json(port, "GET", "/settings/desktop.close_to_tray", token.as_deref())
        .and_then(|payload| payload.get("value").and_then(serde_json::Value::as_bool))
        .unwrap_or(false)
}

fn monitoring_runtime_active(app: &AppHandle) -> Option<bool> {
    if !owned_backend_ready(app) {
        return None;
    }
    let port = current_port(app)?;
    let token = current_token(app);
    http_json(port, "GET", "/monitoring/status", token.as_deref())
        .and_then(|payload| payload.get("active").and_then(serde_json::Value::as_bool))
}

fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_always_on_top(true);
        let _ = window.set_focus();
        let win = window.clone();
        thread::spawn(move || {
            thread::sleep(Duration::from_millis(300));
            let _ = win.set_always_on_top(false);
        });
    }
}

fn route_main_window(app: &AppHandle, route: &str) {
    show_main_window(app);
    let safe_route = match route {
        "/settings" | "/monitoring" | "/alerts" => route,
        _ => "/monitoring",
    };
    if let Some(window) = app.get_webview_window("main") {
        let script = format!(
            "window.history.pushState({{}}, '', '{}'); window.dispatchEvent(new PopStateEvent('popstate'));",
            safe_route
        );
        let _ = window.eval(&script);
    }
}

fn publish_backend_status_locked(app: &AppHandle, status: &SidecarStatus) {
    if let Some(window) = app.get_webview_window("main") {
        let base_url = status.base_url.replace('\\', "").replace('\'', "");
        let _ = window.eval(&format!("window.__AIMA_API_BASE_URL__ = '{}';", base_url));
    }
    let _ = app.emit(BACKEND_EVENT, status.clone());
}

fn update_backend_status(
    current: &mut SidecarStatus,
    state_name: &str,
    port: u16,
    pid: Option<u32>,
    error: Option<String>,
) {
    current.state = state_name.to_string();
    current.port = port;
    current.base_url = if port > 0 { format!("http://127.0.0.1:{port}") } else { String::new() };
    current.pid = pid;
    current.ownership_verified = state_name == "ready";
    current.last_error = error.map(|value| value.chars().take(240).collect());
}

fn sidecar_status(app: &AppHandle) -> Option<SidecarStatus> {
    app.try_state::<OwnedSidecar>()
        .and_then(|state| state.status.lock().ok().map(|status| status.clone()))
}

fn emit_backend_status_for_generation(
    app: &AppHandle,
    generation: u64,
    state_name: &str,
    port: u16,
    pid: Option<u32>,
    error: Option<String>,
) -> bool {
    emit_backend_status_for_generation_owner(app, generation, state_name, port, pid, error, false)
}

fn emit_backend_status_for_generation_owner(
    app: &AppHandle,
    generation: u64,
    state_name: &str,
    port: u16,
    pid: Option<u32>,
    error: Option<String>,
    manual_owner: bool,
) -> bool {
    let Some(state) = app.try_state::<OwnedSidecar>() else {
        return false;
    };
    let Ok(mut current) = state.status.lock() else {
        return false;
    };
    if !status_transition_is_authorized(
        generation,
        state.generation.load(Ordering::SeqCst),
        manual_owner,
        state.manual_restart_in_progress.load(Ordering::SeqCst),
        state.shutdown_requested.load(Ordering::SeqCst),
    ) {
        return false;
    }
    if state_name == "ready" {
        let Some(expected_pid) = pid else { return false; };
        let Ok(child) = state.child.lock() else { return false; };
        let Ok(ownership) = state.ownership.lock() else { return false; };
        if child.as_ref().map(CommandChild::pid) != Some(expected_pid)
            || !ownership.as_ref().is_some_and(|tree| tree.pid() == expected_pid && tree.launcher_is_alive())
        {
            return false;
        }
    }
    update_backend_status(&mut current, state_name, port, pid, error);
    // Keep event publication inside the same short lock as the generation
    // check and status write. A stale ready event cannot overtake stop or a
    // newer start after the lock is released.
    publish_backend_status_locked(app, &current);
    true
}

fn owns_sidecar_generation(app: &AppHandle, generation: u64) -> bool {
    let Some(state) = app.try_state::<OwnedSidecar>() else {
        return false;
    };
    state.generation.load(Ordering::SeqCst) == generation
        && state.child.lock().map(|child| child.is_some()).unwrap_or(false)
}

fn owns_sidecar_pid_generation(
    app: &AppHandle,
    generation: u64,
    pid: u32,
    manual_owner: bool,
) -> bool {
    let Some(state) = app.try_state::<OwnedSidecar>() else {
        return false;
    };
    sidecar_generation_can_start(app, generation, manual_owner)
        && state.child.lock().map(|child| child.as_ref().map(CommandChild::pid) == Some(pid)).unwrap_or(false)
        && state.ownership.lock().map(|ownership| {
            ownership.as_ref().is_some_and(|owned| owned.pid() == pid && owned.launcher_is_alive())
        }).unwrap_or(false)
}

fn registered_sidecar_generation_is_current(
    app: &AppHandle,
    generation: u64,
    pid: u32,
    manual_owner: bool,
) -> bool {
    let Some(state) = app.try_state::<OwnedSidecar>() else { return false; };
    sidecar_generation_can_start(app, generation, manual_owner)
        && state.child.lock().map(|child| child.as_ref().map(CommandChild::pid) == Some(pid)).unwrap_or(false)
        && state.ownership.lock().map(|owned| owned.as_ref().is_some_and(|tree| tree.pid() == pid)).unwrap_or(false)
}

fn owned_sidecar_process_alive(app: &AppHandle) -> bool {
    app.try_state::<OwnedSidecar>()
        .and_then(|state| {
            let child_alive = state.child.lock().ok().map(|child| child.is_some()).unwrap_or(false);
            let cleanup_pending = state.ownership.lock().ok().map(|ownership| ownership.is_some()).unwrap_or(false);
            Some(child_alive || cleanup_pending)
        })
        .unwrap_or(false)
}

fn kill_owned_child(process: CommandChild) {
    // CommandChild.kill delegates to the retained std::process::Child handle,
    // which avoids targeting a recycled numeric PID. Never search by port or
    // executable name. Verified PyInstaller workers are retired through the
    // Job Object held beside this exact launcher handle.
    let _ = process.kill();
}

fn take_owned_child_if_pid(app: &AppHandle, pid: u32) -> Option<CommandChild> {
    let state = app.try_state::<OwnedSidecar>()?;
    let mut child = state.child.lock().ok()?;
    if child.as_ref().map(|owned| owned.pid()) == Some(pid) {
        child.take()
    } else {
        None
    }
}

fn prepare_owned_tree_for_termination(
    port: u16,
    identity: &SessionIdentity,
    pid: u32,
    ownership: &OwnedProcessTree,
) -> Result<(), String> {
    if !ownership.has_verified_worker() {
        let evidence = sidecar_health_evidence(port, identity, pid).map_err(|error| {
            format!("cannot prove the PyInstaller worker is owned; automatic restart is blocked: {error}")
        })?;
        if evidence.launcher_pid != pid {
            return Err("health worker launcher identity changed; automatic restart is blocked".to_string());
        }
        ownership.verify_health_worker(evidence.backend_pid)?;
    }
    if !ownership.terminate_and_wait() {
        return Err("owned sidecar Job Object did not become empty; automatic restart is blocked".to_string());
    }
    Ok(())
}

fn finish_owned_tree_cleanup(
    app: &AppHandle,
    generation: u64,
    ownership: Option<&std::sync::Arc<OwnedProcessTree>>,
) -> bool {
    let Some(state) = app.try_state::<OwnedSidecar>() else { return false; };
    let Ok(_operation) = state.operation_lock.lock() else { return false; };
    let Ok(status) = state.status.lock() else { return false; };
    let Ok(child) = state.child.lock() else { return false; };
    let Ok(mut session) = state.session.lock() else { return false; };
    let Ok(mut current_ownership) = state.ownership.lock() else { return false; };
    if state.generation.load(Ordering::SeqCst) != generation || child.is_some() {
        return false;
    }
    if let Some(expected) = ownership {
        if !current_ownership.as_ref().is_some_and(|current| std::sync::Arc::ptr_eq(current, expected)) {
            return false;
        }
    } else if current_ownership.is_some() {
        return false;
    }
    *current_ownership = None;
    *session = None;
    // Keep the state selected by the caller (stopped/recovering) and only
    // publish after all ownership slots are consistent under the status lock.
    publish_backend_status_locked(app, &status);
    true
}

fn report_cleanup_failure(app: &AppHandle, generation: u64, error: String) {
    let Some(state) = app.try_state::<OwnedSidecar>() else { return; };
    let Ok(mut status) = state.status.lock() else { return; };
    if state.generation.load(Ordering::SeqCst) != generation { return; }
    let port = status.port;
    update_backend_status(&mut status, "degraded", port, None, Some(error));
    publish_backend_status_locked(app, &status);
}

fn retire_owned_generation(
    app: &AppHandle,
    expected_generation: u64,
    expected_pid: Option<u32>,
    state_name: &str,
    error: Option<String>,
) -> Option<u64> {
    let state = app.try_state::<OwnedSidecar>()?;
    if state.cleanup_in_progress.compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst).is_err() {
        return None;
    }
    let _cleanup_guard = AtomicFlagReset(&state.cleanup_in_progress);
    let operation = state.operation_lock.lock().ok()?;
    let (next_generation, port, process, ownership, identity, process_pid) = {
        let mut status = state.status.lock().ok()?;
        let mut child = state.child.lock().ok()?;
        let session = state.session.lock().ok()?;
        let ownership = state.ownership.lock().ok()?;
        if state.generation.load(Ordering::SeqCst) != expected_generation {
            return None;
        }
        if expected_pid.is_some() && child.as_ref().map(CommandChild::pid) != expected_pid {
            return None;
        }
        let next_generation = expected_generation.wrapping_add(1);
        state.generation.store(next_generation, Ordering::SeqCst);
        let port = status.port;
        update_backend_status(&mut status, state_name, port, None, error);
        publish_backend_status_locked(app, &status);
        let process = child.take();
        let ownership = ownership.clone();
        let identity = session.clone();
        let process_pid = expected_pid.or_else(|| ownership.as_ref().map(|item| item.pid()))
            .or_else(|| process.as_ref().map(CommandChild::pid));
        (next_generation, port, process, ownership, identity, process_pid)
    };
    drop(operation);

    let cleanup = match (&ownership, &identity, process_pid) {
        (Some(tree), Some(identity), Some(pid)) => prepare_owned_tree_for_termination(port, identity, pid, tree),
        (Some(_), _, _) => Err("sidecar ownership metadata is incomplete; automatic restart is blocked".to_string()),
        (None, _, _) if process.is_some() => Err("spawned sidecar has no retained process ownership handle".to_string()),
        (None, _, _) => Ok(()),
    };
    if let Some(process) = process { kill_owned_child(process); }
    if let Err(cleanup_error) = cleanup {
        report_cleanup_failure(app, next_generation, cleanup_error);
        return None;
    }

    if !finish_owned_tree_cleanup(app, next_generation, ownership.as_ref()) {
        report_cleanup_failure(app, next_generation, "sidecar cleanup raced a newer lifecycle action".to_string());
        return None;
    }
    Some(next_generation)
}

fn retire_current_owned_sidecar(app: &AppHandle, state_name: &str, error: Option<String>) -> Result<(), String> {
    let Some(state) = app.try_state::<OwnedSidecar>() else {
        return Err("owned sidecar state is unavailable".to_string());
    };
    if state.cleanup_in_progress.compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst).is_err() {
        return Err("sidecar ownership cleanup is already in progress".to_string());
    }
    let _cleanup_guard = AtomicFlagReset(&state.cleanup_in_progress);
    let operation = state.operation_lock.lock().map_err(|_| "sidecar lifecycle lock is unavailable".to_string())?;
    let (generation, port, process, ownership, identity, pid) = {
        let mut status = state.status.lock().map_err(|_| "sidecar status lock is unavailable".to_string())?;
        let mut child = state.child.lock().map_err(|_| "sidecar child lock is unavailable".to_string())?;
        let session = state.session.lock().map_err(|_| "sidecar session lock is unavailable".to_string())?;
        let ownership = state.ownership.lock().map_err(|_| "sidecar ownership lock is unavailable".to_string())?;
        let generation = state.generation.fetch_add(1, Ordering::SeqCst).wrapping_add(1);
        let port = status.port;
        update_backend_status(&mut status, state_name, port, None, error);
        publish_backend_status_locked(app, &status);
        let process = child.take();
        let ownership = ownership.clone();
        let identity = session.clone();
        let pid = ownership.as_ref().map(|tree| tree.pid()).or_else(|| process.as_ref().map(CommandChild::pid));
        (generation, port, process, ownership, identity, pid)
    };
    drop(operation);

    let cleanup = match (&ownership, &identity, pid) {
        (Some(tree), Some(identity), Some(pid)) => prepare_owned_tree_for_termination(port, identity, pid, tree),
        (Some(_), _, _) => Err("sidecar ownership metadata is incomplete".to_string()),
        (None, _, _) if process.is_some() => Err("spawned sidecar has no retained ownership handle".to_string()),
        (None, _, _) => Ok(()),
    };
    if let Some(process) = process { kill_owned_child(process); }
    if let Err(cleanup_error) = cleanup {
        report_cleanup_failure(app, generation, cleanup_error.clone());
        return Err(cleanup_error);
    }
    if !finish_owned_tree_cleanup(app, generation, ownership.as_ref()) {
        let message = "sidecar cleanup raced a newer lifecycle action".to_string();
        report_cleanup_failure(app, generation, message.clone());
        return Err(message);
    }
    Ok(())
}

fn stop_owned_sidecar(app: &AppHandle) {
    let Some(state) = app.try_state::<OwnedSidecar>() else {
        return;
    };
    // Pair the shutdown intent with ready-status publication under the same
    // lock so an old health waiter cannot publish ready after Exit was chosen.
    if let Ok(_status) = state.status.lock() {
        state.shutdown_requested.store(true, Ordering::SeqCst);
    } else {
        state.shutdown_requested.store(true, Ordering::SeqCst);
    }
    let _ = retire_current_owned_sidecar(app, "stopped", None);
}

#[derive(Clone, Copy)]
enum SidecarStartKind {
    Initial,
    AutomaticRecovery,
    ManualRestart,
}

struct SidecarStartFailure {
    message: String,
    recovery_generation: Option<u64>,
}

impl SidecarStartFailure {
    fn terminal(message: impl Into<String>) -> Self {
        Self { message: message.into(), recovery_generation: None }
    }

    fn retryable(message: impl Into<String>, generation: u64) -> Self {
        Self { message: message.into(), recovery_generation: Some(generation) }
    }
}

impl SidecarStartKind {
    fn manual_owner(self) -> bool {
        matches!(self, Self::ManualRestart)
    }
}

struct AtomicFlagReset<'a>(&'a AtomicBool);

impl Drop for AtomicFlagReset<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::SeqCst);
    }
}

fn mark_start_generation_degraded(app: &AppHandle, generation: u64, error: String, manual_owner: bool) {
    let current = sidecar_status(app);
    emit_backend_status_for_generation_owner(
        app,
        generation,
        "degraded",
        current.as_ref().map(|status| status.port).unwrap_or(0),
        None,
        Some(error),
        manual_owner,
    );
}

fn handle_owned_launcher_exit(app: &AppHandle, generation: u64, pid: u32, detail: String) {
    if let Some(recovery_generation) = retire_owned_generation(app, generation, Some(pid), "degraded", Some(detail.clone())) {
        schedule_automatic_recovery(app.clone(), recovery_generation, detail);
    }
}

fn start_owned_sidecar_watchers(
    app: &AppHandle,
    generation: u64,
    port: u16,
    pid: u32,
    ownership: std::sync::Arc<OwnedProcessTree>,
    mut events: tauri::async_runtime::Receiver<CommandEvent>,
    manual_owner: bool,
) {
    let event_app = app.clone();
    let event_ownership = ownership.clone();
    tauri::async_runtime::spawn(async move {
        while let Some(event) = events.recv().await {
            match event {
                CommandEvent::Terminated(payload) => {
                    if registered_sidecar_generation_is_current(&event_app, generation, pid, manual_owner) {
                        handle_owned_launcher_exit(
                            &event_app,
                            generation,
                            pid,
                            format!("owned backend launcher exited with code {:?}", payload.code),
                        );
                    }
                    break;
                }
                CommandEvent::Error(error) => {
                    if registered_sidecar_generation_is_current(&event_app, generation, pid, manual_owner) {
                        emit_backend_status_for_generation(
                            &event_app,
                            generation,
                            "degraded",
                            port,
                            Some(pid),
                            Some(error),
                        );
                    }
                }
                _ => {}
            }
        }
        drop(event_ownership);
    });

    let watch_app = app.clone();
    thread::spawn(move || loop {
        if !registered_sidecar_generation_is_current(&watch_app, generation, pid, manual_owner) {
            break;
        }
        if !ownership.launcher_is_alive() {
            handle_owned_launcher_exit(
                &watch_app,
                generation,
                pid,
                "Tauri-owned launcher process handle exited unexpectedly".to_string(),
            );
            break;
        }
        thread::sleep(Duration::from_millis(250));
    });
}

fn start_owned_sidecar(
    app: &AppHandle,
    start_kind: SidecarStartKind,
) -> Result<u64, SidecarStartFailure> {
    let state = app
        .try_state::<OwnedSidecar>()
        .ok_or_else(|| SidecarStartFailure::terminal("owned sidecar state is unavailable"))?;
    if state.shutdown_requested.load(Ordering::SeqCst) {
        return Err(SidecarStartFailure::terminal(
            "sidecar startup suppressed because this desktop instance is shutting down",
        ));
    }
    let (generation, port, identity, pid, ownership, events) = {
        // Serialize only port selection, spawn, and ownership registration.
        // Health checks can take up to 30 seconds and intentionally happen
        // after this lock is released so stop/restart/exit stay responsive.
        let _operation = state
            .operation_lock
            .lock()
            .map_err(|_| SidecarStartFailure::terminal("sidecar lifecycle lock is unavailable"))?;
        if state.shutdown_requested.load(Ordering::SeqCst) {
            return Err(SidecarStartFailure::terminal(
                "sidecar startup suppressed because this desktop instance is shutting down",
            ));
        }
        if state.manual_restart_in_progress.load(Ordering::SeqCst) && !start_kind.manual_owner() {
            return Err(SidecarStartFailure::terminal(
                "sidecar startup superseded by a manual restart",
            ));
        }
        if state.cleanup_in_progress.load(Ordering::SeqCst) {
            return Err(SidecarStartFailure::terminal("sidecar startup suppressed while prior ownership is being cleaned"));
        }
        if state.ownership_failure_blocked.load(Ordering::SeqCst) {
            return Err(SidecarStartFailure::terminal(
                "previous sidecar ownership could not be proven; restart is blocked until the desktop app is relaunched",
            ));
        }
        if state.ownership.lock().map(|ownership| ownership.is_some()).unwrap_or(true) {
            return Err(SidecarStartFailure::terminal(
                "prior sidecar worker ownership is unresolved; automatic restart is blocked",
            ));
        }

        let (generation, restart_count) = {
            let mut status = state
                .status
                .lock()
                .map_err(|_| SidecarStartFailure::terminal("owned sidecar status lock is unavailable"))?;
            if state.shutdown_requested.load(Ordering::SeqCst)
                || (state.manual_restart_in_progress.load(Ordering::SeqCst)
                    && !start_kind.manual_owner())
            {
                return Err(SidecarStartFailure::terminal("sidecar startup was superseded before its generation began"));
            }
            let generation = state.generation.fetch_add(1, Ordering::SeqCst).wrapping_add(1);
            status.restart_count = status.restart_count.saturating_add(1);
            status.state = "starting".to_string();
            status.port = 0;
            status.base_url.clear();
            status.pid = None;
            status.instance_id = None;
            status.contract_version = DESKTOP_CONTRACT_VERSION.to_string();
            status.ownership_verified = false;
            status.last_error = None;
            publish_backend_status_locked(app, &status);
            (generation, status.restart_count)
        };
        let port = match select_sidecar_port() {
            Ok(port) => port,
            Err(error) => {
                mark_start_generation_degraded(app, generation, error.clone(), start_kind.manual_owner());
                return Err(SidecarStartFailure::retryable(error, generation));
            }
        };
        let identity = SessionIdentity {
            instance_id: Uuid::new_v4().to_string(),
            ownership_token: Uuid::new_v4().to_string(),
        };
        if let Ok(mut status) = state.status.lock() {
            status.port = port;
            status.base_url = format!("http://127.0.0.1:{port}");
            status.instance_id = Some(identity.instance_id.clone());
        }
        emit_backend_status_for_generation_owner(app, generation, "starting", port, None, None, start_kind.manual_owner());
        if !sidecar_generation_can_start(app, generation, start_kind.manual_owner()) {
            return Err(SidecarStartFailure::terminal("sidecar startup superseded before spawn"));
        }

        let command = match app.shell().sidecar("ai-market-analyst-backend") {
            Ok(command) => command,
            Err(_) => {
                let error = "packaged backend sidecar is missing".to_string();
                mark_start_generation_degraded(app, generation, error.clone(), start_kind.manual_owner());
                return Err(SidecarStartFailure::retryable(error, generation));
            }
        }
        // These arguments are assembled in Rust. The WebView has no shell
        // spawn permission and cannot replace the executable, port or model.
        .args({
            let port_arg = port.to_string();
            vec![
                "--host".to_string(),
                "127.0.0.1".to_string(),
                "--port".to_string(),
                port_arg,
                "--instance-id".to_string(),
                identity.instance_id.clone(),
                "--ownership-token".to_string(),
                identity.ownership_token.clone(),
                "--owner-pid".to_string(),
                std::process::id().to_string(),
            ]
        })
        .env("AIMA_SIDECAR_BOUND_PORT", port.to_string())
        .env("AIMA_INSTANCE_ID", identity.instance_id.clone())
        .env("AIMA_OWNERSHIP_TOKEN", identity.ownership_token.clone())
        .env("AIMA_PACKAGED_SIDECAR", "1")
        .env("API_CORS_ORIGINS", "https://tauri.localhost,tauri://localhost,http://tauri.localhost")
        .env("ALLOW_FIXTURE_FALLBACK", "0");
        let (events, child) = match command.spawn() {
            Ok(spawned) => spawned,
            Err(_) => {
                let error = "owned backend sidecar could not be started".to_string();
                mark_start_generation_degraded(app, generation, error.clone(), start_kind.manual_owner());
                return Err(SidecarStartFailure::retryable(error, generation));
            }
        };
        let pid = child.pid();
        let ownership = match OwnedProcessTree::new(pid, std::process::id()) {
            Ok(ownership) => std::sync::Arc::new(ownership),
            Err(error) => {
                state.ownership_failure_blocked.store(true, Ordering::SeqCst);
                kill_owned_child(child);
                let message = format!("could not establish exact sidecar process ownership: {error}");
                mark_start_generation_degraded(app, generation, message.clone(), start_kind.manual_owner());
                return Err(SidecarStartFailure::terminal(message));
            }
        };
        if !sidecar_generation_can_start(app, generation, start_kind.manual_owner()) {
            kill_owned_child(child);
            return Err(SidecarStartFailure::terminal(
                "sidecar startup superseded before child ownership was registered",
            ));
        }
        let mut unregistered_child = Some(child);
        let mut unregistered_ownership = Some(ownership.clone());
        let registration: Result<bool, String> = (|| {
            let mut status = state.status.lock().map_err(|_| "owned sidecar status lock is unavailable".to_string())?;
            let mut slot = state.child.lock().map_err(|_| "owned sidecar child state is unavailable".to_string())?;
            let mut session = state.session.lock().map_err(|_| "owned sidecar session state is unavailable".to_string())?;
            let mut owned_tree = state.ownership.lock().map_err(|_| "owned sidecar process ownership lock is unavailable".to_string())?;
            let still_current = state.generation.load(Ordering::SeqCst) == generation
                && !state.shutdown_requested.load(Ordering::SeqCst)
                && (start_kind.manual_owner()
                    || !state.manual_restart_in_progress.load(Ordering::SeqCst));
            if !still_current || slot.is_some() || owned_tree.is_some() {
                Ok(false)
            } else {
                *slot = unregistered_child.take();
                *owned_tree = unregistered_ownership.take();
                status.pid = Some(pid);
                status.restart_count = restart_count;
                *session = Some(identity.clone());
                publish_backend_status_locked(app, &status);
                Ok(true)
            }
        })();
        let registered = match registration {
            Ok(registered) => registered,
            Err(error) => {
                let cleanup = unregistered_ownership
                    .as_ref()
                    .map(|tree| prepare_owned_tree_for_termination(port, &identity, pid, tree));
                if let Some(process) = unregistered_child.take() {
                    kill_owned_child(process);
                }
                if let Some(Err(_)) = cleanup {
                    state.ownership_failure_blocked.store(true, Ordering::SeqCst);
                }
                if let Some(tree) = unregistered_ownership.take() {
                    if !tree.terminate_and_wait() {
                        state.ownership_failure_blocked.store(true, Ordering::SeqCst);
                    }
                }
                mark_start_generation_degraded(app, generation, error.clone(), start_kind.manual_owner());
                return Err(SidecarStartFailure::retryable(error, generation));
            }
        };
        if !registered {
            // The exact newly spawned handle was never installed over another
            // child. Retire only this local handle if a lifecycle action won.
            let cleanup = unregistered_ownership
                .as_ref()
                .map(|tree| prepare_owned_tree_for_termination(port, &identity, pid, tree));
            if let Some(process) = unregistered_child.take() {
                kill_owned_child(process);
            }
            if let Some(tree) = unregistered_ownership.take() {
                if !tree.terminate_and_wait() || matches!(cleanup, Some(Err(_))) {
                    state.ownership_failure_blocked.store(true, Ordering::SeqCst);
                }
            }
            let error = "sidecar startup was superseded or another owned child is still registered".to_string();
            let generation_is_current = state.generation.load(Ordering::SeqCst) == generation
                && !state.shutdown_requested.load(Ordering::SeqCst)
                && (start_kind.manual_owner()
                    || !state.manual_restart_in_progress.load(Ordering::SeqCst));
            if generation_is_current {
                mark_start_generation_degraded(app, generation, error.clone(), start_kind.manual_owner());
                return Err(SidecarStartFailure::retryable(error, generation));
            }
            return Err(SidecarStartFailure::terminal(error));
        }
        if !owns_sidecar_pid_generation(app, generation, pid, start_kind.manual_owner()) {
            if let Some(process) = take_owned_child_if_pid(app, pid) {
                kill_owned_child(process);
            }
            return Err(SidecarStartFailure::terminal(
                "sidecar startup superseded while registering child ownership",
            ));
        }
        emit_backend_status_for_generation_owner(app, generation, "starting", port, Some(pid), None, start_kind.manual_owner());
        (generation, port, identity, pid, ownership, events)
    };

    start_owned_sidecar_watchers(
        app,
        generation,
        port,
        pid,
        ownership.clone(),
        events,
        start_kind.manual_owner(),
    );

    if let Err(error) = wait_for_sidecar(app, generation, port, &identity, pid, &ownership, start_kind.manual_owner()) {
        let retired_generation = retire_owned_generation(app, generation, Some(pid), "degraded", Some(error.clone()));
        return Err(SidecarStartFailure { message: error, recovery_generation: retired_generation });
    }
    // Health can return after a manual stop/restart has replaced this child.
    // Recheck both generation and exact child PID before announcing readiness.
    if !owns_sidecar_pid_generation(app, generation, pid, start_kind.manual_owner()) {
        return Err(SidecarStartFailure::terminal(
            "sidecar startup was superseded before health ownership verification",
        ));
    }
    if !emit_backend_status_for_generation_owner(app, generation, "ready", port, Some(pid), None, start_kind.manual_owner()) {
        return Err(SidecarStartFailure::terminal("owned launcher exited or lifecycle changed before ready publication"));
    }

    let health_app = app.clone();
    let health_identity = identity.clone();
    let health_ownership = ownership.clone();
    thread::spawn(move || {
        let mut misses = 0_u8;
        loop {
            thread::sleep(Duration::from_secs(2));
            if !owns_sidecar_generation(&health_app, generation) {
                break;
            }
            let Some(pid) = sidecar_status(&health_app).and_then(|status| status.pid) else {
                misses = misses.saturating_add(1);
                continue;
            };
            if sidecar_is_healthy(port, &health_identity, pid, &health_ownership).is_ok() {
                misses = 0;
                if sidecar_status(&health_app).map(|status| status.state.as_str() != "ready").unwrap_or(false) {
                    emit_backend_status_for_generation(&health_app, generation, "ready", port, Some(pid), None);
                }
                continue;
            }
            misses = misses.saturating_add(1);
            if misses >= 3 {
                let detail = "owned backend health or launcher ownership watchdog failed three checks".to_string();
                emit_backend_status_for_generation(
                    &health_app,
                    generation,
                    "degraded",
                    port,
                    None,
                    Some(detail.clone()),
                );
                schedule_automatic_recovery(health_app.clone(), generation, detail);
                break;
            }
        }
    });
    Ok(generation)
}

fn automatic_recovery_is_current(app: &AppHandle, expected_generation: u64) -> bool {
    let Some(state) = app.try_state::<OwnedSidecar>() else {
        return false;
    };
    automatic_recovery_may_continue(
        expected_generation,
        state.generation.load(Ordering::SeqCst),
        state.manual_restart_in_progress.load(Ordering::SeqCst),
        state.shutdown_requested.load(Ordering::SeqCst),
    )
}

fn schedule_automatic_recovery(app: AppHandle, generation: u64, cause: String) {
    let Some(state) = app.try_state::<OwnedSidecar>() else {
        return;
    };
    if !automatic_recovery_is_current(&app, generation)
        || state.automatic_recovery_in_progress.compare_exchange(
            false,
            true,
            Ordering::SeqCst,
            Ordering::SeqCst,
        ).is_err()
    {
        return;
    }
    let port = sidecar_status(&app).map(|status| status.port).unwrap_or(0);
    let first_delay = automatic_recovery_delay(1).unwrap_or(Duration::from_secs(1));
    emit_backend_status_for_generation(
        &app,
        generation,
        "recovering",
        port,
        None,
        Some(format!(
            "Owned sidecar recovery 1/{MAX_AUTOMATIC_RECOVERY_ATTEMPTS} scheduled in {}s: {cause}",
            first_delay.as_secs(),
        )),
    );

    thread::spawn(move || {
        let Some(recovery_state) = app.try_state::<OwnedSidecar>() else {
            return;
        };
        let _recovery_guard = AtomicFlagReset(&recovery_state.automatic_recovery_in_progress);
        let mut expected_generation = generation;
        let mut last_error = cause;

        for attempt in 1..=MAX_AUTOMATIC_RECOVERY_ATTEMPTS {
            if !automatic_recovery_is_current(&app, expected_generation) {
                return;
            }
            let Some(delay) = automatic_recovery_delay(attempt) else {
                break;
            };
            let status = sidecar_status(&app);
            emit_backend_status_for_generation(
                &app,
                expected_generation,
                "recovering",
                status.as_ref().map(|item| item.port).unwrap_or(0),
                None,
                Some(format!(
                    "Owned sidecar recovery {attempt}/{MAX_AUTOMATIC_RECOVERY_ATTEMPTS} in {}s: {last_error}",
                    delay.as_secs(),
                )),
            );
            // Backoff holds no lifecycle lock; explicit stop/restart can bump
            // generation and cancel this worker immediately.
            thread::sleep(delay);
            if !automatic_recovery_is_current(&app, expected_generation) {
                return;
            }

            if retire_owned_generation(
                &app,
                expected_generation,
                None,
                "recovering",
                Some(format!("Automatic recovery attempt {attempt}/{MAX_AUTOMATIC_RECOVERY_ATTEMPTS}")),
            ).is_none() {
                return;
            }
            match start_owned_sidecar(&app, SidecarStartKind::AutomaticRecovery) {
                Ok(_) => return,
                Err(failure) => {
                    last_error = failure.message;
                    if recovery_state.manual_restart_in_progress.load(Ordering::SeqCst)
                        || recovery_state.shutdown_requested.load(Ordering::SeqCst)
                    {
                        return;
                    }
                    let Some(next_generation) = failure.recovery_generation else {
                        return;
                    };
                    expected_generation = next_generation;
                    if !automatic_recovery_is_current(&app, expected_generation) {
                        return;
                    }
                }
            }
        }

        if automatic_recovery_is_current(&app, expected_generation) {
            let status = sidecar_status(&app);
            emit_backend_status_for_generation(
                &app,
                expected_generation,
                "degraded",
                status.as_ref().map(|item| item.port).unwrap_or(0),
                None,
                Some(format!(
                    "Owned sidecar recovery exhausted after {MAX_AUTOMATIC_RECOVERY_ATTEMPTS} attempts; manual restart required: {last_error}"
                )),
            );
        }
    });
}

fn set_monitoring_tray_text<R: tauri::Runtime>(app: &AppHandle, status_item: &MenuItem<R>, toggle_item: &MenuItem<R>) {
    if !owned_backend_ready(app) {
        let _ = status_item.set_text("盯盘降级：后端不可用");
        let _ = toggle_item.set_text("恢复盯盘");
        return;
    }
    let Some(port) = current_port(app) else {
        let _ = status_item.set_text("盯盘降级：后端不可用");
        let _ = toggle_item.set_text("恢复盯盘");
        return;
    };
    let token = current_token(app);
    let Some(payload) = http_json(port, "GET", "/monitoring/status", token.as_deref()) else {
        let _ = status_item.set_text("盯盘降级：后端不可用");
        let _ = toggle_item.set_text("恢复盯盘");
        return;
    };
    let state = payload.get("state").and_then(serde_json::Value::as_str).unwrap_or("unknown");
    let active = payload.get("active").and_then(serde_json::Value::as_bool).unwrap_or(false);
    let symbols = payload
        .get("active_symbols")
        .and_then(serde_json::Value::as_array)
        .map(|items| items.len())
        .unwrap_or(0);
    let state_zh = match state.to_lowercase().as_str() {
        "running" => "运行中", "paused" => "已暂停", "stopped" => "已停止",
        "terminated" => "已终止", "starting" => "正在启动", "degraded" => "降级运行", _ => "待核验",
    };
    let _ = status_item.set_text(format!("盯盘：{} · {} 个品种", state_zh, symbols));
    let _ = toggle_item.set_text(if active { "暂停盯盘" } else { "恢复盯盘" });
}

fn notify_monitoring_continues(app: &AppHandle) {
    let Some(state) = app.try_state::<OwnedSidecar>() else {
        return;
    };
    if state.close_notice_sent.swap(true, Ordering::SeqCst) {
        return;
    }
    let _ = app.emit(
        CLOSE_TO_TRAY_EVENT,
        serde_json::json!({"active": true, "route": "/monitoring"}),
    );
    // The native notice is best effort; the always-mounted WebView bridge
    // supplies an in-app routed notice even when Windows notification access
    // is denied.
    let _ = app
        .notification()
        .builder()
        .title("AI 市场分析师")
        .body("盯盘仍在后台运行。可通过系统托盘暂停盯盘或退出应用。")
        .show();
}

#[tauri::command]
fn restart_backend(app: AppHandle) -> Result<SidecarStatus, String> {
    let state = app
        .try_state::<OwnedSidecar>()
        .ok_or_else(|| "owned sidecar state is unavailable".to_string())?;
    if state.shutdown_requested.load(Ordering::SeqCst) {
        return Err("backend restart is unavailable while this desktop instance is shutting down".to_string());
    }
    {
        let _status = state
            .status
            .lock()
            .map_err(|_| "owned sidecar status lock is unavailable".to_string())?;
        if state.shutdown_requested.load(Ordering::SeqCst) {
            return Err("backend restart is unavailable while this desktop instance is shutting down".to_string());
        }
        state
            .manual_restart_in_progress
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .map_err(|_| "a manual backend restart is already in progress".to_string())?;
    }
    let _manual_restart_guard = AtomicFlagReset(&state.manual_restart_in_progress);

    // Invalidate any startup/recovery health waiter before replacing its exact
    // owned child. The generation bump makes stale workers unable to publish
    // ready, while no lifecycle lock is held during stop/start or health I/O.
    if let Err(error) = retire_current_owned_sidecar(
        &app,
        "recovering",
        Some("Manual backend restart requested".to_string()),
    ) {
        return Err(format!("manual restart was blocked because the previous sidecar is not safely retired: {error}"));
    }
    match start_owned_sidecar(&app, SidecarStartKind::ManualRestart) {
        Ok(_) => sidecar_status(&app).ok_or_else(|| "backend status is unavailable after restart".to_string()),
        Err(failure) => {
            if let Some(generation) = failure.recovery_generation {
                mark_start_generation_degraded(&app, generation, failure.message.clone(), true);
            }
            Err(failure.message)
        }
    }
}

#[tauri::command]
fn backend_status(app: AppHandle) -> Result<SidecarStatus, String> {
    sidecar_status(&app).ok_or_else(|| "owned backend status is unavailable".to_string())
}

fn main() {
    std::panic::set_hook(Box::new(|info| {
        let log_dir = std::env::var("LOCALAPPDATA")
            .map(|d| std::path::PathBuf::from(d).join("AI Market Analyst").join("logs"))
            .unwrap_or_else(|_| std::path::PathBuf::from("."));
        let _ = std::fs::create_dir_all(&log_dir);
        let panic_file = log_dir.join("tauri_panic.log");
        let payload = format!("Panic occurred: {}\nLocation: {:?}\n", info, info.location());
        let _ = std::fs::write(panic_file, payload);
    }));

    tauri::Builder::default()
        .manage(OwnedSidecar::new())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_shell::init())
        // A second launch must never create a duplicate instance (which would
        // start a second owned sidecar and contend for the same SQLite
        // database).  Instead it focuses the existing window, including when
        // that window was hidden to the tray while monitoring was active.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_main_window(app);
        }))
        .invoke_handler(tauri::generate_handler![backend_status, restart_backend])
        .setup(|app| {
            #[cfg(desktop)]
            app.handle().plugin(
                tauri_plugin_autostart::Builder::new()
                    .app_name("AI Market Analyst")
                    .build(),
            )?;

            let startup_app = app.handle().clone();
            thread::spawn(move || {
                if let Err(failure) = start_owned_sidecar(&startup_app, SidecarStartKind::Initial) {
                    if let Some(generation) = failure.recovery_generation {
                        schedule_automatic_recovery(startup_app.clone(), generation, failure.message);
                    }
                }
            });

            let show = MenuItem::with_id(app, "show", "显示交易员工作台", true, None::<&str>)?;
            let terminal = MenuItem::with_id(app, "terminal", "打开盯盘终端", true, None::<&str>)?;
            let monitoring_status = MenuItem::with_id(app, "monitoring-status", "盯盘：正在启动", false, None::<&str>)?;
            let monitoring_toggle = MenuItem::with_id(app, "monitoring-toggle", "恢复盯盘", true, None::<&str>)?;
            let settings = MenuItem::with_id(app, "settings", "桌面设置", true, None::<&str>)?;
            let restart = MenuItem::with_id(app, "restart-backend", "重启后端", true, None::<&str>)?;
            let exit = MenuItem::with_id(app, "exit", "退出", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &terminal, &monitoring_status, &monitoring_toggle, &settings, &restart, &exit])?;

            let tray_status = monitoring_status.clone();
            let tray_toggle = monitoring_toggle.clone();
            let tray_app = app.handle().clone();
            thread::spawn(move || loop {
                thread::sleep(Duration::from_secs(2));
                if sidecar_status(&tray_app).is_none() {
                    break;
                }
                set_monitoring_tray_text(&tray_app, &tray_status, &tray_toggle);
            });

            let event_status = monitoring_status.clone();
            let event_toggle = monitoring_toggle.clone();
            TrayIconBuilder::with_id("main-tray")
                .menu(&menu)
                .tooltip("AI 市场分析师 · 本地盯盘")
                .on_menu_event(move |app, event| match event.id().as_ref() {
                    "show" => show_main_window(app),
                    "terminal" => route_main_window(app, "/monitoring"),
                    "settings" => route_main_window(app, "/settings"),
                    "monitoring-toggle" => {
                        let active = monitoring_runtime_active(app).unwrap_or(false);
                        let path = if active { "/monitoring/pause" } else { "/monitoring/resume" };
                        let port = current_port(app);
                        let token = current_token(app);
                        let action_succeeded = port
                            .and_then(|value| http_json(value, "POST", path, token.as_deref()))
                            .map(|payload| payload.get("error").is_none())
                            .unwrap_or(false);
                        if !owned_backend_ready(app) || !action_succeeded {
                            let _ = event_status.set_text("盯盘降级：操作失败");
                            let _ = event_toggle.set_text("恢复盯盘");
                        } else {
                            set_monitoring_tray_text(app, &event_status, &event_toggle);
                        }
                    }
                    "restart-backend" => {
                        if restart_backend(app.clone()).is_err() {
                            let _ = event_status.set_text("Monitoring: DEGRADED · backend restart failed");
                        }
                    }
                    "exit" => {
                        stop_owned_sidecar(app);
                        app.exit(0);
                    }
                    _ => {}
                })
                .build(app)?;
            set_monitoring_tray_text(&app.handle(), &monitoring_status, &monitoring_toggle);
            show_main_window(&app.handle());
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let active = monitoring_runtime_active(&window.app_handle());
                let owned_backend_alive = owned_sidecar_process_alive(&window.app_handle());
                if active == Some(true) || (active.is_none() && owned_backend_alive) {
                    api.prevent_close();
                    let _ = window.hide();
                    notify_monitoring_continues(&window.app_handle());
                } else if close_to_tray_enabled(&window.app_handle()) {
                    api.prevent_close();
                    let _ = window.hide();
                } else {
                    stop_owned_sidecar(&window.app_handle());
                    // A tray icon keeps the Tauri event loop alive after the
                    // last window closes.  In the explicit non-tray branch,
                    // terminate the application as well as its exact-owned
                    // sidecar so X has the documented exit semantics.
                    window.app_handle().exit(0);
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running AI Market Analyst desktop");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn automatic_recovery_uses_three_bounded_exponential_delays() {
        assert_eq!(automatic_recovery_delay(1), Some(Duration::from_secs(1)));
        assert_eq!(automatic_recovery_delay(2), Some(Duration::from_secs(2)));
        assert_eq!(automatic_recovery_delay(3), Some(Duration::from_secs(4)));
        assert_eq!(automatic_recovery_delay(0), None);
        assert_eq!(automatic_recovery_delay(4), None);
        assert_eq!(MAX_AUTOMATIC_RECOVERY_ATTEMPTS, 3);
    }

    #[test]
    fn stale_manual_and_shutdown_generations_cancel_automatic_recovery() {
        assert!(automatic_recovery_may_continue(7, 7, false, false));
        assert!(!automatic_recovery_may_continue(7, 8, false, false));
        assert!(!automatic_recovery_may_continue(7, 7, true, false));
        assert!(!automatic_recovery_may_continue(7, 7, false, true));
    }

    #[test]
    fn ready_or_degraded_status_cannot_publish_for_a_stale_generation() {
        assert!(status_transition_is_authorized(4, 4, false, false, false));
        assert!(!status_transition_is_authorized(4, 5, false, false, false));
        assert!(!status_transition_is_authorized(4, 4, false, true, false));
        assert!(status_transition_is_authorized(4, 4, true, true, false));
        assert!(!status_transition_is_authorized(4, 4, true, false, true));
    }

    #[test]
    fn health_identity_cannot_be_satisfied_by_an_unrelated_worker_pid_on_windows() {
        let expected_launcher = 41;
        let worker_pid = 52;
        assert!(health_launcher_matches(expected_launcher, worker_pid, expected_launcher));
        assert!(!health_launcher_matches(expected_launcher, worker_pid, 99));
        #[cfg(target_os = "windows")]
        assert!(!health_launcher_matches(expected_launcher, expected_launcher, 99));
    }
}
