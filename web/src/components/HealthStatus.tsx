import type { HealthResponse } from "../api/types";
import { useI18n, type TranslationKey } from "../i18n";

export type BackendHealthState = "loading" | "connected" | "unavailable";

interface HealthStatusProps {
  state: BackendHealthState;
  health?: HealthResponse;
}

const stateCopy: Record<BackendHealthState, { label: TranslationKey; description: TranslationKey }> = {
  loading: {
    label: "common.checkingBackend",
    description: "common.apiHealthRequest",
  },
  connected: {
    label: "common.backendConnected",
    description: "common.apiHealthAvailable",
  },
  unavailable: {
    label: "common.backendUnavailable",
    description: "common.apiHealthUnknown",
  },
};

export function HealthStatus({ state, health }: HealthStatusProps) {
  const { t } = useI18n();
  const copy = stateCopy[state];
  return (
    <section className={`health-status health-status--${state}`} aria-labelledby="backend-status-title">
      <div className="health-status__heading">
        <span className="health-status__dot" aria-hidden="true" />
        <h2 id="backend-status-title">{t("common.backendStatus")}</h2>
      </div>
      <p className="health-status__label" role="status" aria-live="polite">
        {t(copy.label)}
      </p>
      <p className="health-status__description">{t(copy.description)}</p>
      {state === "connected" && health ? (
        <p className="health-status__meta">
          API v{health.api_version} · {t("common.phase")} {health.phase}
        </p>
      ) : null}
    </section>
  );
}
