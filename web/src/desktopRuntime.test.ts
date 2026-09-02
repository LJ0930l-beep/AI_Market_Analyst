import { afterEach, describe, expect, it, vi } from "vitest";

const autostart = vi.hoisted(() => ({
  disable: vi.fn(),
  enable: vi.fn(),
  isEnabled: vi.fn(),
}));

vi.mock("@tauri-apps/plugin-autostart", () => autostart);

import { setAutostartState } from "./desktopRuntime";

describe("desktop autostart adapter", () => {
  afterEach(() => {
    autostart.disable.mockReset();
    autostart.enable.mockReset();
    autostart.isEnabled.mockReset();
    Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
  });

  it("treats disabling an already-absent Windows entry as idempotent success", async () => {
    Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: {} });
    autostart.disable.mockRejectedValue(new Error("registry value not found"));
    autostart.isEnabled.mockResolvedValue(false);

    await expect(setAutostartState(false)).resolves.toBe(false);
    expect(autostart.isEnabled).toHaveBeenCalledOnce();
  });

  it("reports a failed disable when Windows still has autostart enabled", async () => {
    Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: {} });
    autostart.disable.mockRejectedValue(new Error("access denied"));
    autostart.isEnabled.mockResolvedValue(true);

    await expect(setAutostartState(false)).resolves.toBe(true);
  });
});
