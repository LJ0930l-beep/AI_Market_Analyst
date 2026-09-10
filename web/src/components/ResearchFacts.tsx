import type { ReactNode } from "react";
import { useI18n } from "../i18n";

export interface ResearchFact {
  label: string;
  value: ReactNode;
}

export function useResearchFormatters() {
  const { formatDateTime, formatNumber, t } = useI18n();
  return {
    exactNumberText(value: unknown): string {
      return typeof value === "number" && Number.isFinite(value)
        ? formatNumber(value, { maximumFractionDigits: 20, useGrouping: false })
        : t("common.notSupplied");
    },
    numberText(value: unknown, maximumFractionDigits = 4): string {
      return typeof value === "number" && Number.isFinite(value)
        ? formatNumber(value, { maximumFractionDigits })
        : t("common.notSupplied");
    },
    timestampText(value: unknown): string {
      if (typeof value !== "string" || !value) return t("common.notSupplied");
      const parsed = new Date(value);
      return Number.isNaN(parsed.getTime())
        ? t("common.notParseable")
        : formatDateTime(parsed, { dateStyle: "medium", timeStyle: "medium", timeZone: "Asia/Hong_Kong" });
    },
    primitiveText(value: unknown): string {
      if (value === null || value === undefined || value === "") return t("common.notSupplied");
      if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
      return t("common.notSupplied");
    },
    booleanText(value: unknown): string {
      return typeof value === "boolean" ? t(value ? "common.yes" : "common.no") : t("common.notSupplied");
    },
  };
}

export function ResearchFacts({ facts, compact = false }: { facts: ResearchFact[]; compact?: boolean }) {
  const visibleFacts = facts.filter((fact) => fact.value !== undefined);
  return (
    <dl className={`fact-list${compact ? " fact-list--compact" : ""}`}>
      {visibleFacts.map((fact, index) => (
        <div className="fact-list__row" key={`${fact.label}-${index}`}>
          <dt>{fact.label}</dt>
          <dd>{fact.value}</dd>
        </div>
      ))}
    </dl>
  );
}
