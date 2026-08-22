import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";

import type { ApplicationShellApiClient } from "../api/client";
import type {
  CalibrationCurrent,
  PerformanceBucket,
  PerformanceBuckets,
  PerformanceFilters,
  PerformanceMetrics,
  PerformanceSummary,
  SourceType,
  Timeframe,
} from "../api/types";
import { AsyncPanel, type PanelState } from "../components/AsyncPanel";
import { useI18n, type TranslationKey } from "../i18n";
import { ResearchFacts, useResearchFormatters } from "../components/ResearchFacts";
import type { AsyncResource } from "../hooks/useAsyncResource";
import { useAsyncResource } from "../hooks/useAsyncResource";
import { emptyDashboardProvenance, type DashboardProvenance } from "./DashboardPage";

interface PerformancePageProps {
  apiClient: ApplicationShellApiClient;
  onProvenanceChange: (provenance: DashboardProvenance) => void;
}

type FilterDraft = {
  source_type: SourceType;
  symbol: string;
  timeframe: Timeframe | "";
  model_id: string;
  prompt_version: string;
  replay_run_id: string;
};

const timeframes: Timeframe[] = ["5m", "15m", "1h", "4h", "1d"];
const initialDraft: FilterDraft = {
  source_type: "live",
  symbol: "",
  timeframe: "",
  model_id: "",
  prompt_version: "",
  replay_run_id: "",
};
const initialFilters: PerformanceFilters = { source_type: "live" };

function resourceState<T>(resource: AsyncResource<T>, empty: boolean): PanelState {
  if (resource.status === "loading") return "loading";
  if (resource.status === "unavailable") return "unavailable";
  return empty ? "empty" : "ready";
}

function buildFilters(draft: FilterDraft): PerformanceFilters {
  return {
    source_type: draft.source_type,
    ...(draft.symbol.trim() ? { symbol: draft.symbol.trim().toUpperCase() } : {}),
    ...(draft.timeframe ? { timeframe: draft.timeframe } : {}),
    ...(draft.model_id.trim() ? { model_id: draft.model_id.trim() } : {}),
    ...(draft.prompt_version.trim() ? { prompt_version: draft.prompt_version.trim() } : {}),
    ...(draft.replay_run_id.trim() ? { replay_run_id: draft.replay_run_id.trim() } : {}),
  };
}

function metricValue(metrics: PerformanceMetrics, key: keyof PerformanceMetrics, primitiveText: (value: unknown) => string): string {
  const value = metrics[key];
  if (typeof value === "number") return String(value);
  if (value === null) return primitiveText(value);
  return primitiveText(value);
}

const metricDefinitions: Array<[keyof PerformanceMetrics, TranslationKey]> = [
  ["sample_count", "common.samples"],
  ["actionable_count", "common.actionable"],
  ["resolved_actionable", "replay.resolvedActionable"],
  ["pending_actionable", "common.pendingActionable"],
  ["wait_count", "common.actionWait"],
  ["invalid_count", "common.invalid"],
  ["wins", "common.wins"],
  ["losses", "common.losses"],
  ["flats", "common.flats"],
  ["win_rate", "common.winRate"],
  ["avg_r", "common.averageR"],
  ["expectancy_r", "common.expectancyR"],
  ["profit_factor", "common.profitFactor"],
  ["max_drawdown_r", "common.maxDrawdownR"],
  ["mfe_r_avg", "common.mfeR"],
  ["mfe_r_median", "common.mfeR"],
  ["mfe_r_p90", "common.mfeR"],
  ["mae_r_avg", "common.maeR"],
  ["mae_r_median", "common.maeR"],
  ["mae_r_p90", "common.maeR"],
  ["timeout_count", "common.timeouts"],
  ["timeout_rate", "common.timeoutRate"],
  ["coverage", "common.coverage"],
  ["action_rate", "common.rate"],
  ["wait_rate", "common.rate"],
  ["brier_raw", "common.brier"],
  ["brier_calibrated", "common.brier"],
  ["ece_raw", "common.ece"],
  ["ece_calibrated", "common.ece"],
];

function summaryFacts(summary: PerformanceSummary, metrics: PerformanceMetrics, t: ReturnType<typeof useI18n>["t"], formatters: ReturnType<typeof useResearchFormatters>) {
  const facts = metricDefinitions.flatMap(([key, labelKey]) => {
    if (!(key in metrics)) return [];
    const value = metrics[key];
    if (typeof value === "number") {
      const keyName = String(key);
      const suffix = keyName.includes("median") ? ` · ${t("common.median")}` : keyName.includes("avg") ? ` · ${t("common.average")}` : keyName.includes("p90") ? " · p90" : keyName.endsWith("_raw") ? ` · ${t("common.rawLabel")}` : keyName.endsWith("_calibrated") ? ` · ${t("common.calibrated")}` : "";
      return [{ label: `${t(labelKey)}${suffix}`, value: keyName.includes("rate") || keyName.includes("coverage") || keyName.includes("brier") || keyName.includes("ece") || keyName === "win_rate" ? formatters.numberText(value, 6) : metricValue(metrics, key, formatters.primitiveText) }];
    }
    return [{ label: t(labelKey), value: formatters.primitiveText(value) }];
  });
  const topLevel = [
    { label: t("performance.windowStart"), value: formatters.timestampText(summary.window_start) },
    { label: t("performance.windowEnd"), value: formatters.timestampText(summary.window_end) },
  ];
  return [...topLevel, ...facts];
}

function SummaryPanel({ summary }: { summary: PerformanceSummary }) {
  const { t } = useI18n();
  const formatters = useResearchFormatters();
  const metrics = summary.metrics;
  if (!metrics) {
    return <p className="panel-reading">{t("performance.noMetrics")}</p>;
  }
  const resolved = metrics.resolved_actionable;
  const hasResolved = typeof resolved === "number" && Number.isFinite(resolved);
  const evidenceText = resolved === 0
    ? t("performance.noResolvedEvidence")
    : !hasResolved
      ? t("performance.resolvedMissingEvidence")
      : t("performance.resolvedEvidence");
  return (
    <div className="performance-summary-body">
      <p className="preliminary-banner" role="status"><strong>{summary.status ?? metrics.status ?? t("performance.statusMissing")}</strong> · {evidenceText}</p>
      <ResearchFacts facts={[
        { label: t("performance.scopeSource"), value: summary.source_type ?? formatters.primitiveText(summary.scope?.source_type) },
        { label: t("performance.scopeSymbol"), value: formatters.primitiveText(summary.scope?.symbol) },
        { label: t("performance.scopeTimeframe"), value: formatters.primitiveText(summary.scope?.timeframe) },
        { label: t("performance.scopeModel"), value: summary.model_id ?? formatters.primitiveText(summary.scope?.model_id) },
        { label: t("performance.scopePrompt"), value: summary.prompt_version ?? formatters.primitiveText(summary.scope?.prompt_version) },
        { label: t("performance.scopeReplayRun"), value: formatters.primitiveText(summary.scope?.replay_run_id) },
        { label: t("performance.totalSamples"), value: formatters.primitiveText(summary.sample_count) },
        { label: t("performance.actionableSamples"), value: formatters.primitiveText(summary.actionable_count) },
        { label: t("replay.resolvedActionable"), value: formatters.primitiveText(resolved) },
      ]} />
      <div className="record-detail__section"><p className="subsection-label">{t("performance.returnedFields")}</p><ResearchFacts compact facts={summaryFacts(summary, metrics, t, formatters)} /></div>
    </div>
  );
}

function BucketTable({ buckets }: { buckets: PerformanceBucket[] }) {
  const { t } = useI18n();
  const { exactNumberText, numberText, primitiveText } = useResearchFormatters();
  return (
    <div aria-label={t("performance.scrollBuckets")} className="table-wrap" tabIndex={0}>
      <table className="performance-table" aria-label={t("performance.buckets")}>
        <thead><tr><th scope="col">{t("performance.bucket")}</th><th scope="col">{t("common.count")}</th><th scope="col">{t("performance.rawAverageConfidence")}</th><th scope="col">{t("performance.calibratedConfidence")}</th><th scope="col">{t("performance.empiricalWinRate")}</th></tr></thead>
        <tbody>{buckets.map((bucket, index) => <tr key={`${bucket.bucket ?? "bucket"}-${index}`}>
          <td>{primitiveText(bucket.bucket)}</td>
          <td>{primitiveText(bucket.count)}</td>
          <td>{exactNumberText(bucket.raw_avg_confidence)}</td>
          <td>{exactNumberText(bucket.calibrated_confidence)}</td>
          <td>{numberText(bucket.empirical_win_rate, 6)}</td>
        </tr>)}</tbody>
      </table>
    </div>
  );
}

function BucketsPanel({ data }: { data: PerformanceBuckets }) {
  const { t } = useI18n();
  const buckets = data.confidence_buckets ?? [];
  return (
    <>
      <p className="panel-reading">{t("performance.bucketNote")}</p>
      {buckets.length > 0 ? <BucketTable buckets={buckets} /> : <p className="panel-reading">{t("performance.noBuckets")}</p>}
    </>
  );
}

function calibrationFacts(calibration: CalibrationCurrent, t: ReturnType<typeof useI18n>["t"], formatters: ReturnType<typeof useResearchFormatters>) {
  return [
    { label: t("common.status"), value: formatters.primitiveText(calibration.status) },
    { label: t("performance.calibrationId"), value: formatters.primitiveText(calibration.calibration_id) },
    { label: t("performance.scopeSource"), value: formatters.primitiveText(calibration.scope?.source_type) },
    { label: t("performance.scopeSymbol"), value: formatters.primitiveText(calibration.scope?.symbol) },
    { label: t("performance.scopeTimeframe"), value: formatters.primitiveText(calibration.scope?.timeframe) },
    { label: t("performance.scopeModel"), value: formatters.primitiveText(calibration.scope?.model_id) },
    { label: t("performance.scopePrompt"), value: formatters.primitiveText(calibration.scope?.prompt_version) },
    { label: t("performance.scopeReplayRun"), value: formatters.primitiveText(calibration.scope?.replay_run_id) },
    { label: t("performance.method"), value: formatters.primitiveText(calibration.method) },
    { label: t("common.version"), value: formatters.primitiveText(calibration.version) },
    { label: t("common.sample"), value: formatters.primitiveText(calibration.sample_count) },
    { label: t("performance.trainedUntil"), value: formatters.timestampText(calibration.trained_until) },
    { label: t("performance.fallback"), value: formatters.primitiveText(calibration.fallback) },
    { label: `${t("common.brier")} · ${t("common.rawLabel")}`, value: formatters.numberText(calibration.brier_raw, 6) },
    { label: `${t("common.brier")} · ${t("common.calibrated")}`, value: formatters.numberText(calibration.brier_calibrated, 6) },
    { label: `${t("common.ece")} · ${t("common.rawLabel")}`, value: formatters.numberText(calibration.ece_raw, 6) },
    { label: `${t("common.ece")} · ${t("common.calibrated")}`, value: formatters.numberText(calibration.ece_calibrated, 6) },
  ];
}

function CalibrationPanel({ data }: { data: CalibrationCurrent }) {
  const { t } = useI18n();
  const formatters = useResearchFormatters();
  const buckets = data.buckets ?? [];
  return (
    <>
      <p className="panel-reading">{t("performance.calibrationNote")}</p>
      <ResearchFacts facts={calibrationFacts(data, t, formatters)} />
      {buckets.length > 0 ? (
        <div aria-label={t("performance.scrollCalibration")} className="table-wrap" tabIndex={0}><table className="performance-table" aria-label={t("performance.calibration")}><thead><tr><th scope="col">{t("performance.lower")}</th><th scope="col">{t("performance.upper")}</th><th scope="col">{t("common.sample")}</th><th scope="col">{t("common.wins")}</th><th scope="col">{t("performance.empiricalRate")}</th><th scope="col">{t("performance.shrunkRate")}</th></tr></thead><tbody>{buckets.map((bucket, index) => <tr key={`${bucket.lower ?? "lower"}-${index}`}><td>{formatters.numberText(bucket.lower, 6)}</td><td>{formatters.numberText(bucket.upper, 6)}</td><td>{formatters.primitiveText(bucket.n)}</td><td>{formatters.primitiveText(bucket.wins)}</td><td>{formatters.numberText(bucket.empirical_rate, 6)}</td><td>{formatters.numberText(bucket.shrunk_rate, 6)}</td></tr>)}</tbody></table></div>
      ) : <p className="panel-reading">{t("performance.noCalibrationBuckets")}</p>}
    </>
  );
}

export function PerformancePage({ apiClient, onProvenanceChange }: PerformancePageProps) {
  const { t } = useI18n();
  const [draft, setDraft] = useState<FilterDraft>(initialDraft);
  const [filters, setFilters] = useState<PerformanceFilters>(initialFilters);

  const summaryLoader = useCallback((signal: AbortSignal) => apiClient.performanceSummary(filters, signal), [apiClient, filters]);
  const bucketFilters = useMemo(() => ({
    source_type: filters.source_type,
    ...(filters.model_id ? { model_id: filters.model_id } : {}),
    ...(filters.prompt_version ? { prompt_version: filters.prompt_version } : {}),
    ...(filters.replay_run_id ? { replay_run_id: filters.replay_run_id } : {}),
  }), [filters]);
  const bucketsLoader = useCallback((signal: AbortSignal) => apiClient.performanceBuckets(bucketFilters, signal), [apiClient, bucketFilters]);
  const calibrationLoader = useCallback((signal: AbortSignal) => apiClient.calibrationCurrent(signal), [apiClient]);
  const summary = useAsyncResource(summaryLoader);
  const buckets = useAsyncResource(bucketsLoader);
  const calibration = useAsyncResource(calibrationLoader);

  useEffect(() => {
    onProvenanceChange(emptyDashboardProvenance(t));
  }, [onProvenanceChange, t]);

  const applyFilters = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFilters(buildFilters(draft));
  };

  const summaryData = summary.data;
  const resolved = summaryData?.metrics?.resolved_actionable;
  const summaryPanelState: PanelState = summary.status === "loading"
    ? "loading"
    : summary.status === "unavailable"
      ? "unavailable"
      : !summaryData || !summaryData.metrics
        ? "empty"
        : typeof resolved !== "number" || !Number.isFinite(resolved) || resolved === 0
          ? "degraded"
          : "ready";
  const calibrationDegraded = calibration.data?.status !== undefined && calibration.data.status !== "ACTIVE";
  const calibrationDegradedMessage = calibration.data?.status === "INSUFFICIENT_SAMPLE"
    ? t("performance.insufficientCalibrationMessage")
    : calibration.data?.status
      ? `${t("performance.calibrationStatusPrefix")} ${calibration.data.status}; ${t("performance.inactiveCalibration")}`
      : undefined;
  const calibrationState: PanelState = calibration.status === "loading" ? "loading" : calibration.status === "unavailable" ? "unavailable" : !calibration.data ? "empty" : calibrationDegraded ? "degraded" : "ready";
  const summaryQuery = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) if (value !== undefined) summaryQuery.set(key, String(value));

  return (
    <section className="workflow-page performance-page" aria-labelledby="performance-title">
      <header className="page-intro">
        <p className="eyebrow">{t("performance.eyebrow")}</p>
        <h1 id="performance-title">{t("performance.title")}</h1>
        <p className="page-intro__description">{t("performance.description")}</p>
        <p className="page-boundary">{t("performance.boundary")}</p>
      </header>

      <form className="workflow-filters" aria-label={t("performance.filters")} onSubmit={applyFilters}>
        <div className="workflow-filters__grid">
          <label>{t("common.sourceType")}<select value={draft.source_type} onChange={(event) => setDraft((current) => ({ ...current, source_type: event.target.value === "replay" ? "replay" : "live" }))}><option value="live">{t("common.sourceLive")}</option><option value="replay">{t("common.sourceReplay")}</option></select></label>
          <label>{t("common.symbol")}<input value={draft.symbol} onChange={(event) => setDraft((current) => ({ ...current, symbol: event.target.value }))} placeholder={t("predictions.allSymbols")} /></label>
          <label>{t("common.timeframe")}<select value={draft.timeframe} onChange={(event) => setDraft((current) => ({ ...current, timeframe: timeframes.includes(event.target.value as Timeframe) ? event.target.value as Timeframe : "" }))}><option value="">{t("performance.allTimeframes")}</option>{timeframes.map((value) => <option key={value}>{value}</option>)}</select></label>
          <label>{t("common.modelId")}<input value={draft.model_id} onChange={(event) => setDraft((current) => ({ ...current, model_id: event.target.value }))} placeholder={t("performance.allModels")} /></label>
          <label>{t("common.promptVersion")}<input value={draft.prompt_version} onChange={(event) => setDraft((current) => ({ ...current, prompt_version: event.target.value }))} placeholder={t("performance.allPrompts")} /></label>
          <label>{t("common.replayRun")}<input value={draft.replay_run_id} onChange={(event) => setDraft((current) => ({ ...current, replay_run_id: event.target.value }))} placeholder={t("performance.allRuns")} /></label>
        </div>
        <button className="primary-button" type="submit">{t("common.applyFilters")}</button>
      </form>

      <div className="performance-grid">
        <AsyncPanel className="workflow-panel performance-panel performance-panel--summary" title={t("performance.summary")} source={`GET /performance/summary${summaryQuery.toString() ? `?${summaryQuery.toString()}` : ""}`} freshness={t("performance.summaryFreshness")} state={summaryPanelState} error={summary.error} onRetry={summary.retry} emptyMessage={t("performance.noSummary")} degradedMessage={typeof resolved === "number" && resolved === 0 ? t("performance.zeroResolved") : t("performance.noResolved")}>
          {summaryData ? <SummaryPanel summary={summaryData} /> : null}
        </AsyncPanel>

        <AsyncPanel className="workflow-panel performance-panel" title={t("performance.buckets")} source="GET /performance/buckets (source/model/prompt/replay scope)" freshness={t("performance.bucketFreshness")} state={resourceState(buckets, (buckets.data?.confidence_buckets?.length ?? 0) === 0)} error={buckets.error} onRetry={buckets.retry} emptyMessage={t("performance.noBuckets")}>
          {buckets.data ? <BucketsPanel data={buckets.data} /> : null}
        </AsyncPanel>

        <AsyncPanel className="workflow-panel performance-panel" title={t("performance.calibration")} source="GET /calibration/current (independent artifact)" freshness={t("performance.calibrationFreshness")} state={calibrationState} error={calibration.error} onRetry={calibration.retry} emptyMessage={t("performance.noCalibration")} degradedMessage={calibrationDegradedMessage}>
          {calibration.data ? <CalibrationPanel data={calibration.data} /> : null}
        </AsyncPanel>
      </div>
    </section>
  );
}
