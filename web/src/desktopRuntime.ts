/** Small, fixed-scope bridge for desktop-owned lifecycle operations. */

declare global {
  interface Window {
    __AIMA_API_BASE_URL__?: string;
  }
}

export interface DesktopBackendStatus {
  state: "starting" | "ready" | "degraded" | "stopped" | string;
  port: number;
  base_url: string;
  pid: number | null;
  instance_id: string | null;
  contract_version: string;
  ownership_verified: boolean;
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

export function applyDesktopBackendStatus(status: DesktopBackendStatus): DesktopBackendStatus {
  if (typeof window !== "undefined") {
    window.__AIMA_API_BASE_URL__ = status.base_url || undefined;
  }
  return status;
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
    else {
      try {
        await disable();
      } catch {
        // The Windows backend can report a missing registry value as an
        // error.  Disabling an already-absent entry is successful, but a
        // value that remains enabled must still be surfaced to the caller.
        return await isEnabled();
      }
    }
    return await isEnabled();
  } catch {
    return null;
  }
}

export async function restartOwnedBackend(): Promise<DesktopBackendStatus | null> {
  if (!isTauriRuntime()) return null;
  const { invoke } = await import("@tauri-apps/api/core");
  return applyDesktopBackendStatus(await invoke<DesktopBackendStatus>("restart_backend"));
}

export async function readDesktopBackendStatus(): Promise<DesktopBackendStatus | null> {
  if (!isTauriRuntime()) return null;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    return applyDesktopBackendStatus(await invoke<DesktopBackendStatus>("backend_status"));
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
    const unregister = await listen<DesktopBackendStatus>("aima://backend-state", (event) => onState(applyDesktopBackendStatus(event.payload)));
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
