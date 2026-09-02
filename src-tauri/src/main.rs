#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;
use std::thread;
use std::time::Duration;

#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

use serde::Serialize;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, WindowEvent};
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;
use uuid::Uuid;

const DEFAULT_SIDECAR_PORT: u16 = 18_765;
const MAX_PORT_PROBES: u16 = 64;
const API_VERSION: &str = "1.2.1";
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
    status: Mutex<SidecarStatus>,
    session: Mutex<Option<SessionIdentity>>,
    generation: AtomicU64,
    close_notice_sent: AtomicBool,
}

impl OwnedSidecar {
    fn new() -> Self {
        Self {
            child: Mutex::new(None),
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
            generation: AtomicU64::new(0),
            close_notice_sent: AtomicBool::new(false),
        }
    }
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

fn sidecar_is_healthy(port: u16, identity: &SessionIdentity, pid: u32) -> Result<(), String> {
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
    let matches = payload.get("status").and_then(serde_json::Value::as_str) == Some("ok")
        && payload.get("ready").and_then(serde_json::Value::as_bool) == Some(true)
        && payload.get("product").and_then(serde_json::Value::as_str) == Some("AI Market Analyst")
        && payload.get("api_version").and_then(serde_json::Value::as_str) == Some(API_VERSION)
        && payload.get("contract_version").and_then(serde_json::Value::as_str) == Some(DESKTOP_CONTRACT_VERSION)
        && payload.get("instance_id").and_then(serde_json::Value::as_str) == Some(identity.instance_id.as_str())
        // PyInstaller one-file uses a small bootstrap process which owns the
        // child Python process.  The child PID is still reported truthfully;
        // launcher_pid binds the health contract to the exact Rust-owned
        // process tree that can be stopped without touching foreign listeners.
        && payload.get("pid").and_then(serde_json::Value::as_u64).map(|value| value > 0).unwrap_or(false)
        && payload.get("launcher_pid").and_then(serde_json::Value::as_u64) == Some(pid as u64)
        && payload.get("port").and_then(serde_json::Value::as_u64) == Some(port as u64)
        && payload.get("ownership_verified").and_then(serde_json::Value::as_bool) == Some(true);
    if matches { Ok(()) } else { Err("backend health contract or ownership identity mismatch".to_string()) }
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

fn wait_for_sidecar(port: u16, identity: &SessionIdentity, pid: u32) -> Result<(), String> {
    let mut last_error = "backend is not ready".to_string();
    for _ in 0..120 {
        match sidecar_is_healthy(port, identity, pid) {
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
        let _ = window.set_focus();
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

fn emit_backend_status(app: &AppHandle, state_name: &str, port: u16, pid: Option<u32>, error: Option<String>) {
    let Some(state) = app.try_state::<OwnedSidecar>() else {
        return;
    };
    let status = if let Ok(mut current) = state.status.lock() {
        current.state = state_name.to_string();
        current.port = port;
        current.base_url = if port > 0 { format!("http://127.0.0.1:{port}") } else { String::new() };
        current.pid = pid;
        current.ownership_verified = state_name == "ready";
        current.last_error = error.map(|value| value.chars().take(240).collect());
        current.clone()
    } else {
        return;
    };
    if let Some(window) = app.get_webview_window("main") {
        let base_url = status.base_url.replace('\\', "").replace('\'', "");
        let _ = window.eval(&format!("window.__AIMA_API_BASE_URL__ = '{}';", base_url));
    }
    let _ = app.emit(BACKEND_EVENT, status);
}

fn sidecar_status(app: &AppHandle) -> Option<SidecarStatus> {
    app.try_state::<OwnedSidecar>()
        .and_then(|state| state.status.lock().ok().map(|status| status.clone()))
}

fn owns_sidecar_generation(app: &AppHandle, generation: u64) -> bool {
    let Some(state) = app.try_state::<OwnedSidecar>() else {
        return false;
    };
    state.generation.load(Ordering::SeqCst) == generation
        && state.child.lock().map(|child| child.is_some()).unwrap_or(false)
}

fn owned_sidecar_process_alive(app: &AppHandle) -> bool {
    app.try_state::<OwnedSidecar>()
        .and_then(|state| state.child.lock().ok().map(|child| child.is_some()))
        .unwrap_or(false)
}

fn clear_child_if_generation(app: &AppHandle, generation: u64) -> bool {
    let Some(state) = app.try_state::<OwnedSidecar>() else {
        return false;
    };
    if state.generation.load(Ordering::SeqCst) != generation {
        return false;
    }
    if let Ok(mut child) = state.child.lock() {
        *child = None;
    }
    if let Ok(mut session) = state.session.lock() {
        *session = None;
    }
    true
}

fn stop_owned_sidecar(app: &AppHandle) {
    let Some(state) = app.try_state::<OwnedSidecar>() else {
        return;
    };
    state.generation.fetch_add(1, Ordering::SeqCst);
    let process = state.child.lock().ok().and_then(|mut child| child.take());
    if let Ok(mut session) = state.session.lock() {
        *session = None;
    }
    if let Some(process) = process {
        let pid = process.pid();
        // This is the exact PID returned by the Rust-owned sidecar spawn.  No
        // port/name search is used, so foreign processes remain untouched.
        #[cfg(target_os = "windows")]
        {
            let _ = std::process::Command::new("taskkill")
                .args(["/PID", &pid.to_string(), "/T", "/F"])
                .creation_flags(0x0800_0000)
                .output();
        }
        let _ = process.kill();
    }
    emit_backend_status(app, "stopped", 0, None, None);
}

fn start_owned_sidecar(app: &AppHandle) -> Result<(), String> {
    let port = select_sidecar_port()?;
    let state = app
        .try_state::<OwnedSidecar>()
        .ok_or_else(|| "owned sidecar state is unavailable".to_string())?;
    let generation = state.generation.fetch_add(1, Ordering::SeqCst) + 1;
    let identity = SessionIdentity {
        instance_id: Uuid::new_v4().to_string(),
        ownership_token: Uuid::new_v4().to_string(),
    };
    let restart_count = if let Ok(mut status) = state.status.lock() {
        status.restart_count = status.restart_count.saturating_add(1);
        status.state = "starting".to_string();
        status.port = port;
        status.base_url = format!("http://127.0.0.1:{port}");
        status.pid = None;
        status.instance_id = Some(identity.instance_id.clone());
        status.contract_version = DESKTOP_CONTRACT_VERSION.to_string();
        status.ownership_verified = false;
        status.last_error = None;
        status.restart_count
    } else {
        0
    };
    emit_backend_status(app, "starting", port, None, None);

    let command = app
        .shell()
        .sidecar("ai-market-analyst-backend")
        .map_err(|_| "packaged backend sidecar is missing".to_string())?
        // These arguments are assembled in Rust.  The WebView has no shell
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
            ]
        })
        .env("AIMA_SIDECAR_BOUND_PORT", port.to_string())
        .env("AIMA_INSTANCE_ID", identity.instance_id.clone())
        .env("AIMA_OWNERSHIP_TOKEN", identity.ownership_token.clone())
        .env("AIMA_PACKAGED_SIDECAR", "1")
        .env("API_CORS_ORIGINS", "tauri://localhost,http://tauri.localhost")
        .env("ALLOW_FIXTURE_FALLBACK", "0");
    let (mut events, child) = command
        .spawn()
        .map_err(|_| "owned backend sidecar could not be started".to_string())?;
    let pid = child.pid();
    if let Ok(mut slot) = state.child.lock() {
        *slot = Some(child);
    }
    if let Ok(mut status) = state.status.lock() {
        status.pid = Some(pid);
        status.restart_count = restart_count;
    }
    if let Ok(mut session) = state.session.lock() {
        *session = Some(identity.clone());
    }
    emit_backend_status(app, "starting", port, Some(pid), None);

    if let Err(error) = wait_for_sidecar(port, &identity, pid) {
        stop_owned_sidecar(app);
        emit_backend_status(app, "degraded", port, None, Some(error.clone()));
        return Err(error);
    }
    emit_backend_status(app, "ready", port, Some(pid), None);

    let event_app = app.clone();
    tauri::async_runtime::spawn(async move {
        while let Some(event) = events.recv().await {
            match event {
                CommandEvent::Terminated(payload) => {
                    if owns_sidecar_generation(&event_app, generation) {
                        let detail = format!("owned backend exited with code {:?}", payload.code);
                        clear_child_if_generation(&event_app, generation);
                        emit_backend_status(&event_app, "degraded", port, None, Some(detail));
                    }
                    break;
                }
                CommandEvent::Error(error) => {
                    if owns_sidecar_generation(&event_app, generation) {
                        emit_backend_status(&event_app, "degraded", port, None, Some(error));
                    }
                }
                _ => {}
            }
        }
    });

    let health_app = app.clone();
    thread::spawn(move || {
        let mut misses = 0_u8;
        loop {
            thread::sleep(Duration::from_secs(2));
            if !owns_sidecar_generation(&health_app, generation) {
                break;
            }
            let Some(identity) = health_app
                .try_state::<OwnedSidecar>()
                .and_then(|state| state.session.lock().ok().and_then(|value| value.clone()))
            else {
                break;
            };
            let Some(pid) = sidecar_status(&health_app).and_then(|status| status.pid) else {
                misses = misses.saturating_add(1);
                continue;
            };
            if sidecar_is_healthy(port, &identity, pid).is_ok() {
                misses = 0;
                continue;
            }
            misses = misses.saturating_add(1);
            if misses >= 3 {
                emit_backend_status(
                    &health_app,
                    "degraded",
                    port,
                    None,
                    Some("owned backend health watchdog could not reach /health".to_string()),
                );
                break;
            }
        }
    });
    Ok(())
}

fn set_monitoring_tray_text<R: tauri::Runtime>(app: &AppHandle, status_item: &MenuItem<R>, toggle_item: &MenuItem<R>) {
    if !owned_backend_ready(app) {
        let _ = status_item.set_text("Monitoring: DEGRADED · backend unavailable");
        let _ = toggle_item.set_text("Resume monitoring");
        return;
    }
    let Some(port) = current_port(app) else {
        let _ = status_item.set_text("Monitoring: DEGRADED · backend unavailable");
        let _ = toggle_item.set_text("Resume monitoring");
        return;
    };
    let token = current_token(app);
    let Some(payload) = http_json(port, "GET", "/monitoring/status", token.as_deref()) else {
        let _ = status_item.set_text("Monitoring: DEGRADED · backend unavailable");
        let _ = toggle_item.set_text("Resume monitoring");
        return;
    };
    let state = payload.get("state").and_then(serde_json::Value::as_str).unwrap_or("unknown");
    let active = payload.get("active").and_then(serde_json::Value::as_bool).unwrap_or(false);
    let symbols = payload
        .get("active_symbols")
        .and_then(serde_json::Value::as_array)
        .map(|items| items.len())
        .unwrap_or(0);
    let _ = status_item.set_text(format!("Monitoring: {} · {} symbols", state.to_uppercase(), symbols));
    let _ = toggle_item.set_text(if active { "Pause monitoring" } else { "Resume monitoring" });
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
        .title("AI Market Analyst")
        .body("Monitoring continues in the background. Open the tray to pause or exit.")
        .show();
}

#[tauri::command]
fn restart_backend(app: AppHandle) -> Result<SidecarStatus, String> {
    stop_owned_sidecar(&app);
    match start_owned_sidecar(&app) {
        Ok(()) => sidecar_status(&app).ok_or_else(|| "backend status is unavailable after restart".to_string()),
        Err(error) => {
            emit_backend_status(&app, "degraded", 0, None, Some(error.clone()));
            Err(error)
        }
    }
}

#[tauri::command]
fn backend_status(app: AppHandle) -> Result<SidecarStatus, String> {
    sidecar_status(&app).ok_or_else(|| "owned backend status is unavailable".to_string())
}

fn main() {
    tauri::Builder::default()
        .manage(OwnedSidecar::new())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_shell::init())
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

            if let Err(error) = start_owned_sidecar(&app.handle()) {
                emit_backend_status(&app.handle(), "degraded", 0, None, Some(error));
            }

            let show = MenuItem::with_id(app, "show", "Show AI Market Analyst", true, None::<&str>)?;
            let terminal = MenuItem::with_id(app, "terminal", "Open monitoring terminal", true, None::<&str>)?;
            let monitoring_status = MenuItem::with_id(app, "monitoring-status", "Monitoring: STARTING", false, None::<&str>)?;
            let monitoring_toggle = MenuItem::with_id(app, "monitoring-toggle", "Resume monitoring", true, None::<&str>)?;
            let settings = MenuItem::with_id(app, "settings", "Desktop settings", true, None::<&str>)?;
            let restart = MenuItem::with_id(app, "restart-backend", "Restart backend", true, None::<&str>)?;
            let exit = MenuItem::with_id(app, "exit", "Exit", true, None::<&str>)?;
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
                .tooltip("AI Market Analyst · local monitoring")
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
                            let _ = event_status.set_text("Monitoring: DEGRADED · action failed");
                            let _ = event_toggle.set_text("Resume monitoring");
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
