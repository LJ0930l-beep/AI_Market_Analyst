import type { HealthResponse, Instrument, JsonRecord, ModelHealthResponse, ProviderHealthResponse, StatsResponse } from "../api/types";
import { Link } from "react-router-dom";

function textValue(value: unknown): string {
  if (value === null) {
    return "null";
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value) ?? "Value could not be displayed";
  } catch {
    return "Value could not be displayed";
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

function labelForKey(key: string): string {
  return key.replaceAll("_", " ");
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="fact-list__row">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

export function HealthFacts({ health }: { health: HealthResponse }) {
  return (
    <dl className="fact-list">
      <Fact label="Status" value={health.status} />
      <Fact label="API version" value={health.api_version} />
      <Fact label="Phase" value={String(health.phase)} />
      <Fact label="Real orders" value={String(health.real_orders)} />
      <Fact label="Private keys" value={String(health.private_keys)} />
    </dl>
  );
}

export function ModelFacts({ model }: { model: ModelHealthResponse }) {
  const availability = model.available === true ? "available" : model.available === false ? "unavailable" : "not supplied";
  return (
    <dl className="fact-list">
      <Fact label="Provider" value={model.provider ?? "Not supplied"} />
      <Fact label="Availability" value={availability} />
      {model.error_code ? <Fact label="Error code" value={model.error_code} /> : null}
    </dl>
  );
}

export function ProviderRouting({ provider }: { provider: ProviderHealthResponse }) {
  const routes = provider.routes ?? [];
  const news = provider.news;
  return (
    <div className="provider-routing">
      <p className="panel-reading">
        {provider.available === true
          ? "Provider routing reports available."
          : provider.available === false
            ? "Provider routing reports unavailable."
            : "Provider routing availability was not supplied."}
      </p>
      <div className="provider-routing__section">
        <p className="subsection-label">Market routes</p>
        <ul className="route-list">
          {routes.map((route, index) => {
            const symbol = stringValue(route, "symbol") ?? `Route ${index + 1}`;
            const providers = stringList(route, "providers");
            return (
              <li className="route-list__item" key={`${symbol}-${index}`}>
                <strong>{symbol}</strong>
                <span>
                  {stringValue(route, "asset_type") ?? "Asset type not supplied"} · {providers.length > 0 ? providers.join(", ") : "Provider names not supplied"}
                </span>
                <span>Mode: {stringValue(route, "mode") ?? "not supplied"}</span>
              </li>
            );
          })}
        </ul>
      </div>
      <div className="provider-routing__section provider-routing__news">
        <p className="subsection-label">News route</p>
        <dl className="fact-list fact-list--compact">
          <Fact label="Provider" value={news ? stringValue(news, "provider") ?? "Not supplied" : "Not supplied"} />
          <Fact label="Configured" value={news ? textValue(news.configured) : "Not supplied"} />
          <Fact label="Probe" value={news ? stringValue(news, "probe") ?? "Not supplied" : "Not supplied"} />
        </dl>
      </div>
    </div>
  );
}

export function CountLedger({ stats }: { stats: StatsResponse }) {
  const entries = Object.entries(stats);
  return (
    <dl className="count-ledger">
      {entries.map(([key, value]) => (
        <div className="count-ledger__row" key={key}>
          <dt>{labelForKey(key)}</dt>
          <dd>{textValue(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

export function InstrumentRoster({ instruments }: { instruments: Instrument[] }) {
  return (
    <ul className="instrument-roster">
      {instruments.map((instrument) => (
        <li className="instrument-roster__item" key={instrument.symbol}>
          <Link to={`/assets/${encodeURIComponent(instrument.symbol)}`}>
            <span className="instrument-roster__symbol">{instrument.symbol}</span>
            <span className="instrument-roster__meta">
              {instrument.asset_type} · {instrument.exchange} · {instrument.sector || "Sector not supplied"}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}
