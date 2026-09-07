import { useState } from "react";
import { apiClient } from "../api/client";
import type { IntelligenceEvent } from "../api/types";
import { useI18n } from "../i18n";

export function V2News({
  event,
  onRefresh,
}: {
  event: IntelligenceEvent;
  onRefresh: () => void;
}) {
  const { language } = useI18n();
  const zh = language === "zh-CN";
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const translated = typeof event.title_zh === "string" ? event.title_zh : null;

  async function translate() {
    setBusy(true);
    try {
      const result = await apiClient.v2<{ numeric_guard_passed: boolean }>(
        `/news/${encodeURIComponent(String(event.event_id))}/translate`,
        "POST",
      );
      if (!result.numeric_guard_passed)
        setError(
          zh
            ? "翻译未通过数值一致性校验，保留原文。"
            : "Translation did not pass numeric check; original retained.",
        );
      else onRefresh();
    } catch {
      setError(
        zh
          ? "本地翻译模型响应超时，保留原文。"
          : "Translation model timed out; original retained.",
      );
    } finally {
      setBusy(false);
    }
  }

  const policy = String(event.macro_policy || "NEUTRAL");
  const policyLabel = String(
    event.macro_policy_label || (policy === "BULLISH_POLICY" ? "利多政策" : policy === "BEARISH_POLICY" ? "利空政策" : policy === "CIRCUIT_BREAKER" ? "宏观熔断警戒" : "中性观望")
  );
  let sourceDisplay = String(event.source_display || event.source || (zh ? "海外财经快讯" : "Financial Wire"));
  if (sourceDisplay.includes(".com") || sourceDisplay.includes(".org") || sourceDisplay.includes(".net")) {
    sourceDisplay = sourceDisplay.replace(/^www\./i, "").split("/")[0];
  }
  const stars = Number(event.impact_stars) || (policy === "CIRCUIT_BREAKER" ? 3 : 2);
  const traderTake = typeof event.trader_take === "string" ? event.trader_take : null;

  return (
    <article className={`v2-news-card v2-news-card--${policy.toLowerCase()}`}>
      <div className="v2-news-header">
        <span className="v2-source-tag">{sourceDisplay}</span>
        <span className={`v2-badge ${policy === "BULLISH_POLICY" ? "v2-badge--bull" : policy === "BEARISH_POLICY" ? "v2-badge--bear" : policy === "CIRCUIT_BREAKER" ? "v2-badge--warning" : "v2-badge--neutral"}`}>
          {policy === "BULLISH_POLICY" ? "🟢 " : policy === "BEARISH_POLICY" ? "🔴 " : policy === "CIRCUIT_BREAKER" ? "⚠️ " : "⚪ "}
          {policyLabel}
        </span>
        <span className="v2-stars" title={`影响星级: ${stars}星`}>
          {"★".repeat(stars)}{"☆".repeat(Math.max(0, 3 - stars))}
        </span>
        <time className="v2-news-time">{String(event.published_at ?? "").slice(11, 16)}</time>
      </div>

      <h3 className="v2-news-title">
        {zh && !translated && (
          <span className="v2-news-en-badge" title="海外一手英文快讯，可点击下方 Qwen 翻译">
            [英文原文]
          </span>
        )}
        {zh && translated ? translated : String(event.title ?? "")}
      </h3>

      {Boolean(event.summary_raw) && (
        <p className="v2-news-summary">{String(event.summary_raw)}</p>
      )}

      {traderTake && (
        <div className="v2-trader-take">
          <span className="v2-trader-take__label">{zh ? "交易员快评" : "Trader Take"}:</span> {traderTake}
        </div>
      )}

      <div className="v2-news-footer">
        {zh && !translated && (
          <button className="v2-btn-inline" disabled={busy} onClick={() => void translate()}>
            {busy ? "翻译中…" : "Qwen 翻译 (4B)"}
          </button>
        )}
        {typeof event.url === "string" && event.url.startsWith("https://") && (
          <a className="v2-news-link" href={event.url} target="_blank" rel="noreferrer">
            {zh ? "查看一手原文" : "Primary Source"} ↗
          </a>
        )}
      </div>
      {error && <p className="v2-error-inline" role="status">{error}</p>}
    </article>
  );
}
