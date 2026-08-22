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
import { ResearchFacts, useResearchFormatters, type ResearchFact } from "../components/ResearchFacts";
import type { AsyncResource } from "../hooks/useAsyncResource";
import { useAsyncResource } from "../hooks/useAsyncResource";
import { useI18n, type TranslationKey } from "../i18n";

interface ReplayLabPageProps {
  apiClient: ApplicationShellApiClient;
}

const replayStatuses: ReplayStatus[] = ["PENDING", "RUNNING", "COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"];
const pageLimits = [25, 50, 100];
const initialFilters: ReplayRunFilters = { limit: 25, offset: 0 };

const countFields: Array<[string, TranslationKey]> = [
  ["planned", "replay.plannedSamples"],
  ["sample_rows", "replay.sampleRows"],
  ["completed", "replay.completedSamples"],
  ["wait", "replay.waitSamples"],
  ["errors", "replay.errors"],
  ["actionable", "replay.actionable"],
  ["resolved_actionable", "replay.resolvedActionable"],
  ["outcomes", "replay.outcomes"],
];

const samplingFields: Array<[string, TranslationKey]> = [
  ["samples", "replay.requestedSamples"],
  ["resume", "replay.resumeEnabled"],
  ["execution", "replay.executionMode"],
  ["min_history_bars", "replay.minimumHistoryBars"],
  ["deterministic_seed", "replay.deterministicSeed"],
  ["order", "replay.samplingOrder"],
  ["news_history_available", "replay.historicalNewsAvailable"],
];

const configFields: Array<[string, TranslationKey]> = [
  ["samples", "replay.configuredSamples"],
  ["seed", "replay.seed"],
  ["db_path", "replay.databasePath"],
  ["manifest_path", "replay.manifestPath"],
  ["requested_via", "replay.requestedVia"],
  ["execution", "replay.cliWorkflow"],
];

function resourceState<T>(resource: AsyncResource<T>, empty: boolean): PanelState {
  if (resource.status === "idle" || resource.status === "loading") return "loading";
  if (resource.status === "unavailable") return "unavailable";
  return empty ? "empty" : "ready";
}

function factsFrom(record: JsonRecord | undefined, fields: Array<[string, TranslationKey]>, t: ReturnType<typeof useI18n>["t"], primitiveText: (value: unknown) => string): ResearchFact[] {
  if (!record) return [];
  return fields.flatMap(([key, labelKey]) => Object.prototype.hasOwnProperty.call(record, key)
    ? [{ label: t(labelKey), value: primitiveText(record[key]) }]
    : []);
}

function joined(values: unknown, fallback: string): string {
  return Array.isArray(values) && values.every((value) => typeof value === "string") && values.length > 0
    ? values.join(", ")
    : fallback;
}

function replayStatusClass(status: ReplayStatus): string {
  return `replay-status replay-status--${status.toLowerCase().replaceAll("_", "-")}`;
}

function statusNote(run: ReplayRun, t: ReturnType<typeof useI18n>["t"]): string {
  if (run.status === "PENDING") {
    return t("replay.pendingNote");
  }
  if (run.status === "RUNNING") {
    return t("replay.runningNote");
  }
  if (run.status === "COMPLETED_WITH_ERRORS") {
    return t("replay.completedErrorsNote");
  }
  if (run.status === "FAILED") {
    return t("replay.failedNote");
  }
  return t("replay.completedNote");
}

function sampleCapabilities(sample: ReplaySample, t: ReturnType<typeof useI18n>["t"]): string {
  const flags = sample.capability_flags;
  if (!flags) return t("common.notSupplied");
  const values: string[] = [];
  if (typeof flags.news_history_available === "boolean") {
    values.push(`${t("replay.historicalNews")}: ${flags.news_history_available ? t("replay.available") : t("replay.notAvailable")}`);
  }
  if (typeof flags.technical_only === "boolean") {
    values.push(`${t("replay.technicalOnly")}: ${flags.technical_only ? t("replay.yes") : t("replay.no")}`);
  }
  return values.length > 0 ? values.join(" · ") : t("common.notSupplied");
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
  const { t, text } = useI18n();
  const { primitiveText, timestampText } = useResearchFormatters();
  const countFacts = factsFrom(run.counts, countFields, t, primitiveText);
  const samplingFacts = factsFrom(run.sampling_policy, samplingFields, t, primitiveText);
  const configFacts = factsFrom(run.config, configFields, t, primitiveText);
  const samples = run.samples ?? [];

  return (
    <div className="record-detail replay-detail">
      <div className="record-detail__heading">
        <div>
          <p className="eyebrow">{t("replay.provenance")}</p>
          <h3>{run.run_id}</h3>
        </div>
        <span className={replayStatusClass(run.status)}>{text(run.status)}</span>
      </div>

      <p className="replay-status-note" role="status">{statusNote(run, t)}</p>

      <ResearchFacts
        facts={[
          { label: t("replay.runId"), value: run.run_id },
          { label: t("common.status"), value: text(run.status) },
          { label: t("replay.createdAt"), value: timestampText(run.created_at) },
          { label: t("replay.completedAt"), value: timestampText(run.completed_at) },
          { label: t("common.model"), value: primitiveText(run.model_id) },
          { label: t("replay.promptVersion"), value: primitiveText(run.prompt_version) },
          { label: t("replay.symbols"), value: joined(run.symbols, t("common.notSupplied")) },
          { label: t("replay.timeframes"), value: joined(run.timeframes, t("common.notSupplied")) },
          { label: t("replay.manifestHash"), value: primitiveText(run.manifest_hash) },
          { label: t("replay.errorCode"), value: primitiveText(run.error_code) },
        ]}
      />

      <section className="record-detail__section" aria-labelledby="replay-counts-title">
        <p className="subsection-label" id="replay-counts-title">{t("replay.returnedCounts")}</p>
        {countFacts.length > 0
          ? <ResearchFacts compact facts={countFacts} />
          : <p className="panel-reading">{t("replay.noCountFields")}</p>}
      </section>

      <section className="record-detail__section replay-detail__split" aria-label={t("replay.configurationProvenance")}>
        <div>
          <p className="subsection-label">{t("replay.samplingPolicy")}</p>
          {samplingFacts.length > 0
            ? <ResearchFacts compact facts={samplingFacts} />
            : <p className="panel-reading">{t("replay.noSamplingFields")}</p>}
        </div>
        <div>
          <p className="subsection-label">{t("replay.runConfiguration")}</p>
          {configFacts.length > 0
            ? <ResearchFacts compact facts={configFacts} />
            : <p className="panel-reading">{t("replay.noConfigFields")}</p>}
        </div>
      </section>

      <section className="record-detail__section" aria-labelledby="sample-coverage-title">
        <p className="subsection-label" id="sample-coverage-title">{t("replay.sampleCoverage")}</p>
        {samples.length > 0 ? (
          <div className="table-wrap" tabIndex={0} aria-label={t("replay.scrollSamples")}>
            <table className="replay-sample-table">
              <thead>
                <tr><th scope="col">{t("replay.routeAsOf")}</th><th scope="col">{t("common.status")}</th><th scope="col">{t("common.prediction")}</th><th scope="col">{t("replay.capabilities")}</th><th scope="col">{t("replay.timing")}</th></tr>
              </thead>
              <tbody>
                {samples.map((sample, index) => (
                  <tr key={`${sample.symbol ?? "unknown"}-${sample.timeframe ?? "unknown"}-${sample.as_of ?? index}`}>
                    <td><span className="data-strong">{primitiveText(sample.symbol)} · {primitiveText(sample.timeframe)}</span><span className="data-meta">{t("replay.asOf")} {timestampText(sample.as_of)}</span></td>
                    <td><span className="data-strong">{text(primitiveText(sample.status))}</span><span className="data-meta">{t("common.error")}: {primitiveText(sample.error_code)}</span></td>
                    <td>{primitiveText(sample.prediction_id)}</td>
                    <td>{sampleCapabilities(sample, t)}</td>
                    <td><span className="data-meta">{t("replay.started")} {timestampText(sample.started_at)}</span><span className="data-meta">{t("replay.completed")} {timestampText(sample.completed_at)}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <p className="panel-reading">{t("replay.noSampleRows")}</p>}
      </section>
    </div>
  );
}

export function ReplayLabPage({ apiClient }: ReplayLabPageProps) {
  const { t, text } = useI18n();
  const { primitiveText, timestampText } = useResearchFormatters();
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
        <p className="eyebrow">{t("replay.eyebrow")}</p>
        <h1 id="replay-title">{t("replay.title")}</h1>
        <p className="page-intro__description">{t("replay.description")}</p>
        <p className="page-boundary">{t("replay.boundary")}</p>
      </header>

      <div className="capability-ledger capability-ledger--warning" role="note" aria-label={t("replay.executionBoundary")}>
        <span className="capability-ledger__label">{t("replay.executionBoundary")}</span>
        <p>{t("replay.executionNote")}</p>
      </div>

      <form className="workflow-filters" aria-label={t("replay.filters")} onSubmit={applyFilters}>
        <div className="workflow-filters__grid replay-filters__grid">
          <label>
            {t("replay.runStatus")}
            <select value={statusDraft} onChange={(event) => setStatusDraft(replayStatuses.includes(event.target.value as ReplayStatus) ? event.target.value as ReplayStatus : "")}>
              <option value="">{t("replay.allStatuses")}</option>
              {replayStatuses.map((status) => <option key={status} value={status}>{text(status)}</option>)}
            </select>
          </label>
          <label>
            {t("replay.rows")}
            <select value={limit} onChange={(event) => setLimit(Number(event.target.value))}>
              {pageLimits.map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
          </label>
        </div>
        <button className="primary-button" type="submit">{t("common.applyFilters")}</button>
      </form>

      <div className="workflow-grid workflow-grid--records">
        <AsyncPanel
          className="workflow-panel workflow-panel--list"
          title={t("replay.runs")}
          source={requestLabel}
          freshness={t("replay.currentFreshness")}
          state={resourceState(runs, runList.length === 0)}
          error={runs.error}
          onRetry={runs.retry}
          emptyMessage={t("replay.noRuns")}
        >
          {runList.length > 0 ? (
            <div className="table-wrap" tabIndex={0} aria-label={t("replay.scrollRuns")}>
              <table className="replay-run-table" aria-label={t("replay.runs")}>
                <thead><tr><th scope="col">{t("common.run")}</th><th scope="col">{t("common.status")}</th><th scope="col">{t("replay.scope")}</th><th scope="col">{t("replay.modelPrompt")}</th><th scope="col">{t("replay.timing")}</th></tr></thead>
                <tbody>
                  {runList.map((run) => (
                    <tr key={run.run_id}>
                      <td><button className="record-link" type="button" onClick={() => setSelectedId(run.run_id)}>{run.run_id}</button></td>
                      <td><span className={replayStatusClass(run.status)}>{text(run.status)}</span>{run.error_code ? <span className="data-meta">{t("common.error")}: {run.error_code}</span> : null}</td>
                      <td><span className="data-strong">{joined(run.symbols, t("common.notSupplied"))}</span><span className="data-meta">{joined(run.timeframes, t("common.notSupplied"))}</span></td>
                      <td><span className="data-strong">{primitiveText(run.model_id)}</span><span className="data-meta">{primitiveText(run.prompt_version)}</span></td>
                      <td><span className="data-meta">{t("replay.created")} {timestampText(run.created_at)}</span><span className="data-meta">{t("replay.completed")} {timestampText(run.completed_at)}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          <div className="pagination-controls" aria-label={t("replay.pagination")}>
            <span>{t("common.offset")} {filters.offset ?? 0} · {t("common.maximum")} {filters.limit ?? limit} {t("common.rowsValue")}</span>
            <div>
              <button className="quiet-button" type="button" disabled={(filters.offset ?? 0) === 0} onClick={() => movePage(-1)}>{t("common.previous")}</button>
              <button className="quiet-button" type="button" disabled={runList.length < (filters.limit ?? limit)} onClick={() => movePage(1)}>{t("common.next")}</button>
            </div>
          </div>
        </AsyncPanel>

        <AsyncPanel
          className="workflow-panel workflow-panel--detail"
          title={t("replay.detail")}
          source={selectedId ? `GET /replay/runs/${selectedId}` : "GET /replay/runs/{run_id}"}
          freshness={t("replay.detailFreshness")}
          state={detailState(detail.data, detail, Boolean(selectedId))}
          error={detail.error}
          onRetry={detail.retry}
          emptyMessage={t("replay.selectDetail")}
          degradedMessage={t("replay.degraded")}
        >
          {detail.data ? <ReplayRunDetail run={detail.data} /> : null}
        </AsyncPanel>
      </div>
    </section>
  );
}
