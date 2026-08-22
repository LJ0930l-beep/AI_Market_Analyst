import { useId, type ReactNode } from "react";

import { ApiError } from "../api/client";
import { useI18n, type TranslationKey } from "../i18n";

export type PanelState = "loading" | "ready" | "empty" | "unavailable" | "degraded";

interface AsyncPanelProps {
  title: string;
  source: string;
  freshness: string;
  state: PanelState;
  onRetry?: () => void;
  error?: unknown;
  emptyMessage?: string;
  degradedMessage?: string;
  children?: ReactNode;
  className?: string;
}

const stateLabels: Record<PanelState, TranslationKey> = {
  loading: "common.loading",
  ready: "common.loaded",
  empty: "common.noRecords",
  unavailable: "common.unavailable",
  degraded: "common.degraded",
};

const requestLabels: Record<PanelState, TranslationKey> = {
  loading: "common.pending",
  ready: "common.complete",
  empty: "common.complete",
  unavailable: "common.failed",
  degraded: "common.complete",
};

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    return `${error.code}: ${error.message}`;
  }
  return fallback;
}

function PanelMessage({ state, message, onRetry, title, error }: Pick<AsyncPanelProps, "state" | "onRetry" | "title" | "error"> & { message: string }) {
  const { t } = useI18n();
  const role = state === "unavailable" ? "alert" : "status";
  return (
    <div className={`panel-message panel-message--${state}`} role={role}>
      <p>{message}</p>
      {state === "unavailable" && error ? <p className="panel-message__error">{errorMessage(error, t("common.endpointUnusable"))}</p> : null}
      {onRetry ? (
        <button className="quiet-button" type="button" onClick={onRetry}>
          {t("common.retry")} {title}
        </button>
      ) : null}
    </div>
  );
}

export function AsyncPanel({
  title,
  source,
  freshness,
  state,
  onRetry,
  error,
  emptyMessage,
  degradedMessage,
  children,
  className = "",
}: AsyncPanelProps) {
  const { t } = useI18n();
  const titleId = useId();
  const resolvedEmptyMessage = emptyMessage ?? t("common.endpointNoRecords");
  return (
    <section className={`async-panel async-panel--${state} ${className}`.trim()} aria-labelledby={titleId}>
      <header className="async-panel__header">
        <div className="async-panel__heading">
          <h2 id={titleId}>{title}</h2>
          <p className="async-panel__source">
            {t("common.source")} <code>{source}</code> · {t("common.request")} {t(requestLabels[state])} · {t("common.freshness")} {freshness}
          </p>
        </div>
        <span className={`panel-state panel-state--${state}`}>{t(stateLabels[state])}</span>
      </header>
      <div className="async-panel__body">
        {state === "loading" ? <PanelMessage state={state} message={t("common.requestInProgress")} title={title} /> : null}
        {state === "empty" ? (
          <>
            <PanelMessage state={state} message={resolvedEmptyMessage} onRetry={onRetry} title={title} />
            {children}
          </>
        ) : null}
        {state === "unavailable" ? (
          <PanelMessage
            error={error}
            message={t("common.panelUnavailable")}
            onRetry={onRetry}
            state={state}
            title={title}
          />
        ) : null}
        {state === "degraded" ? (
          <>
            {degradedMessage ? <p className="panel-degraded-note">{degradedMessage}</p> : null}
            {children}
          </>
        ) : null}
        {state === "ready" ? children : null}
      </div>
    </section>
  );
}
