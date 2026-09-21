import type { HealthResponse, Instrument, JsonRecord, ModelHealthResponse, ProviderHealthResponse, StatsResponse } from "../api/types";
import { Link } from "react-router-dom";
import { useI18n, type TranslationKey } from "../i18n";

function textValue(value: unknown, fallback: string): string {
  if (value === null) {
    return "null";
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value) ?? fallback;
  } catch {
    return fallback;
  }
}

function stringValue(record: JsonRecord, key: string): string | undefined {
  const value = record[key];
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function stringList(record: JsonRecord, key: string): string[] {
  const value = record[key];
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function isBonsaiModelIdentity(value: unknown): boolean {
  if (typeof value !== "string" || !value.trim()) return false;
  let basename = value.trim().replace(/\\/g, "/").split("/").pop() || "";
  if (basename.toLowerCase().endsWith(".gguf")) basename = basename.slice(0, -5);
  if (basename.toLowerCase().startsWith("ternary-")) basename = basename.slice("ternary-".length);
  return basename.toLowerCase() === "bonsai-2-27b-ptq1_0";
}

const countLabels: Record<string, TranslationKey> = {
  instruments: "common.instruments",
  market_snapshots: "common.marketSnapshots",
  predictions: "common.prediction",
  paper_trades: "common.paperTrades",
  outcomes: "common.outcomes",
  news_events: "common.newsEvents",
  provider_snapshots: "common.providerSnapshots",
  model_runs: "common.modelRuns",
  replay_runs: "common.replayRuns",
  replay_samples: "common.replaySamples",
  performance_snapshots: "common.performanceSnapshots",
  calibration_results: "common.calibrationResults",
  calibration_buckets: "common.calibrationBuckets",
  watchlist_entries: "common.watchlistEntries",
  app_settings: "common.appSettings",
  scheduler_runs: "common.schedulerRuns",
  scheduler_items: "common.schedulerItems",
  scheduler_cache_entries: "common.schedulerCacheEntries",
  benchmark_metadata: "common.benchmarkMetadata",
  phase6_events: "common.phase6Events",
  phase6_event_clusters: "common.phase6EventClusters",
  market_memory_features: "common.marketMemoryFeatures",
  alerts: "common.alerts",
  daily_briefs: "common.dailyBriefs",
};

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="fact-list__row">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

export function HealthFacts({ health }: { health: HealthResponse }) {
  const { t, text } = useI18n();
  return (
    <dl className="fact-list">
      <Fact label={t("common.status")} value={text(health.status)} />
      <Fact label={t("common.apiVersion")} value={health.api_version} />
      <Fact label={t("common.phase")} value={String(health.phase)} />
      <Fact label={t("common.realOrders")} value={t(health.real_orders ? "common.yes" : "common.no")} />
      <Fact label={t("common.privateKeys")} value={t(health.private_keys ? "common.yes" : "common.no")} />
    </dl>
  );
}

export function ModelFacts({ model }: { model: ModelHealthResponse }) {
  const { t } = useI18n();
  const availability = model.available === true ? t("common.available") : model.available === false ? t("common.unavailableValue") : t("common.notSuppliedValue");
  const consult = model.consult;
  const consultAvailability = consult?.available === true ? t("common.available") : consult?.available === false ? t("common.unavailableValue") : t("common.notSuppliedValue");
  const actualModelId = stringValue(model, "actual_model_id") ?? stringValue(model, "model_version");
  const actualModelLabel = actualModelId?.replace(/\\/g, "/").split("/").pop();
  const identityVerified =
    model.available === true &&
    model.model_available === true &&
    isBonsaiModelIdentity(stringValue(model, "model_id")) &&
    isBonsaiModelIdentity(actualModelId) &&
    stringValue(model, "model_identity_source") === "verified_manifest";
  const identityStatus = model.available === false || model.model_available === false
    ? t("settings.modelIdentityUnavailable")
    : identityVerified
      ? t("settings.modelIdentityVerified")
      : t("settings.modelIdentityUnverified");
  return (
    <dl className="fact-list">
      <Fact label={t("common.provider")} value={model.provider ?? t("common.notSupplied")} />
      <Fact label={t("common.availability")} value={availability} />
      <Fact label={t("settings.consultModel")} value={stringValue(model, "model_id") ?? t("common.notSupplied")} />
      <Fact label={t("settings.modelIdentity")} value={actualModelLabel ?? t("common.notSupplied")} />
      <Fact label={t("settings.modelIdentityStatus")} value={identityStatus} />
      <Fact label={t("settings.modelIdentitySource")} value={stringValue(model, "model_identity_source") ?? t("common.notSupplied")} />
      {model.error_code ? <Fact label={t("common.errorCode")} value={model.error_code} /> : null}
      {consult ? <>
        <Fact label={t("settings.consultAvailability")} value={consultAvailability} />
        <Fact label={t("settings.consultModel")} value={consult.model_id ?? t("common.notSupplied")} />
        <Fact label={t("settings.consultContract")} value={consult.contract_version ?? t("common.notSupplied")} />
        <Fact label={t("settings.consultStorage")} value={consult.conversation_storage ?? t("common.notSupplied")} />
        <Fact label={t("settings.consultWrites")} value={consult.database_writes === false ? t("settings.consultNoWrites") : t("common.notSuppliedValue")} />
      </> : null}
    </dl>
  );
}

export function ProviderRouting({ provider }: { provider: ProviderHealthResponse }) {
  const { t, text } = useI18n();
  const routes = provider.routes ?? [];
  const news = provider.news;
  return (
    <div className="provider-routing">
      <p className="panel-reading">
        {provider.available === true
          ? t("common.providerRoutingAvailable")
          : provider.available === false
            ? t("common.providerRoutingUnavailable")
            : t("common.providerRoutingUnknown")}
      </p>
      <div className="provider-routing__section">
        <p className="subsection-label">{t("common.marketRoutes")}</p>
        <ul className="route-list">
          {routes.map((route, index) => {
            const symbol = stringValue(route, "symbol") ?? `${t("common.route")} ${index + 1}`;
            const providers = stringList(route, "providers");
            return (
              <li className="route-list__item" key={`${symbol}-${index}`}>
                <strong>{symbol}</strong>
                <span>
                  {text(stringValue(route, "asset_type") ?? t("common.assetTypeNotSupplied"))} · {providers.length > 0 ? providers.join(", ") : t("common.providerNamesNotSupplied")}
                </span>
                <span>{t("common.mode")} {text(stringValue(route, "mode") ?? t("common.notSuppliedValue"))}</span>
              </li>
            );
          })}
        </ul>
      </div>
      <div className="provider-routing__section provider-routing__news">
        <p className="subsection-label">{t("common.newsRoute")}</p>
        <dl className="fact-list fact-list--compact">
          <Fact label={t("common.provider")} value={news ? stringValue(news, "provider") ?? t("common.notSupplied") : t("common.notSupplied")} />
          <Fact label={t("common.configured")} value={news ? typeof news.configured === "boolean" ? t(news.configured ? "common.yes" : "common.no") : textValue(news.configured, t("common.notDisplayable")) : t("common.notSupplied")} />
          <Fact label={t("common.probe")} value={news ? stringValue(news, "probe") ?? t("common.notSupplied") : t("common.notSupplied")} />
        </dl>
      </div>
    </div>
  );
}

export function CountLedger({ stats }: { stats: StatsResponse }) {
  const { t } = useI18n();
  const entries = Object.entries(stats);
  return (
    <dl className="count-ledger">
      {entries.map(([key, value]) => (
        <div className="count-ledger__row" key={key}>
          <dt>{countLabels[key] ? t(countLabels[key]) : key}</dt>
          <dd>{textValue(value, t("common.notDisplayable"))}</dd>
        </div>
      ))}
    </dl>
  );
}

export function InstrumentRoster({ instruments }: { instruments: Instrument[] }) {
  const { t } = useI18n();
  return (
    <ul className="instrument-roster">
      {instruments.map((instrument) => (
        <li className="instrument-roster__item" key={instrument.symbol}>
          <Link to={`/assets/${encodeURIComponent(instrument.symbol)}`}>
            <span className="instrument-roster__symbol">{instrument.symbol}</span>
            <span className="instrument-roster__meta">
              {instrument.asset_type === "equity" ? t("common.assetEquity") : instrument.asset_type === "crypto" ? t("common.assetCrypto") : instrument.asset_type} · {instrument.exchange} · {instrument.sector || t("asset.sectorMissing")}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}
