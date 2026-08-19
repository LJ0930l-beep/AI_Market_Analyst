import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ApplicationShell } from "../App";
import { ApiError } from "../api/client";
import type { PaperTrade } from "../api/types";
import { createFakeClient, fakeOutcome, fakePaperTrade } from "../test/fakeClient";

function renderPaperTrades(client = createFakeClient()) {
  return render(
    <MemoryRouter initialEntries={["/paper-trades"]}>
      <ApplicationShell apiClient={client} />
    </MemoryRouter>,
  );
}

describe("PaperTradesPage", () => {
  it("renders the permanent paper-only boundary and linked detail evidence", async () => {
    const trade: PaperTrade = { ...fakePaperTrade, outcome: fakeOutcome, outcome_status: "TP1" };
    const client = createFakeClient({
      paperTrades: vi.fn().mockResolvedValue([trade]),
      paperTrade: vi.fn().mockResolvedValue(trade),
    });
    renderPaperTrades(client);

    expect(await screen.findByText(/Permanent boundary: PaperTrade means a local record/i)).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: trade.prediction_id }));
    const detail = await screen.findByRole("region", { name: "PaperTrade detail" });
    expect(within(detail).getByText(/No real order, broker action or execution is performed/)).toBeInTheDocument();
    expect(within(detail).getByText("Linked Prediction")).toBeInTheDocument();
    expect(within(detail).getByText("Linked Outcome")).toBeInTheDocument();
    expect(within(detail).getAllByText("TP1").length).toBeGreaterThan(0);
    expect(within(detail).getByText("0.72")).toBeInTheDocument();
    expect(within(detail).queryByRole("button", { name: /follow/i })).not.toBeInTheDocument();
    expect(within(detail).queryByText(/order was sent/i)).not.toBeInTheDocument();
  });

  it("sends supported filters and isolates list failure from the paper-only page shell", async () => {
    const paperTrades = vi.fn().mockResolvedValue([fakePaperTrade]);
    const client = createFakeClient({ paperTrades });
    const first = renderPaperTrades(client);

    await screen.findByRole("button", { name: fakePaperTrade.prediction_id });
    fireEvent.change(screen.getByLabelText("Symbol"), { target: { value: "nvda" } });
    fireEvent.change(screen.getByLabelText("PaperTrade status"), { target: { value: "OPEN" } });
    fireEvent.change(screen.getByLabelText("Outcome status"), { target: { value: "TP1" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    expect(await screen.findByText("PaperTrade records")).toBeInTheDocument();
    await waitFor(() => expect(paperTrades).toHaveBeenLastCalledWith(
      { limit: 25, offset: 0, symbol: "NVDA", status: "OPEN", outcome_status: "TP1" },
      expect.any(AbortSignal),
    ));

    first.unmount();
    const failedClient = createFakeClient({
      paperTrades: vi.fn().mockRejectedValue(new ApiError(503, { code: "STORE_UNAVAILABLE", message: "store unavailable" })),
    });
    const second = renderPaperTrades(failedClient);
    expect(await screen.findByRole("alert")).toHaveTextContent("STORE_UNAVAILABLE: store unavailable");
    expect(screen.getByText(/PaperTrade means a local record/i)).toBeInTheDocument();
    second.unmount();
  });
});
