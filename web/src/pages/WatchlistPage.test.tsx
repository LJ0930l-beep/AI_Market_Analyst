import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ApplicationShell } from "../App";
import { ApiError, type ApplicationShellApiClient } from "../api/client";
import type { Instrument } from "../api/types";
import { createFakeClient, fakeInstrument, fakeWatchlistEntry } from "../test/fakeClient";

function renderWatchlist(overrides: Partial<ApplicationShellApiClient> = {}) {
  const client = createFakeClient(overrides);
  return render(
    <MemoryRouter initialEntries={["/watchlist"]}>
      <ApplicationShell apiClient={client} />
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
  it("separates saved membership from the canonical available universe", async () => {
    renderWatchlist({
      instruments: vi.fn().mockResolvedValue([fakeInstrument, cryptoInstrument]),
      watchlist: vi.fn().mockResolvedValue([fakeWatchlistEntry]),
    });

    const roster = await screen.findByRole("list", { name: "Available instrument roster" });
    const saved = screen.getByRole("list", { name: "Saved watchlist entries" });
    expect(within(saved).getByText("NVDA")).toBeInTheDocument();
    expect(within(saved).getByRole("button", { name: "Remove NVDA from saved watchlist" })).toBeInTheDocument();
    expect(within(roster).getByText("NASDAQ")).toBeInTheDocument();
    expect(within(roster).getByText("America/New_York")).toBeInTheDocument();
    expect(within(roster).getByText("Technology")).toBeInTheDocument();
    expect(within(roster).getAllByRole("link", { name: "Open asset detail" })[0]).toHaveAttribute("href", "/assets/NVDA");
    expect(within(roster).getAllByRole("link", { name: "Open asset detail" })[1]).toHaveAttribute("href", "/assets/BTC-USD");

    fireEvent.change(screen.getByLabelText("Search available instruments"), { target: { value: "digital assets" } });
    expect(within(roster).queryByText("NVDA")).not.toBeInTheDocument();
    expect(within(roster).getByText("BTC-USD")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search available instruments"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Asset type"), { target: { value: "equity" } });
    expect(within(roster).getByText("NVDA")).toBeInTheDocument();
    expect(within(roster).queryByText("BTC-USD")).not.toBeInTheDocument();
    expect(screen.getByText(/Registration performs bounded network validation.*Ranking, scans, scheduling and alerts are not active/)).toBeInTheDocument();
    expect(screen.getByText("Neutral placeholder")).toBeInTheDocument();
  });

  it("adds and removes saved membership through the typed client and refreshes state", async () => {
    const watchlist = vi.fn()
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([fakeWatchlistEntry])
      .mockResolvedValueOnce([]);
    const addWatchlist = vi.fn().mockResolvedValue(fakeWatchlistEntry);
    const removeWatchlist = vi.fn().mockResolvedValue({ symbol: "NVDA", deleted: true });
    renderWatchlist({ watchlist, addWatchlist, removeWatchlist });

    const available = await screen.findByRole("region", { name: "Available instruments" });
    const saved = screen.getByRole("region", { name: "Saved watchlist" });
    fireEvent.click(within(available).getByRole("button", { name: "Add NVDA to saved watchlist" }));
    expect(addWatchlist).toHaveBeenCalledWith("NVDA");
    expect(await within(saved).findByRole("button", { name: "Remove NVDA from saved watchlist" })).toBeInTheDocument();

    fireEvent.click(within(saved).getByRole("button", { name: "Remove NVDA from saved watchlist" }));
    expect(removeWatchlist).toHaveBeenCalledWith("NVDA");
    expect(await within(saved).findByText("No instruments are saved yet. Add one from the available canonical universe.")).toBeInTheDocument();
  });

  it("distinguishes empty and unavailable roster states with scoped retry", async () => {
    const emptyThenLoaded = vi.fn()
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([fakeInstrument]);
    const emptyView = renderWatchlist({ instruments: emptyThenLoaded });

    expect(await screen.findByText("The instrument registry returned no instruments.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry Available instruments" }));
    expect(await screen.findByText("NVDA")).toBeInTheDocument();
    expect(emptyThenLoaded).toHaveBeenCalledTimes(2);
    emptyView.unmount();

    const unavailableThenLoaded = vi.fn()
      .mockRejectedValueOnce(new ApiError(503, { code: "INSTRUMENTS_UNAVAILABLE", message: "roster unavailable" }))
      .mockResolvedValueOnce([fakeInstrument]);
    renderWatchlist({ instruments: unavailableThenLoaded });

    expect(await screen.findByRole("alert")).toHaveTextContent("INSTRUMENTS_UNAVAILABLE: roster unavailable");
    fireEvent.click(screen.getByRole("button", { name: "Retry Available instruments" }));
    expect(await screen.findByText("NVDA")).toBeInTheDocument();
    expect(unavailableThenLoaded).toHaveBeenCalledTimes(2);
  });

  it("keeps an incomplete but usable roster visible as degraded", async () => {
    const incomplete: Instrument = { ...fakeInstrument, symbol: "AMD", trading_hours: "" };
    renderWatchlist({ instruments: vi.fn().mockResolvedValue([incomplete]) });

    const panel = await screen.findByRole("region", { name: "Available instruments" });
    expect(within(panel).getByText("Degraded")).toBeInTheDocument();
    expect(within(panel).getByText(/missing descriptive metadata/)).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: "Open asset detail" })).toHaveAttribute("href", "/assets/AMD");
  });

  it("validates and saves a new provider-compatible instrument", async () => {
    const registered = {
      ...fakeInstrument,
      symbol: "MSFT",
      exchange: "UNKNOWN",
      sector: null,
      registry_source: "registered",
      metadata_status: "inferred",
      validation_provider: "test-public-probe",
    };
    const registerInstrument = vi.fn().mockResolvedValue({
      instrument: registered,
      registered: true,
      idempotent: false,
      validation: { status: "validated", mode: "injected_test", provider: "test-public-probe" },
    });
    const addWatchlist = vi.fn().mockResolvedValue({ ...fakeWatchlistEntry, symbol: "MSFT", instrument: registered });
    renderWatchlist({ registerInstrument, addWatchlist });

    const registration = screen.getByRole("region", { name: "Register an instrument" });
    fireEvent.change(within(registration).getByLabelText("Instrument symbol"), { target: { value: "MSFT" } });
    fireEvent.click(within(registration).getByRole("button", { name: "Validate and add" }));

    expect(registerInstrument).toHaveBeenCalledWith("MSFT", "equity");
    expect(await within(registration).findByRole("status")).toHaveTextContent("MSFT passed provider validation and was saved");
    expect(addWatchlist).toHaveBeenCalledWith("MSFT");
  });

  it("shows provider validation failures without claiming registration", async () => {
    const registerInstrument = vi.fn().mockRejectedValue(
      new ApiError(503, { code: "PROVIDER_UNAVAILABLE", message: "provider offline" }),
    );
    const addWatchlist = vi.fn();
    renderWatchlist({ registerInstrument, addWatchlist });

    fireEvent.change(screen.getByLabelText("Instrument symbol"), { target: { value: "MSFT" } });
    fireEvent.click(screen.getByRole("button", { name: "Validate and add" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("PROVIDER_UNAVAILABLE: provider offline");
    expect(addWatchlist).not.toHaveBeenCalled();
  });
});
