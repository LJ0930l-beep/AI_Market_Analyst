import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApplicationShell } from "../App";
import { createFakeClient, fakeProviderHealth } from "../test/fakeClient";

function renderSettings() {
  const providerHealth = vi
    .fn()
    .mockRejectedValueOnce(new Error("sentinel provider failure"))
    .mockResolvedValue(fakeProviderHealth);
  const client = createFakeClient({
    providerHealth,
    modelHealth: vi.fn().mockResolvedValue({
      provider: "ollama",
      available: false,
      error_code: "MODEL_NOT_CONFIGURED",
      consult: {
        available: false,
        model_id: "qwen3.5:4b",
        contract_version: "qwen_consult_v2",
        conversation_storage: "browser_session_only",
        database_writes: false,
      },
    }),
  });

  render(
    <MemoryRouter initialEntries={["/settings"]}>
      <ApplicationShell apiClient={client} />
    </MemoryRouter>,
  );
  return { client, providerHealth };
}

describe("SettingsHealthPage", () => {
  afterEach(() => window.localStorage.clear());
  it("separates backend, routing and model states and retries routing without write controls", async () => {
    const { providerHealth } = renderSettings();

    const backend = await screen.findByRole("region", { name: "Backend service" });
    expect(await within(backend).findByText("1.2.0")).toBeInTheDocument();
    expect(within(backend).getAllByText("No")).toHaveLength(2);
    expect(screen.getByRole("region", { name: "Release / local capability" })).toHaveTextContent("Phase 7 · API 1.2.0");

    const provider = screen.getByRole("region", { name: "Market and news routing" });
    expect(await within(provider).findByRole("alert")).toHaveTextContent("This panel is unavailable");
    expect(within(provider).queryByText("sentinel provider failure")).not.toBeInTheDocument();

    const model = screen.getByRole("region", { name: "Local model" });
    expect(await within(model).findByText(/Model health is degraded or unavailable/)).toBeInTheDocument();
    expect(within(model).getByText("MODEL_NOT_CONFIGURED")).toBeInTheDocument();
    expect(within(model).getByText("qwen_consult_v2")).toBeInTheDocument();
    expect(within(model).getByText("disabled (read-only consultation)")).toBeInTheDocument();
    expect(screen.getByText(/No secrets, broker connections, real orders or cloud notifiers are exposed here; alerts stay local/)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Alert reconciliation" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Local Watchlist scheduler" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enable scheduler" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Phase 6 context capabilities" })).toHaveTextContent("market_memory_v1");
    fireEvent.click(screen.getByRole("button", { name: "Send test Windows notification" }));
    expect(await screen.findByText("Windows notification is unavailable in this runtime or permission is denied.")).toBeInTheDocument();

    fireEvent.click(within(provider).getByRole("button", { name: "Retry Market and news routing" }));
    expect(await within(provider).findByText("fixture_news")).toBeInTheDocument();
    expect(providerHealth).toHaveBeenCalledTimes(2);
    expect(within(provider).getByText("deferred_until_symbol_request")).toBeInTheDocument();
  });

  it("persists V1.1 language and deterministic model preferences through typed settings", async () => {
    const { client } = renderSettings();
    await screen.findByRole("heading", { name: "Language · AI" });
    fireEvent.change(screen.getByLabelText("AI response language"), { target: { value: "zh-CN" } });
    fireEvent.change(screen.getByLabelText("Local alert language"), { target: { value: "zh-CN" } });
    fireEvent.change(screen.getByLabelText("Model preference"), { target: { value: "smart" } });
    fireEvent.click(screen.getByRole("button", { name: "Save preferences" }));
    expect(await screen.findByText("Preferences saved")).toBeInTheDocument();
    expect(client.updateAppSetting).toHaveBeenCalledWith("ai.response_language", "zh-CN");
    expect(client.updateAppSetting).toHaveBeenCalledWith("notifications.language", "zh-CN");
    expect(client.updateAppSetting).toHaveBeenCalledWith("ai.model_preference", "smart");
  });

  it("renders nested operational, scheduler and count facts in zh-CN without fixed English leakage", async () => {
    window.localStorage.setItem("ai-market-analyst.language", "zh-CN");
    const client = createFakeClient({ providerHealth: vi.fn().mockResolvedValue(fakeProviderHealth) });
    render(
      <MemoryRouter initialEntries={["/settings"]}>
        <ApplicationShell apiClient={client} />
      </MemoryRouter>,
    );

    const backend = await screen.findByRole("region", { name: "后端服务" });
    expect(within(backend).getByText("状态")).toBeInTheDocument();
    expect(within(backend).getByText("API 版本")).toBeInTheDocument();
    expect(within(backend).getAllByText("否")).toHaveLength(2);
    const routing = screen.getByRole("region", { name: "市场与新闻路由" });
    expect(await within(routing).findByText("提供方路由报告可用。")).toBeInTheDocument();
    expect(within(routing).getByText("市场路由")).toBeInTheDocument();
    expect(within(routing).getByText("新闻路由")).toBeInTheDocument();
    const scheduler = screen.getByRole("region", { name: "本地关注列表调度器" });
    expect(within(scheduler).getByText("生命周期")).toBeInTheDocument();
    expect(within(scheduler).getByText("资源")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "数据库计数" })).toHaveTextContent("预测");
    for (const leaked of ["Status", "API Version", "Real Orders", "MARKET ROUTES", "Lifecycle", "Resource", "Performance Refresh"]) {
      expect(screen.queryByText(leaked, { exact: true })).not.toBeInTheDocument();
    }
  });
});
