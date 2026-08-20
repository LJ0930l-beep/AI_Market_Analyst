import { useCallback, useState, type FormEvent } from "react";

import type { ApplicationShellApiClient } from "../api/client";
import type { AlertFilters, AlertRecord, AlertSeverity, AlertSource, AlertStatus } from "../api/types";
import { AsyncPanel, type PanelState } from "../components/AsyncPanel";
import type { AsyncResource } from "../hooks/useAsyncResource";
import { useAsyncResource } from "../hooks/useAsyncResource";

interface AlertsPageProps {
  apiClient: ApplicationShellApiClient;
}

const alertSources: AlertSource[] = ["prediction", "outcome", "radar", "news_event", "operational"];
const alertSeverities: AlertSeverity[] = ["INFO", "WARNING", "CRITICAL"];
const alertStatuses: AlertStatus[] = ["OPEN", "ACKNOWLEDGED"];

function resourceState<T>(resource: AsyncResource<T>, empty: boolean): PanelState {
  if (resource.status === "idle" || resource.status === "loading") return "loading";
  if (resource.status === "unavailable") return "unavailable";
  return empty ? "empty" : "ready";
}

function timestamp(value: string | null | undefined): string {
  if (!value) return "Not supplied";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function sourceLabel(source: string): string {
  return source.replaceAll("_", " ");
}

function evidenceText(alert: AlertRecord): string {
  return JSON.stringify(alert.evidence, null, 2) ?? "{}";
}

function AlertCard({ alert, onAcknowledge, busy }: { alert: AlertRecord; onAcknowledge: (alertId: string) => void; busy: boolean }) {
  const acknowledged = alert.status === "ACKNOWLEDGED";
  return (
    <li className={`alert-card alert-card--${String(alert.severity).toLowerCase()}`}>
      <div className="alert-card__header">
        <div>
          <p className="eyebrow">{sourceLabel(String(alert.source))} · {alert.symbol ?? "workspace"}</p>
          <h3>{alert.title}</h3>
        </div>
        <div className="alert-card__badges" aria-label={`Alert ${alert.severity} ${alert.status}`}>
          <span className="status-chip status-chip--neutral">{alert.severity}</span>
          <span className="status-chip status-chip--neutral">{alert.status}</span>
        </div>
      </div>
      <p className="alert-card__message">{alert.message}</p>
      <dl className="fact-list fact-list--compact alert-card__facts">
        <div className="fact-list__row"><dt>Last seen</dt><dd>{timestamp(alert.last_seen_at)}</dd></div>
        <div className="fact-list__row"><dt>Occurrences</dt><dd>{alert.occurrence_count}</dd></div>
        <div className="fact-list__row"><dt>Event identity</dt><dd>{alert.event_identity}</dd></div>
        <div className="fact-list__row"><dt>Policy</dt><dd>{alert.policy_version}</dd></div>
      </dl>
      <details className="alert-card__evidence">
        <summary>Show evidence</summary>
        <pre>{evidenceText(alert)}</pre>
      </details>
      <div className="alert-card__actions">
        {acknowledged ? <span className="panel-reading">Acknowledged {timestamp(alert.acknowledged_at)}</span> : (
          <button className="quiet-button" type="button" onClick={() => onAcknowledge(alert.alert_id)} disabled={busy}>
            {busy ? "Acknowledging…" : "Acknowledge alert"}
          </button>
        )}
      </div>
    </li>
  );
}

export function AlertsPage({ apiClient }: AlertsPageProps) {
  const [draftSource, setDraftSource] = useState<AlertSource | "">("");
  const [draftStatus, setDraftStatus] = useState<AlertStatus | "">("");
  const [draftSeverity, setDraftSeverity] = useState<AlertSeverity | "">("");
  const [filters, setFilters] = useState<AlertFilters>({ limit: 50, offset: 0 });
  const [busyAlertId, setBusyAlertId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const listLoader = useCallback((signal: AbortSignal) => apiClient.alerts(filters, signal), [apiClient, filters]);
  const statusLoader = useCallback((signal: AbortSignal) => apiClient.alertStatus(signal), [apiClient]);
  const alerts = useAsyncResource(listLoader);
  const alertStatus = useAsyncResource(statusLoader);
  const records = alerts.data?.alerts ?? [];
  const counts = alerts.data?.counts ?? alertStatus.data?.counts;

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFilters({
      limit: 50,
      offset: 0,
      ...(draftSource ? { source: draftSource } : {}),
      ...(draftStatus ? { status: draftStatus } : {}),
      ...(draftSeverity ? { severity: draftSeverity } : {}),
    });
  }

  async function acknowledge(alertId: string) {
    setBusyAlertId(alertId);
    setActionError(null);
    try {
      await apiClient.acknowledgeAlert(alertId);
      alerts.retry();
      alertStatus.retry();
    } catch (error: unknown) {
      setActionError(error instanceof Error ? error.message : "Alert acknowledgement failed.");
    } finally {
      setBusyAlertId(null);
    }
  }

  return (
    <section className="workflow-page alerts-page" aria-labelledby="alerts-title">
      <header className="page-intro">
        <p className="eyebrow">Local observability / durable ledger</p>
        <h1 id="alerts-title">Alert Center</h1>
        <p className="page-intro__description">Review deterministic local evidence from predictions, outcomes, Radar transitions and operational failures.</p>
        <p className="page-boundary">Alerts stay in SQLite. No Telegram, Discord, email, cloud notifier, broker, real order or PaperTrade action is connected.</p>
      </header>

      <div className="capability-ledger capability-ledger--warning" role="note" aria-label="Alert boundary">
        <span className="capability-ledger__label">alert_policy_v1</span>
        <p>Repeated evidence is deduplicated across restarts. Operational failures coalesce in bounded cooldown windows; acknowledgement changes only alert status.</p>
      </div>

      <section className="alert-summary" aria-label="Alert counts">
        <div><span className="alert-summary__label">Open</span><strong>{counts?.open ?? "—"}</strong></div>
        <div><span className="alert-summary__label">Unread</span><strong>{counts?.unread ?? "—"}</strong></div>
        <div><span className="alert-summary__label">Acknowledged</span><strong>{counts?.acknowledged ?? "—"}</strong></div>
        <div><span className="alert-summary__label">Retention</span><strong>{alertStatus.data?.policy.retention_limit ?? 500}</strong></div>
      </section>

      <form className="workflow-filters alerts-filters" aria-label="Alert filters" onSubmit={applyFilters}>
        <div className="workflow-filters__grid">
          <label>
            Source
            <select value={draftSource} onChange={(event) => setDraftSource(event.target.value as AlertSource | "")}>
              <option value="">All sources</option>
              {alertSources.map((source) => <option key={source} value={source}>{sourceLabel(source)}</option>)}
            </select>
          </label>
          <label>
            Status
            <select value={draftStatus} onChange={(event) => setDraftStatus(event.target.value as AlertStatus | "")}>
              <option value="">All statuses</option>
              {alertStatuses.map((status) => <option key={status} value={status}>{status}</option>)}
            </select>
          </label>
          <label>
            Severity
            <select aria-label="Severity filter" value={draftSeverity} onChange={(event) => setDraftSeverity(event.target.value as AlertSeverity | "")}>
              <option value="">All severities</option>
              {alertSeverities.map((severity) => <option key={severity} value={severity}>{severity}</option>)}
            </select>
          </label>
          <button className="primary-button" type="submit">Apply filters</button>
        </div>
      </form>

      {actionError ? <p className="panel-message panel-message--unavailable" role="alert">{actionError}</p> : null}

      <AsyncPanel
        title="Alert ledger"
        source="GET /alerts"
        freshness="last_seen_at from durable SQLite evidence"
        state={resourceState(alerts, records.length === 0)}
        error={alerts.error}
        onRetry={alerts.retry}
        emptyMessage="No alerts match the current filters. A scheduler reconciliation creates rows only from stored deterministic evidence."
      >
        <ul className="alert-list" aria-label="Stored alerts">
          {records.map((alert) => <AlertCard key={alert.alert_id} alert={alert} busy={busyAlertId === alert.alert_id} onAcknowledge={(id) => void acknowledge(id)} />)}
        </ul>
      </AsyncPanel>
    </section>
  );
}
