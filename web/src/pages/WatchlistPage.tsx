import { useCallback, useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { ApiError, type ApplicationShellApiClient } from "../api/client";
import type { AssetType, Instrument } from "../api/types";
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

function resourceState(
  status: "idle" | "loading" | "ready" | "unavailable",
  loadedCount: number,
  visibleCount = loadedCount,
  degraded = false,
): PanelState {
  if (status === "idle" || status === "loading") return "loading";
  if (status === "unavailable") return "unavailable";
  if (loadedCount === 0 || visibleCount === 0) return "empty";
  return degraded ? "degraded" : "ready";
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.code}: ${error.message}`;
  }
  return "The watchlist request did not return a usable response.";
}

function InstrumentCard({
  instrument,
  saved,
  busy,
  onToggle,
}: {
  instrument: Instrument;
  saved: boolean;
  busy: boolean;
  onToggle: (symbol: string, saved: boolean) => void;
}) {
  return (
    <li className="watchlist-roster__item">
      <article>
        <header className="watchlist-roster__heading">
          <div>
            <p className="eyebrow">
              {instrument.registry_source === "registered" ? "registered" : "canonical"} · {primitiveText(instrument.asset_type)}
            </p>
            <h3>{instrument.symbol}</h3>
          </div>
          <div className="watchlist-roster__actions">
            <Link to={`/assets/${encodeURIComponent(instrument.symbol)}`}>Open asset detail</Link>
            <button
              className="quiet-button"
              type="button"
              disabled={busy}
              aria-label={`${saved ? "Remove" : "Add"} ${instrument.symbol} ${saved ? "from" : "to"} saved watchlist`}
              onClick={() => onToggle(instrument.symbol, saved)}
            >
              {busy ? "Saving…" : saved ? "Remove" : "Add to saved"}
            </button>
          </div>
        </header>
        <ResearchFacts
          compact
          facts={[
            { label: "Exchange", value: primitiveText(instrument.exchange) },
            { label: "Currency", value: primitiveText(instrument.currency || instrument.quote_currency) },
            { label: "Timezone", value: primitiveText(instrument.timezone) },
            { label: "Trading hours", value: primitiveText(instrument.trading_hours) },
            { label: "Sector", value: primitiveText(instrument.sector) },
            { label: "Metadata", value: primitiveText(instrument.metadata_status) },
            { label: "Validation", value: primitiveText(instrument.validation_provider) },
          ]}
        />
      </article>
    </li>
  );
}

export function WatchlistPage({ apiClient }: WatchlistPageProps) {
  const [query, setQuery] = useState("");
  const [assetType, setAssetType] = useState("");
  const [registrationSymbol, setRegistrationSymbol] = useState("");
  const [registrationAssetType, setRegistrationAssetType] = useState<AssetType>("equity");
  const [registrationStatus, setRegistrationStatus] = useState<"idle" | "loading" | "success">("idle");
  const [registrationMessage, setRegistrationMessage] = useState("");
  const [registrationError, setRegistrationError] = useState<unknown>();
  const [mutationSymbol, setMutationSymbol] = useState<string>();
  const [mutationError, setMutationError] = useState<unknown>();
  const rosterLoader = useCallback((signal: AbortSignal) => apiClient.instruments(signal), [apiClient]);
  const savedLoader = useCallback((signal: AbortSignal) => apiClient.watchlist(signal), [apiClient]);
  const roster = useAsyncResource(rosterLoader);
  const saved = useAsyncResource(savedLoader);
  const instruments = roster.data ?? [];
  const savedEntries = saved.data ?? [];
  const savedSymbols = useMemo(() => new Set(savedEntries.map((entry) => entry.symbol)), [savedEntries]);

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
  const rosterState = resourceState(roster.status, instruments.length, visibleInstruments.length, isDegraded);
  const savedState = resourceState(saved.status, savedEntries.length);
  const emptyMessage = instruments.length === 0
    ? "The instrument registry returned no instruments."
    : "No instruments match the current search and asset-type filter.";

  const handleToggle = useCallback(async (symbol: string, isSaved: boolean) => {
    setMutationSymbol(symbol);
    setMutationError(undefined);
    try {
      if (isSaved) {
        await apiClient.removeWatchlist(symbol);
      } else {
        await apiClient.addWatchlist(symbol);
      }
      saved.retry();
    } catch (error: unknown) {
      setMutationError(error);
    } finally {
      setMutationSymbol(undefined);
    }
  }, [apiClient, saved.retry]);

  const handleRegister = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setRegistrationStatus("loading");
    setRegistrationMessage("");
    setRegistrationError(undefined);
    try {
      const result = await apiClient.registerInstrument(registrationSymbol, registrationAssetType);
      await apiClient.addWatchlist(result.instrument.symbol);
      setRegistrationStatus("success");
      setRegistrationMessage(
        `${result.instrument.symbol} passed provider validation and was saved to the watchlist.`,
      );
      setRegistrationSymbol("");
      roster.retry();
      saved.retry();
    } catch (error: unknown) {
      setRegistrationStatus("idle");
      setRegistrationError(error);
    }
  }, [apiClient, registrationAssetType, registrationSymbol, roster.retry, saved.retry]);

  return (
    <section className="workflow-page watchlist-page" aria-labelledby="watchlist-title">
      <header className="page-intro">
        <p className="eyebrow">Saved membership / canonical Phase 5 foundation</p>
        <h1 id="watchlist-title">Watchlist</h1>
        <p className="page-intro__description">
          Keep durable local membership over the canonical universe and explicitly register additional public symbols for Asset Detail evidence.
        </p>
        <p className="page-boundary">
          Registration performs bounded network validation for public US equities or Binance-compatible USDT spot symbols. Ranking, scans, scheduling and alerts are not active.
        </p>
      </header>

      <div className="capability-ledger" role="note" aria-label="Watchlist capability boundary">
        <span className="capability-ledger__label">Phase 5 foundation boundary</span>
        <p>GET /watchlist and POST/PUT/DELETE /watchlist provide saved membership. POST /instruments/register validates equity or USDT crypto symbols before saving metadata; Radar, scanning, scheduling and alerts remain inactive.</p>
      </div>

      {mutationError ? <p className="watchlist-mutation-error" role="alert">{errorMessage(mutationError)}</p> : null}

      <section className="watchlist-registration" aria-labelledby="watchlist-registration-title">
        <header>
          <p className="eyebrow">Explicit network validation</p>
          <h2 id="watchlist-registration-title">Register an instrument</h2>
          <p>Current scope: public US equity tickers and Binance-compatible USDT spot symbols. Metadata is labeled inferred or unknown; no fixture data, model, news or account access is used for compatibility validation.</p>
        </header>
        <form className="watchlist-registration__form" aria-label="Register instrument" onSubmit={handleRegister}>
          <label>
            Instrument symbol
            <input
              value={registrationSymbol}
              onChange={(event) => setRegistrationSymbol(event.target.value)}
              placeholder="MSFT or SOLUSDT"
              autoComplete="off"
              required
            />
          </label>
          <label>
            Instrument asset type
            <select value={registrationAssetType} onChange={(event) => setRegistrationAssetType(event.target.value as AssetType)}>
              <option value="equity">equity</option>
              <option value="crypto">crypto</option>
            </select>
          </label>
          <button className="quiet-button" type="submit" disabled={registrationStatus === "loading"}>
            {registrationStatus === "loading" ? "Validating…" : "Validate and add"}
          </button>
        </form>
        {registrationError ? <p className="watchlist-mutation-error" role="alert">{errorMessage(registrationError)}</p> : null}
        {registrationStatus === "success" ? <p className="watchlist-registration__success" role="status">{registrationMessage}</p> : null}
      </section>

      <form className="workflow-filters" aria-label="Available instrument filters" onSubmit={(event) => event.preventDefault()}>
        <div className="workflow-filters__grid watchlist-filters__grid">
          <label>
            Search available instruments
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
          className="watchlist-panel"
          title="Saved watchlist"
          source="GET /watchlist"
          freshness="durable local membership; timestamps returned by SQLite"
          state={savedState}
          error={saved.error}
          onRetry={saved.retry}
          emptyMessage="No instruments are saved yet. Add one from the available canonical universe."
          degradedMessage="Saved membership loaded, but one or more instrument records is incomplete."
        >
          {savedEntries.length > 0 ? (
            <ul className="watchlist-roster" aria-label="Saved watchlist entries">
              {savedEntries.map((entry) => (
                <InstrumentCard
                  key={entry.symbol}
                  instrument={entry.instrument}
                  saved
                  busy={mutationSymbol === entry.symbol}
                  onToggle={handleToggle}
                />
              ))}
            </ul>
          ) : null}
        </AsyncPanel>

        <AsyncPanel
          className="workflow-panel--list"
          title="Available instruments"
          source="GET /instruments"
          freshness="canonical plus validated registry response; no ranking"
          state={rosterState}
          error={roster.error}
          onRetry={roster.retry}
          emptyMessage={emptyMessage}
          degradedMessage="The canonical universe is usable, but one or more returned instruments is missing descriptive metadata."
        >
          {visibleInstruments.length > 0 ? (
            <>
              <p className="roster-count" role="status">
                Showing {visibleInstruments.length} of {instruments.length} available instruments.
              </p>
              <ul className="watchlist-roster" aria-label="Available instrument roster">
                {visibleInstruments.map((instrument) => (
                  <InstrumentCard
                    key={instrument.symbol}
                    instrument={instrument}
                    saved={savedSymbols.has(instrument.symbol)}
                    busy={mutationSymbol === instrument.symbol}
                    onToggle={handleToggle}
                  />
                ))}
              </ul>
            </>
          ) : null}
        </AsyncPanel>
      </div>
    </section>
  );
}
