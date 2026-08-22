import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";

import type { ApplicationShellApiClient } from "../api/client";
import type { Action, OutcomeStatus, PaperTrade, PaperTradeFilters, Prediction, SourceType, Timeframe } from "../api/types";
import { AsyncPanel, type PanelState } from "../components/AsyncPanel";
import { ResearchFacts, useResearchFormatters } from "../components/ResearchFacts";
import type { AsyncResource } from "../hooks/useAsyncResource";
import { useAsyncResource } from "../hooks/useAsyncResource";
import { useI18n, type TranslationKey } from "../i18n";
import { emptyDashboardProvenance, type DashboardProvenance } from "./DashboardPage";
import { actionLabel, predictionProvenance, predictionValidity, sourceTypeLabel } from "./workflowUtils";

interface PaperTradesPageProps {
  apiClient: ApplicationShellApiClient;
  onProvenanceChange: (provenance: DashboardProvenance) => void;
}

type FilterDraft = {
  symbol: string;
  timeframe: Timeframe | "";
  action: Action | "";
  source_type: SourceType | "";
  status: string;
  outcome_status: OutcomeStatus | "";
};

const timeframeOptions: Timeframe[] = ["5m", "15m", "1h", "4h", "1d"];
const actionOptions: Action[] = ["LONG", "SHORT", "WAIT"];
const outcomeOptions: OutcomeStatus[] = ["TP1", "TP2", "STOP", "TIMEOUT", "INVALIDATED", "PENDING"];
const pageLimits = [25, 50, 100];
const initialDraft: FilterDraft = { symbol: "", timeframe: "", action: "", source_type: "", status: "", outcome_status: "" };
const initialFilters: PaperTradeFilters = { limit: 25, offset: 0 };

function resourceState<T>(resource: AsyncResource<T>, empty: boolean): PanelState {
  if (resource.status === "loading") return "loading";
  if (resource.status === "unavailable") return "unavailable";
  return empty ? "empty" : "ready";
}

function buildFilters(draft: FilterDraft, limit: number): PaperTradeFilters {
  return {
    limit,
    offset: 0,
    ...(draft.symbol.trim() ? { symbol: draft.symbol.trim().toUpperCase() } : {}),
    ...(draft.timeframe ? { timeframe: draft.timeframe } : {}),
    ...(draft.action ? { action: draft.action } : {}),
    ...(draft.source_type ? { source_type: draft.source_type } : {}),
    ...(draft.status.trim() ? { status: draft.status.trim().toUpperCase() } : {}),
    ...(draft.outcome_status ? { outcome_status: draft.outcome_status } : {}),
  };
}

function predictionForTrade(trade: PaperTrade): Prediction | undefined {
  return trade.prediction ?? undefined;
}

function tradeSymbol(trade: PaperTrade, fallback: string): string {
  return trade.prediction?.symbol ?? trade.prediction?.instrument?.symbol ?? fallback;
}

function returnedNumber(record: Record<string, unknown> | undefined, key: string): number | undefined {
  const value = record?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function detailNumberFacts(trade: PaperTrade, t: (key: TranslationKey) => string, numberText: (value: number | null | undefined, digits?: number) => string) {
  const keys: Array<[TranslationKey, string]> = [
    ["common.entryPrice", "entry_price"],
    ["common.stopPrice", "stop_price"],
    ["common.statusTp1", "tp1"],
    ["common.statusTp2", "tp2"],
    ["common.riskR", "risk_r"],
  ];
  return keys.flatMap(([label, key]) => {
    const value = returnedNumber(trade, key);
    return value === undefined ? [] : [{ label: t(label), value: numberText(value) }];
  });
}

function PaperTradeDetail({ trade }: { trade: PaperTrade }) {
  const { t } = useI18n();
  const { exactNumberText, numberText, timestampText } = useResearchFormatters();
  const prediction = predictionForTrade(trade);
  const outcome = trade.outcome;
  return (
    <div className="record-detail">
      <div className="record-detail__heading">
        <div>
          <p className="eyebrow">{t("paperTrades.localRecord")}</p>
          <h3>{trade.prediction_id}</h3>
        </div>
        <span className="status-chip status-chip--neutral">{trade.status}</span>
      </div>
      <p className="paper-only-notice" role="note">{t("paperTrades.only")}</p>

      <ResearchFacts
        facts={[
          { label: t("common.predictionId"), value: trade.prediction_id },
          { label: t("common.status"), value: trade.status },
          { label: t("common.followedAt"), value: timestampText(trade.followed_at) },
          { label: t("common.outcomeStatus"), value: trade.outcome_status ?? outcome?.status ?? t("common.notSupplied") },
          ...(detailNumberFacts(trade, t, numberText)),
        ]}
      />

      <div className="record-detail__section">
        <p className="subsection-label">{t("paperTrades.linkedPrediction")}</p>
        {prediction ? (
          <ResearchFacts
            compact
            facts={[
              { label: t("common.symbol"), value: tradeSymbol(trade, t("common.symbolNotSupplied")) },
              { label: t("common.action"), value: actionLabel(prediction.action, t) },
              { label: t("common.timeframe"), value: prediction.timeframe ?? prediction.analysis_timeframe ?? t("common.notSupplied") },
              { label: t("common.sourceType"), value: sourceTypeLabel(prediction.source_type, t) },
              { label: t("common.rawConfidence"), value: exactNumberText(prediction.raw_confidence) },
              { label: t("common.calibratedConfidence"), value: numberText(prediction.calibrated_confidence, 6) },
              { label: t("common.generated"), value: timestampText(prediction.generated_at) },
              { label: t("common.dataAsOf"), value: timestampText(prediction.data_as_of) },
              { label: t("common.validUntil"), value: timestampText(prediction.signal_valid_until) },
              { label: t("common.validity"), value: predictionValidity(prediction) === "active" ? t("common.active") : predictionValidity(prediction) === "expired" ? t("common.expired") : t("common.unknown") },
              { label: t("common.model"), value: prediction.model_id ?? t("common.notSupplied") },
            ]}
          />
        ) : <p className="panel-reading">{t("common.noLinkedPrediction")}</p>}
      </div>

      <div className="record-detail__section">
        <p className="subsection-label">{t("paperTrades.linkedOutcome")}</p>
        {outcome ? (
          <ResearchFacts
            compact
            facts={[
              { label: t("common.status"), value: outcome.status },
              { label: t("common.settledAt"), value: timestampText(outcome.settled_at) },
              { label: t("common.realizedR"), value: numberText(returnedNumber(outcome, "realized_r")) },
              { label: t("common.mfeR"), value: numberText(returnedNumber(outcome, "mfe_r")) },
              { label: t("common.maeR"), value: numberText(returnedNumber(outcome, "mae_r")) },
            ]}
          />
        ) : <p className="panel-reading">{t("common.noLinkedOutcome")}</p>}
      </div>
    </div>
  );
}

export function PaperTradesPage({ apiClient, onProvenanceChange }: PaperTradesPageProps) {
  const { t, text } = useI18n();
  const { timestampText } = useResearchFormatters();
  const [draft, setDraft] = useState<FilterDraft>(initialDraft);
  const [limit, setLimit] = useState(25);
  const [filters, setFilters] = useState<PaperTradeFilters>(initialFilters);
  const [selectedId, setSelectedId] = useState<string>();

  const listLoader = useCallback((signal: AbortSignal) => apiClient.paperTrades(filters, signal), [apiClient, filters]);
  const trades = useAsyncResource(listLoader);
  const detailLoader = useCallback(
    (signal: AbortSignal) => selectedId ? apiClient.paperTrade(selectedId, signal) : Promise.resolve(undefined),
    [apiClient, selectedId],
  );
  const detail = useAsyncResource(detailLoader, Boolean(selectedId));

  useEffect(() => {
    const prediction = detail.data ? predictionForTrade(detail.data) : undefined;
    if (prediction) {
      const provenance = predictionProvenance(prediction, t);
      onProvenanceChange({
        ...provenance,
        footer: `PaperTrade ${detail.data?.prediction_id} · ${t("paperTrades.provenanceFooter")}`,
      });
    } else {
      onProvenanceChange(emptyDashboardProvenance(t));
    }
  }, [detail.data, onProvenanceChange, t]);

  const list = trades.data ?? [];
  const requestLabel = useMemo(() => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) if (value !== undefined) query.set(key, String(value));
    return query.toString() ? `GET /paper-trades?${query.toString()}` : "GET /paper-trades";
  }, [filters]);

  const applyFilters = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFilters(buildFilters(draft, limit));
    setSelectedId(undefined);
  };

  const movePage = (direction: -1 | 1) => {
    const currentOffset = filters.offset ?? 0;
    setFilters((current) => ({ ...current, offset: Math.max(0, currentOffset + direction * (current.limit ?? limit)) }));
    setSelectedId(undefined);
  };

  return (
    <section className="workflow-page paper-trades-page" aria-labelledby="paper-trades-title">
      <header className="page-intro">
        <p className="eyebrow">{t("paperTrades.eyebrow")}</p>
        <h1 id="paper-trades-title">{t("paperTrades.title")}</h1>
        <p className="page-intro__description">{t("paperTrades.description")}</p>
        <p className="page-boundary">{t("paperTrades.boundary")}</p>
      </header>

      <p className="paper-only-notice paper-only-notice--wide" role="note">{t("paperTrades.permanentBoundary")}</p>

      <form className="workflow-filters" aria-label={t("paperTrades.filters")} onSubmit={applyFilters}>
        <div className="workflow-filters__grid">
          <label>{t("common.symbol")}<input value={draft.symbol} onChange={(event) => setDraft((current) => ({ ...current, symbol: event.target.value }))} placeholder={t("paperTrades.allSymbols")} /></label>
          <label>{t("common.timeframe")}<select value={draft.timeframe} onChange={(event) => setDraft((current) => ({ ...current, timeframe: timeframeOptions.includes(event.target.value as Timeframe) ? event.target.value as Timeframe : "" }))}><option value="">{t("common.allTimeframes")}</option>{timeframeOptions.map((value) => <option key={value}>{value}</option>)}</select></label>
          <label>{t("common.action")}<select value={draft.action} onChange={(event) => setDraft((current) => ({ ...current, action: actionOptions.includes(event.target.value as Action) ? event.target.value as Action : "" }))}><option value="">{t("common.allActions")}</option>{actionOptions.map((value) => <option key={value} value={value}>{actionLabel(value, t)}</option>)}</select></label>
          <label>{t("common.sourceType")}<select value={draft.source_type} onChange={(event) => setDraft((current) => ({ ...current, source_type: event.target.value === "live" || event.target.value === "replay" ? event.target.value : "" }))}><option value="">{t("common.allSources")}</option><option value="live">{t("common.sourceLive")}</option><option value="replay">{t("common.sourceReplay")}</option></select></label>
          <label>{t("paperTrades.status")}<input value={draft.status} onChange={(event) => setDraft((current) => ({ ...current, status: event.target.value }))} placeholder={t("paperTrades.anyStatus")} /></label>
          <label>{t("common.outcomeStatus")}<select value={draft.outcome_status} onChange={(event) => setDraft((current) => ({ ...current, outcome_status: outcomeOptions.includes(event.target.value as OutcomeStatus) ? event.target.value as OutcomeStatus : "" }))}><option value="">{t("common.anyOutcome")}</option>{outcomeOptions.map((value) => <option key={value} value={value}>{text(value)}</option>)}</select></label>
          <label>{t("common.rows")}<select value={limit} onChange={(event) => setLimit(Number(event.target.value))}>{pageLimits.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
        </div>
        <button className="primary-button" type="submit">{t("common.applyFilters")}</button>
      </form>

      <div className="workflow-grid workflow-grid--records">
        <AsyncPanel
          className="workflow-panel workflow-panel--list"
          title={t("paperTrades.records")}
          source={requestLabel}
          freshness={t("common.followedAtRecord")}
          state={resourceState(trades, list.length === 0)}
          error={trades.error}
          onRetry={trades.retry}
          emptyMessage={t("paperTrades.noMatch")}
        >
          {list.length > 0 ? (
            <div className="prediction-ledger-wrap">
              <table className="prediction-ledger" aria-label={t("paperTrades.records")}>
                <thead><tr><th scope="col">{t("common.prediction")}</th><th scope="col">{t("common.actionSymbol")}</th><th scope="col">{t("paperTrades.status")}</th><th scope="col">{t("common.followedAt")}</th><th scope="col">{t("common.outcome")}</th></tr></thead>
                <tbody>{list.map((trade) => <tr key={trade.prediction_id}>
                  <td><button className="record-link" type="button" onClick={() => setSelectedId(trade.prediction_id)}>{trade.prediction_id}</button></td>
                  <td><span className="signal-text">{actionLabel(trade.prediction?.action, t)}</span><span className="data-meta">{tradeSymbol(trade, t("common.symbolNotSupplied"))}</span></td>
                  <td>{trade.status}</td>
                  <td>{timestampText(trade.followed_at)}</td>
                  <td>{trade.outcome_status ?? trade.outcome?.status ?? t("common.noOutcome")}</td>
                </tr>)}</tbody>
              </table>
            </div>
          ) : null}
          <div className="pagination-controls" aria-label={t("paperTrades.pagination")}><span>{t("common.offset")} {filters.offset ?? 0} · {t("common.maximum")} {filters.limit ?? limit} {t("common.rowsValue")}</span><div><button className="quiet-button" type="button" disabled={(filters.offset ?? 0) === 0} onClick={() => movePage(-1)}>{t("common.previous")}</button><button className="quiet-button" type="button" disabled={list.length < (filters.limit ?? limit)} onClick={() => movePage(1)}>{t("common.next")}</button></div></div>
        </AsyncPanel>

        <AsyncPanel
          className="workflow-panel workflow-panel--detail"
          title={t("paperTrades.detail")}
          source={selectedId ? `GET /paper-trades/${selectedId}` : "GET /paper-trades/{prediction_id}"}
          freshness={t("common.returnedTimestampsUtc")}
          state={!selectedId ? "empty" : detail.status === "idle" || detail.status === "loading" ? "loading" : resourceState(detail, !detail.data)}
          error={detail.error}
          onRetry={detail.retry}
          emptyMessage={t("paperTrades.selectDetail")}
        >
          {detail.data ? <PaperTradeDetail trade={detail.data} /> : null}
        </AsyncPanel>
      </div>
    </section>
  );
}
