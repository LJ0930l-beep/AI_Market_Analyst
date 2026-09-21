import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, apiClient } from "../api/client";
import { AITraderPanel, RiskLockCountdown } from "./AITraderPanel";
import { MemoryRouter } from "react-router-dom";

describe("AI trader durable plan console", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shows the server-reported cooldown countdown and stops ticking at expiry", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2030-01-02T12:00:00Z"));
    const { unmount } = render(<RiskLockCountdown blockedUntil="2030-01-02T12:00:02Z" reasons={["TWO_LOSSES"]} />);
    expect(screen.getByRole("group", { name: "风控静默锁状态" })).toHaveTextContent("冷却倒计时 00:00:02");
    expect(screen.getByRole("group", { name: "风控静默锁状态" })).toHaveTextContent("开仓权限以服务端风控状态为准");
    act(() => { vi.advanceTimersByTime(2_000); });
    expect(screen.getByLabelText("冷却倒计时")).toHaveTextContent("冷却时间已到，等待服务端确认解除");
    expect(vi.getTimerCount()).toBe(0);
    unmount();
    vi.useRealTimers();
  });

  it("shows stopped worker despite persisted running session and accepts slow account reads", async () => {
    let resolveAccount!: (value: never) => void;
    const slow = new Promise<never>(resolve => { resolveAccount = resolve; });
    vi.spyOn(apiClient, 'v2').mockImplementation(async (path: string) => {
      if (path === '/accounts') return { accounts: [{ account_id: 'gate_testnet', mode: 'TESTNET', venue: 'gate' }] } as never;
      if (path.startsWith('/ai-session/status')) return { session: { state: 'RUNNING' }, ai_session: { state: 'STOPPED', enabled: false, worker_alive: false } } as never;
      if (path.startsWith('/gate/account?')) return slow;
      if (path.startsWith('/ai-session/cycles')) return { cycles: [{ cycle_id: 'cycle-new', action: 'WAIT', decision_origin: 'MODEL', model_called: true, reason: '新闻与趋势冲突', payload: { strategy_plan: { name: '当前模型方案', thesis: '谨慎等待', entry_conditions: ['等待共振'], exit_conditions: ['结构失效'] } } }] } as never;
      return {} as never;
    });
    render(<MemoryRouter><AITraderPanel activeAccount="gate_testnet" currentMode="TESTNET" /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText('SESSION STATE').parentElement).toHaveTextContent('STOPPED'));
    const decisionPanel = await screen.findByRole('region', { name: 'AI 交易计划与时间线' });
    expect(decisionPanel).toHaveTextContent('当前模型方案');
    expect(decisionPanel).toHaveTextContent('WAIT');
    resolveAccount({ data_status: 'AVAILABLE', equity: 50172, available_margin: 50172, used_margin: 0, positions: [], pending_orders: [] } as never);
    await waitFor(() => expect(screen.getByTestId('ai-remote-account-card')).toHaveTextContent('50,172'));
  });

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
    expect(screen.getByTestId('strategy-run-summary')).toHaveTextContent('未报告');
    expect(screen.getByTestId('strategy-run-summary')).not.toHaveTextContent('15 分钟');
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
            stage_trace: [{ stage: "AI_MODEL", status: "BLOCKED", human_message: "Bonsai-2-27B 未运行。" }],
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
    expect(card).toHaveTextContent("系统阻断 · 未调用模型");
    expect(card).toHaveTextContent("未调用");
    expect(screen.getByTestId("cycle-decision-origin")).not.toHaveTextContent("AI 决策");
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

  it("waits through a stale runtime lease and completes one start action", async () => {
    let starts = 0;
    const v2 = vi.spyOn(apiClient, 'v2').mockImplementation(async (path: string, method = 'GET') => {
      if (path === '/accounts') return { accounts: [{ account_id: 'paper_handoff', mode: 'PAPER', venue: 'simulated' }] } as never;
      if (path.startsWith('/ai-session/status')) return { session: { state: 'STOPPED', generation: 2 }, ai_session: { state: 'STOPPED', enabled: false, worker_alive: false } } as never;
      if (path.startsWith('/trade-plans')) return { plans: [] } as never;
      if (path.startsWith('/ai-session/cycles')) return { cycles: [] } as never;
      if (method === 'POST' && path.startsWith('/ai-session/start')) {
        starts += 1;
        if (starts === 1) throw new ApiError(409, { code: 'Runtime lease held by another instance', message: 'Runtime lease held by another instance' });
        return { state: 'RUNNING' } as never;
      }
      return {} as never;
    });
    render(<MemoryRouter><AITraderPanel currentMode="PAPER" activeAccount="paper_handoff" /></MemoryRouter>);
    const start = await screen.findByRole('button', { name: /Start AI trading/i });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    expect(await screen.findByText(/正在接管上一实例的安全锁/)).toBeInTheDocument();
    await waitFor(() => expect(starts).toBe(2), { timeout: 2500 });
    expect(v2).toHaveBeenCalledWith('/ai-session/start?account_id=paper_handoff', 'POST', undefined);
  });

  it("shows the effective strategy schedule and separates model confidence from trigger completion", async () => {
    vi.spyOn(apiClient, 'v2').mockImplementation(async (path: string) => {
      if (path === '/accounts') return { accounts: [{ account_id: 'paper_ui', mode: 'PAPER', venue: 'simulated' }] } as never;
      if (path.startsWith('/ai-session/status')) return {
        session: { state: 'RUNNING', generation: 4 },
        ai_session: {
          state: 'RUNNING', enabled: true, worker_alive: true, max_symbols: 4,
          schedule: { interval_minutes: 5, strategy_id: 'aggressive_breakout_5m', strategy_name: '突破脉冲', strategy_revision: 7, alignment: 'minute % 5 == 0', next_scan_at: '2030-01-02T12:05:00Z' },
        },
      } as never;
      if (path.startsWith('/ai-session/cycles')) return { cycles: [{ action: 'WAIT', status: 'COMPLETED', decision_origin: 'MODEL', model_called: true, reason: '等待回踩确认', timestamp: '2030-01-02T12:00:00Z', payload: { model_output: { extra_fields: { confidence: 65 } } } }] } as never;
      if (path.startsWith('/trade-plans')) return { plans: [] } as never;
      if (path.startsWith('/gate/account')) return { data_status: 'UNAVAILABLE' } as never;
      return {} as never;
    });

    render(<MemoryRouter><AITraderPanel currentMode="PAPER" activeAccount="paper_ui" /></MemoryRouter>);

    expect(await screen.findByTestId('strategy-run-summary')).toHaveTextContent('突破脉冲');
    expect(screen.getByTestId('strategy-run-summary')).toHaveTextContent('5 分钟');
    expect(screen.getByTestId('strategy-run-summary')).toHaveTextContent('4 个');
    expect(screen.getByTestId('market-readiness')).toHaveTextContent('—');
    expect(screen.getByTestId('market-readiness')).toHaveTextContent('暂无可信触发度');
    expect(screen.getByRole('region', { name: 'AI execution overview' })).toHaveTextContent('AI 置信度 65 / 100 · 不代表胜率');
    expect(screen.getByRole('region', { name: 'AI execution overview' })).not.toHaveTextContent('BONSAI COMPLETE');
  });

  it("shows Bonsai as verified only for a fresh manifest-backed runtime health response", async () => {
    vi.spyOn(apiClient, 'v2').mockResolvedValue({} as never);
    vi.spyOn(apiClient, 'modelHealth').mockResolvedValue({
      status: 'READY',
      available: true,
      model_available: true,
      model_id: 'Bonsai-2-27B-PTQ1_0',
      actual_model_id: 'Ternary-Bonsai-2-27B-PTQ1_0.gguf',
      model_identity_source: 'verified_manifest',
      checked_at: new Date().toISOString(),
    } as never);

    render(<MemoryRouter><AITraderPanel currentMode="PAPER" activeAccount="paper_health" /></MemoryRouter>);

    expect(await screen.findByLabelText('Bonsai model identity status')).toHaveTextContent('Bonsai 2.27B · 身份已核验');
  });

  it("downgrades an expired Bonsai health response in the trading cockpit", async () => {
    vi.spyOn(apiClient, 'v2').mockResolvedValue({} as never);
    vi.spyOn(apiClient, 'modelHealth').mockResolvedValue({
      status: 'READY',
      available: true,
      model_available: true,
      model_id: 'Bonsai-2-27B-PTQ1_0',
      actual_model_id: 'Ternary-Bonsai-2-27B-PTQ1_0.gguf',
      model_identity_source: 'verified_manifest',
      checked_at: new Date(Date.now() - 120_000).toISOString(),
    } as never);

    render(<MemoryRouter><AITraderPanel currentMode="PAPER" activeAccount="paper_health_stale" /></MemoryRouter>);

    expect(await screen.findByLabelText('Bonsai model identity status')).toHaveTextContent('Model identity check expired');
  });

  it("does not claim Bonsai is verified when the model health request fails", async () => {
    vi.spyOn(apiClient, 'v2').mockResolvedValue({} as never);
    vi.spyOn(apiClient, 'modelHealth').mockRejectedValue(new Error('model health unavailable'));

    render(<MemoryRouter><AITraderPanel currentMode="PAPER" activeAccount="paper_health_error" /></MemoryRouter>);

    expect(await screen.findByLabelText('Bonsai model identity status')).toHaveTextContent('Model identity unverified');
  });
});
