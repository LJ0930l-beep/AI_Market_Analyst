import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiClient } from "../api/client";
import { I18nProvider } from "../i18n";
import { V2WorkspacePage } from "./V2WorkspacePage";
import { createFakeClient } from "../test/fakeClient";

function show(surface:"dashboard"|"monitor"|"strategies"|"intel"|"analysis"|"gate-live"="dashboard") {
  render(<I18nProvider><MemoryRouter><V2WorkspacePage surface={surface}/></MemoryRouter></I18nProvider>);
}
beforeEach(()=>{
  const fake=createFakeClient();
  vi.spyOn(apiClient,"marketIntelligence").mockImplementation(fake.marketIntelligence);
  vi.spyOn(apiClient,"chartBars").mockImplementation(fake.chartBars);
  vi.spyOn(apiClient,"v2").mockResolvedValue({watchlist:[{symbol:"BTCUSDT"}],subscriptions:[],runtime:{state:"stopped"},decisions:[],positions:[],allow_unknown_macro:false});
});
afterEach(()=>vi.restoreAllMocks());
describe("V2 task workspace",()=>{
  it("keeps the AI trading cockpit above the chart and market radar",async()=>{
    show();
    const cockpit=await screen.findByRole("region",{name:"AI execution overview"});
    const grid=document.querySelector(".v2-grid");
    expect(grid).not.toBeNull();
    expect(cockpit.compareDocumentPosition(grid as Node) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const radar = await screen.findByRole("heading", { name: "盘口与全球市场" });
    expect(grid!.compareDocumentPosition(radar) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(vi.mocked(apiClient.v2).mock.calls.some(([path]) => path === "/market-radar?symbols=BTCUSDT")).toBe(true);
  });
  it("opens the microstructure subtab inside trading analysis", async () => {
    show("analysis");
    const navigation = await screen.findByRole("group", { name: "Trading analysis sections" });
    const microstructureButton = within(navigation).getByRole("button", { name: "Market microstructure & derivatives" });
    expect(within(navigation).getByRole("button", { name: "AI decisions & trade review" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(microstructureButton);
    expect(microstructureButton).toHaveAttribute("aria-pressed", "true");
    expect(await screen.findByRole("heading", { name: "市场微观结构与衍生品雷达" })).toBeInTheDocument();
    expect(apiClient.v2).toHaveBeenCalledWith("/market-radar", "GET", undefined, expect.any(AbortSignal));
    expect(screen.queryByText("MODEL ROUTE / VERIFIED RUNTIME")).not.toBeInTheDocument();
  });
  it("adds liquidation, chain-flow and cross-market radar to the intelligence room", async () => {
    show("intel");
    expect(await screen.findByRole("heading", { name: "爆仓分布" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "链上资金走向" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "全球宏观与跨市场走廊" })).toBeInTheDocument();
    expect(apiClient.v2).toHaveBeenCalledWith("/market-radar", "GET", undefined, expect.any(AbortSignal));
  });
  it("opens the strategy route on the account-scoped AI strategy studio", async () => {
    window.sessionStorage.setItem("aima.trading-account", "gate_testnet");
    const sections = { role: "role", frequency: "discipline", entry_standards: "entry", decision_process: "exits", custom_prompt: "limits" };
    const execution = {
      symbols: [], universe_mode: "ALL", scan_interval_minutes: 15, direction: "BOTH", sizing_mode: "RISK_BASED",
      fixed_notional_usdt: 1000, equity_notional_pct: 5, max_notional_usdt: 5000, risk_per_trade_pct: .2,
      leverage: 3, max_positions: 3, max_margin_pct: 20, min_confidence: 68, min_net_rr: 2,
      cooldown_minutes: 30, order_preference: "LIMIT", atr_adaptive_sizing: true,
      consecutive_loss_lock_enabled: true, us_open_defense_enabled: true,
    };
    const templates = [
      { id: "aggressive_impulse", name: "Breakout Pulse", sections, style: "AGGRESSIVE", scan_interval_minutes: 5, order_preference: "AUTO", profile: { limit_priority: true, order_preference: "AUTO" } },
      { id: "aggressive_breakout", name: "Trend Acceleration", sections, style: "AGGRESSIVE", scan_interval_minutes: 15, order_preference: "AUTO", profile: { limit_priority: true, order_preference: "AUTO" } },
      { id: "conservative_pullback", name: "Measured Pullback", sections, style: "CONSERVATIVE", scan_interval_minutes: 15, order_preference: "AUTO", profile: { limit_priority: true, order_preference: "AUTO" } },
      { id: "conservative_defense", name: "Range Confirmation", sections, style: "CONSERVATIVE", scan_interval_minutes: 15, order_preference: "AUTO", profile: { limit_priority: true, order_preference: "AUTO" } },
    ];
    vi.mocked(apiClient.v2).mockImplementation(async (path: string) => {
      if (path === "/accounts") return { accounts: [{ account_id: "gate_testnet", mode: "TESTNET", venue: "gate" }] } as never;
      if (path.startsWith("/ai-strategy")) return { active: { account_id: "gate_testnet", revision: 4, template_id: "conservative_pullback", name: "Measured Pullback", style: "CONSERVATIVE", sections, execution, digest: "active" }, templates } as never;
      if (path.startsWith("/gate/markets")) return { markets: [{ symbol: "BTC_USDT" }] } as never;
      if (path.startsWith("/gate/account")) return { data_status: "UNAVAILABLE" } as never;
      if (path.startsWith("/ai-session/memory")) return { items: [] } as never;
      return { watchlist: [{ symbol: "BTCUSDT" }], subscriptions: [], runtime: { state: "stopped" }, decisions: [], positions: [], allow_unknown_macro: false } as never;
    });

    show("strategies");

    expect(await screen.findByRole("heading", { name: "让策略驱动 AI 找机会。" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Breakout Pulse/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Trend Acceleration/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Measured Pullback/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Range Confirmation/ })).toBeInTheDocument();
    expect(document.querySelectorAll(".ai-template-card")).toHaveLength(4);
    expect(screen.getByRole("button", { name: /Trend Acceleration/ })).toHaveTextContent("限价优先 · 阈值达标才市价");
    expect(document.querySelector(".v2-strategy-card")).toBeNull();
  });
  it("shows useful deterministic data and explicit analysis without model POST",async()=>{
    show();
    expect(await screen.findByText(/No strategy proposal yet/)).toBeInTheDocument();
    expect(screen.getByRole("link",{name:"Ask AI explicitly"})).toHaveAttribute("href","/consult?symbol=BTCUSDT");
    expect(apiClient.v2).toHaveBeenCalledWith("/workspace","GET",undefined,expect.any(AbortSignal));
    expect(screen.getByText(/Structured macro provider not configured/)).toBeInTheDocument();
  });
  it("does not invent a Bonsai ready state or decision when runtime evidence is absent", async () => {
    show("analysis");
    const panel = await screen.findByRole("region", { name: "Bonsai trading analysis and decision records" });
    expect(within(panel).getByText(/Latest decision · historical ledger/)).toBeInTheDocument();
    expect(within(panel).getByText("No decision record")).toBeInTheDocument();
    expect(within(panel).getByText(/No recorded decision yet/)).toBeInTheDocument();
    expect(within(panel).getByText(/Model status: UNKNOWN/)).toBeInTheDocument();
    expect(within(panel).queryByText(/MODEL_READY|ACTIVE/)).not.toBeInTheDocument();
    expect(within(panel).queryByText(/autonomous analysis online|Ollama/)).not.toBeInTheDocument();
  });
  it("shows model availability only from the workspace runtime health response", async () => {
    vi.mocked(apiClient.v2).mockImplementation(async (path: string) =>
      path.startsWith("/workspace")
        ? {
            watchlist: [{ symbol: "BTCUSDT" }], subscriptions: [], runtime: { state: "stopped" }, decisions: [], positions: [], allow_unknown_macro: false,
            risk_cockpit: { model: { status: "UNAVAILABLE", required_model: "Bonsai-2-27B-PTQ1_0", model_id: "Bonsai-2-27B-PTQ1_0", available: false, model_available: false, checked_at: new Date().toISOString() } },
          }
        : path === "/accounts"
          ? { accounts: [] }
          : { watchlist: [{ symbol: "BTCUSDT" }], subscriptions: [], runtime: { state: "stopped" }, decisions: [], positions: [], allow_unknown_macro: false },
    );
    show("analysis");
    const panel = await screen.findByRole("region", { name: "Bonsai trading analysis and decision records" });
    expect(within(panel).getByText(/Target model: Bonsai-2-27B-PTQ1_0 · Model status: UNAVAILABLE/)).toBeInTheDocument();
    expect(within(panel).getByText("UNAVAILABLE")).toBeInTheDocument();
    expect(within(panel).queryByText(/ACTIVE|ready for the active strategy cadence/i)).not.toBeInTheDocument();
  });
  it("shows only the model artifact basename from runtime health", async () => {
    vi.mocked(apiClient.v2).mockImplementation(async (path: string) =>
      path.startsWith("/workspace")
        ? {
            watchlist: [{ symbol: "BTCUSDT" }], subscriptions: [], runtime: { state: "stopped" }, decisions: [], positions: [], allow_unknown_macro: false,
            risk_cockpit: { model: { status: "READY", model_id: "Bonsai-2-27B-PTQ1_0", actual_model_id: "D:\\models\\Ternary-Bonsai-2-27B-PTQ1_0.gguf", model_identity_source: "verified_manifest", available: true, model_available: true, checked_at: new Date().toISOString() } },
          }
        : path === "/accounts"
          ? { accounts: [] }
          : { watchlist: [{ symbol: "BTCUSDT" }], subscriptions: [], runtime: { state: "stopped" }, decisions: [], positions: [], allow_unknown_macro: false },
    );
    show("analysis");
    const panel = await screen.findByRole("region", { name: "Bonsai trading analysis and decision records" });
    expect(within(panel).getByText(/Target model: Bonsai-2-27B-PTQ1_0 · Model status: READY/)).toBeInTheDocument();
    expect(within(panel).getByText("Ternary-Bonsai-2-27B-PTQ1_0.gguf")).toBeInTheDocument();
    expect(within(panel).queryByText(/D:\\models/)).not.toBeInTheDocument();
  });
  it("downgrades a verified Bonsai identity after its runtime health receipt expires", async () => {
    vi.mocked(apiClient.v2).mockImplementation(async (path: string) =>
      path.startsWith("/workspace")
        ? {
            watchlist: [{ symbol: "BTCUSDT" }], subscriptions: [], runtime: { state: "stopped" }, decisions: [], positions: [], allow_unknown_macro: false,
            risk_cockpit: { model: { status: "READY", model_id: "Bonsai-2-27B-PTQ1_0", actual_model_id: "Ternary-Bonsai-2-27B-PTQ1_0.gguf", model_identity_source: "verified_manifest", available: true, model_available: true, checked_at: new Date(Date.now() - 120_000).toISOString() } },
          }
        : path === "/accounts"
          ? { accounts: [] }
          : { watchlist: [{ symbol: "BTCUSDT" }], subscriptions: [], runtime: { state: "stopped" }, decisions: [], positions: [], allow_unknown_macro: false },
    );
    show("analysis");
    const panel = await screen.findByRole("region", { name: "Bonsai trading analysis and decision records" });
    expect(within(panel).getByText(/Model status: STALE/)).toBeInTheDocument();
    expect(within(panel).getByText("Identity unverified")).toBeInTheDocument();
    expect(within(panel).queryByText("Ternary-Bonsai-2-27B-PTQ1_0.gguf")).not.toBeInTheDocument();
  });
  it("does not assign a runtime model to a decision without a persisted receipt", async () => {
    window.sessionStorage.setItem("aima.trading-account", "paper_ui");
    vi.mocked(apiClient.v2).mockImplementation(async (path: string) => {
      if (path.startsWith("/workspace")) return {
        watchlist: [{ symbol: "BTCUSDT" }], subscriptions: [], runtime: { state: "stopped" }, decisions: [], positions: [], allow_unknown_macro: false,
        risk_cockpit: { model: { status: "READY", model_id: "Bonsai-2-27B-PTQ1_0", actual_model_id: "Ternary-Bonsai-2-27B-PTQ1_0.gguf", model_identity_source: "verified_manifest", available: true, model_available: true, checked_at: new Date().toISOString() } },
      };
      if (path.startsWith("/accounts")) return { accounts: [{ account_id: "paper_ui", mode: "PAPER", venue: "simulated" }] };
      if (path.startsWith("/ai-analysis/dashboard")) return {
        status: "AVAILABLE", scope: { account_id: "paper_ui" },
        timeline: [{ cycle_id: "blocked-cycle", action: "SYSTEM_BLOCKED", status: "BLOCKED", reason: "MODEL_RESPONSE_INVALID", scheduled_at: "2030-01-02T12:00:00Z", details: { model: null, model_receipt: { model_id: "Bonsai-2-27B-PTQ1_0", actual_model_id: "Qwen3.5-9B.gguf", verified_manifest_model_id: "Qwen3.5-9B.gguf", model_identity_source: "completion_response" }, ai_analysis: null } }],
      };
      if (path.startsWith("/ai-analysis")) return {
        account: { initial_capital_usdt: null, current_equity_usdt: null, net_pnl_usdt: null, total_roi_pct: null, margin_used_usdt: null, margin_available_usdt: null, win_rate_pct: 0, total_trades: 0, winning_trades: 0, losing_trades: 0, profit_factor: 0, max_drawdown_pct: null, avg_leverage: 0, leverage_range: "UNREPORTED" },
        style_dna: { risk_temperament: "UNKNOWN", style_label: "UNKNOWN", discipline_score: null, avg_leverage: null, style_report: "UNKNOWN", best_strategy: "UNKNOWN", leverage_distribution: { conservative_5_25x: null, moderate_25_50x: null, aggressive_50_100x: null } },
        strategy_matrix: [], trades: [],
      };
      return { watchlist: [{ symbol: "BTCUSDT" }], subscriptions: [], runtime: { state: "stopped" }, decisions: [], positions: [], allow_unknown_macro: false };
    });
    show("analysis");
    const panel = await screen.findByRole("region", { name: "Bonsai trading analysis and decision records" });
    expect(within(panel).getByText(/Target model: Bonsai-2-27B-PTQ1_0 · Model status: READY/)).toBeInTheDocument();
    expect(within(panel).getByText(/SYSTEM_BLOCKED · BLOCKED/)).toBeInTheDocument();
    expect(within(panel).getByText(/Latest decision · historical ledger/)).toBeInTheDocument();
    expect(within(panel).getByText(/【Decision model not reported Market Thesis】/)).toBeInTheDocument();
    expect(within(panel).queryByText(/Bonsai-2-27B-PTQ1_0 Market Thesis/)).not.toBeInTheDocument();
  });
  it("does not treat a configured Bonsai name as verified runtime identity", async () => {
    vi.mocked(apiClient.v2).mockImplementation(async (path: string) =>
      path.startsWith("/workspace")
        ? {
            watchlist: [{ symbol: "BTCUSDT" }], subscriptions: [], runtime: { state: "stopped" }, decisions: [], positions: [], allow_unknown_macro: false,
            risk_cockpit: { model: { status: "READY", model_id: "Bonsai-2-27B-PTQ1_0", available: true, model_available: true, checked_at: new Date().toISOString() } },
          }
        : path === "/accounts"
          ? { accounts: [] }
          : { watchlist: [{ symbol: "BTCUSDT" }], subscriptions: [], runtime: { state: "stopped" }, decisions: [], positions: [], allow_unknown_macro: false },
    );
    show("analysis");
    const panel = await screen.findByRole("region", { name: "Bonsai trading analysis and decision records" });
    expect(within(panel).getByText(/Model status: IDENTITY_UNVERIFIED/)).toBeInTheDocument();
    expect(within(panel).getByText("Identity unverified")).toBeInTheDocument();
  });
  it("keeps an incomplete account cockpit read-only and leaves missing facts unverified", async () => {
    vi.mocked(apiClient.v2).mockImplementation(async (path: string) =>
      path.startsWith("/workspace")
        ? {
            watchlist: [{ symbol: "BTCUSDT" }], subscriptions: [], runtime: { state: "stopped" }, decisions: [], positions: [], allow_unknown_macro: false,
            risk_cockpit: { status: "AVAILABLE" },
          }
        : path === "/accounts"
          ? { accounts: [] }
          : { watchlist: [{ symbol: "BTCUSDT" }], subscriptions: [], runtime: { state: "stopped" }, decisions: [], positions: [], allow_unknown_macro: false },
    );
    show("gate-live");
    const cockpit = await screen.findByRole("region", { name: "Account risk cockpit" });
    expect(cockpit).toHaveTextContent("NOT SELECTED · NOT REPORTED");
    expect(cockpit).toHaveTextContent("UNVERIFIED");
    expect(cockpit).toHaveTextContent("READ-ONLY SNAPSHOT");
    expect(cockpit).toHaveTextContent("No verified protection data");
    expect(cockpit).toHaveTextContent("Risk guidance not reported.");
    expect(cockpit).not.toHaveTextContent(/ACTIVE LINK|SYNCED|50,173\.00|2,508\.65|15,051\.88|All positions guarded|2% risk cap/);
  });
  it("hides local Gate TestNet ledger amounts when remote account truth is missing", async () => {
    vi.mocked(apiClient.v2).mockImplementation(async (path: string) =>
      path.startsWith("/workspace")
        ? {
            watchlist: [{ symbol: "BTCUSDT" }], subscriptions: [], runtime: { state: "stopped" }, decisions: [], positions: [], allow_unknown_macro: false,
            risk_cockpit: {
              status: "AVAILABLE", account_id: "gate_testnet", venue: "gate", mode: "TESTNET",
              account_truth_authority: "REMOTE_GATE_TESTNET_PRIVATE_API",
              account_truth: { status: "UNAVAILABLE", equity: null, available_margin: null, used_margin: null },
              ledger_snapshot: { net_equity: "50173.00", cash: "2508.65", reserved_risk: "15051.88", role: "REMOTE_FACTS_AUDIT_CACHE_ONLY" },
              capacity: { single_trade_risk_available: "12.34", portfolio_risk_available: "56.78", status: "UNKNOWN" },
              reconciliation: { status: "SYNCED", last_reconciled_at: "2030-01-02T12:00:00Z" },
            },
          }
        : path === "/accounts"
          ? { accounts: [{ account_id: "gate_testnet", mode: "TESTNET", venue: "gate" }] }
          : { watchlist: [{ symbol: "BTCUSDT" }], subscriptions: [], runtime: { state: "stopped" }, decisions: [], positions: [], allow_unknown_macro: false },
    );
    show("gate-live");
    const cockpit = await screen.findByRole("region", { name: "Account risk cockpit" });
    expect(cockpit).toHaveTextContent("Remote account unverified · local ledger is audit-only");
    expect(cockpit).toHaveTextContent("--");
    expect(cockpit).not.toHaveTextContent(/50,173\.00|2,508\.65|15,051\.88|12\.34 USDT|56\.78 USDT|SYNCED|2030-01-02T12:00:00Z/);
  });
  it("discloses backend failures without simulated values",async()=>{
    vi.mocked(apiClient.v2).mockRejectedValue(new Error("offline"));
    vi.mocked(apiClient.chartBars).mockRejectedValue(new Error("offline"));show();
    expect(await screen.findByRole("alert")).toHaveTextContent("Backend unavailable");
    expect(screen.getByText(/No stored bars yet/)).toBeInTheDocument();
  });
  it("keeps a long macro calendar in a keyboard-focusable scroll region",async()=>{
    vi.mocked(apiClient.v2).mockImplementation(async (path: string) =>
      path.startsWith("/workspace")
        ? {
            watchlist:[{symbol:"BTCUSDT"}],
            subscriptions:[],
            runtime:{state:"stopped"},
            decisions:[],
            positions:[],
            allow_unknown_macro:false,
            macro_calendar:{status:"SCHEDULE_ONLY",provider:"Forex Factory"},
            macro_events:[{
              event_id:"macro-ui-1",
              title:"CPI y/y",
              currency:"USD",
              event_time:"2030-01-02T14:00:00Z",
              previous:"2%",
              forecast:"3%",
              actual:null,
              source_url:"https://www.forexfactory.com/calendar",
              directive:"NONE",
              directive_expires_at:"2030-01-02T14:00:00Z",
              schedule_only:true,
            }],
          }
        : {watchlist:[{symbol:"BTCUSDT"}],subscriptions:[],runtime:{state:"stopped"},decisions:[],positions:[],allow_unknown_macro:false},
    );
    show();
    const region=await screen.findByRole("region",{name:"Macro calendar events (scrollable)"});
    expect(region).toHaveAttribute("tabindex","0");
    expect(region).toHaveTextContent("CPI y/y");
  });
  it("enables a strategy only through explicit user action",async()=>{
    vi.mocked(apiClient.v2).mockImplementation(async (path: string) =>
      path === "/accounts"
        ? { accounts: [{ account_id: "paper_test", mode: "PAPER", venue: "simulated" }] }
        : {watchlist:[{symbol:"BTCUSDT"}],subscriptions:[],runtime:{state:"stopped"},decisions:[],positions:[],allow_unknown_macro:false},
    );
    show("monitor");
    const startBtn=await screen.findByRole("button", { name: /Start/i });
    expect(startBtn).not.toBeDisabled();
    fireEvent.click(startBtn);
    await waitFor(()=>expect(apiClient.v2).toHaveBeenCalledWith("/subscriptions/BTCUSDT/ema_trend?account_id=paper_test","PUT",{enabled:true,params:{}}));
  });
  it("renders authoritative strategy fills once and exposes scoped pending filters",async()=>{
    const analysis = {
      account:{initial_capital_usdt:10000,current_equity_usdt:10000,net_pnl_usdt:0,total_roi_pct:0,margin_used_usdt:0,margin_available_usdt:10000,win_rate_pct:0,total_trades:0,winning_trades:0,losing_trades:0,profit_factor:1,max_drawdown_pct:0,avg_leverage:1,leverage_range:"5x ~ 100x"},
      style_dna:{risk_temperament:"UNKNOWN",style_label:"UNKNOWN",discipline_score:0,avg_leverage:1,style_report:"UNKNOWN",best_strategy:"UNKNOWN",leverage_distribution:{conservative_5_25x:0,moderate_25_50x:0,aggressive_50_100x:0}},
      strategy_matrix:[],
      trades:[],
      execution_records:[{record_id:"fill-ui-1",record_type:"FILL",account_id:"paper_ui",venue:"simulated",mode:"PAPER",fill_id:"fill-ui-1",intent_id:"intent-ui-1",order_id:"order-ui-1",trade_id:"trade-ui-1",position_id:"position-ui-1",symbol:"BTCUSDT",side:"BUY",economic_role:"ENTRY",status:"FILLED",quantity:1,price:100,fee:0,fee_currency:"USDT",strategy_id:"ema_trend",strategy_version:"ema-ui",decision_path:"STRATEGY_DRIVEN",attribution:"STRATEGY_DRIVEN",cycle_id:null,trade_plan_id:"plan-ui-1",position_status:"OPEN",protection_status:"ACTIVE",scope_status:"SCOPED",event_at:"2030-01-02T12:00:00Z",created_at:"2030-01-02T12:00:00Z",concrete_economic_fill:true}],
      execution_positions:[{position_id:"position-ui-1",account_id:"paper_ui",venue:"simulated",mode:"PAPER",symbol:"BTCUSDT",side:"LONG",status:"OPEN",protection_status:"ACTIVE",remaining_contracts:1,quantity:1,entry_price:100,stop_loss:90,realized_pnl:0,strategy_id:"ema_trend",strategy_version:"ema-ui",decision_path:"STRATEGY_DRIVEN",attribution:"STRATEGY_DRIVEN",cycle_id:null,trade_plan_id:"plan-ui-1",scope_status:"SCOPED",updated_at:"2030-01-02T12:00:00Z"}],
      execution_orders:[{intent_id:"intent-pending-ui",order_id:"order-pending-ui",account_id:"paper_ui",venue:"simulated",mode:"PAPER",symbol:"BTCUSDT",side:"BUY",order_type:"limit",quantity:1,price:99,reduce_only:false,status:"ACKNOWLEDGED",economic_status:"NOT_FILLED_PENDING",fill_count:0,filled_quantity:0,position_id:null,strategy_id:"ema_trend",strategy_version:"ema-ui",decision_path:"STRATEGY_DRIVEN",cycle_id:null,trade_plan_id:null,scope_status:"SCOPED",created_at:"2030-01-02T12:00:00Z",updated_at:"2030-01-02T12:00:00Z"}],
      execution_summary:{fill_count:1,entry_fill_count:1,exit_fill_count:0,order_count:2,open_position_count:1,partial_position_count:0,closed_position_count:0,legacy_unconfirmed_count:0},
      execution_scope:{account_id:"paper_ui",venue:"simulated",mode:"PAPER",status:"SCOPED",read_at:"2030-01-02T12:01:00Z"},
      execution_pagination:{page:1,page_size:50,total_records:1,returned_records:1,has_more:false},
      equity_curve:[],
    };
    vi.mocked(apiClient.v2).mockImplementation(async (path: string) => {
      if (path.startsWith("/accounts")) return {accounts:[{account_id:"paper_ui",mode:"PAPER",venue:"simulated"}]};
      if (path.startsWith("/ai-analysis/dashboard")) return {
        status:"AVAILABLE",
        scope:{account_id:"paper_ui"},
        timeline:[{
          cycle_id:"cycle-ui-1",
          action:"OPEN_LONG",
          status:"MODEL_DECISION",
          reason:"Recorded model decision for the test fixture.",
          scheduled_at:"2030-01-02T12:00:00Z",
          details:{model_receipt:{model_id:"Bonsai-2-27B-PTQ1_0",actual_model_id:"Ternary-Bonsai-2-27B-PTQ1_0.gguf",verified_manifest_model_id:"Ternary-Bonsai-2-27B-PTQ1_0.gguf",model_identity_source:"completion_response"},action:"OPEN_LONG",status:"MODEL_DECISION"},
        }],
      };
      if (path.startsWith("/ai-analysis")) return analysis;
      if (path.startsWith("/workspace")) return {watchlist:[{symbol:"BTCUSDT"}],subscriptions:[],runtime:{state:"stopped"},decisions:[],positions:[],allow_unknown_macro:false};
      return {watchlist:[{symbol:"BTCUSDT"}],subscriptions:[],runtime:{state:"stopped"},decisions:[],positions:[],allow_unknown_macro:false};
    });
    show("analysis");
    expect(await screen.findByText(/OPEN_LONG · MODEL_DECISION/)).toBeInTheDocument();
    expect(screen.getByText(/Target model: Bonsai-2-27B-PTQ1_0/)).toBeInTheDocument();
    expect(screen.getByText(/【Bonsai-2-27B-PTQ1_0 Market Thesis】/)).toBeInTheDocument();
    fireEvent.click(await screen.findByText(/Open compatibility tables and ledger details/));
    expect(await screen.findByText("fill-ui-1")).toBeInTheDocument();
    expect(screen.getByText("Position lifecycle")).toBeInTheDocument();
    expect(screen.getAllByText("STRATEGY_DRIVEN")).toHaveLength(2);
    expect(screen.getByText("intent-pending-ui")).toBeInTheDocument();
    expect(screen.getByText("NOT_FILLED_PENDING")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Analysis start time"), {target:{value:"2030-01-02T11:00"}});
    await waitFor(()=>expect(apiClient.v2).toHaveBeenCalledWith(expect.stringContaining("from_at=2030-01-02T03%3A00%3A00.000Z"),"GET",undefined,expect.any(AbortSignal)));
  });
  it("shows separate Gate TestNet and Live account profiles without remote access",async()=>{
      vi.mocked(apiClient.v2).mockImplementation(async (path: string) => {
      if (path === "/gate/accounts") {
        return {accounts:[
          {account_id:"gate_testnet",mode:"TESTNET",venue:"gate",account_kind:"TESTNET",api_environment:"TESTNET",api_base_url:"https://api-testnet.gateapi.io/api/v4",execution_adapter:"GATE_TESTNET_API",live_status:"AVAILABLE",private_api_access:"NOT_ATTEMPTED",credentials:{configured:false,api_key_masked:"",testnet:true,updated_at:null}},
          {account_id:"gate_live",mode:"LIVE",venue:"gate",account_kind:"LIVE",api_environment:"LIVE",api_base_url:"https://api.gateio.ws/api/v4",execution_adapter:"GATE_LIVE_API",live_status:"LOCKED",private_api_access:"NOT_ATTEMPTED",credentials:{configured:false,api_key_masked:"",testnet:false,updated_at:null}},
        ]};
      }
      if (path === "/accounts") return {accounts:[{account_id:"gate_testnet",mode:"TESTNET",venue:"gate"},{account_id:"gate_live",mode:"LIVE",venue:"gate"}]};
      if (path.startsWith("/gate/config")) return {configured:false,api_key_masked:"",live_enabled:false,testnet:false,updated_at:null};
      if (path.startsWith("/gate/account")) return {configured:false,account_id:"gate_testnet",mode:"TESTNET",data_status:"NOT_CONFIGURED_NO_TESTNET_CREDENTIALS",balance:{total:null,free:null,used:null},positions:[]};
      if (path.startsWith("/gate/trades")) return {configured:false,is_sample:false,trades:[],summary:{total_trades:0,total_fee_cost:null,source:"NOT_CONFIGURED_NO_TESTNET_CREDENTIALS"}};
      if (path.startsWith("/gate/markets")) return {markets:[]};
      if (path.startsWith("/workspace")) return {watchlist:[{symbol:"BTCUSDT"}],subscriptions:[],runtime:{state:"stopped"},decisions:[],positions:[],allow_unknown_macro:false};
      return {watchlist:[{symbol:"BTCUSDT"}],subscriptions:[],runtime:{state:"stopped"},decisions:[],positions:[],allow_unknown_macro:false};
    });
    show("gate-live");
    expect(await screen.findByTestId("gate-account-profiles")).toBeInTheDocument();
    expect(screen.getAllByText("gate_testnet").length).toBeGreaterThan(0);
    expect(screen.getAllByText("gate_live").length).toBeGreaterThan(0);
    expect(screen.getByText(/Register default pair/)).toBeInTheDocument();
    expect(screen.getByText(/private APIs are accessed only by an explicit/i)).toBeInTheDocument();
    expect(screen.getByTestId("gate-testnet-e2e")).toHaveTextContent("Gate TestNet 独立链路验收");
    expect(screen.getByRole("button", { name: /执行 TestNet 独立验收/ })).not.toBeDisabled();
  });
  it("verifies and saves credentials in the selected Gate account slot",async()=>{
    const verifyCalls: Array<{path:string; method:string; body:unknown}> = [];
    vi.mocked(apiClient.v2).mockImplementation(async (path: string, method?: string, body?: unknown) => {
      if (path === "/gate/accounts") {
        return {accounts:[
          {account_id:"gate_testnet",mode:"TESTNET",venue:"gate",account_kind:"TESTNET",api_environment:"TESTNET",api_base_url:"https://api-testnet.gateapi.io/api/v4",execution_adapter:"GATE_TESTNET_API",live_status:"AVAILABLE",private_api_access:"NOT_ATTEMPTED",credentials:{configured:false,api_key_masked:"",testnet:true,updated_at:null}},
          {account_id:"gate_live",mode:"LIVE",venue:"gate",account_kind:"LIVE",api_environment:"LIVE",api_base_url:"https://api.gateio.ws/api/v4",execution_adapter:"GATE_LIVE_API",live_status:"LOCKED",private_api_access:"NOT_ATTEMPTED",credentials:{configured:false,api_key_masked:"",testnet:false,updated_at:null}},
        ]};
      }
      if (path === "/accounts") return {accounts:[{account_id:"gate_testnet",mode:"TESTNET",venue:"gate"},{account_id:"gate_live",mode:"LIVE",venue:"gate"}]};
      if (path.endsWith("/credentials/verify")) {
        verifyCalls.push({path,method:method || "",body});
        return {
          saved:true,
          private_api_access:"EXPLICITLY_REQUESTED",
          account:{account_id:"gate_testnet",mode:"TESTNET",venue:"gate",account_kind:"GATE_TESTNET",api_environment:"TESTNET",api_base_url:"https://api-testnet.gateapi.io/api/v4",execution_adapter:"GATE_TESTNET_API",live_status:"AVAILABLE",private_api_access:"EXPLICITLY_REQUESTED",credentials:{configured:true,api_key_masked:"test***1234",testnet:true,updated_at:"2030-01-02T00:00:00Z"}},
          validation:{valid:true,status:"VERIFIED_READ_ONLY",private_api_access:"EXPLICITLY_REQUESTED",account_id:"gate_testnet",api_environment:"TESTNET",account_type:"Gate.io Futures / Swap",data_status:"AVAILABLE",total_usdt:123.45},
        };
      }
      if (path.startsWith("/gate/config")) return {configured:false,api_key_masked:"",live_enabled:false,testnet:true,updated_at:null};
      if (path.startsWith("/gate/account")) return {configured:true,account_id:"gate_testnet",mode:"TESTNET",data_status:"AVAILABLE",equity:123.45,available_margin:123.45,used_margin:0,balance:{total:123.45,free:123.45,used:0},positions:[],pending_orders:[],source:"GATE_TESTNET_PRIVATE_API"};
      if (path.startsWith("/gate/trades")) return {configured:true,is_sample:false,trades:[],summary:{total_trades:0,total_fee_cost:null,source:"Gate.io v4 TESTNET Private API"}};
      if (path.startsWith("/gate/markets")) return {markets:[]};
      if (path.startsWith("/workspace")) return {watchlist:[{symbol:"BTCUSDT"}],subscriptions:[],runtime:{state:"stopped"},decisions:[],positions:[],allow_unknown_macro:false};
      return {watchlist:[{symbol:"BTCUSDT"}],subscriptions:[],runtime:{state:"stopped"},decisions:[],positions:[],allow_unknown_macro:false};
    });
    show("gate-live");
    const key = await screen.findByLabelText(/API Key/);
    const secret = screen.getByLabelText(/API Secret/);
    fireEvent.change(key,{target:{value:"paper-verify-key"}});
    fireEvent.change(secret,{target:{value:"paper-verify-secret"}});
    fireEvent.click(await screen.findByRole("button",{name:/Verify & save/}));
    expect(await screen.findByTestId("gate-verification-result")).toHaveTextContent("Read-only API verification passed");
    await waitFor(()=>expect(verifyCalls).toEqual([{
      path:"/gate/accounts/gate_testnet/credentials/verify",
      method:"POST",
      body:{api_key:"paper-verify-key",api_secret:"paper-verify-secret"},
      }]));
  });
  it("provisions a missing Gate profile before saving credentials through the scoped endpoint",async()=>{
    const calls: Array<{path:string;method:string;body:unknown}> = [];
    const testnetProfile = {
      account_id:"gate_testnet",mode:"TESTNET",venue:"gate",account_kind:"TESTNET",api_environment:"TESTNET",
      api_base_url:"https://api-testnet.gateapi.io/api/v4",execution_adapter:"GATE_TESTNET_API",live_status:"AVAILABLE",
      private_api_access:"NOT_ATTEMPTED",credentials:{configured:false,api_key_masked:"",testnet:true,updated_at:null},
    };
    vi.mocked(apiClient.v2).mockImplementation(async (path: string, method?: string, body?: unknown) => {
      calls.push({path,method:method || "GET",body});
      if (path === "/gate/accounts") return {accounts:[]};
      if (path === "/accounts") return {accounts:[{account_id:"gate_testnet",mode:"TESTNET",venue:"gate"}]};
      if (path === "/gate/accounts/provision-defaults") return {accounts:[testnetProfile]};
      if (path.endsWith("/credentials/verify")) return {
        saved:true,
        private_api_access:"EXPLICITLY_REQUESTED",
        account:{...testnetProfile,credentials:{configured:true,api_key_masked:"test***1234",testnet:true,updated_at:"2030-01-02T00:00:00Z"}},
        validation:{valid:true,status:"VERIFIED_READ_ONLY",private_api_access:"EXPLICITLY_REQUESTED",account_id:"gate_testnet",api_environment:"TESTNET"},
      };
      if (path.startsWith("/gate/account/refresh")) return {account_id:"gate_testnet",data_status:"AVAILABLE"};
      if (path.startsWith("/gate/config")) return {configured:false,api_key_masked:"",live_enabled:false,testnet:true,updated_at:null};
      if (path.startsWith("/gate/account")) return {configured:false,account_id:"gate_testnet",mode:"TESTNET",data_status:"NOT_CONFIGURED_NO_TESTNET_CREDENTIALS",balance:{total:null,free:null,used:null},positions:[]};
      if (path.startsWith("/gate/trades")) return {configured:false,is_sample:false,trades:[],summary:{total_trades:0,total_fee_cost:null,source:"NOT_CONFIGURED_NO_TESTNET_CREDENTIALS"}};
      if (path.startsWith("/gate/markets")) return {markets:[]};
      if (path.startsWith("/workspace")) return {watchlist:[{symbol:"BTCUSDT"}],subscriptions:[],runtime:{state:"stopped"},decisions:[],positions:[],allow_unknown_macro:false};
      return {watchlist:[{symbol:"BTCUSDT"}],subscriptions:[],runtime:{state:"stopped"},decisions:[],positions:[],allow_unknown_macro:false};
    });
    show("gate-live");
    const keyInput = await screen.findByLabelText(/API Key/);
    fireEvent.change(keyInput,{target:{value:"migrated-account-key"}});
    fireEvent.change(screen.getByLabelText(/API Secret/),{target:{value:"migrated-account-secret"}});
    fireEvent.submit(keyInput.closest("form")!);
    expect(await screen.findByTestId("gate-verification-result")).toHaveTextContent("Read-only API verification passed");
    expect(calls).toContainEqual({path:"/gate/accounts/provision-defaults",method:"POST",body:{}});
    expect(calls).toContainEqual({
      path:"/gate/accounts/gate_testnet/credentials/verify",
      method:"POST",
      body:{api_key:"migrated-account-key",api_secret:"migrated-account-secret"},
    });
    expect(calls).not.toContainEqual(expect.objectContaining({path:"/gate/config",method:"POST"}));
  });
  it("runs a scoped read-only Gate connection test without saving or sending",async()=>{
    const calls: Array<{path:string;method:string;body:unknown}> = [];
    vi.mocked(apiClient.v2).mockImplementation(async (path: string, method?: string, body?: unknown) => {
      if (path === "/gate/accounts") {
        return {accounts:[
          {account_id:"gate_testnet",mode:"TESTNET",venue:"gate",account_kind:"TESTNET",api_environment:"TESTNET",api_base_url:"https://api-testnet.gateapi.io/api/v4",execution_adapter:"GATE_TESTNET_API",live_status:"AVAILABLE",private_api_access:"NOT_ATTEMPTED",credentials:{configured:false,api_key_masked:"",testnet:true,updated_at:null}},
        ]};
      }
      if (path === "/accounts") return {accounts:[{account_id:"gate_testnet",mode:"TESTNET",venue:"gate"}]};
      if (path.endsWith("/connection-test")) {
        calls.push({path,method:method || "",body});
        return {valid:true,status:"VERIFIED_READ_ONLY",code:"GATE_CONNECTION_VERIFIED",message_zh:"只读连接、账户、持仓和挂单查询成功。",account_id:"gate_testnet",api_environment:"TESTNET",read_only:true,orders_sent:0,model_called:false,authorization_created:false};
      }
      if (path.startsWith("/gate/config")) return {configured:false,api_key_masked:"",live_enabled:false,testnet:true,updated_at:null};
      if (path.startsWith("/gate/account")) return {configured:false,account_id:"gate_testnet",mode:"TESTNET",data_status:"NOT_CONFIGURED_NO_TESTNET_CREDENTIALS",balance:{total:null,free:null,used:null},positions:[]};
      if (path.startsWith("/gate/trades")) return {configured:false,is_sample:false,trades:[],summary:{total_trades:0,total_fee_cost:null,source:"NOT_CONFIGURED_NO_TESTNET_CREDENTIALS"}};
      if (path.startsWith("/gate/markets")) return {markets:[]};
      if (path.startsWith("/workspace")) return {watchlist:[{symbol:"BTCUSDT"}],subscriptions:[],runtime:{state:"stopped"},decisions:[],positions:[],allow_unknown_macro:false};
      return {watchlist:[{symbol:"BTCUSDT"}],subscriptions:[],runtime:{state:"stopped"},decisions:[],positions:[],allow_unknown_macro:false};
    });
    show("gate-live");
    fireEvent.change(await screen.findByLabelText(/API Key/),{target:{value:"readonly-key"}});
    fireEvent.change(screen.getByLabelText(/API Secret/),{target:{value:"readonly-secret"}});
    fireEvent.click(await screen.findByTestId("gate-connection-test"));
    expect(await screen.findByTestId("gate-connection-test-result")).toHaveTextContent("orders_sent=0");
    await waitFor(()=>expect(calls).toEqual([{
      path:"/gate/accounts/gate_testnet/connection-test",
      method:"POST",
      body:{api_key:"readonly-key",api_secret:"readonly-secret"},
    }]));
  });
});
