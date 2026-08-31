/** Small, fixed-scope bridge for desktop-owned lifecycle operations. */

export interface DesktopBackendStatus {
  state: "starting" | "ready" | "degraded" | "stopped" | string;
  port: number;
  pid: number | null;
  restart_count: number;
  last_error: string | null;
}

export interface DesktopMonitoringNotice {
  active: boolean;
  route: "/monitoring";
}

export function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export async function readAutostartState(): Promise<boolean | null> {
  if (!isTauriRuntime()) return null;
  try {
    const { isEnabled } = await import("@tauri-apps/plugin-autostart");
    return await isEnabled();
  } catch {
    return null;
  }
}

export async function setAutostartState(enabled: boolean): Promise<boolean | null> {
  if (!isTauriRuntime()) return null;
  try {
    const { enable, disable, isEnabled } = await import("@tauri-apps/plugin-autostart");
    if (enabled) await enable();
    else await disable();
    return await isEnabled();
  } catch {
    return null;
  }
}

export async function restartOwnedBackend(): Promise<DesktopBackendStatus | null> {
  if (!isTauriRuntime()) return null;
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<DesktopBackendStatus>("restart_backend");
}

export async function readDesktopBackendStatus(): Promise<DesktopBackendStatus | null> {
  if (!isTauriRuntime()) return null;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    return await invoke<DesktopBackendStatus>("backend_status");
  } catch {
    return null;
  }
}

export async function registerDesktopBackendState(
  onState: (status: DesktopBackendStatus) => void,
): Promise<() => void> {
  if (!isTauriRuntime()) return () => undefined;
  try {
    const { listen } = await import("@tauri-apps/api/event");
    const unregister = await listen<DesktopBackendStatus>("aima://backend-state", (event) => onState(event.payload));
    // setup() can emit the first degraded event before React has mounted. Read
    // the current desktop-owned state after installing the listener so startup
    // failures and occupied ports are still visible in the WebView.
    const current = await readDesktopBackendStatus();
    if (current) onState(current);
    return unregister;
  } catch {
    return () => undefined;
  }
}

export async function registerDesktopMonitoringNotice(
  onNotice: (notice: DesktopMonitoringNotice) => void,
): Promise<() => void> {
  if (!isTauriRuntime()) return () => undefined;
  try {
    const { listen } = await import("@tauri-apps/api/event");
    return await listen<DesktopMonitoringNotice>("aima://monitoring-close-to-tray", (event) => {
      if (event.payload?.active === true && event.payload.route === "/monitoring") onNotice(event.payload);
    });
  } catch {
    return () => undefined;
  }
}
