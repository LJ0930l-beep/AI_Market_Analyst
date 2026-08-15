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

export type PerformanceSummary = JsonRecord & {
  scope?: JsonRecord;
  metrics?: JsonRecord;
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
  signal?: JsonRecord;
  instrument?: JsonRecord;
  quote?: JsonRecord;
  quant?: JsonRecord;
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
