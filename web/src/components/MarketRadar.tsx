import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiClient } from "../api/client";
import "./marketRadar.css";

type MetricPoint = {
  venue?: string;
  symbol: string;
  time: string;
  price?: number | null;
  cvd_contracts?: number | null;
  delta_contracts?: number | null;
  buy_contracts?: number | null;
  sell_contracts?: number | null;
  volume?: number | null;
  open_interest?: number | null;
  open_interest_usdt?: number | null;
  unit?: string;
};

type MatrixRow = {
  symbol: string;
  gate?: {
    open_interest?: number | null;
    oi_change_pct?: number | null;
    funding_rate_pct?: number | null;
    time?: string | null;
  };
  binance?: {
    status?: string;
    reason?: string;
    open_interest?: number | null;
    open_interest_usdt?: number | null;
    oi_change_pct?: number | null;
    funding_rate_pct?: number | null;
  };
  price_change_pct?: number | null;
  crowding_score?: number | null;
  crowding_label?: string;
  score_basis?: string | null;
};

type Radar = {
  status?: string;
  generated_at?: string;
  symbols?: string[];
  cvd?: { status?: string; source?: string; as_of?: string | null; unit?: string; series?: MetricPoint[]; synthetic?: boolean };
  volume?: { status?: string; source?: string; as_of?: string | null; unit?: string; series?: MetricPoint[]; synthetic?: boolean };
  open_interest?: { status?: string; source?: string; as_of?: string | null; series?: MetricPoint[]; binance_status?: string; synthetic?: boolean };
  derivatives_matrix?: MatrixRow[];
  liquidations?: {
    status?: string;
    source?: string;
    as_of?: string | null;
    window_hours?: number;
    counts?: Record<string, number>;
    estimated_notional?: Record<string, number>;
    recent?: Array<{ event_id: string; symbol: string; time: string; direction: string; size_contracts?: number | null; price?: number | null; estimated_notional?: number | null }>;
    synthetic?: boolean;
  };
  onchain?: {
    status?: string;
    source?: string;
    as_of?: string | null;
    events?: Array<{ event_id: string; provider: string; asset?: string; amount?: number; amount_usd?: number; direction: string; from_label?: string; to_label?: string; event_at?: string; received_at?: string }>;
    providers?: Record<string, { status: string; transport: string; free?: boolean }>;
    synthetic?: boolean;
  };
  cross_market?: {
    status?: string;
    message?: string;
    as_of?: string | null;
    items?: Array<{ symbol: string; value?: number | null; change_pct?: number | null; status: string }>;
    synthetic?: boolean;
  };
};

type Metric = "volume" | "delta_contracts" | "cvd_contracts" | "open_interest";
type RadarMode = "dashboard" | "analysis" | "intel";

const CROSS_MARKET_SYMBOLS = ["DXY", "US10Y", "NQ", "BTC.D"];
const CROWDING_NAMES: Record<string, string> = {
  LONG_CROWDED: "多头拥挤",
  SHORT_CROWDED: "空头拥挤",
  BALANCED: "中性",
  EVIDENCE_INSUFFICIENT: "证据不足",
};

function statusText(status?: string): string {
  switch ((status || "LOADING").toUpperCase()) {
    case "AVAILABLE": return "有真实样本";
    case "NO_DATA": return "暂无样本";
    case "UNAVAILABLE": return "数据源不可用";
    case "CONFIG_REQUIRED":
    case "SOURCE_REQUIRED": return "需要配置数据源";
    case "WAITING_FOR_WEBHOOK": return "等待 Webhook 事件";
    case "NOT_CONNECTED": return "未连接";
    case "READY": return "已配置，等待数据";
    case "SYNTHETIC": return "模拟样本已拦截";
    case "LOADING": return "读取中";
    default: return status || "未知状态";
  }
}

function formatNumber(value: number | null | undefined, digits = 2): string {
  return value == null || !Number.isFinite(value)
    ? "—"
    : value.toLocaleString("zh-CN", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

function formatSignedPct(value?: number | null, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value > 0 ? "+" : ""}${formatNumber(value, digits)}%`;
}

function formatTime(value?: string | null): string {
  if (!value) return "时间未知";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "时间未知" : parsed.toLocaleString("zh-CN", { hour12: false });
}

function valuesFor(points: MetricPoint[], field: "cvd_contracts" | "delta_contracts" | "open_interest" | "price" | "volume") {
  return points.map(point => point[field]).filter((value): value is number => typeof value === "number" && Number.isFinite(value));
}

function linePoints(values: number[], startY: number, height: number): string {
  if (values.length < 2) return "";
  const low = Math.min(...values);
  const span = Math.max(...values) - low || 1;
  return values.map((value, index) => `${(index / (values.length - 1)) * 100},${startY + height - ((value - low) / span) * height}`).join(" ");
}

function OpenInterestChart({ points }: { points: MetricPoint[] }) {
  const byVenue = (venue: string) => points
    .filter(point => point.venue === venue && typeof point.open_interest === "number" && point.open_interest > 0 && Number.isFinite(Date.parse(point.time)))
    .sort((a, b) => Date.parse(a.time) - Date.parse(b.time));
  const gate = byVenue("gate");
  const binance = byVenue("binance");
  const normalize = (rows: MetricPoint[]) => {
    if (rows.length < 2 || !rows[0].open_interest) return [];
    const base = Number(rows[0].open_interest);
    return rows.map(point => ({ time: Date.parse(point.time), value: ((Number(point.open_interest) / base) - 1) * 100 }));
  };
  const gateOi = normalize(gate);
  const binanceOi = normalize(binance);
  const oiVenues = [gateOi.length > 1 ? "Gate" : "", binanceOi.length > 1 ? "Binance" : ""].filter(Boolean);
  const priceSource = gate.length > 1 ? gate : binance;
  const priceRows = priceSource.filter(point => typeof point.price === "number" && point.price > 0 && Number.isFinite(Date.parse(point.time)));
  const price = priceRows.length > 1
    ? priceRows.map(point => ({ time: Date.parse(point.time), value: ((Number(point.price) / Number(priceRows[0].price)) - 1) * 100 }))
    : [];
  const lines = [gateOi, binanceOi, price].filter(line => line.length > 1);
  if (!lines.length) return <div className="market-radar__empty" role="status">样本不足，至少需要两个有效时间点</div>;
  const allPoints = lines.flat();
  const minTime = Math.min(...allPoints.map(point => point.time));
  const maxTime = Math.max(...allPoints.map(point => point.time));
  const minValue = Math.min(0, ...allPoints.map(point => point.value));
  const maxValue = Math.max(0, ...allPoints.map(point => point.value));
  const span = maxValue - minValue || 1;
  const paddedMin = minValue - span * .08;
  const paddedMax = maxValue + span * .08;
  const xFor = (time: number) => maxTime === minTime ? 50 : ((time - minTime) / (maxTime - minTime)) * 100;
  const yFor = (value: number) => 7 + 39 - ((value - paddedMin) / (paddedMax - paddedMin)) * 39;
  const toPoints = (line: Array<{ time: number; value: number }>) => line.map(point => xFor(point.time) + "," + yFor(point.value)).join(" ");
  return <div className="market-radar__chart-wrap">
    <svg className="market-radar__chart" viewBox="0 0 100 52" preserveAspectRatio="none" role="img" aria-label={`${oiVenues.join(" 与 ")} OI 变化及参考价格变化率`}>
      <line x1="0" y1={yFor(0)} x2="100" y2={yFor(0)} />
      {gateOi.length > 1 && <polyline className="market-radar__line market-radar__line--oi" points={toPoints(gateOi)} />}
      {binanceOi.length > 1 && <polyline className="market-radar__line market-radar__line--oi-binance" points={toPoints(binanceOi)} />}
      {price.length > 1 && <polyline className="market-radar__line market-radar__line--price" points={toPoints(price)} />}
    </svg>
    <div className="market-radar__legend">
      {gateOi.length > 1 && <span className="market-radar__legend-oi">Gate OI Δ%</span>}
      {binanceOi.length > 1 && <span className="market-radar__legend-binance">Binance OI Δ%</span>}
      {price.length > 1 && <span className="market-radar__legend-price">{priceSource === gate ? "Gate 价格 Δ%" : "Binance OI 隐含均价 Δ%"}</span>}
    </div>
    <small className="market-radar__chart-note">各交易所 OI 从样本起点独立归一到 0%，只比较变化率、不叠加合约单位；Binance 参考线使用 OI 隐含均价，并非独立行情报价。</small>
  </div>;
}

function VolumeChart({ points }: { points: MetricPoint[] }) {
  const rows = points
    .filter(point => typeof point.volume === "number" && Number.isFinite(point.volume) && point.volume >= 0 && Number.isFinite(Date.parse(point.time)))
    .sort((a, b) => Date.parse(a.time) - Date.parse(b.time));
  const values = rows.map(point => Number(point.volume));
  if (values.length < 2) return <div className="market-radar__empty" role="status">样本不足，至少需要两个有效时间点</div>;
  const max = Math.max(...values) || 1;
  const barDuration = 15 * 60_000;
  const startTime = Date.parse(rows[0].time) - barDuration;
  const endTime = Date.parse(rows[rows.length - 1].time);
  const duration = endTime - startTime;
  if (duration <= 0) return <div className="market-radar__empty" role="status">样本时间无效，无法绘制成交量柱状图</div>;
  const barWidth = Math.max(.25, Math.min(92, (barDuration / duration) * 92));
  return <svg className="market-radar__chart market-radar__chart--bars" viewBox="0 0 100 52" preserveAspectRatio="none" role="img" aria-label="Gate 15m 合约成交量柱状图">
    <line x1="0" y1="47" x2="100" y2="47" />
    {rows.map((point, index) => {
      const value = Number(point.volume);
      const height = Math.max(.8, (value / max) * 38);
      const barStart = Date.parse(point.time) - barDuration;
      const x = ((barStart - startTime) / duration) * 100;
      return <rect key={String(point.symbol) + "-" + point.time + "-" + index} x={x} y={47 - height} width={barWidth} height={height} rx=".4" className="is-positive"><title>{point.symbol + " · " + formatTime(point.time) + " · " + formatNumber(value, 4) + " contracts"}</title></rect>;
    })}
  </svg>;
}

function MarketChart({ points, metric }: { points: MetricPoint[]; metric: Metric }) {
  if (metric === "open_interest") return <OpenInterestChart points={points} />;
  if (metric === "volume") return <VolumeChart points={points} />;
  const field = metric as "cvd_contracts" | "delta_contracts";
  const values = valuesFor(points, field);
  if (values.length < 2) {
    return <div className="market-radar__empty" role="status">样本不足，至少需要两个有效时间点</div>;
  }
  if (metric === "delta_contracts") {
    const maxAbs = Math.max(...values.map(Math.abs)) || 1;
    const step = 100 / values.length;
    return <svg className="market-radar__chart market-radar__chart--bars" viewBox="0 0 100 52" preserveAspectRatio="none" role="img" aria-label="主动买卖差分时柱状图">
      <line x1="0" y1="26" x2="100" y2="26" />
      {values.map((value, index) => {
        const height = Math.max(0.8, (Math.abs(value) / maxAbs) * 20);
        const y = value >= 0 ? 26 - height : 26;
        return <rect key={`${index}-${value}`} x={index * step + step * 0.14} y={y} width={Math.max(0.25, step * 0.72)} height={height} rx="0.4" className={value >= 0 ? "is-positive" : "is-negative"} />;
      })}
    </svg>;
  }

  const deltas = points.map(point => typeof point.delta_contracts === "number" && Number.isFinite(point.delta_contracts) ? point.delta_contracts : 0);
  const maxDelta = Math.max(...deltas.map(Math.abs)) || 1;
  const step = 100 / Math.max(values.length, 1);
  return <svg className="market-radar__chart" viewBox="0 0 100 52" preserveAspectRatio="none" role="img" aria-label="CVD 累计主动买卖差曲线与分时成交差">
    <line x1="0" y1="45" x2="100" y2="45" />
    <line x1="0" y1="26" x2="100" y2="26" />
    <polyline className="market-radar__line market-radar__line--cvd" points={linePoints(values, 5, 35)} />
    {deltas.slice(0, values.length).map((value, index) => {
      const height = Math.max(0.6, (Math.abs(value) / maxDelta) * 8);
      return <rect key={`${index}-${value}`} x={index * step + step * 0.25} y={value >= 0 ? 43 - height : 43} width={Math.max(0.25, step * 0.5)} height={height} rx="0.3" className={value >= 0 ? "is-positive" : "is-negative"} />;
    })}
  </svg>;
}

function SourceBadge({ status }: { status?: string }) {
  return <span className="market-radar__status" data-status={(status || "LOADING").toUpperCase()}>{statusText(status)}</span>;
}

function SourceMeta({ source, asOf }: { source?: string; asOf?: string | null }) {
  return <small className="market-radar__source-meta">来源：{source || "接口未提供"} <span aria-hidden="true">·</span> 更新：{formatTime(asOf)}</small>;
}

function EmptyState({ title, detail }: { title: string; detail: string }) {
  return <div className="market-radar__empty-state"><strong>{title}</strong><span>{detail}</span></div>;
}

function getOnchainEmpty(data: Radar | null): { title: string; detail: string } {
  const status = data?.onchain?.status?.toUpperCase();
  if (status === "CONFIG_REQUIRED") return { title: "尚未配置入站 Webhook", detail: "为 Arkham 告警或支持的链上来源配置 Webhook 后，这里才会出现事件。" };
  if (status === "WAITING_FOR_WEBHOOK") return { title: "Webhook 已配置，暂无事件", detail: "尚未收到可展示的已验证事件；没有用示例数据填充。" };
  if (status === "UNAVAILABLE") return { title: "链上事件源不可用", detail: "检查 Webhook 服务状态后可重试。" };
  return { title: "暂无链上资金流事件", detail: "收到已验证的 Arkham / Whale Alert Webhook 后会展示在这里。" };
}

function LiquidationPanel({ data }: { data: Radar | null }) {
  const liquidations = data?.liquidations;
  const available = liquidations?.status?.toUpperCase() === "AVAILABLE" && liquidations.synthetic !== true;
  const sides = [
    { id: "LONG", name: "多头爆仓", tone: "long" },
    { id: "SHORT", name: "空头爆仓", tone: "short" },
    { id: "UNKNOWN", name: "方向未知", tone: "unknown" },
  ];
  return <section className="terminal-panel market-radar__section" aria-labelledby="market-radar-liquidation-title">
    <header className="market-radar__section-head"><div><small>GATE PUBLIC LIQUIDATION STREAM</small><h2 id="market-radar-liquidation-title">爆仓分布</h2></div><SourceBadge status={liquidations?.synthetic ? "SYNTHETIC" : liquidations?.status} /></header>
    {available ? <>
      <div className="market-radar__stat-grid">
        {sides.map(side => <article className={`market-radar__liq-card is-${side.tone}`} key={side.id}>
          <small>{side.name}</small>
          <strong>{formatNumber(liquidations?.counts?.[side.id] ?? 0, 0)}<em> 笔</em></strong>
          <span>估算名义额 {formatNumber(liquidations?.estimated_notional?.[side.id])} USDT</span>
        </article>)}
      </div>
      {!!liquidations?.recent?.length && <div className="market-radar__table-wrap"><table className="market-radar__table">
        <thead><tr><th>合约</th><th>方向</th><th>时间</th><th>估算名义额</th></tr></thead>
        <tbody>{liquidations.recent.slice(0, 8).map(row => <tr key={row.event_id}><td>{row.symbol}</td><td>{row.direction}</td><td>{formatTime(row.time)}</td><td>{row.estimated_notional == null ? "—" : `${formatNumber(row.estimated_notional)} USDT`}</td></tr>)}</tbody>
      </table></div>}
    </> : <EmptyState title={liquidations?.synthetic ? "接口返回了模拟数据，已隐藏" : liquidations?.status === "UNAVAILABLE" ? "爆仓行情流不可用" : "暂未收到爆仓事件"} detail="分布仅根据已采集的 Gate 公共爆仓事件统计；没有事件样本时不推断多空强弱。" />}
    <p className="market-radar__basis">窗口：最近 {liquidations?.window_hours ?? 24} 小时。名义额只有在数量与成交价都存在时估算；它不是交易所官方账户损失金额。</p>
    <SourceMeta source={liquidations?.source} asOf={liquidations?.as_of} />
  </section>;
}

function OnchainPanel({ data }: { data: Radar | null }) {
  const onchain = data?.onchain;
  const events = onchain?.synthetic === true ? [] : onchain?.events || [];
  return <section className="terminal-panel market-radar__section" aria-labelledby="market-radar-onchain-title">
    <header className="market-radar__section-head"><div><small>VERIFIED WEBHOOK EVENTS</small><h2 id="market-radar-onchain-title">链上资金走向</h2></div><SourceBadge status={onchain?.synthetic ? "SYNTHETIC" : onchain?.status} /></header>
    {events.length ? <div className="market-radar__event-list">{events.slice(0, 8).map(event => <article key={event.event_id}>
      <div className="market-radar__event-direction" data-direction={event.direction.toUpperCase()}>{event.direction}</div>
      <strong>{event.asset || "未知资产"} {formatNumber(event.amount, 4)}</strong>
      <span>{event.amount_usd == null ? "美元估值未提供" : `$${formatNumber(event.amount_usd)}`} · {event.provider}</span>
      <small>{event.from_label || "未知来源"} <b aria-hidden="true">→</b> {event.to_label || "未知去向"}</small>
      <time dateTime={event.event_at || event.received_at}>{formatTime(event.event_at || event.received_at)}</time>
    </article>)}</div> : onchain?.synthetic === true ? <EmptyState title="接口返回了模拟数据，已隐藏" detail="只显示已验证 Webhook 写入的真实事件。" /> : <EmptyState {...getOnchainEmpty(data)} />}
    <div className="market-radar__provider-row" aria-label="链上数据源配置状态">{Object.entries(onchain?.providers || {}).map(([provider, item]) => <div key={provider} data-status={item.status}>
      <b>{provider === "whale_alert" ? "Whale Alert" : provider === "arkham" ? "Arkham" : provider}</b>
      <span>{statusText(item.status)}</span>
      <small>{item.transport}{item.free === false ? " · 可能产生费用" : ""}</small>
    </div>)}</div>
    <SourceMeta source={onchain?.source || "已验证的 Webhook 入站事件"} asOf={onchain?.as_of} />
  </section>;
}

function CrossMarketPanel({ data, compact = false }: { data: Radar | null; compact?: boolean }) {
  const isSynthetic = data?.cross_market?.synthetic === true;
  const items = new Map((isSynthetic ? [] : data?.cross_market?.items || []).map(item => [item.symbol.toUpperCase(), item]));
  return <div className={`market-radar__cross-market${compact ? " market-radar__cross-market--compact" : ""}`} aria-label="跨市场数据状态">
    {CROSS_MARKET_SYMBOLS.map(symbol => {
      const item = items.get(symbol);
      const status = isSynthetic ? "SYNTHETIC" : item?.status || "SOURCE_REQUIRED";
      const available = !isSynthetic && status.toUpperCase() === "AVAILABLE" && item?.value != null;
      return <article key={symbol} data-status={status.toUpperCase()}>
        <div><b>{symbol}</b><SourceBadge status={status} /></div>
        <strong>{available ? formatNumber(item.value, symbol === "US10Y" ? 3 : 2) : "—"}{available && (symbol === "BTC.D" || symbol === "US10Y") ? "%" : ""}</strong>
        <span>{available && item.change_pct != null ? formatSignedPct(item.change_pct) : statusText(status)}</span>
      </article>;
    })}
  </div>;
}

function DerivativesMatrix({ rows }: { rows: MatrixRow[] }) {
  if (!rows.length) return <EmptyState title="尚无衍生品样本" detail="Gate OI / 资金费率由后台采集；Binance 公共合约数据为可选源。" />;
  return <div className="market-radar__matrix">{rows.map(row => {
    const gate = row.gate || {};
    const binance = row.binance || {};
    const crowding = row.crowding_score;
    const label = row.crowding_label || "EVIDENCE_INSUFFICIENT";
    return <article key={row.symbol} data-crowding={label}>
      <header><strong>{row.symbol}</strong><span>{CROWDING_NAMES[label] || label}</span></header>
      <div className="market-radar__matrix-values">
        <div><small>Gate OI 变化</small><b>{formatSignedPct(gate.oi_change_pct)}</b></div>
        <div><small>Gate 资金费率</small><FundingRate value={gate.funding_rate_pct} venue="Gate" /></div>
        <div><small>Binance OI 变化</small><b>{formatSignedPct(binance.oi_change_pct)}</b></div>
        <div><small>Binance 资金费率</small><FundingRate value={binance.funding_rate_pct} venue="Binance" /></div>
      </div>
      <div className="market-radar__crowding"><span>拥挤度</span>{crowding == null ? <b>证据不足</b> : <>
        <div className="market-radar__crowding-track" role="meter" aria-label={`${row.symbol} 拥挤度`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.max(0, Math.min(100, crowding))}><i style={{ width: `${Math.max(0, Math.min(100, crowding))}%` }} /></div>
        <b>{formatNumber(crowding, 1)} / 100</b>
      </>}</div>
      <footer><span>价格变化 {formatSignedPct(row.price_change_pct)}</span><span>Binance：{statusText(binance.status)}</span></footer>
      {row.score_basis && <small className="market-radar__score-basis">拥挤评分依据：{row.score_basis}</small>}
    </article>;
  })}</div>;
}

function FundingRate({ value, venue }: { value?: number | null; venue: string }) {
  if (value == null || !Number.isFinite(value)) return <b>—</b>;
  const intensity = Math.min(1, Math.abs(value) / 0.05);
  const width = `${Math.max(4, intensity * 100)}%`;
  const tone = value > 0 ? "positive" : value < 0 ? "negative" : "neutral";
  return <div className="market-radar__funding-rate" data-tone={tone} aria-label={`${venue} 资金费率 ${formatSignedPct(value, 4)}`}>
    <span><i style={{ width }} /></span>
    <b>{formatSignedPct(value, 4)}</b>
  </div>;
}

export function MarketRadar({ mode = "analysis", preferredSymbol }: { mode?: RadarMode; preferredSymbol?: string }) {
  const [data, setData] = useState<Radar | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [metric, setMetric] = useState<Metric>("cvd_contracts");
  const [selectedSymbol, setSelectedSymbol] = useState("");
  const inFlightRef = useRef(false);
  const requestControllerRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);

  const refresh = useCallback(async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    const controller = new AbortController();
    requestControllerRef.current = controller;
    setRefreshing(true);
    try {
      const requestedSymbol = mode === "dashboard" ? preferredSymbol?.trim().toUpperCase() : "";
      const symbolQuery = requestedSymbol?.endsWith("USDT") ? `?symbols=${encodeURIComponent(requestedSymbol)}` : "";
      const result = await apiClient.v2<Radar>(`/market-radar${symbolQuery}`, "GET", undefined, controller.signal);
      if (!mountedRef.current || controller.signal.aborted) return;
      setData(result);
      setSelectedSymbol(current => result.symbols?.includes(current) ? current : result.symbols?.[0] || "");
      setError("");
    } catch (reason) {
      if (mountedRef.current && !controller.signal.aborted) setError(reason instanceof Error ? reason.message : "市场雷达请求失败");
    } finally {
      if (requestControllerRef.current === controller) {
        requestControllerRef.current = null;
        inFlightRef.current = false;
      }
      if (mountedRef.current && !controller.signal.aborted) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, [mode, preferredSymbol]);

  useEffect(() => {
    mountedRef.current = true;
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 30_000);
    return () => {
      mountedRef.current = false;
      window.clearInterval(timer);
      requestControllerRef.current?.abort();
      requestControllerRef.current = null;
      inFlightRef.current = false;
    };
  }, [refresh]);

  const symbols = data?.symbols || [];
  const normalizedPreferred = preferredSymbol?.trim().toUpperCase();
  const requestedAvailable = !!normalizedPreferred && symbols.includes(normalizedPreferred);
  const dashboardSymbol = requestedAvailable ? normalizedPreferred : selectedSymbol || symbols[0] || "";
  const symbol = mode === "dashboard"
    ? dashboardSymbol
    : selectedSymbol || symbols[0] || "";
  const showSymbolSelector = symbols.length > 1 && !(mode === "dashboard" && requestedAvailable);
  const cvdPoints = useMemo(() => data?.cvd?.synthetic === true ? [] : (data?.cvd?.series || []).filter(point => !symbol || point.symbol === symbol).slice(-48), [data, symbol]);
  const volumePoints = useMemo(() => data?.volume?.synthetic === true ? [] : (data?.volume?.series || []).filter(point => (!symbol || point.symbol === symbol) && typeof point.volume === "number").slice(-48), [data, symbol]);
  const oiPoints = useMemo(() => {
    if (data?.open_interest?.synthetic === true) return [];
    const scoped = (data?.open_interest?.series || []).filter(point => !symbol || point.symbol === symbol);
    return ["gate", "binance"].flatMap(venue => scoped
      .filter(point => point.venue === venue)
      .sort((a, b) => Date.parse(a.time) - Date.parse(b.time))
      .slice(-48));
  }, [data, symbol]);
  const activePoints = metric === "volume" ? volumePoints : metric === "open_interest" ? oiPoints : cvdPoints;
  const activeFeed = metric === "volume" ? data?.volume : metric === "open_interest" ? data?.open_interest : data?.cvd;
  const lastUpdated = data?.generated_at;
  const stale = !!error && !!data;
  const sectionStatus = data?.status || (loading ? "LOADING" : "UNAVAILABLE");

  const header = (eyebrow: string, title: string, status = sectionStatus) => <header className="market-radar__main-head">
    <div><small>{eyebrow}</small><h2>{title}</h2><p>{stale ? "刷新失败，以下保留最近一次成功读取的数据。" : `最近更新：${formatTime(lastUpdated)}`}</p></div>
    <div className="market-radar__head-actions"><SourceBadge status={status} /><button type="button" onClick={() => void refresh()} disabled={refreshing} aria-label="刷新市场雷达">{refreshing ? "读取中…" : "刷新"}</button></div>
  </header>;

  if (mode === "dashboard") return <section role="region" aria-label="盘口与全球市场" className="terminal-panel market-radar market-radar--compact">
    {header("MARKET MICROSTRUCTURE", "盘口与全球市场", data?.status || (loading ? "LOADING" : "UNAVAILABLE"))}
    {stale && <p className="market-radar__error" role="status">刷新失败：{error} · 仍显示 {formatTime(lastUpdated)} 的数据</p>}
    <div className="market-radar__toolbar">
      <div className="market-radar__tabs" role="group" aria-label="副图指标">
        {[{ id: "volume", text: "成交量" }, { id: "cvd_contracts", text: "CVD" }, { id: "open_interest", text: "OI" }].map(item => <button key={item.id} type="button" aria-pressed={metric === item.id} className={metric === item.id ? "is-active" : ""} onClick={() => setMetric(item.id as Metric)}>{item.text}</button>)}
      </div>
      {showSymbolSelector && <label className="market-radar__symbol-select">交易品种<select value={symbol} onChange={event => setSelectedSymbol(event.target.value)} aria-label="选择交易品种">{symbols.map(item => <option key={item} value={item}>{item}</option>)}</select></label>}
    </div>
    {mode === "dashboard" && normalizedPreferred && !requestedAvailable && symbols.length > 0 && <p className="market-radar__symbol-fallback" role="status">上方 K 线为 {normalizedPreferred}；此处该标的暂无雷达样本，副图显示 {symbol}。</p>}
    <div className="market-radar__dashboard-chart"><div className="market-radar__chart-title"><b>{symbol || "交易品种"}</b><span>{metric === "volume" ? "Gate 15m 合约成交量 · contracts" : metric === "cvd_contracts" ? "累计主动买卖差 · contracts" : "Gate / Binance OI 与价格变化率"}</span></div><MarketChart points={activePoints} metric={metric} /></div>
    <CrossMarketPanel data={data} compact />
    <footer className="market-radar__footer"><span>{activeFeed?.source || "行情源未报告"}</span><span>{activePoints.length ? String(activePoints.length) + " 个" + (activeFeed?.synthetic === false ? "真实" : "") + "采集点" : activeFeed ? statusText(activeFeed.synthetic ? "SYNTHETIC" : activeFeed.status) : loading ? statusText("LOADING") : "当前 API 未提供该指标"}</span></footer>
  </section>;

  if (mode === "intel") return <div role="region" aria-label="市场微观结构与资金流" className="market-radar market-radar--intel">
    {header("INTELLIGENCE ROOM", "市场微观结构与资金流", data?.status || (loading ? "LOADING" : "UNAVAILABLE"))}
    {stale && <p className="market-radar__error" role="status">刷新失败：{error} · 仍显示 {formatTime(lastUpdated)} 的数据</p>}
    <LiquidationPanel data={data} />
    <OnchainPanel data={data} />
    <section className="terminal-panel market-radar__section" aria-labelledby="market-radar-corridor-title">
      <header className="market-radar__section-head"><div><small>GLOBAL LIQUIDITY CORRIDOR</small><h2 id="market-radar-corridor-title">全球宏观与跨市场走廊</h2></div><SourceBadge status={data?.cross_market?.status} /></header>
      <CrossMarketPanel data={data} />
      <p className="market-radar__basis">{data?.cross_market?.message || "跨市场指标仅在已连接的授权行情源返回数据后展示。"}</p>
      <SourceMeta source={data?.cross_market?.items?.some(item => item.value != null) ? "API 返回的跨市场行情" : "尚未配置授权数据源"} asOf={data?.cross_market?.as_of} />
    </section>
  </div>;

  return <section role="region" aria-label="市场微观结构与衍生品雷达" className="terminal-panel market-radar market-radar--analysis">
    {header("ORDER FLOW + DERIVATIVES", "市场微观结构与衍生品雷达", data?.status || (loading ? "LOADING" : "UNAVAILABLE"))}
    {error && !data && <div role="alert" className="market-radar__error">市场雷达读取失败：{error}<button type="button" onClick={() => void refresh()}>重试</button></div>}
    {stale && <p className="market-radar__error" role="status">刷新失败：{error} · 下方为最近一次成功读取的数据</p>}
    {symbols.length > 1 && <label className="market-radar__symbol-select">交易品种<select value={symbol} onChange={event => setSelectedSymbol(event.target.value)} aria-label="选择交易品种">{symbols.map(item => <option key={item} value={item}>{item}</option>)}</select></label>}
    <div className="market-radar__analysis-grid">
      <article aria-labelledby="market-radar-cvd-title"><header><div><small>TAKER FLOW</small><h3 id="market-radar-cvd-title">{symbol || "市场"} · CVD 与主动买卖差</h3></div><SourceBadge status={data?.cvd?.synthetic ? "SYNTHETIC" : data?.cvd?.status} /></header>{data?.cvd?.synthetic ? <EmptyState title="接口返回了模拟数据，已隐藏" detail="这里只绘制 Gate 公共成交样本。" /> : <MarketChart points={cvdPoints} metric="cvd_contracts" />}<div className="market-radar__legend"><span className="market-radar__legend-cvd">累计 CVD</span><span>柱形为分时主动买卖差</span></div><SourceMeta source={data?.cvd?.source} asOf={data?.cvd?.as_of} /></article>
      <article aria-labelledby="market-radar-oi-title"><header><div><small>OPEN INTEREST + PRICE</small><h3 id="market-radar-oi-title">{symbol || "市场"} · OI 与参考价变化率</h3></div><SourceBadge status={data?.open_interest?.synthetic ? "SYNTHETIC" : data?.open_interest?.status} /></header>{data?.open_interest?.synthetic ? <EmptyState title="接口返回了模拟数据，已隐藏" detail="这里只绘制交易所真实公开 OI 样本。" /> : <MarketChart points={oiPoints} metric="open_interest" />}<SourceMeta source={data?.open_interest?.source} asOf={data?.open_interest?.as_of} /></article>
    </div>
    <div className="market-radar__matrix-heading"><div><small>DERIVATIVES MATRIX</small><h3>资金费率极值与多空拥挤度</h3></div><span>分数由 OI 变化与资金费率估算，不是方向预测</span></div>
    <DerivativesMatrix rows={data?.derivatives_matrix || []} />
    {error && !data && <p className="market-radar__source-meta">数据接口不可用时不会显示模拟指标。</p>}
  </section>;
}
