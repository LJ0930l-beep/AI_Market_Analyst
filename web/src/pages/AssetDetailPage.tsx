import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ApiError, type ApplicationShellApiClient } from "../api/client";
import type {
  Action,
  AnalysisResult,
  Instrument,
  InstrumentNews,
  MarketBar,
  MarketSnapshot,
  ModelStatus,
  NewsCluster,
  ProviderSnapshot,
  SignalProposal,
  TimePolicy,
} from "../api/types";
import { AsyncPanel, type PanelState } from "../components/AsyncPanel";
import { OhlcvChart } from "../components/OhlcvChart";
import type { DashboardProvenance } from "./DashboardPage";
import type { AsyncResource } from "../hooks/useAsyncResource";
import { useAsyncResource } from "../hooks/useAsyncResource";

const TIMEFRAMES = ["5m", "15m", "1h", "4h", "1d"] as const;
const DEFAULT_TIMEFRAME = "1h" as const;
const SNAPSHOT_LIMIT = 120;

type AssetTimeframe = (typeof TIMEFRAMES)[number];

export const EMPTY_ASSET_PROVENANCE: DashboardProvenance = {
  dataSource: "Asset snapshot not analyzed",
  model: "No model result supplied",
  state: "neutral",
  footer: "Run analysis explicitly to create a durable Prediction record. No PaperTrade is created here.",
};

interface AssetDetailPageProps {
  apiClient: ApplicationShellApiClient;
  onProvenanceChange: (provenance: DashboardProvenance) => void;
}

type AnalysisState =
  | { status: "idle" }
  | { status: "pending" }
  | { status: "ready"; data: AnalysisResult }
  | { status: "unavailable"; error: unknown };

function decodeRouteSymbol(value: string | undefined): string {
  if (!value) {
    return "";
  }
  try {
    return decodeURIComponent(value).trim().toUpperCase();
  } catch {
    return value.trim().toUpperCase();
  }
}

function parseTimestamp(value: unknown): Date | undefined {
  if (typeof value !== "string" || value.length === 0) {
    return undefined;
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? undefined : parsed;
}

function timestampText(value: unknown): string {
  return typeof value === "string" && value.length > 0 ? value : "Not supplied";
}

function numberText(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString(undefined, { maximumFractionDigits: 6 })
    : "Not supplied";
}

function exactNumberText(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : "Not supplied";
}

function stringText(value: unknown): string {
  return typeof value === "string" && value.length > 0 ? value : "Not supplied";
}

function booleanText(value: unknown): string {
  return typeof value === "boolean" ? String(value) : "Not supplied";
}

function safeExternalUrl(value: unknown): string | undefined {
  if (typeof value !== "string" || value.length === 0) {
    return undefined;
  }
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? value : undefined;
  } catch {
    return undefined;
  }
}

function resourcePanelState<T>(resource: AsyncResource<T>, empty: boolean, degraded = false): PanelState {
  if (resource.status === "loading") {
    return "loading";
  }
  if (resource.status === "unavailable") {
    return "unavailable";
  }
  if (resource.status === "idle" || empty) {
    return "empty";
  }
  return degraded ? "degraded" : "ready";
}

function fieldEntries(record: Record<string, unknown>, fields: Array<[string, string]>): Array<[string, unknown]> {
  return fields
    .map(([label, key]): [string, unknown] => [label, record[key]])
    .filter(([, value]) => value !== undefined);
}

function DataFacts({ entries }: { entries: Array<[string, string]> }) {
  return (
    <dl className="fact-list asset-facts">
      {entries.map(([label, value]) => (
        <div className="fact-list__row" key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function isMarketBar(value: unknown): value is MarketBar {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.timestamp === "string" &&
    ["open", "high", "low", "close", "volume"].every(
      (key) => typeof candidate[key] === "number" && Number.isFinite(candidate[key]),
    )
  );
}

function returnedBars(snapshot: MarketSnapshot | undefined): MarketBar[] {
  return Array.isArray(snapshot?.bars) ? snapshot.bars.filter(isMarketBar) : [];
}

function snapshotFreshness(snapshot: MarketSnapshot | undefined): string {
  return snapshot?.data_as_of ? `data_as_of ${snapshot.data_as_of}` : "data_as_of not supplied";
}

function SnapshotEvidence({ snapshot }: { snapshot: MarketSnapshot }) {
  const quote = snapshot.quote;
  const quant = snapshot.quant;
  const provider = snapshot.provider_snapshot;
  const bars = returnedBars(snapshot);
  const quoteEntries = quote
    ? fieldEntries(quote, [
        ["Price", "price"],
        ["Change %", "change_pct"],
        ["High", "high"],
        ["Low", "low"],
        ["Quote timestamp", "timestamp"],
      ]).map(([label, value]): [string, string] => [label, label.endsWith("timestamp") ? timestampText(value) : numberText(value)])
    : [];
  const quantEntries = quant
    ? fieldEntries(quant, [
        ["Price", "price"],
        ["EMA 20", "ema20"],
        ["EMA 50", "ema50"],
        ["RSI 14", "rsi14"],
        ["MACD", "macd"],
        ["MACD signal", "macd_signal"],
        ["ATR 14", "atr14"],
        ["Volume ratio", "volume_ratio"],
        ["Support", "support"],
        ["Resistance", "resistance"],
        ["Market regime", "market_regime"],
        ["Trend score", "trend_score"],
        ["Momentum score", "momentum_score"],
      ]).map(([label, value]): [string, string] => [
        label,
        typeof value === "string" ? value : numberText(value),
      ])
    : [];
  const provenanceEntries = provider
    ? fieldEntries(provider, [
        ["Provider", "provider"],
        ["Fetched at", "fetched_at"],
        ["Provider data_as_of", "data_as_of"],
        ["Stale", "stale"],
        ["Error code", "error_code"],
      ]).map(([label, value]): [string, string] => [
        label,
        label === "Stale" ? booleanText(value) : label.includes("at") || label.includes("as_of") ? timestampText(value) : stringText(value),
      ])
    : [];

  return (
    <div className="asset-evidence-stack">
      <div className="asset-fact-columns">
        <section aria-labelledby="quote-facts-title">
          <h3 id="quote-facts-title" className="subsection-label">
            Quote
          </h3>
          {quoteEntries.length > 0 ? <DataFacts entries={quoteEntries} /> : <p className="panel-reading">Quote values were not supplied.</p>}
        </section>
        <section aria-labelledby="quant-facts-title">
          <h3 id="quant-facts-title" className="subsection-label">
            Deterministic quant facts
          </h3>
          {quantEntries.length > 0 ? <DataFacts entries={quantEntries} /> : <p className="panel-reading">Quant values were not supplied.</p>}
        </section>
      </div>
      <section className="asset-provenance-facts" aria-labelledby="snapshot-provenance-title">
        <h3 id="snapshot-provenance-title" className="subsection-label">
          Market data provenance
        </h3>
        <DataFacts
          entries={[
            ["Symbol", snapshot.symbol],
            ["Timeframe", snapshot.timeframe],
            ["Response time", timestampText(snapshot.response_time)],
            ["Data as of", timestampText(snapshot.data_as_of)],
            ...(provenanceEntries.length > 0 ? provenanceEntries : [["Provider snapshot", "Not supplied"] as [string, string]]),
          ]}
        />
      </section>
      <section aria-labelledby="ohlcv-title">
        <h3 id="ohlcv-title" className="subsection-label">
          OHLCV evidence
        </h3>
        <OhlcvChart bars={bars} symbol={snapshot.symbol} timeframe={snapshot.timeframe} />
      </section>
    </div>
  );
}

function NewsEvidence({ news }: { news: InstrumentNews }) {
  const events = Array.isArray(news.events) ? news.events : [];
  const clusters = Array.isArray(news.clusters) ? news.clusters : [];
  return (
    <div className="news-evidence">
      <DataFacts
        entries={[
          ["Provider", stringText(news.provider)],
          ["Available", booleanText(news.available)],
          ["Fetched at", timestampText(news.fetched_at)],
          ["Error code", stringText(news.error_code)],
        ]}
      />
      {news.available === false ? (
        <p className="panel-degraded-note" role="status">
          News provider is unavailable for this request; no event evidence is claimed.
        </p>
      ) : events.length === 0 ? (
        <p className="panel-reading" role="status">
          News provider responded, but no events were supplied.
        </p>
      ) : (
        <ol className="news-event-list">
          {events.map((event, index) => (
            <li className="news-event" key={event.id ?? `${event.title ?? "event"}-${index}`}>
              <div className="news-event__heading">
                <h3>{stringText(event.title)}</h3>
                {safeExternalUrl(event.url) ? (
                  <a href={safeExternalUrl(event.url)} target="_blank" rel="noreferrer">
                    Open source
                  </a>
                ) : null}
              </div>
              <DataFacts
                entries={[
                  ["Source", stringText(event.source)],
                  ["Published", timestampText(event.published_at)],
                  ["Category", stringText(event.category)],
                  ["Sentiment", numberText(event.sentiment)],
                  ["Importance", numberText(event.importance)],
                  ["Credibility", numberText(event.credibility)],
                  ["Impact horizon", stringText(event.impact_horizon)],
                ]}
              />
              {event.summary_raw ? <p className="news-event__summary">{event.summary_raw}</p> : null}
            </li>
          ))}
        </ol>
      )}
      {clusters.length > 0 ? <NewsClusters clusters={clusters} /> : null}
    </div>
  );
}

function NewsClusters({ clusters }: { clusters: NewsCluster[] }) {
  return (
    <section className="news-clusters" aria-labelledby="news-clusters-title">
      <h3 id="news-clusters-title" className="subsection-label">
        Returned event clusters
      </h3>
      <ul className="news-cluster-list">
        {clusters.map((cluster, index) => (
          <li key={cluster.cluster_id ?? `${cluster.title ?? "cluster"}-${index}`}>
            <strong>{stringText(cluster.title)}</strong>
            <span>
              {numberText(cluster.importance)} importance · {numberText(cluster.sentiment)} sentiment
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function actionText(action: Action | undefined): string {
  if (action === "LONG" || action === "SHORT") {
    return `${action} · actionable proposal`;
  }
  if (action === "WAIT") {
    return "WAIT · saved coverage result";
  }
  return "Action not supplied";
}

function modelText(signal: SignalProposal, model: ModelStatus | undefined): string {
  if (signal.model_id) {
    return signal.model_id;
  }
  if (model?.model_id) {
    return model.model_id;
  }
  if (model?.provider) {
    return model.provider;
  }
  return "Not supplied";
}

function SignalCard({ result }: { result: AnalysisResult }) {
  const signal = result.signal;
  const model = result.model;
  if (!signal) {
    return <p className="panel-degraded-note">The analysis response did not supply a Signal Proposal.</p>;
  }
  const actionable = signal.action === "LONG" || signal.action === "SHORT";
  const levels: Array<[string, string]> = [
    ["Entry low", numberText(signal.entry_low)],
    ["Entry high", numberText(signal.entry_high)],
    ["Stop", numberText(signal.stop)],
    ["TP1", numberText(signal.tp1)],
    ["TP2", numberText(signal.tp2)],
  ];
  const signalFacts: Array<[string, string]> = [
    ["Prediction id", stringText(signal.prediction_id)],
    ["Source type", stringText(signal.source_type)],
    ["Analysis timeframe", stringText(signal.analysis_timeframe ?? result.timeframe)],
    ["Model id", modelText(signal, model)],
    ["Model version", stringText(signal.model_version ?? model?.model_version)],
    ["Prompt version", stringText(signal.prompt_version ?? model?.prompt_version)],
    ["Parse status", stringText(signal.parse_status)],
    ["Raw confidence", exactNumberText(signal.raw_confidence)],
    ["Calibrated confidence", numberText(signal.calibrated_confidence)],
    ["Generated at", timestampText(signal.generated_at)],
    ["Response time", timestampText(result.response_time)],
    ["Data as of", timestampText(result.data_as_of ?? signal.data_as_of)],
    ["Signal valid until", timestampText(signal.signal_valid_until)],
    ["Expected hold until", timestampText(signal.expected_hold_until)],
    ["Max hold until", timestampText(signal.max_hold_until)],
    ["Re-evaluate at", timestampText(signal.reevaluate_at)],
    ["Signal validity minutes", numberText(signal.signal_validity_minutes)],
    ["Expected hold minutes", numberText(signal.expected_hold_minutes)],
    ["Max hold minutes", numberText(signal.max_hold_minutes)],
  ];
  const timePolicy = result.time_policy;
  const invalidation = Array.isArray(signal.invalidation) ? signal.invalidation : [];
  const reasonCodes = Array.isArray(signal.reason_codes) ? signal.reason_codes : [];
  return (
    <article className="signal-card" aria-labelledby="signal-card-title">
      <header className="signal-card__header">
        <div>
          <p className="eyebrow">Persisted result / POST analysis</p>
          <h3 id="signal-card-title">Signal proposal</h3>
        </div>
        <span className={`signal-action signal-action--${signal.action?.toLowerCase() ?? "unknown"}`}>
          {actionText(signal.action)}
        </span>
      </header>
      <p className="signal-card__summary">{stringText(signal.summary)}</p>
      <DataFacts entries={signalFacts} />
      <section className="signal-card__section" aria-labelledby="model-status-title">
        <h4 id="model-status-title" className="subsection-label">
          Model status returned with analysis
        </h4>
        <DataFacts
          entries={[
            ["Provider", stringText(model?.provider)],
            ["Available", booleanText(model?.available)],
            ["Error code", stringText(model?.error_code)],
          ]}
        />
      </section>
      <section className="signal-card__section" aria-labelledby="signal-reasons-title">
        <h4 id="signal-reasons-title" className="subsection-label">
          Reason codes
        </h4>
        {reasonCodes.length > 0 ? (
          <ul className="tag-list">
            {reasonCodes.map((reason) => <li key={reason}>{reason}</li>)}
          </ul>
        ) : (
          <p className="panel-reading">Reason codes were not supplied.</p>
        )}
      </section>
      <section className="signal-card__section" aria-labelledby="signal-levels-title">
        <h4 id="signal-levels-title" className="subsection-label">
          Price levels
        </h4>
        {actionable ? (
          <DataFacts entries={levels} />
        ) : (
          <p className="panel-reading">Entry, stop and targets are not applicable to a WAIT result.</p>
        )}
      </section>
      <section className="signal-card__section" aria-labelledby="signal-invalidation-title">
        <h4 id="signal-invalidation-title" className="subsection-label">
          Invalidation conditions
        </h4>
        {invalidation.length > 0 ? (
          <ul className="plain-list">
            {invalidation.map((condition) => <li key={condition}>{condition}</li>)}
          </ul>
        ) : (
          <p className="panel-reading">Invalidation conditions were not supplied.</p>
        )}
      </section>
      <section className="signal-card__section" aria-labelledby="time-policy-title">
        <h4 id="time-policy-title" className="subsection-label">
          Time policy
        </h4>
        {timePolicy ? <TimePolicyFacts policy={timePolicy} /> : <p className="panel-reading">Time policy was not supplied.</p>}
      </section>
      <p className="signal-card__boundary">
        This is a saved Prediction result. It does not create a PaperTrade, order or broker action.
      </p>
    </article>
  );
}

function TimePolicyFacts({ policy }: { policy: TimePolicy }) {
  const rangeText = (value: unknown): string =>
    Array.isArray(value) ? value.filter((item) => typeof item === "number").join(" – ") || "Not supplied" : "Not supplied";
  return (
    <DataFacts
      entries={[
        ["Timeframe", stringText(policy.timeframe)],
        ["Signal validity range", rangeText(policy.signal_validity_minutes)],
        ["Holding horizon range", rangeText(policy.holding_horizon_minutes)],
        ["Re-evaluate range", rangeText(policy.reevaluate_minutes)],
        ["Volatility ratio", numberText(policy.volatility_ratio)],
        ["Market regime", stringText(policy.market_regime)],
        ["Event risk", booleanText(policy.event_risk)],
      ]}
    />
  );
}

function provenanceForAnalysis(result: AnalysisResult): DashboardProvenance {
  const signal = result.signal;
  if (!signal) {
    return EMPTY_ASSET_PROVENANCE;
  }
  const expiry = parseTimestamp(signal.signal_valid_until);
  const provider = result.provider_snapshot?.provider;
  const source = signal.source_type;
  const dataSource = [source ? `source_type: ${source}` : undefined, provider ? `provider: ${provider}` : undefined]
    .filter((value): value is string => Boolean(value))
    .join(" · ") || "Data source not supplied";
  const model = modelText(signal, result.model);
  return {
    generatedAt: signal.generated_at,
    reevaluateAt: signal.reevaluate_at,
    expiresAt: signal.signal_valid_until,
    dataSource,
    model: model === "Not supplied" ? "Model not supplied" : `model_id: ${model}`,
    state: expiry ? (expiry.getTime() <= Date.now() ? "expired" : "active") : "neutral",
    footer: signal.prediction_id
      ? `Prediction ${signal.prediction_id} returned by explicit analysis. No PaperTrade was created.`
      : "Explicit analysis returned without a prediction id.",
  };
}

function AnalysisPanel({
  state,
  onRun,
}: {
  state: AnalysisState;
  onRun: () => void;
}) {
  const requestState =
    state.status === "idle"
      ? "not started"
      : state.status === "pending"
        ? "pending"
        : state.status === "ready"
          ? "complete"
          : "failed";
  return (
    <section className={`async-panel analysis-panel analysis-panel--${state.status}`} aria-labelledby="analysis-panel-title">
      <header className="async-panel__header">
        <div className="async-panel__heading">
          <h2 id="analysis-panel-title">Explicit analysis</h2>
          <p className="async-panel__source">
            Source <code>POST /analysis/:symbol</code> · Request: {requestState} · Creates one Prediction
          </p>
        </div>
        <span className={`panel-state panel-state--${state.status === "unavailable" ? "unavailable" : state.status === "pending" ? "loading" : state.status === "ready" ? "ready" : "empty"}`}>
          {state.status === "pending" ? "Running" : state.status === "ready" ? "Result" : state.status === "unavailable" ? "Unavailable" : "Not run"}
        </span>
      </header>
      <div className="async-panel__body">
        <p className="panel-reading">
          Run this explicit request to persist exactly one Prediction. WAIT is retained as a saved result; no PaperTrade, order or broker action is created.
        </p>
        {state.status === "pending" ? <p className="analysis-status" role="status">Analysis request in progress.</p> : null}
        {state.status === "unavailable" ? (
          <div className="panel-message panel-message--unavailable" role="alert">
            <p>Analysis was not completed. The existing snapshot and news evidence remain separate.</p>
            <p className="panel-message__error">{safeErrorText(state.error)}</p>
          </div>
        ) : null}
        {state.status === "ready" ? <SignalCard result={state.data} /> : null}
        <button className="primary-button" type="button" onClick={onRun} disabled={state.status === "pending"}>
          {state.status === "pending" ? "Running analysis…" : "Run analysis"}
        </button>
      </div>
    </section>
  );
}

function safeErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.code}: ${error.message}`;
  }
  return "The analysis endpoint did not return a usable response.";
}

function InstrumentSelector({
  instruments,
  selectedSymbol,
  onSelect,
}: {
  instruments: Instrument[];
  selectedSymbol: string;
  onSelect: (symbol: string) => void;
}) {
  return (
    <div className="asset-selector">
      <label htmlFor="asset-symbol">Instrument</label>
      <select id="asset-symbol" value={selectedSymbol} onChange={(event) => onSelect(event.target.value)}>
        {instruments.map((instrument) => (
          <option key={instrument.symbol} value={instrument.symbol}>
            {instrument.symbol} · {instrument.exchange}
          </option>
        ))}
      </select>
      <p className="data-meta">Source: GET /instruments · backend roster</p>
    </div>
  );
}

export function AssetDetailPage({ apiClient, onProvenanceChange }: AssetDetailPageProps) {
  const { symbol: routeParam } = useParams<{ symbol: string }>();
  const navigate = useNavigate();
  const requestedSymbol = decodeRouteSymbol(routeParam);
  const [timeframe, setTimeframe] = useState<AssetTimeframe>(DEFAULT_TIMEFRAME);
  const [analysisState, setAnalysisState] = useState<AnalysisState>({ status: "idle" });
  const analysisController = useRef<AbortController | null>(null);

  const instrumentsLoader = useCallback((signal: AbortSignal) => apiClient.instruments(signal), [apiClient]);
  const instruments = useAsyncResource(instrumentsLoader);
  const selectedInstrument = useMemo(
    () => instruments.data?.find((instrument) => instrument.symbol.toUpperCase() === requestedSymbol),
    [instruments.data, requestedSymbol],
  );
  const canonicalSymbol = selectedInstrument?.symbol ?? requestedSymbol;

  const snapshotLoader = useCallback(
    (signal: AbortSignal) =>
      apiClient.instrumentSnapshot(canonicalSymbol, { timeframe, limit: SNAPSHOT_LIMIT }, signal),
    [apiClient, canonicalSymbol, timeframe],
  );
  const newsLoader = useCallback(
    (signal: AbortSignal) => apiClient.instrumentNews(canonicalSymbol, signal),
    [apiClient, canonicalSymbol],
  );
  const snapshot = useAsyncResource(snapshotLoader, Boolean(selectedInstrument));
  const news = useAsyncResource(newsLoader, Boolean(selectedInstrument));

  useEffect(() => {
    if (selectedInstrument && selectedInstrument.symbol !== requestedSymbol) {
      navigate(`/assets/${encodeURIComponent(selectedInstrument.symbol)}`, { replace: true });
    }
  }, [navigate, requestedSymbol, selectedInstrument]);

  useEffect(() => {
    analysisController.current?.abort();
    analysisController.current = null;
    setAnalysisState({ status: "idle" });
    onProvenanceChange(EMPTY_ASSET_PROVENANCE);
  }, [onProvenanceChange, requestedSymbol, timeframe]);

  useEffect(() => () => analysisController.current?.abort(), []);

  const runAnalysis = useCallback(() => {
    if (!selectedInstrument || analysisState.status === "pending") {
      return;
    }
    analysisController.current?.abort();
    const controller = new AbortController();
    analysisController.current = controller;
    setAnalysisState({ status: "pending" });
    void apiClient
      .analysis(canonicalSymbol, { timeframe, limit: SNAPSHOT_LIMIT }, controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) {
          setAnalysisState({ status: "ready", data: result });
          onProvenanceChange(provenanceForAnalysis(result));
        }
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted && !(error instanceof Error && error.name === "AbortError")) {
          setAnalysisState({ status: "unavailable", error });
        }
      });
  }, [analysisState.status, apiClient, canonicalSymbol, onProvenanceChange, selectedInstrument, timeframe]);

  const snapshotData = snapshot.data;
  const snapshotProvider: ProviderSnapshot | undefined = snapshotData?.provider_snapshot;
  const snapshotDegraded = Boolean(snapshotProvider?.stale || snapshotProvider?.error_code);
  const newsData = news.data;
  const newsEmpty = Boolean(newsData && newsData.available !== false && (newsData.events?.length ?? 0) === 0);
  const newsDegraded = newsData?.available === false;

  const handleInstrumentSelect = (symbol: string) => {
    navigate(`/assets/${encodeURIComponent(symbol)}`);
  };

  return (
    <section className="asset-page" aria-labelledby="asset-detail-title">
      <header className="page-intro asset-page__intro">
        <p className="eyebrow">Asset evidence / read-only snapshot</p>
        <div className="asset-page__title-row">
          <div>
            <h1 id="asset-detail-title">Asset detail / {requestedSymbol || "instrument"}</h1>
            <p className="page-intro__description">
              Returned market evidence, provider provenance and an explicit analysis workflow for one instrument.
            </p>
          </div>
          {instruments.status === "ready" && instruments.data && selectedInstrument ? (
            <InstrumentSelector instruments={instruments.data} onSelect={handleInstrumentSelect} selectedSymbol={selectedInstrument.symbol} />
          ) : null}
        </div>
        <p className="page-boundary">GET snapshot and GET news are read-only. Only Run analysis calls POST /analysis and creates a Prediction.</p>
      </header>

      {instruments.status === "loading" ? (
        <div className="asset-roster-state" role="status">Loading instrument roster from GET /instruments.</div>
      ) : null}
      {instruments.status === "unavailable" ? (
        <div className="asset-roster-state asset-roster-state--error" role="alert">
          <p>Instrument roster is unavailable; the requested symbol was not substituted.</p>
          <button className="quiet-button" type="button" onClick={instruments.retry}>Retry instrument roster</button>
        </div>
      ) : null}
      {instruments.status === "ready" && !selectedInstrument ? (
        <div className="asset-roster-state asset-roster-state--error" role="alert">
          <p>{requestedSymbol || "This route"} is not present in the backend instrument roster.</p>
          <p className="panel-reading">Choose an instrument from the roster; no default symbol was selected.</p>
          <ul className="asset-roster-links">
            {(instruments.data ?? []).map((instrument) => (
              <li key={instrument.symbol}>
                <Link to={`/assets/${encodeURIComponent(instrument.symbol)}`}>{instrument.symbol}</Link>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {selectedInstrument ? (
        <>
          <div className="asset-context-row">
            <div>
              <p className="subsection-label">Instrument context</p>
              <p className="asset-context-row__title">{selectedInstrument.symbol} · {selectedInstrument.exchange}</p>
              <p className="data-meta">{selectedInstrument.asset_type} · {selectedInstrument.sector || "Sector not supplied"} · {selectedInstrument.currency}</p>
            </div>
            <div className="timeframe-control">
              <span className="subsection-label" id="timeframe-label">Snapshot timeframe</span>
              <div className="timeframe-options" role="group" aria-labelledby="timeframe-label">
                {TIMEFRAMES.map((value) => (
                  <button
                    className={`timeframe-button${value === timeframe ? " timeframe-button--active" : ""}`}
                    type="button"
                    key={value}
                    aria-pressed={value === timeframe}
                    onClick={() => setTimeframe(value)}
                  >
                    {value}
                  </button>
                ))}
              </div>
            </div>
          </div>

          <div className="asset-grid">
            <AsyncPanel
              title="Market snapshot"
              source={`GET /instruments/${canonicalSymbol}/snapshot?timeframe=${timeframe}&limit=${SNAPSHOT_LIMIT}`}
              freshness={snapshotFreshness(snapshotData)}
              state={resourcePanelState(snapshot, !snapshotData, snapshotDegraded)}
              error={snapshot.error}
              onRetry={snapshot.retry}
              emptyMessage="The snapshot response supplied no market evidence."
              degradedMessage="Provider provenance is stale or carries an error code; returned facts remain labeled as supplied."
              className="asset-panel asset-panel--wide"
            >
              {snapshotData ? <SnapshotEvidence snapshot={snapshotData} /> : null}
            </AsyncPanel>

            <AsyncPanel
              title="News evidence"
              source={`GET /instruments/${canonicalSymbol}/news`}
              freshness={newsData?.fetched_at ? `fetched_at ${newsData.fetched_at}` : "fetched_at not supplied"}
              state={resourcePanelState(news, !newsData || newsEmpty, newsDegraded)}
              error={news.error}
              onRetry={news.retry}
              emptyMessage="The news provider returned no events for this request."
              degradedMessage="News provider availability is separate from the market snapshot and is not inferred from it."
              className="asset-panel"
            >
              {newsData ? <NewsEvidence news={newsData} /> : null}
            </AsyncPanel>

            <AnalysisPanel state={analysisState} onRun={runAnalysis} />
          </div>
        </>
      ) : null}
    </section>
  );
}
