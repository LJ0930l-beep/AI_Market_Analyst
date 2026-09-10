import { useCallback, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import type { ApplicationShellApiClient } from "../api/client";
import type { AlertFilters, AlertRecord, AlertSeverity, AlertSource, AlertStatus } from "../api/types";
import { AsyncPanel, type PanelState } from "../components/AsyncPanel";
import type { AsyncResource } from "../hooks/useAsyncResource";
import { useAsyncResource } from "../hooks/useAsyncResource";
import { useI18n, type TranslationKey } from "../i18n";

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

const sourceKeys: Record<AlertSource, TranslationKey> = {
  prediction: "common.prediction",
  outcome: "common.outcome",
  radar: "common.radar",
  news_event: "common.newsEvent",
  operational: "common.operational",
};

function evidenceText(alert: AlertRecord): string {
  return JSON.stringify(alert.evidence, null, 2) ?? "{}";
}

function AlertCard({ alert, onAcknowledge, busy }: { alert: AlertRecord; onAcknowledge: (alertId: string) => void; busy: boolean }) {
  const { formatDateTime, t, text } = useI18n();
  const acknowledged = alert.status === "ACKNOWLEDGED";
  const timestamp = (value: string | null | undefined) => {
    if (!value) return t("common.notSupplied");
    return Number.isNaN(new Date(value).getTime()) ? value : formatDateTime(value, { dateStyle: "medium", timeStyle: "medium", timeZone: "Asia/Hong_Kong" });
  };
  const source = sourceKeys[alert.source as AlertSource] ? t(sourceKeys[alert.source as AlertSource]) : alert.source;
  return (
    <li className={`alert-card alert-card--${String(alert.severity).toLowerCase()}`}>
      <div className="alert-card__header">
        <div>
          <p className="eyebrow">{source} · {alert.symbol ? <Link to={`/assets/${encodeURIComponent(alert.symbol)}`}>{alert.symbol}</Link> : t("alerts.workspace")}</p>
          <h3>{alert.title}</h3>
        </div>
        <div className="alert-card__badges" aria-label={`${t("common.alert")} ${text(alert.severity)} ${text(alert.status)}`}>
          <span className="status-chip status-chip--neutral">{text(alert.severity)}</span>
          <span className="status-chip status-chip--neutral">{text(alert.status)}</span>
        </div>
      </div>
      <p className="alert-card__message">{alert.message}</p>
      <dl className="fact-list fact-list--compact alert-card__facts">
        <div className="fact-list__row"><dt>{t("alerts.lastSeen")}</dt><dd>{timestamp(alert.last_seen_at)}</dd></div>
        <div className="fact-list__row"><dt>{t("alerts.occurrences")}</dt><dd>{alert.occurrence_count}</dd></div>
        <div className="fact-list__row"><dt>{t("alerts.eventIdentity")}</dt><dd>{alert.event_identity}</dd></div>
        <div className="fact-list__row"><dt>{t("alerts.policyLabel")}</dt><dd>{alert.policy_version}</dd></div>
      </dl>
      <details className="alert-card__evidence">
        <summary>{t("alerts.showEvidence")}</summary>
        <pre>{evidenceText(alert)}</pre>
      </details>
      <div className="alert-card__actions">
        {acknowledged ? <span className="panel-reading">{t("alerts.acknowledgedAt")} {timestamp(alert.acknowledged_at)}</span> : (
          <button className="quiet-button" type="button" onClick={() => onAcknowledge(alert.alert_id)} disabled={busy}>
            {busy ? t("alerts.acknowledging") : t("alerts.acknowledge")}
          </button>
        )}
      </div>
    </li>
  );
}

export function AlertsPage({ apiClient }: AlertsPageProps) {
  const { t, text } = useI18n();
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
      setActionError(error instanceof Error ? error.message : t("alerts.acknowledgeFailed"));
    } finally {
      setBusyAlertId(null);
    }
  }

  return (
    <section className="workflow-page alerts-page" aria-labelledby="alerts-title">
      <header className="page-intro">
        <p className="eyebrow">{t("alerts.eyebrow")}</p>
        <h1 id="alerts-title">{t("alerts.title")}</h1>
        <p className="page-intro__description">{t("alerts.description")}</p>
        <p className="page-boundary">{t("alerts.boundary")}</p>
      </header>

      <div className="capability-ledger capability-ledger--warning" role="note" aria-label={t("alerts.boundaryLabel")}>
        <span className="capability-ledger__label">{t("alerts.policy")}</span>
        <p>{t("alerts.capability")}</p>
      </div>

      <section className="alert-summary" aria-label={t("alerts.counts")}>
        <div><span className="alert-summary__label">{t("alerts.open")}</span><strong>{counts?.open ?? "—"}</strong></div>
        <div><span className="alert-summary__label">{t("alerts.unread")}</span><strong>{counts?.unread ?? "—"}</strong></div>
        <div><span className="alert-summary__label">{t("alerts.acknowledged")}</span><strong>{counts?.acknowledged ?? "—"}</strong></div>
        <div><span className="alert-summary__label">{t("alerts.retention")}</span><strong>{alertStatus.data?.policy.retention_limit ?? 500}</strong></div>
      </section>

      <form className="workflow-filters alerts-filters" aria-label={t("alerts.filters")} onSubmit={applyFilters}>
        <div className="workflow-filters__grid">
          <label>
            {t("common.source")}
            <select value={draftSource} onChange={(event) => setDraftSource(event.target.value as AlertSource | "")}>
              <option value="">{t("common.allSources")}</option>
              {alertSources.map((source) => <option key={source} value={source}>{t(sourceKeys[source])}</option>)}
            </select>
          </label>
          <label>
            {t("common.status")}
            <select value={draftStatus} onChange={(event) => setDraftStatus(event.target.value as AlertStatus | "")}>
              <option value="">{t("common.allStatuses")}</option>
              {alertStatuses.map((status) => <option key={status} value={status}>{text(status)}</option>)}
            </select>
          </label>
          <label>
            {t("common.severity")}
            <select aria-label={t("alerts.severityFilter")} value={draftSeverity} onChange={(event) => setDraftSeverity(event.target.value as AlertSeverity | "")}>
              <option value="">{t("common.allSeverities")}</option>
              {alertSeverities.map((severity) => <option key={severity} value={severity}>{text(severity)}</option>)}
            </select>
          </label>
          <button className="primary-button" type="submit">{t("common.applyFilters")}</button>
        </div>
      </form>

      {actionError ? <p className="panel-message panel-message--unavailable" role="alert">{actionError}</p> : null}

      <AsyncPanel
        title={t("alerts.ledger")}
        source="GET /alerts"
        freshness={t("common.lastSeenEvidence")}
        state={resourceState(alerts, records.length === 0)}
        error={alerts.error}
        onRetry={alerts.retry}
        emptyMessage={t("alerts.noMatch")}
      >
        <ul className="alert-list" aria-label={t("alerts.stored")}>
          {records.map((alert) => <AlertCard key={alert.alert_id} alert={alert} busy={busyAlertId === alert.alert_id} onAcknowledge={(id) => void acknowledge(id)} />)}
        </ul>
      </AsyncPanel>
    </section>
  );
}
