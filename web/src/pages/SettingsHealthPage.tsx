import { useCallback, useState } from "react";

import type { ApplicationShellApiClient } from "../api/client";
import { AsyncPanel, type PanelState } from "../components/AsyncPanel";
import { CountLedger, HealthFacts, ModelFacts, ProviderRouting } from "../components/OperationalFacts";
import type { AlertStatusResponse, SchedulerHistory, SchedulerStatus } from "../api/types";
import type { AsyncResource } from "../hooks/useAsyncResource";
import { useAsyncResource } from "../hooks/useAsyncResource";

interface SettingsHealthPageProps {
  apiClient: ApplicationShellApiClient;
}

function resourceState<T>(resource: AsyncResource<T>, empty: boolean): PanelState {
  if (resource.status === "loading") {
    return "loading";
  }
  if (resource.status === "unavailable") {
    return "unavailable";
  }
  return empty ? "empty" : "ready";
}

function schedulerState(resource: AsyncResource<SchedulerStatus>): PanelState {
  if (resource.status === "loading") {
    return "loading";
  }
  if (resource.status === "unavailable") {
    return "unavailable";
  }
  const status = resource.data;
  if (!status) {
    return "empty";
  }
  const resourceAvailable = status.resource?.available;
  const backoffActive = status.backoff?.active;
  return resourceAvailable === false || backoffActive === true || Boolean(status.last_error) ? "degraded" : "ready";
}

function alertState(resource: AsyncResource<AlertStatusResponse>): PanelState {
  if (resource.status === "loading") return "loading";
  if (resource.status === "unavailable") return "unavailable";
  if (!resource.data) return "empty";
  return resource.data.last_reconciliation?.status === "COMPLETED_WITH_ERRORS" ? "degraded" : "ready";
}

function schedulerText(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "Not supplied";
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value) ?? "Not displayable";
}

function SchedulerFacts({ status, history }: { status: SchedulerStatus; history?: SchedulerHistory }) {
  const lastRun = status.last_run;
  const counts = lastRun?.counts;
  return (
    <div className="scheduler-facts">
      <dl className="fact-list fact-list--compact">
        <div className="fact-list__row"><dt>Lifecycle</dt><dd>{status.state} · enabled {String(status.enabled)} · running {String(status.running)}</dd></div>
        <div className="fact-list__row"><dt>Interval / session</dt><dd>{status.interval_seconds}s · {status.session_policy}</dd></div>
        <div className="fact-list__row"><dt>Concurrency</dt><dd>configured {status.configured_concurrency} · effective model limit {status.effective_concurrency}</dd></div>
        <div className="fact-list__row"><dt>Last run</dt><dd>{lastRun ? `${lastRun.status} · ${lastRun.started_at}` : "No scan run recorded"}</dd></div>
        <div className="fact-list__row"><dt>Next run</dt><dd>{schedulerText(status.next_run_at)}</dd></div>
        <div className="fact-list__row"><dt>Last counts</dt><dd>{counts ? schedulerText(counts) : "No item counts recorded"}</dd></div>
        <div className="fact-list__row"><dt>Resource</dt><dd>{schedulerText(status.resource)}</dd></div>
        <div className="fact-list__row"><dt>Backoff</dt><dd>{schedulerText(status.backoff)}</dd></div>
        <div className="fact-list__row"><dt>Cache</dt><dd>{schedulerText(status.cache)}</dd></div>
        <div className="fact-list__row"><dt>Settlement</dt><dd>{status.settlement ? schedulerText(status.settlement) : "No settlement run recorded"}</dd></div>
        <div className="fact-list__row"><dt>Performance refresh</dt><dd>{status.performance_refresh ? schedulerText(status.performance_refresh) : "No live refresh recorded"}</dd></div>
      </dl>
      <p className="panel-reading">Model analysis is serial (effective concurrency 1). Scheduler cache metadata survives restart, while cached context is intentionally cold after restart.</p>
      <p className="panel-reading">Outcome settlement is deterministic, point-in-time and read-only for live records; it never creates PaperTrades or real orders. Alerts are local SQLite observability only; no broker connectivity or outbound notifier is activated. Regular-equity session checks omit exchange holiday calendars; `always` is an explicit user override.</p>
      {history && history.runs.length > 0 ? (
        <ul className="scheduler-history" aria-label="Recent scheduler runs">
          {history.runs.slice(0, 3).map((run) => (
            <li key={run.run_id}><strong>{run.status}</strong> · {run.trigger} · {run.started_at}</li>
          ))}
        </ul>
      ) : <p className="panel-reading">No scheduler history is available.</p>}
    </div>
  );
}

export function SettingsHealthPage({ apiClient }: SettingsHealthPageProps) {
  const healthLoader = useCallback((signal: AbortSignal) => apiClient.health(signal), [apiClient]);
  const providerLoader = useCallback((signal: AbortSignal) => apiClient.providerHealth(signal), [apiClient]);
  const modelLoader = useCallback((signal: AbortSignal) => apiClient.modelHealth(signal), [apiClient]);
  const statsLoader = useCallback((signal: AbortSignal) => apiClient.stats(signal), [apiClient]);
  const schedulerLoader = useCallback((signal: AbortSignal) => apiClient.schedulerStatus(signal), [apiClient]);
  const schedulerHistoryLoader = useCallback((signal: AbortSignal) => apiClient.schedulerHistory(5, signal), [apiClient]);
  const alertLoader = useCallback((signal: AbortSignal) => apiClient.alertStatus(signal), [apiClient]);

  const health = useAsyncResource(healthLoader);
  const provider = useAsyncResource(providerLoader);
  const model = useAsyncResource(modelLoader);
  const stats = useAsyncResource(statsLoader);
  const scheduler = useAsyncResource(schedulerLoader);
  const schedulerHistory = useAsyncResource(schedulerHistoryLoader);
  const alerts = useAsyncResource(alertLoader);
  const [action, setAction] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const healthDegraded = health.status === "ready" && health.data?.status !== "ok";
  const providerDegraded = provider.status === "ready" && provider.data?.available === false;
  const modelDegraded = model.status === "ready" && model.data?.available === false;
  const providerHasContent = Boolean(provider.data && ((provider.data.routes?.length ?? 0) > 0 || provider.data.news));
  const modelHasContent = Boolean(model.data && Object.keys(model.data).length > 0);
  const statsHasContent = Boolean(stats.data && Object.keys(stats.data).length > 0);

  async function runSchedulerAction(name: "enable" | "disable" | "start" | "stop" | "run") {
    setAction(name);
    setActionError(null);
    try {
      if (name === "enable") {
        await apiClient.updateAppSetting("scheduler.enabled", true);
      } else if (name === "disable") {
        await apiClient.updateAppSetting("scheduler.enabled", false);
      } else if (name === "start") {
        await apiClient.startScheduler();
      } else if (name === "stop") {
        await apiClient.stopScheduler();
      } else {
        await apiClient.runSchedulerOnce();
      }
      scheduler.retry();
      schedulerHistory.retry();
    } catch (error: unknown) {
      setActionError(error instanceof Error ? error.message : "Scheduler action failed.");
    } finally {
      setAction(null);
    }
  }

  return (
    <section className="settings-page" aria-labelledby="settings-health-title">
      <header className="page-intro">
        <p className="eyebrow">Local operations / explicit lifecycle</p>
        <h1 id="settings-health-title">Settings / health</h1>
        <p className="page-intro__description">
          Separate backend, market/news routing, model, scheduler and stored-count signals. Scheduler controls are explicit and local.
        </p>
        <p className="page-boundary">No secrets, broker connections, real orders or external notifiers are exposed here; alerts are local observability evidence only, while outcome settlement and performance refresh remain read-only.</p>
      </header>

      <div className="settings-grid">
        <AsyncPanel
          title="Backend service"
          source="GET /health"
          freshness="endpoint timestamp not supplied"
          state={healthDegraded ? "degraded" : resourceState(health, false)}
          error={health.error}
          onRetry={health.retry}
          degradedMessage="The backend answered with a non-ok status; provider and model probes remain separate."
        >
          {health.data ? <HealthFacts health={health.data} /> : null}
        </AsyncPanel>

        <AsyncPanel
          title="Market and news routing"
          source="GET /health/providers"
          freshness="probe response; timestamp not supplied"
          state={providerDegraded ? "degraded" : resourceState(provider, !providerHasContent)}
          error={provider.error}
          onRetry={provider.retry}
          emptyMessage="No routing rows or news probe details were returned."
          degradedMessage="Provider routing is degraded. This does not mean the backend itself is unavailable."
        >
          {provider.data ? <ProviderRouting provider={provider.data} /> : null}
        </AsyncPanel>

        <AsyncPanel
          title="Local model"
          source="GET /health/model"
          freshness="health probe response; timestamp not supplied"
          state={modelDegraded ? "degraded" : resourceState(model, !modelHasContent)}
          error={model.error}
          onRetry={model.retry}
          emptyMessage="The model endpoint returned no health fields."
          degradedMessage="Model health is degraded or unavailable while backend health is tracked separately."
        >
          {model.data ? <ModelFacts model={model.data} /> : null}
        </AsyncPanel>

        <AsyncPanel
          title="Local Watchlist scheduler"
          source="GET /scheduler/status"
          freshness="runtime status response"
          state={schedulerState(scheduler)}
          error={scheduler.error}
          onRetry={scheduler.retry}
          degradedMessage="The local scheduler is blocked or backing off. It never kills competing processes or retries without a bounded cap."
          className="settings-panel--scheduler"
        >
          {scheduler.data ? (
            <>
              <SchedulerFacts status={scheduler.data} history={schedulerHistory.data} />
              <div className="scheduler-actions" aria-label="Scheduler lifecycle controls">
                {!scheduler.data.enabled ? <button className="primary-button" type="button" onClick={() => void runSchedulerAction("enable")} disabled={action !== null}>{action === "enable" ? "Enabling…" : "Enable scheduler"}</button> : null}
                {scheduler.data.enabled && !scheduler.data.running ? <button className="primary-button" type="button" onClick={() => void runSchedulerAction("start")} disabled={action !== null}>{action === "start" ? "Starting…" : "Start scheduler"}</button> : null}
                {scheduler.data.running ? <button className="quiet-button" type="button" onClick={() => void runSchedulerAction("stop")} disabled={action !== null}>{action === "stop" ? "Stopping…" : "Stop scheduler"}</button> : null}
                {scheduler.data.enabled && !scheduler.data.running ? <button className="quiet-button" type="button" onClick={() => void runSchedulerAction("run")} disabled={action !== null}>{action === "run" ? "Scanning…" : "Run one Watchlist scan"}</button> : null}
                {scheduler.data.enabled ? <button className="quiet-button" type="button" onClick={() => void runSchedulerAction("disable")} disabled={action !== null}>{action === "disable" ? "Disabling…" : "Disable scheduler"}</button> : null}
              </div>
              {actionError ? <p className="panel-message panel-message--unavailable" role="alert">{actionError}</p> : null}
            </>
          ) : null}
        </AsyncPanel>

        <AsyncPanel
          title="Database counts"
          source="GET /stats"
          freshness="count response; timestamp not supplied"
          state={resourceState(stats, !statsHasContent)}
          error={stats.error}
          onRetry={stats.retry}
          emptyMessage="No stored-count keys were returned. Database condition is not inferred beyond this response."
        >
          {stats.data ? <CountLedger stats={stats.data} /> : null}
        </AsyncPanel>

        <AsyncPanel
          title="Alert reconciliation"
          source="GET /alerts/status"
          freshness="durable local policy and last reconciliation"
          state={alertState(alerts)}
          error={alerts.error}
          onRetry={alerts.retry}
          degradedMessage="Alert reconciliation reported an isolated evidence error; scheduler and stored financial records remain separate."
          className="settings-panel--alerts"
        >
          {alerts.data ? (
            <div className="scheduler-facts">
              <dl className="fact-list fact-list--compact">
                <div className="fact-list__row"><dt>Policy</dt><dd>{alerts.data.policy.version} · retention {alerts.data.policy.retention_limit ?? "not supplied"}</dd></div>
                <div className="fact-list__row"><dt>Counts</dt><dd>{schedulerText(alerts.data.counts)}</dd></div>
                <div className="fact-list__row"><dt>Last refresh</dt><dd>{alerts.data.last_reconciliation ? schedulerText(alerts.data.last_reconciliation) : "No reconciliation recorded"}</dd></div>
                <div className="fact-list__row"><dt>Capabilities</dt><dd>{schedulerText(alerts.data.capabilities)}</dd></div>
              </dl>
              <p className="panel-reading">Alert evidence is deduped in SQLite and acknowledgement is idempotent. No cloud notification, broker, order, PaperTrade, calibration or confidence mutation is connected.</p>
            </div>
          ) : null}
        </AsyncPanel>
      </div>
    </section>
  );
}
