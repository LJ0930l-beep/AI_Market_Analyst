import { describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError, getApiBaseUrl, getRealtimeStreamUrl, type ApiErrorPayload, serializeQuery } from "./client";

type FetchMock = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("ApiClient", () => {
  it("uses the non-colliding development API prefix and desktop loopback default", () => {
    expect(getApiBaseUrl(undefined, true)).toBe("/api");
    expect(getApiBaseUrl(undefined, false)).toBe("http://127.0.0.1:18765");
    expect(getApiBaseUrl("http://127.0.0.1:8000///", true)).toBe("http://127.0.0.1:8000");
    expect(getRealtimeStreamUrl("BTCUSDT", "15m", "http://127.0.0.1:18765", false)).toBe("ws://127.0.0.1:18765/market/realtime/BTCUSDT/stream?timeframe=15m");
    expect(getRealtimeStreamUrl("BTCUSDT", "1h", "/api", true)).toContain("/api/market/realtime/BTCUSDT/stream?timeframe=1h");
  });

  it("honors the Tauri-injected loopback port at request time", async () => {
    const fetchImpl = vi.fn<FetchMock>().mockImplementation(async () => response({ status: "ok" }));
    const client = new ApiClient({ fetchImpl });
    const previous = window.__AIMA_API_BASE_URL__;
    try {
      window.__AIMA_API_BASE_URL__ = "http://127.0.0.1:18769";
      await client.health();
      expect(fetchImpl).toHaveBeenLastCalledWith("http://127.0.0.1:18769/health", expect.anything());

      window.__AIMA_API_BASE_URL__ = "http://127.0.0.1:18770";
      await client.health();
      expect(fetchImpl).toHaveBeenLastCalledWith("http://127.0.0.1:18770/health", expect.anything());
    } finally {
      window.__AIMA_API_BASE_URL__ = previous;
    }
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

  it("preserves FastAPI detail codes used by runtime recovery", async () => {
    const fetchImpl = vi.fn<FetchMock>().mockResolvedValue(response({
      detail: "Runtime lease held by another instance: Runtime lease held by another instance",
    }, 409));
    const client = new ApiClient({ baseUrl: "http://localhost:8000", fetchImpl });

    await expect(client.v2("/ai-session/start", "POST")).rejects.toMatchObject({
      status: 409,
      code: "Runtime lease held by another instance",
      message: "Runtime lease held by another instance",
    });
  });

  it("parses the Qwen NDJSON response incrementally without buffering the full answer", async () => {
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(`{"type":"meta","contract_version":"qwen_consult_v2","request_id":"r1","provider":"ollama","model_id":"Bonsai-2-27B-PTQ1_0","model_tier":"smart","model_route":{"version":"qwen_route_v1","reason":"auto_smart_task"},"context":{"status":"unavailable","sources":[],"missing_reasons":[],"read_only":true}}\n{"type":"delta","content":"first `));
        controller.enqueue(encoder.encode(`chunk"}\n{"type":"delta","content":"second"}\n{"type":"done","finish_reason":"stop","output_chars":18,"model_receipt":{"model_id":"Bonsai-2-27B-PTQ1_0","actual_model_id":"Ternary-Bonsai-2-27B-PTQ1_0.gguf","verified_manifest_model_id":"Ternary-Bonsai-2-27B-PTQ1_0.gguf","model_identity_source":"completion_response","raw_response":"must not reach UI"}}\n`));
        controller.close();
      },
    });
    const fetchImpl = vi.fn<FetchMock>().mockResolvedValue(new Response(stream, {
      status: 200,
      headers: { "content-type": "application/x-ndjson" },
    }));
    const client = new ApiClient({ baseUrl: "http://localhost:8000", fetchImpl });
    const events: string[] = [];
    const controller = new AbortController();

    await client.consultStream(
      { language: "en", messages: [{ role: "user", content: "question" }] },
      (event) => {
        events.push(event.type === "delta" ? event.content : event.type);
        if (event.type === "done") expect(event.model_receipt).toEqual({
          model_id: "Bonsai-2-27B-PTQ1_0",
          actual_model_id: "Ternary-Bonsai-2-27B-PTQ1_0.gguf",
          verified_manifest_model_id: "Ternary-Bonsai-2-27B-PTQ1_0.gguf",
          model_identity_source: "completion_response",
        });
      },
      controller.signal,
    );

    expect(events).toEqual(["meta", "first chunk", "second", "done"]);
    expect(fetchImpl).toHaveBeenCalledWith(
      "http://localhost:8000/consult/stream",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ language: "en", messages: [{ role: "user", content: "question" }] }),
        signal: controller.signal,
      }),
    );
  });

  it("rejects a Qwen stream that ends without a terminal event", async () => {
    const fetchImpl = vi.fn<FetchMock>().mockResolvedValue(new Response('{"type":"delta","content":"partial"}\n', {
      status: 200,
      headers: { "content-type": "application/x-ndjson" },
    }));
    const client = new ApiClient({ baseUrl: "http://localhost:8000", fetchImpl });

    await expect(client.consultStream(
      { language: "en", messages: [{ role: "user", content: "question" }] },
      () => undefined,
    )).rejects.toMatchObject({ code: "QWEN_STREAM_INTERRUPTED" });
  });

  it("rejects an unbounded Qwen stream event before it can grow indefinitely", async () => {
    const fetchImpl = vi.fn<FetchMock>().mockResolvedValue(new Response("x".repeat(65_537), {
      status: 200,
      headers: { "content-type": "application/x-ndjson" },
    }));
    const client = new ApiClient({ baseUrl: "http://localhost:8000", fetchImpl });

    await expect(client.consultStream(
      { language: "en", messages: [{ role: "user", content: "question" }] },
      () => undefined,
    )).rejects.toMatchObject({ code: "QWEN_STREAM_INVALID" });
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

  it("keeps durable watchlist and safe setting CRUD routes typed", async () => {
    const fetchImpl = vi
      .fn<FetchMock>()
      .mockResolvedValueOnce(response([]))
      .mockResolvedValueOnce(response({ symbol: "NVDA", instrument: { symbol: "NVDA" }, added_at: "now", updated_at: "now" }))
      .mockResolvedValueOnce(response({ symbol: "NVDA", instrument: { symbol: "NVDA" }, added_at: "now", updated_at: "later" }))
      .mockResolvedValueOnce(response({ symbol: "NVDA", deleted: true }))
      .mockResolvedValueOnce(response([{ key: "scheduler.enabled", value: false }]))
      .mockResolvedValueOnce(response({ key: "scheduler.enabled", value: true }))
      .mockResolvedValueOnce(response({ key: "scheduler.enabled", deleted: true, setting: { key: "scheduler.enabled", value: false } }));
    const client = new ApiClient({ baseUrl: "http://localhost:8000", fetchImpl });

    await client.watchlist();
    await client.addWatchlist("NVDA");
    await client.upsertWatchlist("NVDA");
    await client.removeWatchlist("NVDA");
    await client.appSettings();
    await client.updateAppSetting("scheduler.enabled", true);
    await client.resetAppSetting("scheduler.enabled");

    expect(fetchImpl).toHaveBeenNthCalledWith(1, "http://localhost:8000/watchlist", expect.objectContaining({ method: "GET" }));
    expect(fetchImpl).toHaveBeenNthCalledWith(2, "http://localhost:8000/watchlist", expect.objectContaining({ method: "POST", body: JSON.stringify({ symbol: "NVDA" }) }));
    expect(fetchImpl).toHaveBeenNthCalledWith(3, "http://localhost:8000/watchlist/NVDA", expect.objectContaining({ method: "PUT", body: JSON.stringify({ symbol: "NVDA" }) }));
    expect(fetchImpl).toHaveBeenNthCalledWith(4, "http://localhost:8000/watchlist/NVDA", expect.objectContaining({ method: "DELETE" }));
    expect(fetchImpl).toHaveBeenNthCalledWith(5, "http://localhost:8000/settings", expect.objectContaining({ method: "GET" }));
    expect(fetchImpl).toHaveBeenNthCalledWith(6, "http://localhost:8000/settings/scheduler.enabled", expect.objectContaining({ method: "PUT", body: JSON.stringify({ value: true }) }));
    expect(fetchImpl).toHaveBeenNthCalledWith(7, "http://localhost:8000/settings/scheduler.enabled", expect.objectContaining({ method: "DELETE" }));
  });

  it("keeps local scheduler status, history and lifecycle routes explicit", async () => {
    const fetchImpl = vi
      .fn<FetchMock>()
      .mockResolvedValueOnce(response({ state: "disabled", enabled: false, running: false }))
      .mockResolvedValueOnce(response({ runs: [], status: { state: "disabled" } }))
      .mockResolvedValueOnce(response({ state: "running", enabled: true, running: true }))
      .mockResolvedValueOnce(response({ run: { status: "COMPLETED" }, items: [], status: { state: "stopped" } }))
      .mockResolvedValueOnce(response({ state: "disabled", enabled: false, running: false }));
    const client = new ApiClient({ baseUrl: "http://localhost:8000", fetchImpl });

    await client.schedulerStatus();
    await client.schedulerHistory(5);
    await client.startScheduler();
    await client.runSchedulerOnce();
    await client.stopScheduler();

    expect(fetchImpl).toHaveBeenNthCalledWith(1, "http://localhost:8000/scheduler/status", expect.objectContaining({ method: "GET" }));
    expect(fetchImpl).toHaveBeenNthCalledWith(2, "http://localhost:8000/scheduler/history?limit=5", expect.objectContaining({ method: "GET" }));
    expect(fetchImpl).toHaveBeenNthCalledWith(3, "http://localhost:8000/scheduler/start", expect.objectContaining({ method: "POST" }));
    expect(fetchImpl).toHaveBeenNthCalledWith(4, "http://localhost:8000/scheduler/run-once", expect.objectContaining({ method: "POST" }));
    expect(fetchImpl).toHaveBeenNthCalledWith(5, "http://localhost:8000/scheduler/stop", expect.objectContaining({ method: "POST" }));
  });

  it("posts the narrow provider-validation registration payload", async () => {
    const fetchImpl = vi.fn<FetchMock>().mockResolvedValue(
      response({
        instrument: { symbol: "SOLUSDT", asset_type: "crypto" },
        registered: true,
        idempotent: false,
        validation: { status: "validated", mode: "public_probe" },
      }),
    );
    const client = new ApiClient({ baseUrl: "http://localhost:8000", fetchImpl });

    await client.registerInstrument("SOL", "crypto");

    expect(fetchImpl).toHaveBeenCalledWith(
      "http://localhost:8000/instruments/register",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ symbol: "SOL", asset_type: "crypto" }),
      }),
    );
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

  it("requests the read-only versioned Radar with stable filters", async () => {
    const fetchImpl = vi.fn<FetchMock>().mockResolvedValue(
      response({ scoring_version: "opportunity_v1", status: "empty", entries: [] }),
    );
    const client = new ApiClient({ baseUrl: "http://localhost:8000", fetchImpl });

    await client.radar({ asset_type: "crypto", category: "WAIT" });

    expect(fetchImpl).toHaveBeenCalledWith(
      "http://localhost:8000/radar?asset_type=crypto&category=WAIT",
      expect.objectContaining({ method: "GET" }),
    );
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
