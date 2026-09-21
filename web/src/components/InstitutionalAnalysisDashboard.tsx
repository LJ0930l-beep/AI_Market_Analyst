import type { CSSProperties, ReactNode } from "react";

export interface InstitutionalDashboard {
  status: "EMPTY" | "AVAILABLE" | "DEGRADED" | string;
  scope?: {
    account_id?: string;
    provider?: string;
    environment?: string;
    mode?: string;
    venue?: string;
    currency?: string;
  };
  data_quality?: {
    is_sample?: boolean;
    source?: string;
    status?: string;
    reason?: string;
    counts?: Record<string, number>;
    fee_unknown_count?: number;
    unknown_fields?: string[];
    note_zh?: string;
    capital_basis_status?: string;
    capital_observed_at?: string | null;
    capital_stale?: boolean | null;
  };
  empty_state?: { code?: string; message_zh?: string; message_en?: string } | null;
  account?: {
    initial_capital_usdt?: number | null;
    initial_capital_basis?: string | null;
    initial_capital_observed_at?: string | null;
    current_equity_usdt?: number | null;
    available_margin_usdt?: number | null;
    used_margin_usdt?: number | null;
    realized_pnl_usdt?: number | null;
    unrealized_pnl_usdt?: number | null;
    net_pnl_usdt?: number | null;
    cumulative_fees_usdt?: number | null;
    cumulative_fees_basis?: string | null;
    total_roi_pct?: number | null;
    max_drawdown_pct?: number | null;
    local_execution_net_pnl_usdt?: number | null;
    equity_basis?: string;
    capital_source?: string | null;
    provider_source?: string | null;
    truth_observed_at?: string | null;
    truth_age_seconds?: number | null;
    truth_stale?: boolean;
    truth_error_code?: string | null;
  };
  equity_drawdown?: { series?: Array<{ time: string; equity_usdt?: number | null; drawdown_pct?: number | null }>; max_drawdown_pct?: number | null };
  realized_pnl_bars?: Array<{ time: string; pnl_usdt?: number | null }>;
  strategy_bars?: Array<{ strategy_id: string; name: string; trade_count?: number; sample_size?: number; win_count?: number; win_rate_pct?: number | null; pnl_usdt?: number | null }>;
  decision_mix?: Array<{ label: string; count: number }>;
  heatmap?: { weekday?: Array<{ key: number; label: string; count: number; pnl_usdt?: number | null }>; session?: Array<{ key: string; label: string; count: number; pnl_usdt?: number | null }> };
  candles?: Array<{ time: string; symbol?: string; open?: number | null; high?: number | null; low?: number | null; close?: number | null; markers?: Array<{ type: string; price?: number | null; candidate_id?: string; trade_id?: string; intent_id?: string }> }>;
  timeline?: Array<{ cycle_id?: string; action?: string; status?: string; reason?: string; scheduled_at?: string | null; completed_at?: string | null; candidate_count?: number; calibration_state?: string; details?: unknown }>;
  details?: { fills?: unknown[]; orders?: unknown[]; positions?: unknown[]; candidates?: unknown[]; cycles?: unknown[]; memory?: unknown[] };
}

interface Props {
  dashboard: InstitutionalDashboard | null;
  loading?: boolean;
  error?: string;
  zh?: boolean;
}

type EquitySeries = NonNullable<NonNullable<InstitutionalDashboard["equity_drawdown"]>["series"]>;

const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);

function money(value: number | null | undefined, digits = 2): string {
  return finite(value) ? `${value >= 0 ? "+" : ""}${value.toFixed(digits)} USDT` : "—";
}

function pct(value: number | null | undefined, digits = 1): string {
  return finite(value) ? `${value.toFixed(digits)}%` : "—";
}

function shortTime(value?: string | null): string {
  if (!value) return "—";
  const point = new Date(value);
  return Number.isNaN(point.getTime()) ? value : point.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

function Panel({ title, eyebrow, children, className = "" }: { title: string; eyebrow?: string; children: ReactNode; className?: string }) {
  return (
    <section className={`v2-institutional-panel ${className}`}>
      <header className="v2-institutional-panel__header">
        <div>
          {eyebrow && <span className="v2-institutional-eyebrow">{eyebrow}</span>}
          <h3>{title}</h3>
        </div>
      </header>
      {children}
    </section>
  );
}

function EmptyState({ text, code = "NOT_RUN" }: { text: string; code?: string }) {
  return (
    <div className="v2-institutional-empty" role="status">
      <span className="v2-institutional-empty__code">{code}</span>
      <span>{text}</span>
    </div>
  );
}

function EquityChart({ series, zh }: { series: EquitySeries | undefined; zh: boolean }) {
  const points = (series ?? []).filter((item) => finite(item.equity_usdt));
  if (!points.length) return <EmptyState text={zh ? "尚无可验证权益曲线。" : "No verified equity curve yet."} />;
  const width = 720;
  const height = 205;
  const pad = 22;
  const values = points.map((item) => item.equity_usdt as number);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const polyline = points.map((item, index) => {
    const x = pad + (index / Math.max(1, points.length - 1)) * (width - pad * 2);
    const y = height - pad - ((item.equity_usdt as number - min) / span) * (height - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return (
    <svg className="v2-institutional-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={zh ? "权益与回撤曲线" : "Equity and drawdown chart"}>
      <defs>
        <linearGradient id="institutional-equity-fill" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stopColor="#64e8b0" stopOpacity="0.25" />
          <stop offset="100%" stopColor="#64e8b0" stopOpacity="0" />
        </linearGradient>
      </defs>
      <line x1={pad} y1={height - pad} x2={width - pad} y2={height - pad} className="v2-chart-axis" />
      <polyline points={`${pad},${height - pad} ${polyline} ${width - pad},${height - pad}`} fill="url(#institutional-equity-fill)" stroke="none" />
      <polyline points={polyline} fill="none" stroke="#64e8b0" strokeWidth="3" strokeLinejoin="round" strokeLinecap="round" />
      {points.map((item, index) => {
        const x = pad + (index / Math.max(1, points.length - 1)) * (width - pad * 2);
        const y = height - pad - ((item.equity_usdt as number - min) / span) * (height - pad * 2);
        return <circle key={`${item.time}-${index}`} cx={x} cy={y} r="3" fill="#0c111d" stroke="#64e8b0" strokeWidth="2" />;
      })}
      <text x={pad} y={height - 5} className="v2-chart-label">{shortTime(points[0]?.time)}</text>
      <text x={width - pad} y={height - 5} textAnchor="end" className="v2-chart-label">{shortTime(points[points.length - 1]?.time)}</text>
    </svg>
  );
}

function PnlBars({ bars, zh }: { bars: InstitutionalDashboard["realized_pnl_bars"]; zh: boolean }) {
  const points = (bars ?? []).filter((item) => finite(item.pnl_usdt));
  if (!points.length) return <EmptyState text={zh ? "尚无已实现盈亏事件。" : "No realized PnL events yet."} />;
  const maxAbs = Math.max(1, ...points.map((item) => Math.abs(item.pnl_usdt as number)));
  return (
    <div className="v2-institutional-pnl-bars" role="img" aria-label={zh ? "已实现盈亏柱状图" : "Realized PnL bars"}>
      {points.slice(-20).map((item) => {
        const value = item.pnl_usdt as number;
        return (
          <div className="v2-institutional-pnl-bar" key={item.time} title={`${item.time}: ${money(value)}`}>
            <span className={value >= 0 ? "v2-val--bull" : "v2-val--bear"} style={{ height: `${Math.max(4, Math.abs(value) / maxAbs * 82)}%` }} />
            <small>{shortTime(item.time)}</small>
          </div>
        );
      })}
    </div>
  );
}

function StrategyBars({ bars, zh }: { bars: InstitutionalDashboard["strategy_bars"]; zh: boolean }) {
  const rows = bars ?? [];
  const maxAbs = Math.max(1, ...rows.map((item) => Math.abs(item.pnl_usdt ?? 0)));
  return (
    <div className="v2-institutional-strategy-bars">
      {rows.map((item) => {
        const value = item.pnl_usdt;
        return (
          <div className="v2-institutional-strategy-row" key={item.strategy_id}>
            <div className="v2-institutional-strategy-row__meta"><strong>{item.name}</strong><span>{item.sample_size ?? 0} {zh ? "候选" : "candidates"} · {item.trade_count ?? 0} {zh ? "平仓" : "exits"}</span></div>
            <div className="v2-institutional-strategy-row__track"><span className={finite(value) && value < 0 ? "is-negative" : ""} style={{ width: finite(value) ? `${Math.max(3, Math.abs(value) / maxAbs * 100)}%` : "0%" }} /></div>
            <strong className={finite(value) && value >= 0 ? "v2-val--bull" : finite(value) ? "v2-val--bear" : "v2-dim"}>{finite(value) ? money(value) : "—"}</strong>
          </div>
        );
      })}
    </div>
  );
}

function DecisionDonut({ mix, zh }: { mix: InstitutionalDashboard["decision_mix"]; zh: boolean }) {
  const rows = mix ?? [];
  const total = rows.reduce((sum, item) => sum + Math.max(0, item.count), 0);
  if (!total) return <EmptyState text={zh ? "尚无 AI 周期决策记录。" : "No AI cycle decisions yet."} />;
  const colors = ["#64e8b0", "#73a7ff", "#ffc857", "#c68cff", "#ff7b96", "#8d9bb5"];
  let cursor = 0;
  const stops = rows.map((item, index) => {
    const start = cursor / total * 100;
    cursor += Math.max(0, item.count);
    return `${colors[index % colors.length]} ${start.toFixed(2)}% ${(cursor / total * 100).toFixed(2)}%`;
  }).join(", ");
  return (
    <div className="v2-institutional-donut-wrap">
      <div className="v2-institutional-donut" style={{ background: `conic-gradient(${stops})` }} role="img" aria-label={zh ? "AI 决策结构环图" : "AI decision mix donut"}><span>{total}<small>{zh ? "周期" : "cycles"}</small></span></div>
      <div className="v2-institutional-legend">
        {rows.map((item, index) => <span key={item.label}><i style={{ background: colors[index % colors.length] }} />{item.label} · {item.count}</span>)}
      </div>
    </div>
  );
}

function Heatmap({ heatmap, zh }: { heatmap: InstitutionalDashboard["heatmap"]; zh: boolean }) {
  const rows = [...(heatmap?.weekday ?? []), ...(heatmap?.session ?? [])];
  if (!rows.some((row) => row.count > 0)) return <EmptyState text={zh ? "成交不足，暂不生成时段热力图。" : "Not enough fills for a timing heatmap."} />;
  const max = Math.max(1, ...rows.map((row) => row.count));
  return (
    <div className="v2-institutional-heatmap" aria-label={zh ? "交易时段热力图" : "Trading timing heatmap"}>
      {rows.map((row) => <div className="v2-institutional-heat-cell" key={row.key} style={{ "--heat": `${row.count / max}` } as CSSProperties} title={`${row.label}: ${row.count} fills`}><strong>{row.label}</strong><span>{row.count}</span><small>{finite(row.pnl_usdt) ? money(row.pnl_usdt, 2) : "PnL —"}</small></div>)}
    </div>
  );
}

function CandlestickChart({ candles, zh }: { candles: InstitutionalDashboard["candles"]; zh: boolean }) {
  const rows = (candles ?? []).filter((item) => [item.open, item.high, item.low, item.close].every(finite)).slice(-72);
  if (!rows.length) return <EmptyState text={zh ? "没有足够的合格 15m K 线或图表标的。" : "No qualified 15m candles are available."} />;
  const high = Math.max(...rows.map((row) => row.high as number));
  const low = Math.min(...rows.map((row) => row.low as number));
  const span = high - low || 1;
  const width = 720;
  const height = 230;
  const plotTop = 12;
  const plotBottom = 195;
  const step = width / rows.length;
  const y = (price: number) => plotBottom - (price - low) / span * (plotBottom - plotTop);
  return (
    <svg className="v2-institutional-candle-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={zh ? "15 分钟 K 线及执行标记" : "15 minute candles with execution markers"}>
      {rows.map((row, index) => {
        const x = step * index + step / 2;
        const open = row.open as number;
        const close = row.close as number;
        const bodyTop = y(Math.max(open, close));
        const bodyHeight = Math.max(2, Math.abs(y(open) - y(close)));
        return <g key={`${row.time}-${index}`}><line x1={x} x2={x} y1={y(row.high as number)} y2={y(row.low as number)} className="v2-candle-wick" /><rect x={x - Math.max(1, step * 0.28)} y={bodyTop} width={Math.max(2, step * 0.56)} height={bodyHeight} className={close >= open ? "v2-candle-body is-up" : "v2-candle-body is-down"} />{(row.markers ?? []).map((marker, markerIndex) => <circle key={`${marker.type}-${markerIndex}`} cx={x} cy={finite(marker.price) ? y(marker.price) : y(close)} r="4" className={`v2-candle-marker v2-candle-marker--${marker.type.toLowerCase()}`} />)}</g>;
      })}
      <line x1="0" y1={plotBottom} x2={width} y2={plotBottom} className="v2-chart-axis" />
      <text x="4" y="220" className="v2-chart-label">{shortTime(rows[0]?.time)}</text>
      <text x={width - 4} y="220" textAnchor="end" className="v2-chart-label">{shortTime(rows[rows.length - 1]?.time)}</text>
    </svg>
  );
}

function Timeline({ items, zh }: { items: InstitutionalDashboard["timeline"]; zh: boolean }) {
  if (!items?.length) return <EmptyState text={zh ? "尚无 AI 时间线记录。" : "No AI timeline records yet."} />;
  return (
    <div className="v2-institutional-timeline">
      {items.slice(0, 20).map((item, index) => <details className="v2-institutional-timeline-card" key={item.cycle_id || index}><summary><span className="v2-institutional-timeline-dot" style={{ background: item.action === "SYSTEM_BLOCKED" || item.status === "BLOCKED" ? "#e5b65b" : "#5de4c7" }} /><div><strong>{item.action || "UNKNOWN"}</strong><span style={{ display: "block", color: "#9aabc0", marginTop: 6 }}>{item.reason || (zh ? "无附加原因" : "No reason recorded")}</span><small>{shortTime(item.scheduled_at || item.completed_at)} · {item.status || "UNKNOWN"}</small></div><em>{item.candidate_count ?? 0} candidates</em></summary><p>{item.reason || (zh ? "无附加原因。" : "No reason recorded.")}</p><div className="v2-institutional-timeline-meta"><span>cycle: {item.cycle_id || "—"}</span><span>calibration: {item.calibration_state || "—"}</span><span>scheduled: {shortTime(item.scheduled_at)}</span></div><pre>{JSON.stringify(item.details || {}, null, 2)}</pre></details>)}
    </div>
  );
}

export function InstitutionalAnalysisDashboard({ dashboard, loading = false, error = "", zh = true }: Props) {
  if (loading && !dashboard) return <section className="v2-institutional-dashboard"><div className="v2-institutional-loading" role="status">{zh ? "正在读取账户作用域账本…" : "Reading scoped execution ledger…"}</div></section>;
  if (!dashboard) return <section className="v2-institutional-dashboard"><EmptyState text={error || (zh ? "分析看板暂不可用。" : "Analysis dashboard unavailable.")} code="UNAVAILABLE" /></section>;
  const status = String(dashboard.status || "EMPTY").toUpperCase();
  const account = dashboard.account || {};
  const counts = dashboard.data_quality?.counts || {};
  const empty = dashboard.empty_state;
  // A managed Gate account whose remote facts were never observed reports no
  // capital at all — the local seed balance is deliberately not shown.
  const capitalMissing = account.equity_basis === "NOT_OBSERVED";
  const capitalStale =
    !capitalMissing &&
    account.equity_basis === "GATE_TESTNET_REMOTE_ACCOUNT_TRUTH" &&
    Boolean(account.truth_stale);
  return (
    <section className={`v2-institutional-dashboard is-${status.toLowerCase()}`} aria-label={zh ? "机构级 AI 做单审计看板" : "Institutional AI execution audit dashboard"}>
      <header className="v2-institutional-dashboard__header">
        <div><span className="v2-institutional-eyebrow">INSTITUTIONAL EXECUTION LENS</span><h2>{zh ? "AI 做单审计看板" : "AI execution audit dashboard"}</h2><p>{zh ? "只读服务端投影 · 成交、费用、决策与保护均按账户作用域核对" : "Read-only server projection · fills, fees, decisions and protection stay account-scoped"}</p></div>
        <div className="v2-institutional-scope"><span className={`v2-badge ${status === "AVAILABLE" ? "v2-badge--bull" : status === "DEGRADED" ? "v2-badge--stop" : "v2-badge--neutral"}`}>{status}</span><strong>{dashboard.scope?.account_id || "—"}</strong><small>{dashboard.scope?.provider || "—"} · {dashboard.scope?.environment || dashboard.scope?.mode || "—"}</small></div>
      </header>
      <div className="v2-institutional-quality" role="status"><span>{dashboard.data_quality?.is_sample ? "FIXTURE" : "LIVE LEDGER FACTS"}</span><small>{dashboard.data_quality?.source || "authoritative_local_execution_ledger"}</small><small>{dashboard.data_quality?.note_zh || (zh ? "没有观测到的字段不会被填成零。" : "Unobserved fields are not filled with zeros.")}</small></div>
      {empty && <EmptyState text={empty.message_zh || (zh ? "当前账户尚无可验证事实。" : empty.message_en || "No verified facts.")} code={empty.code || "NOT_RUN"} />}
      {error && <div className="v2-institutional-error" role="alert">{error}</div>}
      {capitalMissing && (
        <div className="v2-institutional-error" role="note">
          {zh
            ? "尚未同步 Gate 模拟盘远端账户事实：资金与权益保持为空，本地种子存款不作为资金口径。请在「AI 做单」中点击「刷新模拟盘对账」。"
            : "Gate TestNet remote account facts are not synchronised: capital and equity stay blank and the local seed is not used. Use \"Refresh & Reconcile\" on the AI desk."}
        </div>
      )}
      {capitalStale && (
        <div className="v2-institutional-quality" role="note">
          <span>{zh ? "远端快照已过期" : "Remote snapshot is stale"}</span>
          <small>{`${zh ? "观测于" : "observed"} ${shortTime(account.truth_observed_at)}`}</small>
          <small>{zh ? "点击「刷新模拟盘对账」拉取最新账户事实" : "Use Refresh & Reconcile to pull fresh account facts"}</small>
        </div>
      )}
      <div className="v2-institutional-kpis">
        {[
          {
            label: zh ? "起算权益（远端）" : "Baseline equity",
            value: finite(account.initial_capital_usdt) ? `${account.initial_capital_usdt.toFixed(2)} USDT` : "—",
            sub: account.initial_capital_observed_at ? shortTime(account.initial_capital_observed_at) : account.initial_capital_basis || "—",
          },
          {
            label: zh ? "当前权益" : "Current equity",
            value: finite(account.current_equity_usdt) ? `${account.current_equity_usdt.toFixed(2)} USDT` : "—",
            sub: account.equity_basis || "observed ledger",
          },
          { label: zh ? "净盈亏" : "Net PnL", value: money(account.net_pnl_usdt), sub: zh ? "远端权益变动" : "remote equity delta" },
          { label: zh ? "已实现盈亏" : "Realized PnL", value: money(account.realized_pnl_usdt), sub: zh ? "Gate 账户累计" : "Gate account cumulative" },
          {
            label: zh ? "可用保证金" : "Available margin",
            value: finite(account.available_margin_usdt) ? `${account.available_margin_usdt.toFixed(2)} USDT` : "—",
            sub: finite(account.used_margin_usdt) ? `${zh ? "已用" : "used"} ${account.used_margin_usdt.toFixed(2)}` : "—",
          },
          {
            label: zh ? "累计费用" : "Fees",
            value: money(account.cumulative_fees_usdt),
            sub: account.cumulative_fees_basis || (dashboard.data_quality?.fee_unknown_count ? `${dashboard.data_quality.fee_unknown_count} unknown` : "—"),
          },
          { label: zh ? "最大回撤" : "Max drawdown", value: pct(account.max_drawdown_pct), sub: zh ? "远端权益曲线" : "remote equity curve" },
        ].map(({ label, value, sub }) => <article key={label} className="v2-institutional-kpi"><span>{label}</span><strong>{value}</strong><small>{sub}</small></article>)}
      </div>
      <div className="v2-institutional-grid v2-institutional-grid--wide">
        <Panel title={zh ? "权益 / 回撤" : "Equity / drawdown"} eyebrow="01"><EquityChart series={dashboard.equity_drawdown?.series} zh={zh} /></Panel>
        <Panel title={zh ? "已实现盈亏" : "Realized PnL"} eyebrow="02"><PnlBars bars={dashboard.realized_pnl_bars} zh={zh} /></Panel>
      </div>
      <div className="v2-institutional-grid">
        <Panel title={zh ? "六策略表现" : "Six-strategy performance"} eyebrow="03"><StrategyBars bars={dashboard.strategy_bars} zh={zh} /></Panel>
        <Panel title={zh ? "决策结构" : "Decision mix"} eyebrow="04"><DecisionDonut mix={dashboard.decision_mix} zh={zh} /></Panel>
        <Panel title={zh ? "交易时段热力" : "Timing heatmap"} eyebrow="05"><Heatmap heatmap={dashboard.heatmap} zh={zh} /></Panel>
      </div>
      <Panel title={zh ? "15m K 线与事件标记" : "15m candles and event markers"} eyebrow="06" className="v2-institutional-panel--chart"><CandlestickChart candles={dashboard.candles} zh={zh} /><div className="v2-institutional-chart-legend"><span><i className="is-candidate" />candidate</span><span><i className="is-order" />order</span><span><i className="is-fill" />fill</span></div></Panel>
      <Panel title={zh ? "AI 决策时间线" : "AI decision timeline"} eyebrow="07"><Timeline items={dashboard.timeline} zh={zh} /></Panel>
      <details className="v2-institutional-details"><summary>{zh ? "查看统一账本与审计明细" : "Open unified ledger details"}</summary><div className="v2-institutional-detail-grid">{Object.entries(dashboard.details || {}).map(([key, items]) => <div key={key}><h4>{key} · {Array.isArray(items) ? items.length : 0}</h4><pre>{JSON.stringify(items || [], null, 2)}</pre></div>)}</div></details>
      <footer className="v2-institutional-footer"><span>{zh ? "记录数" : "Facts"}: {Object.values(counts).reduce((sum, value) => sum + (Number(value) || 0), 0)}</span><span>{zh ? "投影不访问私有 API" : "Projection does not access private APIs"}</span><span>{dashboard.scope?.currency || "USDT"}</span></footer>
    </section>
  );
}

export default InstitutionalAnalysisDashboard;
