import { useCallback, useState } from "react";
import { Link } from "react-router-dom";

import type { ApplicationShellApiClient } from "../api/client";
import type { HeatmapCell, IntelligenceEvent, MarketIntelligenceResponse } from "../api/types";
import { useAsyncResource } from "../hooks/useAsyncResource";
import { useI18n, type TranslationKey } from "../i18n";

type Surface = "markets" | "calendar" | "news" | "heatmap";

const titles: Record<Surface, TranslationKey> = {
  markets: "v11.marketsTitle",
  calendar: "v11.calendarTitle",
  news: "v11.newsTitle",
  heatmap: "v11.heatmapTitle",
};
const descriptions: Record<Surface, TranslationKey> = {
  markets: "v11.marketsDescription",
  calendar: "v11.calendarDescription",
  news: "v11.newsDescription",
  heatmap: "v11.heatmapDescription",
};

function value(value: unknown): string {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

function EventRows({ events, emptyKey }: { events: IntelligenceEvent[]; emptyKey: TranslationKey }) {
  const { formatDateTime, t } = useI18n();
  if (events.length === 0) return <p className="terminal-empty">{t(emptyKey)}</p>;
  return <div className="intelligence-table-wrap" tabIndex={0} aria-label={t("v11.calendar")}><table className="intelligence-table"><thead><tr><th>{t("v11.eventTime")}</th><th>{t("common.source")}</th><th>{t("common.category")}</th><th>{t("v11.impact")}</th><th>{t("v11.forecast")}</th><th>{t("v11.previous")}</th><th>{t("v11.actual")}</th><th>{t("v11.affected")}</th><th>{t("v11.knownAt")}</th></tr></thead><tbody>{events.map((event, index) => <tr key={event.event_id ?? index}><td>{event.event_at ? formatDateTime(event.event_at) : "—"}<strong>{event.title ?? "—"}</strong></td><td>{value(event.source)}</td><td>{value(event.category)}</td><td><span className={`impact-pill impact-pill--${Number(event.importance ?? 0) >= 70 ? "high" : "normal"}`}>{value(event.importance)}</span></td><td>{value(event.forecast)}</td><td>{value(event.previous)}</td><td>{value(event.actual)}</td><td>{event.affected_symbols?.join(", ") || "—"}</td><td>{event.known_at ? formatDateTime(event.known_at) : "—"}</td></tr>)}</tbody></table></div>;
}

function MarketCards({ data }: { data: MarketIntelligenceResponse }) {
  const { formatNumber, t } = useI18n();
  return <div className="market-card-grid">{data.pulse.map((item) => <article className={`market-card market-card--${item.change_pct === null ? "neutral" : item.change_pct >= 0 ? "positive" : "negative"}`} key={item.symbol}><div><strong>{item.symbol}</strong><span>{String(item.freshness.status ?? item.status)}</span></div><p>{item.price === null ? t("v11.unavailable") : formatNumber(item.price, { maximumFractionDigits: 2 })}</p><b>{item.change_pct === null ? "—" : `${item.change_pct >= 0 ? "+" : ""}${formatNumber(item.change_pct)}%`}</b><small>{item.missing_reasons.join(" · ") || t("v11.savedEvidence")}</small></article>)}</div>;
}

function groupLabel(group: string, language: string): string {
  const labels: Record<string, [string, string]> = {
    semiconductors: ["Semiconductors", "半导体"], ai_technology: ["AI / Technology", "AI / 科技"], energy: ["Energy", "能源"], financials: ["Financials", "金融"], crypto_majors: ["Crypto majors", "主流 Crypto"], crypto_layer1: ["Layer 1", "Layer 1"], crypto_ai: ["AI tokens", "AI Token"], crypto_l2: ["Layer 2", "Layer 2"], crypto_defi: ["DeFi", "DeFi"], crypto_rwa: ["RWA", "RWA"],
  };
  return labels[group]?.[language === "zh-CN" ? 1 : 0] ?? group;
}

function HeatmapBoard({ cells }: { cells: HeatmapCell[] }) {
  const { formatNumber, language, t } = useI18n();
  const [assetType, setAssetType] = useState<"all" | "equity" | "crypto">("all");
  const shown = cells.filter((cell) => assetType === "all" || cell.asset_type === assetType);
  return <><div className="heatmap-tabs" role="group" aria-label={t("common.assetType")}><button type="button" className={assetType === "all" ? "is-active" : ""} onClick={() => setAssetType("all")}>{t("common.allAssetTypes")}</button><button type="button" className={assetType === "equity" ? "is-active" : ""} onClick={() => setAssetType("equity")}>{t("common.assetEquity")}</button><button type="button" className={assetType === "crypto" ? "is-active" : ""} onClick={() => setAssetType("crypto")}>{t("common.assetCrypto")}</button></div><div className="heatmap-board">{shown.map((cell) => <article className={`heatmap-tile heatmap-tile--${cell.change_pct === null ? "neutral" : cell.change_pct >= 0 ? "positive" : "negative"}`} key={`${cell.group}-${cell.symbol}`}><span>{groupLabel(cell.group, language)}</span><strong>{cell.symbol}</strong><b>{cell.change_pct === null ? t("v11.unavailable") : `${cell.change_pct >= 0 ? "+" : ""}${formatNumber(cell.change_pct, { maximumFractionDigits: 2 })}%`}</b><small>{cell.status} · {cell.missing_reasons.join(" · ") || t("v11.savedEvidence")}</small></article>)}</div></>;
}

export function MarketIntelligencePage({ apiClient, surface }: { apiClient: ApplicationShellApiClient; surface: Surface }) {
  const { t } = useI18n();
  const loader = useCallback((signal: AbortSignal) => apiClient.marketIntelligence(signal), [apiClient]);
  const resource = useAsyncResource(loader);
  if (resource.status === "loading") return <section className="terminal-loading" aria-live="polite">{t("common.loading")}</section>;
  if (resource.status === "unavailable" || !resource.data) return <section className="terminal-error" role="alert"><h1>{t(titles[surface])}</h1><p>{resource.error ? String(resource.error) : t("common.panelUnavailable")}</p><button type="button" onClick={resource.retry}>{t("common.retry")}</button></section>;
  const data = resource.data;
  return <section className={`intelligence-page intelligence-page--${surface}`} aria-labelledby={`${surface}-title`}><header className="page-intro page-intro--terminal"><p className="eyebrow">V1.1 · {data.contract_version}</p><h1 id={`${surface}-title`}>{t(titles[surface])}</h1><p>{t(descriptions[surface])}</p><p className="page-boundary">{t("v11.asOf")} {data.as_of} · GET {t("common.readOnly")} · {t("v11.providerCalls")} {String(data.provider_calls)} · {t("v11.domainWrites")} {String(data.domain_writes)}</p></header><section className="terminal-panel intelligence-main">{surface === "markets" ? <MarketCards data={data} /> : null}{surface === "calendar" ? <EventRows events={data.calendar.events} emptyKey="v11.noEvents" /> : null}{surface === "news" ? <EventRows events={data.news.items} emptyKey="v11.noNews" /> : null}{surface === "heatmap" ? <HeatmapBoard cells={data.heatmap.cells} /> : null}</section><nav className="surface-crosslinks" aria-label={t("v11.marketIntelligenceNav")}><Link to="/markets">{t("nav.markets")}</Link><Link to="/calendar">{t("nav.calendar")}</Link><Link to="/news">{t("nav.news")}</Link><Link to="/heatmap">{t("nav.heatmap")}</Link></nav></section>;
}
