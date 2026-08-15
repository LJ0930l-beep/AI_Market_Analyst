import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ApplicationShell } from "../App";
import { createFakeClient, fakePrediction } from "../test/fakeClient";

function renderDashboard(client = createFakeClient()) {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <ApplicationShell apiClient={client} />
    </MemoryRouter>,
  );
}

function panel(name: string): HTMLElement {
  return screen.getByRole("region", { name });
}

describe("DashboardPage", () => {
  it("renders actual health, routing, counts, instruments, predictions, performance and provenance", async () => {
    const waitPrediction = {
      ...fakePrediction,
      prediction_id: "prediction-wait",
      action: "WAIT" as const,
      generated_at: "2030-01-02T11:00:00Z",
      signal_valid_until: undefined,
      raw_confidence: undefined,
      calibrated_confidence: undefined,
    };
    renderDashboard(createFakeClient({ predictions: vi.fn().mockResolvedValue([fakePrediction, waitPrediction]) }));

    expect(await within(panel("Recent predictions")).findByText("LONG · actionable")).toBeInTheDocument();
    expect(within(panel("Recent predictions")).getByText("WAIT · coverage only")).toBeInTheDocument();
    expect(within(panel("Recent predictions")).getByText("Raw 0.72 · Calibrated 0.64")).toBeInTheDocument();
    expect(within(panel("Recent predictions")).getAllByText("live")).toHaveLength(2);
    expect(within(panel("Recent predictions")).getAllByText("1h")).toHaveLength(2);
    expect(within(panel("Recent predictions")).getByText(/Valid until 2030-01-02T13:00:00.000Z · Active/)).toBeInTheDocument();

    expect(within(panel("Provider routing")).getByText("fixture_news")).toBeInTheDocument();
    expect(within(panel("Provider routing")).getByText("deferred_until_symbol_request")).toBeInTheDocument();
    expect(within(panel("Model health")).getByText("ollama")).toBeInTheDocument();
    expect(within(panel("Stored counts")).getByText("unknown counter")).toBeInTheDocument();
    expect(within(panel("Instrument roster")).getByRole("link", { name: /NVDA/ })).toHaveAttribute(
      "href",
      "/assets/NVDA",
    );

    const performance = panel("Live performance");
    expect(within(performance).getByText("Returned scope: live")).toBeInTheDocument();
    expect(within(performance).getByText("Resolved actionable sample: 2")).toBeInTheDocument();
    expect(within(performance).getByText("hit rate")).toBeInTheDocument();
    expect(within(performance).getByText("0.5")).toBeInTheDocument();
    expect(within(performance).getByText("zero metric")).toBeInTheDocument();

    const rail = screen.getByRole("heading", { name: "Signal validity rail" }).closest("section");
    expect(rail).not.toBeNull();
    expect(await within(rail as HTMLElement).findByRole("listitem", { name: /Generated: 2030-01-02T12:00:00.000Z/ })).toBeInTheDocument();
    expect(within(rail as HTMLElement).getByRole("listitem", { name: "Re-evaluate: Not set" })).toBeInTheDocument();
    expect(within(rail as HTMLElement).getByRole("listitem", { name: /Expiry: 2030-01-02T13:00:00.000Z/ })).toBeInTheDocument();
    expect(within(rail as HTMLElement).getByText("Signal window supplied")).toBeInTheDocument();
    expect(within(rail as HTMLElement).getByText("source_type: live")).toBeInTheDocument();
    expect(within(rail as HTMLElement).getByText("model_id: qwen3.5:4b")).toBeInTheDocument();
  });

  it("keeps usable panels visible when provider routing fails", async () => {
    const client = createFakeClient({ providerHealth: vi.fn().mockRejectedValue(new Error("sentinel provider failure")) });
    renderDashboard(client);

    const provider = panel("Provider routing");
    expect(await within(provider).findByRole("alert")).toHaveTextContent("This panel is unavailable");
    expect(within(provider).queryByText("sentinel provider failure")).not.toBeInTheDocument();
    expect(within(panel("Stored counts")).getByText("unknown counter")).toBeInTheDocument();
    expect(within(panel("Instrument roster")).getByRole("link", { name: /NVDA/ })).toBeInTheDocument();
    expect(within(panel("Recent predictions")).getByText("LONG · actionable")).toBeInTheDocument();
  });

  it("shows empty prediction and degraded model states without inventing performance samples", async () => {
    const client = createFakeClient({
      modelHealth: vi.fn().mockResolvedValue({ provider: "ollama", available: false, error_code: "MODEL_NOT_CONFIGURED" }),
      predictions: vi.fn().mockResolvedValue([]),
      performanceSummary: vi.fn().mockResolvedValue({
        scope: { source_type: "live" },
        sample_count: 0,
        actionable_count: 0,
        metrics: { hit_rate: 0.42 },
      }),
    });
    renderDashboard(client);

    expect(await within(panel("Model health")).findByText(/model is unavailable or not configured/i)).toBeInTheDocument();
    expect(within(panel("Model health")).getByText("MODEL_NOT_CONFIGURED")).toBeInTheDocument();
    expect(await within(panel("Recent predictions")).findByText("No prediction records were returned for this recent ledger.")).toBeInTheDocument();

    const performance = panel("Live performance");
    expect(within(performance).getByText(/Resolved sample size was not supplied/)).toBeInTheDocument();
    expect(within(performance).getByText("0.42")).toBeInTheDocument();
    expect(within(performance).getByText("Degraded")).toBeInTheDocument();
    expect(within(performance).queryByText("Resolved actionable sample: 0")).not.toBeInTheDocument();

    const rail = screen.getByRole("heading", { name: "Signal validity rail" }).closest("section");
    expect(within(rail as HTMLElement).getByText("Neutral placeholder")).toBeInTheDocument();
    expect(within(rail as HTMLElement).getByText("No recent prediction with usable provenance was returned.")).toBeInTheDocument();
  });

  it("selects the newest actual prediction and derives an expired rail state from its expiry", async () => {
    const newerExpired = {
      ...fakePrediction,
      prediction_id: "prediction-expired",
      generated_at: "2030-01-02T14:00:00Z",
      signal_valid_until: "2020-01-02T13:00:00Z",
      action: "SHORT" as const,
    };
    const client = createFakeClient({ predictions: vi.fn().mockResolvedValue([fakePrediction, newerExpired]) });
    renderDashboard(client);

    const predictions = panel("Recent predictions");
    expect(await within(predictions).findByText("SHORT · actionable")).toBeInTheDocument();
    const rail = screen.getByRole("heading", { name: "Signal validity rail" }).closest("section");
    expect(within(rail as HTMLElement).getByText("Signal window expired")).toBeInTheDocument();
    expect(await within(rail as HTMLElement).findByRole("listitem", { name: /Generated: 2030-01-02T14:00:00.000Z/ })).toBeInTheDocument();
    expect(within(rail as HTMLElement).getByRole("listitem", { name: /Expiry: 2020-01-02T13:00:00.000Z/ })).toBeInTheDocument();
    expect(within(rail as HTMLElement).getByText(/Prediction prediction-expired selected/)).toBeInTheDocument();
  });

  it("treats selected predictions without resolved actionable outcomes as degraded metadata", async () => {
    const client = createFakeClient({
      performanceSummary: vi.fn().mockResolvedValue({
        scope: { source_type: "live" },
        sample_count: 5,
        actionable_count: 2,
        metrics: { resolved_actionable: 0, hit_rate: 0, pending_count: 5 },
      }),
    });
    renderDashboard(client);

    const performance = panel("Live performance");
    expect(await within(performance).findByText("Degraded")).toBeInTheDocument();
    expect(within(performance).getByText("No resolved sample is reported. Returned metrics are shown as metadata only.")).toBeInTheDocument();
    expect(within(performance).getByText("Resolved actionable sample: 0")).toBeInTheDocument();
    expect(within(performance).getByText("pending count")).toBeInTheDocument();
    expect(within(performance).queryByText("Resolved sample: 5")).not.toBeInTheDocument();
  });
});
