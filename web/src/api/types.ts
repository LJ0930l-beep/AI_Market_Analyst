export type JsonRecord = Record<string, unknown>;

export type Action = "LONG" | "SHORT" | "WAIT";
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

export interface ProviderHealthResponse extends JsonRecord {
  available?: boolean;
  routes?: JsonRecord[];
  news?: JsonRecord;
}

export interface ModelHealthResponse extends JsonRecord {
  provider?: string;
  available?: boolean;
  error_code?: string;
}

export interface Instrument extends JsonRecord {
  symbol: string;
  asset_type: string;
  exchange: string;
  currency: string;
  quote_currency: string;
  timezone: string;
  trading_hours: string;
  sector: string;
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
  signal_validity_minutes?: number;
  signal_valid_until?: string;
  expected_hold_minutes?: number;
  expected_hold_until?: string;
  max_hold_minutes?: number;
  max_hold_until?: string;
  reevaluate_at?: string;
  invalidation?: string[];
  raw_confidence?: number;
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
}

export type Prediction = JsonRecord & {
  prediction_id: string;
  symbol?: string;
  action?: Action;
  timeframe?: Timeframe;
  source_type?: SourceType;
  generated_at?: string;
  signal_valid_until?: string;
  raw_confidence?: number;
  calibrated_confidence?: number;
  paper_trade?: PaperTrade | null;
  outcome?: Outcome | null;
  outcome_status?: OutcomeStatus | null;
};

export type PaperTrade = JsonRecord & {
  prediction_id: string;
  status: string;
  followed_at?: string;
  prediction?: Prediction | null;
  outcome?: Outcome | null;
};

export type Outcome = JsonRecord & {
  prediction_id: string;
  status: OutcomeStatus;
  settled_at?: string;
  prediction?: Prediction | null;
  paper_trade?: PaperTrade | null;
};

export type ReplaySample = JsonRecord & {
  run_id?: string;
  prediction_id?: string;
  symbol?: string;
  timeframe?: Timeframe;
  status?: string;
};

export type ReplayRun = JsonRecord & {
  run_id: string;
  status: ReplayStatus;
  model_id?: string;
  prompt_version?: string;
  samples?: ReplaySample[];
};

export interface PerformanceMetrics extends JsonRecord {
  resolved_actionable?: number;
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
  confidence_buckets?: JsonRecord[];
};

export type CalibrationCurrent = JsonRecord & {
  status?: string;
  sample_count?: number;
  buckets?: JsonRecord[];
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

export type AnalysisResult = JsonRecord & {
  instrument?: AnalysisInstrument;
  timeframe?: Timeframe;
  response_time?: string;
  data_as_of?: string;
  provider_snapshot?: ProviderSnapshot;
  quote?: MarketQuote;
  quant?: QuantFacts;
  news?: InstrumentNews;
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

export interface FollowRequest {
  status?: string;
}

export interface FollowResponse {
  prediction_id: string;
  status: string;
  real_order: false;
}
