import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiClient } from "../api/client";
import { MarketRadar } from "./MarketRadar";

const unavailableRadar = {
  status: "NO_DATA",
  generated_at: "2026-09-21T01:00:00Z",
  symbols: ["BTCUSDT"],
  cvd: { status: "NO_DATA", source: "gate_public_trades_ws_and_rest", as_of: null, series: [], synthetic: false },
  volume: { status: "NO_DATA", source: "gate_public_closed_15m_candles", as_of: null, series: [], synthetic: false },
  open_interest: { status: "NO_DATA", source: "gate_public_derivatives_rest", as_of: null, series: [], binance_status: "NOT_CONNECTED", synthetic: false },
  derivatives_matrix: [],
  liquidations: { status: "NO_DATA", source: "gate_futures.public_liquidates_websocket", as_of: null, window_hours: 24, counts: {}, estimated_notional: {}, recent: [], synthetic: false },
  onchain: {
    status: "CONFIG_REQUIRED",
    as_of: null,
    events: [],
    providers: {
      arkham: { status: "CONFIG_REQUIRED", transport: "user_configured_alert_webhook" },
      whale_alert: { status: "CONFIG_REQUIRED", transport: "provider_api_or_websocket", free: false },
    },
    synthetic: false,
  },
  cross_market: {
    status: "CONFIG_REQUIRED",
    message: "DXY、US10Y 与 NQ 需要授权行情源；BTC.D 尚未连接。",
    items: ["DXY", "US10Y", "NQ", "BTC.D"].map(symbol => ({ symbol, value: null, change_pct: null, status: "SOURCE_REQUIRED" })),
    synthetic: false,
  },
} as const;

const availableRadar = {
  ...unavailableRadar,
  status: "AVAILABLE",
  symbols: ["BTCUSDT", "ETHUSDT"],
  generated_at: "2026-09-21T01:15:00Z",
  cvd: {
    status: "AVAILABLE",
    source: "gate_public_trades_ws_and_rest",
    as_of: "2026-09-21T01:14:00Z",
    unit: "contracts",
    synthetic: false,
    series: [
      { venue: "gate", symbol: "BTCUSDT", time: "2026-09-21T01:10:00Z", price: 65000, buy_contracts: 12, sell_contracts: 9, delta_contracts: 3, cvd_contracts: 3 },
      { venue: "gate", symbol: "BTCUSDT", time: "2026-09-21T01:14:00Z", price: 65100, buy_contracts: 10, sell_contracts: 13, delta_contracts: -3, cvd_contracts: 0 },
    ],
  },
  volume: {
    status: "AVAILABLE",
    source: "gate_public_closed_15m_candles",
    as_of: "2026-09-21T01:14:00Z",
    unit: "contracts",
    synthetic: false,
    series: [
      { venue: "gate", symbol: "BTCUSDT", time: "2026-09-21T01:00:00Z", price: 64900, volume: 1240 },
      { venue: "gate", symbol: "BTCUSDT", time: "2026-09-21T01:15:00Z", price: 65100, volume: 1810 },
      { venue: "gate", symbol: "ETHUSDT", time: "2026-09-21T01:00:00Z", price: 3500, volume: 820 },
      { venue: "gate", symbol: "ETHUSDT", time: "2026-09-21T01:15:00Z", price: 3540, volume: 1050 },
    ],
  },
  open_interest: {
    status: "AVAILABLE",
    source: "gate_and_binance_public_derivatives",
    as_of: "2026-09-21T01:14:00Z",
    binance_status: "AVAILABLE",
    synthetic: false,
    series: [
      { venue: "gate", symbol: "BTCUSDT", time: "2026-09-21T01:10:00Z", open_interest: 12300, price: 65000, unit: "contracts" },
      { venue: "gate", symbol: "BTCUSDT", time: "2026-09-21T01:14:00Z", open_interest: 12400, price: 65100, unit: "contracts" },
      { venue: "binance", symbol: "BTCUSDT", time: "2026-09-21T01:10:00Z", open_interest: 21000, price: 65010, unit: "contracts" },
      { venue: "binance", symbol: "BTCUSDT", time: "2026-09-21T01:14:00Z", open_interest: 21150, price: 65120, unit: "contracts" },
    ],
  },
  derivatives_matrix: [{
    symbol: "BTCUSDT",
    gate: { open_interest: 12400, oi_change_pct: 0.81, funding_rate_pct: 0.012, time: "2026-09-21T01:14:00Z" },
    binance: { status: "AVAILABLE", open_interest: 12000, oi_change_pct: 0.4, funding_rate_pct: 0.009 },
    price_change_pct: 0.23,
    crowding_score: 36.2,
    crowding_label: "LONG_CROWDED",
    score_basis: "gate sample",
  }],
  liquidations: {
    status: "AVAILABLE",
    source: "gate_futures.public_liquidates_websocket",
    as_of: "2026-09-21T01:14:00Z",
    window_hours: 24,
    counts: { LONG: 2, SHORT: 1, UNKNOWN: 0 },
    estimated_notional: { LONG: 4000, SHORT: 1500, UNKNOWN: 0 },
    recent: [{ event_id: "liq-1", symbol: "BTCUSDT", time: "2026-09-21T01:14:00Z", direction: "LONG", estimated_notional: 2100 }],
    synthetic: false,
  },
  onchain: {
    status: "AVAILABLE",
    source: "verified webhook events",
    as_of: "2026-09-21T01:12:00Z",
    events: [{ event_id: "flow-1", provider: "arkham", asset: "BTC", amount: 10, amount_usd: 650000, direction: "OUTFLOW", from_label: "Exchange", to_label: "Unknown wallet", event_at: "2026-09-21T01:12:00Z", received_at: "2026-09-21T01:12:01Z" }],
    providers: { arkham: { status: "READY", transport: "user_configured_alert_webhook" } },
    synthetic: false,
  },
  cross_market: {
    status: "AVAILABLE",
    message: "Real public-source values",
    as_of: "2026-09-21T01:14:00Z",
    items: [
      { symbol: "DXY", value: 102.5, change_pct: 0.1, status: "AVAILABLE" },
      { symbol: "US10Y", value: 4.05, change_pct: -0.2, status: "AVAILABLE" },
      { symbol: "NQ", value: null, change_pct: null, status: "SOURCE_REQUIRED" },
      { symbol: "BTC.D", value: null, change_pct: null, status: "SOURCE_REQUIRED" },
    ],
    synthetic: false,
  },
} as const;

describe("MarketRadar", () => {
  beforeEach(() => {
    vi.spyOn(apiClient, "v2").mockResolvedValue(unavailableRadar as never);
  });
  afterEach(() => vi.restoreAllMocks());

  it("renders the dashboard metric switcher and keeps unconfigured macro feeds visibly empty", async () => {
    render(<MarketRadar mode="dashboard" />);

    expect(await screen.findByRole("heading", { name: "盘口与全球市场" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "CVD" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "成交量" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("button", { name: "OI" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("status")).toHaveTextContent("样本不足");
    fireEvent.click(screen.getByRole("button", { name: "成交量" }));
    expect(screen.getByRole("button", { name: "成交量" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "OI" }));
    expect(screen.getByRole("button", { name: "OI" })).toHaveAttribute("aria-pressed", "true");
    for (const symbol of ["DXY", "US10Y", "NQ", "BTC.D"]) {
      const card = screen.getByText(symbol).closest("article");
      expect(card).toHaveTextContent("—");
      expect(card).toHaveTextContent("需要配置数据源");
    }
    expect(vi.mocked(apiClient.v2).mock.calls.some(([path]) => path === "/market-radar")).toBe(true);
  });

  it("requests and follows the dashboard's preferred chart symbol", async () => {
    vi.mocked(apiClient.v2).mockImplementation(async (path: string) => {
      if (path.includes("symbols=SOLUSDT")) return { ...availableRadar, symbols: ["SOLUSDT"] } as never;
      return availableRadar as never;
    });
    const { container, rerender } = render(<MarketRadar mode="dashboard" preferredSymbol="ETHUSDT" />);

    await screen.findByRole("heading", { name: "盘口与全球市场" });
    const chartSymbol = () => container.querySelector(".market-radar__chart-title b")?.textContent;
    expect(chartSymbol()).toBe("ETHUSDT");
    expect(screen.queryByRole("combobox", { name: "选择交易品种" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "成交量" }));
    const volumeChart = screen.getByRole("img", { name: "Gate 15m 合约成交量柱状图" });
    const volumeBars = Array.from(volumeChart.querySelectorAll("rect"));
    expect(volumeBars).toHaveLength(2);
    expect(Number(volumeBars[0].getAttribute("x"))).toBeCloseTo(0);
    expect(Number(volumeBars[1].getAttribute("x"))).toBeCloseTo(50);

    rerender(<MarketRadar mode="dashboard" preferredSymbol="BTCUSDT" />);
    expect(chartSymbol()).toBe("BTCUSDT");

    rerender(<MarketRadar mode="dashboard" preferredSymbol="SOLUSDT" />);
    await waitFor(() => expect(chartSymbol()).toBe("SOLUSDT"));
    expect(vi.mocked(apiClient.v2).mock.calls.some(([path]) => path === "/market-radar?symbols=ETHUSDT")).toBe(true);
    expect(vi.mocked(apiClient.v2).mock.calls.some(([path]) => path === "/market-radar?symbols=SOLUSDT")).toBe(true);

  });

  it("makes a missing preferred dashboard symbol visible and lets the radar select an available symbol", async () => {
    vi.mocked(apiClient.v2).mockResolvedValue(availableRadar as never);
    const { container } = render(<MarketRadar mode="dashboard" preferredSymbol="SOLUSDT" />);

    expect(await screen.findByText(/上方 K 线为 SOLUSDT；此处该标的暂无雷达样本/)).toBeInTheDocument();
    const selector = screen.getByRole("combobox", { name: "选择交易品种" });
    fireEvent.change(selector, { target: { value: "ETHUSDT" } });
    expect(container.querySelector(".market-radar__chart-title b")).toHaveTextContent("ETHUSDT");
  });

  it("shows observed derivatives data, sample source, and selected-asset plots", async () => {
    vi.mocked(apiClient.v2).mockResolvedValue(availableRadar as never);
    render(<MarketRadar mode="analysis" />);

    expect(await screen.findByRole("heading", { name: "BTCUSDT · CVD 与主动买卖差" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Gate 与 Binance OI 变化及参考价格变化率" })).toBeInTheDocument();
    expect(screen.getByText("Gate OI Δ%")).toBeInTheDocument();
    expect(screen.getByText("Binance OI Δ%")).toBeInTheDocument();
    expect(screen.getByText("Gate 价格 Δ%")).toBeInTheDocument();
    expect(screen.getByText("多头拥挤")).toBeInTheDocument();
    expect(screen.getByText("+0.81%")).toBeInTheDocument();
    expect(screen.getByText(/来源：gate_public_trades_ws_and_rest/)).toBeInTheDocument();

    const selector = screen.getByRole("combobox", { name: "选择交易品种" });
    fireEvent.change(selector, { target: { value: "ETHUSDT" } });
    expect(screen.getByRole("heading", { name: "ETHUSDT · CVD 与主动买卖差" })).toBeInTheDocument();
  });

  it("labels the Binance OI-derived average accurately when Gate reference prices are missing", async () => {
    vi.mocked(apiClient.v2).mockResolvedValue({
      ...availableRadar,
      open_interest: {
        ...availableRadar.open_interest,
        series: availableRadar.open_interest.series.filter(point => point.venue === "binance"),
      },
    } as never);
    render(<MarketRadar mode="analysis" />);

    expect(await screen.findByText("Binance OI 隐含均价 Δ%")).toBeInTheDocument();
    expect(screen.getByText(/并非独立行情报价/)).toBeInTheDocument();
  });

  it("does not turn missing liquidation/onchain/cross-market evidence into live-looking values", async () => {
    render(<MarketRadar mode="intel" />);

    const liquidation = await screen.findByRole("region", { name: "爆仓分布" });
    expect(within(liquidation).getByText("暂未收到爆仓事件")).toBeInTheDocument();
    expect(within(liquidation).queryByText("0 笔")).not.toBeInTheDocument();
    expect(screen.getByText("尚未配置入站 Webhook")).toBeInTheDocument();
    expect(screen.getByText("Whale Alert").closest("div")).toHaveTextContent("可能产生费用");
    const dxy = screen.getByText("DXY").closest("article");
    expect(dxy).toHaveTextContent("需要配置数据源");
    expect(dxy).toHaveTextContent("—");
  });

  it("keeps the latest successful data visible and marks a failed refresh as stale", async () => {
    vi.mocked(apiClient.v2)
      .mockResolvedValueOnce(availableRadar as never)
      .mockRejectedValueOnce(new Error("radar timeout"));
    render(<MarketRadar mode="analysis" />);

    expect(await screen.findByText("多头拥挤")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "刷新市场雷达" }));
    expect(await screen.findByRole("status")).toHaveTextContent("radar timeout");
    expect(screen.getByText("多头拥挤")).toBeInTheDocument();
    expect(screen.getAllByText(/最近一次成功读取的数据/).length).toBeGreaterThan(0);
  });
});
