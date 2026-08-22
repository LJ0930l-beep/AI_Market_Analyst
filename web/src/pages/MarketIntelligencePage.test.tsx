import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { I18nProvider } from "../i18n";
import { createFakeClient } from "../test/fakeClient";
import { MarketIntelligencePage } from "./MarketIntelligencePage";

function renderSurface(surface: "markets" | "calendar" | "news" | "heatmap", client = createFakeClient()) {
  render(<MemoryRouter><I18nProvider><MarketIntelligencePage apiClient={client} surface={surface} /></I18nProvider></MemoryRouter>);
}

describe("MarketIntelligencePage", () => {
  it("renders saved market and calendar evidence with localized Intl time", async () => {
    renderSurface("calendar");
    expect(await screen.findByRole("heading", { name: "Economic Calendar" })).toBeInTheDocument();
    expect(screen.getByText("Saved macro event")).toBeInTheDocument();
    expect(screen.getByText(/fixture/)).toBeInTheDocument();
    expect(screen.getByText(/GET read-only/)).toBeInTheDocument();
  });

  it("keeps heatmap missing evidence visible and offers keyboard controls", async () => {
    renderSurface("heatmap");
    expect(await screen.findByRole("heading", { name: "Market Heatmap" })).toBeInTheDocument();
    expect(screen.getByText("NVDA")).toBeInTheDocument();
    expect(screen.getByText("AAPL").closest("article")).toHaveTextContent("Unavailable");
    expect(screen.getByRole("button", { name: "equity" })).toBeVisible();
  });

  it("isolates read failure and never calls analysis", async () => {
    const client = createFakeClient({ marketIntelligence: vi.fn().mockRejectedValue(new Error("read failed")) });
    renderSurface("news", client);
    expect(await screen.findByRole("alert")).toHaveTextContent("read failed");
    expect(client.analysis).not.toHaveBeenCalled();
  });
});
