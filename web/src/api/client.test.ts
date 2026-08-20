import { describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError, getApiBaseUrl, type ApiErrorPayload, serializeQuery } from "./client";

type FetchMock = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("ApiClient", () => {
  it("uses the non-colliding development API prefix by default and preserves explicit or production bases", () => {
    expect(getApiBaseUrl(undefined, true)).toBe("/api");
    expect(getApiBaseUrl(undefined, false)).toBe("");
    expect(getApiBaseUrl("http://127.0.0.1:8000///", true)).toBe("http://127.0.0.1:8000");
  });

  it("serializes typed filters and forwards an AbortSignal", async () => {
    const fetchImpl = vi.fn<FetchMock>().mockResolvedValue(
      response([{ prediction_id: "prediction-1" }]),
    );
    const client = new ApiClient({ baseUrl: "http://localhost:8000/", fetchImpl });
    const controller = new AbortController();

    await client.predictions(
      {
        symbol: "BTC/USD",
        action: "LONG",
        has_outcome: false,
        limit: 25,
        offset: 2,
        source_type: "live",
      },
      controller.signal,
    );

    expect(fetchImpl).toHaveBeenCalledWith(
      "http://localhost:8000/predictions?symbol=BTC%2FUSD&action=LONG&has_outcome=false&limit=25&offset=2&source_type=live",
      expect.objectContaining({ method: "GET", signal: controller.signal }),
    );
    expect(serializeQuery({ empty: "", missing: undefined, valid: "value" })).toBe("?valid=value");
  });

  it("invokes fetch as a plain function without binding the ApiClient as this", async () => {
    const fetchImpl: FetchMock = function (this: unknown) {
      if (this !== undefined) {
        throw new TypeError("Illegal invocation");
      }
      return Promise.resolve(response({ predictions: 1 }));
    };
    const client = new ApiClient({ baseUrl: "http://localhost:8000", fetchImpl });

    await expect(client.stats()).resolves.toEqual({ predictions: 1 });
  });

  it("decodes the structured API error contract", async () => {
    const errorPayload: { error: ApiErrorPayload } = {
      error: {
        code: "INVALID_SYMBOL",
        message: "unsupported instrument symbol",
        detail: "not in universe",
        context: { symbol: "NOPE" },
      },
    };
    const fetchImpl = vi.fn<FetchMock>().mockResolvedValue(response(errorPayload, 400));
    const client = new ApiClient({ baseUrl: "http://localhost:8000", fetchImpl });

    try {
      await client.performanceSummary({ symbol: "NOPE" });
      throw new Error("expected structured API error");
    } catch (error: unknown) {
      expect(error).toBeInstanceOf(ApiError);
      expect(error).toMatchObject({
        status: 400,
        code: "INVALID_SYMBOL",
        message: "unsupported instrument symbol",
        detail: "not in universe",
        context: { symbol: "NOPE" },
      });
    }
  });

  it("requests typed stored counts from the stats endpoint", async () => {
    const fetchImpl = vi.fn<FetchMock>().mockResolvedValue(
      response({ predictions: 12, paper_trades: 3, unknown_counter: 2 }),
    );
    const client = new ApiClient({ baseUrl: "http://localhost:8000", fetchImpl });

    const stats = await client.stats();

    expect(stats).toEqual({ predictions: 12, paper_trades: 3, unknown_counter: 2 });
    expect(fetchImpl).toHaveBeenCalledWith("http://localhost:8000/stats", expect.objectContaining({ method: "GET" }));
  });

  it("encodes snapshot filters and sends the explicit analysis body", async () => {
    const fetchImpl = vi
      .fn<FetchMock>()
      .mockResolvedValueOnce(response({ symbol: "NVDA", timeframe: "4h", bars: [] }))
      .mockResolvedValueOnce(response({ signal: { action: "WAIT" } }));
    const client = new ApiClient({ baseUrl: "http://localhost:8000", fetchImpl });
    const controller = new AbortController();

    await client.instrumentSnapshot("NVDA", { timeframe: "4h", limit: 120 }, controller.signal);
    await client.analysis("NVDA", { timeframe: "4h", limit: 120 }, controller.signal);

    expect(fetchImpl).toHaveBeenNthCalledWith(
      1,
      "http://localhost:8000/instruments/NVDA/snapshot?timeframe=4h&limit=120",
      expect.objectContaining({ method: "GET", signal: controller.signal }),
    );
    expect(fetchImpl).toHaveBeenNthCalledWith(
      2,
      "http://localhost:8000/analysis/NVDA",
      expect.objectContaining({
        method: "POST",
        signal: controller.signal,
        body: JSON.stringify({ timeframe: "4h", limit: 120 }),
      }),
    );
  });

  it("keeps workflow detail, performance bucket and PaperTrade follow routes typed", async () => {
    const fetchImpl = vi
      .fn<FetchMock>()
      .mockResolvedValueOnce(response({ prediction_id: "p-1", status: "OPEN" }))
      .mockResolvedValueOnce(response({ scope: { source_type: "live" }, confidence_buckets: [] }))
      .mockResolvedValueOnce(response({ prediction_id: "p-1", status: "OPEN", real_order: false }));
    const client = new ApiClient({ baseUrl: "http://localhost:8000", fetchImpl });

    await client.paperTrade("p/1");
    await client.performanceBuckets({ source_type: "replay", model_id: "model-x" });
    await client.followPrediction("p/1", {}, new AbortController().signal);

    expect(fetchImpl).toHaveBeenNthCalledWith(1, "http://localhost:8000/paper-trades/p%2F1", expect.objectContaining({ method: "GET" }));
    expect(fetchImpl).toHaveBeenNthCalledWith(2, "http://localhost:8000/performance/buckets?source_type=replay&model_id=model-x", expect.objectContaining({ method: "GET" }));
    expect(fetchImpl).toHaveBeenNthCalledWith(3, "http://localhost:8000/predictions/p%2F1/follow", expect.objectContaining({ method: "POST", body: "{}" }));
  });

  it("encodes replay run filters and the selected run path", async () => {
    const fetchImpl = vi
      .fn<FetchMock>()
      .mockResolvedValueOnce(response([{ run_id: "run/7", status: "COMPLETED_WITH_ERRORS" }]))
      .mockResolvedValueOnce(response({ run_id: "run/7", status: "COMPLETED_WITH_ERRORS", samples: [] }));
    const client = new ApiClient({ baseUrl: "http://localhost:8000", fetchImpl });
    const controller = new AbortController();

    await client.replayRuns({ status: "COMPLETED_WITH_ERRORS", limit: 50, offset: 100 }, controller.signal);
    await client.replayRun("run/7", controller.signal);

    expect(fetchImpl).toHaveBeenNthCalledWith(
      1,
      "http://localhost:8000/replay/runs?status=COMPLETED_WITH_ERRORS&limit=50&offset=100",
      expect.objectContaining({ method: "GET", signal: controller.signal }),
    );
    expect(fetchImpl).toHaveBeenNthCalledWith(
      2,
      "http://localhost:8000/replay/runs/run%2F7",
      expect.objectContaining({ method: "GET", signal: controller.signal }),
    );
  });
});
