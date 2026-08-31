/** Local notification boundary for both Tauri and sidecar background events. */

import { getMonitoringEventsUrl } from "./api/client";

export interface DesktopAlert {
  title: string;
  body: string;
  route: string;
}

export interface MonitoringAlertEvent {
  event_id: string;
  alert_id: string;
  alert: {
    title: string;
    message: string;
    symbol?: string | null;
  };
  route: string;
  notification: {
    in_app: boolean;
    native: boolean;
  };
}

function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export function safeNotificationRoute(value: unknown): string | undefined {
  if (typeof value !== "string" || value.startsWith("//")) return undefined;
  if (value === "/alerts" || value === "/monitoring") return value;
  if (/^\/assets\/[A-Za-z0-9_-]+$/.test(value)) return value;
  return undefined;
}

export async function registerDesktopNotificationRouting(onRoute: (route: string) => void): Promise<() => void> {
  if (!isTauriRuntime()) return () => undefined;
  try {
    const { onAction } = await import("@tauri-apps/plugin-notification");
    const listener = await onAction((notification) => {
      const route = safeNotificationRoute(notification.extra?.route);
      if (route) onRoute(route);
    });
    return () => {
      void listener.unregister();
    };
  } catch {
    // A missing native permission/plugin is surfaced by the settings/runtime
    // boundary; a failed listener must not break normal chart navigation.
    return () => undefined;
  }
}

function parseMonitoringAlertEvent(value: unknown): MonitoringAlertEvent | undefined {
  if (typeof value !== "object" || value === null) return undefined;
  const record = value as Record<string, unknown>;
  const alert = typeof record.alert === "object" && record.alert !== null ? record.alert as Record<string, unknown> : undefined;
  const notification = typeof record.notification === "object" && record.notification !== null ? record.notification as Record<string, unknown> : undefined;
  const route = safeNotificationRoute(record.route);
  if (
    record.type !== "alert.created" ||
    typeof record.event_id !== "string" ||
    typeof record.alert_id !== "string" ||
    !alert || typeof alert.title !== "string" || typeof alert.message !== "string" ||
    !notification || typeof notification.in_app !== "boolean" || typeof notification.native !== "boolean" ||
    !route
  ) return undefined;
  return {
    event_id: record.event_id,
    alert_id: record.alert_id,
    alert: {
      title: alert.title,
      message: alert.message,
      symbol: typeof alert.symbol === "string" ? alert.symbol : null,
    },
    route,
    notification: { in_app: notification.in_app, native: notification.native },
  };
}

/**
 * Subscribe to the sidecar's always-on alert stream.  EventSource reconnects
 * itself, and the server emits only new durable alert ids, so the hidden
 * desktop shell does not duplicate page-triggered notifications.
 */
export function registerMonitoringAlertStream(
  onAlert: (event: MonitoringAlertEvent) => void,
  configuredBaseUrl?: string,
): () => void {
  if (typeof EventSource === "undefined") return () => undefined;
  let source: EventSource;
  try {
    source = new EventSource(getMonitoringEventsUrl(configuredBaseUrl));
  } catch {
    return () => undefined;
  }
  const seenEventIds = new Set<string>();
  const handler = (event: MessageEvent<string>) => {
    try {
      const parsed = parseMonitoringAlertEvent(JSON.parse(event.data) as unknown);
      if (!parsed || seenEventIds.has(parsed.event_id)) return;
      seenEventIds.add(parsed.event_id);
      if (seenEventIds.size > 256) {
        const oldest = seenEventIds.values().next().value;
        if (oldest) seenEventIds.delete(oldest);
      }
      onAlert(parsed);
    } catch {
      // Malformed sidecar events are ignored; the Alert Center remains the
      // durable source of truth and the stream must not break the shell.
    }
  };
  source.addEventListener("alert.created", handler as EventListener);
  return () => {
    source.removeEventListener("alert.created", handler as EventListener);
    source.close();
  };
}

export async function notifyDesktopAlert(alert: DesktopAlert): Promise<boolean> {
  if (!isTauriRuntime()) return false;
  try {
    const { isPermissionGranted, requestPermission, sendNotification } = await import("@tauri-apps/plugin-notification");
    let granted = await isPermissionGranted();
    if (!granted) granted = (await requestPermission()) === "granted";
    if (!granted) return false;
    await sendNotification({
      title: alert.title,
      body: alert.body,
      autoCancel: true,
      extra: { route: alert.route },
    });
    return true;
  } catch {
    // Notifications are optional local observability; no alert is retried as a
    // different channel and no external notifier is contacted.
    return false;
  }
}
