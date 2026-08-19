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
import { ResearchFacts, exactNumberText, numberText, primitiveText, timestampText } from "../components/ResearchFacts";
import type { AsyncResource } from "../hooks/useAsyncResource";
import { useAsyncResource } from "../hooks/useAsyncResource";
import { EMPTY_DASHBOARD_PROVENANCE, type DashboardProvenance } from "./DashboardPage";

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

function metricValue(metrics: PerformanceMetrics, key: keyof PerformanceMetrics): string {
  const value = metrics[key];
  if (typeof value === "number") return String(value);
  if (value === null) return "Not supplied";
  return primitiveText(value);
}

const metricDefinitions: Array<[keyof PerformanceMetrics, string]> = [
  ["sample_count", "Samples"],
  ["actionable_count", "Actionable"],
  ["resolved_actionable", "Resolved actionable"],
  ["pending_actionable", "Pending actionable"],
  ["wait_count", "WAIT"],
  ["invalid_count", "Invalid"],
  ["wins", "Wins"],
  ["losses", "Losses"],
  ["flats", "Flats"],
  ["win_rate", "Win rate"],
  ["avg_r", "Average R"],
  ["expectancy_r", "Expectancy R"],
  ["profit_factor", "Profit factor"],
  ["max_drawdown_r", "Max drawdown R"],
  ["mfe_r_avg", "MFE R average"],
  ["mfe_r_median", "MFE R median"],
  ["mfe_r_p90", "MFE R p90"],
  ["mae_r_avg", "MAE R average"],
  ["mae_r_median", "MAE R median"],
  ["mae_r_p90", "MAE R p90"],
  ["timeout_count", "Timeouts"],
  ["timeout_rate", "Timeout rate"],
  ["coverage", "Coverage"],
  ["action_rate", "Action rate"],
  ["wait_rate", "WAIT rate"],
  ["brier_raw", "Brier · raw"],
  ["brier_calibrated", "Brier · calibrated"],
  ["ece_raw", "ECE · raw"],
  ["ece_calibrated", "ECE · calibrated"],
];

function summaryFacts(summary: PerformanceSummary, metrics: PerformanceMetrics) {
  const facts = metricDefinitions.flatMap(([key, label]) => {
    if (!(key in metrics)) return [];
    const value = metrics[key];
    if (typeof value === "number") {
      const keyName = String(key);
      return [{ label, value: keyName.includes("rate") || keyName.includes("coverage") || keyName.includes("brier") || keyName.includes("ece") || keyName === "win_rate" ? numberText(value, 6) : metricValue(metrics, key) }];
    }
    return [{ label, value: value === null ? "Not supplied" : primitiveText(value) }];
  });
  const topLevel = [
    { label: "Window start", value: timestampText(summary.window_start) },
    { label: "Window end", value: timestampText(summary.window_end) },
  ];
  return [...topLevel, ...facts];
}

function SummaryPanel({ summary }: { summary: PerformanceSummary }) {
  const metrics = summary.metrics;
  if (!metrics) {
    return <p className="panel-reading">The response did not supply metrics. No performance success state is inferred.</p>;
  }
  const resolved = metrics.resolved_actionable;
  const hasResolved = typeof resolved === "number" && Number.isFinite(resolved);
  const evidenceText = resolved === 0
    ? "No resolved actionable sample is available. Returned primitive metrics remain metadata, not a success claim."
    : !hasResolved
      ? "Resolved sample size was not supplied; no performance success claim is made."
      : "Resolved actionable evidence is available for the returned scope.";
  return (
    <div className="performance-summary-body">
      <p className="preliminary-banner" role="status"><strong>{summary.status ?? metrics.status ?? "Status not supplied"}</strong> · {evidenceText}</p>
      <ResearchFacts facts={[
        { label: "Scope source", value: summary.source_type ?? primitiveText(summary.scope?.source_type) },
        { label: "Scope symbol", value: primitiveText(summary.scope?.symbol) },
        { label: "Scope timeframe", value: primitiveText(summary.scope?.timeframe) },
        { label: "Scope model", value: summary.model_id ?? primitiveText(summary.scope?.model_id) },
        { label: "Scope prompt", value: summary.prompt_version ?? primitiveText(summary.scope?.prompt_version) },
        { label: "Scope replay run", value: primitiveText(summary.scope?.replay_run_id) },
        { label: "Total samples", value: summary.sample_count === undefined ? "Not supplied" : String(summary.sample_count) },
        { label: "Actionable samples", value: summary.actionable_count === undefined ? "Not supplied" : String(summary.actionable_count) },
        { label: "Resolved actionable", value: resolved === undefined ? "Not supplied" : String(resolved) },
      ]} />
      <div className="record-detail__section"><p className="subsection-label">Returned performance fields</p><ResearchFacts compact facts={summaryFacts(summary, metrics)} /></div>
    </div>
  );
}

function BucketTable({ buckets }: { buckets: PerformanceBucket[] }) {
  return (
    <div className="table-wrap">
      <table className="performance-table" aria-label="Performance confidence buckets">
        <thead><tr><th scope="col">Bucket</th><th scope="col">Count</th><th scope="col">Raw average confidence</th><th scope="col">Calibrated confidence</th><th scope="col">Empirical win rate</th></tr></thead>
        <tbody>{buckets.map((bucket, index) => <tr key={`${bucket.bucket ?? "bucket"}-${index}`}>
          <td>{bucket.bucket ?? "Not supplied"}</td>
          <td>{bucket.count === undefined ? "Not supplied" : String(bucket.count)}</td>
          <td>{exactNumberText(bucket.raw_avg_confidence)}</td>
          <td>{exactNumberText(bucket.calibrated_confidence)}</td>
          <td>{numberText(bucket.empirical_win_rate, 6)}</td>
        </tr>)}</tbody>
      </table>
    </div>
  );
}

function BucketsPanel({ data }: { data: PerformanceBuckets }) {
  const buckets = data.confidence_buckets ?? [];
  return (
    <>
      <p className="panel-reading">Raw and calibrated confidence columns are returned bucket values; this endpoint scope does not include the selected symbol or timeframe.</p>
      {buckets.length > 0 ? <BucketTable buckets={buckets} /> : <p className="panel-reading">No confidence buckets were returned for this scope.</p>}
    </>
  );
}

function calibrationFacts(calibration: CalibrationCurrent) {
  return [
    { label: "Status", value: calibration.status ?? "Not supplied" },
    { label: "Calibration id", value: calibration.calibration_id ?? "Not supplied" },
    { label: "Scope source", value: primitiveText(calibration.scope?.source_type) },
    { label: "Scope symbol", value: primitiveText(calibration.scope?.symbol) },
    { label: "Scope timeframe", value: primitiveText(calibration.scope?.timeframe) },
    { label: "Scope model", value: primitiveText(calibration.scope?.model_id) },
    { label: "Scope prompt", value: primitiveText(calibration.scope?.prompt_version) },
    { label: "Scope replay run", value: primitiveText(calibration.scope?.replay_run_id) },
    { label: "Method", value: calibration.method ?? "Not supplied" },
    { label: "Version", value: calibration.version ?? "Not supplied" },
    { label: "Sample", value: calibration.sample_count === undefined ? "Not supplied" : String(calibration.sample_count) },
    { label: "Trained until", value: timestampText(calibration.trained_until) },
    { label: "Fallback", value: calibration.fallback ?? "Not supplied" },
    { label: "Brier · raw", value: numberText(calibration.brier_raw, 6) },
    { label: "Brier · calibrated", value: numberText(calibration.brier_calibrated, 6) },
    { label: "ECE · raw", value: numberText(calibration.ece_raw, 6) },
    { label: "ECE · calibrated", value: numberText(calibration.ece_calibrated, 6) },
  ];
}

function CalibrationPanel({ data }: { data: CalibrationCurrent }) {
  const buckets = data.buckets ?? [];
  return (
    <>
      <p className="panel-reading">Current calibration is a separate artifact and is not assumed to match arbitrary Performance filters.</p>
      <ResearchFacts facts={calibrationFacts(data)} />
      {buckets.length > 0 ? (
        <div className="table-wrap"><table className="performance-table" aria-label="Current calibration buckets"><thead><tr><th scope="col">Lower</th><th scope="col">Upper</th><th scope="col">Sample</th><th scope="col">Wins</th><th scope="col">Empirical rate</th><th scope="col">Shrunk rate</th></tr></thead><tbody>{buckets.map((bucket, index) => <tr key={`${bucket.lower ?? "lower"}-${index}`}><td>{numberText(bucket.lower, 6)}</td><td>{numberText(bucket.upper, 6)}</td><td>{bucket.n === undefined ? "Not supplied" : String(bucket.n)}</td><td>{bucket.wins === undefined ? "Not supplied" : String(bucket.wins)}</td><td>{numberText(bucket.empirical_rate, 6)}</td><td>{numberText(bucket.shrunk_rate, 6)}</td></tr>)}</tbody></table></div>
      ) : <p className="panel-reading">No current calibration buckets were returned.</p>}
    </>
  );
}

export function PerformancePage({ apiClient, onProvenanceChange }: PerformancePageProps) {
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
    onProvenanceChange(EMPTY_DASHBOARD_PROVENANCE);
  }, [onProvenanceChange]);

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
    ? "Calibration status is INSUFFICIENT_SAMPLE; no fitted calibration is active. Returned fields remain visible for inspection."
    : calibration.data?.status
      ? `Calibration status is ${calibration.data.status}; only ACTIVE is treated as the fitted current calibration. Returned fields remain visible for inspection.`
      : undefined;
  const calibrationState: PanelState = calibration.status === "loading" ? "loading" : calibration.status === "unavailable" ? "unavailable" : !calibration.data ? "empty" : calibrationDegraded ? "degraded" : "ready";
  const summaryQuery = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) if (value !== undefined) summaryQuery.set(key, String(value));

  return (
    <section className="workflow-page performance-page" aria-labelledby="performance-title">
      <header className="page-intro">
        <p className="eyebrow">Outcome evidence / preliminary</p>
        <h1 id="performance-title">Performance</h1>
        <p className="page-intro__description">Inspect returned outcome metrics, confidence buckets and the separately scoped current calibration artifact.</p>
        <p className="page-boundary">PRELIMINARY is shown as returned. A zero resolved actionable sample is insufficient evidence, not a successful zero-result performance view.</p>
      </header>

      <form className="workflow-filters" aria-label="Performance filters" onSubmit={applyFilters}>
        <div className="workflow-filters__grid">
          <label>Source type<select value={draft.source_type} onChange={(event) => setDraft((current) => ({ ...current, source_type: event.target.value === "replay" ? "replay" : "live" }))}><option value="live">live</option><option value="replay">replay</option></select></label>
          <label>Symbol<input value={draft.symbol} onChange={(event) => setDraft((current) => ({ ...current, symbol: event.target.value }))} placeholder="All symbols" /></label>
          <label>Timeframe<select value={draft.timeframe} onChange={(event) => setDraft((current) => ({ ...current, timeframe: timeframes.includes(event.target.value as Timeframe) ? event.target.value as Timeframe : "" }))}><option value="">All timeframes</option>{timeframes.map((value) => <option key={value}>{value}</option>)}</select></label>
          <label>Model id<input value={draft.model_id} onChange={(event) => setDraft((current) => ({ ...current, model_id: event.target.value }))} placeholder="All models" /></label>
          <label>Prompt version<input value={draft.prompt_version} onChange={(event) => setDraft((current) => ({ ...current, prompt_version: event.target.value }))} placeholder="All prompts" /></label>
          <label>Replay run<input value={draft.replay_run_id} onChange={(event) => setDraft((current) => ({ ...current, replay_run_id: event.target.value }))} placeholder="All runs" /></label>
        </div>
        <button className="primary-button" type="submit">Apply filters</button>
      </form>

      <div className="performance-grid">
        <AsyncPanel className="workflow-panel performance-panel performance-panel--summary" title="Performance summary" source={`GET /performance/summary${summaryQuery.toString() ? `?${summaryQuery.toString()}` : ""}`} freshness="window timestamps and scope returned by API" state={summaryPanelState} error={summary.error} onRetry={summary.retry} emptyMessage="No performance summary fields were returned." degradedMessage={typeof resolved === "number" && resolved === 0 ? "Resolved actionable is exactly zero; this is insufficient evidence for a success state." : "Resolved actionable was not supplied; no success state is inferred."}>
          {summaryData ? <SummaryPanel summary={summaryData} /> : null}
        </AsyncPanel>

        <AsyncPanel className="workflow-panel performance-panel" title="Confidence buckets" source="GET /performance/buckets (source/model/prompt/replay scope)" freshness="bucket values returned by API" state={resourceState(buckets, (buckets.data?.confidence_buckets?.length ?? 0) === 0)} error={buckets.error} onRetry={buckets.retry} emptyMessage="No confidence bucket rows were returned.">
          {buckets.data ? <BucketsPanel data={buckets.data} /> : null}
        </AsyncPanel>

        <AsyncPanel className="workflow-panel performance-panel" title="Current calibration" source="GET /calibration/current (independent artifact)" freshness="trained_until when supplied" state={calibrationState} error={calibration.error} onRetry={calibration.retry} emptyMessage="No current calibration artifact was returned." degradedMessage={calibrationDegradedMessage}>
          {calibration.data ? <CalibrationPanel data={calibration.data} /> : null}
        </AsyncPanel>
      </div>
    </section>
  );
}
