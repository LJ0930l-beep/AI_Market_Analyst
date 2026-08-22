import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ApiError, type ApplicationShellApiClient } from "../api/client";
import { useI18n } from "../i18n";
import type {
  Action,
  AnalysisResult,
  Instrument,
  MarketContextResponse,
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
import { useResearchFormatters } from "../components/ResearchFacts";
import type { DashboardProvenance } from "./DashboardPage";
import type { AsyncResource } from "../hooks/useAsyncResource";
import { useAsyncResource } from "../hooks/useAsyncResource";
import { sourceTypeLabel } from "./workflowUtils";

const TIMEFRAMES = ["5m", "15m", "1h", "4h", "1d"] as const;
const DEFAULT_TIMEFRAME = "1h" as const;
const SNAPSHOT_LIMIT = 120;

type AssetTimeframe = (typeof TIMEFRAMES)[number];

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

function SnapshotEvidence({ snapshot }: { snapshot: MarketSnapshot }) {
  const { t, text } = useI18n();
  const { booleanText, numberText, primitiveText: stringText, timestampText } = useResearchFormatters();
  const quote = snapshot.quote;
  const quant = snapshot.quant;
  const provider = snapshot.provider_snapshot;
  const bars = returnedBars(snapshot);
  const quoteEntries = quote
    ? fieldEntries(quote, [
        [t("asset.price"), "price"],
        [t("asset.changePercent"), "change_pct"],
        [t("asset.high"), "high"],
        [t("asset.low"), "low"],
        [t("asset.quoteTimestamp"), "timestamp"],
      ]).map(([label, value]): [string, string] => [label, label === t("asset.quoteTimestamp") ? timestampText(value) : numberText(value)])
    : [];
  const quantEntries = quant
    ? fieldEntries(quant, [
        [t("asset.price"), "price"],
        ["EMA 20", "ema20"],
        ["EMA 50", "ema50"],
        ["RSI 14", "rsi14"],
        ["MACD", "macd"],
        [t("asset.macdSignal"), "macd_signal"],
        ["ATR 14", "atr14"],
        [t("asset.volumeRatio"), "volume_ratio"],
        [t("asset.support"), "support"],
        [t("asset.resistance"), "resistance"],
        [t("asset.marketRegime"), "market_regime"],
        [t("asset.trendScore"), "trend_score"],
        [t("asset.momentumScore"), "momentum_score"],
      ]).map(([label, value]): [string, string] => [
        label,
        typeof value === "string" ? label === t("asset.marketRegime") ? text(value) : value : numberText(value),
      ])
    : [];
  const provenanceEntries = provider
    ? fieldEntries(provider, [
        [t("common.provider"), "provider"],
        [t("asset.fetchedAt"), "fetched_at"],
        [t("asset.providerDataAsOf"), "data_as_of"],
        [t("common.stale"), "stale"],
        [t("common.errorCode"), "error_code"],
      ]).map(([label, value]): [string, string] => [
        label,
        label === t("common.stale") ? booleanText(value) : label === t("asset.fetchedAt") || label === t("asset.providerDataAsOf") ? timestampText(value) : stringText(value),
      ])
    : [];

  return (
    <div className="asset-evidence-stack">
      <div className="asset-fact-columns">
        <section aria-labelledby="quote-facts-title">
          <h3 id="quote-facts-title" className="subsection-label">
            {t("asset.quote")}
          </h3>
          {quoteEntries.length > 0 ? <DataFacts entries={quoteEntries} /> : <p className="panel-reading">{t("asset.quoteMissing")}</p>}
        </section>
        <section aria-labelledby="quant-facts-title">
          <h3 id="quant-facts-title" className="subsection-label">
            {t("asset.quant")}
          </h3>
          {quantEntries.length > 0 ? <DataFacts entries={quantEntries} /> : <p className="panel-reading">{t("asset.quantMissing")}</p>}
        </section>
      </div>
      <section className="asset-provenance-facts" aria-labelledby="snapshot-provenance-title">
        <h3 id="snapshot-provenance-title" className="subsection-label">
          {t("asset.marketProvenance")}
        </h3>
        <DataFacts
          entries={[
            [t("common.symbol"), snapshot.symbol],
            [t("common.timeframe"), snapshot.timeframe],
            [t("asset.responseTime"), timestampText(snapshot.response_time)],
            [t("common.dataAsOf"), timestampText(snapshot.data_as_of)],
            ...(provenanceEntries.length > 0 ? provenanceEntries : [[t("asset.providerSnapshot"), t("common.notSupplied")] as [string, string]]),
          ]}
        />
      </section>
      <section aria-labelledby="ohlcv-title">
        <h3 id="ohlcv-title" className="subsection-label">
          {t("asset.ohlcv")}
        </h3>
        <OhlcvChart bars={bars} symbol={snapshot.symbol} timeframe={snapshot.timeframe} />
      </section>
    </div>
  );
}

function NewsEvidence({ news }: { news: InstrumentNews }) {
  const { t, text } = useI18n();
  const { booleanText, numberText, primitiveText: stringText, timestampText } = useResearchFormatters();
  const events = Array.isArray(news.events) ? news.events : [];
  const clusters = Array.isArray(news.clusters) ? news.clusters : [];
  return (
    <div className="news-evidence">
      <DataFacts
        entries={[
          [t("common.provider"), stringText(news.provider)],
          [t("common.available"), booleanText(news.available)],
          [t("asset.fetchedAt"), timestampText(news.fetched_at)],
          [t("common.errorCode"), stringText(news.error_code)],
        ]}
      />
      {news.available === false ? (
        <p className="panel-degraded-note" role="status">
          {t("asset.newsUnavailable")}
        </p>
      ) : events.length === 0 ? (
        <p className="panel-reading" role="status">
          {t("asset.newsNoEvents")}
        </p>
      ) : (
        <ol className="news-event-list">
          {events.map((event, index) => (
            <li className="news-event" key={event.id ?? `${event.title ?? "event"}-${index}`}>
              <div className="news-event__heading">
                <h3>{stringText(event.title)}</h3>
                {safeExternalUrl(event.url) ? (
                  <a href={safeExternalUrl(event.url)} target="_blank" rel="noreferrer">
                    {t("asset.openSource")}
                  </a>
                ) : null}
              </div>
              <DataFacts
                entries={[
                  [t("common.source"), stringText(event.source)],
                  [t("asset.published"), timestampText(event.published_at)],
                  [t("common.category"), text(stringText(event.category))],
                  [t("asset.sentiment"), numberText(event.sentiment)],
                  [t("asset.importance"), numberText(event.importance)],
                  [t("asset.credibility"), numberText(event.credibility)],
                  [t("asset.impactHorizon"), stringText(event.impact_horizon)],
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
  const { t } = useI18n();
  const { numberText, primitiveText: stringText } = useResearchFormatters();
  return (
    <section className="news-clusters" aria-labelledby="news-clusters-title">
      <h3 id="news-clusters-title" className="subsection-label">
        {t("asset.returnedClusters")}
      </h3>
      <ul className="news-cluster-list">
        {clusters.map((cluster, index) => (
          <li key={cluster.cluster_id ?? `${cluster.title ?? "cluster"}-${index}`}>
            <strong>{stringText(cluster.title)}</strong>
            <span>
              {t("asset.importance")} {numberText(cluster.importance)} · {t("asset.sentiment")} {numberText(cluster.sentiment)}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function ContextEvidence({ context }: { context: MarketContextResponse }) {
  const { t, text } = useI18n();
  const { booleanText, numberText, primitiveText: stringText, timestampText } = useResearchFormatters();
  const benchmark = context.benchmark_context;
  const events = context.events;
  const memory = context.market_memory;
  const clusters = Array.isArray(events?.clusters) ? events.clusters : [];
  return (
    <div className="news-evidence">
      <section aria-labelledby="benchmark-context-title">
        <h3 id="benchmark-context-title" className="subsection-label">{t("asset.benchmark")}</h3>
        <DataFacts
          entries={[
            [t("common.status"), text(stringText(benchmark?.status))],
            [t("asset.mapping"), stringText(benchmark?.benchmark?.benchmark_symbol)],
            [t("asset.mappingVersion"), stringText(benchmark?.benchmark?.mapping_version)],
            [t("common.provider"), stringText(benchmark?.provider)],
            [t("asset.relativePerformance"), numberText(benchmark?.relative_performance)],
            [t("asset.relativeStrength"), numberText(benchmark?.relative_strength)],
            [t("v11.asOf"), timestampText(benchmark?.as_of)],
          ]}
        />
        {benchmark?.status !== "available" ? <p className="panel-degraded-note" role="status">{t("asset.benchmarkUnavailable")}</p> : null}
      </section>
      <section aria-labelledby="event-context-title">
        <h3 id="event-context-title" className="subsection-label">{t("asset.events")}</h3>
        <DataFacts
          entries={[
            [t("common.provider"), stringText(events?.provider)],
            [t("common.available"), booleanText(events?.available)],
            [t("asset.eventSchema"), stringText(events?.schema_version)],
            [t("asset.clusters"), numberText(clusters.length)],
            [t("v11.asOf"), timestampText(events?.as_of)],
          ]}
        />
        {events?.available === false ? <p className="panel-degraded-note" role="status">{t("asset.eventsUnavailable")}</p> : null}
        {clusters.length > 0 ? (
          <ul className="news-cluster-list">
            {clusters.slice(0, 3).map((cluster, index) => (
              <li key={cluster.cluster_id ?? `${cluster.title ?? "cluster"}-${index}`}>
                <strong>{stringText(cluster.title)}</strong>
                <span>{text(stringText(cluster.consensus))} · {numberText(cluster.source_count)} {t("asset.sources")} · {t("asset.importance")} {numberText(cluster.importance)}</span>
              </li>
            ))}
          </ul>
        ) : <p className="panel-reading">{t("asset.noEventCluster")}</p>}
      </section>
      <section aria-labelledby="memory-context-title">
        <h3 id="memory-context-title" className="subsection-label">{t("asset.memory")}</h3>
        <DataFacts
          entries={[
            [t("common.status"), text(stringText(memory?.status))],
            [t("asset.memoryVersion"), stringText(memory?.version)],
            [t("asset.featureVersion"), stringText(memory?.feature_version)],
            [t("asset.eligibleSamples"), numberText(memory?.eligible_sample_count)],
            [t("asset.resolvedSamples"), numberText(memory?.resolved_sample_count)],
            [t("asset.similarRecords"), numberText(memory?.similar_count)],
            [t("common.winRate"), numberText(memory?.win_rate)],
            [t("v11.asOf"), timestampText(memory?.as_of)],
          ]}
        />
        <p className="panel-reading">{t("asset.memoryNote")}</p>
      </section>
      <p className="panel-boundary">{t("asset.contextReadOnly")}</p>
    </div>
  );
}

function actionText(action: Action | undefined, t: ReturnType<typeof useI18n>["t"]): string {
  if (action === "LONG" || action === "SHORT") {
    return `${action} · ${t("common.actionableProposal")}`;
  }
  if (action === "WAIT") {
    return `WAIT · ${t("asset.savedCoverageResult")}`;
  }
  return t("common.actionNotSupplied");
}

function modelText(signal: SignalProposal, model: ModelStatus | undefined, fallback: string): string {
  if (signal.model_id) {
    return signal.model_id;
  }
  if (model?.model_id) {
    return model.model_id;
  }
  if (model?.provider) {
    return model.provider;
  }
  return fallback;
}

function SignalCard({ result }: { result: AnalysisResult }) {
  const { t } = useI18n();
  const { booleanText, exactNumberText, numberText, primitiveText: stringText, timestampText } = useResearchFormatters();
  const signal = result.signal;
  const model = result.model;
  if (!signal) {
    return <p className="panel-degraded-note">{t("asset.noSignal")}</p>;
  }
  const actionable = signal.action === "LONG" || signal.action === "SHORT";
  const levels: Array<[string, string]> = [
    [t("common.entryLow"), numberText(signal.entry_low)],
    [t("common.entryHigh"), numberText(signal.entry_high)],
    [t("common.stop"), numberText(signal.stop)],
    [t("common.statusTp1"), numberText(signal.tp1)],
    [t("common.statusTp2"), numberText(signal.tp2)],
  ];
  const signalFacts: Array<[string, string]> = [
    [t("common.predictionId"), stringText(signal.prediction_id)],
    [t("common.sourceType"), signal.source_type ? sourceTypeLabel(signal.source_type, t) : stringText(signal.source_type)],
    [t("asset.analysisTimeframe"), stringText(signal.analysis_timeframe ?? result.timeframe)],
    [t("common.modelId"), modelText(signal, model, t("common.notSupplied"))],
    [t("common.modelVersion"), stringText(signal.model_version ?? model?.model_version)],
    [t("common.promptVersion"), stringText(signal.prompt_version ?? model?.prompt_version)],
    [t("common.parseStatus"), stringText(signal.parse_status)],
    [t("common.rawConfidence"), exactNumberText(signal.raw_confidence)],
    [t("common.calibratedConfidence"), numberText(signal.calibrated_confidence)],
    [t("asset.generatedAt"), timestampText(signal.generated_at)],
    [t("asset.responseTime"), timestampText(result.response_time)],
    [t("common.dataAsOf"), timestampText(result.data_as_of ?? signal.data_as_of)],
    [t("asset.signalValidUntil"), timestampText(signal.signal_valid_until)],
    [t("common.expectedHoldUntil"), timestampText(signal.expected_hold_until)],
    [t("common.maximumHoldUntil"), timestampText(signal.max_hold_until)],
    [t("asset.reevaluateAt"), timestampText(signal.reevaluate_at)],
    [t("common.validityMinutes"), numberText(signal.signal_validity_minutes)],
    [t("common.expectedHoldMinutes"), numberText(signal.expected_hold_minutes)],
    [t("common.maximumHoldMinutes"), numberText(signal.max_hold_minutes)],
  ];
  const timePolicy = result.time_policy;
  const invalidation = Array.isArray(signal.invalidation) ? signal.invalidation : [];
  const reasonCodes = Array.isArray(signal.reason_codes) ? signal.reason_codes : [];
  return (
    <article className="signal-card" aria-labelledby="signal-card-title">
      <header className="signal-card__header">
        <div>
          <p className="eyebrow">{t("asset.persistedResult")}</p>
          <h3 id="signal-card-title">{t("asset.signalProposal")}</h3>
        </div>
        <span className={`signal-action signal-action--${signal.action?.toLowerCase() ?? "unknown"}`}>
          {actionText(signal.action, t)}
        </span>
      </header>
      <p className="signal-card__summary">{stringText(signal.summary)}</p>
      <DataFacts entries={signalFacts} />
      <section className="signal-card__section" aria-labelledby="model-status-title">
        <h4 id="model-status-title" className="subsection-label">
          {t("asset.modelStatus")}
        </h4>
        <DataFacts
          entries={[
            [t("common.provider"), stringText(model?.provider)],
            [t("common.available"), booleanText(model?.available)],
            [t("common.errorCode"), stringText(model?.error_code)],
          ]}
        />
      </section>
      <section className="signal-card__section" aria-labelledby="signal-reasons-title">
        <h4 id="signal-reasons-title" className="subsection-label">
          {t("asset.reasonCodes")}
        </h4>
        {reasonCodes.length > 0 ? (
          <ul className="tag-list">
            {reasonCodes.map((reason) => <li key={reason}>{reason}</li>)}
          </ul>
        ) : (
          <p className="panel-reading">{t("asset.reasonMissing")}</p>
        )}
      </section>
      <section className="signal-card__section" aria-labelledby="signal-levels-title">
        <h4 id="signal-levels-title" className="subsection-label">
          {t("asset.priceLevels")}
        </h4>
        {actionable ? (
          <DataFacts entries={levels} />
        ) : (
          <p className="panel-reading">{t("asset.waitLevels")}</p>
        )}
      </section>
      <section className="signal-card__section" aria-labelledby="signal-invalidation-title">
        <h4 id="signal-invalidation-title" className="subsection-label">
          {t("asset.invalidation")}
        </h4>
        {invalidation.length > 0 ? (
          <ul className="plain-list">
            {invalidation.map((condition) => <li key={condition}>{condition}</li>)}
          </ul>
        ) : (
          <p className="panel-reading">{t("asset.invalidationMissing")}</p>
        )}
      </section>
      <section className="signal-card__section" aria-labelledby="time-policy-title">
        <h4 id="time-policy-title" className="subsection-label">
          {t("asset.timePolicy")}
        </h4>
        {timePolicy ? <TimePolicyFacts policy={timePolicy} /> : <p className="panel-reading">{t("asset.timePolicyMissing")}</p>}
      </section>
      <p className="signal-card__boundary">
        {t("asset.savedBoundary")}
      </p>
    </article>
  );
}

function TimePolicyFacts({ policy }: { policy: TimePolicy }) {
  const { t, text } = useI18n();
  const { booleanText, numberText, primitiveText: stringText } = useResearchFormatters();
  const rangeText = (value: unknown): string =>
    Array.isArray(value) ? value.filter((item) => typeof item === "number").join(" – ") || t("common.notSupplied") : t("common.notSupplied");
  return (
    <DataFacts
      entries={[
        [t("common.timeframe"), stringText(policy.timeframe)],
        [t("asset.signalValidityRange"), rangeText(policy.signal_validity_minutes)],
        [t("asset.holdingHorizonRange"), rangeText(policy.holding_horizon_minutes)],
        [t("asset.reevaluateRange"), rangeText(policy.reevaluate_minutes)],
        [t("asset.volatilityRatio"), numberText(policy.volatility_ratio)],
        [t("asset.marketRegime"), text(stringText(policy.market_regime))],
        [t("asset.eventRisk"), booleanText(policy.event_risk)],
      ]}
    />
  );
}

function provenanceForAnalysis(result: AnalysisResult, t: ReturnType<typeof useI18n>["t"]): DashboardProvenance {
  const signal = result.signal;
  if (!signal) {
    return {
      dataSource: t("asset.snapshotNotAnalyzed"),
      model: t("asset.noModelResult"),
      state: "neutral",
      footer: t("asset.provenanceBoundary"),
    };
  }
  const expiry = parseTimestamp(signal.signal_valid_until);
  const provider = result.provider_snapshot?.provider;
  const source = signal.source_type;
  const dataSource = [source ? `source_type: ${source}` : undefined, provider ? `provider: ${provider}` : undefined]
    .filter((value): value is string => Boolean(value))
    .join(" · ") || t("common.sourceNotSupplied");
  const model = modelText(signal, result.model, t("common.notSupplied"));
  return {
    generatedAt: signal.generated_at ?? undefined,
    reevaluateAt: signal.reevaluate_at ?? undefined,
    expiresAt: signal.signal_valid_until ?? undefined,
    dataSource,
    model: model === t("common.notSupplied") ? t("common.modelMissing") : `model_id: ${model}`,
    state: expiry ? (expiry.getTime() <= Date.now() ? "expired" : "active") : "neutral",
    footer: signal.prediction_id
      ? `${t("common.predictionPrefix")} ${signal.prediction_id} · ${t("asset.noPaperTradeCreated")}`
      : t("asset.analysisNoPredictionId"),
  };
}

function AnalysisPanel({
  state,
  onRun,
}: {
  state: AnalysisState;
  onRun: () => void;
}) {
  const { t } = useI18n();
  const requestState =
    state.status === "idle"
      ? t("asset.analysisNotRun")
      : state.status === "pending"
        ? t("common.pending")
        : state.status === "ready"
          ? t("common.complete")
          : t("common.failed");
  return (
    <section className={`async-panel analysis-panel analysis-panel--${state.status}`} aria-labelledby="analysis-panel-title">
      <header className="async-panel__header">
        <div className="async-panel__heading">
          <h2 id="analysis-panel-title">{t("asset.analysis")}</h2>
          <p className="async-panel__source">
            {t("common.source")} <code>POST /analysis/:symbol</code> · {t("asset.requestState")}: {requestState} · {t("asset.analysisCreates")}
          </p>
        </div>
        <span className={`panel-state panel-state--${state.status === "unavailable" ? "unavailable" : state.status === "pending" ? "loading" : state.status === "ready" ? "ready" : "empty"}`}>
          {state.status === "pending" ? t("asset.analysisRunning") : state.status === "ready" ? t("asset.analysisResult") : state.status === "unavailable" ? t("common.unavailable") : t("asset.analysisNotRun")}
        </span>
      </header>
      <div className="async-panel__body">
        <p className="panel-reading">
          {t("asset.analysisBoundary")}
        </p>
        {state.status === "pending" ? <p className="analysis-status" role="status">{t("asset.analysisProgress")}</p> : null}
        {state.status === "unavailable" ? (
          <div className="panel-message panel-message--unavailable" role="alert">
            <p>{t("asset.analysisFailed")}</p>
            <p className="panel-message__error">{safeErrorText(state.error, t("asset.analysisNotUsable"))}</p>
          </div>
        ) : null}
        {state.status === "ready" ? <SignalCard result={state.data} /> : null}
        <button className="primary-button" type="button" onClick={onRun} disabled={state.status === "pending"}>
          {state.status === "pending" ? t("asset.runningAnalysis") : t("asset.runAnalysis")}
        </button>
      </div>
    </section>
  );
}

function safeErrorText(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    return `${error.code}: ${error.message}`;
  }
  return fallback;
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
  const { t } = useI18n();
  return (
    <div className="asset-selector">
      <label htmlFor="asset-symbol">{t("asset.instrument")}</label>
      <select id="asset-symbol" value={selectedSymbol} onChange={(event) => onSelect(event.target.value)}>
        {instruments.map((instrument) => (
          <option key={instrument.symbol} value={instrument.symbol}>
            {instrument.symbol} · {instrument.exchange}
          </option>
        ))}
      </select>
      <p className="data-meta">{t("asset.instrumentSource")}</p>
    </div>
  );
}

export function AssetDetailPage({ apiClient, onProvenanceChange }: AssetDetailPageProps) {
  const { t } = useI18n();
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
  const contextLoader = useCallback(
    (signal: AbortSignal) => apiClient.instrumentContext(canonicalSymbol, { timeframe, limit: SNAPSHOT_LIMIT }, signal),
    [apiClient, canonicalSymbol, timeframe],
  );
  const snapshot = useAsyncResource(snapshotLoader, Boolean(selectedInstrument));
  const news = useAsyncResource(newsLoader, Boolean(selectedInstrument));
  const context = useAsyncResource(contextLoader, Boolean(selectedInstrument));

  useEffect(() => {
    if (selectedInstrument && selectedInstrument.symbol !== requestedSymbol) {
      navigate(`/assets/${encodeURIComponent(selectedInstrument.symbol)}`, { replace: true });
    }
  }, [navigate, requestedSymbol, selectedInstrument]);

  useEffect(() => {
    analysisController.current?.abort();
    analysisController.current = null;
    setAnalysisState({ status: "idle" });
    onProvenanceChange({
      dataSource: t("asset.snapshotNotAnalyzed"),
      model: t("asset.noModelResult"),
      state: "neutral",
      footer: t("asset.provenanceBoundary"),
    });
  }, [onProvenanceChange, requestedSymbol, t, timeframe]);

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
          onProvenanceChange(provenanceForAnalysis(result, t));
        }
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted && !(error instanceof Error && error.name === "AbortError")) {
          setAnalysisState({ status: "unavailable", error });
        }
      });
  }, [analysisState.status, apiClient, canonicalSymbol, onProvenanceChange, selectedInstrument, t, timeframe]);

  const snapshotData = snapshot.data;
  const snapshotProvider: ProviderSnapshot | undefined = snapshotData?.provider_snapshot;
  const snapshotDegraded = Boolean(snapshotProvider?.stale || snapshotProvider?.error_code);
  const newsData = news.data;
  const newsEmpty = Boolean(newsData && newsData.available !== false && (newsData.events?.length ?? 0) === 0);
  const newsDegraded = newsData?.available === false;
  const contextData = context.data;
  const contextDegraded = contextData?.benchmark_context?.status === "unavailable" || contextData?.events?.available === false;

  const handleInstrumentSelect = (symbol: string) => {
    navigate(`/assets/${encodeURIComponent(symbol)}`);
  };

  return (
    <section className="asset-page" aria-labelledby="asset-detail-title">
      <header className="page-intro asset-page__intro">
        <p className="eyebrow">{t("asset.eyebrow")}</p>
        <div className="asset-page__title-row">
          <div>
            <h1 id="asset-detail-title">{t("asset.title")} {requestedSymbol || t("asset.instrument")}</h1>
            <p className="page-intro__description">
              {t("asset.description")}
            </p>
          </div>
          {instruments.status === "ready" && instruments.data && selectedInstrument ? (
            <InstrumentSelector instruments={instruments.data} onSelect={handleInstrumentSelect} selectedSymbol={selectedInstrument.symbol} />
          ) : null}
        </div>
        <p className="page-boundary">{t("asset.boundary")}</p>
      </header>

      {instruments.status === "loading" ? (
        <div className="asset-roster-state" role="status">{t("asset.loadingRoster")}</div>
      ) : null}
      {instruments.status === "unavailable" ? (
        <div className="asset-roster-state asset-roster-state--error" role="alert">
          <p>{t("asset.rosterUnavailable")}</p>
          <button className="quiet-button" type="button" onClick={instruments.retry}>{t("asset.retryRoster")}</button>
        </div>
      ) : null}
      {instruments.status === "ready" && !selectedInstrument ? (
        <div className="asset-roster-state asset-roster-state--error" role="alert">
          <p>{requestedSymbol || t("common.route")} {t("asset.notInRoster")}</p>
          <p className="panel-reading">{t("asset.chooseRoster")}</p>
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
              <p className="subsection-label">{t("asset.context")}</p>
              <p className="asset-context-row__title">{selectedInstrument.symbol} · {selectedInstrument.exchange}</p>
              <p className="data-meta">{selectedInstrument.asset_type === "equity" ? t("common.assetEquity") : selectedInstrument.asset_type === "crypto" ? t("common.assetCrypto") : selectedInstrument.asset_type} · {selectedInstrument.sector || t("asset.sectorMissing")} · {selectedInstrument.currency}</p>
              <Link className="quiet-button asset-consult-link" to={`/consult?symbol=${encodeURIComponent(selectedInstrument.symbol)}`}>
                {t("asset.consultQwen")}
              </Link>
            </div>
            <div className="timeframe-control">
              <span className="subsection-label" id="timeframe-label">{t("asset.snapshotTimeframe")}</span>
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
              title={t("asset.snapshot")}
              source={`GET /instruments/${canonicalSymbol}/snapshot?timeframe=${timeframe}&limit=${SNAPSHOT_LIMIT}`}
              freshness={snapshotData?.data_as_of ? `data_as_of ${snapshotData.data_as_of}` : t("asset.snapshotFreshness")}
              state={resourcePanelState(snapshot, !snapshotData, snapshotDegraded)}
              error={snapshot.error}
              onRetry={snapshot.retry}
              emptyMessage={t("asset.snapshotEmpty")}
              degradedMessage={t("asset.snapshotDegraded")}
              className="asset-panel asset-panel--wide"
            >
              {snapshotData ? <SnapshotEvidence snapshot={snapshotData} /> : null}
            </AsyncPanel>

            <AsyncPanel
              title={t("asset.news")}
              source={`GET /instruments/${canonicalSymbol}/news`}
              freshness={newsData?.fetched_at ? `fetched_at ${newsData.fetched_at}` : t("asset.newsFetchedMissing")}
              state={resourcePanelState(news, !newsData || newsEmpty, newsDegraded)}
              error={news.error}
              onRetry={news.retry}
              emptyMessage={t("asset.newsEmpty")}
              degradedMessage={t("asset.newsDegraded")}
              className="asset-panel"
            >
              {newsData ? <NewsEvidence news={newsData} /> : null}
            </AsyncPanel>

            <AsyncPanel
              title={t("asset.contextTitle")}
              source={`GET /instruments/${canonicalSymbol}/context?timeframe=${timeframe}&limit=${SNAPSHOT_LIMIT}`}
              freshness={contextData?.data_as_of ? `data_as_of ${contextData.data_as_of}` : t("asset.snapshotFreshness")}
              state={resourcePanelState(context, !contextData, contextDegraded)}
              error={context.error}
              onRetry={context.retry}
              emptyMessage={t("asset.contextEmpty")}
              degradedMessage={t("asset.contextDegraded")}
              className="asset-panel asset-panel--wide"
            >
              {contextData ? <ContextEvidence context={contextData} /> : null}
            </AsyncPanel>

            <AnalysisPanel state={analysisState} onRun={runAnalysis} />
          </div>
        </>
      ) : null}
    </section>
  );
}
