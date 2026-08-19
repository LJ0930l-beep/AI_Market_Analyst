import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";

import type { ApplicationShellApiClient } from "../api/client";
import type { Action, OutcomeStatus, PaperTrade, PaperTradeFilters, Prediction, SourceType, Timeframe } from "../api/types";
import { AsyncPanel, type PanelState } from "../components/AsyncPanel";
import { ResearchFacts, exactNumberText, numberText, timestampText } from "../components/ResearchFacts";
import type { AsyncResource } from "../hooks/useAsyncResource";
import { useAsyncResource } from "../hooks/useAsyncResource";
import { EMPTY_DASHBOARD_PROVENANCE, type DashboardProvenance } from "./DashboardPage";
import { actionLabel, predictionProvenance, predictionValidity } from "./workflowUtils";

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

function tradeSymbol(trade: PaperTrade): string {
  return trade.prediction?.symbol ?? trade.prediction?.instrument?.symbol ?? "Symbol not supplied";
}

function returnedNumber(record: Record<string, unknown> | undefined, key: string): number | undefined {
  const value = record?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function detailNumberFacts(trade: PaperTrade) {
  const keys: Array<[string, string]> = [
    ["Entry price", "entry_price"],
    ["Stop price", "stop_price"],
    ["TP1", "tp1"],
    ["TP2", "tp2"],
    ["Risk R", "risk_r"],
  ];
  return keys.flatMap(([label, key]) => {
    const value = returnedNumber(trade, key);
    return value === undefined ? [] : [{ label, value: numberText(value) }];
  });
}

function PaperTradeDetail({ trade }: { trade: PaperTrade }) {
  const prediction = predictionForTrade(trade);
  const outcome = trade.outcome;
  return (
    <div className="record-detail">
      <div className="record-detail__heading">
        <div>
          <p className="eyebrow">Local PaperTrade record</p>
          <h3>{trade.prediction_id}</h3>
        </div>
        <span className="status-chip status-chip--neutral">{trade.status}</span>
      </div>
      <p className="paper-only-notice" role="note">PaperTrade only: this record is local research coverage. No real order, broker action or execution is performed.</p>

      <ResearchFacts
        facts={[
          { label: "Prediction id", value: trade.prediction_id },
          { label: "Status", value: trade.status },
          { label: "Followed at", value: timestampText(trade.followed_at) },
          { label: "Outcome status", value: trade.outcome_status ?? outcome?.status ?? "Not supplied" },
          ...(detailNumberFacts(trade)),
        ]}
      />

      <div className="record-detail__section">
        <p className="subsection-label">Linked Prediction</p>
        {prediction ? (
          <ResearchFacts
            compact
            facts={[
              { label: "Symbol", value: tradeSymbol(trade) },
              { label: "Action", value: actionLabel(prediction.action) },
              { label: "Timeframe", value: prediction.timeframe ?? prediction.analysis_timeframe ?? "Not supplied" },
              { label: "Source type", value: prediction.source_type ?? "Not supplied" },
              { label: "Raw confidence", value: exactNumberText(prediction.raw_confidence) },
              { label: "Calibrated confidence", value: numberText(prediction.calibrated_confidence, 6) },
              { label: "Generated at", value: timestampText(prediction.generated_at) },
              { label: "Data as of", value: timestampText(prediction.data_as_of) },
              { label: "Signal valid until", value: timestampText(prediction.signal_valid_until) },
              { label: "Validity", value: predictionValidity(prediction) },
              { label: "Model", value: prediction.model_id ?? "Not supplied" },
            ]}
          />
        ) : <p className="panel-reading">No linked Prediction evidence was returned.</p>}
      </div>

      <div className="record-detail__section">
        <p className="subsection-label">Linked Outcome</p>
        {outcome ? (
          <ResearchFacts
            compact
            facts={[
              { label: "Status", value: outcome.status },
              { label: "Settled at", value: timestampText(outcome.settled_at) },
              { label: "Realized R", value: numberText(returnedNumber(outcome, "realized_r")) },
              { label: "MFE R", value: numberText(returnedNumber(outcome, "mfe_r")) },
              { label: "MAE R", value: numberText(returnedNumber(outcome, "mae_r")) },
            ]}
          />
        ) : <p className="panel-reading">No linked Outcome record was returned.</p>}
      </div>
    </div>
  );
}

export function PaperTradesPage({ apiClient, onProvenanceChange }: PaperTradesPageProps) {
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
      const provenance = predictionProvenance(prediction);
      onProvenanceChange({
        ...provenance,
        footer: `PaperTrade ${detail.data?.prediction_id} linked to returned Prediction provenance.`,
      });
    } else {
      onProvenanceChange(EMPTY_DASHBOARD_PROVENANCE);
    }
  }, [detail.data, onProvenanceChange]);

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
        <p className="eyebrow">Local follow ledger / paper-only</p>
        <h1 id="paper-trades-title">Paper trades</h1>
        <p className="page-intro__description">Review local PaperTrade records and their linked Prediction and Outcome evidence.</p>
        <p className="page-boundary">This page never places, routes or represents a real order. It has no broker or execution connection.</p>
      </header>

      <p className="paper-only-notice paper-only-notice--wide" role="note">Permanent boundary: PaperTrade means a local record for research coverage only. No capital, brokerage, private keys or real order action are involved.</p>

      <form className="workflow-filters" aria-label="PaperTrade filters" onSubmit={applyFilters}>
        <div className="workflow-filters__grid">
          <label>Symbol<input value={draft.symbol} onChange={(event) => setDraft((current) => ({ ...current, symbol: event.target.value }))} placeholder="All symbols" /></label>
          <label>Timeframe<select value={draft.timeframe} onChange={(event) => setDraft((current) => ({ ...current, timeframe: timeframeOptions.includes(event.target.value as Timeframe) ? event.target.value as Timeframe : "" }))}><option value="">All timeframes</option>{timeframeOptions.map((value) => <option key={value}>{value}</option>)}</select></label>
          <label>Action<select value={draft.action} onChange={(event) => setDraft((current) => ({ ...current, action: actionOptions.includes(event.target.value as Action) ? event.target.value as Action : "" }))}><option value="">All actions</option>{actionOptions.map((value) => <option key={value}>{value}</option>)}</select></label>
          <label>Source type<select value={draft.source_type} onChange={(event) => setDraft((current) => ({ ...current, source_type: event.target.value === "live" || event.target.value === "replay" ? event.target.value : "" }))}><option value="">All sources</option><option value="live">live</option><option value="replay">replay</option></select></label>
          <label>PaperTrade status<input value={draft.status} onChange={(event) => setDraft((current) => ({ ...current, status: event.target.value }))} placeholder="Any status" /></label>
          <label>Outcome status<select value={draft.outcome_status} onChange={(event) => setDraft((current) => ({ ...current, outcome_status: outcomeOptions.includes(event.target.value as OutcomeStatus) ? event.target.value as OutcomeStatus : "" }))}><option value="">Any outcome</option>{outcomeOptions.map((value) => <option key={value}>{value}</option>)}</select></label>
          <label>Rows<select value={limit} onChange={(event) => setLimit(Number(event.target.value))}>{pageLimits.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
        </div>
        <button className="primary-button" type="submit">Apply filters</button>
      </form>

      <div className="workflow-grid workflow-grid--records">
        <AsyncPanel
          className="workflow-panel workflow-panel--list"
          title="PaperTrade records"
          source={requestLabel}
          freshness="followed_at shown per record"
          state={resourceState(trades, list.length === 0)}
          error={trades.error}
          onRetry={trades.retry}
          emptyMessage="No PaperTrade records matched these filters."
        >
          {list.length > 0 ? (
            <div className="prediction-ledger-wrap">
              <table className="prediction-ledger" aria-label="PaperTrade records">
                <thead><tr><th scope="col">Prediction</th><th scope="col">Action / symbol</th><th scope="col">PaperTrade status</th><th scope="col">Followed at</th><th scope="col">Outcome</th></tr></thead>
                <tbody>{list.map((trade) => <tr key={trade.prediction_id}>
                  <td><button className="record-link" type="button" onClick={() => setSelectedId(trade.prediction_id)}>{trade.prediction_id}</button></td>
                  <td><span className="signal-text">{actionLabel(trade.prediction?.action)}</span><span className="data-meta">{tradeSymbol(trade)}</span></td>
                  <td>{trade.status}</td>
                  <td>{timestampText(trade.followed_at)}</td>
                  <td>{trade.outcome_status ?? trade.outcome?.status ?? "No Outcome"}</td>
                </tr>)}</tbody>
              </table>
            </div>
          ) : null}
          <div className="pagination-controls" aria-label="PaperTrade pagination"><span>Offset {filters.offset ?? 0} · maximum {filters.limit ?? limit} rows</span><div><button className="quiet-button" type="button" disabled={(filters.offset ?? 0) === 0} onClick={() => movePage(-1)}>Previous</button><button className="quiet-button" type="button" disabled={list.length < (filters.limit ?? limit)} onClick={() => movePage(1)}>Next</button></div></div>
        </AsyncPanel>

        <AsyncPanel
          className="workflow-panel workflow-panel--detail"
          title="PaperTrade detail"
          source={selectedId ? `GET /paper-trades/${selectedId}` : "GET /paper-trades/{prediction_id}"}
          freshness="returned timestamps are shown in UTC"
          state={!selectedId ? "empty" : detail.status === "idle" || detail.status === "loading" ? "loading" : resourceState(detail, !detail.data)}
          error={detail.error}
          onRetry={detail.retry}
          emptyMessage="Select a PaperTrade record to inspect its linked evidence."
        >
          {detail.data ? <PaperTradeDetail trade={detail.data} /> : null}
        </AsyncPanel>
      </div>
    </section>
  );
}
