import { useCallback, useEffect, useMemo } from "react";

import type { ApplicationShellApiClient } from "../api/client";
import type { PerformanceSummary, Prediction, JsonRecord } from "../api/types";
import type { ProvenanceRailState } from "../components/TimeProvenanceRail";
import type { AsyncResource } from "../hooks/useAsyncResource";
import { useAsyncResource } from "../hooks/useAsyncResource";
import { AsyncPanel, type PanelState } from "../components/AsyncPanel";
import { CountLedger, HealthFacts, InstrumentRoster, ModelFacts, ProviderRouting } from "../components/OperationalFacts";

export interface DashboardProvenance {
  generatedAt?: string;
  reevaluateAt?: string;
  expiresAt?: string;
  dataSource: string;
  model: string;
  state: ProvenanceRailState;
  footer: string;
}

export const EMPTY_DASHBOARD_PROVENANCE: DashboardProvenance = {
  dataSource: "No prediction record supplied",
  model: "No prediction record supplied",
  state: "neutral",
  footer: "No recent prediction with usable provenance was returned.",
};

interface DashboardPageProps {
  apiClient: ApplicationShellApiClient;
  onProvenanceChange: (provenance: DashboardProvenance) => void;
}

function panelState<T>(resource: AsyncResource<T>, empty: boolean): PanelState {
  if (resource.status === "loading") {
    return "loading";
  }
  if (resource.status === "unavailable") {
    return "unavailable";
  }
  return empty ? "empty" : "ready";
}

function parseTimestamp(value: unknown): Date | undefined {
  if (typeof value !== "string" || value.length === 0) {
    return undefined;
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? undefined : parsed;
}

function timestampText(value: unknown): string {
  if (typeof value !== "string" || value.length === 0) {
    return "Not supplied";
  }
  const parsed = parseTimestamp(value);
  return parsed ? parsed.toISOString() : "Not parseable";
}

function stringField(record: JsonRecord, key: string): string | undefined {
  const value = record[key];
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function numericField(record: JsonRecord, key: string): number | undefined {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function valueText(value: unknown): string {
  if (value === null) {
    return "null";
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value) ?? "Not displayable";
  } catch {
    return "Not displayable";
  }
}

function actionText(action: Prediction["action"]): string {
  if (action === "LONG" || action === "SHORT") {
    return `${action} · actionable`;
  }
  if (action === "WAIT") {
    return "WAIT · coverage only";
  }
  return "Action not supplied";
}

function confidenceText(prediction: Prediction): string {
  const values: string[] = [];
  if (typeof prediction.raw_confidence === "number") {
    values.push(`Raw ${prediction.raw_confidence}`);
  }
  if (typeof prediction.calibrated_confidence === "number") {
    values.push(`Calibrated ${prediction.calibrated_confidence}`);
  }
  return values.length > 0 ? values.join(" · ") : "Confidence not supplied";
}

function validityText(prediction: Prediction): string {
  const rawExpiry = prediction.signal_valid_until;
  const expiry = parseTimestamp(rawExpiry);
  if (!expiry) {
    return typeof rawExpiry === "string" && rawExpiry.length > 0 ? "Validity not parseable" : "Validity not supplied";
  }
  const status = expiry.getTime() <= Date.now() ? "Expired" : "Active";
  return `Valid until ${expiry.toISOString()} · ${status}`;
}

function latestPrediction(predictions: Prediction[]): Prediction | undefined {
  return predictions
    .map((prediction, index) => ({ prediction, index, generated: parseTimestamp(prediction.generated_at) }))
    .sort((left, right) => {
      if (left.generated && right.generated) {
        return right.generated.getTime() - left.generated.getTime();
      }
      if (left.generated) {
        return -1;
      }
      if (right.generated) {
        return 1;
      }
      return left.index - right.index;
    })[0]?.prediction;
}

function provenanceFor(predictions: Prediction[]): DashboardProvenance {
  const prediction = latestPrediction(predictions);
  if (!prediction) {
    return EMPTY_DASHBOARD_PROVENANCE;
  }

  const expiry = parseTimestamp(prediction.signal_valid_until);
  const modelId = stringField(prediction, "model_id");
  const sourceType = prediction.source_type;
  return {
    generatedAt: typeof prediction.generated_at === "string" ? prediction.generated_at : undefined,
    expiresAt: typeof prediction.signal_valid_until === "string" ? prediction.signal_valid_until : undefined,
    dataSource: sourceType ? `source_type: ${sourceType}` : "Source type not supplied by prediction record",
    model: modelId ? `model_id: ${modelId}` : "Model not supplied by prediction record",
    state: expiry ? (expiry.getTime() <= Date.now() ? "expired" : "active") : "neutral",
    footer: `Prediction ${prediction.prediction_id} selected from the recent ledger. Re-evaluation time was not supplied.`,
  };
}

function primitiveEntries(record: JsonRecord): Array<[string, unknown]> {
  return Object.entries(record).filter(([, value]) => value === null || ["string", "number", "boolean"].includes(typeof value));
}

function performanceMetrics(summary: PerformanceSummary): JsonRecord {
  return summary.metrics ?? {};
}

function resolvedActionableCount(summary: PerformanceSummary): number | undefined {
  return numericField(performanceMetrics(summary), "resolved_actionable");
}

function performanceState(resource: AsyncResource<PerformanceSummary>): PanelState {
  if (resource.status === "loading") {
    return "loading";
  }
  if (resource.status === "unavailable") {
    return "unavailable";
  }
  const metrics = performanceMetrics(resource.data ?? {});
  if (Object.keys(metrics).length === 0) {
    return "empty";
  }
  const resolvedActionable = resolvedActionableCount(resource.data ?? {});
  return resolvedActionable === undefined || resolvedActionable === 0 ? "degraded" : "ready";
}

function PerformanceView({ summary }: { summary: PerformanceSummary }) {
  const metrics = performanceMetrics(summary);
  const resolvedActionable = resolvedActionableCount(summary);
  const scope = summary.scope ?? {};
  const scopeSource = stringField(scope, "source_type") ?? "not supplied by response";
  return (
    <div className="performance-view">
      <p className="panel-reading">Returned scope: {scopeSource}</p>
      {resolvedActionable === undefined ? (
        <p className="panel-degraded-note">Resolved sample size was not supplied; no performance success claim is made.</p>
      ) : (
        <p className="panel-reading">Resolved actionable sample: {resolvedActionable}</p>
      )}
      <dl className="metric-ledger">
        {primitiveEntries(metrics).map(([key, value]) => (
          <div className="metric-ledger__row" key={key}>
            <dt>{key.replaceAll("_", " ")}</dt>
            <dd>{valueText(value)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function PredictionLedger({ predictions }: { predictions: Prediction[] }) {
  return (
    <div className="prediction-ledger-wrap">
      <table className="prediction-ledger">
        <caption className="visually-hidden">Recent prediction records</caption>
        <thead>
          <tr>
            <th scope="col">Record</th>
            <th scope="col">Action</th>
            <th scope="col">Source / frame</th>
            <th scope="col">Confidence</th>
            <th scope="col">Validity</th>
          </tr>
        </thead>
        <tbody>
          {predictions.map((prediction) => (
            <tr key={prediction.prediction_id}>
              <td data-label="Record">
                <span className="data-strong">{prediction.symbol ?? "Symbol not supplied"}</span>
                <span className="data-meta">{prediction.prediction_id}</span>
                <span className="data-meta">Generated {timestampText(prediction.generated_at)}</span>
              </td>
              <td data-label="Action">
                <span className={`signal-text signal-text--${prediction.action?.toLowerCase() ?? "unknown"}`}>
                  {actionText(prediction.action)}
                </span>
              </td>
              <td data-label="Source / frame">
                <span>{prediction.source_type ?? "Source type not supplied"}</span>
                <span className="data-meta">{prediction.timeframe ?? "Timeframe not supplied"}</span>
              </td>
              <td data-label="Confidence">{confidenceText(prediction)}</td>
              <td data-label="Validity">{validityText(prediction)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function DashboardPage({ apiClient, onProvenanceChange }: DashboardPageProps) {
  const healthLoader = useCallback((signal: AbortSignal) => apiClient.health(signal), [apiClient]);
  const providerLoader = useCallback((signal: AbortSignal) => apiClient.providerHealth(signal), [apiClient]);
  const modelLoader = useCallback((signal: AbortSignal) => apiClient.modelHealth(signal), [apiClient]);
  const statsLoader = useCallback((signal: AbortSignal) => apiClient.stats(signal), [apiClient]);
  const instrumentsLoader = useCallback((signal: AbortSignal) => apiClient.instruments(signal), [apiClient]);
  const predictionsLoader = useCallback((signal: AbortSignal) => apiClient.predictions({ limit: 8 }, signal), [apiClient]);
  const performanceLoader = useCallback(
    (signal: AbortSignal) => apiClient.performanceSummary({ source_type: "live" }, signal),
    [apiClient],
  );

  const health = useAsyncResource(healthLoader);
  const provider = useAsyncResource(providerLoader);
  const model = useAsyncResource(modelLoader);
  const stats = useAsyncResource(statsLoader);
  const instruments = useAsyncResource(instrumentsLoader);
  const predictions = useAsyncResource(predictionsLoader);
  const performance = useAsyncResource(performanceLoader);

  const provenance = useMemo(() => provenanceFor(predictions.data ?? []), [predictions.data]);
  useEffect(() => onProvenanceChange(provenance), [onProvenanceChange, provenance]);

  const providerHasContent = Boolean(provider.data && ((provider.data.routes?.length ?? 0) > 0 || provider.data.news));
  const modelHasContent = Boolean(model.data && Object.keys(model.data).length > 0);
  const statsHasContent = Boolean(stats.data && Object.keys(stats.data).length > 0);
  const healthDegraded = health.status === "ready" && health.data?.status !== "ok";
  const providerDegraded = provider.status === "ready" && provider.data?.available === false;
  const modelDegraded = model.status === "ready" && model.data?.available === false;

  return (
    <section className="dashboard-page" aria-labelledby="dashboard-title">
      <header className="page-intro">
        <p className="eyebrow">Current evidence / local API</p>
        <h1 id="dashboard-title">Dashboard</h1>
        <p className="page-intro__description">
          A read-only desk for orientation, provenance and the records returned by the local research backend.
        </p>
        <p className="page-boundary">No radar ranking, watchlist scan or trading action is performed here.</p>
      </header>

      <div className="dashboard-grid">
        <AsyncPanel
          title="Backend API"
          source="GET /health"
          freshness="endpoint timestamp not supplied"
          state={healthDegraded ? "degraded" : panelState(health, false)}
          error={health.error}
          onRetry={health.retry}
          degradedMessage="The backend answered with a non-ok status; this does not describe model or provider health."
          className="dashboard-panel dashboard-panel--health"
        >
          {health.data ? <HealthFacts health={health.data} /> : null}
        </AsyncPanel>

        <AsyncPanel
          title="Provider routing"
          source="GET /health/providers"
          freshness="probe response; timestamp not supplied"
          state={providerDegraded ? "degraded" : panelState(provider, !providerHasContent)}
          error={provider.error}
          onRetry={provider.retry}
          emptyMessage="No provider routes or news routing details were returned."
          degradedMessage="Provider routing is reporting unavailable. Backend health is tracked separately."
          className="dashboard-panel"
        >
          {provider.data ? <ProviderRouting provider={provider.data} /> : null}
        </AsyncPanel>

        <AsyncPanel
          title="Model health"
          source="GET /health/model"
          freshness="health probe response; timestamp not supplied"
          state={modelDegraded ? "degraded" : panelState(model, !modelHasContent)}
          error={model.error}
          onRetry={model.retry}
          emptyMessage="The model endpoint returned no health fields."
          degradedMessage="The model is unavailable or not configured; the backend may still be connected."
          className="dashboard-panel"
        >
          {model.data ? <ModelFacts model={model.data} /> : null}
        </AsyncPanel>

        <AsyncPanel
          title="Stored counts"
          source="GET /stats"
          freshness="count response; timestamp not supplied"
          state={panelState(stats, !statsHasContent)}
          error={stats.error}
          onRetry={stats.retry}
          emptyMessage="No count keys were returned by the stats endpoint."
          className="dashboard-panel"
        >
          {stats.data ? <CountLedger stats={stats.data} /> : null}
        </AsyncPanel>

        <AsyncPanel
          title="Instrument roster"
          source="GET /instruments"
          freshness="roster response; quote freshness not supplied"
          state={panelState(instruments, (instruments.data?.length ?? 0) === 0)}
          error={instruments.error}
          onRetry={instruments.retry}
          emptyMessage="No instruments were returned by the backend universe."
          className="dashboard-panel"
        >
          {instruments.data ? <InstrumentRoster instruments={instruments.data} /> : null}
        </AsyncPanel>

        <AsyncPanel
          title="Recent predictions"
          source="GET /predictions?limit=8"
          freshness="generated_at and signal_valid_until shown per record when supplied"
          state={panelState(predictions, (predictions.data?.length ?? 0) === 0)}
          error={predictions.error}
          onRetry={predictions.retry}
          emptyMessage="No prediction records were returned for this recent ledger."
          className="dashboard-panel dashboard-panel--wide"
        >
          {predictions.data ? <PredictionLedger predictions={predictions.data} /> : null}
        </AsyncPanel>

        <AsyncPanel
          title="Live performance"
          source="GET /performance/summary?source_type=live"
          freshness="summary response; no as-of timestamp supplied"
          state={performanceState(performance)}
          error={performance.error}
          onRetry={performance.retry}
          emptyMessage="No performance metrics were returned; there is no resolved sample to summarize."
          degradedMessage="No resolved sample is reported. Returned metrics are shown as metadata only."
          className="dashboard-panel dashboard-panel--wide"
        >
          {performance.data ? <PerformanceView summary={performance.data} /> : null}
        </AsyncPanel>
      </div>
    </section>
  );
}
