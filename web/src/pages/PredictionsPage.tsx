import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";

import { ApiError, type ApplicationShellApiClient } from "../api/client";
import type { OutcomeStatus, Prediction, PredictionFilters, SourceType, Timeframe } from "../api/types";
import { AsyncPanel, type PanelState } from "../components/AsyncPanel";
import { ResearchFacts, exactNumberText, numberText, timestampText } from "../components/ResearchFacts";
import type { AsyncResource } from "../hooks/useAsyncResource";
import { useAsyncResource } from "../hooks/useAsyncResource";
import { EMPTY_DASHBOARD_PROVENANCE, type DashboardProvenance } from "./DashboardPage";
import { actionLabel, predictionProvenance, predictionValidity } from "./workflowUtils";

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

function predictionSymbol(prediction: Prediction): string {
  return prediction.symbol ?? prediction.instrument?.symbol ?? "Symbol not supplied";
}

function predictionStatus(prediction: Prediction): string {
  const validity = predictionValidity(prediction);
  if (validity === "expired") {
    return "Expired validity window";
  }
  if (validity === "active") {
    return "Active validity window";
  }
  return prediction.signal_valid_until ? "Validity not parseable" : "Validity not supplied";
}

interface PredictionDetailProps {
  prediction: Prediction;
  followState: FollowState;
  followMessage: string;
  onFollow: () => void;
}

function followDecision(prediction: Prediction): { enabled: boolean; reason: string } {
  if (prediction.paper_trade) {
    return { enabled: false, reason: "Already followed as a PaperTrade record." };
  }
  if (prediction.action === "WAIT") {
    return { enabled: false, reason: "WAIT is retained for coverage only; it cannot be followed." };
  }
  if (prediction.action !== "LONG" && prediction.action !== "SHORT") {
    return { enabled: false, reason: "Only a returned LONG or SHORT signal can be followed." };
  }
  const validity = predictionValidity(prediction);
  if (validity === "expired") {
    return { enabled: false, reason: "The returned signal validity window has expired." };
  }
  if (validity === "unknown") {
    return { enabled: false, reason: "Signal expiry was not supplied or was not parseable; Follow is unavailable." };
  }
  return { enabled: true, reason: "Creates or retains one local PaperTrade record; no order is sent." };
}

function PredictionDetail({ prediction, followState, followMessage, onFollow }: PredictionDetailProps) {
  const decision = followDecision(prediction);
  const outcome = prediction.outcome;
  const levelFacts = ([
    ["Entry low", prediction.entry_low],
    ["Entry high", prediction.entry_high],
    ["Stop", prediction.stop],
    ["TP1", prediction.tp1],
    ["TP2", prediction.tp2],
  ] as const).flatMap(([label, value]) => typeof value === "number" && Number.isFinite(value) ? [{ label, value: numberText(value) }] : []);

  return (
    <div className="record-detail">
      <div className="record-detail__heading">
        <div>
          <p className="eyebrow">Allowlisted prediction evidence</p>
          <h3>{prediction.prediction_id}</h3>
        </div>
        <span className={`status-chip status-chip--${(prediction.action ?? "neutral").toLowerCase()}`}>
          {prediction.action ?? "Action not supplied"}
        </span>
      </div>

      <ResearchFacts
        facts={[
          { label: "Symbol", value: predictionSymbol(prediction) },
          { label: "Action", value: actionLabel(prediction.action) },
          { label: "Timeframe", value: prediction.timeframe ?? prediction.analysis_timeframe ?? "Not supplied" },
          { label: "Source type", value: prediction.source_type ?? "Not supplied" },
          { label: "Replay run", value: prediction.replay_run_id ?? "Not supplied" },
          { label: "Raw confidence", value: exactNumberText(prediction.raw_confidence) },
          { label: "Calibrated confidence", value: numberText(prediction.calibrated_confidence, 6) },
          { label: "Generated at", value: timestampText(prediction.generated_at) },
          { label: "Data as of", value: timestampText(prediction.data_as_of) },
          { label: "Re-evaluate at", value: timestampText(prediction.reevaluate_at) },
          { label: "Signal valid until", value: timestampText(prediction.signal_valid_until) },
          { label: "Expected hold until", value: timestampText(prediction.expected_hold_until) },
          { label: "Maximum hold until", value: timestampText(prediction.max_hold_until) },
          { label: "Validity minutes", value: numberText(prediction.signal_validity_minutes) },
          { label: "Expected hold minutes", value: numberText(prediction.expected_hold_minutes) },
          { label: "Maximum hold minutes", value: numberText(prediction.max_hold_minutes) },
          { label: "Model", value: prediction.model_id ?? "Not supplied" },
          { label: "Model version", value: prediction.model_version ?? "Not supplied" },
          { label: "Prompt version", value: prediction.prompt_version ?? "Not supplied" },
          { label: "Parse status", value: prediction.parse_status ?? "Not supplied" },
          { label: "Calibration version", value: prediction.calibration_version ?? "Not supplied" },
        ]}
      />

      {prediction.summary ? (
        <div className="record-detail__section">
          <p className="subsection-label">Summary</p>
          <p className="panel-reading">{prediction.summary}</p>
        </div>
      ) : null}

      {levelFacts.length > 0 ? (
        <div className="record-detail__section">
          <p className="subsection-label">Returned risk references</p>
          <ResearchFacts
            compact
            facts={levelFacts}
          />
        </div>
      ) : null}

      {prediction.reason_codes && prediction.reason_codes.length > 0 ? (
        <div className="record-detail__section">
          <p className="subsection-label">Reason codes</p>
          <ul className="tag-list">
            {prediction.reason_codes.map((reason) => <li key={reason}>{reason}</li>)}
          </ul>
        </div>
      ) : null}

      {prediction.invalidation && prediction.invalidation.length > 0 ? (
        <div className="record-detail__section">
          <p className="subsection-label">Invalidation conditions</p>
          <ul className="plain-list">
            {prediction.invalidation.map((condition) => <li key={condition}>{condition}</li>)}
          </ul>
        </div>
      ) : null}

      <div className="record-detail__section follow-section" aria-label="PaperTrade follow status">
        <p className="subsection-label">PaperTrade follow</p>
        {prediction.paper_trade ? (
          <p className="panel-reading">
            Already followed as a local PaperTrade · status {prediction.paper_trade.status} · followed at {timestampText(prediction.paper_trade.followed_at)}.
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
              {followState === "pending" ? "Saving PaperTrade…" : "Follow as PaperTrade"}
            </button>
          </>
        )}
      </div>

      <div className="record-detail__section">
        <p className="subsection-label">Outcome</p>
        {outcome ? (
          <ResearchFacts
            compact
            facts={[
              { label: "Status", value: outcome.status },
              { label: "Settled at", value: timestampText(outcome.settled_at) },
              { label: "Realized R", value: numberText(typeof outcome.realized_r === "number" ? outcome.realized_r : undefined) },
              { label: "MFE R", value: numberText(typeof outcome.mfe_r === "number" ? outcome.mfe_r : undefined) },
              { label: "MAE R", value: numberText(typeof outcome.mae_r === "number" ? outcome.mae_r : undefined) },
            ]}
          />
        ) : (
          <p className="panel-reading">No linked Outcome record was returned.</p>
        )}
      </div>
    </div>
  );
}

export function PredictionsPage({ apiClient, onProvenanceChange }: PredictionsPageProps) {
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
  const detailLoader = useCallback(
    (signal: AbortSignal) => selectedId ? apiClient.prediction(selectedId, signal) : Promise.resolve(undefined),
    [apiClient, selectedId],
  );
  const detail = useAsyncResource(detailLoader, Boolean(selectedId));

  useEffect(() => {
    if (!selectedId) {
      onProvenanceChange(EMPTY_DASHBOARD_PROVENANCE);
      return;
    }
    if (detail.data) {
      const provenance = predictionProvenance(detail.data);
      onProvenanceChange({
        ...provenance,
        footer: `Prediction ${detail.data.prediction_id} selected; timestamps are returned by the API.`,
      });
    } else {
      onProvenanceChange(EMPTY_DASHBOARD_PROVENANCE);
    }
  }, [detail.data, onProvenanceChange, selectedId]);

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

  const list = predictions.data ?? [];
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
    if (!prediction || followState === "pending" || !followDecision(prediction).enabled) {
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
      setFollowMessage(`PaperTrade record saved with status ${result.status}; no order was sent.`);
      detail.retry();
      predictions.retry();
    } catch (error: unknown) {
      if (!isCurrentRequest()) return;
      const message = error instanceof ApiError ? `${error.code}: ${error.message}` : "PaperTrade follow failed.";
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
        <p className="eyebrow">Prediction ledger / returned records</p>
        <h1 id="predictions-title">Predictions</h1>
        <p className="page-intro__description">
          Browse stored prediction evidence and validity windows. WAIT records remain visible for coverage; only eligible LONG or SHORT records can be followed as local PaperTrades.
        </p>
        <p className="page-boundary">No raw model response or context payload is rendered. Follow never sends an order.</p>
      </header>

      <form className="workflow-filters" aria-label="Prediction filters" onSubmit={applyFilters}>
        <div className="workflow-filters__grid">
          <label>Symbol<input value={draft.symbol} onChange={(event) => setDraftValue("symbol", event.target.value)} placeholder="All symbols" /></label>
          <label>Timeframe<select value={draft.timeframe} onChange={(event) => setDraftValue("timeframe", event.target.value)}><option value="">All timeframes</option>{timeframeOptions.map((value) => <option key={value}>{value}</option>)}</select></label>
          <label>Action<select value={draft.action} onChange={(event) => setDraftValue("action", event.target.value)}><option value="">All actions</option>{actionOptions.map((value) => <option key={value}>{value}</option>)}</select></label>
          <label>Source type<select value={draft.source_type} onChange={(event) => setDraftValue("source_type", event.target.value)}><option value="">All sources</option><option value="live">live</option><option value="replay">replay</option></select></label>
          <label>Outcome status<select value={draft.outcome_status} onChange={(event) => setDraftValue("outcome_status", event.target.value)}><option value="">Any outcome</option>{outcomeOptions.map((value) => <option key={value}>{value}</option>)}</select></label>
          <label>Outcome presence<select value={draft.outcome_presence} onChange={(event) => setDraftValue("outcome_presence", event.target.value)}><option value="">Any presence</option><option value="true">Has outcome</option><option value="false">No outcome</option></select></label>
          <label>Rows<select value={limit} onChange={(event) => setLimit(Number(event.target.value))}>{pageLimits.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
        </div>
        <button className="primary-button" type="submit">Apply filters</button>
      </form>

      <div className="workflow-grid workflow-grid--records">
        <AsyncPanel
          className="workflow-panel workflow-panel--list"
          title="Prediction records"
          source={requestLabel}
          freshness="server response; generated_at shown per record"
          state={resourceState(predictions, list.length === 0)}
          error={predictions.error}
          onRetry={predictions.retry}
          emptyMessage="No Prediction records matched these filters."
        >
          {list.length > 0 ? (
            <div className="prediction-ledger-wrap">
              <table className="prediction-ledger" aria-label="Prediction records">
                <thead><tr><th scope="col">Record</th><th scope="col">Action</th><th scope="col">Confidence</th><th scope="col">Source / generated</th><th scope="col">Validity</th></tr></thead>
                <tbody>
                  {list.map((prediction) => (
                    <tr key={prediction.prediction_id}>
                      <td>
                        <button className="record-link" type="button" onClick={() => setSelectedId(prediction.prediction_id)}>{prediction.prediction_id}</button>
                        <span className="data-meta">{predictionSymbol(prediction)}</span>
                      </td>
                      <td><span className={`signal-text signal-text--${(prediction.action ?? "unknown").toLowerCase()}`}>{actionLabel(prediction.action)}</span><span className="data-meta">{prediction.timeframe ?? prediction.analysis_timeframe ?? "Timeframe not supplied"}</span></td>
                      <td><span className="data-strong">{exactNumberText(prediction.raw_confidence)}</span><span className="data-meta">Calibrated: {numberText(prediction.calibrated_confidence, 6)}</span></td>
                      <td><span className="data-strong">{prediction.source_type ?? "Source not supplied"}</span><span className="data-meta">{timestampText(prediction.generated_at)}</span></td>
                      <td>{predictionStatus(prediction)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          <div className="pagination-controls" aria-label="Prediction pagination">
            <span>Offset {filters.offset ?? 0} · maximum {filters.limit ?? limit} rows</span>
            <div><button className="quiet-button" type="button" disabled={(filters.offset ?? 0) === 0} onClick={() => movePage(-1)}>Previous</button><button className="quiet-button" type="button" disabled={list.length < (filters.limit ?? limit)} onClick={() => movePage(1)}>Next</button></div>
          </div>
        </AsyncPanel>

        <AsyncPanel
          className="workflow-panel workflow-panel--detail"
          title="Prediction detail"
          source={selectedId ? `GET /predictions/${selectedId}` : "GET /predictions/{id}"}
          freshness="returned timestamps are shown in UTC"
          state={detailState}
          error={detail.error}
          onRetry={detail.retry}
          emptyMessage="Select a Prediction record to inspect its allowlisted evidence."
        >
          {detail.data ? <PredictionDetail prediction={detail.data} followState={followState} followMessage={followMessage} onFollow={() => void handleFollow()} /> : null}
        </AsyncPanel>
      </div>
    </section>
  );
}
