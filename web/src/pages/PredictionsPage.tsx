import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { ApiError, type ApplicationShellApiClient } from "../api/client";
import type { MarketIntelligenceResponse, MarketPulseItem, OutcomeStatus, Prediction, PredictionFilters, SourceType, Timeframe, TriggerEvent } from "../api/types";
import { AsyncPanel, type PanelState } from "../components/AsyncPanel";
import { ResearchFacts, useResearchFormatters } from "../components/ResearchFacts";
import type { AsyncResource } from "../hooks/useAsyncResource";
import { useAsyncResource } from "../hooks/useAsyncResource";
import { useI18n, type TranslationKey } from "../i18n";
import { emptyDashboardProvenance, type DashboardProvenance } from "./DashboardPage";
import { actionLabel, predictionProvenance, predictionValidity, sourceTypeLabel } from "./workflowUtils";

interface PredictionsPageProps {
  apiClient: ApplicationShellApiClient;
  onProvenanceChange: (provenance: DashboardProvenance) => void;
}

type FilterDraft = {
  symbol: string;
  timeframe: Timeframe | "";
  action: Prediction["action"] | "";
  source_type: SourceType | "";
  outcome_status: OutcomeStatus | "";
  outcome_presence: "" | "true" | "false";
};

type FollowState = "idle" | "pending" | "complete" | "failed";

const timeframeOptions: Timeframe[] = ["5m", "15m", "1h", "4h", "1d"];
const actionOptions: Array<NonNullable<Prediction["action"]>> = ["LONG", "SHORT", "WAIT"];
const outcomeOptions: OutcomeStatus[] = ["TP1", "TP2", "STOP", "TIMEOUT", "INVALIDATED", "PENDING"];
const pageLimits = [25, 50, 100];
const defaultDiscoverySymbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"];

const initialDraft: FilterDraft = {
  symbol: "",
  timeframe: "",
  action: "",
  source_type: "",
  outcome_status: "",
  outcome_presence: "",
};

const initialFilters: PredictionFilters = { limit: 25, offset: 0 };

function resourceState<T>(resource: AsyncResource<T>, empty: boolean): PanelState {
  if (resource.status === "loading") {
    return "loading";
  }
  if (resource.status === "unavailable") {
    return "unavailable";
  }
  return empty ? "empty" : "ready";
}

function parseAction(value: string): Prediction["action"] | "" {
  return value === "LONG" || value === "SHORT" || value === "WAIT" ? value : "";
}

function parseTimeframe(value: string): Timeframe | "" {
  return timeframeOptions.includes(value as Timeframe) ? (value as Timeframe) : "";
}

function parseSource(value: string): SourceType | "" {
  return value === "live" || value === "replay" ? value : "";
}

function parseOutcome(value: string): OutcomeStatus | "" {
  return outcomeOptions.includes(value as OutcomeStatus) ? (value as OutcomeStatus) : "";
}

function buildFilters(draft: FilterDraft, limit: number): PredictionFilters {
  return {
    limit,
    offset: 0,
    ...(draft.symbol.trim() ? { symbol: draft.symbol.trim().toUpperCase() } : {}),
    ...(draft.timeframe ? { timeframe: draft.timeframe } : {}),
    ...(draft.action ? { action: draft.action } : {}),
    ...(draft.source_type ? { source_type: draft.source_type } : {}),
    ...(draft.outcome_status ? { outcome_status: draft.outcome_status } : {}),
    ...(draft.outcome_presence ? { has_outcome: draft.outcome_presence === "true" } : {}),
  };
}

function predictionSymbol(prediction: Prediction, fallback: string): string {
  return prediction.symbol ?? prediction.instrument?.symbol ?? fallback;
}

function predictionStatus(prediction: Prediction, t: (key: TranslationKey) => string): string {
  const validity = predictionValidity(prediction);
  if (validity === "expired") {
    return t("predictions.expiredValidity");
  }
  if (validity === "active") {
    return t("predictions.activeValidity");
  }
  return prediction.signal_valid_until ? t("predictions.validityNotParseable") : t("predictions.unknownValidity");
}

function recordString(record: Record<string, unknown> | undefined, key: string): string | undefined {
  const value = record?.[key];
  return typeof value === "string" && value.trim() ? value : undefined;
}

function discoveryItems(intelligence: MarketIntelligenceResponse | undefined): MarketPulseItem[] {
  const pulse = new Map((intelligence?.pulse ?? []).map((item) => [item.symbol, item]));
  const symbols = [...defaultDiscoverySymbols];
  for (const item of intelligence?.watchlist ?? []) {
    if (item.symbol && !symbols.includes(item.symbol)) symbols.push(item.symbol);
  }
  return symbols.slice(0, 6).map((symbol) => pulse.get(symbol) ?? {
    symbol,
    status: "unavailable",
    price: null,
    change_pct: null,
    freshness: { status: "unavailable" },
    missing_reasons: ["no_current_market_evidence"],
  });
}

function latestTrigger(events: TriggerEvent[], symbol: string): TriggerEvent | undefined {
  return events.find((event) => event.instrument_id === symbol);
}

interface PredictionDetailProps {
  prediction: Prediction;
  followState: FollowState;
  followMessage: string;
  onFollow: () => void;
}

function followDecision(prediction: Prediction, t: (key: TranslationKey) => string): { enabled: boolean; reason: string } {
  if (prediction.paper_trade) {
    return { enabled: false, reason: t("predictions.alreadyFollowed") };
  }
  if (prediction.action === "WAIT") {
    return { enabled: false, reason: t("predictions.waitNoFollow") };
  }
  if (prediction.action !== "LONG" && prediction.action !== "SHORT") {
    return { enabled: false, reason: t("predictions.onlyActionable") };
  }
  const validity = predictionValidity(prediction);
  if (validity === "expired") {
    return { enabled: false, reason: t("predictions.expired") };
  }
  if (validity === "unknown") {
    return { enabled: false, reason: t("predictions.expiryUnknown") };
  }
  return { enabled: true, reason: t("predictions.followReason") };
}

function PredictionDetail({ prediction, followState, followMessage, onFollow }: PredictionDetailProps) {
  const { t } = useI18n();
  const { exactNumberText, numberText, timestampText } = useResearchFormatters();
  const decision = followDecision(prediction, t);
  const outcome = prediction.outcome;
  const levelFacts = ([
    ["common.entryLow", prediction.entry_low],
    ["common.entryHigh", prediction.entry_high],
    ["common.stop", prediction.stop],
    ["common.statusTp1", prediction.tp1],
    ["common.statusTp2", prediction.tp2],
  ] as const).flatMap(([label, value]) => typeof value === "number" && Number.isFinite(value) ? [{ label: t(label), value: numberText(value) }] : []);

  return (
    <div className="record-detail">
      <div className="record-detail__heading">
        <div>
          <p className="eyebrow">{t("predictions.allowlisted")}</p>
          <h3>{prediction.prediction_id}</h3>
        </div>
        <span className={`status-chip status-chip--${(prediction.action ?? "neutral").toLowerCase()}`}>
          {prediction.action ?? t("common.actionNotSupplied")}
        </span>
      </div>
      <Link className="quiet-button asset-consult-link" to={`/consult?symbol=${encodeURIComponent(predictionSymbol(prediction, ""))}&prediction_id=${encodeURIComponent(prediction.prediction_id)}`}>
        {t("v11.askAiSignal")}
      </Link>

      <ResearchFacts
        facts={[
          { label: t("common.symbol"), value: predictionSymbol(prediction, t("common.symbolNotSupplied")) },
          { label: t("common.action"), value: actionLabel(prediction.action, t) },
          { label: t("common.timeframe"), value: prediction.timeframe ?? prediction.analysis_timeframe ?? t("common.notSupplied") },
          { label: t("common.sourceType"), value: sourceTypeLabel(prediction.source_type, t) },
          { label: t("common.replayRun"), value: prediction.replay_run_id ?? t("common.notSupplied") },
          { label: t("common.rawConfidence"), value: exactNumberText(prediction.raw_confidence) },
          { label: t("common.calibratedConfidence"), value: numberText(prediction.calibrated_confidence, 6) },
          { label: t("common.generated"), value: timestampText(prediction.generated_at) },
          { label: t("common.dataAsOf"), value: timestampText(prediction.data_as_of) },
          { label: t("common.reevaluate"), value: timestampText(prediction.reevaluate_at) },
          { label: t("common.validUntil"), value: timestampText(prediction.signal_valid_until) },
          { label: t("common.expectedHoldUntil"), value: timestampText(prediction.expected_hold_until) },
          { label: t("common.maximumHoldUntil"), value: timestampText(prediction.max_hold_until) },
          { label: t("common.validityMinutes"), value: numberText(prediction.signal_validity_minutes) },
          { label: t("common.expectedHoldMinutes"), value: numberText(prediction.expected_hold_minutes) },
          { label: t("common.maximumHoldMinutes"), value: numberText(prediction.max_hold_minutes) },
          { label: t("common.model"), value: prediction.model_id ?? t("common.notSupplied") },
          { label: t("common.modelVersion"), value: prediction.model_version ?? t("common.notSupplied") },
          { label: t("common.promptVersion"), value: prediction.prompt_version ?? t("common.notSupplied") },
          { label: t("common.parseStatus"), value: prediction.parse_status ?? t("common.notSupplied") },
          { label: t("common.calibrationVersion"), value: prediction.calibration_version ?? t("common.notSupplied") },
        ]}
      />

      {prediction.summary ? (
        <div className="record-detail__section">
          <p className="subsection-label">{t("common.summary")}</p>
          <p className="panel-reading">{prediction.summary}</p>
        </div>
      ) : null}

      {levelFacts.length > 0 ? (
        <div className="record-detail__section">
          <p className="subsection-label">{t("predictions.returnedRisk")}</p>
          <ResearchFacts
            compact
            facts={levelFacts}
          />
        </div>
      ) : null}

      {prediction.reason_codes && prediction.reason_codes.length > 0 ? (
        <div className="record-detail__section">
          <p className="subsection-label">{t("asset.reasonCodes")}</p>
          <ul className="tag-list">
            {prediction.reason_codes.map((reason) => <li key={reason}>{reason}</li>)}
          </ul>
        </div>
      ) : null}

      {prediction.invalidation && prediction.invalidation.length > 0 ? (
        <div className="record-detail__section">
          <p className="subsection-label">{t("asset.invalidation")}</p>
          <ul className="plain-list">
            {prediction.invalidation.map((condition) => <li key={condition}>{condition}</li>)}
          </ul>
        </div>
      ) : null}

      <div className="record-detail__section follow-section" aria-label={t("predictions.paperFollowStatus")}>
        <p className="subsection-label">{t("predictions.paperFollow")}</p>
        {prediction.paper_trade ? (
          <p className="panel-reading">
            {t("predictions.alreadyFollowed")} · {t("common.status")} {prediction.paper_trade.status} · {t("common.followedAt")} {timestampText(prediction.paper_trade.followed_at)}.
          </p>
        ) : (
          <>
            <p className="panel-reading">{decision.reason}</p>
            {followState === "failed" ? <p className="panel-message__error" role="alert">{followMessage}</p> : null}
            {followState === "complete" ? <p className="follow-success" role="status">{followMessage}</p> : null}
            <button
              className="primary-button"
              type="button"
              disabled={!decision.enabled || followState === "pending" || followState === "complete"}
              onClick={onFollow}
            >
              {followState === "pending" ? t("predictions.savingPaperTrade") : t("predictions.follow")}
            </button>
          </>
        )}
      </div>

      <div className="record-detail__section">
        <p className="subsection-label">{t("common.outcome")}</p>
        {outcome ? (
          <ResearchFacts
            compact
            facts={[
              { label: t("common.status"), value: outcome.status },
              { label: t("common.settledAt"), value: timestampText(outcome.settled_at) },
              { label: t("common.realizedR"), value: numberText(typeof outcome.realized_r === "number" ? outcome.realized_r : undefined) },
              { label: t("common.mfeR"), value: numberText(typeof outcome.mfe_r === "number" ? outcome.mfe_r : undefined) },
              { label: t("common.maeR"), value: numberText(typeof outcome.mae_r === "number" ? outcome.mae_r : undefined) },
            ]}
          />
        ) : (
          <p className="panel-reading">{t("predictions.noOutcome")}</p>
        )}
      </div>
    </div>
  );
}

export function PredictionsPage({ apiClient, onProvenanceChange }: PredictionsPageProps) {
  const { t, text } = useI18n();
  const { exactNumberText, numberText, timestampText } = useResearchFormatters();
  const [draft, setDraft] = useState<FilterDraft>(initialDraft);
  const [limit, setLimit] = useState(25);
  const [filters, setFilters] = useState<PredictionFilters>(initialFilters);
  const [selectedId, setSelectedId] = useState<string>();
  const [followState, setFollowState] = useState<FollowState>("idle");
  const [followMessage, setFollowMessage] = useState("");
  const followController = useRef<AbortController | null>(null);
  const followInFlight = useRef(false);
  const followRequestVersion = useRef(0);

  const listLoader = useCallback((signal: AbortSignal) => apiClient.predictions(filters, signal), [apiClient, filters]);
  const predictions = useAsyncResource(listLoader);
  const list = predictions.data ?? [];
  const discoveryEnabled = predictions.status === "ready" && list.length === 0;
  const intelligenceLoader = useCallback((signal: AbortSignal) => apiClient.marketIntelligence(signal), [apiClient]);
  const runtimeLoader = useCallback((signal: AbortSignal) => apiClient.monitoringStatus(signal), [apiClient]);
  const triggerLoader = useCallback((signal: AbortSignal) => apiClient.triggerEvents(undefined, 30, signal), [apiClient]);
  const opportunityLoader = useCallback((signal: AbortSignal) => apiClient.monitoringOpportunities(undefined, 20, signal), [apiClient]);
  const intelligence = useAsyncResource(intelligenceLoader, discoveryEnabled);
  const runtime = useAsyncResource(runtimeLoader, discoveryEnabled);
  const triggers = useAsyncResource(triggerLoader, discoveryEnabled);
  const opportunities = useAsyncResource(opportunityLoader, discoveryEnabled);
  const detailLoader = useCallback(
    (signal: AbortSignal) => selectedId ? apiClient.prediction(selectedId, signal) : Promise.resolve(undefined),
    [apiClient, selectedId],
  );
  const detail = useAsyncResource(detailLoader, Boolean(selectedId));

  useEffect(() => {
    if (!selectedId) {
      onProvenanceChange(emptyDashboardProvenance(t));
      return;
    }
    if (detail.data) {
      const provenance = predictionProvenance(detail.data, t);
      onProvenanceChange({
        ...provenance,
        footer: `${t("common.predictionPrefix")} ${detail.data.prediction_id} · ${t("predictions.provenanceFooter")}`,
      });
    } else {
      onProvenanceChange(emptyDashboardProvenance(t));
    }
  }, [detail.data, onProvenanceChange, selectedId, t]);

  useEffect(() => {
    followRequestVersion.current += 1;
    followController.current?.abort();
    followController.current = null;
    followInFlight.current = false;
    setFollowState("idle");
    setFollowMessage("");
  }, [selectedId]);

  useEffect(() => () => {
    followRequestVersion.current += 1;
    followController.current?.abort();
    followController.current = null;
    followInFlight.current = false;
  }, []);

  const marketItems = useMemo(() => discoveryItems(intelligence.data), [intelligence.data]);
  const triggerItems = triggers.data?.events ?? [];
  const opportunityItems = opportunities.data?.analyses ?? [];
  const detailState: PanelState = !selectedId
    ? "empty"
    : detail.status === "idle" || detail.status === "loading"
      ? "loading"
      : resourceState(detail, !detail.data);
  const requestLabel = useMemo(() => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined) query.set(key, String(value));
    }
    return query.toString() ? `GET /predictions?${query.toString()}` : "GET /predictions";
  }, [filters]);

  const setDraftValue = (key: keyof FilterDraft, value: string) => {
    setDraft((current) => ({
      ...current,
      [key]: key === "action" ? parseAction(value) : key === "timeframe" ? parseTimeframe(value) : key === "source_type" ? parseSource(value) : key === "outcome_status" ? parseOutcome(value) : value,
    }));
  };

  const applyFilters = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFilters(buildFilters(draft, limit));
    setSelectedId(undefined);
  };

  const movePage = (direction: -1 | 1) => {
    const currentOffset = filters.offset ?? 0;
    const nextOffset = Math.max(0, currentOffset + direction * (filters.limit ?? limit));
    setFilters((current) => ({ ...current, offset: nextOffset }));
    setSelectedId(undefined);
  };

  const handleFollow = async () => {
    if (followInFlight.current) {
      return;
    }
    const prediction = detail.data;
    if (!prediction || followState === "pending" || !followDecision(prediction, t).enabled) {
      return;
    }
    followInFlight.current = true;
    const requestVersion = followRequestVersion.current + 1;
    followRequestVersion.current = requestVersion;
    const controller = new AbortController();
    followController.current?.abort();
    followController.current = controller;
    const isCurrentRequest = () => (
      followRequestVersion.current === requestVersion
      && followController.current === controller
      && !controller.signal.aborted
    );
    setFollowState("pending");
    setFollowMessage("");
    try {
      const result = await apiClient.followPrediction(prediction.prediction_id, {}, controller.signal);
      if (!isCurrentRequest()) return;
      setFollowState("complete");
      setFollowMessage(`${t("common.paperTradeSavedPrefix")} ${result.status}; ${t("predictions.noOrderSent")}`);
      detail.retry();
      predictions.retry();
    } catch (error: unknown) {
      if (!isCurrentRequest()) return;
      const message = error instanceof ApiError ? `${error.code}: ${error.message}` : t("predictions.followFailed");
      setFollowState("failed");
      setFollowMessage(message);
    } finally {
      if (followRequestVersion.current === requestVersion && followController.current === controller) {
        followController.current = null;
        followInFlight.current = false;
      }
    }
  };

  return (
    <section className="workflow-page predictions-page" aria-labelledby="predictions-title">
      <header className="page-intro">
        <p className="eyebrow">{t("predictions.eyebrow")}</p>
        <h1 id="predictions-title">{t("nav.predictions")}</h1>
        <p className="page-intro__description">
          {t("predictions.description")}
        </p>
        <p className="page-boundary">{t("predictions.boundary")}</p>
      </header>

      <form className="workflow-filters" aria-label={t("predictions.filters")} onSubmit={applyFilters}>
        <div className="workflow-filters__grid">
          <label>{t("common.symbol")}<input value={draft.symbol} onChange={(event) => setDraftValue("symbol", event.target.value)} placeholder={t("predictions.allSymbols")} /></label>
          <label>{t("common.timeframe")}<select value={draft.timeframe} onChange={(event) => setDraftValue("timeframe", event.target.value)}><option value="">{t("common.allTimeframes")}</option>{timeframeOptions.map((value) => <option key={value}>{value}</option>)}</select></label>
          <label>{t("common.action")}<select value={draft.action} onChange={(event) => setDraftValue("action", event.target.value)}><option value="">{t("common.allActions")}</option>{actionOptions.map((value) => <option key={value} value={value}>{actionLabel(value, t)}</option>)}</select></label>
          <label>{t("common.sourceType")}<select value={draft.source_type} onChange={(event) => setDraftValue("source_type", event.target.value)}><option value="">{t("common.allSources")}</option><option value="live">{t("common.sourceLive")}</option><option value="replay">{t("common.sourceReplay")}</option></select></label>
          <label>{t("common.outcomeStatus")}<select value={draft.outcome_status} onChange={(event) => setDraftValue("outcome_status", event.target.value)}><option value="">{t("common.anyOutcome")}</option>{outcomeOptions.map((value) => <option key={value} value={value}>{text(value)}</option>)}</select></label>
          <label>{t("common.outcomePresence")}<select value={draft.outcome_presence} onChange={(event) => setDraftValue("outcome_presence", event.target.value)}><option value="">{t("common.anyPresence")}</option><option value="true">{t("common.hasOutcome")}</option><option value="false">{t("common.noOutcome")}</option></select></label>
          <label>{t("common.rows")}<select value={limit} onChange={(event) => setLimit(Number(event.target.value))}>{pageLimits.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
        </div>
        <button className="primary-button" type="submit">{t("common.applyFilters")}</button>
      </form>

      {predictions.status === "unavailable" ? (
        <section className="signal-discovery signal-discovery--unavailable" aria-labelledby="signal-discovery-title">
          <div className="signal-discovery__head">
            <div><p className="eyebrow">{t("predictions.discoveryEyebrow")}</p><h2 id="signal-discovery-title">{t("predictions.discoveryTitle")}</h2></div>
            <span className="status-chip status-chip--negative">{t("common.unavailable")}</span>
          </div>
          <p role="alert">{t("predictions.backendUnavailable")}</p>
          <p className="data-meta">{t("predictions.noFabrication")}</p>
          <Link className="quiet-button" to="/settings">{t("predictions.openSettings")}</Link>
        </section>
      ) : discoveryEnabled ? (
        <section className="signal-discovery" aria-labelledby="signal-discovery-title">
          <div className="signal-discovery__head">
            <div><p className="eyebrow">{t("predictions.discoveryEyebrow")}</p><h2 id="signal-discovery-title">{t("predictions.discoveryTitle")}</h2></div>
            <span className="status-chip status-chip--neutral">{t("predictions.noSavedPrediction")}</span>
          </div>
          <p className="signal-discovery__description">{t("predictions.discoveryDescription")}</p>
          <p className="page-boundary">{t("predictions.deterministicBoundary")}</p>
          {intelligence.status === "loading" ? <p role="status">{t("predictions.marketLoading")}</p> : null}
          {intelligence.status === "unavailable" ? (
            <div className="signal-discovery__degraded" role="alert">
              <strong>{t("predictions.marketUnavailable")}</strong>
              <span>{t("predictions.noFabrication")}</span>
              <Link className="quiet-button" to="/settings">{t("predictions.openSettings")}</Link>
            </div>
          ) : null}
          {intelligence.data ? (
            <>
              <div className="signal-discovery__runtime">
                <span><strong>{t("predictions.runtimeState")}</strong> {runtime.data ? text(runtime.data.state) : runtime.status === "unavailable" ? t("common.unavailable") : t("common.loading")}</span>
                <span><strong>{t("predictions.triggerSource")}</strong> {triggers.data?.policy_version ?? t("common.unavailable")}</span>
                <span><strong>{t("common.dataAsOf")}</strong> {timestampText(intelligence.data.as_of)}</span>
              </div>
              {(runtime.status === "unavailable" || triggers.status === "unavailable" || opportunities.status === "unavailable") ? (
                <p className="signal-discovery__warning" role="status">{t("predictions.partialDegraded")}</p>
              ) : null}
              <div className="signal-discovery__grid">
                {marketItems.map((item) => {
                  const freshness = item.freshness as Record<string, unknown>;
                  const trigger = latestTrigger(triggerItems, item.symbol);
                  const opportunity = opportunityItems.find((entry) => entry.symbol === item.symbol);
                  const provider = recordString(freshness, "provider") ?? t("common.notSupplied");
                  const freshnessStatus = recordString(freshness, "status") ?? t("common.unavailable");
                  const dataAsOf = recordString(freshness, "data_as_of");
                  const reasons = item.missing_reasons.length > 0 ? item.missing_reasons.join(", ") : "";
                  return (
                    <article className={`signal-discovery-card signal-discovery-card--${item.status}`} key={item.symbol}>
                      <div className="signal-discovery-card__head">
                        <div><p className="eyebrow">{t("predictions.marketState")}</p><h3>{item.symbol}</h3></div>
                        <span className={`status-chip status-chip--${item.status === "available" ? "positive" : item.status === "degraded" ? "warning" : "neutral"}`}>{text(item.status)}</span>
                      </div>
                      <div className="signal-discovery-card__quote">
                        <strong>{numberText(item.price, 8)}</strong>
                        <span>{t("predictions.change")}: {typeof item.change_pct === "number" ? `${numberText(item.change_pct, 3)}%` : t("common.notSupplied")}</span>
                      </div>
                      <dl className="signal-discovery-card__facts">
                        <div><dt>{t("common.provider")}</dt><dd>{provider}</dd></div>
                        <div><dt>{t("predictions.freshnessState")}</dt><dd>{text(freshnessStatus)}</dd></div>
                        <div><dt>{t("common.dataAsOf")}</dt><dd>{timestampText(dataAsOf)}</dd></div>
                      </dl>
                      {reasons ? <p className="signal-discovery-card__reason"><strong>{t("predictions.missingReasons")}</strong> {reasons}</p> : null}
                      <div className="signal-discovery-card__trigger">
                        <span className="subsection-label">{t("predictions.triggerState")}</span>
                        {trigger ? (
                          <p><strong>{text(trigger.trigger_type)}</strong> · {t("common.score")} {numberText(trigger.trigger_score, 3)} · {text(trigger.status)} / {text(trigger.analysis_status)}<small>{timestampText(trigger.bar_end)}</small></p>
                        ) : <p>{t("predictions.noTrigger")}</p>}
                      </div>
                      {opportunity ? <p className="signal-discovery-card__opportunity"><strong>{t("predictions.latestOpportunity")}</strong> {text(opportunity.bias)} · {opportunity.model_id} · {numberText(opportunity.confidence, 3)}</p> : <p className="signal-discovery-card__opportunity">{t("predictions.noOpportunity")}</p>}
                      <Link className="primary-button signal-discovery-card__action" to={`/assets/${encodeURIComponent(item.symbol)}`}>{t("predictions.openAnalysis")}</Link>
                    </article>
                  );
                })}
              </div>
            </>
          ) : null}
        </section>
      ) : null}

      <div className="workflow-grid workflow-grid--records">
        <AsyncPanel
          className="workflow-panel workflow-panel--list"
          title={t("predictions.records")}
          source={requestLabel}
          freshness={t("common.serverGenerated")}
          state={resourceState(predictions, list.length === 0)}
          error={predictions.error}
          onRetry={predictions.retry}
          emptyMessage={t("predictions.noMatch")}
        >
          {list.length > 0 ? (
            <div className="prediction-ledger-wrap">
              <table className="prediction-ledger" aria-label={t("predictions.records")}>
                <thead><tr><th scope="col">{t("common.record")}</th><th scope="col">{t("common.action")}</th><th scope="col">{t("common.confidence")}</th><th scope="col">{t("common.sourceGenerated")}</th><th scope="col">{t("common.validity")}</th></tr></thead>
                <tbody>
                  {list.map((prediction) => (
                    <tr key={prediction.prediction_id}>
                      <td>
                        <button className="record-link" type="button" onClick={() => setSelectedId(prediction.prediction_id)}>{prediction.prediction_id}</button>
                        <span className="data-meta">{predictionSymbol(prediction, t("common.symbolNotSupplied"))}</span>
                      </td>
                      <td><span className={`signal-text signal-text--${(prediction.action ?? "unknown").toLowerCase()}`}>{actionLabel(prediction.action, t)}</span><span className="data-meta">{prediction.timeframe ?? prediction.analysis_timeframe ?? t("common.timeframeNotSupplied")}</span></td>
                      <td><span className="data-strong">{exactNumberText(prediction.raw_confidence)}</span><span className="data-meta">{t("common.calibrated")}: {numberText(prediction.calibrated_confidence, 6)}</span></td>
                      <td><span className="data-strong">{sourceTypeLabel(prediction.source_type, t)}</span><span className="data-meta">{timestampText(prediction.generated_at)}</span></td>
                      <td>{predictionStatus(prediction, t)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          <div className="pagination-controls" aria-label={t("predictions.pagination")}>
            <span>{t("common.offset")} {filters.offset ?? 0} · {t("common.maximum")} {filters.limit ?? limit} {t("common.rowsValue")}</span>
            <div><button className="quiet-button" type="button" disabled={(filters.offset ?? 0) === 0} onClick={() => movePage(-1)}>{t("common.previous")}</button><button className="quiet-button" type="button" disabled={list.length < (filters.limit ?? limit)} onClick={() => movePage(1)}>{t("common.next")}</button></div>
          </div>
        </AsyncPanel>

        <AsyncPanel
          className="workflow-panel workflow-panel--detail"
          title={t("predictions.detail")}
          source={selectedId ? `GET /predictions/${selectedId}` : "GET /predictions/{id}"}
          freshness={t("common.returnedTimestampsUtc")}
          state={detailState}
          error={detail.error}
          onRetry={detail.retry}
          emptyMessage={t("predictions.selectDetail")}
        >
          {detail.data ? <PredictionDetail prediction={detail.data} followState={followState} followMessage={followMessage} onFollow={() => void handleFollow()} /> : null}
        </AsyncPanel>
      </div>
    </section>
  );
}
