import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApplicationShell } from "../App";
import { ApiError } from "../api/client";
import type { AnalysisResult, Instrument, InstrumentNews } from "../api/types";
import { createFakeClient, fakeAnalysis, fakeInstrument, fakeMarketContext, fakeNews, fakeSnapshot } from "../test/fakeClient";

function renderAsset(client = createFakeClient(), initialEntry = "/assets/NVDA") {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <ApplicationShell apiClient={client} />
    </MemoryRouter>,
  );
}

function panel(name: string): HTMLElement {
  return screen.getByRole("region", { name });
}

const newsWithEvent: InstrumentNews = {
  ...fakeNews,
  events: [
    {
      id: "event-1",
      source: "Fixture Journal",
      published_at: "2030-01-02T11:30:00Z",
      title: "GPU supply update",
      symbols: ["NVDA"],
      category: "product",
      sentiment: 0.4,
      importance: 72,
      credibility: 88,
      impact_horizon: "1-3d",
      url: "https://example.test/event-1",
    },
  ],
};

describe("AssetDetailPage", () => {
  afterEach(() => window.localStorage.clear());
  it("loads roster, snapshot and news without running analysis on mount", async () => {
    const snapshot = vi.fn().mockResolvedValue(fakeSnapshot);
    const news = vi.fn().mockResolvedValue(newsWithEvent);
    const context = vi.fn().mockResolvedValue(fakeMarketContext);
    const analysis = vi.fn().mockResolvedValue(fakeAnalysis);
    const client = createFakeClient({ instrumentSnapshot: snapshot, instrumentNews: news, instrumentContext: context, analysis });

    renderAsset(client);

    expect(await screen.findByRole("heading", { name: "Asset detail / NVDA" })).toBeInTheDocument();
    const snapshotPanel = await screen.findByRole("region", { name: "Market snapshot" });
    expect(await within(snapshotPanel).findByText("OHLCV evidence")).toBeInTheDocument();
    expect(within(snapshotPanel).getByText(/Latest close 101/)).toBeInTheDocument();
    expect(within(snapshotPanel).getAllByText("100").length).toBeGreaterThan(0);
    expect(within(snapshotPanel).getByText("96")).toBeInTheDocument();
    expect(within(panel("News evidence")).getByText("GPU supply update")).toBeInTheDocument();
    expect(within(panel("News evidence")).getByText("Fixture Journal")).toBeInTheDocument();
    expect(await within(panel("Benchmark, events and Market Memory")).findByText("benchmark_mapping_v1")).toBeInTheDocument();
    expect(context).toHaveBeenCalledWith("NVDA", { timeframe: "1h", limit: 120 }, expect.any(AbortSignal));
    expect(snapshot).toHaveBeenCalledWith(
      "NVDA",
      { timeframe: "1h", limit: 120 },
      expect.any(AbortSignal),
    );
    expect(news).toHaveBeenCalledWith("NVDA", expect.any(AbortSignal));
    expect(analysis).not.toHaveBeenCalled();
    expect(screen.getByRole("link", { name: "Consult Qwen about this instrument" })).toHaveAttribute("href", "/consult?symbol=NVDA");
  });

  it("keeps news visible when the snapshot endpoint fails", async () => {
    const snapshot = vi.fn().mockRejectedValue(
      new ApiError(502, { code: "PROVIDER_ERROR", message: "market provider unavailable" }),
    );
    const client = createFakeClient({ instrumentSnapshot: snapshot, instrumentNews: vi.fn().mockResolvedValue(newsWithEvent) });

    renderAsset(client);

    const snapshotPanel = await screen.findByRole("region", { name: "Market snapshot" });
    expect(await within(snapshotPanel).findByRole("alert")).toHaveTextContent("This panel is unavailable");
    expect(within(snapshotPanel).getByText("PROVIDER_ERROR: market provider unavailable")).toBeInTheDocument();
    expect(within(panel("News evidence")).getByText("GPU supply update")).toBeInTheDocument();
  });

  it("resets analysis when timeframe or symbol changes and only reloads the snapshot for timeframe", async () => {
    const amd: Instrument = { ...fakeInstrument, symbol: "AMD", sector: "Semiconductors" };
    const snapshot = vi.fn().mockResolvedValue(fakeSnapshot);
    const news = vi.fn().mockResolvedValue(fakeNews);
    const analysis = vi.fn().mockResolvedValue(fakeAnalysis);
    const client = createFakeClient({
      instruments: vi.fn().mockResolvedValue([fakeInstrument, amd]),
      instrumentSnapshot: snapshot,
      instrumentNews: news,
      analysis,
    });

    renderAsset(client);
    fireEvent.click(await screen.findByRole("button", { name: "Run analysis" }));
    expect(await screen.findByRole("heading", { name: "Signal proposal" })).toBeInTheDocument();
    expect(analysis).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "4h" }));
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Signal proposal" })).not.toBeInTheDocument());
    expect(snapshot).toHaveBeenCalledWith("NVDA", { timeframe: "4h", limit: 120 }, expect.any(AbortSignal));
    expect(news).toHaveBeenCalledTimes(1);
    expect(analysis).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByLabelText("Instrument"), { target: { value: "AMD" } });
    expect(await screen.findByRole("heading", { name: "Asset detail / AMD" })).toBeInTheDocument();
    await waitFor(() => expect(snapshot).toHaveBeenCalledWith("AMD", { timeframe: "4h", limit: 120 }, expect.any(AbortSignal)));
    expect(news).toHaveBeenCalledTimes(2);
    expect(analysis).toHaveBeenCalledTimes(1);
  });

  it("runs explicit analysis once, disables the control while pending, and keeps WAIT levels absent", async () => {
    let resolveAnalysis: (result: AnalysisResult) => void = () => undefined;
    const analysis = vi.fn().mockReturnValue(
      new Promise<AnalysisResult>((resolve) => {
        resolveAnalysis = resolve;
      }),
    );
    const client = createFakeClient({ analysis });
    renderAsset(client);

    const runButton = await screen.findByRole("button", { name: "Run analysis" });
    fireEvent.click(runButton);
    expect(runButton).toBeDisabled();
    fireEvent.click(runButton);
    expect(analysis).toHaveBeenCalledTimes(1);

    resolveAnalysis(fakeAnalysis);
    expect(await screen.findByText("prediction-analysis")).toBeInTheDocument();
    expect(screen.getByText("WAIT · saved coverage result")).toBeInTheDocument();
    expect(within(panel("Explicit analysis")).getByText(/Request: complete/)).toBeInTheDocument();
    expect(screen.getByText("Entry, stop and targets are not applicable to a WAIT result.")).toBeInTheDocument();
    expect(screen.queryByText("Entry low")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /follow/i })).not.toBeInTheDocument();
  });

  it("does not render raw model response or context sentinel fields", async () => {
    const sentinel = "DO_NOT_RENDER_MODEL_RAW_OR_CONTEXT_7f2c";
    const result: AnalysisResult = {
      ...fakeAnalysis,
      raw_model_response: sentinel,
      context_json: sentinel,
      signal: {
        ...(fakeAnalysis.signal ?? {}),
        raw_model_response: sentinel,
        context_json: sentinel,
      },
    };
    const client = createFakeClient({ analysis: vi.fn().mockResolvedValue(result) });
    renderAsset(client);

    fireEvent.click(await screen.findByRole("button", { name: "Run analysis" }));
    expect(await screen.findByRole("heading", { name: "Signal proposal" })).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(sentinel);
  });

  it("labels a failed explicit analysis request as failed", async () => {
    const client = createFakeClient({
      analysis: vi.fn().mockRejectedValue(new ApiError(503, { code: "MODEL_UNAVAILABLE", message: "model unavailable" })),
    });
    renderAsset(client);

    fireEvent.click(await screen.findByRole("button", { name: "Run analysis" }));
    const analysisPanel = await screen.findByRole("region", { name: "Explicit analysis" });
    expect(await within(analysisPanel).findByText(/Request: failed/)).toBeInTheDocument();
    expect(within(analysisPanel).getByRole("alert")).toHaveTextContent("MODEL_UNAVAILABLE: model unavailable");
  });

  it("renders returned actionable levels and exact signal provenance with expired state", async () => {
    const longResult: AnalysisResult = {
      ...fakeAnalysis,
      model: { provider: "ollama", available: true, model_id: "model-x" },
      signal: {
        ...(fakeAnalysis.signal ?? {}),
        prediction_id: "prediction-long",
        action: "LONG",
        generated_at: "2020-01-02T12:00:00Z",
        reevaluate_at: "2020-01-02T12:15:00Z",
        signal_valid_until: "2020-01-02T13:00:00Z",
        source_type: "live",
        model_id: "model-x",
        raw_confidence: 0.123456789,
        entry_low: 99,
        entry_high: 101,
        stop: 95,
        tp1: 110,
        tp2: 115,
      },
    };
    const client = createFakeClient({ analysis: vi.fn().mockResolvedValue(longResult) });
    renderAsset(client);

    fireEvent.click(await screen.findByRole("button", { name: "Run analysis" }));
    expect(await screen.findByText("LONG · actionable proposal")).toBeInTheDocument();
    expect(screen.getByText("0.123456789")).toBeInTheDocument();
    expect(screen.queryByText("0.123457")).not.toBeInTheDocument();
    expect(screen.getByText("99")).toBeInTheDocument();
    expect(screen.getByText("110")).toBeInTheDocument();
    const rail = screen.getByRole("heading", { name: "Signal validity rail" }).closest("section");
    expect(rail).not.toBeNull();
    expect(within(rail as HTMLElement).getByText("Signal window expired")).toBeInTheDocument();
    expect(within(rail as HTMLElement).getByRole("listitem", { name: /Generated: 2020-01-02T12:00:00.000Z/ })).toBeInTheDocument();
    expect(within(rail as HTMLElement).getByRole("listitem", { name: /Re-evaluate: 2020-01-02T12:15:00.000Z/ })).toBeInTheDocument();
    expect(within(rail as HTMLElement).getByRole("listitem", { name: /Expiry: 2020-01-02T13:00:00.000Z/ })).toBeInTheDocument();
    expect(within(rail as HTMLElement).getByText("source_type: live · provider: fixture_market")).toBeInTheDocument();
    expect(within(rail as HTMLElement).getByText("model_id: model-x")).toBeInTheDocument();
  });

  it("does not substitute a default symbol when the roster does not contain the route", async () => {
    const snapshot = vi.fn().mockResolvedValue(fakeSnapshot);
    const news = vi.fn().mockResolvedValue(fakeNews);
    const client = createFakeClient({
      instruments: vi.fn().mockResolvedValue([fakeInstrument]),
      instrumentSnapshot: snapshot,
      instrumentNews: news,
    });

    renderAsset(client, "/assets/NOPE");

    expect(await screen.findByRole("alert")).toHaveTextContent("NOPE is not present");
    expect(screen.queryByText("Asset detail / NVDA")).not.toBeInTheDocument();
    expect(snapshot).not.toHaveBeenCalled();
    expect(news).not.toHaveBeenCalled();
  });

  it("renders ResearchFacts and OHLCV fixed copy with the selected zh-CN locale", async () => {
    window.localStorage.setItem("ai-market-analyst.language", "zh-CN");
    const client = createFakeClient({
      instrumentSnapshot: vi.fn().mockResolvedValue({ ...fakeSnapshot, response_time: "not-a-date", bars: [] }),
      instrumentNews: vi.fn().mockResolvedValue({ ...fakeNews, fetched_at: null }),
    });
    renderAsset(client);

    const snapshotPanel = await screen.findByRole("region", { name: "市场快照" });
    expect(within(snapshotPanel).getByText("OHLCV 证据")).toBeInTheDocument();
    expect(within(snapshotPanel).getByText("此标的和时间周期未提供 OHLCV K 线。")).toBeInTheDocument();
    expect(within(snapshotPanel).getByText("无法解析")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "咨询 Qwen 此标的" })).toBeInTheDocument();
    expect(screen.queryByText("Not supplied", { exact: true })).not.toBeInTheDocument();
    expect(screen.queryByText("Not parseable", { exact: true })).not.toBeInTheDocument();
    expect(screen.queryByText("OHLCV evidence", { exact: true })).not.toBeInTheDocument();
  });
});
