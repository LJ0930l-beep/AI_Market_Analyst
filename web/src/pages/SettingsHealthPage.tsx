import { useCallback } from "react";

import type { ApplicationShellApiClient } from "../api/client";
import { AsyncPanel, type PanelState } from "../components/AsyncPanel";
import { CountLedger, HealthFacts, ModelFacts, ProviderRouting } from "../components/OperationalFacts";
import type { AsyncResource } from "../hooks/useAsyncResource";
import { useAsyncResource } from "../hooks/useAsyncResource";

interface SettingsHealthPageProps {
  apiClient: ApplicationShellApiClient;
}

function resourceState<T>(resource: AsyncResource<T>, empty: boolean): PanelState {
  if (resource.status === "loading") {
    return "loading";
  }
  if (resource.status === "unavailable") {
    return "unavailable";
  }
  return empty ? "empty" : "ready";
}

export function SettingsHealthPage({ apiClient }: SettingsHealthPageProps) {
  const healthLoader = useCallback((signal: AbortSignal) => apiClient.health(signal), [apiClient]);
  const providerLoader = useCallback((signal: AbortSignal) => apiClient.providerHealth(signal), [apiClient]);
  const modelLoader = useCallback((signal: AbortSignal) => apiClient.modelHealth(signal), [apiClient]);
  const statsLoader = useCallback((signal: AbortSignal) => apiClient.stats(signal), [apiClient]);

  const health = useAsyncResource(healthLoader);
  const provider = useAsyncResource(providerLoader);
  const model = useAsyncResource(modelLoader);
  const stats = useAsyncResource(statsLoader);

  const healthDegraded = health.status === "ready" && health.data?.status !== "ok";
  const providerDegraded = provider.status === "ready" && provider.data?.available === false;
  const modelDegraded = model.status === "ready" && model.data?.available === false;
  const providerHasContent = Boolean(provider.data && ((provider.data.routes?.length ?? 0) > 0 || provider.data.news));
  const modelHasContent = Boolean(model.data && Object.keys(model.data).length > 0);
  const statsHasContent = Boolean(stats.data && Object.keys(stats.data).length > 0);

  return (
    <section className="settings-page" aria-labelledby="settings-health-title">
      <header className="page-intro">
        <p className="eyebrow">Read-only operations / local API</p>
        <h1 id="settings-health-title">Settings / health</h1>
        <p className="page-intro__description">
          Separate backend, market/news routing, model and stored-count signals. This surface has no write controls.
        </p>
        <p className="page-boundary">No secrets, scheduler claims, broker connections or execution controls are collected here.</p>
      </header>

      <div className="settings-grid">
        <AsyncPanel
          title="Backend service"
          source="GET /health"
          freshness="endpoint timestamp not supplied"
          state={healthDegraded ? "degraded" : resourceState(health, false)}
          error={health.error}
          onRetry={health.retry}
          degradedMessage="The backend answered with a non-ok status; provider and model probes remain separate."
        >
          {health.data ? <HealthFacts health={health.data} /> : null}
        </AsyncPanel>

        <AsyncPanel
          title="Market and news routing"
          source="GET /health/providers"
          freshness="probe response; timestamp not supplied"
          state={providerDegraded ? "degraded" : resourceState(provider, !providerHasContent)}
          error={provider.error}
          onRetry={provider.retry}
          emptyMessage="No routing rows or news probe details were returned."
          degradedMessage="Provider routing is degraded. This does not mean the backend itself is unavailable."
        >
          {provider.data ? <ProviderRouting provider={provider.data} /> : null}
        </AsyncPanel>

        <AsyncPanel
          title="Local model"
          source="GET /health/model"
          freshness="health probe response; timestamp not supplied"
          state={modelDegraded ? "degraded" : resourceState(model, !modelHasContent)}
          error={model.error}
          onRetry={model.retry}
          emptyMessage="The model endpoint returned no health fields."
          degradedMessage="Model health is degraded or unavailable while backend health is tracked separately."
        >
          {model.data ? <ModelFacts model={model.data} /> : null}
        </AsyncPanel>

        <AsyncPanel
          title="Database counts"
          source="GET /stats"
          freshness="count response; timestamp not supplied"
          state={resourceState(stats, !statsHasContent)}
          error={stats.error}
          onRetry={stats.retry}
          emptyMessage="No stored-count keys were returned. Database condition is not inferred beyond this response."
        >
          {stats.data ? <CountLedger stats={stats.data} /> : null}
        </AsyncPanel>
      </div>
    </section>
  );
}
