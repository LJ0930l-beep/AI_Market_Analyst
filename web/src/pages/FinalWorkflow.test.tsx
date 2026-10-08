import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { apiClient } from "../api/client";
import { I18nProvider } from "../i18n";
import { V2WorkspacePage } from "./V2WorkspacePage";
import { createFakeClient } from "../test/fakeClient";

beforeEach(() => {
  window.localStorage.setItem("ai-market-analyst.language", "zh-CN");
  window.sessionStorage.removeItem("aima.trading-account");
  const fake = createFakeClient();
  vi.spyOn(apiClient, "marketIntelligence").mockImplementation(fake.marketIntelligence);
  vi.spyOn(apiClient, "chartBars").mockImplementation(fake.chartBars);
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

it("separates official speech text from numerical actuals and explains cooldown", async () => {
  vi.spyOn(apiClient, "v2").mockImplementation(async (path: string) => {
    if (path === "/accounts") return {accounts: []} as never;
    if (path === "/macro-calendar/refresh") return {refresh_deferred: true, retry_after_seconds: 89} as never;
    return {watchlist: [], subscriptions: [], runtime: {state:"stopped"}, positions: [], decisions: [],
      macro_calendar: {status:"SCHEDULE_ONLY",actual_supported:true,actual_verified_count:1,
        qualitative_event_count:2,qualitative_verified_count:1,qualitative_recording_count:1},
      macro_events: [{event_id:"official_speech",title:"FOMC Member Waller Speaks",currency:"USD",
        event_time:"2026-10-01T14:00:00Z",actual:null,actual_status:"NON_NUMERIC_EVENT",
        qualitative_status:"OFFICIAL_TEXT_AVAILABLE",qualitative_provider:"Federal Reserve",
        qualitative_title:"Data and AI",qualitative_source_url:"https://www.federalreserve.gov/newsevents/speech/waller20261001a.htm",
        directive:"NONE",schedule_only:true},
        {event_id:"official_recording",title:"FOMC Member Kashkari Speaks",currency:"USD",
        event_time:"2026-09-30T22:00:00Z",actual:null,actual_status:"NON_NUMERIC_EVENT",
        qualitative_status:"OFFICIAL_RECORDING_AVAILABLE",qualitative_source_url:"https://www.minneapolisfed.org/speeches/2026/neel-kashkari-qa-at-the-council-on-foreign-relations",
        directive:"NONE",schedule_only:true}]} as never;
  });
  render(<I18nProvider><MemoryRouter><V2WorkspacePage /></MemoryRouter></I18nProvider>);
  expect(await screen.findByText(/官方原文已同步/)).toBeInTheDocument();
  expect(screen.getAllByText("讲话/声明，无数值公布项")).toHaveLength(4);
  expect(screen.queryByText("公布值来源待接入")).not.toBeInTheDocument();
  expect(screen.getByRole("link",{name:/查看官方原文/})).toHaveAttribute("href","https://www.federalreserve.gov/newsevents/speech/waller20261001a.htm");
  expect(screen.getByText("官方活动介绍及录播已接入，无逐字稿。")).toBeInTheDocument();
  expect(screen.getByRole("link",{name:/查看官方录播/})).toHaveAttribute("href","https://www.minneapolisfed.org/speeches/2026/neel-kashkari-qa-at-the-council-on-foreign-relations");
  fireEvent.click(screen.getByRole("button",{name:"更新公开日历"}));
  expect(await screen.findByText(/刷新冷却中，2 分钟后可重试/)).toBeInTheDocument();
});

it("keeps the console and analysis on the same account without creating orders", async () => {
  const query = vi.spyOn(apiClient, "v2").mockImplementation(async (path: string) => {
    if (path === "/accounts") return {accounts: ["paper_a", "paper_b"].map(account_id => ({account_id, mode: "PAPER", venue: "simulated"}))} as never;
    if (path.startsWith("/ai-analysis")) return {account: {initial_capital_usdt:10000,current_equity_usdt:10000,net_pnl_usdt:0,total_roi_pct:0,margin_used_usdt:0,margin_available_usdt:10000,win_rate_pct:0,total_trades:0,winning_trades:0,losing_trades:0,profit_factor:1,max_drawdown_pct:0,avg_leverage:1},execution_records: [], execution_positions: [], execution_orders: [], trades: [], strategy_matrix: [], equity_curve: []} as never;
    return {watchlist: [], subscriptions: [], runtime: {state: "stopped"}, positions: [], decisions: [], allow_unknown_macro: false} as never;
  });
  const dashboard = render(<I18nProvider><MemoryRouter><V2WorkspacePage surface="dashboard" /></MemoryRouter></I18nProvider>);
  const selector = await screen.findByRole("combobox", {name: "交易账户"});
  fireEvent.change(selector, {target: {value: "paper_b"}});
  await waitFor(() => expect(window.sessionStorage.getItem("aima.trading-account")).toBe("paper_b"));
  expect(screen.getByRole("heading", {name: /AI 交易指挥舱/})).toBeInTheDocument();
  dashboard.unmount();

  render(<I18nProvider><MemoryRouter><V2WorkspacePage surface="analysis" /></MemoryRouter></I18nProvider>);
  await waitFor(() => expect(query).toHaveBeenCalledWith(expect.stringContaining("/ai-analysis?account_id=paper_b"), "GET", undefined, expect.any(AbortSignal)));
  expect(window.sessionStorage.getItem("aima.trading-account")).toBe("paper_b");
  expect(query.mock.calls.every(call => call[1] === undefined || call[1] === "GET")).toBe(true);
});

it("shows a sourced Chinese calendar and explicit missing actual values", async () => {
  vi.spyOn(apiClient, "v2").mockImplementation(async (path: string) => {
    if (path === "/accounts") return {accounts: []} as never;
    return {watchlist: [], subscriptions: [], runtime: {state:"stopped"}, positions: [], decisions: [],
      macro_calendar: {status:"SCHEDULE_ONLY",provider:"Forex Factory"},
      macro_events: [{event_id:"ff_test",title:"Core CPI y/y",currency:"USD",event_time:new Date().toISOString(),previous:"2%",forecast:"3%",actual:null,source_url:"https://www.forexfactory.com/calendar",schedule_only:true,directive:"NONE"}]} as never;
  });
  render(<I18nProvider><MemoryRouter><V2WorkspacePage /></MemoryRouter></I18nProvider>);
  expect(await screen.findByText(/核心消费者价格指数 同比/)).toBeInTheDocument();
  expect(screen.getAllByText("公布值来源待接入")).toHaveLength(2);
  expect(screen.getByRole("link", {name:/查看原始来源/})).toHaveAttribute("href", "https://www.forexfactory.com/calendar");
  fireEvent.click(screen.getByRole("button", {name:"更新公开日历"}));
  await waitFor(() => expect(apiClient.v2).toHaveBeenCalledWith("/macro-calendar/refresh", "POST"));
});

it("shows official actuals with their source, observation time and local release time", async () => {
  vi.spyOn(apiClient, "v2").mockImplementation(async (path: string) => {
    if (path === "/accounts") return {accounts: []} as never;
    return {watchlist: [], subscriptions: [], runtime: {state:"stopped"}, positions: [], decisions: [],
      macro_calendar: {status:"ACTUALS_AVAILABLE",provider:"Forex Factory + BLS",actual_supported:true},
      macro_events: [{event_id:"ff_official",title:"Unemployment Rate",currency:"USD",
        event_time:"2026-10-02T12:30:00Z",previous:"4.1%",forecast:"4.1%",actual:"4.2%",
        source_url:"https://www.forexfactory.com/calendar",directive:"NONE",schedule_only:false,
        actual_status:"VERIFIED",actual_provider:"BLS",actual_reference_period:"2026-09",
        actual_available_at:"2026-10-02T23:20:00Z",actual_source_url:"https://www.bls.gov/news.release/empsit.nr0.htm"}]} as never;
  });
  render(<I18nProvider><MemoryRouter><V2WorkspacePage /></MemoryRouter></I18nProvider>);
  expect(await screen.findByText("4.2%")).toBeInTheDocument();
  expect(screen.getByText("已公布")).toBeInTheDocument();
  expect(screen.getByRole("link", {name:/查看官方公布/})).toHaveAttribute("href", "https://www.bls.gov/news.release/empsit.nr0.htm");
  expect(screen.getByText(/2026-09.*获取时间/)).toBeInTheDocument();
  // The schedule is UTC; the product consistently renders Hong Kong time.
  expect(screen.getByText(/20:30:00/)).toBeInTheDocument();
  expect(screen.queryByText("此源不提供")).not.toBeInTheDocument();
});

it("distinguishes failed official synchronization from an unreleased value", async () => {
  vi.spyOn(apiClient, "v2").mockImplementation(async (path: string) => {
    if (path === "/accounts") return {accounts: []} as never;
    return {watchlist: [], subscriptions: [], runtime: {state:"stopped"}, positions: [], decisions: [],
      macro_calendar: {status:"PARTIAL",actual_supported:true},
      macro_events: [{event_id:"ff_failed",title:"Non-Farm Employment Change",currency:"USD",
        event_time:"2020-01-03T13:30:00Z",previous:"100K",forecast:"120K",actual:null,
        actual_status:"SOURCE_FETCH_FAILED",actual_error:"官方源暂时无法访问；下一次更新重试。",
        source_url:"https://www.forexfactory.com/calendar",directive:"NONE",schedule_only:false}]} as never;
  });
  render(<I18nProvider><MemoryRouter><V2WorkspacePage /></MemoryRouter></I18nProvider>);
  expect(await screen.findAllByText("官方源同步失败")).toHaveLength(2);
  expect(screen.getByText("官方源暂时无法访问；下一次更新重试。")).toBeInTheDocument();
  expect(screen.queryByText("待核实公布值")).not.toBeInTheDocument();
});

it("keeps a verified actual visible while exposing a failed refresh and derived method", async () => {
  vi.spyOn(apiClient, "v2").mockImplementation(async (path: string) => {
    if (path === "/accounts") return {accounts: []} as never;
    return {watchlist: [], subscriptions: [], runtime: {state:"stopped"}, positions: [], decisions: [],
      macro_calendar: {status:"PARTIAL",actual_supported:true},
      macro_events: [{event_id:"ff_cached",title:"Non-Farm Employment Change",currency:"USD",
        event_time:"2020-01-03T13:30:00Z",actual:"29K",actual_status:"VERIFIED",
        actual_provider:"BLS",actual_method:"DERIVED_FROM_OFFICIAL_SERIES",
        actual_reference_period:"2019-12",actual_available_at:"2020-01-03T13:31:00Z",
        actual_error:"官方源暂时无法访问。",directive:"NONE",schedule_only:false}]} as never;
  });
  render(<I18nProvider><MemoryRouter><V2WorkspacePage /></MemoryRouter></I18nProvider>);
  expect(await screen.findByText("29K")).toBeInTheDocument();
  expect(screen.getByText(/由官方序列计算/)).toBeInTheDocument();
  expect(screen.getByText(/保留已核验公布值；本次同步：官方源暂时无法访问/)).toBeInTheDocument();
});
