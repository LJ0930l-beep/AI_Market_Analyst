import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiClient } from "../api/client";
import { AITraderPanel } from "./AITraderPanel";
import { MemoryRouter } from "react-router-dom";

describe("AI trader durable plan console", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders scoped plan behavior and sends a real session control action", async () => {
    const v2 = vi.spyOn(apiClient, "v2").mockImplementation(async (path: string, method = "GET") => {
      if (path === "/accounts") {
        return { accounts: [{ account_id: "paper_test", mode: "PAPER", venue: "simulated" }] } as never;
      }
      if (path.startsWith("/trading-authorizations/active")) {
        return { authorization: null } as never;
      }
      if (path.startsWith("/ai-session/status")) {
        return {
          session: { state: "RUNNING", generation: 4 },
          latest_cycle: null,
          protection_summary: { active_positions: 1, symbols: ["BTCUSDT"] },
          market_freshness: { status: "HEALTHY" },
          model_status: { status: "UNVERIFIED" },
          last_market_event_at: "2030-01-02T12:00:00Z",
        } as never;
      }
      if (path.startsWith("/trade-plans")) {
        return {
          plans: [{
            plan_id: "plan_1",
            status: "WAITING_TRIGGER",
            plan: {
              symbol: "BTCUSDT",
              action: "REDUCE_POSITION",
              reduce_fraction: 0.25,
              leverage: 3,
              entry_trigger: "typed price condition",
              evidence: [{ type: "local_fixture" }],
              conditions: { entry: { type: "PRICE" } },
            },
            execution_result: { condition_evaluation: { status: "WAITING_TRIGGER" } },
          }],
        } as never;
      }
      if (method === "POST" && path.startsWith("/ai-session/pause")) {
        return { state: "PAUSED" } as never;
      }
      return {} as never;
    });

    render(<MemoryRouter><AITraderPanel currentMode="PAPER" activeAccount="paper_test" /></MemoryRouter>);

    expect(await screen.findByTestId("trade-plans-panel")).toHaveTextContent("BTCUSDT");
    expect(screen.getByTestId("trade-plans-panel")).toHaveTextContent("reduce 0.25");
    expect(screen.getByTestId("trade-plans-panel")).toHaveTextContent("leverage 3");
    expect(screen.getByTestId("trade-plans-panel")).toHaveTextContent("WAITING_TRIGGER");

    fireEvent.click(screen.getByRole("button", { name: /Pause AI/i }));
    await waitFor(() => expect(v2).toHaveBeenCalledWith("/ai-session/pause?account_id=paper_test", "POST"));
  });

  it("does not render a missing protection count as zero", async () => {
    vi.spyOn(apiClient, "v2").mockImplementation(async (path: string) => {
      if (path === "/accounts") {
        return { accounts: [{ account_id: "paper_unknown", mode: "PAPER", venue: "simulated" }] } as never;
      }
      if (path.startsWith("/trading-authorizations/active")) return { authorization: null } as never;
      if (path.startsWith("/ai-session/status")) {
        return { session: { state: "RUNNING", generation: 1 }, model_status: { status: "UNVERIFIED" } } as never;
      }
      if (path.startsWith("/trade-plans")) return { plans: [] } as never;
      return {} as never;
    });

    render(<MemoryRouter><AITraderPanel currentMode="PAPER" activeAccount="paper_unknown" /></MemoryRouter>);

    await waitFor(() => expect(screen.getByText("UNKNOWN")).toBeInTheDocument());
  });
});
