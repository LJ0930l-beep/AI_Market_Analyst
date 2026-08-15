import type { HealthResponse } from "../api/types";

export type BackendHealthState = "loading" | "connected" | "unavailable";

interface HealthStatusProps {
  state: BackendHealthState;
  health?: HealthResponse;
}

const stateCopy: Record<BackendHealthState, { label: string; description: string }> = {
  loading: {
    label: "Checking backend",
    description: "Requesting API health; model and provider status is separate.",
  },
  connected: {
    label: "Backend connected",
    description: "API health is available; model and provider status is separate.",
  },
  unavailable: {
    label: "Backend unavailable",
    description: "The API health request did not complete; model and provider status is unknown.",
  },
};

export function HealthStatus({ state, health }: HealthStatusProps) {
  const copy = stateCopy[state];
  return (
    <section className={`health-status health-status--${state}`} aria-labelledby="backend-status-title">
      <div className="health-status__heading">
        <span className="health-status__dot" aria-hidden="true" />
        <h2 id="backend-status-title">Backend status</h2>
      </div>
      <p className="health-status__label" role="status" aria-live="polite">
        {copy.label}
      </p>
      <p className="health-status__description">{copy.description}</p>
      {state === "connected" && health ? (
        <p className="health-status__meta">
          API v{health.api_version} · phase {health.phase}
        </p>
      ) : null}
    </section>
  );
}
