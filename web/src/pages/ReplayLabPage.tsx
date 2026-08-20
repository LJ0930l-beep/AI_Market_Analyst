import { useCallback, useMemo, useState, type FormEvent } from "react";

import type { ApplicationShellApiClient } from "../api/client";
import type {
  JsonRecord,
  ReplayRun,
  ReplayRunFilters,
  ReplaySample,
  ReplayStatus,
} from "../api/types";
import { AsyncPanel, type PanelState } from "../components/AsyncPanel";
import { ResearchFacts, primitiveText, timestampText, type ResearchFact } from "../components/ResearchFacts";
import type { AsyncResource } from "../hooks/useAsyncResource";
import { useAsyncResource } from "../hooks/useAsyncResource";

interface ReplayLabPageProps {
  apiClient: ApplicationShellApiClient;
}

const replayStatuses: ReplayStatus[] = ["PENDING", "RUNNING", "COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"];
const pageLimits = [25, 50, 100];
const initialFilters: ReplayRunFilters = { limit: 25, offset: 0 };

const countFields: Array<[string, string]> = [
  ["planned", "Planned samples"],
  ["sample_rows", "Sample rows"],
  ["completed", "Completed samples"],
  ["wait", "WAIT samples"],
  ["errors", "Errors"],
  ["actionable", "Actionable"],
  ["resolved_actionable", "Resolved actionable"],
  ["outcomes", "Outcomes"],
];

const samplingFields: Array<[string, string]> = [
  ["samples", "Requested samples"],
  ["resume", "Resume enabled"],
  ["execution", "Execution mode"],
  ["min_history_bars", "Minimum history bars"],
  ["deterministic_seed", "Deterministic seed"],
  ["order", "Sampling order"],
  ["news_history_available", "Historical news available"],
];

const configFields: Array<[string, string]> = [
  ["samples", "Configured samples"],
  ["seed", "Seed"],
  ["db_path", "Database path"],
  ["manifest_path", "Manifest path"],
  ["requested_via", "Requested via"],
  ["execution", "CLI workflow"],
];

function resourceState<T>(resource: AsyncResource<T>, empty: boolean): PanelState {
  if (resource.status === "idle" || resource.status === "loading") return "loading";
  if (resource.status === "unavailable") return "unavailable";
  return empty ? "empty" : "ready";
}

function factsFrom(record: JsonRecord | undefined, fields: Array<[string, string]>): ResearchFact[] {
  if (!record) return [];
  return fields.flatMap(([key, label]) => Object.prototype.hasOwnProperty.call(record, key)
    ? [{ label, value: primitiveText(record[key]) }]
    : []);
}

function joined(values: unknown): string {
  return Array.isArray(values) && values.every((value) => typeof value === "string") && values.length > 0
    ? values.join(", ")
    : "Not supplied";
}

function replayStatusClass(status: ReplayStatus): string {
  return `replay-status replay-status--${status.toLowerCase().replaceAll("_", "-")}`;
}

function statusNote(run: ReplayRun): string {
  if (run.status === "PENDING") {
    return "PENDING is stored metadata only. API-created requests do not self-execute and no worker start is implied.";
  }
  if (run.status === "RUNNING") {
    return "RUNNING is the returned stored status. This read-only page does not manage or schedule replay execution.";
  }
  if (run.status === "COMPLETED_WITH_ERRORS") {
    return "The run completed with one or more sample errors; inspect returned error fields and sample coverage below.";
  }
  if (run.status === "FAILED") {
    return "The run is recorded as failed. Replay Lab exposes the returned error provenance but cannot restart it.";
  }
  return "The run is recorded as completed. Counts and sample rows below are the returned evidence.";
}

function sampleCapabilities(sample: ReplaySample): string {
  const flags = sample.capability_flags;
  if (!flags) return "Not supplied";
  const values: string[] = [];
  if (typeof flags.news_history_available === "boolean") {
    values.push(`Historical news: ${flags.news_history_available ? "available" : "not available"}`);
  }
  if (typeof flags.technical_only === "boolean") {
    values.push(`Technical-only: ${flags.technical_only ? "yes" : "no"}`);
  }
  return values.length > 0 ? values.join(" · ") : "Not supplied";
}

function detailState(run: ReplayRun | undefined, resource: AsyncResource<ReplayRun | undefined>, selected: boolean): PanelState {
  if (!selected) return "empty";
  const baseState = resourceState(resource, !run);
  if (baseState !== "ready") return baseState;
  return run?.status === "COMPLETED_WITH_ERRORS" || run?.status === "FAILED" || Boolean(run?.error_code)
    ? "degraded"
    : "ready";
}

function ReplayRunDetail({ run }: { run: ReplayRun }) {
  const countFacts = factsFrom(run.counts, countFields);
  const samplingFacts = factsFrom(run.sampling_policy, samplingFields);
  const configFacts = factsFrom(run.config, configFields);
  const samples = run.samples ?? [];

  return (
    <div className="record-detail replay-detail">
      <div className="record-detail__heading">
        <div>
          <p className="eyebrow">Replay run provenance</p>
          <h3>{run.run_id}</h3>
        </div>
        <span className={replayStatusClass(run.status)}>{run.status}</span>
      </div>

      <p className="replay-status-note" role="status">{statusNote(run)}</p>

      <ResearchFacts
        facts={[
          { label: "Run id", value: run.run_id },
          { label: "Status", value: run.status },
          { label: "Created at", value: timestampText(run.created_at) },
          { label: "Completed at", value: timestampText(run.completed_at) },
          { label: "Model", value: primitiveText(run.model_id) },
          { label: "Prompt version", value: primitiveText(run.prompt_version) },
          { label: "Symbols", value: joined(run.symbols) },
          { label: "Timeframes", value: joined(run.timeframes) },
          { label: "Manifest hash", value: primitiveText(run.manifest_hash) },
          { label: "Error code", value: primitiveText(run.error_code) },
        ]}
      />

      <section className="record-detail__section" aria-labelledby="replay-counts-title">
        <p className="subsection-label" id="replay-counts-title">Returned counts</p>
        {countFacts.length > 0
          ? <ResearchFacts compact facts={countFacts} />
          : <p className="panel-reading">No count fields were supplied.</p>}
      </section>

      <section className="record-detail__section replay-detail__split" aria-label="Replay configuration provenance">
        <div>
          <p className="subsection-label">Sampling policy</p>
          {samplingFacts.length > 0
            ? <ResearchFacts compact facts={samplingFacts} />
            : <p className="panel-reading">No allowlisted sampling policy fields were supplied.</p>}
        </div>
        <div>
          <p className="subsection-label">Run configuration</p>
          {configFacts.length > 0
            ? <ResearchFacts compact facts={configFacts} />
            : <p className="panel-reading">No allowlisted configuration fields were supplied.</p>}
        </div>
      </section>

      <section className="record-detail__section" aria-labelledby="sample-coverage-title">
        <p className="subsection-label" id="sample-coverage-title">Sample coverage and status</p>
        {samples.length > 0 ? (
          <div className="table-wrap" tabIndex={0} aria-label="Scrollable replay sample coverage">
            <table className="replay-sample-table">
              <thead>
                <tr><th scope="col">Route / as of</th><th scope="col">Status</th><th scope="col">Prediction</th><th scope="col">Capabilities</th><th scope="col">Timing</th></tr>
              </thead>
              <tbody>
                {samples.map((sample, index) => (
                  <tr key={`${sample.symbol ?? "unknown"}-${sample.timeframe ?? "unknown"}-${sample.as_of ?? index}`}>
                    <td><span className="data-strong">{primitiveText(sample.symbol)} · {primitiveText(sample.timeframe)}</span><span className="data-meta">As of {timestampText(sample.as_of)}</span></td>
                    <td><span className="data-strong">{primitiveText(sample.status)}</span><span className="data-meta">Error: {primitiveText(sample.error_code)}</span></td>
                    <td>{primitiveText(sample.prediction_id)}</td>
                    <td>{sampleCapabilities(sample)}</td>
                    <td><span className="data-meta">Started {timestampText(sample.started_at)}</span><span className="data-meta">Completed {timestampText(sample.completed_at)}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <p className="panel-reading">No replay sample rows were returned for this run.</p>}
      </section>
    </div>
  );
}

export function ReplayLabPage({ apiClient }: ReplayLabPageProps) {
  const [statusDraft, setStatusDraft] = useState<ReplayStatus | "">("");
  const [limit, setLimit] = useState(25);
  const [filters, setFilters] = useState<ReplayRunFilters>(initialFilters);
  const [selectedId, setSelectedId] = useState<string>();

  const listLoader = useCallback((signal: AbortSignal) => apiClient.replayRuns(filters, signal), [apiClient, filters]);
  const runs = useAsyncResource(listLoader);
  const detailLoader = useCallback(
    (signal: AbortSignal) => selectedId ? apiClient.replayRun(selectedId, signal) : Promise.resolve(undefined),
    [apiClient, selectedId],
  );
  const detail = useAsyncResource(detailLoader, Boolean(selectedId));
  const runList = runs.data ?? [];

  const requestLabel = useMemo(() => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined) query.set(key, String(value));
    }
    return `GET /replay/runs?${query.toString()}`;
  }, [filters]);

  const applyFilters = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFilters({ limit, offset: 0, ...(statusDraft ? { status: statusDraft } : {}) });
    setSelectedId(undefined);
  };

  const movePage = (direction: -1 | 1) => {
    const currentOffset = filters.offset ?? 0;
    setFilters((current) => ({ ...current, offset: Math.max(0, currentOffset + direction * (current.limit ?? limit)) }));
    setSelectedId(undefined);
  };

  return (
    <section className="workflow-page replay-page" aria-labelledby="replay-title">
      <header className="page-intro">
        <p className="eyebrow">Replay provenance / advanced read-only</p>
        <h1 id="replay-title">Replay lab</h1>
        <p className="page-intro__description">Inspect recorded replay runs, configuration provenance and sample coverage without mutating replay state.</p>
        <p className="page-boundary">Replay Lab is read-only. API-created requests do not self-execute; actual replay execution remains an explicit CLI workflow.</p>
      </header>

      <div className="capability-ledger capability-ledger--warning" role="note" aria-label="Replay execution boundary">
        <span className="capability-ledger__label">Execution boundary</span>
        <p>No scheduler or background worker is controlled here. There is no create, start, resume or retry-run action on this page.</p>
      </div>

      <form className="workflow-filters" aria-label="Replay run filters" onSubmit={applyFilters}>
        <div className="workflow-filters__grid replay-filters__grid">
          <label>
            Run status
            <select value={statusDraft} onChange={(event) => setStatusDraft(replayStatuses.includes(event.target.value as ReplayStatus) ? event.target.value as ReplayStatus : "")}>
              <option value="">All statuses</option>
              {replayStatuses.map((status) => <option key={status} value={status}>{status}</option>)}
            </select>
          </label>
          <label>
            Rows
            <select value={limit} onChange={(event) => setLimit(Number(event.target.value))}>
              {pageLimits.map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
          </label>
        </div>
        <button className="primary-button" type="submit">Apply filters</button>
      </form>

      <div className="workflow-grid workflow-grid--records">
        <AsyncPanel
          className="workflow-panel workflow-panel--list"
          title="Replay runs"
          source={requestLabel}
          freshness="current stored run metadata"
          state={resourceState(runs, runList.length === 0)}
          error={runs.error}
          onRetry={runs.retry}
          emptyMessage="No replay runs matched the current status filter."
        >
          {runList.length > 0 ? (
            <div className="table-wrap" tabIndex={0} aria-label="Scrollable replay run list">
              <table className="replay-run-table" aria-label="Replay runs">
                <thead><tr><th scope="col">Run</th><th scope="col">Status</th><th scope="col">Scope</th><th scope="col">Model / prompt</th><th scope="col">Timing</th></tr></thead>
                <tbody>
                  {runList.map((run) => (
                    <tr key={run.run_id}>
                      <td><button className="record-link" type="button" onClick={() => setSelectedId(run.run_id)}>{run.run_id}</button></td>
                      <td><span className={replayStatusClass(run.status)}>{run.status}</span>{run.error_code ? <span className="data-meta">Error: {run.error_code}</span> : null}</td>
                      <td><span className="data-strong">{joined(run.symbols)}</span><span className="data-meta">{joined(run.timeframes)}</span></td>
                      <td><span className="data-strong">{primitiveText(run.model_id)}</span><span className="data-meta">{primitiveText(run.prompt_version)}</span></td>
                      <td><span className="data-meta">Created {timestampText(run.created_at)}</span><span className="data-meta">Completed {timestampText(run.completed_at)}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          <div className="pagination-controls" aria-label="Replay run pagination">
            <span>Offset {filters.offset ?? 0} · maximum {filters.limit ?? limit} rows</span>
            <div>
              <button className="quiet-button" type="button" disabled={(filters.offset ?? 0) === 0} onClick={() => movePage(-1)}>Previous</button>
              <button className="quiet-button" type="button" disabled={runList.length < (filters.limit ?? limit)} onClick={() => movePage(1)}>Next</button>
            </div>
          </div>
        </AsyncPanel>

        <AsyncPanel
          className="workflow-panel workflow-panel--detail"
          title="Replay run detail"
          source={selectedId ? `GET /replay/runs/${selectedId}` : "GET /replay/runs/{run_id}"}
          freshness="returned run and sample timestamps"
          state={detailState(detail.data, detail, Boolean(selectedId))}
          error={detail.error}
          onRetry={detail.retry}
          emptyMessage="Select a replay run to inspect its allowlisted provenance and sample coverage."
          degradedMessage="This run returned an error-bearing status. Other replay records remain available."
        >
          {detail.data ? <ReplayRunDetail run={detail.data} /> : null}
        </AsyncPanel>
      </div>
    </section>
  );
}
