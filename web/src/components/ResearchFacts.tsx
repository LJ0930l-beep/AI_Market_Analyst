import type { ReactNode } from "react";

export interface ResearchFact {
  label: string;
  value: ReactNode;
}

export function exactNumberText(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : "Not supplied";
}

export function numberText(value: number | null | undefined, maximumFractionDigits = 4): string {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "Not supplied";
  }
  return new Intl.NumberFormat(undefined, { maximumFractionDigits }).format(value);
}

export function timestampText(value: string | null | undefined): string {
  if (!value) {
    return "Not supplied";
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "Not parseable" : parsed.toISOString();
}

export function primitiveText(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "Not supplied";
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return "Not supplied";
}

export function ResearchFacts({ facts, compact = false }: { facts: ResearchFact[]; compact?: boolean }) {
  const visibleFacts = facts.filter((fact) => fact.value !== undefined);
  return (
    <dl className={`fact-list${compact ? " fact-list--compact" : ""}`}>
      {visibleFacts.map((fact) => (
        <div className="fact-list__row" key={fact.label}>
          <dt>{fact.label}</dt>
          <dd>{fact.value}</dd>
        </div>
      ))}
    </dl>
  );
}
