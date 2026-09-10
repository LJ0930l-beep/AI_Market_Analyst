import type {
  AnalysisRequest,
  AnalysisResult,
  AlertFilters,
  AlertRecord,
  AlertStatusResponse,
  AlertsResponse,
  AppSetting,
  AppSettingKey,
  AppSettingResetResponse,
  AppSettingValue,
  CalibrationCurrent,
  ChartAnnotationsResponse,
  ChartBarsResponse,
  ContextHealthResponse,
  ContextFilters,
  ConsultRequest,
  ConsultStreamEvent,
  FollowRequest,
  FollowResponse,
  HealthResponse,
  AssetType,
  Instrument,
  InstrumentRegistrationResponse,
  InstrumentNews,
  JsonRecord,
  MarketSnapshot,
  MonitoringPolicy,
  MonitoringResponse,
  MonitoringRuntimeStatus,
  OpportunityAnalysesResponse,
  MarketContextResponse,
  MarketIntelligenceResponse,
  PublicHydrationStatus,
  DailyBriefResponse,
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
  RealtimeMarketResponse,
  ReplayRun,
  ReleaseHealthResponse,
  ReplayRunFilters,
  RadarFilters,
  RadarResponse,
  SnapshotFilters,
  SchedulerHistory,
  SchedulerStatus,
  StatsResponse,
  TriggerEvent,
  WatchlistDeleteResponse,
  WatchlistEntry,
} from "./types";

type QueryValue = string | number | boolean | null | undefined;
type QueryParams = object;
type FetchImplementation = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

declare global {
  interface Window {
    __AIMA_API_BASE_URL__?: string;
  }
}

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
  releaseHealth(signal?: AbortSignal): Promise<ReleaseHealthResponse>;
  providerHealth(signal?: AbortSignal): Promise<ProviderHealthResponse>;
  contextHealth(signal?: AbortSignal): Promise<ContextHealthResponse>;
  modelHealth(signal?: AbortSignal): Promise<ModelHealthResponse>;
  marketIntelligence(signal?: AbortSignal): Promise<MarketIntelligenceResponse>;
  hydrationStatus(signal?: AbortSignal): Promise<PublicHydrationStatus>;
  refreshHydration(signal?: AbortSignal): Promise<PublicHydrationStatus>;
  dailyBrief(language?: "en" | "zh-CN", signal?: AbortSignal): Promise<DailyBriefResponse>;
  generateDailyBrief(language: "en" | "zh-CN", modelPreference: "auto" | "fast" | "smart", signal?: AbortSignal): Promise<DailyBriefResponse>;
  consultStream(request: ConsultRequest, onEvent: (event: ConsultStreamEvent) => void, signal?: AbortSignal): Promise<void>;
  stats(signal?: AbortSignal): Promise<StatsResponse>;
  schedulerStatus(signal?: AbortSignal): Promise<SchedulerStatus>;
  schedulerHistory(limit?: number, signal?: AbortSignal): Promise<SchedulerHistory>;
  startScheduler(signal?: AbortSignal): Promise<SchedulerStatus>;
  stopScheduler(signal?: AbortSignal): Promise<SchedulerStatus>;
  runSchedulerOnce(signal?: AbortSignal): Promise<{ run?: unknown; items?: unknown[]; status: SchedulerStatus }>;
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
  realtimeMarket(symbol: string, signal?: AbortSignal): Promise<RealtimeMarketResponse>;
  monitoring(signal?: AbortSignal): Promise<MonitoringResponse>;
  monitoringStatus(signal?: AbortSignal): Promise<MonitoringRuntimeStatus>;
  startMonitoring(signal?: AbortSignal): Promise<MonitoringRuntimeStatus>;
  resumeMonitoring(signal?: AbortSignal): Promise<MonitoringRuntimeStatus>;
  pauseMonitoring(signal?: AbortSignal): Promise<MonitoringRuntimeStatus>;
  stopMonitoring(signal?: AbortSignal): Promise<MonitoringRuntimeStatus>;
  monitoringOpportunities(symbol?: string, limit?: number, signal?: AbortSignal): Promise<OpportunityAnalysesResponse>;
  updateMonitoringPolicy(policy: MonitoringPolicy, signal?: AbortSignal): Promise<MonitoringPolicy>;
  runMonitoring(symbols?: string[], signal?: AbortSignal): Promise<JsonRecord>;
  triggerEvents(symbol?: string, limit?: number, signal?: AbortSignal): Promise<{ policy_version: string; events: TriggerEvent[]; [key: string]: unknown }>;
  chartBars(symbol: string, timeframe?: "15m" | "1h", limit?: number, signal?: AbortSignal): Promise<ChartBarsResponse>;
  chartAnnotations(symbol: string, timeframe?: "15m" | "1h", signal?: AbortSignal): Promise<ChartAnnotationsResponse>;
  localizedNews(symbol: string, locale?: "en" | "zh-CN", signal?: AbortSignal): Promise<InstrumentNews>;
  translateNews(newsId: string, signal?: AbortSignal): Promise<JsonRecord>;
  instrumentSnapshot(symbol: string, signal?: AbortSignal): Promise<MarketSnapshot>;
  instrumentSnapshot(symbol: string, filters?: SnapshotFilters, signal?: AbortSignal): Promise<MarketSnapshot>;
  instrumentNews(symbol: string, signal?: AbortSignal): Promise<InstrumentNews>;
  instrumentContext(symbol: string, filters?: ContextFilters, signal?: AbortSignal): Promise<MarketContextResponse>;
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
  radar(filters?: RadarFilters, signal?: AbortSignal): Promise<RadarResponse>;
  alerts(filters?: AlertFilters, signal?: AbortSignal): Promise<AlertsResponse>;
  alertStatus(signal?: AbortSignal): Promise<AlertStatusResponse>;
  acknowledgeAlert(alertId: string, signal?: AbortSignal): Promise<AlertRecord>;
  replayRuns(filters?: ReplayRunFilters, signal?: AbortSignal): Promise<ReplayRun[]>;
  replayRun(runId: string, signal?: AbortSignal): Promise<ReplayRun>;
  followPrediction(predictionId: string, request?: FollowRequest, signal?: AbortSignal): Promise<FollowResponse>;
}

export type ApplicationShellApiClient = Pick<
  MarketApiClient,
  | "health"
  | "releaseHealth"
  | "providerHealth"
  | "contextHealth"
  | "modelHealth"
  | "marketIntelligence"
  | "hydrationStatus"
  | "refreshHydration"
  | "dailyBrief"
  | "generateDailyBrief"
  | "consultStream"
  | "stats"
  | "schedulerStatus"
  | "schedulerHistory"
  | "startScheduler"
  | "stopScheduler"
  | "runSchedulerOnce"
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
  | "realtimeMarket"
  | "monitoring"
  | "monitoringStatus"
  | "startMonitoring"
  | "resumeMonitoring"
  | "pauseMonitoring"
  | "stopMonitoring"
  | "monitoringOpportunities"
  | "updateMonitoringPolicy"
  | "runMonitoring"
  | "triggerEvents"
  | "chartBars"
  | "chartAnnotations"
  | "localizedNews"
  | "translateNews"
  | "instrumentSnapshot"
  | "instrumentNews"
  | "instrumentContext"
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
  | "radar"
  | "alerts"
  | "alertStatus"
  | "acknowledgeAlert"
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
  configured?: string,
  isDevelopment = import.meta.env.DEV,
): string {
  const runtimeBaseUrl = typeof window !== "undefined" ? window.__AIMA_API_BASE_URL__ : undefined;
  const explicitBaseUrl = (configured ?? runtimeBaseUrl ?? import.meta.env.VITE_API_BASE_URL ?? "").trim().replace(/\/+$/, "");
  if (explicitBaseUrl) {
    return explicitBaseUrl;
  }
  if (
    typeof window !== "undefined" &&
    (window.location.hostname === "tauri.localhost" ||
      window.location.protocol === "tauri:" ||
      "__TAURI_INTERNALS__" in window)
  ) {
    return "http://127.0.0.1:18765";
  }
  return isDevelopment ? "/api" : "";
}

export function getRealtimeStreamUrl(
  symbol: string,
  timeframe: "15m" | "1h" = "15m",
  configured?: string,
  isDevelopment = import.meta.env.DEV,
): string {
  const base = getApiBaseUrl(configured, isDevelopment);
  const origin = typeof window === "undefined" ? "http://127.0.0.1" : window.location.origin;
  const url = new URL(base ? (base.startsWith("http://") || base.startsWith("https://") ? base : `${origin}${base}`) : origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = `${url.pathname.replace(/\/$/, "")}/market/realtime/${encodePathSegment(symbol)}/stream`;
  url.search = serializeQuery({ timeframe }).slice(1);
  return url.toString();
}

export function getMonitoringEventsUrl(
  configured?: string,
  isDevelopment = import.meta.env.DEV,
): string {
  const base = getApiBaseUrl(configured, isDevelopment);
  const origin = typeof window === "undefined" ? "http://127.0.0.1" : window.location.origin;
  const url = new URL(base ? (base.startsWith("http://") || base.startsWith("https://") ? base : `${origin}${base}`) : origin);
  url.pathname = `${url.pathname.replace(/\/$/, "")}/monitoring/events`;
  return url.toString();
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

function parseConsultStreamEvent(payload: unknown): ConsultStreamEvent {
  if (!isRecord(payload) || typeof payload.type !== "string") {
    throw new ApiError(502, { code: "QWEN_STREAM_INVALID", message: "Qwen stream returned an invalid event." });
  }
  if (payload.type === "delta" && typeof payload.content === "string") {
    return { type: "delta", content: payload.content };
  }
  if (payload.type === "done" && typeof payload.finish_reason === "string" && typeof payload.output_chars === "number") {
    return { type: "done", finish_reason: payload.finish_reason, output_chars: payload.output_chars };
  }
  if (payload.type === "error" && isRecord(payload.error) && typeof payload.error.code === "string" && typeof payload.error.message === "string") {
    return { type: "error", error: { code: payload.error.code, message: payload.error.message } };
  }
  if (
    payload.type === "meta" &&
    typeof payload.contract_version === "string" &&
    typeof payload.request_id === "string" &&
    typeof payload.provider === "string" &&
    typeof payload.model_id === "string" &&
    isRecord(payload.context)
  ) {
    return payload as ConsultStreamEvent;
  }
  throw new ApiError(502, { code: "QWEN_STREAM_INVALID", message: "Qwen stream returned an invalid event." });
}

export class ApiClient implements MarketApiClient {
  private readonly configuredBaseUrl?: string;
  private readonly fetchImpl: FetchImplementation;

  constructor(options: ApiClientOptions = {}) {
    this.configuredBaseUrl = options.baseUrl;
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  private get baseUrl(): string {
    return getApiBaseUrl(this.configuredBaseUrl);
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

  v2<T>(path: string, method: "GET" | "POST" | "PUT" | "DELETE" = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
    return this.request<T>(`/v2${path}`, { method, body, signal });
  }

  v3<T>(path: string, method: "GET" | "POST" | "PUT" | "DELETE" = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
    return this.request<T>(`/v3${path}`, { method, body, signal });
  }

  releaseHealth(signal?: AbortSignal): Promise<ReleaseHealthResponse> {
    return this.request<ReleaseHealthResponse>("/health/release", { signal });
  }

  providerHealth(signal?: AbortSignal): Promise<ProviderHealthResponse> {
    return this.request<ProviderHealthResponse>("/health/providers", { signal });
  }

  contextHealth(signal?: AbortSignal): Promise<ContextHealthResponse> {
    return this.request<ContextHealthResponse>("/health/context", { signal });
  }

  modelHealth(signal?: AbortSignal): Promise<ModelHealthResponse> {
    return this.request<ModelHealthResponse>("/health/model", { signal });
  }

  marketIntelligence(signal?: AbortSignal): Promise<MarketIntelligenceResponse> {
    return this.request<MarketIntelligenceResponse>("/market-intelligence", { signal });
  }

  hydrationStatus(signal?: AbortSignal): Promise<PublicHydrationStatus> {
    return this.request<PublicHydrationStatus>("/hydration/status", { signal });
  }

  refreshHydration(signal?: AbortSignal): Promise<PublicHydrationStatus> {
    return this.request<PublicHydrationStatus>("/hydration/refresh", { method: "POST", signal });
  }

  dailyBrief(language?: "en" | "zh-CN", signal?: AbortSignal): Promise<DailyBriefResponse> {
    return this.request<DailyBriefResponse>("/daily-brief", { query: { language }, signal });
  }

  generateDailyBrief(
    language: "en" | "zh-CN",
    modelPreference: "auto" | "fast" | "smart",
    signal?: AbortSignal,
  ): Promise<DailyBriefResponse> {
    return this.request<DailyBriefResponse>("/daily-brief/generate", {
      method: "POST",
      body: { language, model_preference: modelPreference },
      signal,
    });
  }

  async consultStream(
    request: ConsultRequest,
    onEvent: (event: ConsultStreamEvent) => void,
    signal?: AbortSignal,
  ): Promise<void> {
    const headers = new Headers({
      accept: "application/x-ndjson",
      "content-type": "application/json",
    });
    const fetchImpl = this.fetchImpl;
    const response = await fetchImpl(`${this.baseUrl}/consult/stream`, {
      method: "POST",
      headers,
      body: JSON.stringify(request),
      signal,
    });
    if (!response.ok) {
      const responseText = await response.text();
      let payload: unknown;
      try {
        payload = responseText ? JSON.parse(responseText) : undefined;
      } catch {
        payload = undefined;
      }
      throw ApiError.fromResponse(response.status, payload);
    }
    if (!response.body) {
      throw new ApiError(502, { code: "QWEN_STREAM_UNAVAILABLE", message: "Qwen stream body is unavailable." });
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let terminal = false;
    const dispatchLine = (line: string) => {
      if (!line.trim()) return;
      let payload: unknown;
      try {
        payload = JSON.parse(line);
      } catch {
        throw new ApiError(502, { code: "QWEN_STREAM_INVALID", message: "Qwen stream returned invalid JSON." });
      }
      const event = parseConsultStreamEvent(payload);
      if (event.type === "done" || event.type === "error") terminal = true;
      onEvent(event);
    };
    try {
      while (true) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        if (buffer.length > 65_536) {
          throw new ApiError(502, { code: "QWEN_STREAM_INVALID", message: "Qwen stream exceeded the client event limit." });
        }
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines) dispatchLine(line);
        if (done) break;
      }
      if (buffer.trim()) dispatchLine(buffer);
      if (!terminal) {
        throw new ApiError(502, { code: "QWEN_STREAM_INTERRUPTED", message: "Qwen stream ended before a terminal event." });
      }
    } finally {
      reader.releaseLock();
    }
  }

  stats(signal?: AbortSignal): Promise<StatsResponse> {
    return this.request<StatsResponse>("/stats", { signal });
  }

  schedulerStatus(signal?: AbortSignal): Promise<SchedulerStatus> {
    return this.request<SchedulerStatus>("/scheduler/status", { signal });
  }

  schedulerHistory(limit = 20, signal?: AbortSignal): Promise<SchedulerHistory> {
    return this.request<SchedulerHistory>("/scheduler/history", { query: { limit }, signal });
  }

  startScheduler(signal?: AbortSignal): Promise<SchedulerStatus> {
    return this.request<SchedulerStatus>("/scheduler/start", { method: "POST", signal });
  }

  stopScheduler(signal?: AbortSignal): Promise<SchedulerStatus> {
    return this.request<SchedulerStatus>("/scheduler/stop", { method: "POST", signal });
  }

  runSchedulerOnce(signal?: AbortSignal): Promise<{ run?: unknown; items?: unknown[]; status: SchedulerStatus }> {
    return this.request<{ run?: unknown; items?: unknown[]; status: SchedulerStatus }>("/scheduler/run-once", {
      method: "POST",
      signal,
    });
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

  realtimeMarket(symbol: string, signal?: AbortSignal): Promise<RealtimeMarketResponse> {
    return this.request<RealtimeMarketResponse>(`/market/realtime/${encodePathSegment(symbol)}`, { signal });
  }

  monitoring(signal?: AbortSignal): Promise<MonitoringResponse> {
    return this.request<MonitoringResponse>("/monitoring", { signal });
  }

  monitoringStatus(signal?: AbortSignal): Promise<MonitoringRuntimeStatus> {
    return this.request<MonitoringRuntimeStatus>("/monitoring/status", { signal });
  }

  startMonitoring(signal?: AbortSignal): Promise<MonitoringRuntimeStatus> {
    return this.request<MonitoringRuntimeStatus>("/monitoring/start", { method: "POST", signal });
  }

  resumeMonitoring(signal?: AbortSignal): Promise<MonitoringRuntimeStatus> {
    return this.request<MonitoringRuntimeStatus>("/monitoring/resume", { method: "POST", signal });
  }

  pauseMonitoring(signal?: AbortSignal): Promise<MonitoringRuntimeStatus> {
    return this.request<MonitoringRuntimeStatus>("/monitoring/pause", { method: "POST", signal });
  }

  stopMonitoring(signal?: AbortSignal): Promise<MonitoringRuntimeStatus> {
    return this.request<MonitoringRuntimeStatus>("/monitoring/stop", { method: "POST", signal });
  }

  monitoringOpportunities(symbol?: string, limit = 100, signal?: AbortSignal): Promise<OpportunityAnalysesResponse> {
    return this.request<OpportunityAnalysesResponse>("/monitoring/opportunities", {
      query: { symbol, limit },
      signal,
    });
  }

  updateMonitoringPolicy(policy: MonitoringPolicy, signal?: AbortSignal): Promise<MonitoringPolicy> {
    return this.request<MonitoringPolicy>("/monitoring", { method: "PUT", body: policy, signal });
  }

  runMonitoring(symbols?: string[], signal?: AbortSignal): Promise<JsonRecord> {
    return this.request<JsonRecord>("/monitoring/run", {
      method: "POST",
      body: symbols === undefined ? {} : { symbols },
      signal,
    });
  }

  triggerEvents(symbol?: string, limit = 100, signal?: AbortSignal): Promise<{ policy_version: string; events: TriggerEvent[]; [key: string]: unknown }> {
    return this.request<{ policy_version: string; events: TriggerEvent[]; [key: string]: unknown }>("/triggers", {
      query: { symbol, limit },
      signal,
    });
  }

  chartBars(symbol: string, timeframe: "15m" | "1h" = "15m", limit = 240, signal?: AbortSignal): Promise<ChartBarsResponse> {
    return this.request<ChartBarsResponse>(`/chart/${encodePathSegment(symbol)}/bars`, {
      query: { timeframe, limit },
      signal,
    });
  }

  chartAnnotations(symbol: string, timeframe: "15m" | "1h" = "15m", signal?: AbortSignal): Promise<ChartAnnotationsResponse> {
    return this.request<ChartAnnotationsResponse>(`/chart/${encodePathSegment(symbol)}/annotations`, {
      query: { timeframe },
      signal,
    });
  }

  localizedNews(symbol: string, locale: "en" | "zh-CN" = "zh-CN", signal?: AbortSignal): Promise<InstrumentNews> {
    return this.request<InstrumentNews>(`/news/${encodePathSegment(symbol)}`, { query: { locale }, signal });
  }

  translateNews(newsId: string, signal?: AbortSignal): Promise<JsonRecord> {
    return this.request<JsonRecord>(`/news/translate/${encodePathSegment(newsId)}`, { method: "POST", body: {}, signal });
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

  instrumentContext(
    symbol: string,
    filters: ContextFilters = {},
    signal?: AbortSignal,
  ): Promise<MarketContextResponse> {
    return this.request<MarketContextResponse>(`/instruments/${encodePathSegment(symbol)}/context`, {
      query: filters,
      signal,
    });
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

  radar(filters: RadarFilters = {}, signal?: AbortSignal): Promise<RadarResponse> {
    return this.request<RadarResponse>("/radar", { query: filters, signal });
  }

  alerts(filters: AlertFilters = {}, signal?: AbortSignal): Promise<AlertsResponse> {
    return this.request<AlertsResponse>("/alerts", { query: filters, signal });
  }

  alertStatus(signal?: AbortSignal): Promise<AlertStatusResponse> {
    return this.request<AlertStatusResponse>("/alerts/status", { signal });
  }

  acknowledgeAlert(alertId: string, signal?: AbortSignal): Promise<AlertRecord> {
    return this.request<AlertRecord>(`/alerts/${encodePathSegment(alertId)}/acknowledge`, {
      method: "POST",
      body: {},
      signal,
    });
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
