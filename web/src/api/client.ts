import type {
  AnalysisRequest,
  AnalysisResult,
  AppSetting,
  AppSettingKey,
  AppSettingResetResponse,
  AppSettingValue,
  CalibrationCurrent,
  FollowRequest,
  FollowResponse,
  HealthResponse,
  AssetType,
  Instrument,
  InstrumentRegistrationResponse,
  InstrumentNews,
  MarketSnapshot,
  ModelHealthResponse,
  Outcome,
  OutcomeFilters,
  PaperTrade,
  PaperTradeFilters,
  PerformanceBuckets,
  PerformanceFilters,
  PerformanceSummary,
  Prediction,
  PredictionCalibration,
  PredictionFilters,
  ProviderHealthResponse,
  ReplayRun,
  ReplayRunFilters,
  SnapshotFilters,
  StatsResponse,
  WatchlistDeleteResponse,
  WatchlistEntry,
} from "./types";

type QueryValue = string | number | boolean | null | undefined;
type QueryParams = object;
type FetchImplementation = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

export interface ApiErrorPayload {
  code: string;
  message: string;
  detail?: unknown;
  context?: Record<string, unknown>;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail?: unknown;
  readonly context?: Record<string, unknown>;

  constructor(status: number, payload: ApiErrorPayload) {
    super(payload.message);
    this.name = "ApiError";
    this.status = status;
    this.code = payload.code;
    this.detail = payload.detail;
    this.context = payload.context;
  }

  static fromResponse(status: number, payload: unknown): ApiError {
    if (isRecord(payload) && isRecord(payload.error)) {
      const error = payload.error;
      const code = typeof error.code === "string" ? error.code : "HTTP_ERROR";
      const message = typeof error.message === "string" ? error.message : `Request failed with status ${status}`;
      const context = isRecord(error.context) ? error.context : undefined;
      return new ApiError(status, {
        code,
        message,
        detail: error.detail,
        context,
      });
    }

    return new ApiError(status, {
      code: "HTTP_ERROR",
      message: `Request failed with status ${status}`,
    });
  }
}

export interface ApiClientOptions {
  baseUrl?: string;
  fetchImpl?: FetchImplementation;
}

export interface MarketApiClient {
  health(signal?: AbortSignal): Promise<HealthResponse>;
  providerHealth(signal?: AbortSignal): Promise<ProviderHealthResponse>;
  modelHealth(signal?: AbortSignal): Promise<ModelHealthResponse>;
  stats(signal?: AbortSignal): Promise<StatsResponse>;
  instruments(signal?: AbortSignal): Promise<Instrument[]>;
  registerInstrument(symbol: string, assetType: AssetType, signal?: AbortSignal): Promise<InstrumentRegistrationResponse>;
  watchlist(signal?: AbortSignal): Promise<WatchlistEntry[]>;
  addWatchlist(symbol: string, signal?: AbortSignal): Promise<WatchlistEntry>;
  upsertWatchlist(symbol: string, signal?: AbortSignal): Promise<WatchlistEntry>;
  removeWatchlist(symbol: string, signal?: AbortSignal): Promise<WatchlistDeleteResponse>;
  appSettings(signal?: AbortSignal): Promise<AppSetting[]>;
  appSetting(key: AppSettingKey, signal?: AbortSignal): Promise<AppSetting>;
  updateAppSetting(key: AppSettingKey, value: AppSettingValue, signal?: AbortSignal): Promise<AppSetting>;
  resetAppSetting(key: AppSettingKey, signal?: AbortSignal): Promise<AppSettingResetResponse>;
  instrumentSnapshot(symbol: string, signal?: AbortSignal): Promise<MarketSnapshot>;
  instrumentSnapshot(symbol: string, filters?: SnapshotFilters, signal?: AbortSignal): Promise<MarketSnapshot>;
  instrumentNews(symbol: string, signal?: AbortSignal): Promise<InstrumentNews>;
  analysis(symbol: string, request?: AnalysisRequest, signal?: AbortSignal): Promise<AnalysisResult>;
  predictions(filters?: PredictionFilters, signal?: AbortSignal): Promise<Prediction[]>;
  prediction(predictionId: string, signal?: AbortSignal): Promise<Prediction>;
  paperTrades(filters?: PaperTradeFilters, signal?: AbortSignal): Promise<PaperTrade[]>;
  paperTrade(predictionId: string, signal?: AbortSignal): Promise<PaperTrade>;
  outcomes(filters?: OutcomeFilters, signal?: AbortSignal): Promise<Outcome[]>;
  outcome(predictionId: string, signal?: AbortSignal): Promise<Outcome>;
  performanceSummary(filters?: PerformanceFilters, signal?: AbortSignal): Promise<PerformanceSummary>;
  performanceBySymbol(symbol: string, filters?: Omit<PerformanceFilters, "symbol">, signal?: AbortSignal): Promise<PerformanceSummary>;
  performanceBuckets(filters?: Pick<PerformanceFilters, "source_type" | "model_id" | "prompt_version" | "replay_run_id">, signal?: AbortSignal): Promise<PerformanceBuckets>;
  calibrationCurrent(signal?: AbortSignal): Promise<CalibrationCurrent>;
  predictionCalibration(predictionId: string, signal?: AbortSignal): Promise<PredictionCalibration>;
  replayRuns(filters?: ReplayRunFilters, signal?: AbortSignal): Promise<ReplayRun[]>;
  replayRun(runId: string, signal?: AbortSignal): Promise<ReplayRun>;
  followPrediction(predictionId: string, request?: FollowRequest, signal?: AbortSignal): Promise<FollowResponse>;
}

export type ApplicationShellApiClient = Pick<
  MarketApiClient,
  | "health"
  | "providerHealth"
  | "modelHealth"
  | "stats"
  | "instruments"
  | "registerInstrument"
  | "watchlist"
  | "addWatchlist"
  | "upsertWatchlist"
  | "removeWatchlist"
  | "appSettings"
  | "appSetting"
  | "updateAppSetting"
  | "resetAppSetting"
  | "instrumentSnapshot"
  | "instrumentNews"
  | "analysis"
  | "predictions"
  | "prediction"
  | "paperTrades"
  | "paperTrade"
  | "outcomes"
  | "outcome"
  | "performanceSummary"
  | "performanceBuckets"
  | "calibrationCurrent"
  | "replayRuns"
  | "replayRun"
  | "followPrediction"
>;

export function serializeQuery(params: QueryParams): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params as Record<string, QueryValue>)) {
    if (value !== undefined && value !== null && value !== "") {
      query.set(key, String(value));
    }
  }
  const encoded = query.toString();
  return encoded ? `?${encoded}` : "";
}

export function getApiBaseUrl(
  configured = import.meta.env.VITE_API_BASE_URL,
  isDevelopment = import.meta.env.DEV,
): string {
  const explicitBaseUrl = (configured ?? "").trim().replace(/\/+$/, "");
  if (explicitBaseUrl) {
    return explicitBaseUrl;
  }
  return isDevelopment ? "/api" : "";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function encodePathSegment(value: string): string {
  return encodeURIComponent(value);
}

function isAbortSignal(value: unknown): value is AbortSignal {
  return (
    typeof value === "object" &&
    value !== null &&
    "aborted" in value &&
    "addEventListener" in value
  );
}

export class ApiClient implements MarketApiClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: FetchImplementation;

  constructor(options: ApiClientOptions = {}) {
    this.baseUrl = getApiBaseUrl(options.baseUrl ?? import.meta.env.VITE_API_BASE_URL);
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  private async request<T>(
    path: string,
    options: {
      method?: "GET" | "POST" | "PUT" | "DELETE";
      query?: QueryParams;
      body?: unknown;
      signal?: AbortSignal;
    } = {},
  ): Promise<T> {
    const headers = new Headers();
    const body = options.body === undefined ? undefined : JSON.stringify(options.body);
    if (body !== undefined) {
      headers.set("content-type", "application/json");
    }

    const requestInit: RequestInit = {
      method: options.method ?? "GET",
      headers,
    };
    if (body !== undefined) {
      requestInit.body = body;
    }
    if (options.signal !== undefined) {
      requestInit.signal = options.signal;
    }

    // Detach the implementation before calling it so native window.fetch receives no ApiClient receiver.
    const fetchImpl = this.fetchImpl;
    const response = await fetchImpl(`${this.baseUrl}${path}${serializeQuery(options.query ?? {})}`, requestInit);
    const responseText = await response.text();
    let payload: unknown;
    if (responseText) {
      try {
        payload = JSON.parse(responseText) as unknown;
      } catch {
        payload = undefined;
      }
    }

    if (!response.ok) {
      throw ApiError.fromResponse(response.status, payload);
    }
    return payload as T;
  }

  health(signal?: AbortSignal): Promise<HealthResponse> {
    return this.request<HealthResponse>("/health", { signal });
  }

  providerHealth(signal?: AbortSignal): Promise<ProviderHealthResponse> {
    return this.request<ProviderHealthResponse>("/health/providers", { signal });
  }

  modelHealth(signal?: AbortSignal): Promise<ModelHealthResponse> {
    return this.request<ModelHealthResponse>("/health/model", { signal });
  }

  stats(signal?: AbortSignal): Promise<StatsResponse> {
    return this.request<StatsResponse>("/stats", { signal });
  }

  instruments(signal?: AbortSignal): Promise<Instrument[]> {
    return this.request<Instrument[]>("/instruments", { signal });
  }

  registerInstrument(
    symbol: string,
    assetType: AssetType,
    signal?: AbortSignal,
  ): Promise<InstrumentRegistrationResponse> {
    return this.request<InstrumentRegistrationResponse>("/instruments/register", {
      method: "POST",
      body: { symbol, asset_type: assetType },
      signal,
    });
  }

  watchlist(signal?: AbortSignal): Promise<WatchlistEntry[]> {
    return this.request<WatchlistEntry[]>("/watchlist", { signal });
  }

  addWatchlist(symbol: string, signal?: AbortSignal): Promise<WatchlistEntry> {
    return this.request<WatchlistEntry>("/watchlist", {
      method: "POST",
      body: { symbol },
      signal,
    });
  }

  upsertWatchlist(symbol: string, signal?: AbortSignal): Promise<WatchlistEntry> {
    return this.request<WatchlistEntry>(`/watchlist/${encodePathSegment(symbol)}`, {
      method: "PUT",
      body: { symbol },
      signal,
    });
  }

  removeWatchlist(symbol: string, signal?: AbortSignal): Promise<WatchlistDeleteResponse> {
    return this.request<WatchlistDeleteResponse>(`/watchlist/${encodePathSegment(symbol)}`, {
      method: "DELETE",
      signal,
    });
  }

  appSettings(signal?: AbortSignal): Promise<AppSetting[]> {
    return this.request<AppSetting[]>("/settings", { signal });
  }

  appSetting(key: AppSettingKey, signal?: AbortSignal): Promise<AppSetting> {
    return this.request<AppSetting>(`/settings/${encodePathSegment(key)}`, { signal });
  }

  updateAppSetting(key: AppSettingKey, value: AppSettingValue, signal?: AbortSignal): Promise<AppSetting> {
    return this.request<AppSetting>(`/settings/${encodePathSegment(key)}`, {
      method: "PUT",
      body: { value },
      signal,
    });
  }

  resetAppSetting(key: AppSettingKey, signal?: AbortSignal): Promise<AppSettingResetResponse> {
    return this.request<AppSettingResetResponse>(`/settings/${encodePathSegment(key)}`, {
      method: "DELETE",
      signal,
    });
  }

  instrumentSnapshot(
    symbol: string,
    filtersOrSignal: SnapshotFilters | AbortSignal = {},
    signal?: AbortSignal,
  ): Promise<MarketSnapshot> {
    const filters = isAbortSignal(filtersOrSignal) ? undefined : filtersOrSignal;
    const requestSignal = isAbortSignal(filtersOrSignal) ? filtersOrSignal : signal;
    return this.request<MarketSnapshot>(`/instruments/${encodePathSegment(symbol)}/snapshot`, {
      query: filters,
      signal: requestSignal,
    });
  }

  instrumentNews(symbol: string, signal?: AbortSignal): Promise<InstrumentNews> {
    return this.request<InstrumentNews>(`/instruments/${encodePathSegment(symbol)}/news`, { signal });
  }

  analysis(symbol: string, request: AnalysisRequest = {}, signal?: AbortSignal): Promise<AnalysisResult> {
    return this.request<AnalysisResult>(`/analysis/${encodePathSegment(symbol)}`, {
      method: "POST",
      body: request,
      signal,
    });
  }

  predictions(filters: PredictionFilters = {}, signal?: AbortSignal): Promise<Prediction[]> {
    return this.request<Prediction[]>("/predictions", { query: filters, signal });
  }

  prediction(predictionId: string, signal?: AbortSignal): Promise<Prediction> {
    return this.request<Prediction>(`/predictions/${encodePathSegment(predictionId)}`, { signal });
  }

  paperTrades(filters: PaperTradeFilters = {}, signal?: AbortSignal): Promise<PaperTrade[]> {
    return this.request<PaperTrade[]>("/paper-trades", { query: filters, signal });
  }

  paperTrade(predictionId: string, signal?: AbortSignal): Promise<PaperTrade> {
    return this.request<PaperTrade>(`/paper-trades/${encodePathSegment(predictionId)}`, { signal });
  }

  outcomes(filters: OutcomeFilters = {}, signal?: AbortSignal): Promise<Outcome[]> {
    return this.request<Outcome[]>("/outcomes", { query: filters, signal });
  }

  outcome(predictionId: string, signal?: AbortSignal): Promise<Outcome> {
    return this.request<Outcome>(`/outcomes/${encodePathSegment(predictionId)}`, { signal });
  }

  performanceSummary(filters: PerformanceFilters = {}, signal?: AbortSignal): Promise<PerformanceSummary> {
    return this.request<PerformanceSummary>("/performance/summary", { query: filters, signal });
  }

  performanceBySymbol(
    symbol: string,
    filters: Omit<PerformanceFilters, "symbol"> = {},
    signal?: AbortSignal,
  ): Promise<PerformanceSummary> {
    return this.request<PerformanceSummary>(`/performance/by-symbol/${encodePathSegment(symbol)}`, {
      query: filters,
      signal,
    });
  }

  performanceBuckets(
    filters: Pick<PerformanceFilters, "source_type" | "model_id" | "prompt_version" | "replay_run_id"> = {},
    signal?: AbortSignal,
  ): Promise<PerformanceBuckets> {
    return this.request<PerformanceBuckets>("/performance/buckets", { query: filters, signal });
  }

  calibrationCurrent(signal?: AbortSignal): Promise<CalibrationCurrent> {
    return this.request<CalibrationCurrent>("/calibration/current", { signal });
  }

  predictionCalibration(predictionId: string, signal?: AbortSignal): Promise<PredictionCalibration> {
    return this.request<PredictionCalibration>(
      `/predictions/${encodePathSegment(predictionId)}/calibration`,
      { signal },
    );
  }

  replayRuns(filters: ReplayRunFilters = {}, signal?: AbortSignal): Promise<ReplayRun[]> {
    return this.request<ReplayRun[]>("/replay/runs", { query: filters, signal });
  }

  replayRun(runId: string, signal?: AbortSignal): Promise<ReplayRun> {
    return this.request<ReplayRun>(`/replay/runs/${encodePathSegment(runId)}`, { signal });
  }

  followPrediction(
    predictionId: string,
    request: FollowRequest = {},
    signal?: AbortSignal,
  ): Promise<FollowResponse> {
    return this.request<FollowResponse>(`/predictions/${encodePathSegment(predictionId)}/follow`, {
      method: "POST",
      body: request,
      signal,
    });
  }
}

export const apiClient = new ApiClient();
