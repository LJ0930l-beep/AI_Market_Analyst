import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ApplicationShell } from "../App";
import { ApiError, type ApplicationShellApiClient } from "../api/client";
import type { Instrument } from "../api/types";
import { createFakeClient, fakeInstrument } from "../test/fakeClient";

function renderWatchlist(instruments: ApplicationShellApiClient["instruments"]) {
  return render(
    <MemoryRouter initialEntries={["/watchlist"]}>
      <ApplicationShell apiClient={createFakeClient({ instruments })} />
    </MemoryRouter>,
  );
}

const cryptoInstrument: Instrument = {
  symbol: "BTC-USD",
  asset_type: "crypto",
  exchange: "CRYPTO",
  currency: "USD",
  quote_currency: "USD",
  timezone: "UTC",
  trading_hours: "24/7",
  sector: "Digital assets",
};

describe("WatchlistPage", () => {
  it("renders and searches the live roster with canonical Asset Detail links", async () => {
    renderWatchlist(vi.fn().mockResolvedValue([fakeInstrument, cryptoInstrument]));

    const roster = await screen.findByRole("list", { name: "Read-only instrument roster" });
    expect(within(roster).getByText("NASDAQ")).toBeInTheDocument();
    expect(within(roster).getByText("America/New_York")).toBeInTheDocument();
    expect(within(roster).getByText("Technology")).toBeInTheDocument();
    expect(within(roster).getAllByRole("link", { name: "Open asset detail" })[0]).toHaveAttribute("href", "/assets/NVDA");
    expect(within(roster).getAllByRole("link", { name: "Open asset detail" })[1]).toHaveAttribute("href", "/assets/BTC-USD");

    fireEvent.change(screen.getByLabelText("Search roster"), { target: { value: "digital assets" } });
    expect(within(roster).queryByText("NVDA")).not.toBeInTheDocument();
    expect(within(roster).getByText("BTC-USD")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search roster"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Asset type"), { target: { value: "equity" } });
    expect(within(roster).getByText("NVDA")).toBeInTheDocument();
    expect(within(roster).queryByText("BTC-USD")).not.toBeInTheDocument();
    expect(screen.getByText(/Saved membership, radar ranking, background scans and alerts are not active/)).toBeInTheDocument();
    expect(screen.getByText(/This browser does not persist a watchlist/)).toBeInTheDocument();
    expect(screen.getByText("Neutral placeholder")).toBeInTheDocument();
  });

  it("distinguishes empty and unavailable roster states with scoped retry", async () => {
    const emptyThenLoaded = vi.fn()
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([fakeInstrument]);
    const emptyView = renderWatchlist(emptyThenLoaded);

    expect(await screen.findByText("The instrument endpoint returned no roster records.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry Instrument roster" }));
    expect(await screen.findByText("NVDA")).toBeInTheDocument();
    expect(emptyThenLoaded).toHaveBeenCalledTimes(2);
    emptyView.unmount();

    const unavailableThenLoaded = vi.fn()
      .mockRejectedValueOnce(new ApiError(503, { code: "INSTRUMENTS_UNAVAILABLE", message: "roster unavailable" }))
      .mockResolvedValueOnce([fakeInstrument]);
    renderWatchlist(unavailableThenLoaded);

    expect(await screen.findByRole("alert")).toHaveTextContent("INSTRUMENTS_UNAVAILABLE: roster unavailable");
    fireEvent.click(screen.getByRole("button", { name: "Retry Instrument roster" }));
    expect(await screen.findByText("NVDA")).toBeInTheDocument();
    expect(unavailableThenLoaded).toHaveBeenCalledTimes(2);
  });

  it("keeps an incomplete but usable roster visible as degraded", async () => {
    const incomplete: Instrument = { ...fakeInstrument, symbol: "AMD", trading_hours: "" };
    renderWatchlist(vi.fn().mockResolvedValue([incomplete]));

    const panel = await screen.findByRole("region", { name: "Instrument roster" });
    expect(within(panel).getByText("Degraded")).toBeInTheDocument();
    expect(within(panel).getByText(/missing descriptive metadata/)).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: "Open asset detail" })).toHaveAttribute("href", "/assets/AMD");
  });
});
