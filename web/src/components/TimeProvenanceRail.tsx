import type { ReactNode } from "react";

export type ProvenanceRailState = "neutral" | "active" | "expired";

export interface TimeProvenanceRailProps {
  generatedAt?: string | null;
  reevaluateAt?: string | null;
  expiresAt?: string | null;
  dataSource?: string;
  model?: string;
  state?: ProvenanceRailState;
  footer?: ReactNode;
}

interface RailStage {
  id: "generated" | "reevaluate" | "expiry";
  label: string;
  timestamp: string | null | undefined;
}

function formatUtcTimestamp(timestamp: string | null | undefined): string {
  if (!timestamp) {
    return "Not set";
  }
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? "Unavailable" : date.toISOString();
}

function stateLabel(state: ProvenanceRailState): string {
  if (state === "active") {
    return "Signal window supplied";
  }
  if (state === "expired") {
    return "Signal window expired";
  }
  return "Neutral placeholder";
}

export function TimeProvenanceRail({
  generatedAt,
  reevaluateAt,
  expiresAt,
  dataSource = "Not queried for this placeholder",
  model = "Not queried for this placeholder",
  state = "neutral",
  footer,
}: TimeProvenanceRailProps) {
  const stages: RailStage[] = [
    { id: "generated", label: "Generated", timestamp: generatedAt },
    { id: "reevaluate", label: "Re-evaluate", timestamp: reevaluateAt },
    { id: "expiry", label: "Expiry", timestamp: expiresAt },
  ];

  return (
    <section className="provenance-rail" aria-labelledby="provenance-rail-title">
      <div className="provenance-rail__header">
        <div>
          <p className="eyebrow">Time + provenance</p>
          <h2 id="provenance-rail-title">Signal validity rail</h2>
        </div>
        <span className={`status-chip status-chip--${state}`}>{stateLabel(state)}</span>
      </div>

      <ol className="provenance-rail__stages" aria-label="Signal time provenance">
        {stages.map((stage) => {
          const value = formatUtcTimestamp(stage.timestamp);
          return (
            <li
              className={`provenance-stage provenance-stage--${state}`}
              key={stage.id}
              aria-label={`${stage.label}: ${value}`}
            >
              <span className="provenance-stage__marker" aria-hidden="true" />
              <span className="provenance-stage__copy">
                <span className="provenance-stage__label">{stage.label}</span>
                <span className="provenance-stage__value">{value}</span>
                <span className="provenance-stage__status">
                  {stage.timestamp ? "Timestamp supplied" : "Awaiting signal data"}
                </span>
              </span>
            </li>
          );
        })}
      </ol>

      <dl className="provenance-rail__sources" aria-label="Data and model provenance">
        <div>
          <dt>Data source</dt>
          <dd>{dataSource}</dd>
        </div>
        <div>
          <dt>Model</dt>
          <dd>{model}</dd>
        </div>
      </dl>
      {footer ? <div className="provenance-rail__footer">{footer}</div> : null}
    </section>
  );
}
