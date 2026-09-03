import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ApplicationShell } from "../App";
import { ApiError } from "../api/client";
import type { Prediction } from "../api/types";
import { createFakeClient, fakeMarketIntelligence, fakePrediction } from "../test/fakeClient";

function renderPredictions(client = createFakeClient()) {
  return render(
    <MemoryRouter initialEntries={["/predictions"]}>
      <ApplicationShell apiClient={client} />
    </MemoryRouter>,
  );
}

function panel(name: string): HTMLElement {
  return screen.getByRole("region", { name });
}

describe("PredictionsPage", () => {
  it("shows deterministic market and trigger evidence before the first Prediction without calling analysis", async () => {
    const analysis = vi.fn();
    const intelligence = {
      ...fakeMarketIntelligence,
      latest_signal: null,
      watchlist: [],
      pulse: fakeMarketIntelligence.pulse.map((item) => item.symbol === "BTCUSDT" ? {
        ...item,
        status: "available",
        price: 77211.87,
        change_pct: 1.25,
        freshness: { status: "fresh", provider: "binance_public", data_as_of: "2030-01-02T12:00:00Z" },
        missing_reasons: [],
      } : item),
    };
    const client = createFakeClient({
      predictions: vi.fn().mockResolvedValue([]),
      marketIntelligence: vi.fn().mockResolvedValue(intelligence),
      monitoringStatus: vi.fn().mockResolvedValue({ state: "running", active: true, worker_alive: true }),
      triggerEvents: vi.fn().mockResolvedValue({
        policy_version: "trigger_policy_v2",
        events: [{
          trigger_event_id: "trigger-btc",
          instrument_id: "BTCUSDT",
          timeframe: "15m",
          bar_start: "2030-01-02T11:45:00Z",
          bar_end: "2030-01-02T12:00:00Z",
          trigger_type: "BREAKOUT",
          trigger_score: 0.81,
          fingerprint: "fingerprint",
          policy_version: "trigger_policy_v2",
          status: "ANALYZED",
          analysis_status: "VALID",
        }],
      }),
      monitoringOpportunities: vi.fn().mockResolvedValue({ contract_version: "opportunity_analysis_v1", validator: "python", model_tier: "smart_9b_only", analyses: [] }),
      analysis,
    });
    renderPredictions(client);

    expect(await screen.findByRole("heading", { name: "Market state before the first Prediction" })).toBeInTheDocument();
    const btcCard = (await screen.findByRole("heading", { name: "BTCUSDT" })).closest("article");
    expect(btcCard).not.toBeNull();
    expect(btcCard as HTMLElement).toHaveTextContent(/77,?211\.87/);
    expect(btcCard as HTMLElement).toHaveTextContent("BREAKOUT");
    expect(btcCard as HTMLElement).toHaveTextContent("score 0.81");
    expect(btcCard as HTMLElement).toHaveTextContent("ANALYZED / VALID");
    expect(screen.getByText(/Loading this page never calls Qwen/)).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "View market / run explicit analysis" })[0]).toHaveAttribute("href", "/assets/BTCUSDT");
    expect(analysis).not.toHaveBeenCalled();
  });

  it("labels degraded provider and partial trigger evidence without inventing a signal", async () => {
    const degraded = {
      ...fakeMarketIntelligence,
      latest_signal: null,
      watchlist: [],
      pulse: [{
        symbol: "BTCUSDT",
        status: "degraded",
        price: 77000,
        change_pct: null,
        freshness: { status: "stale", provider: "binance_public", data_as_of: "2030-01-02T10:00:00Z" },
        missing_reasons: ["change_missing"],
      }],
    };
    const client = createFakeClient({
      predictions: vi.fn().mockResolvedValue([]),
      marketIntelligence: vi.fn().mockResolvedValue(degraded),
      monitoringStatus: vi.fn().mockResolvedValue({ state: "degraded", active: false, worker_alive: false }),
      triggerEvents: vi.fn().mockRejectedValue(new ApiError(503, { code: "TRIGGER_SOURCE_UNAVAILABLE", message: "unavailable" })),
      monitoringOpportunities: vi.fn().mockResolvedValue({ contract_version: "opportunity_analysis_v1", validator: "python", model_tier: "smart_9b_only", analyses: [] }),
    });
    renderPredictions(client);

    expect(await screen.findByText("change_missing")).toBeInTheDocument();
    expect(screen.getByText("Some monitoring or trigger evidence is unavailable. Market cards retain the exact provider freshness and missing reasons returned by the backend.")).toBeInTheDocument();
    expect(screen.getAllByText(/degraded/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText("No deterministic trigger has been recorded for this symbol.").length).toBeGreaterThan(0);
    expect(client.analysis).not.toHaveBeenCalled();
  });

  it("reports owned-backend failure transparently instead of rendering fallback prices", async () => {
    const client = createFakeClient({
      predictions: vi.fn().mockRejectedValue(new ApiError(503, { code: "BACKEND_UNAVAILABLE", message: "backend unavailable" })),
    });
    renderPredictions(client);

    expect(await screen.findByText("The owned backend is unavailable, so current market or trigger state cannot be verified.")).toBeInTheDocument();
    expect(screen.getByText(/No price, trigger or signal is inferred/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Settings / Health" })).toHaveAttribute("href", "/settings");
    expect(screen.queryByText("77211.87")).not.toBeInTheDocument();
    expect(client.analysis).not.toHaveBeenCalled();
  });

  it("keeps the first-run action entry while read-only market evidence is unavailable", async () => {
    const client = createFakeClient({
      predictions: vi.fn().mockResolvedValue([]),
      marketIntelligence: vi.fn().mockRejectedValue(new ApiError(503, { code: "MARKET_EVIDENCE_UNAVAILABLE", message: "unavailable" })),
    });
    renderPredictions(client);

    expect(await screen.findByText("Read-only market evidence is unavailable.")).toBeInTheDocument();
    expect(screen.getByText(/No price, trigger or signal is inferred/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Settings / Health" })).toBeInTheDocument();
    expect(client.analysis).not.toHaveBeenCalled();
  });

  it("encodes the supported list filters and bounded page size", async () => {
    const predictions = vi.fn().mockResolvedValue([fakePrediction]);
    const client = createFakeClient({ predictions });
    renderPredictions(client);

    await screen.findByRole("button", { name: fakePrediction.prediction_id });
    fireEvent.change(screen.getByLabelText("Symbol"), { target: { value: "amd" } });
    fireEvent.change(screen.getByLabelText("Timeframe"), { target: { value: "4h" } });
    fireEvent.change(screen.getByLabelText("Action"), { target: { value: "LONG" } });
    fireEvent.change(screen.getByLabelText("Source type"), { target: { value: "replay" } });
    fireEvent.change(screen.getByLabelText("Outcome presence"), { target: { value: "false" } });
    fireEvent.change(screen.getByLabelText("Rows"), { target: { value: "50" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply filters" }));

    await waitFor(() => expect(predictions).toHaveBeenLastCalledWith(
      {
        limit: 50,
        offset: 0,
        symbol: "AMD",
        timeframe: "4h",
        action: "LONG",
        source_type: "replay",
        has_outcome: false,
      },
      expect.any(AbortSignal),
    ));
  });

  it("loads selected detail, preserves exact raw confidence, and never renders hidden payloads", async () => {
    const sentinel = "PREDICTIONS_RAW_MODEL_SENTINEL_9c8e";
    const detail: Prediction = {
      ...fakePrediction,
      prediction_id: "prediction-safe-detail",
      raw_confidence: 0.123456789,
      raw_model_response: sentinel,
      context_json: sentinel,
      signal_valid_until: "2030-01-02T13:00:00Z",
      reevaluate_at: "2030-01-02T12:20:00Z",
    };
    const prediction = vi.fn().mockResolvedValue(detail);
    const client = createFakeClient({ predictions: vi.fn().mockResolvedValue([detail]), prediction });
    renderPredictions(client);

    fireEvent.click(await screen.findByRole("button", { name: detail.prediction_id }));
    const detailPanel = await screen.findByRole("heading", { name: detail.prediction_id });
    expect(detailPanel).toBeInTheDocument();
    expect(within(panel("Prediction detail")).getByText("0.123456789")).toBeInTheDocument();
    expect(within(panel("Prediction detail")).queryByText("0.123457")).not.toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(sentinel);
    const rail = screen.getByRole("heading", { name: "Signal validity rail" }).closest("section");
    expect(rail).not.toBeNull();
    expect(within(rail as HTMLElement).getByRole("listitem", { name: /Generated: 2030-01-02T12:00:00.000Z/ })).toBeInTheDocument();
    expect(within(rail as HTMLElement).getByRole("listitem", { name: /Re-evaluate: 2030-01-02T12:20:00.000Z/ })).toBeInTheDocument();
    expect(within(rail as HTMLElement).getByRole("listitem", { name: /Expiry: 2030-01-02T13:00:00.000Z/ })).toBeInTheDocument();
    expect(within(rail as HTMLElement).getByText("Prediction source: live")).toBeInTheDocument();
    expect(within(rail as HTMLElement).getByText("qwen3.5:4b")).toBeInTheDocument();
  });

  it("suppresses Follow for WAIT and expired predictions", async () => {
    const waitPrediction: Prediction = { ...fakePrediction, prediction_id: "prediction-wait", action: "WAIT" };
    const expiredPrediction: Prediction = { ...fakePrediction, prediction_id: "prediction-expired", action: "LONG", signal_valid_until: "2020-01-02T13:00:00Z" };
    const follow = vi.fn();
    const client = createFakeClient({ predictions: vi.fn().mockResolvedValue([waitPrediction]), prediction: vi.fn().mockResolvedValue(waitPrediction), followPrediction: follow });
    const firstRender = renderPredictions(client);

    fireEvent.click(await screen.findByRole("button", { name: waitPrediction.prediction_id }));
    const detailPanel = await screen.findByRole("region", { name: "Prediction detail" });
    const waitButton = await within(detailPanel).findByRole("button", { name: "Follow as PaperTrade" });
    expect(waitButton).toBeDisabled();
    expect(within(detailPanel).getByText(/WAIT is retained for coverage only/)).toBeInTheDocument();
    fireEvent.click(waitButton);
    expect(follow).not.toHaveBeenCalled();
    firstRender.unmount();

    const rerenderClient = createFakeClient({ predictions: vi.fn().mockResolvedValue([expiredPrediction]), prediction: vi.fn().mockResolvedValue(expiredPrediction), followPrediction: follow });
    // A fresh mount keeps the expiry assertion isolated from the first selected record.
    renderPredictions(rerenderClient);
    fireEvent.click(await screen.findByRole("button", { name: expiredPrediction.prediction_id }));
    const expiredPanel = await screen.findByRole("region", { name: "Prediction detail" });
    expect(await within(expiredPanel).findByRole("button", { name: "Follow as PaperTrade" })).toBeDisabled();
    expect(within(expiredPanel).getByText(/validity window has expired/)).toBeInTheDocument();
    const rail = screen.getByRole("heading", { name: "Signal validity rail" }).closest("section");
    expect(rail).not.toBeNull();
    expect(within(rail as HTMLElement).getByText("Signal window expired")).toBeInTheDocument();
  });

  it("prevents duplicate Follow submissions and refreshes an idempotent completion", async () => {
    let resolveFollow: (value: { prediction_id: string; status: string; real_order: false }) => void = () => undefined;
    const follow = vi.fn().mockReturnValue(new Promise((resolve) => { resolveFollow = resolve; }));
    const prediction = vi.fn().mockResolvedValue(fakePrediction);
    const client = createFakeClient({ predictions: vi.fn().mockResolvedValue([fakePrediction]), prediction, followPrediction: follow });
    renderPredictions(client);

    fireEvent.click(await screen.findByRole("button", { name: fakePrediction.prediction_id }));
    const detailPanel = await screen.findByRole("region", { name: "Prediction detail" });
    const followButton = await within(detailPanel).findByRole("button", { name: "Follow as PaperTrade" });
    act(() => {
      followButton.click();
      followButton.click();
    });
    expect(follow).toHaveBeenCalledTimes(1);
    expect(followButton).toBeDisabled();

    resolveFollow({ prediction_id: fakePrediction.prediction_id, status: "OPEN", real_order: false });
    expect(await within(detailPanel).findByRole("status")).toHaveTextContent("PaperTrade record saved");
    await waitFor(() => expect(prediction).toHaveBeenCalledTimes(2));
    expect(follow).toHaveBeenCalledWith(fakePrediction.prediction_id, {}, expect.any(AbortSignal));
  });

  it("shows structured Follow failures without exposing a generic success state", async () => {
    const follow = vi.fn().mockRejectedValue(new ApiError(409, { code: "SIGNAL_EXPIRED", message: "signal validity window has expired" }));
    const actionable = { ...fakePrediction, signal_valid_until: "2030-01-02T13:00:00Z" };
    const client = createFakeClient({ predictions: vi.fn().mockResolvedValue([actionable]), prediction: vi.fn().mockResolvedValue(actionable), followPrediction: follow });
    renderPredictions(client);

    fireEvent.click(await screen.findByRole("button", { name: actionable.prediction_id }));
    const detailPanel = await screen.findByRole("region", { name: "Prediction detail" });
    fireEvent.click(await within(detailPanel).findByRole("button", { name: "Follow as PaperTrade" }));
    expect(await within(detailPanel).findByRole("alert")).toHaveTextContent("SIGNAL_EXPIRED: signal validity window has expired");
    expect(within(detailPanel).queryByText(/PaperTrade record saved/)).not.toBeInTheDocument();
  });
});
