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
  ChartAnnotationsResponse,
  ChartBarsResponse,
  ContextHealthResponse,
  ConsultStreamEvent,
  MarketContextResponse,
  MonitoringPolicy,
  MonitoringResponse,
  MonitoringRuntimeStatus,
  OpportunityAnalysesResponse,
  MarketIntelligenceResponse,
  DailyBriefResponse,
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
  RealtimeMarketResponse,
  ReleaseHealthResponse,
  RadarResponse,
  ReplayRun,
  StatsResponse,
  TriggerEvent,
  SchedulerHistory,
  SchedulerStatus,
  WatchlistEntry,
} from "../api/types";

export const fakeHealth: HealthResponse = {
  status: "ok",
  phase: 7,
  api_version: "1.2.1",
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
  { key: "ui.language", value: "en", default_value: "en", value_type: "string", updated_at: null, source: "default", description: "Preferred display language." },
  { key: "ai.response_language", value: "follow_ui", default_value: "follow_ui", value_type: "string", updated_at: null, source: "default", description: "Preferred local model response language." },
  { key: "notifications.language", value: "en", default_value: "en", value_type: "string", updated_at: null, source: "default", description: "Local alert display language." },
  { key: "ai.model_preference", value: "auto", default_value: "auto", value_type: "string", updated_at: null, source: "default", description: "Deterministic local model tier preference." },
  { key: "desktop.close_to_tray", value: false, default_value: false, value_type: "boolean", updated_at: null, source: "default", description: "Keep the desktop window hidden in the system tray when its close action is used." },
  { key: "desktop.auto_start", value: false, default_value: false, value_type: "boolean", updated_at: null, source: "default", description: "Start the desktop application with Windows only after explicit user opt-in." },
  { key: "monitoring.resume", value: false, default_value: false, value_type: "boolean", updated_at: null, source: "default", description: "Resume explicitly enabled monitoring policies after a user-started application session." },
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
  model_id: "Bonsai-2-27B-PTQ1_0",
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
  model_id: "Bonsai-2-27B-PTQ1_0",
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

export const fakeReleaseHealth: ReleaseHealthResponse = {
  status: "ok",
  phase: 7,
  api_version: "1.2.1",
  database: { available: true, schema_version: 13, path: "market_analyst.sqlite3" },
  backup: { available: true, format_version: "phase7_backup_v1", restore_requires_explicit_command: true },
  capabilities: {
    local_only: true,
    cloud_required: false,
    scheduler_default_enabled: false,
    external_notifications: false,
    qwen_consult: {
      contract_version: "qwen_consult_v2",
      configured: true,
      provider: "bonsai_llama_server",
      model_id: "Bonsai-2-27B-PTQ1_0",
      endpoint_scope: "loopback_only",
      streaming: "ndjson",
      conversation_storage: "browser_session_only",
      database_writes: false,
    },
  },
};

export const fakeContextHealth: ContextHealthResponse = {
  phase: 7,
  api_version: "1.2.1",
  benchmark: { context_version: "benchmark_context_v1", mapping_version: "benchmark_mapping_v1" },
  events: { schema_version: "event_schema_v1", cluster_version: "event_cluster_v1" },
  memory: { version: "market_memory_v1", feature_version: "feature_representation_v1", retention_limit: 1000 },
  time_policy: { owner: "python", llm_override: false },
  read_only_get: true,
  cloud_required: false,
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

export const fakeMonitoringPolicy: MonitoringPolicy = {
  contract_version: "monitoring_policy_v1",
  instrument_id: "BTCUSDT",
  enabled: false,
  primary_timeframe: "15m",
  context_timeframe: "1h",
  trigger_types: ["regime", "breakout", "breakdown", "volume", "volatility", "level_proximity", "invalidation", "event_risk", "news_shock"],
  min_trigger_score: 0.65,
  ai_min_confidence: 0.6,
  cooldown_minutes: 60,
  quiet_hours: {},
  notify: { desktop: true, sound: false },
  created_at: null,
  updated_at: null,
};

export const fakeMonitoring: MonitoringResponse = {
  contract_version: "monitoring_policy_v1",
  policy_version: "trigger_policy_v2",
  policies: [fakeMonitoringPolicy],
  realtime: [],
  runtime: {
    contract_version: "monitoring_runtime_v1",
    state: "stopped",
    active: false,
    worker_alive: false,
    active_symbols: [],
    max_symbols: 50,
    resource: { max_symbols: 50, active_symbols: 0, bounded: true },
    stream: { status: "stopped", provider: "binance_public_ws", symbols: [], timeframe: "15m" },
    run_count: 0,
    last_cycle_at: null,
    last_cycle_status: null,
    last_error: null,
    consecutive_failures: 0,
    retry_after_at: null,
    started_at: null,
    transition_at: "2030-01-02T12:00:00Z",
    resume_eligible: false,
    last_reason: "startup_no_scan",
  },
  defaults: { enabled: false, auto_start: false, resume: false },
  capabilities: { explicit_opt_in: true, startup_side_effect: false },
};

export const fakeMonitoringRuntime: MonitoringRuntimeStatus = fakeMonitoring.runtime;

export const fakeTriggerEvent: TriggerEvent = {
  trigger_event_id: "trigger-fixture",
  instrument_id: "BTCUSDT",
  timeframe: "15m",
  bar_start: "2030-01-02T12:00:00Z",
  bar_end: "2030-01-02T12:15:00Z",
  trigger_type: "breakout",
  trigger_score: 0.81,
  fingerprint: "fixture-trigger-fingerprint",
  policy_version: "trigger_policy_v2",
  status: "ANALYZED",
  analysis_status: "PREDICTION_SAVED",
  payload: { reason: "fixture" },
  created_at: "2030-01-02T12:15:00Z",
  updated_at: "2030-01-02T12:15:00Z",
};

export const fakeOpportunityAnalyses: OpportunityAnalysesResponse = {
  contract_version: "opportunity_analysis_v1",
  validator: "python",
  model_tier: "smart_9b_only",
  analyses: [],
};

export const fakeChartBars: ChartBarsResponse = {
  contract_version: "chart_data_v1",
  symbol: "BTCUSDT",
  timeframe: "15m",
  bars: [
    { timestamp: "2030-01-02T12:00:00Z", bar_start: "2030-01-02T12:00:00Z", bar_end: "2030-01-02T12:15:00Z", is_closed: true, open: 100, high: 102, low: 99, close: 101, volume: 1200 },
    { timestamp: "2030-01-02T12:15:00Z", bar_start: "2030-01-02T12:15:00Z", bar_end: "2030-01-02T12:30:00Z", is_closed: true, open: 101, high: 103, low: 100, close: 102, volume: 1400 },
  ],
  data_as_of: "2030-01-02T12:30:00Z",
  provider: { provider: "fixture_market", fetched_at: "2030-01-02T12:30:00Z", data_as_of: "2030-01-02T12:30:00Z", stale: false, error_code: null },
  fetched_at: "2030-01-02T12:30:00Z",
  tradingview: { library: "lightweight-charts", backend_api: false },
};

export const fakeChartAnnotations: ChartAnnotationsResponse = {
  contract_version: "chart_annotations_v1",
  symbol: "BTCUSDT",
  timeframe: "15m",
  annotations: [],
};

export const fakeRealtimeMarket: RealtimeMarketResponse = {
  contract_version: "crypto_realtime_v1",
  symbol: "BTCUSDT",
  provider: { provider: "fixture_market", fetched_at: "2030-01-02T12:30:00Z", data_as_of: "2030-01-02T12:30:00Z", stale: false, error_code: null },
  quote: { timestamp: "2030-01-02T12:30:00Z", price: 102, change_pct: 1.5, volume: 10000 },
  bars: fakeChartBars.bars,
  freshness: { symbol: "BTCUSDT", freshness_status: "fresh", data_as_of: "2030-01-02T12:30:00Z", age_seconds: 0 },
};

export const fakeMarketContext: MarketContextResponse = {
  symbol: "NVDA",
  timeframe: "1h",
  response_time: "2030-01-02T12:05:00Z",
  data_as_of: "2030-01-02T12:00:00Z",
  provider_snapshot: fakeSnapshot.provider_snapshot,
  benchmark_context: {
    version: "benchmark_context_v1",
    status: "available",
    benchmark: { benchmark_symbol: "SOXX", mapping_version: "benchmark_mapping_v1", status: "mapped" },
    provider: "fixture",
    relative_performance: 0.012,
    relative_strength: 0.12,
    capability: { future_bars_excluded: true },
  },
  events: {
    schema_version: "event_schema_v1",
    cluster_version: "event_cluster_v1",
    credibility_version: "source_credibility_v1",
    available: true,
    events: [],
    clusters: [],
    capability: { point_in_time: true, future_evidence_excluded: true },
  },
  market_memory: {
    version: "market_memory_v1",
    feature_version: "feature_representation_v1",
    status: "preliminary",
    eligible_sample_count: 0,
    resolved_sample_count: 0,
    similar_count: 0,
    capability: { reason: "insufficient_point_in_time_resolved_samples", future_evidence_excluded: true },
  },
  time_policy: { event_risk: false, reason_codes: ["timeframe_baseline"] },
  provenance: { read_only: true, prediction_created: false, memory_materialized: false },
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

export const fakeDailyBrief: DailyBriefResponse = {
  contract_version: "daily_brief_v1",
  status: "available",
  brief: {
    brief_id: "brief-fixture",
    generated_at: "2030-01-02T12:10:00Z",
    as_of: "2030-01-02T12:00:00Z",
    language: "en",
    model_id: "Bonsai-2-27B-PTQ1_0",
    model_tier: "smart",
    route_reason: "auto_smart_for_deep_research_task",
    sources: ["durable_watchlist", "latest_saved_live_predictions"],
    missing: ["news_unavailable"],
    content: "Saved evidence is constructive, while news coverage remains unavailable.",
  },
};

export const fakeMarketIntelligence: MarketIntelligenceResponse = {
  contract_version: "market_intelligence_v1",
  as_of: "2030-01-02T12:00:00Z",
  read_only: true,
  provider_calls: false,
  domain_writes: false,
  pulse: [
    { symbol: "BTCUSDT", status: "degraded", price: 66842.1, change_pct: 2.35, freshness: { status: "stale", data_as_of: "2030-01-02T10:00:00Z" }, missing_reasons: [] },
    { symbol: "ETHUSDT", status: "degraded", price: 3245.6, change_pct: 1.68, freshness: { status: "stale", data_as_of: "2030-01-02T10:00:00Z" }, missing_reasons: [] },
    { symbol: "NVDA", status: "available", price: 124.3, change_pct: 3.21, freshness: { status: "fresh", data_as_of: "2030-01-02T12:00:00Z" }, missing_reasons: [] },
    { symbol: "NASDAQ", status: "unavailable", price: null, change_pct: null, freshness: { status: "unavailable" }, missing_reasons: ["unsupported_or_no_saved_prediction"] },
    { symbol: "VIX", status: "unavailable", price: null, change_pct: null, freshness: { status: "unavailable" }, missing_reasons: ["unsupported_or_no_saved_prediction"] },
    { symbol: "US10Y", status: "unavailable", price: null, change_pct: null, freshness: { status: "unavailable" }, missing_reasons: ["unsupported_or_no_saved_prediction"] },
  ],
  calendar: {
    status: "available",
    events: [{ event_id: "event-fixture", title: "Saved macro event", source: "fixture", category: "macro", event_at: "2030-01-02T14:00:00Z", known_at: "2030-01-02T11:00:00Z", importance: 80, affected_symbols: ["NVDA", "BTCUSDT"] }],
    missing_reasons: [],
    capabilities: { macro_fields: "only_when_stored_by_provider" },
  },
  watchlist: [{ symbol: "NVDA", asset_type: "equity", action: "LONG", prediction_id: fakePrediction.prediction_id, summary: fakePrediction.summary, raw_confidence: 0.72, calibrated_confidence: 0.64, market_regime: "bull_trend", freshness: { status: "fresh" }, status: "monitoring" }],
  latest_signal: fakePrediction,
  heatmap: {
    status: "available",
    cells: [
      { symbol: "NVDA", asset_type: "equity", group: "semiconductors", change_pct: 3.21, price: 124.3, status: "available", missing_reasons: [] },
      { symbol: "AAPL", asset_type: "equity", group: "ai_technology", change_pct: null, price: null, status: "unavailable", missing_reasons: ["price_missing"] },
      { symbol: "BTCUSDT", asset_type: "crypto", group: "crypto_majors", change_pct: 2.35, price: 66842.1, status: "degraded", missing_reasons: [] },
    ],
    capability: "saved_prediction_context_only",
    taxonomy_version: "market_taxonomy_v1",
  },
  news: { status: "unavailable", items: [], missing_reasons: ["no_stored_point_in_time_news"] },
  daily_brief: fakeDailyBrief.brief,
  hydration: {
    contract_version: "public_hydration_v1",
    enabled: true,
    state: "ready",
    worker_alive: true,
    market_symbols: ["BTCUSDT", "ETHUSDT", "SOLUSDT", "NVDA", "NASDAQ", "VIX", "US10Y"],
    news_symbols: ["BTCUSDT", "NVDA"],
    sources: { market: { BTCUSDT: "binance_public" }, news: { "news:BTCUSDT": "rss" } },
    last_refresh_at: "2030-01-02T12:00:00Z",
    last_success_at: "2030-01-02T12:00:00Z",
    last_run_id: "hydration-fixture",
    run_count: 1,
    next_refresh_at: "2030-01-02T12:05:00Z",
    last_error: null,
    last_errors: [],
    cache: { symbols: ["BTCUSDT", "ETHUSDT", "SOLUSDT", "NVDA"], fresh_symbols: 4, stale_symbols: 0 },
    provider_calls: 9,
    domain_writes: 0,
  },
  capabilities: { live_fetch_on_get: false, public_hydration: "sidecar_owned" },
};

export function createFakeClient(overrides: Partial<ApplicationShellApiClient> = {}): ApplicationShellApiClient {
  const defaults: ApplicationShellApiClient = {
    health: vi.fn().mockResolvedValue(fakeHealth),
    releaseHealth: vi.fn().mockResolvedValue(fakeReleaseHealth),
    providerHealth: vi.fn().mockResolvedValue(fakeProviderHealth),
    contextHealth: vi.fn().mockResolvedValue(fakeContextHealth),
    modelHealth: vi.fn().mockResolvedValue({
      provider: "bonsai_llama_server",
      available: true,
      model_id: "Bonsai-2-27B-PTQ1_0",
      actual_model_id: "Ternary-Bonsai-2-27B-PTQ1_0.gguf",
      model_identity_source: "verified_manifest",
      model_available: true,
      consult: { contract_version: "qwen_consult_v2", configured: true, available: true, model_id: "Bonsai-2-27B-PTQ1_0", models: { fast: "Bonsai-2-27B-PTQ1_0", smart: "Bonsai-2-27B-PTQ1_0" } },
    }),
    marketIntelligence: vi.fn().mockResolvedValue(fakeMarketIntelligence),
    hydrationStatus: vi.fn().mockResolvedValue(fakeMarketIntelligence.hydration),
    refreshHydration: vi.fn().mockResolvedValue(fakeMarketIntelligence.hydration),
    dailyBrief: vi.fn().mockResolvedValue(fakeDailyBrief),
    generateDailyBrief: vi.fn().mockResolvedValue(fakeDailyBrief),
    consultStream: vi.fn(async (_request, onEvent: (event: ConsultStreamEvent) => void) => {
      onEvent({
        type: "meta",
        contract_version: "qwen_consult_v2",
        request_id: "fake-consult-request",
        provider: "bonsai_llama_server",
        model_id: "Bonsai-2-27B-PTQ1_0",
        model_tier: "smart",
        model_route: { reason: "auto_smart_for_deep_research_task" },
        symbol: "NVDA",
        context: {
          status: "available",
          symbol: "NVDA",
          as_of: "2030-01-02T12:00:00Z",
          freshness: { status: "fresh", age_seconds: 0 },
          sources: ["durable_latest_live_prediction"],
          missing_reasons: [],
          read_only: true,
        },
      });
      onEvent({ type: "delta", content: "Fixture " });
      onEvent({ type: "delta", content: "local model answer." });
      onEvent({
        type: "done",
        finish_reason: "stop",
        output_chars: 20,
        model_receipt: {
          model_id: "Bonsai-2-27B-PTQ1_0",
          actual_model_id: "Ternary-Bonsai-2-27B-PTQ1_0.gguf",
          verified_manifest_model_id: "Ternary-Bonsai-2-27B-PTQ1_0.gguf",
          model_identity_source: "request_bound_to_verified_manifest",
        },
      });
    }),
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
    realtimeMarket: vi.fn().mockResolvedValue(fakeRealtimeMarket),
    monitoring: vi.fn().mockResolvedValue(fakeMonitoring),
    monitoringStatus: vi.fn().mockResolvedValue(fakeMonitoringRuntime),
    startMonitoring: vi.fn().mockResolvedValue({ ...fakeMonitoringRuntime, state: "running", active: true, worker_alive: true }),
    resumeMonitoring: vi.fn().mockResolvedValue({ ...fakeMonitoringRuntime, state: "running", active: true, worker_alive: true }),
    pauseMonitoring: vi.fn().mockResolvedValue({ ...fakeMonitoringRuntime, state: "paused", active: false, worker_alive: false }),
    stopMonitoring: vi.fn().mockResolvedValue(fakeMonitoringRuntime),
    monitoringOpportunities: vi.fn().mockResolvedValue(fakeOpportunityAnalyses),
    updateMonitoringPolicy: vi.fn().mockResolvedValue(fakeMonitoringPolicy),
    runMonitoring: vi.fn().mockResolvedValue({ contract_version: "monitoring_policy_v1", status: "DISABLED", items: [] }),
    triggerEvents: vi.fn().mockResolvedValue({ policy_version: "trigger_policy_v2", events: [fakeTriggerEvent] }),
    chartBars: vi.fn().mockResolvedValue(fakeChartBars),
    chartAnnotations: vi.fn().mockResolvedValue(fakeChartAnnotations),
    localizedNews: vi.fn().mockResolvedValue(fakeNews),
    translateNews: vi.fn().mockResolvedValue({ status: "translated", numeric_guard_passed: true }),
    instrumentSnapshot: vi.fn().mockResolvedValue(fakeSnapshot),
    instrumentNews: vi.fn().mockResolvedValue(fakeNews),
    instrumentContext: vi.fn().mockResolvedValue(fakeMarketContext),
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
