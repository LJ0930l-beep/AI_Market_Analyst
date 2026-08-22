export type JsonRecord = Record<string, unknown>;

export type Action = "LONG" | "SHORT" | "WAIT";
export type AssetType = "equity" | "crypto";
export type SourceType = "live" | "replay";
export type Timeframe = "5m" | "15m" | "1h" | "4h" | "1d";
export type OutcomeStatus = "TP1" | "TP2" | "STOP" | "TIMEOUT" | "INVALIDATED" | "PENDING";
export type ReplayStatus = "PENDING" | "RUNNING" | "COMPLETED" | "COMPLETED_WITH_ERRORS" | "FAILED";

export interface HealthResponse {
  status: string;
  phase: number;
  api_version: string;
  product: string;
  real_orders: false;
  private_keys: false;
}

export interface ReleaseHealthResponse extends JsonRecord {
  status: string;
  phase: number;
  api_version: string;
  database?: JsonRecord;
  backup?: JsonRecord;
  capabilities?: JsonRecord;
}

export interface ProviderHealthResponse extends JsonRecord {
  available?: boolean;
  routes?: JsonRecord[];
  news?: JsonRecord;
}

export interface ContextHealthResponse extends JsonRecord {
  phase?: number;
  api_version?: string;
  benchmark?: JsonRecord;
  events?: JsonRecord;
  memory?: JsonRecord;
  time_policy?: JsonRecord;
  read_only_get?: boolean;
  cloud_required?: boolean;
}

export interface ModelHealthResponse extends JsonRecord {
  provider?: string;
  available?: boolean;
  error_code?: string;
  consult?: ConsultCapability;
}

export type ConsultLanguage = "en" | "zh-CN";
export type ConsultRole = "user" | "assistant";

export interface ConsultMessage {
  role: ConsultRole;
  content: string;
}

export interface ConsultRequest {
  language: ConsultLanguage;
  messages: ConsultMessage[];
  symbol?: string;
  prediction_id?: string;
  model_preference?: "auto" | "fast" | "smart";
  task?: "assistant" | "signal_explanation" | "simple_summary" | "daily_brief" | "event_analysis" | "deep_research";
}

export interface ConsultCapability extends JsonRecord {
  contract_version?: string;
  configured?: boolean;
  available?: boolean;
  provider?: string;
  model_id?: string;
  models?: { fast?: string; smart?: string };
  endpoint_scope?: string;
  streaming?: string;
  availability?: string;
  conversation_storage?: string;
  database_writes?: boolean;
  model_concurrency?: number;
  error_code?: string;
  limits?: JsonRecord;
}

export interface ConsultContextEvidence extends JsonRecord {
  status?: string;
  symbol?: string | null;
  as_of?: string | null;
  freshness?: JsonRecord;
  sources?: string[];
  missing_reasons?: string[];
  evidence_hash?: string;
  read_only?: boolean;
}

export type ConsultStreamEvent =
  | (JsonRecord & {
      type: "meta";
      contract_version: string;
      request_id: string;
      provider: string;
      model_id: string;
      model_tier?: "fast" | "smart";
      model_route?: JsonRecord;
      symbol?: string | null;
      context: ConsultContextEvidence;
    })
  | { type: "delta"; content: string }
  | { type: "done"; finish_reason: string; output_chars: number }
  | { type: "error"; error: { code: string; message: string } };

export interface Instrument extends JsonRecord {
  symbol: string;
  asset_type: AssetType | string;
  exchange: string;
  currency: string;
  quote_currency: string;
  timezone: string;
  trading_hours: string;
  sector: string | null;
  registry_source?: "canonical" | "registered" | string;
  metadata_status?: string;
  metadata_labels?: Record<string, string>;
  validation_provider?: string | null;
  validated_at?: string | null;
}

export interface InstrumentRegistrationValidation extends JsonRecord {
  status: string;
  mode?: string;
  provider?: string;
  validated_at?: string;
  data_as_of?: string;
}

export interface InstrumentRegistrationResponse extends JsonRecord {
  instrument: Instrument;
  registered: boolean;
  idempotent: boolean;
  validation: InstrumentRegistrationValidation;
}

export interface WatchlistEntry extends JsonRecord {
  symbol: string;
  instrument: Instrument;
  added_at: string;
  updated_at: string;
}

export interface WatchlistDeleteResponse extends JsonRecord {
  symbol: string;
  deleted: boolean;
}

export type AppSettingKey =
  | "scheduler.enabled"
  | "scheduler.interval_seconds"
  | "scheduler.concurrency"
  | "scheduler.session_policy"
  | "ui.language"
  | "ai.response_language"
  | "notifications.language"
  | "ai.model_preference";

export interface MarketPulseItem extends JsonRecord {
  symbol: string;
  status: string;
  price: number | null;
  change_pct: number | null;
  freshness: JsonRecord;
  missing_reasons: string[];
}

export interface IntelligenceEvent extends JsonRecord {
  event_id?: string;
  title?: string;
  source?: string;
  category?: string;
  event_at?: string;
  known_at?: string;
  importance?: number;
  affected_symbols?: string[];
  forecast?: unknown;
  previous?: unknown;
  actual?: unknown;
  ai_view?: string | null;
  review?: string | null;
}

export interface HeatmapCell extends JsonRecord {
  symbol: string;
  asset_type: string;
  group: string;
  change_pct: number | null;
  price: number | null;
  status: string;
  missing_reasons: string[];
}

export interface MonitoringItem extends JsonRecord {
  symbol: string;
  asset_type?: string | null;
  action?: Action | null;
  prediction_id?: string | null;
  summary?: string | null;
  raw_confidence?: number | null;
  calibrated_confidence?: number | null;
  market_regime?: string | null;
  freshness: JsonRecord;
  status: string;
}

export interface DailyBrief extends JsonRecord {
  brief_id: string;
  generated_at: string;
  as_of: string;
  language: ConsultLanguage;
  model_id: string;
  model_tier: string;
  route_reason: string;
  sources: string[];
  missing: string[];
  content: string;
}

export interface MarketIntelligenceResponse extends JsonRecord {
  contract_version: string;
  as_of: string;
  read_only: boolean;
  provider_calls: boolean;
  domain_writes: boolean;
  pulse: MarketPulseItem[];
  calendar: { status: string; events: IntelligenceEvent[]; missing_reasons: string[]; capabilities: JsonRecord };
  watchlist: MonitoringItem[];
  latest_signal?: Prediction | null;
  heatmap: { status: string; cells: HeatmapCell[]; capability: string; taxonomy_version: string };
  news: { status: string; items: IntelligenceEvent[]; missing_reasons: string[] };
  daily_brief?: DailyBrief | null;
  capabilities: JsonRecord;
}

export interface DailyBriefResponse extends JsonRecord {
  contract_version: string;
  status: string;
  brief: DailyBrief | null;
}

export type AppSettingValue = boolean | number | string;
export type AppSettingValueType = "boolean" | "integer" | "string";

export interface AppSetting extends JsonRecord {
  key: AppSettingKey;
  value: AppSettingValue;
  default_value: AppSettingValue;
  value_type: AppSettingValueType;
  updated_at: string | null;
  source: "default" | "stored";
  description: string;
}

export interface AppSettingResetResponse extends JsonRecord {
  key: AppSettingKey;
  deleted: boolean;
  setting: AppSetting;
}

export interface ProviderSnapshot extends JsonRecord {
  provider?: string;
  fetched_at?: string;
  data_as_of?: string;
  stale?: boolean;
  error_code?: string | null;
}

export interface MarketQuote extends JsonRecord {
  timestamp?: string;
  price?: number;
  change_pct?: number | null;
  high?: number | null;
  low?: number | null;
}

export interface MarketBar extends JsonRecord {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface QuantFacts extends JsonRecord {
  symbol?: string;
  timeframe?: Timeframe;
  timestamp?: string;
  price?: number;
  ema20?: number;
  ema50?: number;
  rsi14?: number;
  macd?: number;
  macd_signal?: number;
  atr14?: number;
  volume_ratio?: number;
  support?: number;
  resistance?: number;
  market_regime?: string;
  trend_score?: number;
  momentum_score?: number;
}

export interface SnapshotFilters {
  timeframe?: Timeframe;
  limit?: number;
}

export interface MarketSnapshot extends JsonRecord {
  symbol: string;
  timeframe: Timeframe;
  response_time?: string;
  data_as_of?: string;
  provider_snapshot?: ProviderSnapshot;
  quote?: MarketQuote;
  quant?: QuantFacts;
  time_policy?: null;
  bars?: MarketBar[];
}

export interface NewsEvent extends JsonRecord {
  id?: string;
  source?: string;
  published_at?: string;
  title?: string;
  symbols?: string[];
  summary_raw?: string | null;
  url?: string | null;
  category?: string;
  sentiment?: number;
  importance?: number;
  credibility?: number;
  impact_horizon?: string;
  dedupe_hash?: string | null;
}

export interface NewsCluster extends JsonRecord {
  cluster_id?: string;
  symbols?: string[];
  title?: string;
  event_ids?: string[];
  sentiment?: number;
  importance?: number;
}

export interface InstrumentNews extends JsonRecord {
  provider?: string;
  fetched_at?: string;
  available?: boolean;
  error_code?: string | null;
  events?: NewsEvent[];
  clusters?: NewsCluster[];
}

export interface BenchmarkMetadata extends JsonRecord {
  symbol?: string;
  benchmark_symbol?: string;
  benchmark_asset_type?: AssetType | string;
  provider?: string;
  provider_symbol?: string;
  mapping_version?: string;
  relation?: string;
  status?: string;
  mapping_reason?: string;
  metadata_labels?: Record<string, string>;
  updated_at?: string | null;
}

export interface BenchmarkContext extends JsonRecord {
  version?: string;
  symbol?: string;
  timeframe?: string;
  as_of?: string;
  status?: string;
  benchmark?: BenchmarkMetadata;
  provider?: string;
  freshness?: JsonRecord;
  target_return?: number | null;
  benchmark_return?: number | null;
  relative_performance?: number | null;
  relative_strength?: number | null;
  capability?: JsonRecord;
  provenance?: JsonRecord;
}

export interface EventEvidence extends JsonRecord {
  event_id?: string;
  source?: string;
  source_type?: string;
  category?: string;
  event_at?: string;
  published_at?: string | null;
  known_at?: string | null;
  retrieved_at?: string | null;
  importance?: number;
  affected_symbols?: string[];
  title?: string;
  summary?: string | null;
  url?: string | null;
  primary_source?: boolean;
  reported_credibility?: number;
  credibility_score?: number;
  revision_known_at?: string | null;
  capability?: JsonRecord;
}

export interface EventClusterEvidence extends JsonRecord {
  cluster_id?: string;
  title?: string;
  affected_symbols?: string[];
  event_ids?: string[];
  source_count?: number;
  primary_source_count?: number;
  credibility_score?: number;
  importance?: number;
  consensus?: string;
  disagreement?: boolean;
  sources?: JsonRecord[];
  as_of?: string;
}

export interface EventIntelligence extends JsonRecord {
  schema_version?: string;
  cluster_version?: string;
  credibility_version?: string;
  symbol?: string;
  as_of?: string;
  provider?: string;
  fetched_at?: string;
  available?: boolean;
  error_code?: string | null;
  events?: EventEvidence[];
  clusters?: EventClusterEvidence[];
  capability?: JsonRecord;
  provenance?: JsonRecord;
}

export interface MarketMemoryMatch extends JsonRecord {
  prediction_id?: string;
  symbol?: string;
  generated_at?: string;
  distance?: number;
  outcome_status?: string | null;
  realized_r?: number | null;
  outcome_known_as_of?: string | null;
}

export interface MarketMemoryContext extends JsonRecord {
  version?: string;
  feature_version?: string;
  status?: string;
  query_symbol?: string;
  query_timeframe?: string;
  as_of?: string;
  eligible_sample_count?: number;
  resolved_sample_count?: number;
  similar_count?: number;
  win_rate?: number | null;
  avg_r?: number | null;
  typical_outcomes?: string[];
  matches?: MarketMemoryMatch[];
  capability?: JsonRecord;
  provenance?: JsonRecord;
}

export interface MarketContextResponse extends JsonRecord {
  symbol: string;
  timeframe: Timeframe;
  response_time?: string;
  data_as_of?: string;
  provider_snapshot?: ProviderSnapshot;
  quote?: MarketQuote;
  quant?: QuantFacts;
  benchmark_context?: BenchmarkContext;
  events?: EventIntelligence;
  market_memory?: MarketMemoryContext;
  time_policy?: TimePolicy | null;
  provenance?: JsonRecord;
}

export interface TimePolicy extends JsonRecord {
  timeframe?: string;
  signal_validity_minutes?: number[];
  holding_horizon_minutes?: number[];
  reevaluate_minutes?: number[];
  allowed_values?: JsonRecord;
  volatility_ratio?: number;
  market_regime?: string;
  event_risk?: boolean;
  reason_codes?: string[];
}

export interface ModelStatus extends JsonRecord {
  provider?: string;
  available?: boolean;
  error_code?: string | null;
  model_id?: string;
  model_version?: string | null;
  prompt_version?: string | null;
  parse_status?: string;
}

export interface AnalysisInstrument extends JsonRecord {
  symbol?: string;
  asset_type?: string;
  exchange?: string;
  currency?: string;
  quote_currency?: string;
  timezone?: string;
  trading_hours?: string;
  sector?: string;
}

export interface SignalProposal extends JsonRecord {
  prediction_id?: string;
  instrument?: AnalysisInstrument;
  analysis_timeframe?: Timeframe;
  generated_at?: string;
  action?: Action;
  entry_low?: number | null;
  entry_high?: number | null;
  stop?: number | null;
  tp1?: number | null;
  tp2?: number | null;
  signal_validity_minutes?: number | null;
  signal_valid_until?: string | null;
  expected_hold_minutes?: number | null;
  expected_hold_until?: string | null;
  max_hold_minutes?: number | null;
  max_hold_until?: string | null;
  reevaluate_at?: string | null;
  invalidation?: string[] | null;
  raw_confidence?: number | null;
  reason_codes?: string[];
  summary?: string;
  model_id?: string;
  model_version?: string | null;
  prompt_version?: string | null;
  input_hash?: string | null;
  data_as_of?: string | null;
  parse_status?: string;
  latency_ms?: number | null;
  source_type?: SourceType;
  replay_run_id?: string | null;
  calibrated_confidence?: number | null;
  calibration_version?: string | null;
  calibration_scope?: string | null;
  calibration_sample_size?: number | null;
  calibration_fallback?: string | null;
}

export interface StatsResponse extends JsonRecord {
  predictions?: number;
  paper_trades?: number;
  outcomes?: number;
  replay_runs?: number;
  replay_samples?: number;
  calibration_results?: number;
  watchlist_entries?: number;
  app_settings?: number;
  scheduler_runs?: number;
  scheduler_items?: number;
  scheduler_cache_entries?: number;
  benchmark_metadata?: number;
  phase6_events?: number;
  phase6_event_clusters?: number;
  market_memory_features?: number;
}

export interface SchedulerItem extends JsonRecord {
  item_id: string;
  run_id: string;
  symbol: string;
  timeframe: Timeframe | string;
  status: string;
  session_state?: string | null;
  skip_reason?: string | null;
  resource_reason?: string | null;
  cache_key?: string | null;
  cache_status?: string | null;
  error_code?: string | null;
  prediction_id?: string | null;
  stage?: "scan" | "settlement" | string;
  provider?: string | null;
  provider_as_of?: string | null;
  as_of?: string | null;
  capability?: JsonRecord | null;
  outcome_status?: OutcomeStatus | string | null;
  retry_after_at?: string | null;
}

export interface SchedulerRun extends JsonRecord {
  run_id: string;
  trigger: string;
  status: string;
  started_at: string;
  finished_at?: string | null;
  next_run_at?: string | null;
  settings?: JsonRecord;
  counts?: JsonRecord;
  error_code?: string | null;
  error_detail?: string | null;
  items?: SchedulerItem[];
}

export interface SchedulerStatus extends JsonRecord {
  state: string;
  enabled: boolean;
  running: boolean;
  thread_alive: boolean;
  active_run_id?: string | null;
  interval_seconds: number;
  configured_concurrency: number;
  effective_concurrency: number;
  session_policy: string;
  timeframe: string;
  last_run?: SchedulerRun | null;
  next_run_at?: string | null;
  last_error?: JsonRecord | null;
  resource?: JsonRecord;
  backoff?: JsonRecord;
  cache?: JsonRecord;
  settlement?: JsonRecord | null;
  performance_refresh?: JsonRecord | null;
  alerts?: JsonRecord | null;
  capabilities?: JsonRecord;
}

export interface SchedulerHistory extends JsonRecord {
  runs: SchedulerRun[];
  status: SchedulerStatus;
}

export type AlertSource = "prediction" | "outcome" | "radar" | "news_event" | "operational";
export type AlertSeverity = "INFO" | "WARNING" | "CRITICAL";
export type AlertStatus = "OPEN" | "ACKNOWLEDGED";

export interface AlertRecord extends JsonRecord {
  alert_id: string;
  policy_version: string;
  source: AlertSource | string;
  severity: AlertSeverity | string;
  status: AlertStatus | string;
  title: string;
  message: string;
  symbol?: string | null;
  prediction_id?: string | null;
  event_identity: string;
  fingerprint: string;
  evidence: JsonRecord;
  first_seen_at: string;
  last_seen_at: string;
  occurrence_count: number;
  acknowledged_at?: string | null;
  acknowledged_by?: string | null;
  dedupe_key: string;
}

export interface AlertCounts extends JsonRecord {
  total: number;
  open: number;
  unread: number;
  acknowledged: number;
}

export interface AlertPolicy extends JsonRecord {
  version: string;
  mode: string;
  retention_limit?: number;
  operational_cooldown_seconds?: number;
}

export interface AlertStatusResponse extends JsonRecord {
  policy: AlertPolicy;
  capabilities: JsonRecord;
  counts: AlertCounts;
  last_reconciliation?: JsonRecord | null;
}

export interface AlertFilters {
  limit?: number;
  offset?: number;
  source?: AlertSource;
  severity?: AlertSeverity;
  status?: AlertStatus;
}

export interface AlertsResponse extends JsonRecord {
  policy: AlertPolicy;
  capabilities: JsonRecord;
  alerts: AlertRecord[];
  counts: AlertCounts;
  pagination: JsonRecord & { limit: number; offset: number; total: number };
}

export type Prediction = JsonRecord & {
  prediction_id: string;
  symbol?: string;
  instrument?: AnalysisInstrument | null;
  action?: Action;
  timeframe?: Timeframe;
  analysis_timeframe?: Timeframe;
  source_type?: SourceType;
  replay_run_id?: string | null;
  generated_at?: string | null;
  data_as_of?: string | null;
  signal_valid_until?: string | null;
  reevaluate_at?: string | null;
  expected_hold_until?: string | null;
  max_hold_until?: string | null;
  signal_validity_minutes?: number | null;
  expected_hold_minutes?: number | null;
  max_hold_minutes?: number | null;
  raw_confidence?: number | null;
  calibrated_confidence?: number | null;
  calibration_version?: string | null;
  calibration_scope?: string | null;
  calibration_sample_size?: number | null;
  calibration_fallback?: string | null;
  entry_low?: number | null;
  entry_high?: number | null;
  stop?: number | null;
  tp1?: number | null;
  tp2?: number | null;
  invalidation?: string[] | null;
  reason_codes?: string[] | null;
  summary?: string | null;
  model_id?: string | null;
  model_version?: string | null;
  prompt_version?: string | null;
  parse_status?: string | null;
  input_hash?: string | null;
  paper_trade?: PaperTrade | null;
  outcome?: Outcome | null;
  outcome_status?: OutcomeStatus | null;
};

export type PaperTrade = JsonRecord & {
  prediction_id: string;
  status: string;
  followed_at?: string | null;
  outcome_status?: OutcomeStatus | null;
  prediction?: Prediction | null;
  outcome?: Outcome | null;
};

export type Outcome = JsonRecord & {
  prediction_id: string;
  status: OutcomeStatus;
  settled_at?: string | null;
  outcome_status?: OutcomeStatus | null;
  prediction?: Prediction | null;
  paper_trade?: PaperTrade | null;
};

export interface ReplayCounts extends JsonRecord {
  planned?: number;
  sample_rows?: number;
  completed?: number;
  wait?: number;
  errors?: number;
  actionable?: number;
  resolved_actionable?: number;
  outcomes?: number;
}

export interface ReplaySamplingPolicy extends JsonRecord {
  samples?: number;
  resume?: boolean;
  execution?: string;
  min_history_bars?: number;
  deterministic_seed?: number;
  order?: string;
  news_history_available?: boolean;
}

export interface ReplayConfig extends JsonRecord {
  db_path?: string;
  samples?: number;
  seed?: number;
  manifest_path?: string;
  requested_via?: string;
  execution?: string;
}

export interface ReplayCapabilityFlags extends JsonRecord {
  news_history_available?: boolean;
  technical_only?: boolean;
}

export type ReplaySample = JsonRecord & {
  run_id?: string;
  prediction_id?: string | null;
  symbol?: string;
  timeframe?: Timeframe | string;
  as_of?: string;
  status?: string;
  error_code?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  capability_flags?: ReplayCapabilityFlags;
};

export type ReplayRun = JsonRecord & {
  run_id: string;
  status: ReplayStatus;
  model_id?: string;
  prompt_version?: string | null;
  created_at?: string;
  completed_at?: string | null;
  symbols?: string[];
  timeframes?: Array<Timeframe | string>;
  sampling_policy?: ReplaySamplingPolicy;
  manifest_hash?: string;
  counts?: ReplayCounts;
  config?: ReplayConfig;
  error_code?: string | null;
  samples?: ReplaySample[];
};

export interface PerformanceBucket extends JsonRecord {
  bucket?: string;
  count?: number;
  raw_avg_confidence?: number | null;
  empirical_win_rate?: number | null;
  calibrated_confidence?: number | null;
}

export interface PerformanceMetrics extends JsonRecord {
  status?: string;
  sample_count?: number;
  actionable_count?: number;
  resolved_actionable?: number;
  pending_actionable?: number;
  wait_count?: number;
  invalid_count?: number;
  wins?: number;
  losses?: number;
  flats?: number;
  win_rate?: number | null;
  avg_r?: number | null;
  expectancy_r?: number | null;
  profit_factor?: number | null;
  max_drawdown_r?: number | null;
  mfe_r_avg?: number | null;
  mfe_r_median?: number | null;
  mfe_r_p90?: number | null;
  mae_r_avg?: number | null;
  mae_r_median?: number | null;
  mae_r_p90?: number | null;
  timeout_count?: number;
  timeout_rate?: number | null;
  coverage?: number | null;
  action_rate?: number | null;
  wait_rate?: number | null;
  brier_raw?: number | null;
  brier_calibrated?: number | null;
  ece_raw?: number | null;
  ece_calibrated?: number | null;
  confidence_buckets?: PerformanceBucket[];
}

export type PerformanceSummary = JsonRecord & {
  scope?: JsonRecord;
  status?: string;
  source_type?: SourceType;
  model_id?: string | null;
  prompt_version?: string | null;
  window_start?: string | null;
  window_end?: string | null;
  sample_count?: number;
  actionable_count?: number;
  metrics?: PerformanceMetrics;
};

export type PerformanceBuckets = JsonRecord & {
  scope?: JsonRecord;
  confidence_buckets?: PerformanceBucket[];
};

export interface CalibrationBucket extends JsonRecord {
  lower?: number;
  upper?: number;
  n?: number;
  wins?: number;
  empirical_rate?: number | null;
  shrunk_rate?: number | null;
}

export type CalibrationCurrent = JsonRecord & {
  status?: string;
  calibration_id?: string;
  version?: string;
  scope?: JsonRecord;
  method?: string;
  params?: JsonRecord;
  sample_count?: number;
  trained_until?: string | null;
  brier_raw?: number | null;
  brier_calibrated?: number | null;
  ece_raw?: number | null;
  ece_calibrated?: number | null;
  fallback?: string | null;
  buckets?: CalibrationBucket[];
};

export type PredictionCalibration = JsonRecord & {
  prediction_id: string;
  raw_confidence?: number;
  calibrated_confidence?: number;
  calibration_version?: string;
  calibration_scope?: string;
  calibration_sample_size?: number;
  calibration_fallback?: boolean;
  source_type?: SourceType;
};

export type RadarCategory = "STRONG_OPPORTUNITY" | "WATCH" | "AVOID" | "WAIT" | "NOT_RANKED";

export interface RadarFilters {
  asset_type?: AssetType;
  category?: RadarCategory;
}

export interface RadarComponent extends JsonRecord {
  name: string;
  input?: unknown;
  score?: number | null;
  weight?: number;
  contribution?: number | null;
  status?: string;
  available?: boolean;
  reason?: string | null;
  provenance?: string;
}

export interface RadarEntry extends JsonRecord {
  symbol: string;
  instrument: Instrument;
  prediction_id?: string | null;
  action?: Action | null;
  category: RadarCategory;
  status: string;
  ranking_eligible: boolean;
  score?: number | null;
  rank?: number | null;
  generated_at?: string | null;
  data_as_of?: string | null;
  signal_valid_until?: string | null;
  freshness?: JsonRecord;
  inputs?: JsonRecord;
  components: Record<string, RadarComponent>;
  missing_reasons?: string[];
  degraded_reasons?: string[];
  outcome?: JsonRecord | null;
  provenance?: JsonRecord;
}

export interface RadarResponse extends JsonRecord {
  scoring_version: string;
  config: JsonRecord & {
    version?: string;
    weights?: Record<string, number>;
  };
  status: string;
  as_of: string;
  filters?: RadarFilters;
  counts?: JsonRecord;
  provenance?: JsonRecord;
  entries: RadarEntry[];
}

export type AnalysisResult = JsonRecord & {
  instrument?: AnalysisInstrument;
  timeframe?: Timeframe;
  response_time?: string;
  data_as_of?: string;
  provider_snapshot?: ProviderSnapshot;
  quote?: MarketQuote;
  quant?: QuantFacts;
  news?: InstrumentNews;
  benchmark_context?: BenchmarkContext | null;
  events?: EventIntelligence | null;
  market_memory?: MarketMemoryContext | null;
  time_policy?: TimePolicy | null;
  model?: ModelStatus;
  signal?: SignalProposal;
  input_hash?: string;
};

export interface PaginationFilters {
  limit?: number;
  offset?: number;
}

export interface PredictionFilters extends PaginationFilters {
  symbol?: string;
  timeframe?: Timeframe;
  action?: Action;
  source_type?: SourceType;
  replay_run_id?: string;
  model_id?: string;
  prompt_version?: string;
  outcome_status?: OutcomeStatus;
  has_outcome?: boolean;
}

export interface PaperTradeFilters extends PaginationFilters {
  symbol?: string;
  timeframe?: Timeframe;
  action?: Action;
  source_type?: SourceType;
  status?: string;
  outcome_status?: OutcomeStatus;
}

export interface OutcomeFilters extends PaginationFilters {
  symbol?: string;
  timeframe?: Timeframe;
  action?: Action;
  source_type?: SourceType;
  status?: OutcomeStatus;
}

export interface PerformanceFilters {
  source_type?: SourceType;
  symbol?: string;
  timeframe?: Timeframe;
  model_id?: string;
  prompt_version?: string;
  replay_run_id?: string;
}

export interface ReplayRunFilters extends PaginationFilters {
  status?: ReplayStatus;
}

export interface AnalysisRequest {
  timeframe?: Timeframe;
  limit?: number;
}

export interface ContextFilters {
  timeframe?: Timeframe;
  limit?: number;
  as_of?: string;
}

export interface FollowRequest {
  status?: string;
}

export interface FollowResponse {
  prediction_id: string;
  status: string;
  real_order: false;
}
