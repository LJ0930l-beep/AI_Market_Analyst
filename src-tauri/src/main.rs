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

const DEFAULT_SIDECAR_PORT: u16 = 18_765;
const BACKEND_EVENT: &str = "aima://backend-state";
const CLOSE_TO_TRAY_EVENT: &str = "aima://monitoring-close-to-tray";

#[derive(Debug, Clone, Serialize)]
struct SidecarStatus {
    state: String,
    port: u16,
    pid: Option<u32>,
    restart_count: u32,
    last_error: Option<String>,
}

struct OwnedSidecar {
    child: Mutex<Option<CommandChild>>,
    status: Mutex<SidecarStatus>,
    generation: AtomicU64,
    close_notice_sent: AtomicBool,
}

impl OwnedSidecar {
    fn new() -> Self {
        Self {
            child: Mutex::new(None),
            status: Mutex::new(SidecarStatus {
                state: "starting".to_string(),
                port: DEFAULT_SIDECAR_PORT,
                pid: None,
                restart_count: 0,
                last_error: None,
            }),
            generation: AtomicU64::new(0),
            close_notice_sent: AtomicBool::new(false),
        }
    }
}

fn configured_sidecar_port() -> Result<u16, String> {
    match std::env::var("AIMA_SIDECAR_PORT") {
        Ok(value) => {
            let port = value
                .parse::<u16>()
                .map_err(|_| "AIMA_SIDECAR_PORT must be an integer".to_string())?;
            if !(1024..=65_535).contains(&port) {
                return Err("AIMA_SIDECAR_PORT must be between 1024 and 65535".to_string());
            }
            Ok(port)
        }
        Err(std::env::VarError::NotPresent) => Ok(DEFAULT_SIDECAR_PORT),
        Err(std::env::VarError::NotUnicode(_)) => Err("AIMA_SIDECAR_PORT is not valid Unicode".to_string()),
    }
}

fn sidecar_is_healthy(port: u16) -> bool {
    let Ok(mut stream) = TcpStream::connect_timeout(
        &std::net::SocketAddr::from(([127, 0, 0, 1], port)),
        Duration::from_millis(800),
    ) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let request = b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n";
    if stream.write_all(request).is_err() {
        return false;
    }
    let mut response = String::new();
    if stream.read_to_string(&mut response).is_err() {
        return false;
    }
    response.starts_with("HTTP/1.1 200") || response.starts_with("HTTP/1.0 200")
}

fn http_json(port: u16, method: &str, path: &str) -> Option<serde_json::Value> {
    let mut stream = TcpStream::connect_timeout(
        &std::net::SocketAddr::from(([127, 0, 0, 1], port)),
        Duration::from_millis(800),
    )
    .ok()?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let request = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
    );
    stream.write_all(request.as_bytes()).ok()?;
    let mut response = String::new();
    stream.read_to_string(&mut response).ok()?;
    let body = response.split("\r\n\r\n").nth(1)?;
    serde_json::from_str(body).ok()
}

fn wait_for_sidecar(port: u16) -> Result<(), String> {
    for _ in 0..120 {
        if sidecar_is_healthy(port) {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(250));
    }
    Err("owned backend sidecar did not become ready".to_string())
}

fn owned_backend_ready(app: &AppHandle) -> bool {
    sidecar_status(app)
        .map(|status| status.state == "ready")
        .unwrap_or(false)
}

fn close_to_tray_enabled(app: &AppHandle, port: u16) -> bool {
    if !owned_backend_ready(app) {
        return false;
    }
    http_json(port, "GET", "/settings/desktop.close_to_tray")
        .and_then(|payload| payload.get("value").and_then(serde_json::Value::as_bool))
        .unwrap_or(false)
}

fn monitoring_runtime_active(app: &AppHandle, port: u16) -> Option<bool> {
    if !owned_backend_ready(app) {
        return None;
    }
    http_json(port, "GET", "/monitoring/status")
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
        current.pid = pid;
        current.last_error = error.map(|value| value.chars().take(240).collect());
        current.clone()
    } else {
        return;
    };
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

fn stop_owned_sidecar(app: &AppHandle) {
    let Some(state) = app.try_state::<OwnedSidecar>() else {
        return;
    };
    state.generation.fetch_add(1, Ordering::SeqCst);
    let process = state.child.lock().ok().and_then(|mut child| child.take());
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
        let port = sidecar_status(app).map(|item| item.port).unwrap_or(DEFAULT_SIDECAR_PORT);
        emit_backend_status(app, "stopped", port, None, None);
    }
}

fn start_owned_sidecar(app: &AppHandle, port: u16) -> Result<(), String> {
    if TcpListener::bind(("127.0.0.1", port)).is_err() {
        let message = format!("local backend port {port} is already in use; no foreign process was touched");
        emit_backend_status(app, "degraded", port, None, Some(message.clone()));
        return Err(message);
    }
    let state = app
        .try_state::<OwnedSidecar>()
        .ok_or_else(|| "owned sidecar state is unavailable".to_string())?;
    let generation = state.generation.fetch_add(1, Ordering::SeqCst) + 1;
    let restart_count = if let Ok(mut status) = state.status.lock() {
        status.restart_count = status.restart_count.saturating_add(1);
        status.state = "starting".to_string();
        status.port = port;
        status.pid = None;
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
        .args(["--host", "127.0.0.1", "--port", &port.to_string()])
        .env("AIMA_SIDECAR_PORT", port.to_string())
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
    emit_backend_status(app, "starting", port, Some(pid), None);

    if let Err(error) = wait_for_sidecar(port) {
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
            if sidecar_is_healthy(port) {
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

fn set_monitoring_tray_text<R: tauri::Runtime>(app: &AppHandle, status_item: &MenuItem<R>, toggle_item: &MenuItem<R>, port: u16) {
    if !owned_backend_ready(app) {
        let _ = status_item.set_text("Monitoring: DEGRADED · backend unavailable");
        let _ = toggle_item.set_text("Resume monitoring");
        return;
    }
    let Some(payload) = http_json(port, "GET", "/monitoring/status") else {
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
    let port = configured_sidecar_port()?;
    stop_owned_sidecar(&app);
    match start_owned_sidecar(&app, port) {
        Ok(()) => sidecar_status(&app).ok_or_else(|| "backend status is unavailable after restart".to_string()),
        Err(error) => {
            emit_backend_status(&app, "degraded", port, None, Some(error.clone()));
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
            app.handle().plugin(tauri_plugin_autostart::init(
                tauri_plugin_autostart::MacosLauncher::LaunchAgent,
                None,
            ))?;

            let port = configured_sidecar_port().unwrap_or(DEFAULT_SIDECAR_PORT);
            if let Err(error) = start_owned_sidecar(&app.handle(), port) {
                emit_backend_status(&app.handle(), "degraded", port, None, Some(error));
            }
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.eval(&format!(
                    "window.__AIMA_API_BASE_URL__ = 'http://127.0.0.1:{port}';"
                ));
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
                set_monitoring_tray_text(&tray_app, &tray_status, &tray_toggle, port);
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
                        let active = monitoring_runtime_active(app, port).unwrap_or(false);
                        let path = if active { "/monitoring/pause" } else { "/monitoring/resume" };
                        if !owned_backend_ready(app) || http_json(port, "POST", path).is_none() {
                            let _ = event_status.set_text("Monitoring: DEGRADED · action failed");
                            let _ = event_toggle.set_text("Resume monitoring");
                        } else {
                            set_monitoring_tray_text(app, &event_status, &event_toggle, port);
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
            set_monitoring_tray_text(&app.handle(), &monitoring_status, &monitoring_toggle, port);
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let port = configured_sidecar_port().unwrap_or(DEFAULT_SIDECAR_PORT);
                let active = monitoring_runtime_active(&window.app_handle(), port);
                let owned_backend_alive = sidecar_status(&window.app_handle())
                    .map(|status| status.state == "ready" || status.state == "starting")
                    .unwrap_or(false);
                if active == Some(true) || (active.is_none() && owned_backend_alive) {
                    api.prevent_close();
                    let _ = window.hide();
                    notify_monitoring_continues(&window.app_handle());
                } else if close_to_tray_enabled(&window.app_handle(), port) {
                    api.prevent_close();
                    let _ = window.hide();
                } else {
                    stop_owned_sidecar(&window.app_handle());
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running AI Market Analyst desktop");
}
