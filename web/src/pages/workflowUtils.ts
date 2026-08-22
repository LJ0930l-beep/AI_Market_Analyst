import type { Prediction } from "../api/types";
import type { TranslationKey } from "../i18n";

type Translator = (key: TranslationKey) => string;

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

export function predictionProvenance(prediction: Prediction, t: Translator) {
  const validity = predictionValidity(prediction);
  return {
    generatedAt: prediction.generated_at ?? undefined,
    reevaluateAt: prediction.reevaluate_at ?? undefined,
    expiresAt: prediction.signal_valid_until ?? undefined,
    dataSource: prediction.source_type ? `${t("common.predictionSource")}: ${sourceTypeLabel(prediction.source_type, t)}` : t("common.predictionSourceMissing"),
    model: prediction.model_id ?? t("common.modelMissing"),
    state: validity === "active" || validity === "expired" ? validity : "neutral",
  } as const;
}

export function sourceTypeLabel(sourceType: string | null | undefined, t: Translator): string {
  if (sourceType === "live") return t("common.sourceLive");
  if (sourceType === "replay") return t("common.sourceReplay");
  return sourceType ?? t("common.sourceNotSupplied");
}

export function actionLabel(action: Prediction["action"], t: Translator): string {
  if (action === "LONG") {
    return t("predictions.longDirection");
  }
  if (action === "SHORT") {
    return t("predictions.shortDirection");
  }
  if (action === "WAIT") {
    return t("predictions.waitCoverage");
  }
  return t("common.actionNotSupplied");
}

export function hasReturnedNumber(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}
