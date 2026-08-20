import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ApplicationShell } from "../App";
import { createFakeClient, fakeProviderHealth } from "../test/fakeClient";

function renderSettings() {
  const providerHealth = vi
    .fn()
    .mockRejectedValueOnce(new Error("sentinel provider failure"))
    .mockResolvedValue(fakeProviderHealth);
  const client = createFakeClient({
    providerHealth,
    modelHealth: vi.fn().mockResolvedValue({ provider: "ollama", available: false, error_code: "MODEL_NOT_CONFIGURED" }),
  });

  render(
    <MemoryRouter initialEntries={["/settings"]}>
      <ApplicationShell apiClient={client} />
    </MemoryRouter>,
  );
  return { client, providerHealth };
}

describe("SettingsHealthPage", () => {
  it("separates backend, routing and model states and retries routing without write controls", async () => {
    const { providerHealth } = renderSettings();

    const backend = screen.getByRole("region", { name: "Backend service" });
    expect(await within(backend).findByText("0.5.0")).toBeInTheDocument();
    expect(within(backend).getAllByText("false")).toHaveLength(2);

    const provider = screen.getByRole("region", { name: "Market and news routing" });
    expect(await within(provider).findByRole("alert")).toHaveTextContent("This panel is unavailable");
    expect(within(provider).queryByText("sentinel provider failure")).not.toBeInTheDocument();

    const model = screen.getByRole("region", { name: "Local model" });
    expect(await within(model).findByText(/Model health is degraded or unavailable/)).toBeInTheDocument();
    expect(within(model).getByText("MODEL_NOT_CONFIGURED")).toBeInTheDocument();
    expect(screen.getByText(/No secrets, broker connections, real orders or external notifiers are exposed here; alerts are local observability/)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Alert reconciliation" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Local Watchlist scheduler" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enable scheduler" })).toBeInTheDocument();

    fireEvent.click(within(provider).getByRole("button", { name: "Retry Market and news routing" }));
    expect(await within(provider).findByText("fixture_news")).toBeInTheDocument();
    expect(providerHealth).toHaveBeenCalledTimes(2);
    expect(within(provider).getByText("deferred_until_symbol_request")).toBeInTheDocument();
  });
});
