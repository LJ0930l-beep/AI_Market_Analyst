import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { I18nProvider } from "../i18n";
import { MonitoringPage } from "./MonitoringPage";
import { createFakeClient } from "../test/fakeClient";

function renderMonitoring(client = createFakeClient()) {
  render(
    <MemoryRouter initialEntries={["/monitoring"]}>
      <I18nProvider>
        <MonitoringPage apiClient={client} />
      </I18nProvider>
    </MemoryRouter>,
  );
  return client;
}

describe("MonitoringPage", () => {
  it("keeps every policy disabled until the user opts in and exposes the run action", async () => {
    const updateMonitoringPolicy = vi.fn().mockResolvedValue(undefined);
    const runMonitoring = vi.fn().mockResolvedValue({ status: "NO_NEW_CLOSED_BAR", items: [] });
    const client = renderMonitoring(createFakeClient({ updateMonitoringPolicy, runMonitoring }));

    await screen.findByRole("heading", { name: "Smart monitoring" });
    await waitFor(() => expect(screen.getAllByText("Loaded").length).toBeGreaterThan(0));
    const cards = screen.getAllByRole("article");
    expect(cards).toHaveLength(3);
    expect(screen.getAllByRole("checkbox").filter((input) => !(input as HTMLInputElement).checked)).toHaveLength(3);

    const btcCard = cards[0];
    expect(btcCard).not.toBeNull();
    const enabledToggle = within(btcCard as HTMLElement).getAllByRole("checkbox")[0];
    fireEvent.click(enabledToggle);
    await waitFor(() => expect(enabledToggle).toBeChecked());
    fireEvent.click(within(btcCard as HTMLElement).getByRole("button", { name: "Save policy" }));
    await waitFor(() => expect(updateMonitoringPolicy).toHaveBeenCalledWith(expect.objectContaining({ instrument_id: "BTCUSDT", enabled: true })));

    fireEvent.click(screen.getByRole("button", { name: "Run monitoring now" }));
    await waitFor(() => expect(runMonitoring).toHaveBeenCalledWith(["BTCUSDT", "ETHUSDT", "SOLUSDT"]));
    expect(client.monitoringOpportunities).toHaveBeenCalled();
  });
});
