import { useCallback, useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { ApiError, type ApplicationShellApiClient } from "../api/client";
import type { AssetType, Instrument } from "../api/types";
import { AsyncPanel, type PanelState } from "../components/AsyncPanel";
import { ResearchFacts, useResearchFormatters } from "../components/ResearchFacts";
import { useAsyncResource } from "../hooks/useAsyncResource";
import { useI18n } from "../i18n";

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

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    return `${error.code}: ${error.message}`;
  }
  return fallback;
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
  const { t } = useI18n();
  const { primitiveText } = useResearchFormatters();
  return (
    <li className="watchlist-roster__item">
      <article>
        <header className="watchlist-roster__heading">
          <div>
            <p className="eyebrow">
              {instrument.registry_source === "registered" ? t("watchlist.registered") : t("watchlist.canonical")} · {instrument.asset_type === "equity" ? t("common.assetEquity") : instrument.asset_type === "crypto" ? t("common.assetCrypto") : primitiveText(instrument.asset_type)}
            </p>
            <h3>{instrument.symbol}</h3>
          </div>
          <div className="watchlist-roster__actions">
            <Link to={`/assets/${encodeURIComponent(instrument.symbol)}`}>{t("watchlist.openAsset")}</Link>
            <button
              className="quiet-button"
              type="button"
              disabled={busy}
              aria-label={`${saved ? t("watchlist.remove") : t("common.add")} ${instrument.symbol} ${saved ? t("common.fromSavedWatchlist") : t("common.toSavedWatchlist")}`}
              onClick={() => onToggle(instrument.symbol, saved)}
            >
              {busy ? t("watchlist.saving") : saved ? t("watchlist.remove") : t("watchlist.addToSaved")}
            </button>
          </div>
        </header>
        <ResearchFacts
          compact
          facts={[
            { label: t("watchlist.exchange"), value: primitiveText(instrument.exchange) },
            { label: t("watchlist.currency"), value: primitiveText(instrument.currency || instrument.quote_currency) },
            { label: t("watchlist.timezone"), value: primitiveText(instrument.timezone) },
            { label: t("watchlist.tradingHours"), value: primitiveText(instrument.trading_hours) },
            { label: t("watchlist.sector"), value: primitiveText(instrument.sector) },
            { label: t("watchlist.metadata"), value: primitiveText(instrument.metadata_status) },
            { label: t("watchlist.validation"), value: primitiveText(instrument.validation_provider) },
          ]}
        />
      </article>
    </li>
  );
}

export function WatchlistPage({ apiClient }: WatchlistPageProps) {
  const { t } = useI18n();
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
    ? t("watchlist.noRegistry")
    : t("watchlist.noMatch");

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
        `${result.instrument.symbol} ${t("watchlist.registrationSuccess")}`,
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
        <p className="eyebrow">{t("watchlist.eyebrow")}</p>
        <h1 id="watchlist-title">{t("nav.watchlist")}</h1>
        <p className="page-intro__description">
          {t("watchlist.description")}
        </p>
        <p className="page-boundary">
          {t("watchlist.boundary")}
        </p>
      </header>

      <div className="capability-ledger" role="note" aria-label={t("watchlist.capabilityLabel")}>
        <span className="capability-ledger__label">{t("watchlist.capabilityLabel")}</span>
        <p>{t("watchlist.capability")}</p>
      </div>

      {mutationError ? <p className="watchlist-mutation-error" role="alert">{errorMessage(mutationError, t("watchlist.requestFailed"))}</p> : null}

      <section className="watchlist-registration" aria-labelledby="watchlist-registration-title">
        <header>
          <p className="eyebrow">{t("watchlist.registrationEyebrow")}</p>
          <h2 id="watchlist-registration-title">{t("watchlist.register")}</h2>
          <p>{t("watchlist.registrationScope")}</p>
        </header>
        <form className="watchlist-registration__form" aria-label={t("watchlist.registrationForm")} onSubmit={handleRegister}>
          <label>
            {t("watchlist.instrumentSymbol")}
            <input
              value={registrationSymbol}
              onChange={(event) => setRegistrationSymbol(event.target.value)}
              placeholder="MSFT or SOLUSDT"
              autoComplete="off"
              required
            />
          </label>
          <label>
            {t("watchlist.instrumentType")}
            <select value={registrationAssetType} onChange={(event) => setRegistrationAssetType(event.target.value as AssetType)}>
              <option value="equity">{t("common.assetEquity")}</option>
              <option value="crypto">{t("common.assetCrypto")}</option>
            </select>
          </label>
          <button className="quiet-button" type="submit" disabled={registrationStatus === "loading"}>
            {registrationStatus === "loading" ? t("watchlist.validating") : t("watchlist.validateAdd")}
          </button>
        </form>
        {registrationError ? <p className="watchlist-mutation-error" role="alert">{errorMessage(registrationError, t("watchlist.requestFailed"))}</p> : null}
        {registrationStatus === "success" ? <p className="watchlist-registration__success" role="status">{registrationMessage}</p> : null}
      </section>

      <form className="workflow-filters" aria-label={t("watchlist.availableFilters")} onSubmit={(event) => event.preventDefault()}>
        <div className="workflow-filters__grid watchlist-filters__grid">
          <label>
            {t("watchlist.search")}
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t("watchlist.searchPlaceholder")}
            />
          </label>
          <label>
            {t("common.assetType")}
            <select value={assetType} onChange={(event) => setAssetType(event.target.value)}>
              <option value="">{t("common.allAssetTypes")}</option>
              {assetTypes.map((value) => <option key={value} value={value}>{value === "equity" ? t("common.assetEquity") : value === "crypto" ? t("common.assetCrypto") : value}</option>)}
            </select>
          </label>
        </div>
      </form>

      <div className="workflow-grid">
        <AsyncPanel
          className="watchlist-panel"
          title={t("watchlist.saved")}
          source="GET /watchlist"
          freshness={t("common.durableMembership")}
          state={savedState}
          error={saved.error}
          onRetry={saved.retry}
          emptyMessage={t("watchlist.noSaved")}
          degradedMessage={t("watchlist.savedDegraded")}
        >
          {savedEntries.length > 0 ? (
            <ul className="watchlist-roster" aria-label={t("watchlist.savedEntries")}>
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
          title={t("watchlist.available")}
          source="GET /instruments"
          freshness={t("common.validatedRegistry")}
          state={rosterState}
          error={roster.error}
          onRetry={roster.retry}
          emptyMessage={emptyMessage}
          degradedMessage={t("watchlist.rosterDegraded")}
        >
          {visibleInstruments.length > 0 ? (
            <>
              <p className="roster-count" role="status">
                {t("watchlist.showing")} {visibleInstruments.length} {t("watchlist.of")} {instruments.length} {t("watchlist.availableCount")}
              </p>
              <ul className="watchlist-roster" aria-label={t("watchlist.availableRoster")}>
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
