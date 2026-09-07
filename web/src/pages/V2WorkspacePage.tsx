import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { apiClient } from "../api/client";
import type {
  MarketBar,
  MarketIntelligenceResponse,
  MonitoringRuntimeStatus,
} from "../api/types";
import { OhlcvChart } from "../components/OhlcvChart";
import { V2News } from "../components/V2News";
import { useV2Copy } from "../v2copy";
import { useI18n } from "../i18n";

type StrategyId =
  | "ema_trend"
  | "bollinger_squeeze"
  | "liquidity_sweep"
  | "session_vwap"
  | "opening_range_breakout"
  | "funding_extreme";

interface Subscription {
  symbol: string;
  strategy_id: StrategyId;
  enabled: boolean;
  params: Record<string, number>;
}

interface Ledger {
  decision_id?: string;
  position_id?: string;
  symbol: string;
  status: string;
  created_at: string;
  reason?: string;
}

interface MacroEvent {
  event_id: string;
  title: string;
  event_time: string;
  previous: string | null;
  forecast: string | null;
  actual: string | null;
  source_url: string;
  directive: string;
  directive_expires_at: string;
  ai_summary?: string;
  operational_advice?: string;
  importance_stars?: number;
}

interface Workspace {
  macro_events?: MacroEvent[];
  watchlist: { symbol: string }[];
  subscriptions: Subscription[];
  runtime: MonitoringRuntimeStatus;
  decisions: Ledger[];
  positions: Ledger[];
  allow_unknown_macro: boolean;
}

const rules = {
  ema_trend: "ema_rule",
  bollinger_squeeze: "squeeze_rule",
  liquidity_sweep: "sweep_rule",
  session_vwap: "vwap_rule",
  opening_range_breakout: "orb_rule",
  funding_extreme: "funding_rule",
} as const;

const strategyIds = Object.keys(rules) as StrategyId[];

export function V2WorkspacePage({
  surface = "dashboard",
}: {
  surface?: "dashboard" | "monitor" | "strategies" | "intel" | "ledger";
}) {
  const copy = useV2Copy();
  const { formatNumber, text, language } = useI18n();
  const zh = language === "zh-CN";

  const [workspace, setWorkspace] = useState<Workspace>();
  const [market, setMarket] = useState<MarketIntelligenceResponse>();
  const [bars, setBars] = useState<MarketBar[]>([]);
  const [selected, setSelected] = useState("BTCUSDT");
  const [timeframe, setTimeframeValue] = useState<"15m" | "1h">("15m");
  const setTimeframe = (value: string) =>
    setTimeframeValue(value === "1h" ? "1h" : "15m");

  const [symbol, setSymbol] = useState("");
  const [strategy, setStrategy] = useState<StrategyId>("ema_trend");
  const [newsFilter, setNewsFilter] = useState<"all" | "bull" | "bear" | "macro">("all");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const [state, data, chart] = await Promise.all([
          apiClient.v2<Workspace>("/workspace", "GET", undefined, signal),
          apiClient.marketIntelligence(signal),
          apiClient.chartBars(selected, timeframe, 240, signal),
        ]);
        if (signal?.aborted) return;
        setWorkspace(state);
        setMarket(data);
        setBars(chart.bars);
        setError("");
      } catch (e) {
        if (!signal?.aborted)
          setError(e instanceof Error ? e.message : "unavailable");
      }
    },
    [selected, timeframe],
  );

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    const timer = setInterval(() => void refresh(controller.signal), 15000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [refresh]);

  async function action(task: () => Promise<unknown>) {
    setBusy(true);
    try {
      await task();
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "unavailable");
    } finally {
      setBusy(false);
    }
  }

  // Sentiment Barometer Calculations
  const newsItems = market?.news.items ?? [];
  const bullCount = newsItems.filter(
    (n) => n.macro_policy === "BULLISH_POLICY" || (typeof n.sentiment === "number" && n.sentiment > 0.1)
  ).length;
  const bearCount = newsItems.filter(
    (n) => n.macro_policy === "BEARISH_POLICY" || (typeof n.sentiment === "number" && n.sentiment < -0.1)
  ).length;
  const neutralCount = Math.max(0, newsItems.length - bullCount - bearCount);
  const totalPolarity = bullCount + bearCount;
  const bullRatio = totalPolarity > 0 ? Math.round((bullCount / totalPolarity) * 100) : 50;

  const filteredNews = newsItems.filter((item) => {
    if (newsFilter === "bull")
      return item.macro_policy === "BULLISH_POLICY" || (typeof item.sentiment === "number" && item.sentiment > 0.1);
    if (newsFilter === "bear")
      return item.macro_policy === "BEARISH_POLICY" || (typeof item.sentiment === "number" && item.sentiment < -0.1);
    if (newsFilter === "macro")
      return item.category === "macro" || item.macro_policy === "CIRCUIT_BREAKER" || (item.impact_stars && Number(item.impact_stars) >= 3);
    return true;
  });

  const chart = (
    <section className="terminal-panel">
      <header className="terminal-panel__head">
        <h2>{copy.chart}</h2>
        <div className="v2-controls">
          <select
            aria-label={copy.symbol}
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
          >
            {Array.from(
              new Set([
                selected,
                ...(workspace?.watchlist.map((w) => w.symbol) ?? []),
              ]),
            ).map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
          <select
            aria-label={copy.timeframe}
            value={timeframe}
            onChange={(e) => setTimeframe(e.target.value)}
          >
            <option>15m</option>
            <option>1h</option>
          </select>
        </div>
      </header>
      {bars.length ? (
        <OhlcvChart symbol={selected} timeframe={timeframe} bars={bars} />
      ) : (
        <p className="v2-note">{copy.noChart}</p>
      )}
      <div className="v2-chart-footer">
        <span className="v2-data-tag">
          {selected} · {bars.length} {copy.bars} · 15m/1h Gate.io Swap
        </span>
        <Link className="v2-analyze-link" to={`/consult?symbol=${selected}`}>
          <span aria-hidden="true">✦ </span>{copy.analyze}
        </Link>
      </div>
    </section>
  );

  const macroCalendarCard = (
    <section className="terminal-panel v2-macro-panel">
      <header className="v2-panel-header">
        <h2>🏛️ {copy.macro}</h2>
        <span className="v2-badge v2-badge--gold">3-Factor Engine</span>
      </header>

      <div className="v2-macro-list">
        {workspace?.macro_events && workspace.macro_events.length > 0 ? (
          workspace.macro_events.map((event) => {
            const minutesLeft = Math.floor(
              (Date.parse(event.event_time) - Date.now()) / 60000
            );
            const isReleased = Boolean(event.actual && event.actual !== "待公布");
            const isForbidLong = event.directive === "FORBID_LONG";
            const isForbidShort = event.directive === "FORBID_SHORT";

            return (
              <article className="v2-macro-card" key={event.event_id}>
                <div className="v2-macro-card__top">
                  <span className="v2-macro-title">{event.title}</span>
                  <span className="v2-stars">
                    {"★".repeat(event.importance_stars ?? 3)}
                  </span>
                  <span className={`v2-countdown-badge ${minutesLeft <= 60 && !isReleased ? "v2-countdown-badge--urgent" : ""}`}>
                    {isReleased
                      ? zh ? "已公布" : "Released"
                      : minutesLeft > 0
                      ? `${copy.countdown}: ${minutesLeft} ${copy.minutes}`
                      : zh ? "公布中" : "Just now"}
                  </span>
                </div>

                {/* 3-Factor Previous / Forecast / Actual Grid */}
                <div className="v2-macro-factors">
                  <div className="v2-factor-col">
                    <span className="v2-factor-label">{copy.previous}</span>
                    <strong className="v2-factor-val">{event.previous ?? "—"}</strong>
                  </div>
                  <div className="v2-factor-col">
                    <span className="v2-factor-label">{copy.forecast}</span>
                    <strong className="v2-factor-val">{event.forecast ?? "—"}</strong>
                  </div>
                  <div className={`v2-factor-col ${isReleased ? "v2-factor-col--actual" : ""}`}>
                    <span className="v2-factor-label">{copy.actual}</span>
                    <strong className="v2-factor-val v2-factor-val--highlight">
                      {event.actual ?? (zh ? "待公布" : "Pending")}
                    </strong>
                  </div>
                </div>

                {/* Hard Circuit Breaker Alert Banner */}
                {(isForbidLong || isForbidShort) && (
                  <div className="v2-circuit-banner">
                    <span className="v2-circuit-icon">⚠️</span>
                    <div>
                      <strong>{copy.activeDirective}: {event.directive}</strong>
                      <p>{event.operational_advice || (zh ? "底层硬风控已拦截逆势交易开仓！" : "Counter-trend orders blocked by risk engine.")}</p>
                    </div>
                  </div>
                )}

                {event.ai_summary && (
                  <div className="v2-macro-ai">
                    <span className="v2-macro-ai__label">Qwen 3.5 {zh ? "宏观速评" : "Macro Take"}:</span>
                    <p>{event.ai_summary}</p>
                  </div>
                )}

                <div className="v2-macro-footer">
                  <span className="v2-time-hint">{event.event_time.slice(0, 16).replace("T", " ")}</span>
                  {event.source_url && (
                    <a className="v2-news-link" href={event.source_url} target="_blank" rel="noreferrer">
                      {zh ? "官方数据发布源" : "Release Source"} ↗
                    </a>
                  )}
                </div>
              </article>
            );
          })
        ) : (
          <p className="v2-warning">{copy.noMacro}</p>
        )}
      </div>
    </section>
  );

  const macroBarometer = (
    <section className="terminal-panel v2-barometer-panel">
      <header className="v2-panel-header">
        <h2>📊 {copy.macroBarometer}</h2>
        <span className="v2-data-tag">{newsItems.length} {zh ? "条全网快讯样本" : "samples"}</span>
      </header>
      <div className="v2-barometer-stats">
        <div className="v2-barometer-item v2-barometer-item--bull">
          <span className="v2-barometer-label">🟢 {copy.bullishNews}</span>
          <strong className="v2-barometer-count">{bullCount}</strong>
        </div>
        <div className="v2-barometer-item v2-barometer-item--bear">
          <span className="v2-barometer-label">🔴 {copy.bearishNews}</span>
          <strong className="v2-barometer-count">{bearCount}</strong>
        </div>
        <div className="v2-barometer-item v2-barometer-item--neutral">
          <span className="v2-barometer-label">⚪ {copy.neutralNews}</span>
          <strong className="v2-barometer-count">{neutralCount}</strong>
        </div>
      </div>
      <div className="v2-ratio-bar">
        <div className="v2-ratio-fill--bull" style={{ width: `${bullRatio}%` }} title={`利多比率: ${bullRatio}%`} />
        <div className="v2-ratio-fill--bear" style={{ width: `${100 - bullRatio}%` }} title={`利空比率: ${100 - bullRatio}%`} />
      </div>
      <div className="v2-barometer-stance">
        <span>{copy.macroStance}:</span>
        <strong className={bullRatio >= 60 ? "v2-color--long" : bullRatio <= 40 ? "v2-color--short" : "v2-color--neutral"}>
          {bullRatio >= 60
            ? (zh ? "流动性偏多宽松 · 风险偏好扩张" : "Dovish / Risk-On")
            : bullRatio <= 40
            ? (zh ? "紧缩压力与监管扰动 · 防范插针" : "Hawkish / Cautious")
            : (zh ? "多空分歧势均力敌 · 盘整震荡" : "Neutral / Ranging")}
        </strong>
      </div>
    </section>
  );

  const newsFeed = (
    <section className="terminal-panel">
      <header className="v2-panel-header">
        <h2>📰 {copy.news}</h2>
        <div className="v2-filter-group">
          <button
            className={`v2-filter-btn ${newsFilter === "all" ? "v2-filter-btn--active" : ""}`}
            onClick={() => setNewsFilter("all")}
          >
            {copy.filterAll}
          </button>
          <button
            className={`v2-filter-btn ${newsFilter === "bull" ? "v2-filter-btn--active" : ""}`}
            onClick={() => setNewsFilter("bull")}
          >
            {copy.filterBull}
          </button>
          <button
            className={`v2-filter-btn ${newsFilter === "bear" ? "v2-filter-btn--active" : ""}`}
            onClick={() => setNewsFilter("bear")}
          >
            {copy.filterBear}
          </button>
          <button
            className={`v2-filter-btn ${newsFilter === "macro" ? "v2-filter-btn--active" : ""}`}
            onClick={() => setNewsFilter("macro")}
          >
            {copy.filterMacro}
          </button>
        </div>
      </header>

      {filteredNews.length ? (
        filteredNews.map((n, i) => (
          <V2News
            key={String(n.event_id ?? i)}
            event={n}
            onRefresh={() => void refresh()}
          />
        ))
      ) : (
        <p className="v2-note">{copy.noNews}</p>
      )}
    </section>
  );

  const decisions = (
    <section className="terminal-panel">
      <h2>✦ {copy.decisions}</h2>
      {workspace?.decisions.length ? (
        workspace.decisions.map((d: any) => {
          const proposal = d.proposal ?? {};
          const facts = d.facts ?? {};
          const symbol = d.symbol ?? proposal.symbol ?? "UNKNOWN";
          const side = String(proposal.side ?? proposal.mode ?? "LONG").toUpperCase();
          const isLong = side === "LONG";
          const strategyId = String(proposal.strategy_id ?? "unknown");
          const strategyName = copy[strategyId as keyof typeof copy] ?? strategyId;
          const entry = proposal.entry != null ? Number(proposal.entry).toFixed(2) : "—";
          const stop = proposal.stop != null ? Number(proposal.stop).toFixed(2) : "—";
          const targets = Array.isArray(proposal.targets)
            ? proposal.targets.map((t: number) => Number(t).toFixed(2)).join(" / ")
            : "—";
          const price = facts.price != null ? Number(facts.price).toFixed(2) : "—";
          const time = typeof d.created_at === "string"
            ? d.created_at.slice(11, 19)
            : typeof proposal.generated_at === "string"
            ? proposal.generated_at.slice(11, 19)
            : "—";
          const rationale = proposal.rationale || d.reason || "";
          const verdict = d.verdict ?? "APPROVED";
          const isApproved = verdict === "APPROVED";

          return (
            <article className="v2-decision-card" key={d.decision_id}>
              <div className="v2-position-header">
                <div className="v2-position-title">
                  <strong>{symbol}</strong>
                  <span className={`v2-badge ${isLong ? "v2-badge--bull" : "v2-badge--bear"}`}>
                    {isLong ? "🟢 做多 (LONG)" : "🔴 做空 (SHORT)"}
                  </span>
                  <span className="v2-badge v2-badge--gold">
                    {strategyName}
                  </span>
                  <span className={`v2-badge ${isApproved ? "v2-badge--bull" : "v2-badge--warning"}`}>
                    {isApproved ? "🛡️ Qwen 9B 审查通过" : "⚠️ 审查未通过"}
                  </span>
                </div>
                <time className="v2-news-time">{time}</time>
              </div>

              <div className="v2-position-grid">
                <div className="v2-pos-col">
                  <span className="v2-factor-label">{copy.planPrice}</span>
                  <strong className="v2-pos-val">{entry}</strong>
                </div>
                <div className="v2-pos-col">
                  <span className="v2-factor-label">{copy.currentStop}</span>
                  <strong className="v2-pos-val v2-val--stop">{stop}</strong>
                </div>
                <div className="v2-pos-col">
                  <span className="v2-factor-label">{copy.targetTp}</span>
                  <strong className="v2-pos-val v2-val--tp">{targets}</strong>
                </div>
                <div className="v2-pos-col">
                  <span className="v2-factor-label">{copy.currentPrice}</span>
                  <strong className="v2-pos-val">{price}</strong>
                </div>
              </div>

              {rationale && (
                <div className="v2-decision-rationale">
                  <span className="v2-decision-rationale__tag">{copy.planRationale}:</span>
                  <p className="v2-decision-rationale__text">{rationale}</p>
                </div>
              )}

              <details className="v2-details">
                <summary>{copy.rawDiagnostics}</summary>
                <pre className="v2-pre">{JSON.stringify(d, null, 2)}</pre>
              </details>
            </article>
          );
        })
      ) : (
        <p className="v2-note">{copy.noDecisions}</p>
      )}
    </section>
  );

  const sentinelDaemonArchitecture = (
    <section className="terminal-panel v2-sentinel-panel">
      <header className="v2-panel-header">
        <div>
          <h2>🛡️ {copy.sentinelPipeline}</h2>
          <small className="v2-subtitle">
            {zh
              ? "后台独立常驻守护进程 · WebSocket + 15m 收盘原子判定 · 杜绝漏单"
              : "Background Sentinel Daemon · WebSocket + 15m closed-bar atomic pipeline"}
          </small>
        </div>
        <div className="v2-status-pill">
          <span className="v2-pulse-dot" />
          <span>{copy.worker}: {text(workspace?.runtime.state ?? "running")}</span>
        </div>
      </header>

      {/* 5-Step Pipeline Graphical Flow */}
      <div className="v2-pipeline-flow">
        <div className="v2-pipeline-step">
          <span className="v2-step-num">1</span>
          <span className="v2-step-title">{copy.sentinelStep1}</span>
          <small className="v2-step-sub">Gate.io Swap Ticker</small>
        </div>
        <span className="v2-pipeline-arrow">➔</span>
        <div className="v2-pipeline-step">
          <span className="v2-step-num">2</span>
          <span className="v2-step-title">{copy.sentinelStep2}</span>
          <small className="v2-step-sub">Exactly-once</small>
        </div>
        <span className="v2-pipeline-arrow">➔</span>
        <div className="v2-pipeline-step">
          <span className="v2-step-num">3</span>
          <span className="v2-step-title">{copy.sentinelStep3}</span>
          <small className="v2-step-sub">EMA / Squeeze / SMC</small>
        </div>
        <span className="v2-pipeline-arrow">➔</span>
        <div className="v2-pipeline-step">
          <span className="v2-step-num">4</span>
          <span className="v2-step-title">{copy.sentinelStep4}</span>
          <small className="v2-step-sub">CoT 推演审单</small>
        </div>
        <span className="v2-pipeline-arrow">➔</span>
        <div className="v2-pipeline-step">
          <span className="v2-step-num">5</span>
          <span className="v2-step-title">{copy.sentinelStep5}</span>
          <small className="v2-step-sub">止损必须挂载</small>
        </div>
      </div>
    </section>
  );

  return (
    <div className="v2-workspace">
      <header className="v2-title">
        <h1>{surface === "ledger" ? copy.ledger : copy[surface]}</h1>
        <button className="v2-btn-refresh" disabled={busy} onClick={() => void refresh()}>
          {busy ? "…" : `↻ ${copy.retry}`}
        </button>
      </header>
      <p className="v2-safety">{copy.safety}</p>

      {error && (
        <div role="alert" className="v2-warning">
          {copy.unavailable}
          <details>
            <summary>{copy.evidence}</summary>
            {error}
          </details>
        </div>
      )}

      {/* Surface: Dashboard (综合战情室) */}
      {surface === "dashboard" && (
        <>
          {/* Top 48px Ticker Ribbon */}
          <div className="v2-ticker">
            {market?.pulse.slice(0, 4).map((p) => (
              <article key={p.symbol} className="v2-ticker-item">
                <div className="v2-ticker-item__header">
                  <strong>{p.symbol}</strong>
                  <span className={`v2-ticker-change ${Number(p.change_pct) >= 0 ? "v2-color--long" : "v2-color--short"}`}>
                    {p.change_pct === null
                      ? copy.unknown
                      : `${Number(p.change_pct) > 0 ? "+" : ""}${formatNumber(p.change_pct)}%`}
                  </span>
                </div>
                <b>
                  {p.price === null
                    ? "—"
                    : formatNumber(p.price, { maximumFractionDigits: 2 })}
                </b>
                <span className="v2-ticker-sub">
                  Gate.io · {text(String(p.freshness.status))}
                </span>
              </article>
            ))}
          </div>

          {/* 60% Left / 40% Right Viewport Grid */}
          <div className="v2-grid">
            <div className="v2-grid-col-left">
              {chart}
              {decisions}
            </div>
            <aside className="v2-grid-col-right" aria-label={copy.intel}>
              {macroCalendarCard}
              {macroBarometer}
              {newsFeed}
            </aside>
          </div>
        </>
      )}

      {/* Surface: Monitor (自选与盯盘) */}
      {surface === "monitor" && (
        <>
          {sentinelDaemonArchitecture}

          <section className="terminal-panel">
            <header className="v2-panel-header">
              <h2>◉ {zh ? "自选标的与挂载策略管理" : "Watchlist & Mounted Strategies"}</h2>
              <div className="v2-controls">
                <button
                  className="v2-btn-secondary"
                  disabled={busy}
                  onClick={() => void action(() => apiClient.pauseMonitoring())}
                >
                  ⏸ {copy.pause}
                </button>
                <button
                  className="v2-btn-secondary"
                  disabled={busy}
                  onClick={() => void action(() => apiClient.resumeMonitoring())}
                >
                  ▶ {copy.resume}
                </button>
                <button
                  className="v2-btn-danger"
                  disabled={busy}
                  onClick={() => void action(() => apiClient.stopMonitoring())}
                >
                  ⏹ {copy.stop}
                </button>
              </div>
            </header>

            <form
              className="v2-controls v2-add-form"
              onSubmit={(e) => {
                e.preventDefault();
                void action(() =>
                  apiClient.v2("/watchlist", "POST", { symbol }),
                );
              }}
            >
              <label className="v2-input-label">
                {copy.symbol}:
                <input
                  className="v2-input"
                  placeholder="BTCUSDT / ETHUSDT"
                  value={symbol}
                  onChange={(e) => setSymbol(e.target.value.toUpperCase())}
                  required
                  maxLength={40}
                />
              </label>
              <button className="v2-btn-primary" disabled={busy}>+ {copy.add}</button>
              <label className="v2-input-label">
                {copy.attach}:
                <select
                  className="v2-select"
                  value={strategy}
                  onChange={(e) => setStrategy(e.target.value as StrategyId)}
                >
                  {strategyIds.map((s) => (
                    <option key={s} value={s}>
                      {copy[s]}
                    </option>
                  ))}
                </select>
              </label>
            </form>

            {!workspace?.watchlist.length && <p className="v2-note">{copy.noWatch}</p>}
            <div className="v2-watch-list">
              {workspace?.watchlist.map((w) => (
                <article className="v2-watch-row" key={w.symbol}>
                  <div className="v2-watch-symbol-box">
                    <button className="v2-symbol-btn" onClick={() => setSelected(w.symbol)}>
                      {w.symbol}
                    </button>
                    <span className="v2-badge v2-badge--neutral">Gate.io Swap</span>
                  </div>

                  <label className="v2-checkbox-label">
                    <input
                      type="checkbox"
                      disabled={busy}
                      checked={workspace.subscriptions.some(
                        (s) =>
                          s.symbol === w.symbol &&
                          s.strategy_id === strategy &&
                          s.enabled,
                      )}
                      onChange={(e) =>
                        void action(() =>
                          apiClient.v2(
                            `/subscriptions/${w.symbol}/${strategy}`,
                            "PUT",
                            { enabled: e.target.checked, params: {} },
                          ),
                        )
                      }
                    />
                    <span>{copy.enable} ➔ {copy[strategy]}</span>
                  </label>

                  <button
                    className="v2-btn-remove"
                    disabled={busy}
                    onClick={() =>
                      void action(() =>
                        apiClient.v2(`/watchlist/${w.symbol}`, "DELETE"),
                      )
                    }
                  >
                    ✕ {copy.remove}
                  </button>
                </article>
              ))}
            </div>
          </section>

          {chart}
        </>
      )}

      {/* Surface: Strategies (量化与策略库) */}
      {surface === "strategies" && (
        <>
          <div className="v2-strategies-grid">
            {strategyIds.map((s) => (
              <article className="terminal-panel v2-strategy-card" key={s}>
                <header className="v2-strategy-header">
                  <h2>⌁ {copy[s]}</h2>
                  <span className="v2-badge v2-badge--gold">v2.0.0 · 15m/1h</span>
                </header>
                <p className="v2-strategy-desc">{copy[rules[s]]}</p>
                <div className="v2-strategy-footer">
                  <span className="v2-data-tag">严格输出三要素: 触发价 / 强制止损 / 梯级止盈</span>
                  <Link className="v2-attach-btn" to="/monitor">
                    {copy.attach} ➔
                  </Link>
                </div>
              </article>
            ))}
          </div>
          {decisions}
        </>
      )}

      {/* Surface: Intel (资讯与宏观) */}
      {surface === "intel" && (
        <div className="v2-intel-layout">
          {macroCalendarCard}
          {macroBarometer}
          {newsFeed}
        </div>
      )}

      {/* Surface: Ledger (记账与持仓) */}
      {surface === "ledger" && (
        <>
          <div className="v2-controls v2-ledger-controls">
            <button
              className="v2-btn-danger"
              disabled={busy}
              onClick={() =>
                void action(() => apiClient.v2("/emergency-stop", "POST"))
              }
            >
              🛑 {copy.emergency}
            </button>

            <label className="v2-warning v2-inline-warning">
              <input
                type="checkbox"
                checked={workspace?.allow_unknown_macro ?? false}
                disabled={busy}
                onChange={(e) =>
                  void action(() =>
                    apiClient.v2("/simulation/macro-permission", "PUT", {
                      enabled: e.target.checked,
                    }),
                  )
                }
              />
              <span>{copy.macroPermission}</span>
            </label>
          </div>

          <section className="terminal-panel">
            <h2>💼 {copy.positions}</h2>
            {workspace?.positions.length ? (
              workspace.positions.map((pos) => {
                const p = pos as Record<string, any>;
                const isLong = p.side === "LONG";
                const isClosed = p.status === "CLOSED";
                const pnl = Number(p.realized_pnl ?? 0);
                return (
                  <article className="v2-position-card" key={p.position_id}>
                    <div className="v2-position-header">
                      <div className="v2-position-title">
                        <strong>{p.symbol}</strong>
                        <span className={`v2-badge ${isLong ? "v2-badge--bull" : "v2-badge--bear"}`}>
                          {p.side ?? "POSITION"}
                        </span>
                        <span className={`v2-badge ${isClosed ? "v2-badge--neutral" : "v2-badge--gold"}`}>
                          {text(p.status)}
                        </span>
                      </div>
                      <span className={`v2-position-protect ${p.protected ? "v2-protect--active" : "v2-protect--inactive"}`}>
                        {p.protected ? copy.protected : copy.unprotected}
                      </span>
                    </div>

                    <div className="v2-position-grid">
                      <div className="v2-pos-col">
                        <span className="v2-factor-label">{copy.entryPrice}</span>
                        <strong className="v2-pos-val">{p.entry != null ? Number(p.entry).toFixed(2) : "—"}</strong>
                      </div>
                      <div className="v2-pos-col">
                        <span className="v2-factor-label">{copy.currentStop}</span>
                        <strong className="v2-pos-val v2-val--stop">{p.stop != null ? Number(p.stop).toFixed(2) : "—"}</strong>
                      </div>
                      <div className="v2-pos-col">
                        <span className="v2-factor-label">{copy.targetTp}</span>
                        <strong className="v2-pos-val v2-val--tp">
                          {Array.isArray(p.targets) ? p.targets.map((t: number) => Number(t).toFixed(1)).join(" / ") : "—"}
                        </strong>
                      </div>
                      <div className="v2-pos-col">
                        <span className="v2-factor-label">{copy.contracts}</span>
                        <strong className="v2-pos-val">
                          {p.remaining_contracts != null ? p.remaining_contracts : p.filled_contracts ?? "—"}
                        </strong>
                      </div>
                      <div className="v2-pos-col">
                        <span className="v2-factor-label">{copy.pnl}</span>
                        <strong className={`v2-pos-val ${pnl >= 0 ? "v2-val--bull" : "v2-val--bear"}`}>
                          {pnl >= 0 ? `+${pnl.toFixed(2)}` : pnl.toFixed(2)} USDT
                        </strong>
                      </div>
                    </div>

                    <details className="v2-details">
                      <summary>{copy.rawDiagnostics}</summary>
                      <pre>{JSON.stringify(p, null, 2)}</pre>
                    </details>
                  </article>
                );
              })
            ) : (
              <p className="v2-note">{copy.noPositions}</p>
            )}
          </section>

          {decisions}
        </>
      )}
    </div>
  );
}
