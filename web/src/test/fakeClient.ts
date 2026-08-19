import { vi } from "vitest";

import type { ApplicationShellApiClient } from "../api/client";
import type {
  AnalysisResult,
  CalibrationCurrent,
  HealthResponse,
  Instrument,
  InstrumentNews,
  MarketSnapshot,
  Outcome,
  PaperTrade,
  PerformanceBuckets,
  PerformanceSummary,
  Prediction,
  ProviderHealthResponse,
  StatsResponse,
} from "../api/types";

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
  prompt_version: "phase2-json-v8",
  parse_status: "OK",
  summary: "Fixture signal for deterministic UI tests.",
};

export const fakePaperTrade: PaperTrade = {
  prediction_id: fakePrediction.prediction_id,
  status: "OPEN",
  followed_at: "2030-01-02T12:10:00Z",
  prediction: fakePrediction,
  outcome: null,
  outcome_status: null,
};

export const fakeOutcome: Outcome = {
  prediction_id: fakePrediction.prediction_id,
  status: "TP1",
  settled_at: "2030-01-02T14:00:00Z",
  prediction: fakePrediction,
  paper_trade: fakePaperTrade,
  outcome_status: "TP1",
};

export const fakePerformanceSummary: PerformanceSummary = {
  scope: { source_type: "live" },
  status: "PRELIMINARY",
  source_type: "live",
  sample_count: 2,
  actionable_count: 2,
  metrics: { resolved_actionable: 2, hit_rate: 0.5, zero_metric: 0 },
};

export const fakePerformanceBuckets: PerformanceBuckets = {
  scope: { source_type: "live" },
  confidence_buckets: [],
};

export const fakeCalibrationCurrent: CalibrationCurrent = {
  status: "INSUFFICIENT_SAMPLE",
  sample_count: 0,
  buckets: [],
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

export const fakeSnapshot: MarketSnapshot = {
  symbol: "NVDA",
  timeframe: "1h",
  response_time: "2030-01-02T12:05:00Z",
  data_as_of: "2030-01-02T12:00:00Z",
  provider_snapshot: {
    provider: "fixture_market",
    fetched_at: "2030-01-02T12:05:00Z",
    data_as_of: "2030-01-02T12:00:00Z",
    stale: false,
    error_code: null,
  },
  quote: { timestamp: "2030-01-02T12:00:00Z", price: 100, change_pct: 1.2, high: 102, low: 98 },
  quant: { symbol: "NVDA", timeframe: "1h", price: 100, support: 96, resistance: 104, market_regime: "trend" },
  time_policy: null,
  bars: [
    { timestamp: "2030-01-02T11:00:00Z", open: 98, high: 101, low: 97, close: 100, volume: 1200 },
    { timestamp: "2030-01-02T12:00:00Z", open: 100, high: 102, low: 99, close: 101, volume: 1400 },
  ],
};

export const fakeNews: InstrumentNews = {
  provider: "fixture_news",
  fetched_at: "2030-01-02T12:05:00Z",
  available: true,
  error_code: null,
  events: [],
  clusters: [],
};

export const fakeAnalysis: AnalysisResult = {
  instrument: { symbol: "NVDA", asset_type: "equity", exchange: "NASDAQ", quote_currency: "USD" },
  timeframe: "1h",
  response_time: "2030-01-02T12:06:00Z",
  data_as_of: "2030-01-02T12:00:00Z",
  provider_snapshot: fakeSnapshot.provider_snapshot,
  quote: fakeSnapshot.quote,
  quant: fakeSnapshot.quant,
  news: fakeNews,
  time_policy: null,
  model: { provider: "none", available: false, error_code: "MODEL_NOT_CONFIGURED" },
  signal: {
    prediction_id: "prediction-analysis",
    action: "WAIT",
    generated_at: "2030-01-02T12:06:00Z",
    signal_valid_until: "2030-01-02T13:06:00Z",
    reevaluate_at: "2030-01-02T12:21:00Z",
    raw_confidence: 0.4,
    source_type: "live",
    model_id: "none",
    parse_status: "MODEL_NOT_CONFIGURED",
    summary: "WAIT: local model is not configured.",
    reason_codes: ["MODEL_NOT_CONFIGURED"],
  },
};

export function createFakeClient(overrides: Partial<ApplicationShellApiClient> = {}): ApplicationShellApiClient {
  const defaults: ApplicationShellApiClient = {
    health: vi.fn().mockResolvedValue(fakeHealth),
    providerHealth: vi.fn().mockResolvedValue(fakeProviderHealth),
    modelHealth: vi.fn().mockResolvedValue({ provider: "ollama", available: true }),
    stats: vi.fn().mockResolvedValue(fakeStats),
    instruments: vi.fn().mockResolvedValue([fakeInstrument]),
    instrumentSnapshot: vi.fn().mockResolvedValue(fakeSnapshot),
    instrumentNews: vi.fn().mockResolvedValue(fakeNews),
    analysis: vi.fn().mockResolvedValue(fakeAnalysis),
    predictions: vi.fn().mockResolvedValue([fakePrediction]),
    prediction: vi.fn().mockResolvedValue(fakePrediction),
    paperTrades: vi.fn().mockResolvedValue([]),
    paperTrade: vi.fn().mockResolvedValue(fakePaperTrade),
    outcomes: vi.fn().mockResolvedValue([]),
    outcome: vi.fn().mockResolvedValue(fakeOutcome),
    performanceSummary: vi.fn().mockResolvedValue(fakePerformanceSummary),
    performanceBuckets: vi.fn().mockResolvedValue(fakePerformanceBuckets),
    calibrationCurrent: vi.fn().mockResolvedValue(fakeCalibrationCurrent),
    followPrediction: vi.fn().mockResolvedValue({
      prediction_id: fakePrediction.prediction_id,
      status: "OPEN",
      real_order: false,
    }),
  };
  return { ...defaults, ...overrides };
}
