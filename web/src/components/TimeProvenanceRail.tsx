import type { ReactNode } from "react";
import { useI18n, type TranslationKey } from "../i18n";

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
  label: TranslationKey;
  timestamp: string | null | undefined;
}

function formatUtcTimestamp(timestamp: string | null | undefined, unavailable: string, notSet: string): string {
  if (!timestamp) {
    return notSet;
  }
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? unavailable : date.toISOString();
}

function stateLabel(state: ProvenanceRailState): TranslationKey {
  if (state === "active") {
    return "common.signalWindowSupplied";
  }
  if (state === "expired") {
    return "common.signalWindowExpired";
  }
  return "common.neutralPlaceholder";
}

export function TimeProvenanceRail({
  generatedAt,
  reevaluateAt,
  expiresAt,
  dataSource,
  model,
  state = "neutral",
  footer,
}: TimeProvenanceRailProps) {
  const { t, text } = useI18n();
  const stages: RailStage[] = [
    { id: "generated", label: "common.generated", timestamp: generatedAt },
    { id: "reevaluate", label: "common.reevaluate", timestamp: reevaluateAt },
    { id: "expiry", label: "common.expiry", timestamp: expiresAt },
  ];

  return (
    <section className="provenance-rail" aria-labelledby="provenance-rail-title">
      <div className="provenance-rail__header">
        <div>
          <p className="eyebrow">{t("shell.timeAndProvenance")}</p>
          <h2 id="provenance-rail-title">{t("shell.signalValidityRail")}</h2>
        </div>
        <span className={`status-chip status-chip--${state}`}>{t(stateLabel(state))}</span>
      </div>

      <ol className="provenance-rail__stages" aria-label={t("common.signalTimeProvenance")}>
        {stages.map((stage) => {
          const value = formatUtcTimestamp(stage.timestamp, t("common.unavailable"), t("common.notSet"));
          return (
            <li
              className={`provenance-stage provenance-stage--${state}`}
              key={stage.id}
              aria-label={`${t(stage.label)}: ${value}`}
            >
              <span className="provenance-stage__marker" aria-hidden="true" />
              <span className="provenance-stage__copy">
                <span className="provenance-stage__label">{t(stage.label)}</span>
                <span className="provenance-stage__value">{value}</span>
                <span className="provenance-stage__status">
                  {stage.timestamp ? t("common.timestampSupplied") : t("common.awaitingSignalData")}
                </span>
              </span>
            </li>
          );
        })}
      </ol>

      <dl className="provenance-rail__sources" aria-label={`${t("common.dataSource")} · ${t("common.model")}`}>
        <div>
          <dt>{t("common.dataSource")}</dt>
          <dd>{dataSource ? text(dataSource) : t("common.notQueriedPlaceholder")}</dd>
        </div>
        <div>
          <dt>{t("common.model")}</dt>
          <dd>{model ? text(model) : t("common.notQueriedPlaceholder")}</dd>
        </div>
      </dl>
      {footer ? <div className="provenance-rail__footer">{footer}</div> : null}
    </section>
  );
}
