import { describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError, type ApiErrorPayload, serializeQuery } from "./client";

type FetchMock = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("ApiClient", () => {
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
});
