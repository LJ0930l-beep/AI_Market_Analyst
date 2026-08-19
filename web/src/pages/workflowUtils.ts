import type { Prediction } from "../api/types";

export function parseTimestamp(value: string | null | undefined): Date | undefined {
  if (!value) {
    return undefined;
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? undefined : parsed;
}

export function predictionValidity(prediction: Prediction): "active" | "expired" | "unknown" {
  const expiry = parseTimestamp(prediction.signal_valid_until);
  if (!expiry) {
    return "unknown";
  }
  return expiry.getTime() <= Date.now() ? "expired" : "active";
}

export function predictionProvenance(prediction: Prediction) {
  const validity = predictionValidity(prediction);
  return {
    generatedAt: prediction.generated_at ?? undefined,
    reevaluateAt: prediction.reevaluate_at ?? undefined,
    expiresAt: prediction.signal_valid_until ?? undefined,
    dataSource: prediction.source_type ? `Prediction source: ${prediction.source_type}` : "Prediction source not supplied",
    model: prediction.model_id ?? "Model not supplied",
    state: validity === "active" || validity === "expired" ? validity : "neutral",
  } as const;
}

export function actionLabel(action: Prediction["action"]): string {
  if (action === "LONG") {
    return "LONG · actionable direction";
  }
  if (action === "SHORT") {
    return "SHORT · actionable direction";
  }
  if (action === "WAIT") {
    return "WAIT · retained for coverage only";
  }
  return "Action not supplied";
}

export function hasReturnedNumber(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}
