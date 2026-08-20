import { vi } from "vitest";

import type { ApplicationShellApiClient } from "../api/client";
import type {
  AnalysisResult,
  AlertRecord,
  AlertStatusResponse,
  AlertsResponse,
  AssetType,
  AppSetting,
  CalibrationCurrent,
  HealthResponse,
  Instrument,
  InstrumentRegistrationResponse,
  InstrumentNews,
  MarketSnapshot,
  Outcome,
  PaperTrade,
  PerformanceBuckets,
  PerformanceSummary,
  Prediction,
  ProviderHealthResponse,
  RadarResponse,
  ReplayRun,
  StatsResponse,
  SchedulerHistory,
  SchedulerStatus,
  WatchlistEntry,
} from "../api/types";

export const fakeHealth: HealthResponse = {
  status: "ok",
  phase: 5,
  api_version: "0.5.0",
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

export const fakeWatchlistEntry: WatchlistEntry = {
  symbol: fakeInstrument.symbol,
  instrument: fakeInstrument,
  added_at: "2030-01-02T12:00:00Z",
  updated_at: "2030-01-02T12:00:00Z",
};

export const fakeInstrumentRegistration: InstrumentRegistrationResponse = {
  instrument: fakeInstrument,
  registered: true,
  idempotent: false,
  validation: {
    status: "validated",
    mode: "injected_test",
    provider: "e2e-public-probe",
    validated_at: "2030-01-02T12:00:00Z",
    data_as_of: "2030-01-02T11:59:00Z",
  },
};

export const fakeAppSettings: AppSetting[] = [
  {
    key: "scheduler.enabled",
    value: false,
    default_value: false,
    value_type: "boolean",
    updated_at: null,
    source: "default",
    description: "Explicit local scheduler opt-in; start remains a separate lifecycle action.",
  },
  {
    key: "scheduler.interval_seconds",
    value: 900,
    default_value: 900,
    value_type: "integer",
    updated_at: null,
    source: "default",
    description: "Local Watchlist scan interval used by the explicit scheduler lifecycle.",
  },
  {
    key: "scheduler.concurrency",
    value: 1,
    default_value: 1,
    value_type: "integer",
    updated_at: null,
    source: "default",
    description: "Requested local scan concurrency; model analysis is capped at one.",
  },
  {
    key: "scheduler.session_policy",
    value: "market_hours",
    default_value: "market_hours",
    value_type: "string",
    updated_at: null,
    source: "default",
    description: "Local Watchlist scan session policy; market_hours is the conservative default.",
  },
];

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

export const fakeRadar: RadarResponse = {
  scoring_version: "opportunity_v1",
  config: {
    version: "opportunity_v1",
    weights: {
      calibrated_confidence: 0.3,
      risk_reward: 0.2,
      freshness: 0.15,
      regime_alignment: 0.15,
      news_event_risk: 0.1,
      data_quality: 0.1,
    },
  },
  status: "ready",
  as_of: "2030-01-02T12:00:00Z",
  counts: { total: 1, ranking_eligible: 1, strong_opportunity: 1, watch: 0, avoid: 0, wait: 0 },
  entries: [{
    symbol: fakeInstrument.symbol,
    instrument: fakeInstrument,
    prediction_id: fakePrediction.prediction_id,
    action: "LONG",
    category: "STRONG_OPPORTUNITY",
    status: "ranked",
    ranking_eligible: true,
    score: 0.81,
    rank: 1,
    generated_at: fakePrediction.generated_at,
    data_as_of: "2030-01-02T11:55:00Z",
    signal_valid_until: fakePrediction.signal_valid_until,
    inputs: {
      raw_confidence: 0.72,
      calibrated_confidence: 0.64,
      risk_reward: 2.1,
      regime: "bull_trend",
      news_event_risk: "none",
      data_quality: "verified",
    },
    components: {},
    missing_reasons: [],
    degraded_reasons: [],
    provenance: { read_only: true, model_invoked: false, writes: false },
  }],
};

export const fakeReplayRun: ReplayRun = {
  run_id: "replay-fixture",
  status: "COMPLETED",
  created_at: "2030-01-02T10:00:00Z",
  completed_at: "2030-01-02T10:20:00Z",
  model_id: "qwen3.5:4b",
  prompt_version: "phase2-json-v8",
  symbols: ["NVDA"],
  timeframes: ["1h"],
  sampling_policy: {
    min_history_bars: 120,
    deterministic_seed: 7,
    order: "as_of_utc_then_symbol_then_timeframe",
    news_history_available: false,
  },
  manifest_hash: "manifest-fixture",
  counts: {
    planned: 1,
    sample_rows: 1,
    completed: 1,
    wait: 0,
    errors: 0,
    actionable: 1,
    resolved_actionable: 0,
    outcomes: 0,
  },
  config: { samples: 1, seed: 7, manifest_path: "data/fixture.manifest.json" },
  error_code: null,
  samples: [{
    run_id: "replay-fixture",
    prediction_id: "replay-prediction-fixture",
    symbol: "NVDA",
    timeframe: "1h",
    as_of: "2030-01-01T20:00:00Z",
    status: "COMPLETED",
    error_code: null,
    started_at: "2030-01-02T10:01:00Z",
    completed_at: "2030-01-02T10:02:00Z",
    capability_flags: { news_history_available: false, technical_only: true },
  }],
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

export const fakeSchedulerStatus: SchedulerStatus = {
  state: "disabled",
  enabled: false,
  running: false,
  thread_alive: false,
  interval_seconds: 900,
  configured_concurrency: 1,
  effective_concurrency: 1,
  session_policy: "market_hours",
  timeframe: "1h",
  last_run: null,
  next_run_at: null,
  resource: { available: true, reason: "ready", capability: "not_probed" },
  backoff: { active: false, attempts: 0, remaining_seconds: 0 },
  cache: {
    version: "context_cache_v1",
    entries: 0,
    persisted_metadata_entries: 0,
    restart_behavior: "metadata_only_cold_restart",
  },
  settlement: {
    version: "settlement_v1",
    status: "idle",
    counts: { planned: 0, settled: 0, pending: 0, wait: 0, errors: 0 },
    capability: { model_scan_resource_guard: "not_used" },
  },
  performance_refresh: {
    version: "live_performance_v1",
    status: "PRELIMINARY",
    capability: "live_records_only_no_calibration_mutation",
  },
  capabilities: {
    runtime: "local_thread_explicit_lifecycle",
    model_analysis_concurrency: 1,
    session_policy: "weekday_hours_only_no_holiday_calendar",
    crypto_24_7: true,
    real_orders: false,
    alerts: true,
    alert_policy: "alert_policy_v1",
    alert_mode: "local_observability_only",
    outcome_settlement: true,
    outcome_settlement_mode: "live_point_in_time_before_model_scan",
  },
};

export const fakeAlert: AlertRecord = {
  alert_id: "alert-prediction-fixture",
  policy_version: "alert_policy_v1",
  source: "prediction",
  severity: "INFO",
  status: "OPEN",
  title: "New LONG prediction · NVDA",
  message: "A new actionable LONG prediction is available for NVDA.",
  symbol: "NVDA",
  prediction_id: fakePrediction.prediction_id,
  event_identity: `prediction:${fakePrediction.prediction_id}`,
  fingerprint: "fixture-alert-fingerprint",
  evidence: { prediction_id: fakePrediction.prediction_id, action: "LONG", source_type: "live" },
  first_seen_at: "2030-01-02T12:01:00Z",
  last_seen_at: "2030-01-02T12:01:00Z",
  occurrence_count: 1,
  acknowledged_at: null,
  acknowledged_by: null,
  dedupe_key: `prediction:${fakePrediction.prediction_id}`,
};

export const fakeAlertStatus: AlertStatusResponse = {
  policy: {
    version: "alert_policy_v1",
    mode: "local_observability_only",
    retention_limit: 500,
    operational_cooldown_seconds: 900,
  },
  capabilities: {
    reconciliation: "after_settlement_and_scan",
    outbound_notifiers: false,
    broker_or_real_order: false,
  },
  counts: { total: 1, open: 1, unread: 1, acknowledged: 0 },
  last_reconciliation: {
    status: "COMPLETED",
    counts: { created: 1, deduped: 0, errors: 0 },
  },
};

export const fakeAlertsResponse: AlertsResponse = {
  policy: fakeAlertStatus.policy,
  capabilities: fakeAlertStatus.capabilities,
  alerts: [fakeAlert],
  counts: fakeAlertStatus.counts,
  pagination: { limit: 50, offset: 0, total: 1, filters: {} },
};

export const fakeSchedulerHistory: SchedulerHistory = {
  runs: [],
  status: fakeSchedulerStatus,
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
    schedulerStatus: vi.fn().mockResolvedValue(fakeSchedulerStatus),
    schedulerHistory: vi.fn().mockResolvedValue(fakeSchedulerHistory),
    alerts: vi.fn().mockResolvedValue(fakeAlertsResponse),
    alertStatus: vi.fn().mockResolvedValue(fakeAlertStatus),
    acknowledgeAlert: vi.fn().mockResolvedValue({ ...fakeAlert, status: "ACKNOWLEDGED", acknowledged_at: "2030-01-02T12:02:00Z", acknowledged_by: "local_user" }),
    startScheduler: vi.fn().mockResolvedValue({ ...fakeSchedulerStatus, state: "running", enabled: true, running: true, thread_alive: true }),
    stopScheduler: vi.fn().mockResolvedValue(fakeSchedulerStatus),
    runSchedulerOnce: vi.fn().mockResolvedValue({ run: null, items: [], status: fakeSchedulerStatus }),
    instruments: vi.fn().mockResolvedValue([fakeInstrument]),
    registerInstrument: vi.fn<(symbol: string, assetType: AssetType) => Promise<InstrumentRegistrationResponse>>().mockResolvedValue(fakeInstrumentRegistration),
    watchlist: vi.fn().mockResolvedValue([]),
    addWatchlist: vi.fn().mockResolvedValue(fakeWatchlistEntry),
    upsertWatchlist: vi.fn().mockResolvedValue(fakeWatchlistEntry),
    removeWatchlist: vi.fn().mockResolvedValue({ symbol: fakeInstrument.symbol, deleted: true }),
    appSettings: vi.fn().mockResolvedValue(fakeAppSettings),
    appSetting: vi.fn().mockResolvedValue(fakeAppSettings[0]),
    updateAppSetting: vi.fn().mockResolvedValue(fakeAppSettings[0]),
    resetAppSetting: vi.fn().mockResolvedValue({ key: fakeAppSettings[0].key, deleted: true, setting: fakeAppSettings[0] }),
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
    radar: vi.fn().mockResolvedValue(fakeRadar),
    replayRuns: vi.fn().mockResolvedValue([fakeReplayRun]),
    replayRun: vi.fn().mockResolvedValue(fakeReplayRun),
    followPrediction: vi.fn().mockResolvedValue({
      prediction_id: fakePrediction.prediction_id,
      status: "OPEN",
      real_order: false,
    }),
  };
  return { ...defaults, ...overrides };
}
