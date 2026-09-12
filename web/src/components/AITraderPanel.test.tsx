import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiClient } from "../api/client";
import { AITraderPanel } from "./AITraderPanel";
import { MemoryRouter } from "react-router-dom";

describe("AI trader durable plan console", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders scoped plan behavior and sends a real session control action", async () => {
    const v2 = vi.spyOn(apiClient, "v2").mockImplementation(async (path: string, method = "GET") => {
      if (path === "/accounts") {
        return { accounts: [{ account_id: "paper_test", mode: "PAPER", venue: "simulated" }] } as never;
      }
      if (path.startsWith("/qwen-market-scans/status")) return { running: true, latest: null } as never;
      if (path.startsWith("/ai-session/status")) {
        return {
          session: { state: "RUNNING", generation: 4 },
          latest_cycle: null,
          protection_summary: { active_positions: 1, symbols: ["BTCUSDT"] },
          market_freshness: { status: "HEALTHY" },
          model_status: { status: "UNVERIFIED" },
          last_market_event_at: "2030-01-02T12:00:00Z",
        } as never;
      }
      if (path.startsWith("/trade-plans")) {
        return {
          plans: [{
            plan_id: "plan_1",
            status: "WAITING_TRIGGER",
            plan: {
              symbol: "BTCUSDT",
              action: "REDUCE_POSITION",
              reduce_fraction: 0.25,
              leverage: 3,
              entry_trigger: "typed price condition",
              evidence: [{ type: "local_fixture" }],
              conditions: { entry: { type: "PRICE" } },
            },
            execution_result: { condition_evaluation: { status: "WAITING_TRIGGER" } },
          }],
        } as never;
      }
      if (method === "POST" && path.startsWith("/ai-session/pause")) {
        return { running: false } as never;
      }
      return {} as never;
    });

    render(<MemoryRouter><AITraderPanel currentMode="PAPER" activeAccount="paper_test" /></MemoryRouter>);

    expect(await screen.findByTestId("trade-plans-panel")).toHaveTextContent("BTCUSDT");
    expect(screen.getByTestId("trade-plans-panel")).toHaveTextContent("reduce 0.25");
    expect(screen.getByTestId("trade-plans-panel")).toHaveTextContent("leverage 3");
    expect(screen.getByTestId("trade-plans-panel")).toHaveTextContent("WAITING_TRIGGER");

    fireEvent.click(screen.getByRole("button", { name: /Pause AI trading/i }));
    await waitFor(() => expect(v2).toHaveBeenCalledWith("/ai-session/pause?account_id=paper_test", "POST", undefined));
    expect(v2.mock.calls.some(([path]) => path.startsWith('/qwen-market-scans/'))).toBe(false);
  });

  it("does not render a missing protection count as zero", async () => {
    vi.spyOn(apiClient, "v2").mockImplementation(async (path: string) => {
      if (path === "/accounts") {
        return { accounts: [{ account_id: "paper_unknown", mode: "PAPER", venue: "simulated" }] } as never;
      }
      if (path.startsWith("/qwen-market-scans/status")) return { running: false, latest: null } as never;
      if (path.startsWith("/ai-session/status")) {
        return { session: { state: "RUNNING", generation: 1 }, model_status: { status: "UNVERIFIED" } } as never;
      }
      if (path.startsWith("/trade-plans")) return { plans: [] } as never;
      return {} as never;
    });

    render(<MemoryRouter><AITraderPanel currentMode="PAPER" activeAccount="paper_unknown" /></MemoryRouter>);

    await waitFor(() => expect(screen.getByText("PROTECTED POSITIONS").parentElement).toHaveTextContent("UNKNOWN"));
  });

  it("does not surface legacy system-blocked cycles as a Qwen decision", async () => {
    vi.spyOn(apiClient, "v2").mockImplementation(async (path: string) => {
      if (path === "/accounts") return { accounts: [{ account_id: "paper_trace", mode: "PAPER", venue: "simulated" }] } as never;
      if (path.startsWith("/qwen-market-scans/status")) return { running: false, latest: null } as never;
      if (path.startsWith("/ai-session/cycles")) return { cycles: [{ action: 'SYSTEM_BLOCKED', decision_origin: 'SYSTEM', model_called: false, reason: 'SMART_MODEL_UNAVAILABLE' }] } as never;
      if (path.startsWith("/ai-session/status")) {
        return {
          session: { state: "PAUSED", generation: 3 },
          latest_cycle: {
            cycle_id: "cycle-system-1",
            action: "SYSTEM_BLOCKED",
            reason: "SMART_MODEL_UNAVAILABLE",
            decision_origin: "SYSTEM",
            operational_state: "SYSTEM_BLOCKED",
            model_called: false,
            model_result: "NOT_RUN",
            block_stage: "AI_MODEL",
            human_message: "模型不可用，未提交新订单。",
            stage_trace: [{ stage: "AI_MODEL", status: "BLOCKED", human_message: "Qwen3.5-9B 未运行。" }],
          },
          market_freshness: { status: "HEALTHY" },
          model_status: { status: "SMART_MODEL_UNAVAILABLE" },
        } as never;
      }
      if (path.startsWith("/trade-plans")) return { plans: [] } as never;
      return {} as never;
    });

    render(<MemoryRouter><AITraderPanel currentMode="PAPER" activeAccount="paper_trace" /></MemoryRouter>);

    const card = await screen.findByTestId("ai-decision-cycle-card");
    await waitFor(() => expect(card).toHaveTextContent("SYSTEM_BLOCKED"));
    expect(card).toHaveTextContent("系统检查");
    expect(card).toHaveTextContent("未调用");
    expect(card).not.toHaveTextContent("AI 决策");
    expect(card).not.toHaveTextContent("AUTHORIZATION_REQUIRED");
  });

  it("starts the selected trading account and renders the model JSON", async () => {
    const v2 = vi.spyOn(apiClient, 'v2').mockImplementation(async (path: string) => {
      if (path === '/accounts') return { accounts: [{ account_id: 'paper_ai', mode: 'PAPER', venue: 'simulated' }] } as never;
      if (path.startsWith('/ai-session/status')) return { session: { state: 'IDLE', generation: 1 } } as never;
      if (path.startsWith('/trade-plans')) return { plans: [] } as never;
      if (path.startsWith('/ai-session/cycles')) return { cycles: [{ action: 'WAIT', decision_origin: 'MODEL', model_called: true, reason: '等待新闻确认', payload: { strategy_plan: { name: '自拟趋势策略' } } }] } as never;
      return {} as never;
    });
    render(<MemoryRouter><AITraderPanel currentMode="PAPER" activeAccount="paper_ai" /></MemoryRouter>);
    await waitFor(() => expect(screen.getByTestId('ai-decision-cycle-card')).toHaveTextContent('自拟趋势策略'));
    fireEvent.click(screen.getByRole('button', { name: /Start AI trading/ }));
    await waitFor(() => expect(v2).toHaveBeenCalledWith('/ai-session/start?account_id=paper_ai', 'POST', undefined));
    expect(v2.mock.calls.some(([path]) => path.startsWith('/qwen-market-scans/'))).toBe(false);
  });
});
