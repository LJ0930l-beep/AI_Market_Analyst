import { afterEach, describe, expect, it, vi } from "vitest";

import { registerMonitoringAlertStream, safeNotificationRoute } from "./desktopNotifications";

class FakeEventSource {
  static latest: FakeEventSource | undefined;
  readonly url: string;
  closed = false;
  private readonly listeners = new Map<string, EventListener>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.latest = this;
  }

  addEventListener(type: string, listener: EventListener): void {
    this.listeners.set(type, listener);
  }

  removeEventListener(type: string): void {
    this.listeners.delete(type);
  }

  close(): void {
    this.closed = true;
  }

  emit(type: string, data: unknown): void {
    this.listeners.get(type)?.(new MessageEvent(type, { data: JSON.stringify(data) }));
  }
}

const eventSourceSlot = globalThis as unknown as { EventSource?: typeof FakeEventSource };
const originalEventSource = eventSourceSlot.EventSource;

afterEach(() => {
  eventSourceSlot.EventSource = originalEventSource;
  FakeEventSource.latest = undefined;
});

describe("desktop notification routing", () => {
  it("accepts only local product routes", () => {
    expect(safeNotificationRoute("/alerts")).toBe("/alerts");
    expect(safeNotificationRoute("/assets/BTCUSDT")).toBe("/assets/BTCUSDT");
    expect(safeNotificationRoute("https://evil.example/steal")).toBeUndefined();
    expect(safeNotificationRoute("/assets/../../settings")).toBeUndefined();
    expect(safeNotificationRoute("//evil.example")).toBeUndefined();
  });

  it("bridges sidecar alert events while deduping repeated event ids", () => {
    eventSourceSlot.EventSource = FakeEventSource;
    const onAlert = vi.fn();
    const cleanup = registerMonitoringAlertStream(onAlert, "http://127.0.0.1:18765");
    const source = FakeEventSource.latest;
    expect(source?.url).toBe("http://127.0.0.1:18765/monitoring/events");
    const event = {
      type: "alert.created",
      event_id: "monitoring-alert:one",
      alert_id: "one",
      alert: { title: "BTC watch", message: "closed bar", symbol: "BTCUSDT" },
      route: "/assets/BTCUSDT",
      notification: { in_app: true, native: true },
    };
    source?.emit("alert.created", event);
    source?.emit("alert.created", event);
    source?.emit("alert.created", { ...event, route: "https://evil.example" });
    expect(onAlert).toHaveBeenCalledTimes(1);
    expect(onAlert).toHaveBeenCalledWith(expect.objectContaining({ event_id: event.event_id, route: "/assets/BTCUSDT" }));
    cleanup();
    expect(source?.closed).toBe(true);
  });
});
