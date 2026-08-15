import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ApplicationShell } from "./App";
import type { MarketApiClient } from "./api/client";
import type { HealthResponse } from "./api/types";

const connectedHealth: HealthResponse = {
  status: "ok",
  phase: 4,
  api_version: "0.4.0",
  product: "AI Market Analyst",
  real_orders: false,
  private_keys: false,
};

function renderShell(initialEntry: string, health: Pick<MarketApiClient, "health">) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <ApplicationShell apiClient={health} />
    </MemoryRouter>,
  );
}

describe("application shell", () => {
  it("renders semantic navigation, skip navigation and current route state", () => {
    const client = { health: vi.fn().mockResolvedValue(connectedHealth) };
    renderShell("/predictions", client);

    const navigation = screen.getByRole("navigation", { name: "Primary navigation" });
    expect(within(navigation).getByRole("link", { name: "Predictions" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("main")).toHaveAttribute("id", "main-content");
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#main-content");
    expect(screen.getByRole("heading", { name: "Predictions" })).toBeInTheDocument();
  });

  it("shows a distinct loading health state", () => {
    const pending = new Promise<HealthResponse>(() => undefined);
    const client = { health: vi.fn().mockReturnValue(pending) };
    renderShell("/", client);

    expect(screen.getByRole("status")).toHaveTextContent("Checking backend");
    expect(screen.getByText(/model and provider status is separate/i)).toBeInTheDocument();
  });

  it("shows a distinct connected health state without claiming model health", async () => {
    const client = { health: vi.fn().mockResolvedValue(connectedHealth) };
    renderShell("/", client);

    expect(await screen.findByRole("status")).toHaveTextContent("Backend connected");
    expect(screen.getByText(/API health is available; model and provider status is separate/i)).toBeInTheDocument();
  });

  it("shows a distinct unavailable health state", async () => {
    const client = { health: vi.fn().mockRejectedValue(new Error("offline")) };
    renderShell("/", client);

    expect(await screen.findByRole("status")).toHaveTextContent("Backend unavailable");
    expect(screen.getByText(/model and provider status is unknown/i)).toBeInTheDocument();
  });
});
