import { useId, type ReactNode } from "react";

import { ApiError } from "../api/client";

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

const stateLabels: Record<PanelState, string> = {
  loading: "Loading",
  ready: "Loaded",
  empty: "No records",
  unavailable: "Unavailable",
  degraded: "Degraded",
};

const requestLabels: Record<PanelState, string> = {
  loading: "pending",
  ready: "complete",
  empty: "complete",
  unavailable: "failed",
  degraded: "complete",
};

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.code}: ${error.message}`;
  }
  return "The endpoint did not return a usable response.";
}

function PanelMessage({ state, message, onRetry, title, error }: Pick<AsyncPanelProps, "state" | "onRetry" | "title" | "error"> & { message: string }) {
  const role = state === "unavailable" ? "alert" : "status";
  return (
    <div className={`panel-message panel-message--${state}`} role={role}>
      <p>{message}</p>
      {state === "unavailable" && error ? <p className="panel-message__error">{errorMessage(error)}</p> : null}
      {onRetry ? (
        <button className="quiet-button" type="button" onClick={onRetry}>
          Retry {title}
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
  emptyMessage = "The endpoint returned no records.",
  degradedMessage,
  children,
  className = "",
}: AsyncPanelProps) {
  const titleId = useId();
  return (
    <section className={`async-panel async-panel--${state} ${className}`.trim()} aria-labelledby={titleId}>
      <header className="async-panel__header">
        <div className="async-panel__heading">
          <h2 id={titleId}>{title}</h2>
          <p className="async-panel__source">
            Source <code>{source}</code> · Request: {requestLabels[state]} · Freshness: {freshness}
          </p>
        </div>
        <span className={`panel-state panel-state--${state}`}>{stateLabels[state]}</span>
      </header>
      <div className="async-panel__body">
        {state === "loading" ? <PanelMessage state={state} message="Request in progress." title={title} /> : null}
        {state === "empty" ? (
          <>
            <PanelMessage state={state} message={emptyMessage} onRetry={onRetry} title={title} />
            {children}
          </>
        ) : null}
        {state === "unavailable" ? (
          <PanelMessage
            error={error}
            message="This panel is unavailable. Other workstation panels can remain usable."
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
