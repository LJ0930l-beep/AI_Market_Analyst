import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { apiClient } from "../api/client";
import { DecisionExperiencePanel } from "./DecisionExperiencePanel";

afterEach(() => vi.restoreAllMocks());

it("shows settled decision memory with the recorded lesson and its source limits", async () => {
  vi.spyOn(apiClient, "v2").mockResolvedValue({ items: [
    { memory_id: "closed-1", symbol: "BTCUSDT", action: "OPEN_LONG", cycle_status: "COMPLETED", decision_at: "2026-09-20T08:00:00Z", summary_zh: "顺势突破做多", lesson_zh: "BTCUSDT 已平仓：净盈亏 +4.20 USDT（毛 +5.00 / 费用 0.80）。", outcome_status: "WIN", outcome_pnl: 4.2, strategy_name: "趋势加速", outcome_evidence: { position_id: "position-1", gross_realized: 5, fees: .8, fill_count: 2 } },
    { memory_id: "pending-1", symbol: "ETHUSDT", action: "OPEN_SHORT", cycle_status: "COMPLETED", decision_at: "2026-09-20T09:00:00Z", summary_zh: "回落做空", outcome_status: null, outcome_pnl: null },
  ] } as never);

  render(<DecisionExperiencePanel accountId="gate_testnet" />);

  const panel = await screen.findByRole("region", { name: "AI 平仓复盘与长期经验" });
  expect(within(panel).getByText("1 笔已结算 · 2 条记忆")).toBeInTheDocument();
  expect(within(panel).getByText("等待平仓结算")).toBeInTheDocument();
  const closed = within(panel).getByText("趋势加速").closest("details");
  expect(closed).not.toBeNull();
  fireEvent.click(within(closed as HTMLElement).getByText("趋势加速").closest("summary") as HTMLElement);
  expect(await within(closed as HTMLElement).findByText(/已平仓：净盈亏 \+4.20 USDT/)).toBeInTheDocument();
  expect(within(closed as HTMLElement).getByText(/持仓 position-1 · 毛盈亏 5.00 USDT · 费用 0.80 USDT · 成交 2 笔/)).toBeInTheDocument();
  expect(within(closed as HTMLElement).getByText(/未提供止损\/止盈触发诱因或独立滑点归因/)).toBeInTheDocument();
  await waitFor(() => expect(apiClient.v2).toHaveBeenCalledWith("/ai-session/memory?account_id=gate_testnet&limit=20"));
});
