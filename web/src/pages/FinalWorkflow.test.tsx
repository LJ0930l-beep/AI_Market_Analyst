import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { apiClient } from "../api/client";
import { I18nProvider } from "../i18n";
import { V2WorkspacePage } from "./V2WorkspacePage";
import { createFakeClient } from "../test/fakeClient";

beforeEach(() => {
  window.localStorage.setItem("ai-market-analyst.language", "zh-CN");
  const fake = createFakeClient();
  vi.spyOn(apiClient, "marketIntelligence").mockImplementation(fake.marketIntelligence);
  vi.spyOn(apiClient, "chartBars").mockImplementation(fake.chartBars);
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

it("keeps the console and analysis on the same account without creating orders", async () => {
  const query = vi.spyOn(apiClient, "v2").mockImplementation(async (path: string) => {
    if (path === "/accounts") return {accounts: ["paper_a", "paper_b"].map(account_id => ({account_id, mode: "PAPER", venue: "simulated"}))} as never;
    if (path.startsWith("/ai-analysis")) return {account: {initial_capital_usdt:10000,current_equity_usdt:10000,net_pnl_usdt:0,total_roi_pct:0,margin_used_usdt:0,margin_available_usdt:10000,win_rate_pct:0,total_trades:0,winning_trades:0,losing_trades:0,profit_factor:1,max_drawdown_pct:0,avg_leverage:1},execution_records: [], execution_positions: [], execution_orders: [], trades: [], strategy_matrix: [], equity_curve: []} as never;
    return {watchlist: [], subscriptions: [], runtime: {state: "stopped"}, positions: [], decisions: [], allow_unknown_macro: false} as never;
  });
  render(<I18nProvider><MemoryRouter><V2WorkspacePage surface="analysis" /></MemoryRouter></I18nProvider>);
  const selector = await screen.findByRole("combobox", {name: "交易账户"});
  fireEvent.change(selector, {target: {value: "paper_b"}});
  await waitFor(() => expect(query).toHaveBeenCalledWith(expect.stringContaining("/ai-analysis?account_id=paper_b"), "GET", undefined, expect.any(AbortSignal)));
  expect(window.sessionStorage.getItem("aima.trading-account")).toBe("paper_b");
  expect(screen.getByRole("heading", {name: /AI 自主交易控制台/})).toBeInTheDocument();
  expect(screen.getByRole("link", {name: "查看本账户做单分析"})).toHaveAttribute("href", "/ai-analysis");
  expect(query.mock.calls.every(call => call[1] === undefined || call[1] === "GET")).toBe(true);
  vi.useFakeTimers();
  const before = query.mock.calls.filter(call => call[0].startsWith("/ai-analysis")).length;
  await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
  // Timer was scheduled before fake timers; use account change to establish a
  // new scope under the controllable clock, then verify recurring read polling.
  fireEvent.change(selector, {target: {value: "paper_a"}});
  await act(async () => { await vi.advanceTimersByTimeAsync(6100); });
  expect(query.mock.calls.filter(call => call[0].startsWith("/ai-analysis")).length).toBeGreaterThan(before + 1);
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
  expect(screen.getByText("此源不提供")).toBeInTheDocument();
  expect(screen.getByRole("link", {name:/查看原始来源/})).toHaveAttribute("href", "https://www.forexfactory.com/calendar");
  fireEvent.click(screen.getByRole("button", {name:"更新公开日历"}));
  await waitFor(() => expect(apiClient.v2).toHaveBeenCalledWith("/macro-calendar/refresh", "POST"));
});
