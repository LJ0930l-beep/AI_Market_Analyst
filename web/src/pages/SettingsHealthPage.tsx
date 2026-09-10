import { useCallback, useEffect, useRef, useState } from "react";

import type { ApplicationShellApiClient } from "../api/client";
import { AsyncPanel, type PanelState } from "../components/AsyncPanel";
import { CountLedger, HealthFacts, ModelFacts, ProviderRouting } from "../components/OperationalFacts";
import type { AlertStatusResponse, ContextHealthResponse, ReleaseHealthResponse, SchedulerHistory, SchedulerStatus } from "../api/types";
import type { AsyncResource } from "../hooks/useAsyncResource";
import { useAsyncResource } from "../hooks/useAsyncResource";
import { useI18n } from "../i18n";
import { notifyDesktopAlert } from "../desktopNotifications";
import { isTauriRuntime, readAutostartState, registerDesktopBackendState, restartOwnedBackend, setAutostartState, type DesktopBackendStatus } from "../desktopRuntime";

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

function contextState(resource: AsyncResource<ContextHealthResponse>): PanelState {
  if (resource.status === "loading") return "loading";
  if (resource.status === "unavailable") return "unavailable";
  return resource.data ? "ready" : "empty";
}

function releaseState(resource: AsyncResource<ReleaseHealthResponse>): PanelState {
  if (resource.status === "loading") return "loading";
  if (resource.status === "unavailable") return "unavailable";
  if (!resource.data) return "empty";
  return resource.data.status === "ok" ? "ready" : "degraded";
}

function schedulerText(value: unknown, fallback: string): string {
  if (value === null || value === undefined || value === "") {
    return fallback;
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value) ?? fallback;
}

function SchedulerFacts({ status, history }: { status: SchedulerStatus; history?: SchedulerHistory }) {
  const { formatDateTime, t, text } = useI18n();
  const lastRun = status.last_run;
  const counts = lastRun?.counts;
  const timestamp = (value: string | null | undefined) => {
    if (!value) return t("common.notSupplied");
    return Number.isNaN(new Date(value).getTime()) ? value : formatDateTime(value, { dateStyle: "medium", timeStyle: "medium", timeZone: "Asia/Hong_Kong" });
  };
  const value = (input: unknown) => schedulerText(input, t("common.notDisplayable"));
  return (
    <div className="scheduler-facts">
      <dl className="fact-list fact-list--compact">
        <div className="fact-list__row"><dt>{t("settings.lifecycle")}</dt><dd>{text(status.state)} · {t("settings.enabled")} {t(status.enabled ? "common.yes" : "common.no")} · {t("settings.running")} {t(status.running ? "common.yes" : "common.no")}</dd></div>
        <div className="fact-list__row"><dt>{t("settings.intervalSession")}</dt><dd>{status.interval_seconds}s · {text(status.session_policy)}</dd></div>
        <div className="fact-list__row"><dt>{t("settings.concurrency")}</dt><dd>{t("settings.configuredValue")} {status.configured_concurrency} · {t("settings.effectiveModelLimit")} {status.effective_concurrency}</dd></div>
        <div className="fact-list__row"><dt>{t("settings.lastRun")}</dt><dd>{lastRun ? `${text(lastRun.status)} · ${timestamp(lastRun.started_at)}` : t("settings.noScanRun")}</dd></div>
        <div className="fact-list__row"><dt>{t("settings.nextRun")}</dt><dd>{status.next_run_at ? timestamp(status.next_run_at) : t("common.notSupplied")}</dd></div>
        <div className="fact-list__row"><dt>{t("settings.lastCounts")}</dt><dd>{counts ? value(counts) : t("settings.noItemCounts")}</dd></div>
        <div className="fact-list__row"><dt>{t("settings.resource")}</dt><dd>{value(status.resource)}</dd></div>
        <div className="fact-list__row"><dt>{t("settings.backoff")}</dt><dd>{value(status.backoff)}</dd></div>
        <div className="fact-list__row"><dt>{t("settings.cache")}</dt><dd>{value(status.cache)}</dd></div>
        <div className="fact-list__row"><dt>{t("settings.settlement")}</dt><dd>{status.settlement ? value(status.settlement) : t("settings.noSettlement")}</dd></div>
        <div className="fact-list__row"><dt>{t("settings.performanceRefresh")}</dt><dd>{status.performance_refresh ? value(status.performance_refresh) : t("settings.noRefresh")}</dd></div>
      </dl>
      <p className="panel-reading">{t("settings.schedulerFacts")}</p>
      <p className="panel-reading">{t("settings.settlementFacts")}</p>
      {history && history.runs.length > 0 ? (
        <ul className="scheduler-history" aria-label={t("settings.recentRuns")}>
          {history.runs.slice(0, 3).map((run) => (
            <li key={run.run_id}><strong>{text(run.status)}</strong> · {text(run.trigger)} · {timestamp(run.started_at)}</li>
          ))}
        </ul>
      ) : <p className="panel-reading">{t("settings.noHistory")}</p>}
    </div>
  );
}

export function SettingsHealthPage({ apiClient }: SettingsHealthPageProps) {
  const { language, setLanguage, t } = useI18n();
  const healthLoader = useCallback((signal: AbortSignal) => apiClient.health(signal), [apiClient]);
  const providerLoader = useCallback((signal: AbortSignal) => apiClient.providerHealth(signal), [apiClient]);
  const releaseLoader = useCallback((signal: AbortSignal) => apiClient.releaseHealth(signal), [apiClient]);
  const modelLoader = useCallback((signal: AbortSignal) => apiClient.modelHealth(signal), [apiClient]);
  const statsLoader = useCallback((signal: AbortSignal) => apiClient.stats(signal), [apiClient]);
  const schedulerLoader = useCallback((signal: AbortSignal) => apiClient.schedulerStatus(signal), [apiClient]);
  const schedulerHistoryLoader = useCallback((signal: AbortSignal) => apiClient.schedulerHistory(5, signal), [apiClient]);
  const alertLoader = useCallback((signal: AbortSignal) => apiClient.alertStatus(signal), [apiClient]);
  const contextLoader = useCallback((signal: AbortSignal) => apiClient.contextHealth(signal), [apiClient]);
  const appSettingsLoader = useCallback((signal: AbortSignal) => apiClient.appSettings(signal), [apiClient]);
  const hydrationLoader = useCallback((signal: AbortSignal) => apiClient.hydrationStatus(signal), [apiClient]);

  const health = useAsyncResource(healthLoader);
  const provider = useAsyncResource(providerLoader);
  const release = useAsyncResource(releaseLoader);
  const model = useAsyncResource(modelLoader);
  const stats = useAsyncResource(statsLoader);
  const scheduler = useAsyncResource(schedulerLoader);
  const schedulerHistory = useAsyncResource(schedulerHistoryLoader);
  const alerts = useAsyncResource(alertLoader);
  const context = useAsyncResource(contextLoader);
  const appSettings = useAsyncResource(appSettingsLoader);
  const hydration = useAsyncResource(hydrationLoader);
  const [action, setAction] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [preferenceLanguage, setPreferenceLanguage] = useState<"en" | "zh-CN">(language);
  const [aiLanguage, setAiLanguage] = useState<"follow_ui" | "en" | "zh-CN">("follow_ui");
  const [notificationLanguage, setNotificationLanguage] = useState<"en" | "zh-CN">("en");
  const [modelPreference, setModelPreference] = useState<"auto" | "fast" | "smart">("auto");
  const [closeToTray, setCloseToTray] = useState(false);
  const [autoStart, setAutoStart] = useState(false);
  const [resumeMonitoring, setResumeMonitoring] = useState(false);
  const [nativeAutoStart, setNativeAutoStart] = useState<boolean | null>(null);
  const [backendAction, setBackendAction] = useState<string | null>(null);
  const [backendActionStatus, setBackendActionStatus] = useState<string | null>(null);
  const [preferencesStatus, setPreferencesStatus] = useState<string | null>(null);
  const [notificationStatus, setNotificationStatus] = useState<string | null>(null);
  const [desktopBackend, setDesktopBackend] = useState<DesktopBackendStatus | null>(null);
  const [hydrationAction, setHydrationAction] = useState(false);
  const preferencesDirty = useRef(false);

  useEffect(() => {
    if (!isTauriRuntime()) return () => undefined;
    let cleanup: (() => void) | undefined;
    const onState = (status: DesktopBackendStatus) => setDesktopBackend(status);
    void registerDesktopBackendState(onState).then((unregister) => { cleanup = unregister; });
    return () => cleanup?.();
  }, []);

  useEffect(() => {
    if (!appSettings.data || preferencesDirty.current) return;
    const valueFor = (key: string) => appSettings.data?.find((setting) => setting.key === key)?.value;
    const ui = valueFor("ui.language");
    const ai = valueFor("ai.response_language");
    const notification = valueFor("notifications.language");
    const model = valueFor("ai.model_preference");
    const close = valueFor("desktop.close_to_tray");
    const start = valueFor("desktop.auto_start");
    const resume = valueFor("monitoring.resume");
    if (ui === "en" || ui === "zh-CN") setPreferenceLanguage(ui);
    if (ai === "follow_ui" || ai === "en" || ai === "zh-CN") setAiLanguage(ai);
    if (notification === "en" || notification === "zh-CN") setNotificationLanguage(notification);
    if (model === "auto" || model === "fast" || model === "smart") setModelPreference(model);
    if (typeof close === "boolean") setCloseToTray(close);
    // In the packaged desktop the official plugin is the authority.  An
    // AppData preference may survive uninstall while Windows registration is
    // intentionally removed, so allowing that stale value to win creates a
    // checked box beside an "OS disabled" status.  Browser/dev mode has no
    // native registry and continues to use the API value.
    if (typeof start === "boolean" && !isTauriRuntime()) setAutoStart(start);
    if (typeof resume === "boolean") setResumeMonitoring(resume);
  }, [appSettings.data]);

  useEffect(() => {
    let cancelled = false;
    void readAutostartState().then((value) => {
      if (!cancelled && value !== null && !preferencesDirty.current) {
        setNativeAutoStart(value);
        setAutoStart(value);
      }
    });
    return () => { cancelled = true; };
  }, []);

  async function savePreferences() {
    setPreferencesStatus(null);
    try {
      const osAutoStart = await setAutostartState(autoStart);
      if (isTauriRuntime() && osAutoStart !== autoStart) {
        throw new Error(t("settings.autoStartSyncFailed"));
      }
      await Promise.all([
        apiClient.updateAppSetting("ui.language", preferenceLanguage),
        apiClient.updateAppSetting("ai.response_language", aiLanguage),
        apiClient.updateAppSetting("notifications.language", notificationLanguage),
        apiClient.updateAppSetting("ai.model_preference", modelPreference),
        apiClient.updateAppSetting("desktop.close_to_tray", closeToTray),
        apiClient.updateAppSetting("desktop.auto_start", autoStart),
        apiClient.updateAppSetting("monitoring.resume", resumeMonitoring),
      ]);
      setNativeAutoStart(osAutoStart ?? autoStart);
      setLanguage(preferenceLanguage);
      setPreferencesStatus(t("v11.preferencesSaved"));
      appSettings.retry();
    } catch (error: unknown) {
      setPreferencesStatus(error instanceof Error ? error.message : t("common.panelUnavailable"));
    }
  }

  async function restartBackend() {
    setBackendAction("restart");
    setBackendActionStatus(null);
    try {
      const status = await restartOwnedBackend();
      if (!status) throw new Error(t("settings.backendRestartUnavailable"));
      setDesktopBackend(status);
      setBackendActionStatus(`${t("settings.backendRestarted")}: ${status.state}`);
      health.retry();
      provider.retry();
      model.retry();
    } catch (error: unknown) {
      setBackendActionStatus(error instanceof Error ? error.message : t("settings.backendRestartFailed"));
    } finally {
      setBackendAction(null);
    }
  }

  async function refreshHydration() {
    setHydrationAction(true);
    try {
      await apiClient.refreshHydration();
      hydration.retry();
    } finally {
      setHydrationAction(false);
    }
  }

  async function sendTestNotification() {
    setNotificationStatus(null);
    const sent = await notifyDesktopAlert({
      title: t("settings.testNotificationTitle"),
      body: t("settings.testNotificationBody"),
      route: "/alerts",
    });
    setNotificationStatus(t(sent ? "settings.testNotificationSent" : "settings.testNotificationUnavailable"));
  }

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
      setActionError(error instanceof Error ? error.message : t("settings.actionFailed"));
    } finally {
      setAction(null);
    }
  }

  return (
    <section className="settings-page" aria-labelledby="settings-health-title">
      <header className="page-intro">
        <p className="eyebrow">{t("settings.eyebrow")}</p>
        <h1 id="settings-health-title">{t("settings.title")}</h1>
        <p className="page-intro__description">
          {t("settings.description")}
        </p>
        <p className="page-boundary">{t("settings.boundary")}</p>
      </header>

      <div className="settings-grid">
        <section className="async-panel settings-preferences" aria-labelledby="preferences-title">
          <div className="async-panel__header"><div><p className="eyebrow">{t("settings.preferencesEyebrow")}</p><h2 id="preferences-title">{t("settings.preferencesTitle")}</h2></div></div>
          <div className="preference-grid">
            <label>{t("language.label")}<select value={preferenceLanguage} onChange={(event) => { preferencesDirty.current = true; setPreferenceLanguage(event.target.value as "en" | "zh-CN"); }}><option value="zh-CN">{t("language.chinese")}</option><option value="en">{t("language.english")}</option></select></label>
            <label>{t("v11.aiLanguage")}<select value={aiLanguage} onChange={(event) => { preferencesDirty.current = true; setAiLanguage(event.target.value as "follow_ui" | "en" | "zh-CN"); }}><option value="follow_ui">{t("v11.followUi")}</option><option value="zh-CN">{t("language.chinese")}</option><option value="en">{t("language.english")}</option></select></label>
            <label>{t("v11.notificationLanguage")}<select value={notificationLanguage} onChange={(event) => { preferencesDirty.current = true; setNotificationLanguage(event.target.value as "en" | "zh-CN"); }}><option value="zh-CN">{t("language.chinese")}</option><option value="en">{t("language.english")}</option></select></label>
            <label>{t("v11.modelPreference")}<select value={modelPreference} onChange={(event) => { preferencesDirty.current = true; setModelPreference(event.target.value as "auto" | "fast" | "smart"); }}><option value="auto">{t("v11.auto")}</option><option value="fast">{t("v11.fast")}</option><option value="smart">{t("v11.smart")}</option></select></label>
          </div>
          <div className="preference-subsection">
            <p className="subsection-label">{t("settings.desktopTitle")}</p>
            <div className="preference-toggles">
              <label><input type="checkbox" checked={closeToTray} onChange={(event) => { preferencesDirty.current = true; setCloseToTray(event.target.checked); }} />{t("settings.closeToTray")}</label>
              <label><input type="checkbox" checked={autoStart} onChange={(event) => { preferencesDirty.current = true; setAutoStart(event.target.checked); }} />{t("settings.autoStart")}</label>
              <label><input type="checkbox" checked={resumeMonitoring} onChange={(event) => { preferencesDirty.current = true; setResumeMonitoring(event.target.checked); }} />{t("settings.resumeMonitoring")}</label>
            </div>
            <p className="panel-reading">{t("settings.desktopDefaults")}</p>
            <p className="panel-reading">{t("settings.autoStartOsState")}: {nativeAutoStart === null ? t("common.notSupplied") : t(nativeAutoStart ? "common.enabled" : "common.disabled")}</p>
          </div>
          <button className="primary-button" type="button" onClick={() => void savePreferences()}>{t("v11.savePreferences")}</button>
          {preferencesStatus ? <p role="status">{preferencesStatus}</p> : null}
          <div className="preference-action-row">
            <button className="quiet-button" type="button" onClick={() => void sendTestNotification()}>{t("settings.testNotification")}</button>
            {notificationStatus ? <p role="status">{notificationStatus}</p> : null}
          </div>
          <p className="panel-reading">{t("v11.localAlertsOnly")}</p>
        </section>
        <AsyncPanel
          title={t("settings.backend")}
          source="GET /health"
          freshness={t("settings.endpointTimestampMissing")}
          state={healthDegraded ? "degraded" : resourceState(health, false)}
          error={health.error}
          onRetry={health.retry}
          degradedMessage={t("settings.backendDegraded")}
        >
          {health.data ? <HealthFacts health={health.data} /> : null}
          {desktopBackend ? (
            <dl className="fact-list fact-list--compact settings-desktop-facts">
              <div className="fact-list__row"><dt>{t("settings.backendAddress")}</dt><dd>{desktopBackend.base_url || t("common.notSupplied")}</dd></div>
              <div className="fact-list__row"><dt>{t("settings.backendPid")}</dt><dd>{desktopBackend.pid ?? t("common.notSupplied")}</dd></div>
              <div className="fact-list__row"><dt>{t("settings.backendInstance")}</dt><dd>{desktopBackend.instance_id ?? t("common.notSupplied")}</dd></div>
              <div className="fact-list__row"><dt>{t("settings.backendContract")}</dt><dd>{desktopBackend.contract_version} · {t("settings.backendOwnership")} {t(desktopBackend.ownership_verified ? "common.yes" : "common.no")}</dd></div>
              {desktopBackend.last_error ? <div className="fact-list__row"><dt>{t("settings.backendError")}</dt><dd>{desktopBackend.last_error}</dd></div> : null}
            </dl>
          ) : null}
        </AsyncPanel>
        <div className="scheduler-actions" aria-label={t("settings.backendLifecycleControls")}>
          <button className="quiet-button" type="button" onClick={() => void restartBackend()} disabled={backendAction !== null}>{backendAction === "restart" ? t("settings.backendRestarting") : t("settings.restartBackend")}</button>
          {backendActionStatus ? <p className="panel-message" role="status">{backendActionStatus}</p> : null}
        </div>

        <AsyncPanel
          title={t("settings.routing")}
          source="GET /health/providers"
          freshness={t("settings.probeTimestampMissing")}
          state={providerDegraded ? "degraded" : resourceState(provider, !providerHasContent)}
          error={provider.error}
          onRetry={provider.retry}
          emptyMessage={t("settings.noRouting")}
          degradedMessage={t("settings.routingDegraded")}
        >
          {provider.data ? <ProviderRouting provider={provider.data} /> : null}
        </AsyncPanel>

        <AsyncPanel
          title={t("settings.hydration")}
          source="GET /hydration/status"
          freshness={hydration.data?.last_success_at ?? t("settings.healthProbeTimestampMissing")}
          state={hydration.status === "loading" ? "loading" : hydration.status === "unavailable" ? "unavailable" : hydration.data?.state === "degraded" ? "degraded" : hydration.data ? "ready" : "empty"}
          error={hydration.error}
          onRetry={hydration.retry}
          emptyMessage={t("dashboard.hydrationOffline")}
          degradedMessage={t("dashboard.hydrationDegraded")}
        >
          {hydration.data ? (
            <div className="scheduler-facts">
              <dl className="fact-list fact-list--compact">
                <div className="fact-list__row"><dt>{t("settings.hydration")}</dt><dd>{hydration.data.state} · {t("settings.workerAlive")} {t(hydration.data.worker_alive ? "common.yes" : "common.no")}</dd></div>
                <div className="fact-list__row"><dt>{t("settings.hydrationLastUpdate")}</dt><dd>{hydration.data.last_success_at ?? t("common.notSupplied")}</dd></div>
                <div className="fact-list__row"><dt>{t("settings.hydrationSources")}</dt><dd>{schedulerText(hydration.data.sources, t("common.notDisplayable"))}</dd></div>
                <div className="fact-list__row"><dt>{t("settings.cache")}</dt><dd>{schedulerText(hydration.data.cache, t("common.notDisplayable"))}</dd></div>
              </dl>
              <button className="quiet-button" type="button" onClick={() => void refreshHydration()} disabled={hydrationAction}>{hydrationAction ? t("settings.hydrationRefreshing") : t("settings.hydrationRefresh")}</button>
            </div>
          ) : null}
        </AsyncPanel>

        <AsyncPanel
          title={t("settings.release")}
          source="GET /health/release"
          freshness={t("settings.versionedCapability")}
          state={releaseState(release)}
          error={release.error}
          onRetry={release.retry}
          emptyMessage={t("settings.noRelease")}
          degradedMessage={t("settings.releaseDegraded")}
        >
          {release.data ? (
            <div className="scheduler-facts">
              <dl className="fact-list fact-list--compact">
                <div className="fact-list__row"><dt>{t("settings.contract")}</dt><dd>{t("common.phase")} {release.data.phase} · API {release.data.api_version}</dd></div>
                <div className="fact-list__row"><dt>{t("settings.database")}</dt><dd>{schedulerText(release.data.database, t("common.notDisplayable"))}</dd></div>
                <div className="fact-list__row"><dt>{t("settings.backup")}</dt><dd>{schedulerText(release.data.backup, t("common.notDisplayable"))}</dd></div>
                <div className="fact-list__row"><dt>{t("settings.boundaries")}</dt><dd>{schedulerText(release.data.capabilities, t("common.notDisplayable"))}</dd></div>
              </dl>
              <p className="panel-reading">{t("settings.loopback")}</p>
            </div>
          ) : null}
        </AsyncPanel>

        <AsyncPanel
          title={t("settings.localModel")}
          source="GET /health/model"
          freshness={t("settings.healthProbeTimestampMissing")}
          state={modelDegraded ? "degraded" : resourceState(model, !modelHasContent)}
          error={model.error}
          onRetry={model.retry}
          emptyMessage={t("settings.noModel")}
          degradedMessage={t("settings.modelDegraded")}
        >
          {model.data ? <ModelFacts model={model.data} /> : null}
        </AsyncPanel>

        <AsyncPanel
          title={t("settings.scheduler")}
          source="GET /scheduler/status"
          freshness={t("settings.runtimeStatus")}
          state={schedulerState(scheduler)}
          error={scheduler.error}
          onRetry={scheduler.retry}
          degradedMessage={t("settings.schedulerDegraded")}
          className="settings-panel--scheduler"
        >
          {scheduler.data ? (
            <>
              <SchedulerFacts status={scheduler.data} history={schedulerHistory.data} />
              <div className="scheduler-actions" aria-label={t("settings.lifecycleControls")}>
                {!scheduler.data.enabled ? <button className="primary-button" type="button" onClick={() => void runSchedulerAction("enable")} disabled={action !== null}>{action === "enable" ? t("settings.enabling") : t("settings.enable")}</button> : null}
                {scheduler.data.enabled && !scheduler.data.running ? <button className="primary-button" type="button" onClick={() => void runSchedulerAction("start")} disabled={action !== null}>{action === "start" ? t("settings.starting") : t("settings.start")}</button> : null}
                {scheduler.data.running ? <button className="quiet-button" type="button" onClick={() => void runSchedulerAction("stop")} disabled={action !== null}>{action === "stop" ? t("settings.stopping") : t("settings.stop")}</button> : null}
                {scheduler.data.enabled && !scheduler.data.running ? <button className="quiet-button" type="button" onClick={() => void runSchedulerAction("run")} disabled={action !== null}>{action === "run" ? t("settings.scanning") : t("settings.runOnce")}</button> : null}
                {scheduler.data.enabled ? <button className="quiet-button" type="button" onClick={() => void runSchedulerAction("disable")} disabled={action !== null}>{action === "disable" ? t("settings.disabling") : t("settings.disable")}</button> : null}
              </div>
              {actionError ? <p className="panel-message panel-message--unavailable" role="alert">{actionError}</p> : null}
            </>
          ) : null}
        </AsyncPanel>

        <AsyncPanel
          title={t("settings.databaseCounts")}
          source="GET /stats"
          freshness={t("settings.countTimestampMissing")}
          state={resourceState(stats, !statsHasContent)}
          error={stats.error}
          onRetry={stats.retry}
          emptyMessage={t("settings.noCounts")}
        >
          {stats.data ? <CountLedger stats={stats.data} /> : null}
        </AsyncPanel>

        <AsyncPanel
          title={t("settings.alertReconciliation")}
          source="GET /alerts/status"
          freshness={t("settings.alertFreshness")}
          state={alertState(alerts)}
          error={alerts.error}
          onRetry={alerts.retry}
          degradedMessage={t("settings.alertDegraded")}
          className="settings-panel--alerts"
        >
          {alerts.data ? (
            <div className="scheduler-facts">
              <dl className="fact-list fact-list--compact">
                <div className="fact-list__row"><dt>{t("alerts.policyLabel")}</dt><dd>{alerts.data.policy.version} · {t("alerts.retention")} {alerts.data.policy.retention_limit ?? t("common.notSuppliedValue")}</dd></div>
                <div className="fact-list__row"><dt>{t("settings.counts")}</dt><dd>{schedulerText(alerts.data.counts, t("common.notDisplayable"))}</dd></div>
                <div className="fact-list__row"><dt>{t("settings.lastRefresh")}</dt><dd>{alerts.data.last_reconciliation ? schedulerText(alerts.data.last_reconciliation, t("common.notDisplayable")) : t("settings.noReconciliation")}</dd></div>
                <div className="fact-list__row"><dt>{t("settings.capabilities")}</dt><dd>{schedulerText(alerts.data.capabilities, t("common.notDisplayable"))}</dd></div>
              </dl>
              <p className="panel-reading">{t("settings.alertFacts")}</p>
            </div>
          ) : null}
        </AsyncPanel>

        <AsyncPanel
          title={t("settings.contextCapabilities")}
          source="GET /health/context"
          freshness={t("settings.contextFreshness")}
          state={contextState(context)}
          error={context.error}
          onRetry={context.retry}
          emptyMessage={t("settings.noContext")}
          degradedMessage={t("settings.contextDegraded")}
          className="settings-panel--context"
        >
          {context.data ? (
            <div className="scheduler-facts">
              <dl className="fact-list fact-list--compact">
                <div className="fact-list__row"><dt>{t("settings.contract")}</dt><dd>{t("common.phase")} {context.data.phase ?? t("common.notSuppliedValue")} · API {context.data.api_version ?? t("common.notSuppliedValue")}</dd></div>
                <div className="fact-list__row"><dt>{t("settings.benchmark")}</dt><dd>{schedulerText(context.data.benchmark, t("common.notDisplayable"))}</dd></div>
                <div className="fact-list__row"><dt>{t("settings.events")}</dt><dd>{schedulerText(context.data.events, t("common.notDisplayable"))}</dd></div>
                <div className="fact-list__row"><dt>{t("settings.memory")}</dt><dd>{schedulerText(context.data.memory, t("common.notDisplayable"))}</dd></div>
                <div className="fact-list__row"><dt>{t("settings.timePolicyOwner")}</dt><dd>{schedulerText(context.data.time_policy, t("common.notDisplayable"))}</dd></div>
                <div className="fact-list__row"><dt>{t("settings.getBoundary")}</dt><dd>{t("common.readOnly")} {t(context.data.read_only_get ? "common.yes" : "common.no")} · {t("settings.cloudRequired")} {t(context.data.cloud_required ? "common.yes" : "common.no")}</dd></div>
              </dl>
              <p className="panel-reading">{t("settings.contextFacts")}</p>
            </div>
          ) : null}
        </AsyncPanel>
      </div>
    </section>
  );
}
