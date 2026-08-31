import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { type ApplicationShellApiClient } from "../api/client";
import type { MonitoringPolicy, MonitoringResponse, MonitoringRuntimeStatus, OpportunityAnalysis, OpportunityAnalysesResponse, RealtimeState, TriggerEvent, TriggerType } from "../api/types";
import { AsyncPanel, type PanelState } from "../components/AsyncPanel";
import { useAsyncResource } from "../hooks/useAsyncResource";
import { useI18n } from "../i18n";

const SUPPORTED_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"];
const TRIGGER_TYPES: TriggerType[] = [
  "REGIME_CHANGE",
  "BREAKOUT",
  "BREAKDOWN",
  "VOLUME_EXPANSION",
  "VOLATILITY_EXPANSION",
  "LEVEL_PROXIMITY",
  "SIGNAL_INVALIDATION",
  "EVENT_RISK",
  "NEWS_SHOCK",
];

function canonicalTrigger(type: string): TriggerType {
  const aliases: Record<string, TriggerType> = {
    regime: "REGIME_CHANGE",
    regime_change: "REGIME_CHANGE",
    breakout: "BREAKOUT",
    breakdown: "BREAKDOWN",
    volume: "VOLUME_EXPANSION",
    volume_expansion: "VOLUME_EXPANSION",
    volatility: "VOLATILITY_EXPANSION",
    volatility_expansion: "VOLATILITY_EXPANSION",
    level_proximity: "LEVEL_PROXIMITY",
    invalidation: "SIGNAL_INVALIDATION",
    signal_invalidation: "SIGNAL_INVALIDATION",
    event_risk: "EVENT_RISK",
    news_shock: "NEWS_SHOCK",
  };
  return aliases[type.toLowerCase()] ?? type.toUpperCase() as TriggerType;
}

interface MonitoringPageProps {
  apiClient: ApplicationShellApiClient;
}

function panelState(resource: { status: string; data?: MonitoringResponse }): PanelState {
  if (resource.status === "loading") return "loading";
  if (resource.status === "unavailable") return "unavailable";
  if (!resource.data) return "empty";
  return "ready";
}

function opportunityPanelState(resource: { status: string; data?: OpportunityAnalysesResponse }): PanelState {
  if (resource.status === "loading") return "loading";
  if (resource.status === "unavailable") return "unavailable";
  if (!resource.data || resource.data.analyses.length === 0) return "empty";
  return "ready";
}

function policyFor(data: MonitoringResponse | undefined, symbol: string): MonitoringPolicy {
  const existing = data?.policies.find((item) => item.instrument_id === symbol);
  if (existing) return { ...existing, trigger_types: existing.trigger_types.map((item) => canonicalTrigger(item)) };
  return {
    contract_version: "monitoring_policy_v1",
    instrument_id: symbol,
    enabled: false,
    primary_timeframe: "15m",
    context_timeframe: "1h",
    trigger_types: [...TRIGGER_TYPES],
    min_trigger_score: 0.65,
    ai_min_confidence: 0.6,
    cooldown_minutes: 60,
    quiet_hours: {},
    notify: { desktop: true, sound: false },
    notify_in_app: true,
    notify_native_notification: true,
    created_at: null,
    updated_at: null,
  };
}

function triggerLabel(type: TriggerType | string, t: ReturnType<typeof useI18n>["t"]): string {
  const labels: Record<TriggerType, Parameters<typeof t>[0]> = {
    REGIME_CHANGE: "monitoring.triggerRegime",
    BREAKOUT: "monitoring.triggerBreakout",
    BREAKDOWN: "monitoring.triggerBreakdown",
    VOLUME_EXPANSION: "monitoring.triggerVolume",
    VOLATILITY_EXPANSION: "monitoring.triggerVolatility",
    LEVEL_PROXIMITY: "monitoring.triggerLevelProximity",
    SIGNAL_INVALIDATION: "monitoring.triggerInvalidation",
    EVENT_RISK: "monitoring.triggerEventRisk",
    NEWS_SHOCK: "monitoring.triggerNewsShock",
    regime: "monitoring.triggerRegime",
    breakout: "monitoring.triggerBreakout",
    breakdown: "monitoring.triggerBreakdown",
    volume: "monitoring.triggerVolume",
    volatility: "monitoring.triggerVolatility",
    level_proximity: "monitoring.triggerLevelProximity",
    invalidation: "monitoring.triggerInvalidation",
    event_risk: "monitoring.triggerEventRisk",
    news_shock: "monitoring.triggerNewsShock",
  };
  return type in labels ? t(labels[type as TriggerType]) : type;
}

function freshnessLabel(state: RealtimeState | undefined, t: ReturnType<typeof useI18n>["t"]): string {
  if (!state) return t("monitoring.noRealtimeYet");
  if (state.freshness_status === "fresh") return t("monitoring.fresh");
  if (state.freshness_status === "stale") return t("monitoring.stale");
  if (state.freshness_status === "degraded") return t("monitoring.degraded");
  return t("monitoring.unavailable");
}

function eventTime(value: string | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toISOString();
}

function opportunityBiasLabel(value: OpportunityAnalysis["bias"], t: ReturnType<typeof useI18n>["t"]): string {
  if (value === "LONG_WATCH") return t("monitoring.longWatch");
  if (value === "SHORT_WATCH") return t("monitoring.shortWatch");
  return t("monitoring.wait");
}

function runtimeStateLabel(value: string | undefined, t: ReturnType<typeof useI18n>["t"]): string {
  const labels: Record<string, Parameters<typeof t>[0]> = {
    stopped: "monitoring.runtimeStopped",
    starting: "monitoring.runtimeStarting",
    running: "monitoring.runtimeRunning",
    paused: "monitoring.runtimePaused",
    degraded: "monitoring.runtimeDegraded",
    backoff: "monitoring.runtimeBackoff",
  };
  return value && labels[value] ? t(labels[value]) : value || t("common.unknown");
}

export function MonitoringPage({ apiClient }: MonitoringPageProps) {
  const { formatNumber, t } = useI18n();
  const monitoringLoader = useCallback((signal: AbortSignal) => apiClient.monitoring(signal), [apiClient]);
  const triggerLoader = useCallback((signal: AbortSignal) => apiClient.triggerEvents(undefined, 60, signal), [apiClient]);
  const opportunityLoader = useCallback((signal: AbortSignal) => apiClient.monitoringOpportunities(undefined, 20, signal), [apiClient]);
  const runtimeLoader = useCallback((signal: AbortSignal) => apiClient.monitoringStatus(signal), [apiClient]);
  const monitoring = useAsyncResource(monitoringLoader);
  const triggers = useAsyncResource(triggerLoader);
  const opportunities = useAsyncResource(opportunityLoader);
  const runtime = useAsyncResource(runtimeLoader);
  const [drafts, setDrafts] = useState<Record<string, MonitoringPolicy>>({});
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const runtimeStatus: MonitoringRuntimeStatus | undefined = runtime.data ?? monitoring.data?.runtime;

  useEffect(() => {
    if (!monitoring.data || dirty) return;
    setDrafts(Object.fromEntries(SUPPORTED_SYMBOLS.map((symbol) => [symbol, policyFor(monitoring.data, symbol)])));
  }, [dirty, monitoring.data]);

  const realtime = useMemo(
    () => new Map((monitoring.data?.realtime ?? []).map((state) => [state.symbol, state])),
    [monitoring.data?.realtime],
  );

  function changePolicy(symbol: string, change: Partial<MonitoringPolicy>) {
    setDirty(true);
    setDrafts((current) => ({ ...current, [symbol]: { ...policyFor(monitoring.data, symbol), ...current[symbol], ...change } }));
  }

  function toggleTrigger(symbol: string, trigger: TriggerType) {
    const policy = drafts[symbol] ?? policyFor(monitoring.data, symbol);
    const selected = policy.trigger_types.some((item) => canonicalTrigger(item) === trigger)
      ? policy.trigger_types.filter((item) => canonicalTrigger(item) !== trigger).map((item) => canonicalTrigger(item))
      : [...policy.trigger_types, trigger];
    if (selected.length === 0) return;
    changePolicy(symbol, { trigger_types: selected });
  }

  async function savePolicy(symbol: string) {
    const policy = drafts[symbol] ?? policyFor(monitoring.data, symbol);
    setBusy(`save:${symbol}`);
    setMessage(null);
    try {
      await apiClient.updateMonitoringPolicy(policy);
      setMessage(`${symbol} · ${t("monitoring.policySaved")}`);
      setDirty(false);
      monitoring.retry();
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : t("monitoring.actionFailed"));
    } finally {
      setBusy(null);
    }
  }

  async function runNow() {
    setBusy("run");
    setMessage(null);
    try {
      const result = await apiClient.runMonitoring(SUPPORTED_SYMBOLS);
      const status = typeof result.status === "string" ? result.status : t("common.complete");
      setMessage(`${t("monitoring.runComplete")}: ${status}`);
      monitoring.retry();
      triggers.retry();
      opportunities.retry();
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : t("monitoring.actionFailed"));
    } finally {
      setBusy(null);
    }
  }

  async function lifecycleAction(action: "start" | "resume" | "pause" | "stop") {
    setBusy(`runtime:${action}`);
    setMessage(null);
    try {
      const next = action === "start"
        ? await apiClient.startMonitoring()
        : action === "resume"
          ? await apiClient.resumeMonitoring()
          : action === "pause"
            ? await apiClient.pauseMonitoring()
            : await apiClient.stopMonitoring();
      setMessage(`${t("monitoring.runtimeUpdated")}: ${runtimeStateLabel(next.state, t)}`);
      runtime.retry();
      monitoring.retry();
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : t("monitoring.actionFailed"));
    } finally {
      setBusy(null);
    }
  }

  const events: TriggerEvent[] = triggers.data?.events ?? [];
  return (
    <section className="monitoring-page" aria-labelledby="monitoring-title">
      <header className="page-intro monitoring-page__intro">
        <p className="eyebrow">{t("monitoring.eyebrow")}</p>
        <h1 id="monitoring-title">{t("monitoring.title")}</h1>
        <p className="page-intro__description">{t("monitoring.description")}</p>
        <p className="page-boundary">{t("monitoring.boundary")}</p>
      </header>

      <section className="capability-ledger capability-ledger--warning" aria-label={t("monitoring.explicitOptIn")}>
        <span className="capability-ledger__label">{t("monitoring.explicitOptIn")}</span>
        <p>{t("monitoring.noStartupSideEffect")}</p>
      </section>

      <div className="monitoring-actions">
        <button className="primary-button" type="button" onClick={() => void runNow()} disabled={busy !== null}>
          {busy === "run" ? t("monitoring.running") : t("monitoring.runNow")}
        </button>
        <span className="data-meta">{t("monitoring.runHint")}</span>
      </div>
      {message ? <p className="monitoring-action-status" role="status">{message}</p> : null}

      <section className="monitoring-runtime-panel" aria-labelledby="monitoring-runtime-title">
        <div className="monitoring-runtime-panel__header">
          <div><p className="eyebrow">{t("monitoring.runtimeEyebrow")}</p><h2 id="monitoring-runtime-title">{t("monitoring.runtimeTitle")}</h2></div>
          <span className={`status-chip status-chip--${runtimeStatus?.state === "running" ? "positive" : runtimeStatus?.active ? "warning" : "neutral"}`}>
            {runtimeStateLabel(runtimeStatus?.state, t)}
          </span>
        </div>
        <p className="panel-reading">{t("monitoring.runtimeDescription")}</p>
        {runtimeStatus ? (
          <dl className="monitoring-runtime-facts">
            <div><dt>{t("monitoring.runtimeWorker")}</dt><dd>{runtimeStatus.worker_alive ? t("common.yes") : t("common.no")}</dd></div>
            <div><dt>{t("monitoring.runtimeSymbols")}</dt><dd>{runtimeStatus.active_symbols.length} / {runtimeStatus.max_symbols}</dd></div>
            <div><dt>{t("monitoring.runtimeStream")}</dt><dd>{String(runtimeStatus.stream.status ?? t("common.notSupplied"))}</dd></div>
            <div><dt>{t("monitoring.runtimeCycles")}</dt><dd>{runtimeStatus.run_count}</dd></div>
            <div><dt>{t("monitoring.runtimeLastCycle")}</dt><dd>{eventTime(runtimeStatus.last_cycle_at ?? undefined)}</dd></div>
            {runtimeStatus.last_error ? <div><dt>{t("common.errorCode")}</dt><dd>{runtimeStatus.last_error}</dd></div> : null}
          </dl>
        ) : <p className="panel-reading">{t("monitoring.runtimePending")}</p>}
        <div className="monitoring-actions" aria-label={t("monitoring.runtimeControls")}>
          {!runtimeStatus?.active ? <button className="primary-button" type="button" onClick={() => void lifecycleAction(runtimeStatus?.state === "paused" ? "resume" : "start")} disabled={busy !== null}>{busy?.startsWith("runtime:") ? t("monitoring.runtimeWorking") : runtimeStatus?.state === "paused" ? t("monitoring.resume") : t("monitoring.start")}</button> : null}
          {runtimeStatus?.active ? <button className="quiet-button" type="button" onClick={() => void lifecycleAction("pause")} disabled={busy !== null}>{busy === "runtime:pause" ? t("monitoring.runtimeWorking") : t("monitoring.pause")}</button> : null}
          {runtimeStatus?.active || runtimeStatus?.state === "paused" ? <button className="quiet-button" type="button" onClick={() => void lifecycleAction("stop")} disabled={busy !== null}>{busy === "runtime:stop" ? t("monitoring.runtimeWorking") : t("monitoring.stop")}</button> : null}
        </div>
      </section>

      <AsyncPanel
        title={t("monitoring.policyPanel")}
        source="GET /monitoring · PUT /monitoring"
        freshness={monitoring.data ? t("monitoring.freshnessStored") : t("monitoring.freshnessPending")}
        state={panelState(monitoring)}
        error={monitoring.error}
        onRetry={monitoring.retry}
        emptyMessage={t("monitoring.noPolicies")}
        className="monitoring-panel"
      >
        <div className="monitoring-policy-grid">
          {SUPPORTED_SYMBOLS.map((symbol) => {
            const policy = drafts[symbol] ?? policyFor(monitoring.data, symbol);
            const state = realtime.get(symbol);
            return (
              <article className={`monitoring-policy-card${policy.enabled ? " monitoring-policy-card--enabled" : ""}`} key={symbol}>
                <header className="monitoring-policy-card__header">
                  <div><p className="eyebrow">{t("monitoring.cryptoPublic")}</p><h2>{symbol}</h2></div>
                  <label className="monitoring-toggle">
                    <input type="checkbox" checked={policy.enabled} onChange={(event) => changePolicy(symbol, { enabled: event.target.checked })} />
                    <span>{policy.enabled ? t("monitoring.enabled") : t("monitoring.disabled")}</span>
                  </label>
                </header>
                <div className="monitoring-market-state">
                  <span className={`status-chip status-chip--${state?.freshness_status === "fresh" ? "positive" : "neutral"}`}>{freshnessLabel(state, t)}</span>
                  <span className="data-meta">{state?.price !== undefined && state.price !== null ? formatNumber(state.price, { maximumFractionDigits: 6 }) : t("common.notSupplied")}</span>
                  {state?.last_trade_at ? <span className="data-meta">{eventTime(state.last_trade_at)}</span> : null}
                </div>
                <div className="monitoring-policy-fields">
                  <label>{t("monitoring.primaryTimeframe")}<input value={policy.primary_timeframe} readOnly /></label>
                  <label>{t("monitoring.contextTimeframe")}<input value={policy.context_timeframe} readOnly /></label>
                  <label>{t("monitoring.minTriggerScore")}<input type="number" min="0" max="1" step="0.01" value={policy.min_trigger_score} onChange={(event) => changePolicy(symbol, { min_trigger_score: Number(event.target.value) })} /></label>
                  <label>{t("monitoring.aiMinConfidence")}<input type="number" min="0" max="1" step="0.01" value={policy.ai_min_confidence} onChange={(event) => changePolicy(symbol, { ai_min_confidence: Number(event.target.value) })} /></label>
                  <label>{t("monitoring.cooldownMinutes")}<input type="number" min="1" max="1440" step="1" value={policy.cooldown_minutes} onChange={(event) => changePolicy(symbol, { cooldown_minutes: Number(event.target.value) })} /></label>
                </div>
                <fieldset className="monitoring-trigger-list">
                  <legend>{t("monitoring.triggerTypes")}</legend>
                  {TRIGGER_TYPES.map((trigger) => (
                    <label key={trigger}><input type="checkbox" checked={policy.trigger_types.includes(trigger)} onChange={() => toggleTrigger(symbol, trigger)} />{triggerLabel(trigger, t)}</label>
                  ))}
                </fieldset>
                <fieldset className="monitoring-trigger-list monitoring-notification-list">
                  <legend>{t("monitoring.notificationPreferences")}</legend>
                  <label><input type="checkbox" checked={policy.notify_in_app !== false} onChange={(event) => changePolicy(symbol, { notify_in_app: event.target.checked })} />{t("monitoring.notifyInApp")}</label>
                  <label><input type="checkbox" checked={policy.notify_native_notification !== false} onChange={(event) => changePolicy(symbol, { notify_native_notification: event.target.checked })} />{t("monitoring.notifyNative")}</label>
                </fieldset>
                <div className="monitoring-policy-card__footer">
                  <Link className="quiet-button" to={`/assets/${encodeURIComponent(symbol)}`}>{t("monitoring.openChart")}</Link>
                  <button className="primary-button" type="button" onClick={() => void savePolicy(symbol)} disabled={busy !== null}>
                    {busy === `save:${symbol}` ? t("monitoring.saving") : t("monitoring.savePolicy")}
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      </AsyncPanel>

      <AsyncPanel
        title={t("monitoring.opportunityPanel")}
        source="GET /monitoring/opportunities"
        freshness={opportunities.data ? t("monitoring.freshnessStored") : t("monitoring.freshnessPending")}
        state={opportunityPanelState(opportunities)}
        error={opportunities.error}
        onRetry={opportunities.retry}
        emptyMessage={t("monitoring.noOpportunities")}
        className="monitoring-panel"
      >
        <div className="monitoring-opportunity-grid">
          {(opportunities.data?.analyses ?? []).map((analysis) => (
            <article className="monitoring-opportunity-card" key={analysis.analysis_id ?? `${analysis.symbol}-${analysis.trigger_event_id}`}>
              <header className="monitoring-opportunity-card__header">
                <div>
                  <p className="eyebrow">{analysis.symbol} · {analysis.timeframe}</p>
                  <h2>{opportunityBiasLabel(analysis.bias, t)}</h2>
                </div>
                <span className={`signal-action signal-action--${analysis.bias.toLowerCase()}`}>{formatNumber(analysis.confidence, { style: "percent", maximumFractionDigits: 0 })}</span>
              </header>
              <div className="monitoring-opportunity-card__facts">
                <span><strong>{t("monitoring.opportunityModel")}</strong> {analysis.model_id}</span>
                <span><strong>{t("monitoring.opportunityDataAsOf")}</strong> {eventTime(analysis.data_as_of)}</span>
                <span><strong>{t("monitoring.opportunityValidated")}</strong> {t("monitoring.pythonValidated")}</span>
              </div>
              <div className="monitoring-opportunity-card__levels">
                <strong>{t("monitoring.opportunityLevels")}</strong>
                <span>{analysis.watch_zone ? `${formatNumber(analysis.watch_zone.low, { maximumFractionDigits: 6 })} – ${formatNumber(analysis.watch_zone.high, { maximumFractionDigits: 6 })}` : t("common.notSupplied")}</span>
                <span>{t("common.stop")}: {analysis.invalidation_price !== null && analysis.invalidation_price !== undefined ? formatNumber(analysis.invalidation_price, { maximumFractionDigits: 6 }) : t("common.notSupplied")}</span>
                <span>{t("common.statusTp1")}: {analysis.targets[0] !== undefined ? formatNumber(analysis.targets[0], { maximumFractionDigits: 6 }) : t("common.notSupplied")}</span>
              </div>
              <Link className="quiet-button" to={`/assets/${encodeURIComponent(analysis.symbol)}`}>{t("monitoring.openChart")}</Link>
            </article>
          ))}
        </div>
      </AsyncPanel>

      <AsyncPanel
        title={t("monitoring.triggerLedger")}
        source="GET /triggers"
        freshness={triggers.data ? t("monitoring.freshnessStored") : t("monitoring.freshnessPending")}
        state={triggers.status === "loading" ? "loading" : triggers.status === "unavailable" ? "unavailable" : events.length === 0 ? "empty" : "ready"}
        error={triggers.error}
        onRetry={triggers.retry}
        emptyMessage={t("monitoring.noTriggerEvents")}
        className="monitoring-panel"
      >
        <div className="table-wrap" tabIndex={0} aria-label={t("monitoring.triggerLedger")}>
          <table className="monitoring-event-table">
            <caption className="sr-only">{t("monitoring.triggerLedger")}</caption>
            <thead><tr><th>{t("common.symbol")}</th><th>{t("monitoring.trigger")}</th><th>{t("common.score")}</th><th>{t("monitoring.barClose")}</th><th>{t("common.status")}</th></tr></thead>
            <tbody>{events.map((event) => <tr key={event.trigger_event_id}><td>{event.instrument_id}</td><td>{triggerLabel(event.trigger_type as TriggerType, t)}</td><td>{formatNumber(event.trigger_score, { maximumFractionDigits: 3 })}</td><td>{eventTime(event.bar_end)}</td><td>{event.status} · {event.analysis_status}</td></tr>)}</tbody>
          </table>
        </div>
      </AsyncPanel>
    </section>
  );
}
