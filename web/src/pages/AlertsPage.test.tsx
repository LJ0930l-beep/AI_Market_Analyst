import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ApplicationShell } from "../App";
import { createFakeClient, fakeAlert, fakeAlertsResponse } from "../test/fakeClient";

describe("AlertsPage", () => {
  it("loads deterministic local evidence, filters it and acknowledges one alert", async () => {
    const acknowledgeAlert = vi.fn().mockResolvedValue({ ...fakeAlert, status: "ACKNOWLEDGED" });
    const alerts = vi.fn().mockResolvedValue(fakeAlertsResponse);
    const client = createFakeClient({ acknowledgeAlert, alerts });
    render(
      <MemoryRouter initialEntries={["/alerts"]}>
        <ApplicationShell apiClient={client} />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Alert Center" })).toBeInTheDocument();
    expect(screen.getAllByText("alert_policy_v1").length).toBeGreaterThan(0);
    const ledger = screen.getByRole("region", { name: "Alert ledger" });
    expect(within(ledger).getByText(fakeAlert.title)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "prediction" } });
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "OPEN" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => expect(alerts).toHaveBeenLastCalledWith(
      { limit: 50, offset: 0, source: "prediction", status: "OPEN" },
      expect.any(AbortSignal),
    ));

    fireEvent.click(within(ledger).getByRole("button", { name: "Acknowledge alert" }));
    await waitFor(() => expect(acknowledgeAlert).toHaveBeenCalledWith(fakeAlert.alert_id));
    expect(screen.getByText(/No Telegram, Discord, email, cloud notifier/)).toBeInTheDocument();
  });

  it("shows an empty state without exposing mutation controls", async () => {
    const client = createFakeClient({
      alerts: vi.fn().mockResolvedValue({ ...fakeAlertsResponse, alerts: [], counts: { total: 0, open: 0, unread: 0, acknowledged: 0 }, pagination: { limit: 50, offset: 0, total: 0 } }),
    });
    render(
      <MemoryRouter initialEntries={["/alerts"]}>
        <ApplicationShell apiClient={client} />
      </MemoryRouter>,
    );
    const ledger = await screen.findByRole("region", { name: "Alert ledger" });
    expect(within(ledger).getByText(/No alerts match the current filters/)).toBeInTheDocument();
    expect(within(ledger).queryByRole("button", { name: "Acknowledge alert" })).not.toBeInTheDocument();
  });
});
