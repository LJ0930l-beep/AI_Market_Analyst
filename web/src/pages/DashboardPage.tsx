import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import type { ApplicationShellApiClient } from "../api/client";
import type { DailyBrief, HeatmapCell, IntelligenceEvent, MarketIntelligenceResponse, MonitoringItem, PublicHydrationStatus } from "../api/types";
import type { ProvenanceRailState } from "../components/TimeProvenanceRail";
import { useAsyncResource } from "../hooks/useAsyncResource";
import { useI18n } from "../i18n";

export interface DashboardProvenance {
  generatedAt?: string;
  reevaluateAt?: string;
  expiresAt?: string;
  dataSource: string;
  model: string;
  state: ProvenanceRailState;
  footer: string;
}

export function emptyDashboardProvenance(t: ReturnType<typeof useI18n>["t"]): DashboardProvenance {
  return {
    dataSource: t("v11.noPredictionRecord"),
    model: t("v11.noPredictionRecord"),
    state: "neutral",
    footer: t("v11.noRecentPrediction"),
  };
}

interface DashboardPageProps {
  apiClient: ApplicationShellApiClient;
  onProvenanceChange: (provenance: DashboardProvenance) => void;
}

function evidenceTone(value: number | null): string {
  if (value === null) return "neutral";
  return value > 0 ? "positive" : value < 0 ? "negative" : "neutral";
}

function Pulse({ data }: { data: MarketIntelligenceResponse }) {
  const { formatNumber, t, text } = useI18n();
  return (
    <section className="terminal-panel pulse-strip" aria-labelledby="pulse-title">
      <div className="terminal-panel__head"><h2 id="pulse-title">{t("v11.marketPulse")}</h2><span>{t("v11.savedEvidence")} · {data.as_of}</span></div>
      <div className="pulse-grid">
        {data.pulse.map((item) => (
          <article className={`pulse-card pulse-card--${evidenceTone(item.change_pct)}`} key={item.symbol}>
            <div className="pulse-card__symbol"><strong>{item.symbol}</strong><span>{text(String(item.freshness.status ?? item.status))}</span></div>
            {item.price === null ? <p className="pulse-card__missing">{t("v11.unavailable")}</p> : <><p className="pulse-card__price">{formatNumber(item.price, { maximumFractionDigits: 2 })}</p><p className="pulse-card__change">{item.change_pct === null ? t("v11.unavailable") : `${item.change_pct > 0 ? "+" : ""}${formatNumber(item.change_pct, { maximumFractionDigits: 2 })}%`}</p></>}
            <span className="pulse-card__trace" aria-hidden="true" />
          </article>
        ))}
      </div>
    </section>
  );
}

function EventList({ events }: { events: IntelligenceEvent[] }) {
  const { formatDateTime, t, text } = useI18n();
  if (events.length === 0) return <p className="terminal-empty">{t("v11.noEvents")}</p>;
  return <ol className="event-stack">{events.slice(0, 5).map((event, index) => <li key={event.event_id ?? `${event.title}-${index}`}><time>{event.event_at ? formatDateTime(event.event_at, { dateStyle: undefined, timeStyle: "short" }) : "—"}</time><div><strong>{event.title ?? t("v11.unavailable")}</strong><span>{event.source ?? "unknown"} · {text(event.category ?? "other")}</span></div><span className={`impact-pill impact-pill--${Number(event.importance ?? 0) >= 70 ? "high" : "normal"}`}>{event.importance ?? 0}</span></li>)}</ol>;
}

function WatchCards({ items }: { items: MonitoringItem[] }) {
  const { formatPercent, t, text } = useI18n();
  if (items.length === 0) return <p className="terminal-empty">{t("common.noRecords")}</p>;
  return <div className="watch-stack">{items.slice(0, 4).map((item) => <article className="watch-card" key={item.symbol}><div className="watch-card__top"><strong>{item.symbol}</strong><span className={`signal-chip signal-chip--${(item.action ?? "wait").toLowerCase()}`}>{item.action ? text(item.action) : t("v11.awaitingAnalysis")}</span></div><p>{item.summary ?? t("v11.awaitingAnalysis")}</p><div className="watch-card__meta"><span>{text(String(item.market_regime ?? "unknown"))}</span><span>{item.calibrated_confidence == null ? `${t("v11.calibrated")}: —` : `${t("v11.calibrated")}: ${formatPercent(item.calibrated_confidence)}`}</span></div><div className="watch-card__actions"><Link to={`/assets/${encodeURIComponent(item.symbol)}`}>{t("v11.openAsset")}</Link><Link className="ai-link" to={`/consult?symbol=${encodeURIComponent(item.symbol)}`}>{t("v11.askAi")}</Link></div></article>)}</div>;
}

function Heatmap({ cells }: { cells: HeatmapCell[] }) {
  const { formatNumber, t, text } = useI18n();
  if (cells.length === 0) return <p className="terminal-empty">{t("v11.unavailable")}</p>;
  return <div className="terminal-heatmap" role="list" aria-label={t("v11.heatmap")}>{cells.map((cell) => { const magnitude = cell.change_pct === null ? 1 : Math.max(1, Math.min(4, Math.ceil(Math.abs(cell.change_pct)))); return <div className={`heat-cell heat-cell--${evidenceTone(cell.change_pct)} heat-cell--size-${magnitude}`} key={`${cell.group}-${cell.symbol}`} role="listitem"><strong>{cell.symbol}</strong><span>{text(cell.group.replaceAll("_", " "))}</span><b>{cell.change_pct === null ? t("v11.unavailable") : `${cell.change_pct > 0 ? "+" : ""}${formatNumber(cell.change_pct, { maximumFractionDigits: 2 })}%`}</b></div>; })}</div>;
}

function SignalMemo({ signal }: { signal: MarketIntelligenceResponse["latest_signal"] }) {
  const { formatPercent, t, text } = useI18n();
  if (!signal) return <p className="terminal-empty">{t("v11.awaitingAnalysis")}</p>;
  const symbol = signal.symbol ?? "UNKNOWN";
  const action = signal.action ?? "WAIT";
  const confidence = signal.calibrated_confidence ?? signal.raw_confidence ?? 0;
  const boundedConfidence = Math.max(0, Math.min(1, Number.isFinite(confidence) ? confidence : 0));
  const circumference = 2 * Math.PI * 20;
  const dashOffset = circumference * (1 - boundedConfidence);
  return <article className="signal-memo"><div className="signal-memo__identity"><span className="asset-orb" aria-hidden="true">{symbol.slice(0, 1)}</span><div><strong>{symbol}</strong><span>{signal.generated_at ?? "—"}</span></div><span className={`signal-chip signal-chip--${action.toLowerCase()}`}>{text(action)}</span></div><div className="signal-memo__body"><div className="confidence-ring" role="img" aria-label={`${t("common.confidence")} ${formatPercent(boundedConfidence)}`}><svg viewBox="0 0 48 48" aria-hidden="true"><circle className="confidence-ring__track" cx="24" cy="24" r="20" /><circle className="confidence-ring__value" cx="24" cy="24" r="20" strokeDasharray={`${circumference} ${circumference}`} strokeDashoffset={dashOffset} /></svg><div className="confidence-ring__content"><strong>{formatPercent(boundedConfidence)}</strong><span>{t("common.confidence")}</span></div></div><div><p>{signal.summary ?? t("v11.unavailable")}</p><dl className="memo-levels"><div><dt>{t("v11.rawConfidence")}</dt><dd>{signal.raw_confidence == null ? "—" : formatPercent(signal.raw_confidence)}</dd></div><div><dt>{t("v11.calibrated")}</dt><dd>{signal.calibrated_confidence == null ? "—" : formatPercent(signal.calibrated_confidence)}</dd></div><div><dt>{t("common.timeframe")}</dt><dd>{signal.timeframe ?? "—"}</dd></div></dl></div></div><div className="watch-card__actions"><Link to="/predictions">{t("v11.viewSignals")}</Link><Link className="ai-link" to={`/consult?symbol=${encodeURIComponent(symbol)}&prediction_id=${encodeURIComponent(signal.prediction_id ?? "")}`}>{t("v11.askAi")}</Link></div></article>;
}

function HydrationStrip({
  status,
  busy,
  onRefresh,
}: {
  status?: PublicHydrationStatus;
  busy: boolean;
  onRefresh: () => void;
}) {
  const { t } = useI18n();
  const state = status?.state ?? "idle";
  const cache = status?.cache ?? {};
  const fresh = typeof cache.fresh_symbols === "number" ? cache.fresh_symbols : 0;
  const total = Array.isArray(cache.symbols) ? cache.symbols.length : 0;
  const stateKey = state === "ready" ? "dashboard.hydrated" : state === "degraded" ? "dashboard.hydrationDegraded" : state === "disabled" ? "dashboard.hydrationOffline" : "dashboard.hydrating";
  return (
    <section className={`hydration-strip hydration-strip--${state}`} aria-label={t("dashboard.publicHydration")}>
      <div>
        <p className="eyebrow">{t("dashboard.publicHydration")}</p>
        <strong>{t(stateKey)}</strong>
        <span>{fresh}/{total} {t("dashboard.hydrationFreshSymbols")}{status?.last_success_at ? ` · ${status.last_success_at}` : ""}</span>
      </div>
      <div className="hydration-strip__meta">
        <span>{t("dashboard.hydrationSource")}: {t("dashboard.hydrationPublicOnly")}</span>
        <button className="quiet-button" type="button" onClick={onRefresh} disabled={busy || status?.enabled === false}>{busy ? t("dashboard.hydrationRefreshing") : t("dashboard.hydrationRetry")}</button>
      </div>
    </section>
  );
}

function DailyBriefCard({ brief, busy, error, onGenerate }: { brief?: DailyBrief | null; busy: boolean; error: string | null; onGenerate: () => void }) {
  const { formatDateTime, language, t } = useI18n();
  return <section className="terminal-panel terminal-panel--brief" aria-labelledby="brief-title"><div className="terminal-panel__head"><div><span className="ai-kicker">{language === "zh-CN" ? "BONSAI-2-27B · 本地模型" : "BONSAI-2-27B · LOCAL MODEL"}</span><h2 id="brief-title">{t("v11.dailyBrief")}</h2></div><button className="primary-button" type="button" onClick={onGenerate} disabled={busy}>{busy ? t("v11.generatingBrief") : t("v11.generateBrief")}</button></div>{brief ? <><p className="brief-copy">{brief.content}</p><p className="evidence-caption">{t("v11.asOf")} {formatDateTime(brief.as_of)} · {brief.model_id} · {brief.route_reason}</p>{brief.missing.length > 0 ? <p className="capability-note">{brief.missing.join(" · ")}</p> : null}</> : <p className="terminal-empty">{t("v11.noBrief")}</p>}{error ? <p className="panel-message panel-message--unavailable" role="alert">{error}</p> : null}<p className="panel-boundary">{t("v11.briefBoundary")}</p></section>;
}

export function DashboardPage({ apiClient, onProvenanceChange }: DashboardPageProps) {
  const { language, t, text } = useI18n();
  const loader = useCallback((signal: AbortSignal) => apiClient.marketIntelligence(signal), [apiClient]);
  const hydrationLoader = useCallback((signal: AbortSignal) => apiClient.hydrationStatus(signal), [apiClient]);
  const intelligence = useAsyncResource(loader);
  const hydration = useAsyncResource(hydrationLoader);
  const [brief, setBrief] = useState<DailyBrief | null | undefined>();
  const [briefBusy, setBriefBusy] = useState(false);
  const [briefError, setBriefError] = useState<string | null>(null);
  const [hydrationBusy, setHydrationBusy] = useState(false);
  const [hydrationError, setHydrationError] = useState<string | null>(null);
  const data = intelligence.data;
  const hydrationStatus = hydration.data ?? data?.hydration;
  useEffect(() => setBrief(data?.daily_brief), [data?.daily_brief]);
  useEffect(() => {
    const timer = window.setInterval(hydration.retry, 30_000);
    return () => window.clearInterval(timer);
  }, [hydration.retry]);
  useEffect(() => { const signal = data?.latest_signal; onProvenanceChange(signal ? { generatedAt: signal.generated_at ?? undefined, expiresAt: signal.signal_valid_until ?? undefined, dataSource: t("v11.dashboardProvenanceSource"), model: signal.model_id ?? t("common.notSupplied"), state: signal.action === "WAIT" ? "neutral" : "active", footer: t("v11.dashboardReadOnlyFooter") } : { dataSource: t("v11.noPredictionRecord"), model: t("v11.noPredictionRecord"), state: "neutral", footer: t("v11.noRecentPrediction") }); }, [data?.latest_signal, onProvenanceChange, t]);
  const hotCells = useMemo(() => [...(data?.heatmap.cells ?? [])].filter((cell) => cell.change_pct !== null).sort((a, b) => (b.change_pct ?? 0) - (a.change_pct ?? 0)).slice(0, 5), [data]);

  async function generateBrief() {
    setBriefBusy(true); setBriefError(null);
    try {
      const settings = await apiClient.appSettings();
      const configuredLanguage = settings.find((setting) => setting.key === "ai.response_language")?.value;
      const configuredPreference = settings.find((setting) => setting.key === "ai.model_preference")?.value;
      const responseLanguage = configuredLanguage === "en" || configuredLanguage === "zh-CN" ? configuredLanguage : language;
      const preference = configuredPreference === "fast" || configuredPreference === "smart" ? configuredPreference : "auto";
      const response = await apiClient.generateDailyBrief(responseLanguage, preference);
      setBrief(response.brief);
    }
    catch (error: unknown) { setBriefError(error instanceof Error ? error.message : t("consult.errorFallback")); }
    finally { setBriefBusy(false); }
  }

  async function refreshHydration() {
    setHydrationBusy(true);
    setHydrationError(null);
    try {
      await apiClient.refreshHydration();
      hydration.retry();
      intelligence.retry();
    } catch (error: unknown) {
      setHydrationError(error instanceof Error ? error.message : t("dashboard.hydrationDegraded"));
    } finally {
      setHydrationBusy(false);
    }
  }

  if (intelligence.status === "loading") return <section className="terminal-loading" aria-live="polite"><span className="loading-orbit" />{t("common.loading")}</section>;
  if (intelligence.status === "unavailable" || !data) return <section className="terminal-error" role="alert"><h1>{t("v11.dashboardTitle")}</h1><p>{intelligence.error ? String(intelligence.error) : t("common.panelUnavailable")}</p><button type="button" onClick={intelligence.retry}>{t("common.retry")}</button></section>;
  return <section className="v11-dashboard" aria-labelledby="dashboard-title"><header className="dashboard-hero"><div><p className="eyebrow">{t("v11.dashboardEyebrow")}</p><h1 id="dashboard-title">{t("v11.dashboardTitle")}</h1><p>{t("v11.dashboardDescription")}</p></div><div className="asof-seal"><span>{t("v11.asOf")}</span><strong>{data.as_of}</strong><small>{t("v11.readOnlyNoProvider")}</small></div></header><HydrationStrip status={hydrationStatus} busy={hydrationBusy} onRefresh={() => void refreshHydration()} />{hydrationError ? <p className="panel-message panel-message--unavailable" role="alert">{hydrationError}</p> : null}<Pulse data={data} /><div className="terminal-grid"><section className="terminal-panel terminal-panel--events"><div className="terminal-panel__head"><h2>{t("v11.todayEvents")}</h2><Link to="/calendar">{t("nav.calendar")}</Link></div><EventList events={data.calendar.events} /></section><section className="terminal-panel terminal-panel--watch"><div className="terminal-panel__head"><h2>{t("v11.aiWatchlist")}</h2><Link to="/watchlist">{t("nav.watchlist")}</Link></div><WatchCards items={data.watchlist} /></section><section className="terminal-panel terminal-panel--signal"><div className="terminal-panel__head"><h2>{t("v11.latestSignal")}</h2><Link to="/predictions">{t("nav.signals")}</Link></div><SignalMemo signal={data.latest_signal} /></section><section className="terminal-panel terminal-panel--sectors"><div className="terminal-panel__head"><h2>{t("v11.hotSectors")}</h2><Link to="/heatmap">{t("nav.heatmap")}</Link></div><Heatmap cells={hotCells} /></section><section className="terminal-panel terminal-panel--heatmap"><div className="terminal-panel__head"><h2>{t("v11.heatmap")}</h2><span>{data.heatmap.capability}</span></div><Heatmap cells={data.heatmap.cells} /></section><section className="terminal-panel terminal-panel--news"><div className="terminal-panel__head"><h2>{t("v11.newsFeed")}</h2><Link to="/news">{t("nav.news")}</Link></div>{data.news.items.length > 0 ? <EventList events={data.news.items} /> : <p className="terminal-empty">{t("v11.noNews")}</p>}</section></div><DailyBriefCard brief={brief} busy={briefBusy} error={briefError} onGenerate={() => void generateBrief()} /><section className="regime-ribbon" aria-label={t("v11.regimeAria")}><div><span>{t("v11.marketRegime")}</span><strong>{text(String(data.watchlist[0]?.market_regime ?? t("v11.unavailable")))}</strong></div><div><span>{t("v11.riskPosture")}</span><strong>{text(data.calendar.events.some((event) => Number(event.importance ?? 0) >= 70) ? "ELEVATED" : "UNKNOWN")}</strong></div><div><span>{t("v11.opportunity")}</span><strong>{text(data.latest_signal?.action ?? "NOT_RANKED")}</strong></div></section></section>;
}
