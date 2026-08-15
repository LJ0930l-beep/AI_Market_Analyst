import { vi } from "vitest";

import type { ApplicationShellApiClient } from "../api/client";
import type { HealthResponse, Instrument, Prediction, ProviderHealthResponse, StatsResponse } from "../api/types";

export const fakeHealth: HealthResponse = {
  status: "ok",
  phase: 4,
  api_version: "0.4.0",
  product: "AI Market Analyst",
  real_orders: false,
  private_keys: false,
};

export const fakeInstrument: Instrument = {
  symbol: "NVDA",
  asset_type: "equity",
  exchange: "NASDAQ",
  currency: "USD",
  quote_currency: "USD",
  timezone: "America/New_York",
  trading_hours: "regular",
  sector: "Technology",
};

export const fakePrediction: Prediction = {
  prediction_id: "prediction-latest",
  symbol: "NVDA",
  action: "LONG",
  timeframe: "1h",
  source_type: "live",
  generated_at: "2030-01-02T12:00:00Z",
  signal_valid_until: "2030-01-02T13:00:00Z",
  raw_confidence: 0.72,
  calibrated_confidence: 0.64,
  model_id: "qwen3.5:4b",
};

export const fakeProviderHealth: ProviderHealthResponse = {
  available: true,
  routes: [
    { symbol: "NVDA", asset_type: "equity", providers: ["fixture_market"], mode: "fixture" },
  ],
  news: { provider: "fixture_news", configured: true, probe: "deferred_until_symbol_request" },
};

export const fakeStats: StatsResponse = {
  predictions: 12,
  paper_trades: 3,
  outcomes: 2,
  unknown_counter: 2,
};

export function createFakeClient(overrides: Partial<ApplicationShellApiClient> = {}): ApplicationShellApiClient {
  const defaults: ApplicationShellApiClient = {
    health: vi.fn().mockResolvedValue(fakeHealth),
    providerHealth: vi.fn().mockResolvedValue(fakeProviderHealth),
    modelHealth: vi.fn().mockResolvedValue({ provider: "ollama", available: true }),
    stats: vi.fn().mockResolvedValue(fakeStats),
    instruments: vi.fn().mockResolvedValue([fakeInstrument]),
    predictions: vi.fn().mockResolvedValue([fakePrediction]),
    performanceSummary: vi.fn().mockResolvedValue({
      scope: { source_type: "live" },
      sample_count: 2,
      actionable_count: 2,
      metrics: { resolved_actionable: 2, hit_rate: 0.5, zero_metric: 0 },
    }),
  };
  return { ...defaults, ...overrides };
}
