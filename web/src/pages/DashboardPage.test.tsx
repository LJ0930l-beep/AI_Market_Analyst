import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { I18nProvider } from "../i18n";
import { createFakeClient, fakeMarketIntelligence } from "../test/fakeClient";
import { DashboardPage } from "./DashboardPage";

function renderPage(client = createFakeClient(), provenance = vi.fn()) {
  render(<MemoryRouter><I18nProvider><DashboardPage apiClient={client} onProvenanceChange={provenance} /></I18nProvider></MemoryRouter>);
  return provenance;
}

describe("DashboardPage V1.1", () => {
  it("renders the dense read-only evidence deck with honest unavailable pulse rows", async () => {
    const client = createFakeClient();
    const provenance = renderPage(client);
    expect(await screen.findByRole("heading", { name: "Market intelligence, without the noise" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Market Pulse" })).toBeInTheDocument();
    expect(screen.getByText("NASDAQ").closest("article")).toHaveTextContent("Unavailable");
    expect(screen.getByRole("heading", { name: "Today's Events" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "AI Watchlist" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Latest AI Signal" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Market Heatmap" })).toBeInTheDocument();
    expect(client.marketIntelligence).toHaveBeenCalledTimes(1);
    expect(client.analysis).not.toHaveBeenCalled();
    expect(client.followPrediction).not.toHaveBeenCalled();
    await waitFor(() => expect(provenance).toHaveBeenCalledWith(expect.objectContaining({ model: "qwen3.5:4b", state: "active" })));
  });

  it("generates Daily Brief only after the explicit action and displays its audited route", async () => {
    const client = createFakeClient();
    renderPage(client);
    const brief = await screen.findByRole("heading", { name: "AI Daily Brief" });
    const region = brief.closest("section");
    if (!region) throw new Error("brief region missing");
    expect(within(region).getByText(/Saved evidence is constructive/)).toBeInTheDocument();
    fireEvent.click(within(region).getByRole("button", { name: "Generate brief" }));
    await waitFor(() => expect(client.generateDailyBrief).toHaveBeenCalledWith("en", "auto"));
    expect(client.predictions).not.toHaveBeenCalled();
    expect(client.paperTrades).not.toHaveBeenCalled();
  });

  it("renders the premium dashboard explicitly in Chinese", async () => {
    window.localStorage.setItem("ai-market-analyst.language", "zh-CN");
    renderPage();
    expect(await screen.findByRole("heading", { name: "看清市场，不被噪音淹没" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "市场脉搏" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "今日事件" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "AI 每日简报" })).toBeInTheDocument();
    window.localStorage.clear();
  });

  it("isolates intelligence failure without inventing fallback market data", async () => {
    const client = createFakeClient({ marketIntelligence: vi.fn().mockRejectedValue(new Error("offline")) });
    renderPage(client);
    expect(await screen.findByRole("alert")).toHaveTextContent("offline");
    expect(screen.queryByText("66842.1")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(client.marketIntelligence).toHaveBeenCalledTimes(2);
  });

  it("keeps an explicit empty capability state instead of populating fixture values", async () => {
    const empty = { ...fakeMarketIntelligence, pulse: fakeMarketIntelligence.pulse.map((item) => ({ ...item, price: null, change_pct: null, status: "unavailable" })), calendar: { ...fakeMarketIntelligence.calendar, status: "unavailable", events: [] }, watchlist: [], latest_signal: null, news: { status: "unavailable", items: [], missing_reasons: ["none"] }, daily_brief: null };
    renderPage(createFakeClient({ marketIntelligence: vi.fn().mockResolvedValue(empty) }));
    expect(await screen.findByText("No point-in-time event evidence is stored.")).toBeInTheDocument();
    expect(screen.getByText("No saved brief. Generate one explicitly when Qwen is available.")).toBeInTheDocument();
    expect(screen.getAllByText("Unavailable").length).toBeGreaterThan(1);
  });
});
