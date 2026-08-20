import { useCallback, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import type { ApplicationShellApiClient } from "../api/client";
import type { Instrument } from "../api/types";
import { AsyncPanel, type PanelState } from "../components/AsyncPanel";
import { ResearchFacts, primitiveText } from "../components/ResearchFacts";
import { useAsyncResource } from "../hooks/useAsyncResource";

interface WatchlistPageProps {
  apiClient: ApplicationShellApiClient;
}

function supplied(value: unknown): boolean {
  return typeof value === "string" && value.trim().length > 0;
}

function instrumentSearchText(instrument: Instrument): string {
  return [
    instrument.symbol,
    instrument.asset_type,
    instrument.exchange,
    instrument.currency,
    instrument.quote_currency,
    instrument.timezone,
    instrument.trading_hours,
    instrument.sector,
  ].filter((value): value is string => typeof value === "string").join(" ").toLocaleLowerCase();
}

function incompleteInstrument(instrument: Instrument): boolean {
  return !supplied(instrument.symbol)
    || !supplied(instrument.asset_type)
    || !supplied(instrument.exchange)
    || !supplied(instrument.currency)
    || !supplied(instrument.timezone)
    || !supplied(instrument.trading_hours);
}

function rosterState(
  status: "idle" | "loading" | "ready" | "unavailable",
  loadedCount: number,
  visibleCount: number,
  degraded: boolean,
): PanelState {
  if (status === "idle" || status === "loading") return "loading";
  if (status === "unavailable") return "unavailable";
  if (loadedCount === 0 || visibleCount === 0) return "empty";
  return degraded ? "degraded" : "ready";
}

export function WatchlistPage({ apiClient }: WatchlistPageProps) {
  const [query, setQuery] = useState("");
  const [assetType, setAssetType] = useState("");
  const loader = useCallback((signal: AbortSignal) => apiClient.instruments(signal), [apiClient]);
  const roster = useAsyncResource(loader);
  const instruments = roster.data ?? [];

  const assetTypes = useMemo(() => Array.from(new Set(
    instruments
      .map((instrument) => instrument.asset_type)
      .filter((value): value is string => supplied(value)),
  )).sort((left, right) => left.localeCompare(right)), [instruments]);

  const visibleInstruments = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    return instruments.filter((instrument) => (
      (!assetType || instrument.asset_type === assetType)
      && (!normalizedQuery || instrumentSearchText(instrument).includes(normalizedQuery))
    ));
  }, [assetType, instruments, query]);

  const isDegraded = visibleInstruments.some(incompleteInstrument);
  const state = rosterState(roster.status, instruments.length, visibleInstruments.length, isDegraded);
  const emptyMessage = instruments.length === 0
    ? "The instrument endpoint returned no roster records."
    : "No instruments match the current search and asset-type filter.";

  return (
    <section className="workflow-page watchlist-page" aria-labelledby="watchlist-title">
      <header className="page-intro">
        <p className="eyebrow">Instrument index / read-only</p>
        <h1 id="watchlist-title">Watchlist</h1>
        <p className="page-intro__description">
          Search the instrument universe returned by the local API and open an Asset Detail workspace for market evidence.
        </p>
        <p className="page-boundary">
          Saved membership, radar ranking, background scans and alerts are not active. This browser does not persist a watchlist.
        </p>
      </header>

      <div className="capability-ledger" role="note" aria-label="Watchlist capability boundary">
        <span className="capability-ledger__label">Phase 4 boundary</span>
        <p>GET /instruments supplies this roster. Persistence, CRUD, scanning, opportunity scores, scheduling and alerts remain Phase 5 work.</p>
      </div>

      <form className="workflow-filters" aria-label="Instrument roster filters" onSubmit={(event) => event.preventDefault()}>
        <div className="workflow-filters__grid watchlist-filters__grid">
          <label>
            Search roster
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Symbol, exchange, sector…"
            />
          </label>
          <label>
            Asset type
            <select value={assetType} onChange={(event) => setAssetType(event.target.value)}>
              <option value="">All asset types</option>
              {assetTypes.map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
          </label>
        </div>
      </form>

      <div className="workflow-grid">
        <AsyncPanel
          className="workflow-panel--list"
          title="Instrument roster"
          source="GET /instruments"
          freshness="current API response; no saved membership"
          state={state}
          error={roster.error}
          onRetry={roster.retry}
          emptyMessage={emptyMessage}
          degradedMessage="The roster is usable, but one or more returned instruments is missing descriptive metadata."
        >
          {visibleInstruments.length > 0 ? (
            <>
              <p className="roster-count" role="status">
                Showing {visibleInstruments.length} of {instruments.length} returned instruments.
              </p>
              <ul className="watchlist-roster" aria-label="Read-only instrument roster">
                {visibleInstruments.map((instrument) => (
                  <li key={instrument.symbol} className="watchlist-roster__item">
                    <article>
                      <header className="watchlist-roster__heading">
                        <div>
                          <p className="eyebrow">{primitiveText(instrument.asset_type)}</p>
                          <h3>{instrument.symbol}</h3>
                        </div>
                        <Link to={`/assets/${encodeURIComponent(instrument.symbol)}`}>Open asset detail</Link>
                      </header>
                      <ResearchFacts
                        compact
                        facts={[
                          { label: "Exchange", value: primitiveText(instrument.exchange) },
                          { label: "Currency", value: primitiveText(instrument.currency || instrument.quote_currency) },
                          { label: "Timezone", value: primitiveText(instrument.timezone) },
                          { label: "Trading hours", value: primitiveText(instrument.trading_hours) },
                          { label: "Sector", value: primitiveText(instrument.sector) },
                        ]}
                      />
                    </article>
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </AsyncPanel>
      </div>
    </section>
  );
}
