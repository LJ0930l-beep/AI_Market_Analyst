import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { useSearchParams } from "react-router-dom";

import { ApiError, type ApplicationShellApiClient } from "../api/client";
import type {
  ConsultContextEvidence,
  ConsultMessage,
  ConsultStreamEvent,
  Instrument,
  ModelHealthResponse,
} from "../api/types";
import { useI18n, type TranslationKey } from "../i18n";

export const CONSULT_SESSION_KEY = "ai-market-analyst.qwen-consult.v1";
const MAX_SESSION_MESSAGES = 24;
const MAX_MESSAGE_CHARS = 4_000;
const MAX_HISTORY_CHARS = 16_000;

type GenerationStatus = "idle" | "connecting" | "generating" | "stopped" | "complete" | "failed" | "unavailable";
type ModelState = "checking" | "available" | "unavailable" | "failed";

interface DisplayMessage extends ConsultMessage {
  id: string;
}

interface StoredSession {
  messages: ConsultMessage[];
  symbol: string;
}

function messageId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function loadSession(): StoredSession {
  if (typeof window === "undefined") return { messages: [], symbol: "" };
  try {
    const raw = window.sessionStorage.getItem(CONSULT_SESSION_KEY);
    if (!raw) return { messages: [], symbol: "" };
    const parsed = JSON.parse(raw) as unknown;
    if (typeof parsed !== "object" || parsed === null) return { messages: [], symbol: "" };
    const record = parsed as Record<string, unknown>;
    const messages = Array.isArray(record.messages)
      ? record.messages
          .filter((item): item is ConsultMessage => {
            if (typeof item !== "object" || item === null) return false;
            const candidate = item as Record<string, unknown>;
            return (
              (candidate.role === "user" || candidate.role === "assistant") &&
              typeof candidate.content === "string" &&
              candidate.content.length > 0 &&
              candidate.content.length <= MAX_MESSAGE_CHARS
            );
          })
          .slice(-MAX_SESSION_MESSAGES)
      : [];
    const symbol = typeof record.symbol === "string" && record.symbol.length <= 24 ? record.symbol : "";
    return { messages, symbol };
  } catch {
    return { messages: [], symbol: "" };
  }
}

function persistSession(messages: DisplayMessage[], symbol: string): void {
  if (typeof window === "undefined") return;
  const storedMessages = messages
    .filter((message) => message.content.length > 0)
    .slice(-MAX_SESSION_MESSAGES)
    .map(({ role, content }) => ({ role, content }));
  try {
    if (storedMessages.length === 0 && !symbol) {
      window.sessionStorage.removeItem(CONSULT_SESSION_KEY);
      return;
    }
    window.sessionStorage.setItem(CONSULT_SESSION_KEY, JSON.stringify({ messages: storedMessages, symbol }));
  } catch {
    // Session persistence is best effort; the in-memory conversation remains usable.
  }
}

function boundedHistory(messages: ConsultMessage[]): ConsultMessage[] {
  const selected: ConsultMessage[] = [];
  let totalChars = 0;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (!message || totalChars + message.content.length > MAX_HISTORY_CHARS) break;
    selected.unshift(message);
    totalChars += message.content.length;
  }
  return selected;
}

const STATUS_KEYS = {
  idle: "consult.statusIdle",
  connecting: "consult.statusConnecting",
  generating: "consult.statusGenerating",
  stopped: "consult.statusStopped",
  complete: "consult.statusComplete",
  failed: "consult.statusFailed",
  unavailable: "consult.statusUnavailable",
} as const satisfies Record<GenerationStatus, TranslationKey>;

function errorText(error: unknown, fallback: string): string {
  if (error instanceof ApiError) return `${error.code}: ${error.message}`;
  return fallback;
}

export function QwenConsultPage({ apiClient }: { apiClient: ApplicationShellApiClient }) {
  const { language, t } = useI18n();
  const [searchParams, setSearchParams] = useSearchParams();
  const restored = useMemo(loadSession, []);
  const routeSymbol = (searchParams.get("symbol") ?? "").trim().toUpperCase();
  const predictionId = (searchParams.get("prediction_id") ?? "").trim();
  const [messages, setMessages] = useState<DisplayMessage[]>(
    restored.messages.map((message) => ({ ...message, id: messageId() })),
  );
  const [symbol, setSymbolState] = useState(routeSymbol || restored.symbol);
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [instrumentState, setInstrumentState] = useState<"loading" | "ready" | "failed">("loading");
  const [modelState, setModelState] = useState<ModelState>("checking");
  const [modelHealth, setModelHealth] = useState<ModelHealthResponse>();
  const [status, setStatus] = useState<GenerationStatus>("idle");
  const [context, setContext] = useState<ConsultContextEvidence>();
  const [error, setError] = useState<string>();
  const [draft, setDraft] = useState("");
  const [confirmClear, setConfirmClear] = useState(false);
  const [modelPreference, setModelPreference] = useState<"auto" | "fast" | "smart">("auto");
  const [responseLanguage, setResponseLanguage] = useState<"follow_ui" | "en" | "zh-CN">("follow_ui");
  const [actualRoute, setActualRoute] = useState<{ model?: string; tier?: string; reason?: string }>();
  const abortRef = useRef<AbortController | null>(null);
  const logEndRef = useRef<HTMLDivElement | null>(null);

  const checkModel = useCallback(() => {
    const controller = new AbortController();
    setModelState("checking");
    setModelHealth(undefined);
    void apiClient.modelHealth(controller.signal)
      .then((health) => {
        if (controller.signal.aborted) return;
        setModelHealth(health);
        const available = health.consult?.available ?? (health.available === true && health.model_available !== false);
        setModelState(available ? "available" : "unavailable");
        if (!available) setStatus("unavailable");
      })
      .catch((caught: unknown) => {
        if (controller.signal.aborted || (caught instanceof Error && caught.name === "AbortError")) return;
        setModelState("failed");
        setStatus("unavailable");
      });
    return controller;
  }, [apiClient]);

  useEffect(() => {
    const controller = checkModel();
    return () => controller.abort();
  }, [checkModel]);

  useEffect(() => {
    const controller = new AbortController();
    setInstrumentState("loading");
    void apiClient.instruments(controller.signal)
      .then((records) => {
        if (controller.signal.aborted) return;
        setInstruments(records);
        setInstrumentState("ready");
        setSymbolState((currentSymbol) => {
          if (currentSymbol && !records.some((instrument) => instrument.symbol === currentSymbol)) {
            setSearchParams({}, { replace: true });
            return "";
          }
          return currentSymbol;
        });
      })
      .catch((caught: unknown) => {
        if (controller.signal.aborted || (caught instanceof Error && caught.name === "AbortError")) return;
        setInstrumentState("failed");
      });
    return () => controller.abort();
  }, [apiClient, setSearchParams]);

  useEffect(() => {
    const controller = new AbortController();
    void apiClient.appSettings(controller.signal).then((settings) => {
      if (controller.signal.aborted) return;
      const preference = settings.find((setting) => setting.key === "ai.model_preference")?.value;
      const answerLanguage = settings.find((setting) => setting.key === "ai.response_language")?.value;
      if (preference === "auto" || preference === "fast" || preference === "smart") setModelPreference(preference);
      if (answerLanguage === "follow_ui" || answerLanguage === "en" || answerLanguage === "zh-CN") setResponseLanguage(answerLanguage);
    }).catch(() => undefined);
    return () => controller.abort();
  }, [apiClient]);

  useEffect(() => {
    persistSession(messages, symbol);
  }, [messages, symbol]);

  useEffect(() => {
    logEndRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [messages]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const setSymbol = (next: string) => {
    setSymbolState(next);
    setContext(undefined);
    setSearchParams(next ? { symbol: next, ...(predictionId ? { prediction_id: predictionId } : {}) } : {}, { replace: true });
  };

  const stop = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setStatus("stopped");
  };

  const send = async () => {
    const content = draft.trim();
    if (!content || status === "connecting" || status === "generating") return;
    if (modelState !== "available") {
      setStatus("unavailable");
      setError(t("consult.modelUnavailable"));
      return;
    }
    const userMessage: DisplayMessage = { id: messageId(), role: "user", content };
    const assistantId = messageId();
    const assistantMessage: DisplayMessage = { id: assistantId, role: "assistant", content: "" };
    const history = boundedHistory(
      [...messages.filter((message) => message.content), userMessage]
        .slice(-MAX_SESSION_MESSAGES)
        .map(({ role, content: messageContent }) => ({ role, content: messageContent })),
    );
    setMessages((current) => [...current, userMessage, assistantMessage].slice(-(MAX_SESSION_MESSAGES + 1)));
    setDraft("");
    setError(undefined);
    setContext(undefined);
    setStatus("connecting");
    const controller = new AbortController();
    abortRef.current = controller;
    const onEvent = (event: ConsultStreamEvent) => {
      if (event.type === "meta") {
        setContext(event.context);
        setActualRoute({ model: event.model_id, tier: event.model_tier, reason: typeof event.model_route?.reason === "string" ? event.model_route.reason : undefined });
        setStatus("generating");
      } else if (event.type === "delta") {
        setStatus("generating");
        setMessages((current) => current.map((message) => (
          message.id === assistantId ? { ...message, content: message.content + event.content } : message
        )));
      } else if (event.type === "done") {
        setStatus("complete");
      } else {
        const unavailable = ["QWEN_UNAVAILABLE", "QWEN_NOT_CONFIGURED", "QWEN_MODEL_NOT_FOUND"].includes(event.error.code);
        setStatus(unavailable ? "unavailable" : "failed");
        setError(`${event.error.code}: ${event.error.message}`);
      }
    };
    try {
      await apiClient.consultStream(
        {
          language: responseLanguage === "follow_ui" ? language : responseLanguage,
          messages: history,
          model_preference: modelPreference,
          task: predictionId ? "signal_explanation" : "assistant",
          ...(symbol ? { symbol } : {}),
          ...(predictionId ? { prediction_id: predictionId } : {}),
        },
        onEvent,
        controller.signal,
      );
    } catch (caught: unknown) {
      if (controller.signal.aborted || (caught instanceof Error && caught.name === "AbortError")) {
        setStatus("stopped");
      } else {
        const unavailable = caught instanceof ApiError && ["QWEN_UNAVAILABLE", "QWEN_NOT_CONFIGURED", "QWEN_MODEL_NOT_FOUND"].includes(caught.code);
        setStatus(unavailable ? "unavailable" : "failed");
        setError(errorText(caught, t("consult.errorFallback")));
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void send();
  };

  const composerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void send();
    }
  };

  const clear = () => {
    stop();
    setMessages([]);
    setContext(undefined);
    setError(undefined);
    setStatus(modelState === "available" ? "idle" : "unavailable");
    setConfirmClear(false);
    try {
      window.sessionStorage.removeItem(CONSULT_SESSION_KEY);
    } catch {
      // Clearing remains effective in memory when storage is unavailable.
    }
  };

  const statusText = t(STATUS_KEYS[status]);
  const busy = status === "connecting" || status === "generating";
  const capability = modelHealth?.consult;

  return (
    <section className="consult-page" aria-labelledby="consult-title">
      <header className="page-intro">
        <p className="eyebrow">{t("consult.eyebrow")}</p>
        <h1 id="consult-title">{t("consult.title")}</h1>
        <p className="page-intro__description">{t("consult.description")}</p>
        <p className="page-boundary">{t("consult.boundary")}</p>
      </header>

      <section className="consult-capability" aria-labelledby="consult-capability-title">
        <div>
          <h2 id="consult-capability-title">{t("consult.capabilityTitle")}</h2>
          <p className={`consult-state consult-state--${modelState}`} role="status" aria-live="polite">
            {modelState === "checking"
              ? t("consult.modelChecking")
              : modelState === "available"
                ? t("consult.modelAvailable")
                : modelState === "unavailable"
                  ? t("consult.modelUnavailable")
                  : t("consult.modelFailed")}
          </p>
        </div>
        <dl className="fact-list consult-capability__facts">
          <div className="fact-list__row"><dt>{t("common.provider")}</dt><dd data-i18n-skip>{modelHealth?.provider ?? "—"}</dd></div>
          <div className="fact-list__row"><dt>{t("common.model")}</dt><dd data-i18n-skip>{actualRoute?.model ?? capability?.model_id ?? (typeof modelHealth?.model_id === "string" ? modelHealth.model_id : "—")}</dd></div>
          <div className="fact-list__row"><dt>{t("consult.contract")}</dt><dd data-i18n-skip>{capability?.contract_version ?? "qwen_consult_v2"}</dd></div>
          <div className="fact-list__row"><dt>{t("v11.modelPreference")}</dt><dd data-i18n-skip>{modelPreference} · {actualRoute?.tier ?? "pending"} · {actualRoute?.reason ?? "server task policy"}</dd></div>
          <div className="fact-list__row"><dt>{t("consult.sessionStorage")}</dt><dd>{t("consult.sessionOnly")}</dd></div>
        </dl>
        {modelState === "unavailable" || modelState === "failed" ? (
          <button className="quiet-button" type="button" onClick={() => checkModel()}>{t("consult.retryAvailability")}</button>
        ) : null}
      </section>

      <div className="consult-layout">
        <section className="consult-thread" aria-labelledby="consult-conversation-title" aria-busy={busy}>
          <div className="consult-thread__header">
            <div>
              <h2 id="consult-conversation-title">{t("consult.conversationTitle")}</h2>
              <p className="data-meta">{t("consult.restoreNotice")}</p>
            </div>
            <span className={`status-chip consult-status consult-status--${status}`} role="status" aria-live="polite">{statusText}</span>
          </div>

          <div className="consult-symbol-control">
            <label htmlFor="consult-symbol">{t("consult.symbolLabel")}</label>
            <select
              id="consult-symbol"
              value={symbol}
              onChange={(event) => setSymbol(event.target.value)}
              disabled={instrumentState !== "ready" || busy}
            >
              <option value="">{t("consult.noSymbol")}</option>
              {instruments.map((instrument) => (
                <option key={instrument.symbol} value={instrument.symbol}>{instrument.symbol} · {instrument.asset_type}</option>
              ))}
            </select>
            <p className="data-meta">{t("consult.symbolHelp")}</p>
            {instrumentState === "failed" ? <p className="panel-degraded-note" role="alert">{t("common.panelUnavailable")}</p> : null}
            <label htmlFor="consult-model-preference">{t("v11.modelPreference")}</label>
            <select id="consult-model-preference" value={modelPreference} onChange={(event) => setModelPreference(event.target.value as "auto" | "fast" | "smart")} disabled={busy}>
              <option value="auto">{t("v11.auto")}</option><option value="fast">{t("v11.fast")}</option><option value="smart">{t("v11.smart")}</option>
            </select>
          </div>

          <div className="consult-messages" role="log" aria-live="polite" aria-relevant="additions text">
            {messages.length === 0 ? <p className="consult-empty">{t("consult.empty")}</p> : null}
            {messages.map((message) => (
              <article className={`consult-message consult-message--${message.role}`} key={message.id}>
                <p className="consult-message__role">{message.role === "user" ? t("consult.userMessage") : t("consult.assistantMessage")}</p>
                <div className="consult-message__content" data-i18n-skip>
                  {message.content || (message.role === "assistant" && busy ? <span className="consult-caret" aria-hidden="true">▌</span> : null)}
                </div>
              </article>
            ))}
            <div ref={logEndRef} />
          </div>

          {error ? <p className="consult-error" role="alert" data-i18n-skip>{error}</p> : null}

          <form className="consult-composer" onSubmit={submit}>
            <label htmlFor="consult-message">{t("consult.composerLabel")}</label>
            <textarea
              id="consult-message"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={composerKeyDown}
              placeholder={t("consult.placeholder")}
              maxLength={MAX_MESSAGE_CHARS}
              rows={4}
              disabled={busy || modelState !== "available"}
            />
            <div className="consult-composer__footer">
              <span className="data-meta" data-i18n-skip>{draft.length}/{MAX_MESSAGE_CHARS}</span>
              <div className="consult-actions">
                {busy ? <button className="quiet-button" type="button" onClick={stop}>{t("consult.stop")}</button> : null}
                <button className="primary-button" type="submit" disabled={!draft.trim() || busy || modelState !== "available"}>{t("consult.send")}</button>
                <button className="quiet-button" type="button" onClick={() => setConfirmClear(true)} disabled={messages.length === 0 && !error}>{t("consult.clear")}</button>
              </div>
            </div>
          </form>

          {confirmClear ? (
            <div className="consult-clear-confirm" role="group" aria-label={t("consult.clearQuestion")}>
              <p>{t("consult.clearQuestion")}</p>
              <div className="consult-actions">
                <button className="danger-button" type="button" onClick={clear}>{t("consult.clearConfirm")}</button>
                <button className="quiet-button" type="button" onClick={() => setConfirmClear(false)}>{t("consult.cancel")}</button>
              </div>
            </div>
          ) : null}

          <p className="consult-disclaimer">{t("consult.disclaimer")}</p>
        </section>

        <section className="consult-context" aria-labelledby="consult-context-title">
          <h2 id="consult-context-title">{t("consult.contextTitle")}</h2>
          {!context ? <p className="panel-reading">{t("consult.noContextYet")}</p> : (
            <dl className="fact-list" data-i18n-skip>
              <div className="fact-list__row"><dt>{t("consult.contextStatus")}</dt><dd>{context.status ?? "—"}</dd></div>
              <div className="fact-list__row"><dt>{t("common.symbol")}</dt><dd>{context.symbol ?? "—"}</dd></div>
              <div className="fact-list__row"><dt>{t("consult.asOf")}</dt><dd>{context.as_of ?? "—"}</dd></div>
              <div className="fact-list__row"><dt>{t("common.freshness")}</dt><dd>{String(context.freshness?.status ?? "—")}</dd></div>
              <div className="fact-list__row"><dt>{t("consult.sources")}</dt><dd>{context.sources?.join(", ") || "—"}</dd></div>
              <div className="fact-list__row"><dt>{t("consult.missing")}</dt><dd>{context.missing_reasons?.join(", ") || "—"}</dd></div>
            </dl>
          )}
        </section>
      </div>
    </section>
  );
}
