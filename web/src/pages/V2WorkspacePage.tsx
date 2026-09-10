import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { apiClient } from "../api/client";
import type {
  MarketBar,
  MarketIntelligenceResponse,
  MonitoringRuntimeStatus,
} from "../api/types";
import { OhlcvChart } from "../components/OhlcvChart";
import { V2News } from "../components/V2News";
import { AITraderPanel } from "../components/AITraderPanel";
import { InstitutionalEvidencePanel } from "../components/InstitutionalEvidencePanel";
import { InstitutionalAnalysisDashboard, type InstitutionalDashboard } from "../components/InstitutionalAnalysisDashboard";
import {
  type StrategyId,
  STRATEGY_CATALOG,
  getStrategyOptionLabel,
  useV2Copy,
} from "../v2copy";
import { useI18n, LocalizedSurface } from "../i18n";
import { readTradingAccount, rememberTradingAccount } from "../tradingAccountSelection";

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

interface DecisionProposal {
  symbol?: string;
  side?: string;
  mode?: string;
  strategy_id?: string;
  entry?: number;
  stop?: number;
  targets?: number[];
  generated_at?: string;
  rationale?: string;
}

interface DecisionRecord extends Ledger {
  proposal?: DecisionProposal;
  facts?: { price?: number };
  verdict?: string;
}

interface PositionRecord extends Ledger {
  side?: string;
  entry?: number;
  stop?: number;
  targets?: number[];
  remaining_contracts?: number;
  filled_contracts?: number;
  realized_pnl?: number | null;
  protected?: boolean;
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
  schedule_only?: boolean;
  currency?: string;
}

interface Workspace {
  account_id?: string | null;
  scope_required?: boolean;
  macro_events?: MacroEvent[];
  macro_calendar?: { status: string; provider?: string; last_success_at?: string; error?: string; actual_supported?: boolean };
  watchlist: { symbol: string }[];
  subscriptions: Subscription[];
  runtime: MonitoringRuntimeStatus;
  decisions: DecisionRecord[];
  positions: PositionRecord[];
  positions_source?: string;
  positions_data_status?: string;
  positions_remote_truth?: boolean;
  allow_unknown_macro: boolean;
  risk_cockpit?: RiskCockpit;
}

interface RiskCockpit {
  status?: string;
  account_id?: string | null;
  mode?: string;
  venue?: string;
  as_of?: string;
  ledger_snapshot?: {
    net_equity?: string;
    cash?: string;
    reserved_risk?: string;
    daily_loss?: string;
    daily_loss_limit_reached?: boolean;
    unverified_protection_count?: number;
    unvalued_fee_events?: number;
  };
  risk?: {
    new_risk_blocked?: boolean;
    new_risk_block_reasons?: string[];
  };
  capacity?: {
    single_trade_risk_available?: string;
    portfolio_risk_available?: string;
    status?: string;
  };
  protections?: Array<{
    position_id?: string;
    symbol?: string;
    side?: string;
    quantity?: string;
    status?: string;
    stop_price?: number | null;
  }>;
  reconciliation?: {
    status?: string;
    last_reconciled_at?: string | null;
    source?: string;
  };
  model?: { status?: string; reason_code?: string; required_model?: string } | string;
  runtime?: { state?: string; execution_blocked?: boolean };
  market_data?: { status?: string };
  emergency_guidance?: string[];
  legacy_data?: { account_scoped_unverified?: number; unassigned_unverified?: number };
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

interface AiAnalysisResponse {
  account: {
    initial_capital_usdt: number;
    current_equity_usdt: number;
    net_pnl_usdt: number;
    total_roi_pct: number;
    margin_used_usdt: number;
    margin_available_usdt: number;
    win_rate_pct: number;
    total_trades: number;
    winning_trades: number;
    losing_trades: number;
    profit_factor: number;
    max_drawdown_pct: number;
    avg_leverage: number;
    leverage_range: string;
  };
  style_dna: {
    risk_temperament: string;
    style_label: string;
    discipline_score: number | null;
    avg_leverage: number | null;
    style_report: string;
    best_strategy: string;
    leverage_distribution: {
      conservative_5_25x: number | null;
      moderate_25_50x: number | null;
      aggressive_50_100x: number | null;
    };
  };
  strategy_matrix: {
    strategy_id: string;
    name: string;
    style: string;
    trades_count: number;
    wins_count: number;
    losses_count: number;
    win_rate_pct: number;
    net_pnl_usdt: number;
    avg_leverage: number;
    avg_risk_reward: number;
  }[];
  trades: {
    trade_id: string;
    symbol: string;
    strategy_id: string;
    strategy_name: string;
    strategy_style: string;
    side: string;
    leverage: number;
    leverage_reason: string;
    entry_price: number;
    exit_price: number | null;
    stop_loss: number;
    targets: number[];
    pnl_usdt: number;
    roi_pct: number;
    status: string;
    exit_reason?: string;
    created_at?: string;
    closed_at?: string;
    rationale?: string;
  }[];
  execution_records?: {
    record_id: string;
    record_type: string;
    account_id: string | null;
    venue: string;
    mode: string;
    fill_id: string;
    intent_id: string | null;
    order_id: string | null;
    trade_id: string | null;
    position_id: string | null;
    symbol: string;
    side: string;
    economic_role: string;
    status: string;
    order_status?: string | null;
    quantity: number | null;
    price: number | null;
    fee?: number | null;
    realized_pnl_usdt?: number;
    fee_currency?: string | null;
    strategy_id: string;
    strategy_version: string;
    decision_path: string;
    attribution: string;
    cycle_id: string | null;
    trade_plan_id: string | null;
    position_status: string;
    protection_status: string;
    scope_status: string;
    event_at: string | null;
    created_at: string | null;
    concrete_economic_fill: boolean;
  }[];
  execution_orders?: {
    intent_id: string;
    order_id: string | null;
    account_id?: string | null;
    venue?: string;
    mode?: string;
    symbol: string;
    side?: string;
    order_type?: string;
    quantity?: number | null;
    price?: number | null;
    reduce_only?: boolean;
    status: string;
    economic_status: string;
    fill_count: number;
    filled_quantity?: number;
    position_id?: string | null;
    strategy_id?: string;
    strategy_version?: string;
    decision_path?: string;
    cycle_id?: string | null;
    trade_plan_id?: string | null;
    scope_status?: string;
    created_at?: string | null;
    updated_at?: string | null;
  }[];
  execution_positions?: {
    position_id: string;
    account_id?: string | null;
    venue?: string;
    mode?: string;
    symbol?: string | null;
    side?: string;
    status?: string;
    protection_status?: string;
    remaining_contracts?: number | null;
    quantity?: number | null;
    entry_price?: number | null;
    stop_loss?: number | null;
    realized_pnl?: number | null;
    strategy_id?: string;
    strategy_version?: string;
    decision_path?: string;
    attribution?: string;
    cycle_id?: string | null;
    trade_plan_id?: string | null;
    scope_status?: string;
    updated_at?: string | null;
    legacy_unverified?: boolean;
  }[];
  execution_summary?: {
    fill_count: number;
    entry_fill_count: number;
    exit_fill_count: number;
    order_count: number;
    open_position_count: number;
    partial_position_count: number;
    closed_position_count: number;
    legacy_unconfirmed_count: number;
    realized_pnl_usdt?: number;
    pnl_window?: {
      from_at: string | null;
      to_at: string | null;
      basis: string;
    };
  };
  execution_scope?: {
    account_id: string | null;
    venue: string | null;
    mode: string | null;
    status: string;
    read_at: string;
  };
  execution_pagination?: {
    page: number;
    page_size: number;
    total_records: number;
    returned_records: number;
    has_more: boolean;
  };
  performance_by_attribution?: Record<string, {
    fill_count: number;
    entry_fill_count: number;
    exit_fill_count: number;
    position_count: number;
    closed_position_count: number;
    realized_pnl_usdt: number;
  }>;
  ai_led_performance?: {
    position_count: number;
    closed_position_count: number;
    realized_pnl_usdt: number;
  };
  equity_curve: {
    time: string;
    equity: number;
    pnl: number;
  }[];
}

interface GateConfigResponse {
  configured: boolean;
  api_key_masked: string;
  live_enabled: boolean;
  testnet: boolean;
  updated_at: string | null;
  api_environment?: string;
  api_base_url?: string;
}

interface GateCredentialValidation {
  valid: boolean;
  status: string;
  private_api_access: string;
  account_id?: string;
  api_environment?: string;
  account_type?: string;
  data_status?: string;
  total_usdt?: number | null;
  free_usdt?: number | null;
  used_usdt?: number | null;
  reason?: string;
  validated_at?: string;
}

interface GateCredentialVerifyResponse {
  saved: boolean;
  account: GateAccountProfileResponse;
  validation: GateCredentialValidation;
  private_api_access: string;
}

interface GateConnectionTestResponse {
  valid: boolean;
  status: string;
  code?: string;
  message_zh?: string;
  account_id?: string;
  api_environment?: string;
  endpoint?: string;
  read_only: boolean;
  orders_sent: number;
  model_called: boolean;
  authorization_created: boolean;
  balance?: { data_status?: string; total?: number | null; free?: number | null; used?: number | null };
  positions?: unknown[];
  pending_orders?: unknown[];
  permissions?: { orders?: string };
}

interface GateAccountProfileResponse {
  account_id: string;
  mode: string;
  venue: string;
  account_kind: string;
  api_environment: string;
  api_base_url: string;
  execution_adapter: string;
  live_status: string;
  private_api_access: string;
  credentials: {
    configured: boolean;
    api_key_masked: string;
    testnet: boolean;
    updated_at: string | null;
  };
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error) return error.message || fallback;
  if (error && typeof error === "object" && "message" in error) {
    return String((error as { message?: unknown }).message || fallback);
  }
  return fallback;
}

interface GateMarketItem {
  id: string;
  symbol: string;
  base: string;
  quote: string;
  active: boolean;
  min_amount: number;
  max_leverage: number;
}

interface GateTradeItem {
  id: string | null;
  order_id: string | null;
  symbol: string;
  datetime: string | null;
  side: string;
  price: number | null;
  amount: number | null;
  cost: number | null;
  fee_cost: number | null;
  fee_currency: string | null;
  fee_evidence_status?: string;
  pnl?: number;
}

interface GateTradesResponse {
  configured: boolean;
  is_sample: boolean;
  notice?: string;
  trades: GateTradeItem[];
  summary: {
    total_trades: number;
    total_fee_cost: number | null;
    fee_status?: string;
    source: string;
  };
}

interface TradingAccountSummary {
  account_id: string;
  mode: string;
  venue: string;
  status?: string;
}

interface GateAccountResponse {
  configured: boolean;
  account_id?: string | null;
  mode?: string;
  capability_status?: string;
  data_status?: string;
  observed_at?: string | null;
  source?: string | null;
  error_code?: string | null;
  equity?: number | null;
  available_margin?: number | null;
  used_margin?: number | null;
  equity_basis?: string | null;
  available_margin_basis?: string | null;
  used_margin_basis?: string | null;
  unrealized_pnl?: number | null;
  realized_pnl?: number | null;
  positions_status?: string;
  balance: {
    total: number | null;
    free: number | null;
    used: number | null;
  };
  positions: {
    symbol: string | null;
    side: string;
    contracts: number | null;
    entryPrice?: number | null;
    markPrice?: number | null;
    unrealizedPnl?: number | null;
    leverage?: number | null;
    position_status?: string;
  }[];
  pending_orders?: unknown[];
  fills?: unknown[];
  remote_truth?: boolean;
  private_api_access?: string;
}

interface GateTestnetE2EResponse {
  status?: string;
  run_id?: string;
  account_id?: string;
  symbol?: string;
  stages?: { stage?: string; status?: string; reason?: string; reason_code?: string; message_zh?: string; evidence?: Record<string, unknown> }[];
  orders_sent?: number;
  local_fill_created?: boolean;
  error_code?: string;
  message_zh?: string;
  amount?: string | number;
  entry_order?: { order_id?: string; status?: string; protection_status?: string };
}

function formatHktTime(val?: string | null): string {
  if (!val) return "—";
  const d = new Date(val);
  if (isNaN(d.getTime())) return typeof val === "string" ? val.slice(11, 19) : "—";
  return new Intl.DateTimeFormat("zh-HK", {
    timeZone: "Asia/Hong_Kong",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(d);
}

function formatHktDateTime(val?: string | null): string {
  if (!val) return "—";
  const d = new Date(val);
  if (isNaN(d.getTime())) return typeof val === "string" ? val : "—";
  return new Intl.DateTimeFormat("zh-HK", {
    timeZone: "Asia/Hong_Kong",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(d);
}

function normalizeGateSymbol(value: unknown): string {
  return String(value ?? "")
    .toUpperCase()
    .replace(/:USDT$/, "")
    .replace(/[^A-Z0-9]/g, "");
}

function isInstitutionalDashboard(value: unknown): value is InstitutionalDashboard {
  if (!value || typeof value !== "object") return false;
  const candidate = value as { status?: unknown; scope?: { account_id?: unknown } };
  return typeof candidate.status === "string" && typeof candidate.scope?.account_id === "string";
}

export function V2WorkspacePage({
  surface = "dashboard",
}: {
  surface?: "dashboard" | "monitor" | "strategies" | "intel" | "ledger" | "analysis" | "gate-live";
}) {
  const copy = useV2Copy();
  const { formatNumber, text, language } = useI18n();
  const zh = language === "zh-CN";

  const [hkTime, setHkTime] = useState(() =>
    new Intl.DateTimeFormat("zh-HK", {
      timeZone: "Asia/Hong_Kong",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(new Date())
  );

  useEffect(() => {
    const timer = setInterval(() => {
      setHkTime(
        new Intl.DateTimeFormat("zh-HK", {
          timeZone: "Asia/Hong_Kong",
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          hour12: false,
        }).format(new Date())
      );
    }, 1000);
    return () => clearInterval(timer);
  }, []);

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
  const [symbolStrategyMap, setSymbolStrategyMap] = useState<Record<string, StrategyId>>({});

  // Analysis state
  const [analysisData, setAnalysisData] = useState<AiAnalysisResponse | null>(null);
  const [institutionalDashboard, setInstitutionalDashboard] = useState<InstitutionalDashboard | null>(null);
  const [institutionalDashboardError, setInstitutionalDashboardError] = useState("");
  const [analysisLoading, setAnalysisLoading] = useState(false);
  const [analysisError, setAnalysisError] = useState("");
  const [analysisFrom, setAnalysisFrom] = useState("");
  const [analysisTo, setAnalysisTo] = useState("");
  const [analysisPage, setAnalysisPage] = useState(1);
  const analysisRequest = useRef(0);

  // Gate.io Live & Trade states
  const [gateConfig, setGateConfig] = useState<GateConfigResponse | null>(null);
  const [gateMarkets, setGateMarkets] = useState<GateMarketItem[]>([]);
  const [gateTrades, setGateTrades] = useState<GateTradesResponse | null>(null);
  const [gateAccount, setGateAccount] = useState<GateAccountResponse | null>(null);
  const [gateProfiles, setGateProfiles] = useState<GateAccountProfileResponse[]>([]);
  const [gateApiKeyInput, setGateApiKeyInput] = useState("");
  const [gateApiSecretInput, setGateApiSecretInput] = useState("");
  const [gateDryRunMode, setGateDryRunMode] = useState(true);
  const [gateNotice, setGateNotice] = useState<{ msg: string; type: "ok" | "err" } | null>(null);
  const [gateVerification, setGateVerification] = useState<GateCredentialValidation | null>(null);
  const [gateConnectionTest, setGateConnectionTest] = useState<GateConnectionTestResponse | null>(null);
  const [gateVerificationBusy, setGateVerificationBusy] = useState(false);
  const [gateRefreshBusy, setGateRefreshBusy] = useState(false);
  const [marketSearch, setMarketSearch] = useState("");
  const [tradingAccounts, setTradingAccounts] = useState<TradingAccountSummary[]>([]);
  const [selectedTradingAccount, setSelectedTradingAccount] = useState(readTradingAccount);
  useEffect(() => { rememberTradingAccount(selectedTradingAccount); }, [selectedTradingAccount]);
  const accountScopedPath = (path: string) =>
    selectedTradingAccount
      ? `${path}?account_id=${encodeURIComponent(selectedTradingAccount)}`
      : path;

  // Live quantitative pre-reserved order form state
  const [orderSymbol, setOrderSymbol] = useState("BTCUSDT");
  const [orderSide, setOrderSide] = useState<"BUY" | "SELL">("BUY");
  const [orderType, setOrderType] = useState<"LIMIT" | "MARKET">("MARKET");
  // Amount and leverage must be explicit user inputs.  A visible blank value
  // is safer than silently widening an invalid/empty request to a preset.
  const [orderAmount, setOrderAmount] = useState("");
  const [orderPrice, setOrderPrice] = useState("");
  const [orderLeverage, setOrderLeverage] = useState("");
  const [orderStopLoss, setOrderStopLoss] = useState("");
  const [orderTakeProfit, setOrderTakeProfit] = useState("");
  const [orderFeedback, setOrderFeedback] = useState<string | null>(null);
  const [e2eStopType, setE2eStopType] = useState<"PRICE" | "PERCENT" | "ATR">("PRICE");
  const [e2eAmount, setE2eAmount] = useState("");
  const [e2eStopValue, setE2eStopValue] = useState("");
  const [e2eTakeProfitType, setE2eTakeProfitType] = useState<"PRICE" | "PERCENT" | "ATR">("PRICE");
  const [e2eTakeProfitValue, setE2eTakeProfitValue] = useState("");
  const [e2eLeverage, setE2eLeverage] = useState("1");
  const [e2eConfirm, setE2eConfirm] = useState(false);
  const [e2eCleanup, setE2eCleanup] = useState(true);
  const [e2eBusy, setE2eBusy] = useState(false);
  const [e2eFeedback, setE2eFeedback] = useState<GateTestnetE2EResponse | null>(null);

  const handleStartSymbol = async (sym: string, stratId: StrategyId) => {
    await action(async () => {
      if (!selectedTradingAccount) {
        throw new Error(zh ? "没有已登记账户，已阻止启动监控。" : "No registered account is selected; monitoring start blocked.");
      }
      const otherSubs = (workspace?.subscriptions || []).filter(
        (s) => s.symbol === sym && s.strategy_id !== stratId && s.enabled,
      );
      for (const os of otherSubs) {
        await apiClient.v2(accountScopedPath(`/subscriptions/${sym}/${os.strategy_id}`), "PUT", {
          enabled: false,
          params: {},
        });
      }
      await apiClient.v2(accountScopedPath(`/subscriptions/${sym}/${stratId}`), "PUT", {
        enabled: true,
        params: {},
      });
      await apiClient.v2(accountScopedPath("/monitoring/sessions/start"), "POST");
      setSymbolStrategyMap((prev) => ({ ...prev, [sym]: stratId }));
    });
  };

  const handlePauseSymbol = async (sym: string, stratId: StrategyId) => {
    await action(async () => {
      await apiClient.v2(accountScopedPath(`/subscriptions/${sym}/${stratId}`), "PUT", {
        enabled: false,
        params: {},
      });
      setSymbolStrategyMap((prev) => ({ ...prev, [sym]: stratId }));
    });
  };

  const handleStopSymbol = async (sym: string, stratId: StrategyId) => {
    await action(async () => {
      const allSubs = (workspace?.subscriptions || []).filter((s) => s.symbol === sym);
      for (const s of allSubs) {
        if (s.enabled) {
          await apiClient.v2(accountScopedPath(`/subscriptions/${sym}/${s.strategy_id}`), "PUT", {
            enabled: false,
            params: {},
          });
        }
      }
      setSymbolStrategyMap((prev) => ({ ...prev, [sym]: stratId }));
    });
  };

  const handleSelectStrategy = async (
    sym: string,
    newStratId: StrategyId,
    isRunning: boolean,
    currentStratId: StrategyId,
  ) => {
    setSymbolStrategyMap((prev) => ({ ...prev, [sym]: newStratId }));
    if (isRunning) {
      await action(async () => {
        await apiClient.v2(accountScopedPath(`/subscriptions/${sym}/${currentStratId}`), "PUT", {
          enabled: false,
          params: {},
        });
        await apiClient.v2(accountScopedPath(`/subscriptions/${sym}/${newStratId}`), "PUT", {
          enabled: true,
          params: {},
        });
      });
    }
  };

  const handleStartAll = async () => {
    await action(async () => {
      if (!selectedTradingAccount) {
        throw new Error(zh ? "没有已登记账户，已阻止启动监控。" : "No registered account is selected; monitoring start blocked.");
      }
      if (workspace?.watchlist.length) {
        for (const w of workspace.watchlist) {
          const currentSub = workspace.subscriptions.find(
            (s) => s.symbol === w.symbol && s.enabled,
          );
          if (!currentSub) {
            const strat = symbolStrategyMap[w.symbol] || "ema_trend";
            await apiClient.v2(accountScopedPath(`/subscriptions/${w.symbol}/${strat}`), "PUT", {
              enabled: true,
              params: {},
            });
          }
        }
      }
      await apiClient.v2(accountScopedPath("/monitoring/sessions/start"), "POST");
    });
  };

  const fetchAnalysisData = useCallback(async (signal?: AbortSignal) => {
    const request = ++analysisRequest.current;
    if (!selectedTradingAccount) {
      if (!signal?.aborted) {
        setAnalysisData(null);
        setAnalysisError("");
        setAnalysisLoading(false);
      }
      return;
    }
    if (!signal?.aborted) {
      setAnalysisLoading(true);
      setAnalysisError("");
    }
    try {
      const params = new URLSearchParams({
        account_id: selectedTradingAccount,
        page: String(analysisPage),
        page_size: "50",
      });
      if (analysisFrom) {
        const point = new Date(analysisFrom);
        if (!Number.isNaN(point.getTime())) params.set("from_at", point.toISOString());
      }
      if (analysisTo) {
        const point = new Date(analysisTo);
        if (!Number.isNaN(point.getTime())) params.set("to_at", point.toISOString());
      }
      const dashboardParams = new URLSearchParams({ account_id: selectedTradingAccount, limit: "200" });
      if (analysisFrom) {
        const point = new Date(analysisFrom);
        if (!Number.isNaN(point.getTime())) dashboardParams.set("from_at", point.toISOString());
      }
      if (analysisTo) {
        const point = new Date(analysisTo);
        if (!Number.isNaN(point.getTime())) dashboardParams.set("to_at", point.toISOString());
      }
      const dashboardPromise = apiClient.v2<InstitutionalDashboard>(
        `/ai-analysis/dashboard?${dashboardParams.toString()}`,
        "GET",
        undefined,
        signal,
      ).catch((dashboardRequestError: unknown) => ({
        __dashboard_error: dashboardRequestError instanceof Error ? dashboardRequestError.message : "分析看板读取失败。",
      } as InstitutionalDashboard & { __dashboard_error: string }));
      const res = await apiClient.v2<AiAnalysisResponse>(
        `/ai-analysis?${params.toString()}`,
        "GET",
        undefined,
        signal,
      );
      const dashboard = await dashboardPromise;
      if (!res || !res.account || !Array.isArray(res.trades)) {
        throw new Error("做单分析返回格式不完整，请更新后端后重试。");
      }
      if (res.execution_scope?.account_id && res.execution_scope.account_id !== selectedTradingAccount) {
        throw new Error("分析结果账户不匹配，已拒绝显示。");
      }
      if (!signal?.aborted && request === analysisRequest.current) {
        setAnalysisData(res);
        const dashboardError = dashboard && "__dashboard_error" in dashboard ? dashboard.__dashboard_error : "";
        setInstitutionalDashboard(isInstitutionalDashboard(dashboard) ? dashboard : null);
        setInstitutionalDashboardError(
          dashboardError || (isInstitutionalDashboard(dashboard) ? "" : "分析看板返回格式不完整，请更新后端后重试。"),
        );
      }
    } catch (e) {
      if (!signal?.aborted && request === analysisRequest.current) {
        const message = e instanceof Error ? e.message : "unavailable";
        setAnalysisError(message);
        setAnalysisData(null);
        setInstitutionalDashboard(null);
        setInstitutionalDashboardError(message);
      }
    } finally {
      if (!signal?.aborted && request === analysisRequest.current) setAnalysisLoading(false);
    }
  }, [analysisFrom, analysisPage, analysisTo, selectedTradingAccount]);

  useEffect(() => {
    setAnalysisData(null);
    setInstitutionalDashboard(null);
    setInstitutionalDashboardError("");
    setAnalysisError("");
    setAnalysisPage(1);
  }, [selectedTradingAccount]);

  const fetchGateData = useCallback(async (signal?: AbortSignal) => {
    try {
      const [cfg, acc, trd, mkt, accountResponse, profileResponse] = await Promise.all([
        apiClient.v2<GateConfigResponse>(
          selectedTradingAccount
            ? `/gate/config?account_id=${encodeURIComponent(selectedTradingAccount)}`
            : "/gate/config",
          "GET",
          undefined,
          signal,
        ).catch(() => null),
        apiClient.v2<GateAccountResponse>(
          selectedTradingAccount
            ? `/gate/account?account_id=${encodeURIComponent(selectedTradingAccount)}`
            : "/gate/account",
          "GET",
          undefined,
          signal,
        ).catch(() => null),
        apiClient.v2<GateTradesResponse>(
          selectedTradingAccount
            ? `/gate/trades?account_id=${encodeURIComponent(selectedTradingAccount)}`
            : "/gate/trades",
          "GET",
          undefined,
          signal,
        ).catch(() => null),
        apiClient.v2<{ markets: GateMarketItem[] }>("/gate/markets", "GET", undefined, signal).catch(() => ({ markets: [] })),
        apiClient.v2<{ accounts: TradingAccountSummary[] }>("/accounts", "GET", undefined, signal).catch(() => ({ accounts: [] })),
        apiClient.v2<{ accounts: GateAccountProfileResponse[] }>("/gate/accounts", "GET", undefined, signal).catch(() => ({ accounts: [] })),
      ]);
      if (signal?.aborted) return;
      if (cfg) {
        setGateConfig(cfg);
        setGateDryRunMode(!cfg.live_enabled);
      }
      if (acc) setGateAccount(acc);
      if (trd) setGateTrades(trd);
      if (mkt?.markets) setGateMarkets(mkt.markets);
      const nextProfiles = Array.isArray(profileResponse?.accounts)
        ? profileResponse.accounts.filter((item) => item && typeof item.account_id === "string")
        : [];
      setGateProfiles(nextProfiles);
      const nextAccounts = Array.isArray(accountResponse?.accounts)
        ? accountResponse.accounts.filter((item) => item && typeof item.account_id === "string")
        : [];
      setTradingAccounts(nextAccounts);
      if (!nextAccounts.some((item) => item.account_id === selectedTradingAccount)) {
        const preferred = nextAccounts.find((item) => item.mode === (cfg?.testnet ? "TESTNET" : "LIVE")) || nextAccounts[0];
        setSelectedTradingAccount(preferred?.account_id || "");
      }
    } catch (e) {
      if (!signal?.aborted) console.error("Failed to load Gate data", e);
    }
  }, [selectedTradingAccount]);

  const handleProvisionGateAccounts = async () => {
    await action(async () => {
      await apiClient.v2("/gate/accounts/provision-defaults", "POST", {});
      await fetchGateData();
      setGateNotice({
        msg: zh
          ? "Gate TestNet 模拟账户与 Live 账户已幂等登记；Live 仍由发布策略锁定。"
          : "Gate TestNet and Live accounts are registered idempotently; Live remains release-locked.",
        type: "ok",
      });
    });
  };

  const handleSaveGateConfig = async (e: React.FormEvent) => {
    e.preventDefault();
    setGateNotice(null);
    setGateVerification(null);
    setGateConnectionTest(null);
    const selectedAccount = tradingAccounts.find((item) => item.account_id === selectedTradingAccount);
    const selectedProfile = gateProfiles.find((item) => item.account_id === selectedTradingAccount);
    const testnet = selectedAccount?.mode === "TESTNET";
    if (!gateApiKeyInput.trim() || !gateApiSecretInput.trim()) {
      setGateNotice({
        msg: zh ? "请输入 API Key 和 API Secret 后再验证。" : "Enter both API Key and API Secret before verification.",
        type: "err",
      });
      return;
    }
    setGateVerificationBusy(true);
    try {
      if (selectedProfile) {
        const res = await apiClient.v2<GateCredentialVerifyResponse>(
          `/gate/accounts/${encodeURIComponent(selectedProfile.account_id)}/credentials/verify`,
          "POST",
          {
          api_key: gateApiKeyInput.trim(),
          api_secret: gateApiSecretInput.trim(),
          },
        );
        setGateVerification(res.validation);
        const credentials = res.account.credentials;
        setGateConfig({
          configured: credentials.configured,
          api_key_masked: credentials.api_key_masked,
          live_enabled: false,
          testnet: credentials.testnet,
          updated_at: credentials.updated_at,
        });
        if (res.validation.valid && res.saved) {
          setGateDryRunMode(true);
          setGateApiKeyInput("");
          setGateApiSecretInput("");
          setGateNotice({
            msg: zh
              ? `只读 API 验证成功，已安全保存到 ${selectedProfile.account_id}；${selectedProfile.api_environment === "TESTNET" ? "后续请求仅发送到 Gate 官方 TestNet。" : "Live 下单仍由发布锁阻止。"}`
              : `Read-only API verification passed and was saved to ${selectedProfile.account_id}; ${selectedProfile.api_environment === "TESTNET" ? "subsequent requests stay on Gate official TestNet." : "live orders remain blocked by the release lock."}`,
            type: "ok",
          });
        } else {
          setGateNotice({
            msg: (zh ? "API 验证未通过，原有凭证未覆盖：" : "API verification failed; existing credentials were not replaced: ")
              + (res.validation.reason || res.validation.status),
            type: "err",
          });
        }
        if (res.validation.valid && res.saved && selectedProfile.api_environment === "TESTNET") {
          try {
            await apiClient.v2(
              `/gate/account/refresh?account_id=${encodeURIComponent(selectedProfile.account_id)}`,
              "POST",
            );
            await refresh();
          } catch (refreshError: unknown) {
            setGateNotice({
              msg: zh
                ? `凭证已保存，但远端账户尚未完成对账：${errorMessage(refreshError, "UNKNOWN")}`
                : `Credentials were saved, but remote reconciliation is not complete: ${errorMessage(refreshError, "UNKNOWN")}`,
              type: "err",
            });
          }
        }
        await fetchGateData();
        return;
      }
      const res = await apiClient.v2<{
        saved: boolean;
        configured: boolean;
        api_key_masked: string;
        live_enabled: boolean;
        validation: { valid: boolean; error?: string };
      }>("/gate/config", "POST", {
        api_key: gateApiKeyInput.trim(),
        api_secret: gateApiSecretInput.trim(),
        live_enabled: !gateDryRunMode,
        testnet,
      });
      setGateConfig({
        configured: res.configured,
        api_key_masked: res.api_key_masked,
        live_enabled: res.live_enabled,
        testnet,
        updated_at: new Date().toISOString(),
      });
      setGateVerification({
        valid: Boolean(res.validation.valid),
        status: res.validation.valid ? "VERIFIED_READ_ONLY" : "INVALID_OR_UNAVAILABLE",
        private_api_access: "EXPLICITLY_REQUESTED",
        reason: res.validation.error,
      });
      if (res.validation.valid) {
        setGateNotice({
          msg: zh ? "Gate.io API 验证成功并已安全持久化！" : "Gate.io API credentials validated and stored!",
          type: "ok",
        });
      } else {
        setGateNotice({
          msg: (zh ? "配置已保存，但验证提示: " : "Saved, but validation notice: ") + (res.validation.error || "未知"),
          type: "err",
        });
      }
      void fetchGateData();
    } catch (err: unknown) {
      setGateNotice({
        msg: errorMessage(err, zh ? "保存配置失败" : "Failed to save config"),
        type: "err",
      });
    } finally {
      setGateVerificationBusy(false);
    }
  };

  const handleGateConnectionTest = async () => {
    setGateNotice(null);
    setGateVerification(null);
    setGateConnectionTest(null);
    const selectedProfile = gateProfiles.find((item) => item.account_id === selectedTradingAccount);
    if (!selectedProfile) {
      setGateNotice({
        msg: zh ? "请先登记并选择 Gate 账户；只读连接测试不会使用全局凭证。" : "Register and select a Gate account first; the read-only test never uses a global credential slot.",
        type: "err",
      });
      return;
    }
    if (!gateApiKeyInput.trim() || !gateApiSecretInput.trim()) {
      setGateNotice({
        msg: zh ? "请输入 API Key 和 API Secret 后再做只读连接测试。" : "Enter both API Key and API Secret before the read-only connection test.",
        type: "err",
      });
      return;
    }
    setGateVerificationBusy(true);
    try {
      const result = await apiClient.v2<GateConnectionTestResponse>(
        `/gate/accounts/${encodeURIComponent(selectedProfile.account_id)}/connection-test`,
        "POST",
        { api_key: gateApiKeyInput.trim(), api_secret: gateApiSecretInput.trim() },
      );
      setGateConnectionTest(result);
      setGateNotice({
        msg: result.valid
          ? (zh ? "只读连接测试完成：没有保存凭证、调用模型或发送订单。" : "Read-only connection test completed: no credentials were saved, model was called, or order was sent.")
          : (zh ? `只读连接测试未通过：${result.message_zh || result.code || result.status}` : `Read-only connection test failed: ${result.message_zh || result.code || result.status}`),
        type: result.valid ? "ok" : "err",
      });
    } catch (err: unknown) {
      setGateNotice({ msg: errorMessage(err, zh ? "只读连接测试失败" : "Read-only connection test failed"), type: "err" });
    } finally {
      setGateVerificationBusy(false);
    }
  };

  const handlePlaceLiveOrder = async (e: React.FormEvent) => {
    e.preventDefault();
    setOrderFeedback(null);
    const selectedAccount = tradingAccounts.find((item) => item.account_id === selectedTradingAccount);
    if (!selectedAccount) {
      setOrderFeedback(zh ? "没有已登记且明确绑定模式/场所的交易账户，已阻止发单。" : "No registered, explicitly scoped trading account is available; order blocked.");
      return;
    }
    const amount = Number(orderAmount.trim());
    const leverage = Number(orderLeverage.trim());
    const priceText = orderPrice.trim();
    const stopLossText = orderStopLoss.trim();
    const takeProfitText = orderTakeProfit.trim();
    const price = priceText ? Number(priceText) : undefined;
    const stopLoss = stopLossText ? Number(stopLossText) : undefined;
    const takeProfit = takeProfitText ? Number(takeProfitText) : undefined;
    const positiveFinite = (value: number | undefined) =>
      value === undefined || (Number.isFinite(value) && value > 0);
    if (
      !Number.isFinite(amount) || amount <= 0 ||
      !Number.isInteger(leverage) || leverage < 1 || leverage > 100 ||
      (orderType === "LIMIT" && (price === undefined || !Number.isFinite(price) || price <= 0)) ||
      !positiveFinite(price) || !positiveFinite(stopLoss) || !positiveFinite(takeProfit) ||
      stopLoss === undefined
    ) {
      setOrderFeedback(
        zh
          ? "输入无效，已阻止发单：数量、杠杆和硬止损必须明确为有限正数；限价单还必须填写限价。"
          : "Order blocked: amount, leverage, and hard stop must be explicit finite positive values; limit orders also require a limit price.",
      );
      return;
    }
    try {
      const res = await apiClient.v2<unknown>("/gate/orders", "POST", {
        symbol: orderSymbol.toUpperCase(),
        side: orderSide,
        order_type: orderType,
        amount,
        price,
        leverage,
        stop_loss: stopLoss,
        take_profit: takeProfit,
        dry_run: gateDryRunMode,
        account_id: selectedAccount.account_id,
        venue: selectedAccount.venue,
      });
      setOrderFeedback(
        (zh ? "预留接口执行响应: " : "Pre-reserved order response: ") + JSON.stringify(res, null, 2)
      );
      void fetchGateData();
    } catch (err: unknown) {
      setOrderFeedback(
        (zh ? "预留接口发单失败: " : "Order failed: ") + errorMessage(err, "Unknown error")
      );
    }
  };

  const handleGateTestnetE2E = async (e: React.FormEvent) => {
    e.preventDefault();
    setE2eFeedback(null);
    const selectedAccount = tradingAccounts.find((item) => item.account_id === selectedTradingAccount);
    if (!selectedAccount || selectedAccount.mode !== "TESTNET" || selectedAccount.venue.toLowerCase() !== "gate") {
      setOrderFeedback(zh ? "必须选择已登记的 Gate TestNet 账户；Live 和本地账户均被阻止。" : "Select a registered Gate TestNet account; Live and local accounts are blocked.");
      return;
    }
    if (!e2eConfirm) {
      setOrderFeedback(zh ? "请勾选 TestNet 明确确认后再执行独立验收。" : "Explicitly confirm the TestNet check before running it.");
      return;
    }
    const stopValue = Number(e2eStopValue.trim());
    const takeProfitValue = Number(e2eTakeProfitValue.trim());
    const leverage = Number(e2eLeverage.trim());
    const amountText = e2eAmount.trim();
    const amount = amountText ? Number(amountText) : undefined;
    if (![stopValue, takeProfitValue].every((value) => Number.isFinite(value) && value > 0) || !Number.isInteger(leverage) || leverage < 1 || leverage > 100 || (amount !== undefined && (!Number.isInteger(amount) || amount <= 0))) {
      setOrderFeedback(zh ? "止损、止盈和杠杆必须是有限正数；杠杆必须为 1-100 的整数。" : "Stop, take-profit, and leverage must be finite positive values; leverage must be an integer from 1 to 100.");
      return;
    }
    setE2eBusy(true);
    setOrderFeedback(null);
    try {
      const res = await apiClient.v2<GateTestnetE2EResponse>("/gate/testnet/order-test", "POST", {
        account_id: selectedAccount.account_id,
        symbol: orderSymbol.toUpperCase(),
        side: orderSide === "BUY" ? "LONG" : "SHORT",
        stop_type: e2eStopType,
        stop_value: stopValue,
         take_profit_type: e2eTakeProfitType,
         take_profit_value: takeProfitValue,
         amount,
         leverage,
        cleanup: e2eCleanup,
        confirm_testnet: true,
        idempotency_key: `gate-e2e-${selectedAccount.account_id}-${orderSymbol.toUpperCase()}-${Date.now()}`,
      });
      setE2eFeedback(res);
      setOrderFeedback(res.message_zh || (res.status === "COMPLETED" ? "Gate TestNet 独立验收完成。" : "Gate TestNet 独立验收未完成，详见阶段回执。"));
      void fetchGateData();
    } catch (err: unknown) {
      setOrderFeedback((zh ? "Gate TestNet 独立验收被阻止：" : "Gate TestNet order test blocked: ") + errorMessage(err, "UNKNOWN"));
    } finally {
      setE2eBusy(false);
    }
  };

  const handleEmergencyCloseGatePosition = async (sym: string) => {
    const selectedAccount = tradingAccounts.find((item) => item.account_id === selectedTradingAccount);
    if (!selectedAccount) {
      setOrderFeedback(zh ? "没有已登记账户，已阻止平仓请求。" : "No registered account is available; close request blocked.");
      return;
    }
    const scopedAccountData = gateAccountScopeConfirmed && gateAccount.account_id === selectedAccount.account_id;
    const matchingPositions = scopedAccountData
      ? (gateAccount?.positions || []).filter((position) => normalizeGateSymbol(position.symbol) === normalizeGateSymbol(sym))
      : [];
    if (!scopedAccountData || matchingPositions.length !== 1) {
      setOrderFeedback(
        zh
          ? "UNKNOWN：没有该账户/标的唯一且已核对的持仓数量，已阻止一键平仓。请刷新账户数据后再操作。"
          : "UNKNOWN: no unique reconciled position quantity is available for this account/symbol; close blocked until account data is refreshed.",
      );
      return;
    }
    const position = matchingPositions[0];
    const amount = Math.abs(Number(position.contracts));
    const positionSide = String(position.side || "").toUpperCase();
    const closeSide = positionSide.includes("SHORT") || positionSide === "SELL"
      ? "BUY"
      : positionSide.includes("LONG") || positionSide === "BUY"
        ? "SELL"
        : null;
    if (!Number.isFinite(amount) || amount <= 0 || !closeSide) {
      setOrderFeedback(
        zh
          ? "UNKNOWN：持仓方向或数量不可核对，已阻止一键平仓。"
          : "UNKNOWN: the reconciled position direction or quantity is invalid; close blocked.",
      );
      return;
    }
    try {
      const res = await apiClient.v2<unknown>("/gate/orders", "POST", {
        symbol: sym,
        side: closeSide,
        order_type: "MARKET",
        amount,
        dry_run: gateDryRunMode,
        reduce_only: true,
        account_id: selectedAccount.account_id,
        venue: selectedAccount.venue,
      });
      setOrderFeedback((zh ? `已提交 ${sym} 的核对数量 reduce-only 请求（以成交回执为准）: ` : `Submitted a reconciled reduce-only request for ${sym}; verify the fill receipt: `) + JSON.stringify(res));
      void fetchGateData();
    } catch (err: unknown) {
      setOrderFeedback((zh ? "平仓失败: " : "Close failed: ") + errorMessage(err, "Error"));
    }
  };

  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const [state, data, chart, accountResponse] = await Promise.all([
          apiClient.v2<Workspace>(
            selectedTradingAccount
              ? `/workspace?account_id=${encodeURIComponent(selectedTradingAccount)}`
              : "/workspace",
            "GET",
            undefined,
            signal,
          ),
          apiClient.marketIntelligence(signal),
          apiClient.chartBars(selected, timeframe, 240, signal),
          apiClient.v2<{ accounts: TradingAccountSummary[] }>("/accounts", "GET", undefined, signal).catch(() => ({ accounts: [] })),
        ]);
        if (signal?.aborted) return;
        const nextAccounts = Array.isArray(accountResponse?.accounts)
          ? accountResponse.accounts.filter((item) => item && typeof item.account_id === "string")
          : [];
        setTradingAccounts(nextAccounts);
        if (!nextAccounts.some((item) => item.account_id === selectedTradingAccount)) {
          setSelectedTradingAccount(nextAccounts[0]?.account_id || "");
        }
        setWorkspace(state);
        setMarket(data);
        setBars(chart.bars);
        setError("");
      } catch (e) {
        if (!signal?.aborted)
          setError(e instanceof Error ? e.message : "unavailable");
      }
    },
    [selected, timeframe, selectedTradingAccount],
  );

  const refreshGateRemote = useCallback(async () => {
    if (!selectedTradingAccount) return;
    setGateRefreshBusy(true);
    setGateNotice(null);
    try {
      await apiClient.v2(
        `/gate/account/refresh?account_id=${encodeURIComponent(selectedTradingAccount)}`,
        "POST",
      );
      await Promise.all([fetchGateData(), refresh()]);
      setGateNotice({
        msg: zh
          ? "已读取并持久化当前 Gate TestNet 远端账户事实；风险面板现在使用这次对账结果。"
          : "The current Gate TestNet account facts were read and persisted; risk now uses this reconciliation.",
        type: "ok",
      });
    } catch (err: unknown) {
      setGateNotice({
        msg: errorMessage(err, zh ? "远端账户刷新失败" : "Remote account refresh failed"),
        type: "err",
      });
    } finally {
      setGateRefreshBusy(false);
    }
  }, [fetchGateData, refresh, selectedTradingAccount, zh]);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    if (surface === "gate-live") void fetchGateData(controller.signal);
    const timer = setInterval(() => {
      void refresh(controller.signal);
      if (surface === "gate-live") void fetchGateData(controller.signal);
    }, 15000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [refresh, surface, fetchAnalysisData, fetchGateData]);

  // Completion-based polling avoids overlapping/out-of-order history responses.
  // Reading fills never submits or replays an order.
  useEffect(() => {
    if (surface !== "analysis") return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await fetchAnalysisData(controller.signal);
      if (!controller.signal.aborted) timer = setTimeout(() => void poll(), 3000);
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [surface, fetchAnalysisData]);

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

  const gateAccountScopeConfirmed =
    gateAccount?.configured === true &&
    gateAccount.account_id === selectedTradingAccount &&
    gateAccount.data_status === "AVAILABLE";
  const selectedGateProfile = gateProfiles.find((profile) => profile.account_id === selectedTradingAccount);
  const gateUsesTestnet = selectedGateProfile?.api_environment === "TESTNET" || gateConfig?.api_environment === "TESTNET";
  const gateDryRunLabel = gateUsesTestnet
    ? (zh ? "Gate TestNet 参数预审（不发送）" : "Gate TestNet validate-only (no send)")
    : copy.dryRunSafety;
  const gateDryRunDescription = gateUsesTestnet
    ? (zh
      ? "开启时只做全链路风控、保护计划和交易参数校验，不调用 Gate TestNet 下单接口；关闭后才允许按当前 TestNet 账户发送。"
      : "When active, run risk, protection-plan, and order-parameter checks without calling Gate TestNet create-order; uncheck only to send for the selected TestNet account.")
      : copy.dryRunDesc;
  const selectedGateAccount = tradingAccounts.find((account) => account.account_id === selectedTradingAccount);
  const gateTestnetSelected = selectedGateAccount?.mode === "TESTNET" && selectedGateAccount.venue.toLowerCase() === "gate";
  const gateMetric = (value: number | null | undefined) =>
    gateAccountScopeConfirmed && value !== null && value !== undefined && Number.isFinite(value)
      ? value.toFixed(2)
      : (zh ? "未知" : "UNKNOWN");

  const displayObservedNumber = (value: number | null | undefined, digits = 2) =>
    value !== null && value !== undefined && Number.isFinite(value)
      ? value.toFixed(digits)
      : (zh ? "未知" : "UNKNOWN");

  const filteredNews = newsItems.filter((item) => {
    if (newsFilter === "bull")
      return item.macro_policy === "BULLISH_POLICY" || (typeof item.sentiment === "number" && item.sentiment > 0.1);
    if (newsFilter === "bear")
      return item.macro_policy === "BEARISH_POLICY" || (typeof item.sentiment === "number" && item.sentiment < -0.1);
    if (newsFilter === "macro")
      return item.category === "macro" || item.macro_policy === "CIRCUIT_BREAKER" || (item.impact_stars && Number(item.impact_stars) >= 3);
    return true;
  });

  const [translatingAll, setTranslatingAll] = useState(false);

  // Background auto-translation for top untranslated news when zh-CN is active
  useEffect(() => {
    if (!zh || !newsItems.length) return;
    const untranslated = newsItems.filter((n) => !n.title_zh && n.event_id).slice(0, 10);
    if (!untranslated.length) return;

    let active = true;
    (async () => {
      let anyTranslated = false;
      for (const item of untranslated) {
        if (!active) break;
        try {
          await apiClient.v2(`/news/${encodeURIComponent(String(item.event_id))}/translate`, "POST");
          anyTranslated = true;
        } catch {
          // ignore
        }
      }
      if (active && anyTranslated) {
        void refresh();
      }
    })();

    return () => {
      active = false;
    };
  }, [zh, newsItems.length, refresh]);

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
        <span className="v2-badge v2-badge--gold">{zh ? "前值 · 预期 · 实际" : "Previous · Forecast · Actual"}</span>
        <button disabled={busy} onClick={() => void action(() => apiClient.v2("/macro-calendar/refresh", "POST"))}>
          {zh ? "更新公开日历" : "Refresh public calendar"}
        </button>
      </header>

      <p className="v2-time-hint">
        {zh ? "公开日历：" : "Calendar: "}{workspace?.macro_calendar?.provider ?? "Forex Factory"}
        {workspace?.macro_calendar?.last_success_at && ` · ${formatHktDateTime(workspace.macro_calendar.last_success_at)}`}
        {" · "}{zh ? "周历仅提供日程、前值和预期，不提供实际公布值，不等于交易放行或官方核验。" : "Schedule/previous/forecast only; actual values and trading clearance are not provided."}
      </p>
      {workspace?.macro_calendar?.error && <p role="alert" className="v2-warning">{workspace.macro_calendar.error}</p>}

      <div
        className="v2-macro-list"
        role="region"
        tabIndex={0}
        aria-label={zh ? "宏观日历事件（可滚动）" : "Macro calendar events (scrollable)"}
      >
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
                  <span className="v2-macro-title">{event.currency} {text(event.title)}</span>
                  <span className="v2-stars">
                    {"★".repeat(event.importance_stars ?? 3)}
                  </span>
                  <span className={`v2-countdown-badge ${minutesLeft <= 60 && !isReleased ? "v2-countdown-badge--urgent" : ""}`}>
                    {isReleased
                      ? zh ? "已公布" : "Released"
                      : minutesLeft > 0
                      ? `${copy.countdown}: ${minutesLeft} ${copy.minutes}`
                      : zh ? "待核实公布值" : "Actual unverified"}
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
                      {event.actual ?? (event.schedule_only ? (zh ? "此源不提供" : "Not supplied") : (zh ? "待公布" : "Pending"))}
                    </strong>
                  </div>
                </div>

                {/* Hard Circuit Breaker Alert Banner */}
                {(isForbidLong || isForbidShort) && (
                  <div className="v2-circuit-banner">
                    <span className="v2-circuit-icon">⚠️</span>
                    <div>
                      <strong>{copy.activeDirective}: {text(event.directive)}</strong>
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
                      {zh ? "查看原始来源" : "Source"} ↗
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
        <div style={{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" }}>
          <h2>📰 {copy.news}</h2>
          {zh && (
            <button
              className="v2-btn-inline"
              style={{ background: "#1e293b", border: "1px solid #3b82f6", color: "#60a5fa", cursor: "pointer" }}
              disabled={translatingAll}
              onClick={async () => {
                setTranslatingAll(true);
                try {
                  const untranslated = newsItems.filter((n) => !n.title_zh && n.event_id);
                  for (const item of untranslated) {
                    try {
                      await apiClient.v2(`/news/${encodeURIComponent(String(item.event_id))}/translate`, "POST");
                    } catch (translationError) {
                      console.warn("News translation failed; continuing with remaining items", translationError);
                    }
                  }
                  await refresh();
                } finally {
                  setTranslatingAll(false);
                }
              }}
            >
              {translatingAll ? "⚡ 正在批量翻译中…" : "⚡ 全部一键中文化"}
            </button>
          )}
        </div>
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
        workspace.decisions.map((d) => {
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
          const time = formatHktTime(d.created_at || proposal.generated_at);
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
                    {isApproved ? "🛡️ 规则审查通过" : "⚠️ 审查未通过"}
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

  const riskCockpit = (() => {
    const cockpit = workspace?.risk_cockpit;
    const snapshot = cockpit?.ledger_snapshot;
    const risk = cockpit?.risk;
    const capacity = cockpit?.capacity;
    const reconciliation = cockpit?.reconciliation;
    const model = cockpit?.model;
    const modelLabel = typeof model === "string" ? model : model?.status || "UNKNOWN";
    const blocked = Boolean(risk?.new_risk_blocked || cockpit?.runtime?.execution_blocked);
    const fact = (value: unknown) => value === null || value === undefined || value === "" ? "UNKNOWN" : String(value);
    return (
      <section className="terminal-panel v2-risk-cockpit" aria-label={zh ? "账户风险台" : "Account risk cockpit"}>
        <header className="v2-panel-header">
          <div>
            <h2>🧭 {zh ? "可信账户风险台" : "Trusted Account Risk Cockpit"}</h2>
            <small className="v2-subtitle">
              {zh ? "风险优先 · 已有仓位计划 · 新机会 · 当日归因" : "Risk first · position plans · opportunities · attribution"}
            </small>
          </div>
          <span className={`v2-badge ${blocked ? "v2-badge--warning" : "v2-badge--bull"}`}>
            {blocked ? (zh ? "新风险已阻断" : "NEW RISK BLOCKED") : (zh ? "可读快照" : "READ-ONLY SNAPSHOT")}
          </span>
        </header>
        {!cockpit || cockpit.status === "SCOPE_REQUIRED" ? (
          <p className="v2-note">{zh ? "选择已登记账户后显示账本事实。" : "Select a registered account to display ledger facts."}</p>
        ) : (
          <>
            <div className="v2-risk-fact-grid">
              <div><span>{zh ? "账户 / 场所" : "Account / Venue"}</span><strong>{fact(cockpit.account_id)} · {fact(cockpit.venue)}</strong></div>
              <div><span>{zh ? "模式" : "Mode"}</span><strong>{fact(cockpit.mode)}</strong></div>
              <div><span>{zh ? "账本净权益" : "Ledger equity"}</span><strong>{fact(snapshot?.net_equity)}</strong></div>
              <div><span>{zh ? "现金 / 预留风险" : "Cash / Reserved risk"}</span><strong>{fact(snapshot?.cash)} / {fact(snapshot?.reserved_risk)}</strong></div>
              <div><span>{zh ? "单笔可做风险" : "Single-trade capacity"}</span><strong>{fact(capacity?.single_trade_risk_available)}</strong></div>
              <div><span>{zh ? "组合可做风险" : "Portfolio capacity"}</span><strong>{fact(capacity?.portfolio_risk_available)}</strong></div>
              <div><span>{zh ? "对账" : "Reconciliation"}</span><strong>{fact(reconciliation?.status)}</strong><small>{fact(reconciliation?.last_reconciled_at)}</small></div>
              <div><span>{zh ? "模型" : "Model"}</span><strong>{fact(modelLabel)}</strong><small>{typeof model === "object" ? fact(model?.required_model || model?.reason_code) : ""}</small></div>
            </div>
            {(risk?.new_risk_block_reasons?.length || cockpit.legacy_data?.unassigned_unverified) ? (
              <div className="v2-risk-warning">
                <strong>{zh ? "阻断原因 / 数据限制" : "Blocks / data limitations"}</strong>
                <span>{[...(risk?.new_risk_block_reasons || []), ...(cockpit.legacy_data?.unassigned_unverified ? ["LEGACY_UNVERIFIED"] : [])].join(" · ") || "UNKNOWN"}</span>
              </div>
            ) : null}
            <div className="v2-protection-strip">
              <strong>{zh ? "保护单身份与状态" : "Protection identity & status"}</strong>
              {cockpit.protections?.length ? cockpit.protections.map((protection) => (
                <span key={`${protection.position_id}-${protection.symbol}`} className={`v2-badge ${protection.status === "ACTIVE" ? "v2-badge--bull" : "v2-badge--warning"}`}>
                  {fact(protection.symbol)} · {fact(protection.position_id)} · {fact(protection.quantity)} · {fact(protection.status)}
                </span>
              )) : <span className="v2-data-tag">{zh ? "无已确认保护仓位" : "No confirmed protected position"}</span>}
            </div>
            <details className="v2-details">
              <summary>{zh ? "应急处理与未知项" : "Emergency handling & UNKNOWN facts"}</summary>
              <ul className="v2-guidance-list">
                {(cockpit.emergency_guidance || ["UNKNOWN: no authoritative fact is available."]).map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}
              </ul>
            </details>
          </>
        )}
      </section>
    );
  })();

  return (
    <LocalizedSurface><div className="v2-workspace">
      <header className="v2-title">
        <div className="v2-title-main">
          <h1>
            {surface === "ledger"
              ? copy.ledger
              : surface === "analysis"
              ? copy.aiAnalysis
              : surface === "gate-live"
              ? copy.gateLiveDesk
              : copy[surface as "dashboard" | "monitor" | "strategies" | "intel"]}
          </h1>
          <span className="v2-timezone-pill" title="系统统一时区: 中国香港 (HKT · UTC+8)">
            🇭🇰 {zh ? "中国香港时区" : "HKT (UTC+8)"} {hkTime}
          </span>
        </div>
        <button className="v2-btn-refresh" disabled={busy} onClick={() => void refresh()}>
          {busy ? "…" : `↻ ${copy.retry}`}
        </button>
      </header>
      <p className="v2-safety">
        {surface === "analysis"
          ? copy.aiAnalysisSub
          : surface === "gate-live"
          ? copy.gateLiveSub
          : copy.safety}
      </p>

      {error && (
        <div role="alert" className="v2-warning">
          {copy.unavailable}
          <details>
            <summary>{copy.evidence}</summary>
            {error}
          </details>
        </div>
      )}

      {/* AI Autonomous Trader Console (N10) */}
      <AITraderPanel
        activeAccount={selectedTradingAccount}
        currentMode={tradingAccounts.find((item) => item.account_id === selectedTradingAccount)?.mode}
        onAccountChange={setSelectedTradingAccount}
        onRefresh={() => { void refresh(); if (surface === "analysis") void fetchAnalysisData(); }}
      />
      {riskCockpit}
      <InstitutionalEvidencePanel activeAccount={selectedTradingAccount} />

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
              <div className="v2-panel-header-title">
                <h2>◉ {zh ? "自选标的与挂载策略管理" : "Watchlist & Mounted Strategies"}</h2>
                <span
                  className={`v2-badge ${
                    workspace?.runtime?.state === "running"
                      ? "v2-badge--bull"
                      : workspace?.runtime?.state === "paused"
                      ? "v2-badge--warning"
                      : "v2-badge--neutral"
                  }`}
                >
                  {workspace?.runtime?.state === "running"
                    ? `● ${zh ? "引擎运行中" : "Engine Running"}`
                    : workspace?.runtime?.state === "paused"
                    ? `⏸ ${zh ? "引擎已暂停" : "Engine Paused"}`
                    : `⏹ ${zh ? "引擎已结束" : "Engine Stopped"}`}
                </span>
                <span className="v2-data-tag">
                  {copy.singleStrategyNotice}
                </span>
              </div>
              <div className="v2-controls">
                <button
                  className="v2-btn-primary"
                  disabled={busy}
                  title={zh ? "启动盯盘守护引擎" : "Start monitoring engine"}
                  onClick={() => void handleStartAll()}
                >
                  ▶ {copy.startAll}
                </button>
                <button
                  className="v2-btn-secondary"
                  disabled={busy}
                  title={zh ? "暂停盯盘守护引擎" : "Pause monitoring engine"}
                  onClick={() => void action(() => apiClient.pauseMonitoring())}
                >
                  ⏸ {copy.pauseAll}
                </button>
                <button
                  className="v2-btn-danger"
                  disabled={busy}
                  title={zh ? "停止盯盘守护引擎" : "Stop monitoring engine"}
                  onClick={() => void action(() => apiClient.stopMonitoring())}
                >
                  ⏹ {copy.stopAll}
                </button>
              </div>
            </header>

            <form
              className="v2-controls v2-add-form"
              onSubmit={(e) => {
                e.preventDefault();
                if (!symbol.trim()) return;
                const sym = symbol.trim().toUpperCase();
                void action(async () => {
                  await apiClient.v2("/watchlist", "POST", { symbol: sym });
                  setSymbolStrategyMap((prev) => ({ ...prev, [sym]: strategy }));
                  setSymbol("");
                });
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
              <label className="v2-input-label">
                {copy.attach}:
                <select
                  className="v2-select v2-strategy-dropdown"
                  value={strategy}
                  onChange={(e) => setStrategy(e.target.value as StrategyId)}
                >
                  {strategyIds.map((s) => (
                    <option key={s} value={s}>
                      {getStrategyOptionLabel(STRATEGY_CATALOG[s], zh)}
                    </option>
                  ))}
                </select>
              </label>
              <button className="v2-btn-primary" disabled={busy}>+ {copy.add}</button>
            </form>

            {!workspace?.watchlist.length && <p className="v2-note">{copy.noWatch}</p>}
            <div className="v2-watch-list">
              {workspace?.watchlist.map((w) => {
                const symSubs = (workspace.subscriptions || []).filter(
                  (s) => s.symbol === w.symbol,
                );
                const activeSub = symSubs.find((s) => s.enabled);
                const anySub = symSubs[0];
                const currentStrategyId: StrategyId =
                  symbolStrategyMap[w.symbol] ||
                  activeSub?.strategy_id ||
                  anySub?.strategy_id ||
                  "ema_trend";
                const isRunning = Boolean(
                  activeSub && activeSub.strategy_id === currentStrategyId,
                );
                const isPaused = Boolean(
                  !isRunning &&
                    anySub &&
                    !anySub.enabled &&
                    anySub.strategy_id === currentStrategyId,
                );
                const currentMeta = STRATEGY_CATALOG[currentStrategyId];

                return (
                  <article className="v2-watch-row" key={w.symbol}>
                    <div className="v2-watch-symbol-box">
                      <button
                        className="v2-symbol-btn"
                        onClick={() => setSelected(w.symbol)}
                      >
                        {w.symbol}
                      </button>
                      <span className="v2-badge v2-badge--neutral">Gate.io Swap</span>
                    </div>

                    <div className="v2-watch-strategy-picker">
                      <span className="v2-field-label">{zh ? "挂载策略" : "Strategy"}:</span>
                      <select
                        className="v2-select v2-strategy-dropdown"
                        disabled={busy}
                        value={currentStrategyId}
                        onChange={(e) =>
                          void handleSelectStrategy(
                            w.symbol,
                            e.target.value as StrategyId,
                            isRunning,
                            currentStrategyId,
                          )
                        }
                      >
                        {strategyIds.map((s) => (
                          <option key={s} value={s}>
                            {getStrategyOptionLabel(STRATEGY_CATALOG[s], zh)}
                          </option>
                        ))}
                      </select>
                      {currentMeta?.isRecommended && (
                        <span className="v2-badge v2-badge--gold">
                          ⭐ {copy.recommendedBadge}
                        </span>
                      )}
                      <span
                        className={`v2-badge v2-badge--${
                          currentMeta?.badgeVariant || "neutral"
                        }`}
                      >
                        {zh ? currentMeta?.styleZh : currentMeta?.styleEn}
                      </span>
                    </div>

                    <div className="v2-watch-status-box">
                      <span
                        className={`v2-badge ${
                          isRunning
                            ? "v2-badge--bull"
                            : isPaused
                            ? "v2-badge--warning"
                            : "v2-badge--neutral"
                        }`}
                      >
                        {isRunning
                          ? `● ${copy.running}`
                          : isPaused
                          ? `⏸ ${copy.paused}`
                          : `⏹ ${copy.stopped}`}
                      </span>
                    </div>

                    <div className="v2-watch-actions">
                      <button
                        className="v2-btn-start"
                        disabled={busy || isRunning}
                        title={zh ? "开启此标的的量化盯盘" : "Start monitoring this strategy"}
                        onClick={() => void handleStartSymbol(w.symbol, currentStrategyId)}
                      >
                        ▶ {copy.start}
                      </button>
                      <button
                        className="v2-btn-pause"
                        disabled={busy || !isRunning}
                        title={zh ? "暂停此标的的量化盯盘" : "Pause monitoring this strategy"}
                        onClick={() => void handlePauseSymbol(w.symbol, currentStrategyId)}
                      >
                        ⏸ {copy.pause}
                      </button>
                      <button
                        className="v2-btn-stop"
                        disabled={busy || (!isRunning && !isPaused)}
                        title={zh ? "结束此标的的量化盯盘" : "Stop monitoring this strategy"}
                        onClick={() => void handleStopSymbol(w.symbol, currentStrategyId)}
                      >
                        ⏹ {copy.stop}
                      </button>
                      <button
                        className="v2-btn-remove"
                        disabled={busy}
                        title={zh ? "从自选中移除" : "Remove from watchlist"}
                        onClick={() =>
                          void action(() =>
                            apiClient.v2(`/watchlist/${w.symbol}`, "DELETE"),
                          )
                        }
                      >
                        ✕ {copy.remove}
                      </button>
                    </div>
                  </article>
                );
              })}
            </div>
          </section>

          {chart}
        </>
      )}

      {/* Surface: Strategies (量化与策略库) */}
      {surface === "strategies" && (
        <>
          <div className="v2-strategies-grid">
            {strategyIds.map((s) => {
              const meta = STRATEGY_CATALOG[s];
              return (
                <article className="terminal-panel v2-strategy-card" key={s}>
                  <header className="v2-strategy-header">
                    <div className="v2-strategy-title-row">
                      <h2>⌁ {zh ? meta.nameZh : meta.nameEn}</h2>
                      {meta.isRecommended && (
                        <span className="v2-badge v2-badge--gold">
                          ⭐ {zh ? "系统推荐" : "Recommended"}
                        </span>
                      )}
                      <span className={`v2-badge v2-badge--${meta.badgeVariant}`}>
                        {zh ? meta.classificationZh : meta.classificationEn}
                      </span>
                    </div>
                    <span className="v2-badge v2-badge--neutral">v2.0.0 · 15m/1h</span>
                  </header>
                  <p className="v2-strategy-desc">{copy[rules[s]]}</p>
                  <div className="v2-strategy-footer">
                    <span className="v2-data-tag">严格输出三要素: 触发价 / 强制止损 / 梯级止盈</span>
                    <Link
                      className="v2-attach-btn"
                      to="/monitor"
                      onClick={() => setStrategy(s)}
                    >
                      {copy.attach} ➔
                    </Link>
                  </div>
                </article>
              );
            })}
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
                void action(() => apiClient.v2(accountScopedPath("/emergency-stop"), "POST"))
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
                const p = pos;
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
            ) : workspace?.positions_source === "GATE_REMOTE_PRIVATE_API_SNAPSHOT" && workspace.positions_data_status !== "AVAILABLE" ? (
              <p className="v2-note">
                {zh
                  ? `Gate 远端持仓事实当前不可用（${workspace.positions_data_status ?? "UNKNOWN"}），未使用本地历史镜像替代。`
                  : `Gate remote position facts are unavailable (${workspace.positions_data_status ?? "UNKNOWN"}); historical local mirrors are not used.`}
              </p>
            ) : (
              <p className="v2-note">{copy.noPositions}</p>
            )}
          </section>

          {decisions}
        </>
      )}

      {/* Surface: Analysis (AI 做单分析看板与交易风格洞察) */}
      {surface === "analysis" && (
        <div className="v2-analysis-desk">
          <InstitutionalAnalysisDashboard
            dashboard={institutionalDashboard}
            loading={analysisLoading}
            error={institutionalDashboardError}
            zh={zh}
          />
          <details className="v2-analysis-legacy-details">
            <summary>{zh ? "展开兼容明细表与逐笔账本" : "Open compatibility tables and ledger details"}</summary>
            <div className="v2-analysis-legacy-content">
          {/* Top 6 KPI Performance Metrics Cards */}
          <div className="v2-analysis-stats-grid">
            <article className="terminal-panel v2-kpi-card">
              <span className="v2-kpi-label">{copy.initialCapital}</span>
              <strong className="v2-kpi-value">
                ${analysisData?.account?.initial_capital_usdt != null ? analysisData.account.initial_capital_usdt.toFixed(2) : "—"}
              </strong>
              <small className="v2-kpi-sub">USDT · 模拟初始本金</small>
            </article>

            <article className="terminal-panel v2-kpi-card">
              <span className="v2-kpi-label">{copy.currentEquity}</span>
              <strong className="v2-kpi-value">
                ${analysisData?.account?.current_equity_usdt != null ? analysisData.account.current_equity_usdt.toFixed(2) : "—"}
              </strong>
              <small className={`v2-kpi-sub ${(analysisData?.account?.net_pnl_usdt ?? 0) >= 0 ? "v2-val--bull" : "v2-val--bear"}`}>
                {analysisData?.account?.net_pnl_usdt != null ? (
                  <>{analysisData.account.net_pnl_usdt >= 0 ? "+" : ""}{analysisData.account.net_pnl_usdt.toFixed(2)} USDT ({analysisData.account.total_roi_pct.toFixed(2)}%)</>
                ) : "—"}
              </small>
            </article>

            <article className="terminal-panel v2-kpi-card">
              <span className="v2-kpi-label">{copy.winRate}</span>
              <strong className="v2-kpi-value v2-val--bull">
                {analysisData?.account?.win_rate_pct != null ? `${analysisData.account.win_rate_pct.toFixed(1)}%` : "—"}
              </strong>
              <small className="v2-kpi-sub">
                {analysisData ? `${analysisData.account.winning_trades} 胜 / ${analysisData.account.losing_trades} 负 (共 ${analysisData.account.total_trades} 笔)` : "—"}
              </small>
            </article>

            <article className="terminal-panel v2-kpi-card">
              <span className="v2-kpi-label">{copy.profitFactor}</span>
              <strong className="v2-kpi-value">
                {analysisData?.account?.profit_factor != null ? analysisData.account.profit_factor.toFixed(2) : "—"}
              </strong>
              <small className="v2-kpi-sub">毛盈利 / 毛亏损</small>
            </article>

            <article className="terminal-panel v2-kpi-card">
              <span className="v2-kpi-label">{copy.maxDrawdown}</span>
              <strong className="v2-kpi-value v2-val--stop">
                {analysisData?.account?.max_drawdown_pct != null ? `${analysisData.account.max_drawdown_pct.toFixed(1)}%` : "—"}
              </strong>
              <small className="v2-kpi-sub">严格硬止损防护</small>
            </article>

            <article className="terminal-panel v2-kpi-card">
              <span className="v2-kpi-label">{zh ? "保证金占用 / 杠杆范围" : "Margin / Leverage Range"}</span>
              <strong className="v2-kpi-value">
                {analysisData?.account?.leverage_range ?? "—"}
              </strong>
              <small className="v2-kpi-sub">
                {analysisData?.account?.margin_used_usdt != null ? `占用: $${analysisData.account.margin_used_usdt.toFixed(2)} USDT` : "—"}
              </small>
            </article>
          </div>

          {/* AI Trader Style DNA & Dynamic Leverage Intelligence */}
          <section className="terminal-panel v2-dna-section">
            <header className="v2-dna-header">
              <div className="v2-dna-title">
                <h2>🧬 {copy.styleDna}</h2>
                <span className="v2-badge v2-badge--gold">
                  {analysisData?.style_dna?.style_label ?? "NOT_RUN"}
                </span>
                <span className="v2-badge v2-badge--bull">
                  {analysisData ? (zh ? "权威记录 · 归属已标注" : "Authoritative records · attribution labeled") : (zh ? "NOT_RUN · 尚无权威记录" : "NOT_RUN · No authoritative records")}
                </span>
              </div>
              <span className="v2-data-tag">
                {zh ? "自适应动态杠杆 (5x~100x) · 锁定单笔最大风险 ≤2% 本金" : "Dynamic Leverage (5x~100x) · Hard Risk Capped at 2%"}
              </span>
            </header>

            <div className="v2-dna-grid">
              <div className="v2-dna-col">
                <div className="v2-dna-metric-row">
                  <span className="v2-factor-label">{zh ? "交易风控纪律评分" : "Discipline Score"}:</span>
                  <strong className="v2-pos-val v2-val--bull">{analysisData?.style_dna?.discipline_score != null ? `${analysisData.style_dna.discipline_score} / 100` : "—"}</strong>
                </div>
                <div className="v2-meter-bar">
                  <div
                    className="v2-meter-fill"
                    style={{ width: `${analysisData?.style_dna?.discipline_score ?? 0}%` }}
                  />
                </div>

                <div className="v2-dna-stats-mini">
                  <div>
                    <span className="v2-factor-label">{zh ? "平均自主杠杆" : "Avg Leverage"}:</span>
                    <strong>{analysisData?.style_dna?.avg_leverage != null ? `${analysisData.style_dna.avg_leverage}x` : "—"}</strong>
                  </div>
                  <div>
                    <span className="v2-factor-label">{zh ? "首选主力策略" : "Preferred Strategy"}:</span>
                    <strong>{analysisData?.style_dna?.best_strategy ?? "—"}</strong>
                  </div>
                </div>
              </div>

              <div className="v2-dna-col v2-dna-col--text">
                <span className="v2-factor-label">⚡ {copy.dynamicLeverage} (5x ~ 100x):</span>
                <p className="v2-dna-desc">
                  {zh
                    ? "杠杆只展示已记录的规则化风险计算结果；它不代表模型胜率或盈利保证。所有开仓仍受统一单笔与组合预算约束，缺少可追溯输入时显示 UNKNOWN。"
                    : "Leverage is a trace of the recorded rule-based risk calculation, not a model win-rate or profit guarantee. Openings remain bounded by unified single-trade and portfolio budgets; missing inputs stay UNKNOWN."}
                </p>
                <div className="v2-dna-eval">
                  <strong>{zh ? "专家系统评语与风格画像:" : "Evaluation Summary & DNA:"}</strong>
                  <span>{analysisData?.style_dna?.style_report ?? "NOT_RUN"}</span>
                </div>
              </div>
            </div>
          </section>

          {/* 6 Strategies Performance Matrix */}
          <section className="terminal-panel">
            <h2>⌁ {copy.strategyMatrix}</h2>
            <div className="v2-table-responsive">
              <table className="v2-table">
                <thead>
                  <tr>
                    <th>{zh ? "策略名称" : "Strategy"}</th>
                    <th>{zh ? "风险与风格定位" : "Classification"}</th>
                    <th>{zh ? "成交笔数" : "Trades"}</th>
                    <th>{zh ? "胜率" : "Win Rate"}</th>
                    <th>{zh ? "已实现盈亏" : "Realized PnL"}</th>
                    <th>{zh ? "平均杠杆" : "Avg Lev"}</th>
                    <th>{zh ? "平均盈亏比 (R:R)" : "Risk-Reward"}</th>
                    <th>{zh ? "推荐等级" : "Status"}</th>
                  </tr>
                </thead>
                <tbody>
                  {(analysisData?.strategy_matrix ?? []).map((row) => (
                    <tr key={row.strategy_id}>
                      <td>
                        <strong>{row.name}</strong>
                      </td>
                      <td>
                        <span className="v2-badge v2-badge--neutral">{row.style}</span>
                      </td>
                      <td>{row.trades_count}</td>
                      <td className={row.win_rate_pct >= 50 ? "v2-val--bull" : "v2-val--bear"}>
                        {row.win_rate_pct.toFixed(1)}% ({row.wins_count}胜)
                      </td>
                      <td className={row.net_pnl_usdt >= 0 ? "v2-val--bull" : "v2-val--bear"}>
                        {row.net_pnl_usdt >= 0 ? `+${row.net_pnl_usdt.toFixed(2)}` : row.net_pnl_usdt.toFixed(2)} USDT
                      </td>
                      <td>
                        <span className="v2-leverage-pill">{row.avg_leverage}x</span>
                      </td>
                      <td>{row.avg_risk_reward > 0 ? `${row.avg_risk_reward.toFixed(1)}R` : "—"}</td>
                      <td>
                        {row.strategy_id === "ema_trend" ? (
                          <span className="v2-badge v2-badge--gold">⭐ {zh ? "系统推荐" : "Recommended"}</span>
                        ) : (
                          <span className="v2-badge v2-badge--bull">{zh ? "可用" : "Ready"}</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          {/* AI Trading Decisions & Execution Ledger */}
          <section className="terminal-panel">
            <div className="v2-panel-header">
              <div className="v2-panel-header-title">
                <h2>📋 {copy.tradeLedger}</h2>
                {analysisData?.execution_scope && (
                  <span className="v2-badge v2-badge--neutral">
                    {analysisData.execution_scope.mode ?? "UNKNOWN"} · {analysisData.execution_scope.venue ?? "UNKNOWN"}
                  </span>
                )}
              </div>
              {analysisData?.execution_summary && (
                <span className="v2-data-tag">
                  {zh
                    ? `权威成交 ${analysisData.execution_summary.fill_count} · 开仓 ${analysisData.execution_summary.entry_fill_count} · 平仓 ${analysisData.execution_summary.exit_fill_count}`
                    : `Authoritative fills ${analysisData.execution_summary.fill_count} · entries ${analysisData.execution_summary.entry_fill_count} · exits ${analysisData.execution_summary.exit_fill_count}`}
                </span>
              )}
            </div>
            <div className="v2-controls" aria-label={zh ? "成交分析筛选" : "Execution analysis filters"}>
              <label className="v2-data-tag">
                {zh ? "账户" : "Account"}&nbsp;
                <select
                  aria-label={zh ? "分析账户" : "Analysis account"}
                  value={selectedTradingAccount}
                  onChange={(event) => {
                    setSelectedTradingAccount(event.target.value);
                    setAnalysisPage(1);
                  }}
                  disabled={!tradingAccounts.length}
                >
                  {!tradingAccounts.length && <option value="">{zh ? "无已登记账户" : "No registered account"}</option>}
                  {tradingAccounts.map((account) => (
                    <option key={account.account_id} value={account.account_id}>
                      {account.account_id} · {account.mode} · {account.venue}
                    </option>
                  ))}
                </select>
              </label>
              <label className="v2-data-tag">
                {zh ? "从" : "From"}&nbsp;
                <input
                  aria-label={zh ? "分析起始时间" : "Analysis start time"}
                  type="datetime-local"
                  value={analysisFrom}
                  onChange={(event) => {
                    setAnalysisFrom(event.target.value);
                    setAnalysisPage(1);
                  }}
                />
              </label>
              <label className="v2-data-tag">
                {zh ? "至" : "To"}&nbsp;
                <input
                  aria-label={zh ? "分析结束时间" : "Analysis end time"}
                  type="datetime-local"
                  value={analysisTo}
                  onChange={(event) => {
                    setAnalysisTo(event.target.value);
                    setAnalysisPage(1);
                  }}
                />
              </label>
              {(analysisFrom || analysisTo) && (
                <button
                  className="v2-btn-refresh"
                  type="button"
                  onClick={() => {
                    setAnalysisFrom("");
                    setAnalysisTo("");
                    setAnalysisPage(1);
                  }}
                >
                  {zh ? "清除时间" : "Clear time"}
                </button>
              )}
              {analysisData?.execution_scope && (
                <small className="v2-data-tag">
                  {zh ? "读取时间" : "Read at"}: {formatHktDateTime(analysisData.execution_scope.read_at)}
                </small>
              )}
            </div>
            {analysisError && (
              <div role="alert" className="v2-warning">
                {zh ? "成交分析暂不可用；未显示推测数据。" : "Execution analysis unavailable; no inferred data is shown."}
                <details>
                  <summary>{copy.evidence}</summary>
                  {analysisError}
                </details>
              </div>
            )}
            {analysisLoading && !analysisData && (
              <p className="v2-note">{zh ? "正在读取权威订单、成交与持仓记录…" : "Loading authoritative orders, fills, and positions…"}</p>
            )}
              {analysisData?.performance_by_attribution && (
              <div className="v2-table-responsive">
                {analysisData.execution_summary?.pnl_window && (
                  <p className="v2-note">
                    {zh
                      ? "区间已实现盈亏只统计筛选窗口内的权威平仓成交；归属沿完整持仓生命周期解析。"
                      : "Windowed realized PnL uses authoritative exits inside the filter; attribution is resolved across the full position lifecycle."}
                  </p>
                )}
                <table className="v2-table" aria-label={zh ? "执行归属绩效" : "Execution attribution performance"}>
                  <thead>
                    <tr>
                      <th>{zh ? "真实归属" : "Verified attribution"}</th>
                      <th>{zh ? "成交 / 开仓 / 平仓" : "Fills / Entries / Exits"}</th>
                      <th>{zh ? "持仓 / 已平仓" : "Positions / Closed"}</th>
                      <th>{zh ? "已实现盈亏" : "Realized PnL"}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(analysisData.performance_by_attribution).map(([attribution, metrics]) => (
                      <tr key={attribution}>
                        <td><span className={`v2-badge ${attribution === "AI_LED" ? "v2-badge--bull" : attribution === "LEGACY_UNCONFIRMED" ? "v2-badge--gold" : "v2-badge--neutral"}`}>{attribution}</span></td>
                        <td>{metrics.fill_count} / {metrics.entry_fill_count} / {metrics.exit_fill_count}</td>
                        <td>{metrics.position_count} / {metrics.closed_position_count}</td>
                        <td className={metrics.realized_pnl_usdt >= 0 ? "v2-val--bull" : "v2-val--bear"}>{metrics.realized_pnl_usdt.toFixed(4)} USDT</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {analysisData.ai_led_performance && (
                  <small className="v2-data-tag">
                    {zh ? "AI_LED 专属绩效仅统计真实 AI_LED 归属：" : "AI_LED-only performance counts only verified AI_LED attribution: "}
                    {analysisData.ai_led_performance.position_count} {zh ? "个持仓" : "positions"} · {analysisData.ai_led_performance.realized_pnl_usdt.toFixed(4)} USDT
                  </small>
                )}
              </div>
            )}
            {analysisData?.execution_positions !== undefined && (
              <div className="v2-table-responsive">
                <h3>{zh ? "持仓生命周期" : "Position lifecycle"}</h3>
                {analysisData.execution_positions.length > 0 ? (
                  <table className="v2-table" aria-label={zh ? "权威持仓生命周期" : "Authoritative position lifecycle"}>
                    <thead>
                      <tr>
                        <th>{zh ? "持仓 / 标的" : "Position / Symbol"}</th>
                        <th>{zh ? "状态 / 保护" : "Status / Protection"}</th>
                        <th>{zh ? "方向 / 剩余数量" : "Side / Remaining"}</th>
                        <th>{zh ? "开仓 / 止损" : "Entry / Stop"}</th>
                        <th>{zh ? "归属路径 / 策略" : "Attribution / Strategy"}</th>
                        <th>{zh ? "计划 / 更新时间" : "Plan / Updated"}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {analysisData.execution_positions.map((position) => {
                        const positionPath = position.attribution ?? position.decision_path ?? "LEGACY_UNCONFIRMED";
                        const positionTone = positionPath === "AI_LED"
                          ? "v2-badge--bull"
                          : positionPath === "LEGACY_UNCONFIRMED"
                            ? "v2-badge--gold"
                            : "v2-badge--neutral";
                        return (
                          <tr key={position.position_id}>
                            <td>
                              <code>{position.position_id}</code>
                              <br />
                              <strong>{position.symbol ?? "UNKNOWN"}</strong>
                              <br />
                              <small className="v2-dim">{position.mode ?? "UNKNOWN"} · {position.venue ?? "UNKNOWN"}</small>
                            </td>
                            <td>
                              <span className={`v2-badge ${position.status === "CLOSED" ? "v2-badge--neutral" : "v2-badge--bull"}`}>
                                {position.status ?? "UNKNOWN"}
                              </span>
                              <br />
                              <small className="v2-dim">{position.protection_status ?? "UNKNOWN"}</small>
                            </td>
                            <td>{position.side ?? "UNKNOWN"} · {position.remaining_contracts ?? "UNKNOWN"}</td>
                            <td>
                              {position.entry_price != null ? `$${position.entry_price.toFixed(4)}` : "UNKNOWN"}
                              <br />
                              <small className="v2-dim">SL {position.stop_loss != null ? `$${position.stop_loss.toFixed(4)}` : "UNKNOWN"}</small>
                            </td>
                            <td>
                              <span className={`v2-badge ${positionTone}`}>{positionPath}</span>
                              <br />
                              <small className="v2-dim">{position.strategy_id ?? "UNKNOWN"} · {position.strategy_version ?? "UNKNOWN"}</small>
                            </td>
                            <td>
                              <code>{position.trade_plan_id ?? "UNKNOWN"}</code>
                              <br />
                              <small className="v2-dim">{position.updated_at ? new Date(position.updated_at).toLocaleString() : "UNKNOWN"}</small>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                ) : (
                  <p className="v2-note">{zh ? "暂无权威持仓；未知归属不会自动分配到账户。" : "No authoritative positions; unknown ownership is not assigned to this account."}</p>
                )}
              </div>
            )}
            {analysisData?.execution_records !== undefined ? (
              <>
                {analysisData.execution_records.length > 0 ? (
                  <div className="v2-table-responsive">
                    <table className="v2-table">
                      <thead>
                        <tr>
                          <th>{zh ? "时间" : "Time"}</th>
                          <th>{zh ? "标的 / 环境" : "Symbol / Environment"}</th>
                          <th>{zh ? "经济动作" : "Economic role"}</th>
                          <th>{zh ? "归属路径" : "Attribution"}</th>
                          <th>{zh ? "策略 / 版本" : "Strategy / Version"}</th>
                          <th>{zh ? "数量 @ 成交价" : "Quantity @ fill"}</th>
                          <th>{zh ? "持仓 / 计划" : "Position / Plan"}</th>
                          <th>{zh ? "权威 ID" : "Authoritative IDs"}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {analysisData.execution_records.map((record) => {
                          const isEntry = record.economic_role === "ENTRY";
                          const pathTone = record.attribution === "AI_LED"
                            ? "v2-badge--bull"
                            : record.attribution === "LEGACY_UNCONFIRMED"
                              ? "v2-badge--gold"
                              : "v2-badge--neutral";
                          return (
                            <tr key={record.record_id}>
                              <td><small>{record.event_at ? new Date(record.event_at).toLocaleString() : "UNKNOWN"}</small></td>
                              <td>
                                <strong>{record.symbol}</strong>
                                <br />
                                <small className="v2-dim">{record.mode} · {record.venue}</small>
                              </td>
                              <td>
                                <span className={`v2-badge ${isEntry ? "v2-badge--bull" : "v2-badge--bear"}`}>
                                  {record.economic_role} · {record.status}
                                </span>
                                <br />
                                <small className="v2-dim">{record.side}</small>
                              </td>
                              <td>
                                <span className={`v2-badge ${pathTone}`}>{record.attribution}</span>
                                <br />
                                <small className="v2-dim">{record.scope_status}</small>
                              </td>
                              <td>
                                <strong>{record.strategy_id}</strong>
                                <br />
                                <small className="v2-dim">{record.strategy_version}</small>
                              </td>
                              <td>
                                {record.quantity ?? "UNKNOWN"} @ {record.price != null ? `$${record.price.toFixed(4)}` : "UNKNOWN"}
                                <br />
                                <small className="v2-dim">{record.fee != null ? `fee ${record.fee} ${record.fee_currency ?? ""}` : "fee UNKNOWN"}</small>
                              </td>
                              <td>
                                <code>{record.position_id ?? "UNKNOWN"}</code>
                                <br />
                                <small className="v2-dim">{record.trade_plan_id ?? (record.position_status || "UNKNOWN")}</small>
                              </td>
                              <td>
                                <small>
                                  fill <code>{record.fill_id}</code>
                                  <br />order <code>{record.order_id ?? "UNKNOWN"}</code>
                                  <br />intent <code>{record.intent_id ?? "UNKNOWN"}</code>
                                </small>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="v2-note">
                    {zh ? "暂无权威成交；下面的订单状态不会被当作开仓或已成交。" : "No authoritative fills yet; order states below are not treated as entries or fills."}
                  </p>
                )}
                {(analysisData.execution_orders ?? []).some((order) => order.fill_count === 0) && (
                  <div className="v2-table-responsive">
                    <table className="v2-table">
                      <thead>
                        <tr>
                          <th>{zh ? "未成交订单" : "Non-filled orders"}</th>
                          <th>{zh ? "标的" : "Symbol"}</th>
                          <th>{zh ? "订单状态" : "Order status"}</th>
                          <th>{zh ? "经济含义" : "Economic meaning"}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(analysisData.execution_orders ?? []).filter((order) => order.fill_count === 0).map((order) => (
                          <tr key={order.intent_id}>
                            <td><code>{order.intent_id}</code></td>
                            <td>{order.symbol}</td>
                            <td>{order.status}</td>
                            <td className="v2-dim">{order.economic_status}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                {analysisData.execution_pagination && (
                  <div className="v2-controls">
                    <small className="v2-data-tag">
                      {zh
                        ? `第 ${analysisData.execution_pagination.page} 页 · 共 ${analysisData.execution_pagination.total_records} 条成交`
                        : `Page ${analysisData.execution_pagination.page} · ${analysisData.execution_pagination.total_records} fills`}
                    </small>
                    <button
                      className="v2-btn-refresh"
                      type="button"
                      disabled={analysisLoading || analysisPage <= 1}
                      onClick={() => setAnalysisPage((page) => Math.max(1, page - 1))}
                    >
                      {zh ? "上一页" : "Previous"}
                    </button>
                    <button
                      className="v2-btn-refresh"
                      type="button"
                      disabled={analysisLoading || !analysisData.execution_pagination.has_more}
                      onClick={() => setAnalysisPage((page) => page + 1)}
                    >
                      {zh ? "下一页" : "Next"}
                    </button>
                  </div>
                )}
              </>
            ) : analysisData?.trades && analysisData.trades.length > 0 ? (
              <div className="v2-table-responsive">
                <table className="v2-table">
                  <thead>
                    <tr>
                      <th>{zh ? "交易 ID" : "Trade ID"}</th>
                      <th>{zh ? "标的" : "Symbol"}</th>
                      <th>{zh ? "策略" : "Strategy"}</th>
                      <th>{zh ? "方向" : "Side"}</th>
                      <th>{zh ? "入场价" : "Entry"}</th>
                      <th>{zh ? "平仓价" : "Exit"}</th>
                      <th>{zh ? "硬止损 / 止盈" : "SL / TP"}</th>
                      <th>{zh ? "AI 自主杠杆" : "AI Leverage"}</th>
                      <th>{zh ? "盈亏 (USDT / %)" : "PnL (USDT / %)"}</th>
                      <th>{zh ? "决策依据与风控记录" : "Decision evidence & risk record"}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {analysisData.trades.map((trade) => {
                      const isLong = trade.side === "LONG";
                      const isWin = trade.pnl_usdt >= 0;
                      return (
                        <tr key={trade.trade_id}>
                          <td><code>{trade.trade_id}</code></td>
                          <td><strong>{trade.symbol}</strong></td>
                          <td><span className="v2-badge v2-badge--neutral">{trade.strategy_name}</span></td>
                          <td>
                            <span className={`v2-badge ${isLong ? "v2-badge--bull" : "v2-badge--bear"}`}>
                              {trade.side}
                            </span>
                          </td>
                          <td>${trade.entry_price.toFixed(2)}</td>
                          <td>{trade.exit_price != null ? `$${trade.exit_price.toFixed(2)}` : "—"}</td>
                          <td>
                            <small className="v2-val--stop">SL: ${trade.stop_loss.toFixed(2)}</small>
                            <br />
                            <small className="v2-val--tp">TP: {trade.targets.map(t => `$${t.toFixed(1)}`).join(" / ")}</small>
                          </td>
                          <td>
                            <span className={`v2-leverage-pill ${trade.leverage >= 50 ? "v2-lev--high" : trade.leverage >= 25 ? "v2-lev--mid" : "v2-lev--low"}`}>
                              {trade.leverage}x
                            </span>
                            <br />
                            <small className="v2-dim">{trade.leverage_reason}</small>
                          </td>
                          <td className={isWin ? "v2-val--bull" : "v2-val--bear"}>
                            <strong>{isWin ? `+${trade.pnl_usdt.toFixed(2)}` : trade.pnl_usdt.toFixed(2)} USDT</strong>
                            <br />
                            <small>({isWin ? `+${trade.roi_pct.toFixed(1)}` : trade.roi_pct.toFixed(1)}%)</small>
                          </td>
                          <td className="v2-rationale-cell">
                            <details className="v2-details">
                              <summary>{zh ? "查看推演逻辑" : "View rationale"}</summary>
                              <p className="v2-rationale-popup">{trade.rationale || trade.leverage_reason}</p>
                            </details>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="v2-note">{zh ? "暂无历史成交，策略盯盘中触发入场后将在此自动记录交易明细与 AI 动态杠杆。" : "No trade records yet."}</p>
            )}
          </section>
            </div>
          </details>
        </div>
      )}

      {/* Surface: Gate Live Desk (芝麻交易所实盘对接与量化接口预留) */}
      {surface === "gate-live" && (
        <div className="v2-gate-desk">
          <section className="terminal-panel v2-gate-account-panel" data-testid="gate-account-profiles">
            <header className="v2-panel-header">
              <div>
                <h2>🧩 {zh ? "Gate 账户隔离" : "Gate account isolation"}</h2>
                <small className="v2-subtitle">
                  {zh
                    ? "Gate TestNet 与 Live 使用不同 account_id、加密凭证槽和执行环境；只有点击验证、刷新或提交动作时才显式访问对应私有 API。"
                    : "Gate TestNet and Live use different account IDs, encrypted credential slots, and environments; private APIs are accessed only by an explicit verify, refresh, or order action."}
                </small>
              </div>
              <button
                type="button"
                className="v2-btn-secondary"
                disabled={busy}
                onClick={() => void handleProvisionGateAccounts()}
              >
                {zh ? "登记默认双账户" : "Register default pair"}
              </button>
            </header>
            {gateProfiles.length ? (
              <div className="v2-analysis-stats-grid">
                {gateProfiles.map((profile) => (
                  <article className="v2-kpi-card" key={profile.account_id}>
                    <span className="v2-kpi-label">{profile.account_kind} · {profile.mode}</span>
                    <strong className="v2-kpi-value">{profile.account_id}</strong>
                    <small className="v2-kpi-sub">
                      {profile.api_environment} · {profile.execution_adapter}
                    </small>
                    <small className="v2-kpi-sub" title={profile.api_base_url}>
                      {profile.credentials.configured
                        ? `${profile.credentials.api_key_masked} · ${zh ? "凭证已配置" : "credentials configured"}`
                        : (profile.api_environment === "TESTNET"
                          ? (zh ? "TestNet 未配置凭证" : "TestNet credentials not configured")
                          : (zh ? "未配置凭证" : "credentials not configured"))}
                      {profile.mode === "LIVE" ? ` · ${zh ? "发布锁定" : "release locked"}` : ""}
                    </small>
                  </article>
                ))}
              </div>
            ) : (
              <p className="v2-note">
                {zh ? "尚未登记 gate_testnet / gate_live；旧 gate_paper 仅作为迁移别名，运行时统一使用 gate_testnet。" : "gate_testnet / gate_live are not registered yet; legacy gate_paper is migration-only and runtime scope is gate_testnet."}
              </p>
            )}
          </section>

          {/* Gate.io Connection & Remote Account Truth Overview */}
          <section className="terminal-panel v2-gate-status-panel">
            <header className="v2-panel-header">
              <div className="v2-panel-header-title">
                <h2>⚡ {zh ? "Gate.io (芝麻交易所) 账户状态与资产总览" : "Gate.io Account Status & Balances"}</h2>
                <span className={`v2-badge ${gateAccountScopeConfirmed ? (gateDryRunMode ? "v2-badge--gold" : "v2-badge--bull") : gateConfig?.configured ? "v2-badge--gold" : "v2-badge--neutral"}`}>
                  {gateAccountScopeConfirmed
                    ? (gateConfig?.api_environment === "TESTNET"
                        ? (gateDryRunMode
                            ? (zh ? "🟡 Gate TestNet · 仅校验不发送" : "🟡 Gate TestNet · Validate only")
                            : (zh ? "🟢 Gate TestNet 远端账户已核对" : "🟢 Gate TestNet remote account reconciled"))
                        : (zh ? "🔒 Gate Live 远端账户已核对 · 发布锁定" : "🔒 Gate Live remote account reconciled · release locked"))
                    : gateConfig?.configured
                      ? (gateConfig.api_environment === "TESTNET"
                          ? (zh ? "🟡 Gate TestNet 凭证已保存 · 等待远端核对" : "🟡 Gate TestNet credentials saved · awaiting remote check")
                          : (zh ? "🔒 Gate Live 凭证已保存 · 发布锁定" : "🔒 Gate Live credentials saved · release locked"))
                      : (zh ? "⚪ 待配置 API 密钥 (当前为公开行情与样本数据)" : "⚪ Unconfigured API Keys")}
                </span>
              </div>
              <span className="v2-data-tag">
                {zh ? "Gate TestNet 远端账户事实 · 私有读取按账户显式触发" : "Gate TestNet remote account truth · private reads are explicit and account-scoped"}
              </span>
              <label className="v2-data-tag">
                {zh ? "执行账户" : "Execution account"}:&nbsp;
                <select
                  aria-label={zh ? "执行账户" : "Execution account"}
                  value={selectedTradingAccount}
                  onChange={(event) => setSelectedTradingAccount(event.target.value)}
                  disabled={!tradingAccounts.length}
                >
                  {!tradingAccounts.length && <option value="">{zh ? "无已登记账户" : "No registered account"}</option>}
                  {tradingAccounts.map((account) => (
                    <option key={account.account_id} value={account.account_id}>
                      {account.account_id} · {account.mode} · {account.venue}
                    </option>
                  ))}
                </select>
              </label>
            </header>

            <div className="v2-analysis-stats-grid">
              <article className="v2-kpi-card">
                <span className="v2-kpi-label">{zh ? "Gate TestNet 账户权益" : "Gate TestNet Equity"}</span>
                <strong className="v2-kpi-value">
                  {gateMetric(gateAccount?.equity)}
                </strong>
                <small className="v2-kpi-sub">
                  USDT · {gateAccount?.equity_basis || (gateAccount?.data_status === "AVAILABLE" ? (zh ? "Gate 远端已核对" : "Gate remote reconciled") : (zh ? "远端数据未知" : "remote data unknown"))}
                  {gateAccount?.observed_at ? ` · ${formatHktDateTime(gateAccount.observed_at)} HKT` : ""}
                </small>
              </article>

              <article className="v2-kpi-card">
                <span className="v2-kpi-label">{zh ? "可用保证金" : "Available Margin"}</span>
                <strong className="v2-kpi-value v2-val--bull">
                  {gateMetric(gateAccount?.available_margin)}
                </strong>
                <small className="v2-kpi-sub">USDT · {gateAccount?.available_margin_basis || (zh ? "Gate 原生 available" : "Gate native available")}</small>
              </article>

              <article className="v2-kpi-card">
                <span className="v2-kpi-label">{zh ? "已用保证金" : "Used Margin"}</span>
                <strong className="v2-kpi-value">
                  {gateMetric(gateAccount?.used_margin)}
                </strong>
                <small className="v2-kpi-sub">USDT · {gateAccount?.used_margin_basis || (zh ? "Gate 原生 position + order margin" : "Gate native position + order margin")}</small>
              </article>

              <article className="v2-kpi-card">
                <span className="v2-kpi-label">{zh ? "远端持仓 / 待单" : "Remote positions / orders"}</span>
                <strong className="v2-kpi-value">
                  {gateAccountScopeConfirmed && gateAccount?.positions ? `${gateAccount.positions.length} / ${gateAccount.pending_orders?.length ?? 0}` : (zh ? "未知" : "UNKNOWN")}
                </strong>
                <small className="v2-kpi-sub">{zh ? "持仓 / 挂单；来源为 Gate 远端" : "positions / pending orders from Gate"}</small>
              </article>
            </div>
            <div className="v2-gate-remote-meta">
              <span>{zh ? "远端状态" : "Remote status"}: {gateAccount?.data_status || "UNKNOWN"}</span>
              <span>{zh ? "来源" : "Source"}: {gateAccount?.source || "NOT_OBSERVED"}</span>
              <span>{zh ? "未实现盈亏" : "Unrealized PnL"}: {gateAccount?.unrealized_pnl == null ? "UNKNOWN" : `${gateAccount.unrealized_pnl} USDT`}</span>
              <span>{zh ? "已实现盈亏" : "Realized PnL"}: {gateAccount?.realized_pnl == null ? "UNKNOWN" : `${gateAccount.realized_pnl} USDT`}</span>
              {gateAccount?.error_code ? <span className="v2-val--stop">{gateAccount.error_code}</span> : null}
            </div>
          </section>

          {/* Two Columns: Left = API Config, Right = Live Quantitative Pre-reserved Interface */}
          <div className="v2-gate-grid-split">
            {/* Left: API Key Config & Safety Switch */}
            <section className="terminal-panel">
              <h2>🔐 {zh ? "Gate API 凭证验证" : "Gate API credential verification"}</h2>
              <p className="v2-field-sub v2-gate-verification-hint">
                {selectedGateProfile
                  ? (zh
                    ? `${selectedGateProfile.account_id} · ${selectedGateProfile.api_environment}：只读验证账户权限后才保存凭证。不会下单；LIVE 仍保持发布锁定。`
                    : `${selectedGateProfile.account_id} · ${selectedGateProfile.api_environment}: credentials are saved only after a read-only account check. No orders; LIVE remains release-locked.`)
                  : (zh
                    ? "先登记并选择 Gate 账户，再输入 API Key 和 Secret。"
                    : "Register and select a Gate account before entering an API Key and Secret.")}
              </p>
              <form className="v2-gate-form" onSubmit={handleSaveGateConfig}>
                <div className="v2-form-group">
                  <label htmlFor="gate-api-key" className="v2-field-label">{copy.gateApiKey}:</label>
                  <input
                    id="gate-api-key"
                    type="text"
                    className="v2-input"
                    placeholder={gateConfig?.api_key_masked ? `当前已保存: ${gateConfig.api_key_masked}` : "输入 Gate.io API Key (32位十六进制)"}
                    value={gateApiKeyInput}
                    autoComplete="off"
                    onChange={(e) => setGateApiKeyInput(e.target.value)}
                  />
                </div>

                <div className="v2-form-group">
                  <label htmlFor="gate-api-secret" className="v2-field-label">{copy.gateApiSecret}:</label>
                  <input
                    id="gate-api-secret"
                    type="password"
                    className="v2-input"
                    placeholder="输入 Gate.io API Secret (私钥仅在本地加密存储)"
                    value={gateApiSecretInput}
                    autoComplete="new-password"
                    onChange={(e) => setGateApiSecretInput(e.target.value)}
                  />
                </div>

                <div className="v2-form-group v2-dry-run-toggle">
                  <label className="v2-warning v2-inline-warning">
                    <input
                      type="checkbox"
                      checked={gateDryRunMode}
                      onChange={(e) => setGateDryRunMode(e.target.checked)}
                    />
                    <div>
                      <strong>🛡️ {gateDryRunLabel} (建议开启)</strong>
                      <p className="v2-toggle-sub">{gateDryRunDescription}</p>
                    </div>
                  </label>
                </div>

                <div className="v2-form-actions">
                  <button type="submit" className="v2-btn-primary" disabled={busy || gateVerificationBusy}>
                    {gateVerificationBusy ? "⏳ " : "🔎 "}
                    {selectedGateProfile ? (zh ? "验证并保存" : "Verify & save") : copy.saveConfig}
                  </button>
                  <button
                    type="button"
                    className="v2-btn-secondary"
                    disabled={busy || gateVerificationBusy}
                    onClick={() => void handleGateConnectionTest()}
                    data-testid="gate-connection-test"
                  >
                    🧪 {zh ? "只读连接测试（不保存）" : "Read-only connection test (no save)"}
                  </button>
                  <button
                    type="button"
                    className="v2-btn-secondary"
                    onClick={() => void refreshGateRemote()}
                    data-testid="gate-remote-refresh"
                    disabled={busy || gateVerificationBusy || gateRefreshBusy || !selectedTradingAccount}
                  >
                    {gateRefreshBusy ? "⏳ " : "🔄 "}{zh ? "刷新并对账远端账户" : "Refresh & reconcile remote account"}
                  </button>
                </div>

                {gateVerification && (
                  <div
                    className={`v2-gate-verification-result ${gateVerification.valid ? "v2-gate-verification-result--ok" : "v2-gate-verification-result--err"}`}
                    data-testid="gate-verification-result"
                    aria-live="polite"
                  >
                    <strong>
                      {gateVerification.valid
                        ? (zh ? "只读 API 验证通过" : "Read-only API verification passed")
                        : (zh ? "API 验证未通过" : "API verification failed")}
                    </strong>
                    <span>
                      {gateVerification.account_id || selectedTradingAccount} · {gateVerification.api_environment || selectedGateProfile?.api_environment || "—"} · {gateVerification.status}
                    </span>
                    {gateVerification.valid ? (
                      <small>
                        {gateVerification.account_type || "Gate.io Futures / Swap"}
                        {gateVerification.data_status ? ` · ${gateVerification.data_status}` : ""}
                        {gateVerification.total_usdt != null ? ` · USDT ${gateVerification.total_usdt.toFixed(2)}` : ""}
                      </small>
                    ) : (
                      <small>{gateVerification.reason || (zh ? "远程返回无效或不可用" : "The remote read was invalid or unavailable.")}</small>
                    )}
                  </div>
                )}

                {gateConnectionTest && (
                  <div
                    className={`v2-gate-verification-result ${gateConnectionTest.valid ? "v2-gate-verification-result--ok" : "v2-gate-verification-result--err"}`}
                    data-testid="gate-connection-test-result"
                    aria-live="polite"
                  >
                    <strong>{gateConnectionTest.message_zh || gateConnectionTest.code || gateConnectionTest.status}</strong>
                    <span>
                      {gateConnectionTest.account_id || selectedTradingAccount} · {gateConnectionTest.api_environment || selectedGateProfile?.api_environment || "—"} · {gateConnectionTest.status}
                    </span>
                    <small>
                      read_only={String(gateConnectionTest.read_only)} · orders_sent={gateConnectionTest.orders_sent} · model_called={String(gateConnectionTest.model_called)} · authorization_created={String(gateConnectionTest.authorization_created)}
                    </small>
                  </div>
                )}

                {gateNotice && (
                  <div className={`v2-notice-banner ${gateNotice.type === "ok" ? "v2-notice--ok" : "v2-notice--err"}`}>
                    {gateNotice.msg}
                  </div>
                )}
              </form>
            </section>

            {/* Right: Controlled Gate TestNet execution interface */}
            <section className="terminal-panel">
              <header className="v2-panel-header">
                <h2>⚡ Gate TestNet 受控执行接口</h2>
                <span className="v2-badge v2-badge--gold">TestNet / 手工触发</span>
              </header>
              <p className="v2-field-sub">
                {zh
                  ? "仅在明确选择账户并满足授权、租约和风险约束后提交；Gate TestNet 才可能发送，Live 始终发布锁定。"
                  : "Orders are submitted only after explicit account scope, authorization, lease, and risk checks; Gate Live remains release-locked."}
              </p>

              <form className="v2-gate-order-form" onSubmit={handlePlaceLiveOrder}>
                <div className="v2-form-row">
                  <div className="v2-form-group">
                    <label htmlFor="gate-order-symbol" className="v2-field-label">{zh ? "合约标的" : "Symbol"}:</label>
                    <input
                      id="gate-order-symbol"
                      type="text"
                      className="v2-input"
                      value={orderSymbol}
                      onChange={(e) => setOrderSymbol(e.target.value)}
                    />
                  </div>

                  <div className="v2-form-group">
                    <label htmlFor="gate-order-side" className="v2-field-label">{zh ? "买卖方向" : "Side"}:</label>
                    <select
                      id="gate-order-side"
                      className="v2-select"
                      value={orderSide}
                      onChange={(e) => setOrderSide(e.target.value as "BUY" | "SELL")}
                    >
                      <option value="BUY">{zh ? "🟢 做多 (BUY LONG)" : "BUY LONG"}</option>
                      <option value="SELL">{zh ? "🔴 做空 (SELL SHORT)" : "SELL SHORT"}</option>
                    </select>
                  </div>

                  <div className="v2-form-group">
                    <label htmlFor="gate-order-type" className="v2-field-label">{zh ? "订单类型" : "Order Type"}:</label>
                    <select
                      id="gate-order-type"
                      className="v2-select"
                      value={orderType}
                      onChange={(e) => setOrderType(e.target.value as "LIMIT" | "MARKET")}
                    >
                      <option value="MARKET">{zh ? "市价单 (MARKET)" : "MARKET"}</option>
                      <option value="LIMIT">{zh ? "限价单 (LIMIT)" : "LIMIT"}</option>
                    </select>
                  </div>
                </div>

                <div className="v2-form-row">
                  <div className="v2-form-group">
                    <label htmlFor="gate-order-amount" className="v2-field-label">{gateTestnetSelected ? (zh ? "Gate 合约张数" : "Gate contracts") : (zh ? "下单张数/数量" : "Amount")}:</label>
                    <input
                      id="gate-order-amount"
                      type="number"
                      step={gateTestnetSelected ? "1" : "0.001"}
                      min={gateTestnetSelected ? "1" : "0.001"}
                      required
                      className="v2-input"
                      placeholder={zh ? "必填，不使用隐含默认值" : "Required; no implicit default"}
                      value={orderAmount}
                      onChange={(e) => setOrderAmount(e.target.value)}
                    />
                    {gateTestnetSelected && <small className="v2-field-sub">Gate 永续合约按 contracts 计数；1 张不是 0.001 BTC。后台以交易所返回的最小张数/步长为准。</small>}
                  </div>

                  <div className="v2-form-group">
                    <label htmlFor="gate-order-price" className="v2-field-label">{zh ? "限价价格 (市价可留空)" : "Price"}:</label>
                    <input
                      id="gate-order-price"
                      type="number"
                      step="0.1"
                      className="v2-input"
                      placeholder="市价自动成交"
                      value={orderPrice}
                      onChange={(e) => setOrderPrice(e.target.value)}
                    />
                  </div>

                  <div className="v2-form-group">
                    <label htmlFor="gate-order-leverage" className="v2-field-label">{zh ? "杠杆倍数 (1-100x)" : "Leverage"}:</label>
                    <input
                      id="gate-order-leverage"
                      type="number"
                      min="1"
                      max="100"
                      required
                      className="v2-input"
                      placeholder={zh ? "必填，不使用隐含默认值" : "Required; no implicit default"}
                      value={orderLeverage}
                      onChange={(e) => setOrderLeverage(e.target.value)}
                    />
                  </div>
                </div>

                <div className="v2-form-row">
                  <div className="v2-form-group">
                    <label htmlFor="gate-order-sl" className="v2-field-label">{zh ? "硬止损价 (SL)" : "Stop Loss"}:</label>
                    <input
                      id="gate-order-sl"
                      type="number"
                      step="0.1"
                      className="v2-input"
                      placeholder="可选硬止损"
                      value={orderStopLoss}
                      onChange={(e) => setOrderStopLoss(e.target.value)}
                    />
                  </div>

                  <div className="v2-form-group">
                    <label htmlFor="gate-order-tp" className="v2-field-label">{zh ? "目标止盈价 (TP)" : "Take Profit"}:</label>
                    <input
                      id="gate-order-tp"
                      type="number"
                      step="0.1"
                      className="v2-input"
                      placeholder="可选止盈目标"
                      value={orderTakeProfit}
                      onChange={(e) => setOrderTakeProfit(e.target.value)}
                    />
                  </div>
                </div>

                <div className="v2-form-actions">
                  <button type="submit" className="v2-btn-primary" disabled={busy}>
                    ⚡ {copy.placeOrder}
                  </button>
                  <button
                    type="button"
                    className="v2-btn-danger"
                    disabled={busy}
                    onClick={() => void handleEmergencyCloseGatePosition(orderSymbol)}
                  >
                    🛑 {zh ? `一键平仓 ${orderSymbol}` : `Close ${orderSymbol}`}
                  </button>
                </div>

                {orderFeedback && (
                  <pre className="v2-pre v2-feedback-box">{orderFeedback}</pre>
                )}
              </form>
            </section>
          </div>

          <section className="terminal-panel v2-gate-e2e-panel" data-testid="gate-testnet-e2e">
            <header className="v2-panel-header">
              <div>
                <h2>🧪 Gate TestNet 独立链路验收</h2>
                <p className="v2-field-sub">真实 TestNet 订单 → 成交 → 远端持仓 → 原生保护 → reduce-only 清理；不调用 AI 授权，不写本地成交。</p>
              </div>
              <span className={`v2-badge ${gateTestnetSelected ? "v2-badge--warning" : "v2-badge--neutral"}`}>
                {gateTestnetSelected ? "TESTNET 可执行" : "需选择 gate_testnet"}
              </span>
            </header>
            <form className="v2-gate-e2e-form" onSubmit={handleGateTestnetE2E}>
              <div className="v2-form-row">
                <div className="v2-form-group">
                  <label htmlFor="gate-e2e-symbol" className="v2-field-label">合约标的</label>
                  <input id="gate-e2e-symbol" className="v2-input" value={orderSymbol} onChange={(event) => setOrderSymbol(event.target.value)} required />
                </div>
                <div className="v2-form-group">
                  <label htmlFor="gate-e2e-side" className="v2-field-label">方向</label>
                  <select id="gate-e2e-side" className="v2-select" value={orderSide} onChange={(event) => setOrderSide(event.target.value as "BUY" | "SELL")}>
                    <option value="BUY">LONG</option>
                    <option value="SELL">SHORT</option>
                  </select>
                </div>
                  <div className="v2-form-group">
                    <label htmlFor="gate-e2e-leverage" className="v2-field-label">杠杆（整数）</label>
                    <input id="gate-e2e-leverage" type="number" min="1" max="100" step="1" className="v2-input" value={e2eLeverage} onChange={(event) => setE2eLeverage(event.target.value)} required />
                  </div>
                  <div className="v2-form-group">
                    <label htmlFor="gate-e2e-amount" className="v2-field-label">数量（Gate contracts，可留空）</label>
                    <input id="gate-e2e-amount" type="number" min="1" step="1" className="v2-input" value={e2eAmount} onChange={(event) => setE2eAmount(event.target.value)} placeholder="留空=远端最小张数" />
                    <small className="v2-field-sub">不填写时读取 Gate 市场元数据的最小可成交张数。</small>
                  </div>
                </div>
              <div className="v2-form-row">
                <div className="v2-form-group">
                  <label htmlFor="gate-e2e-stop-type" className="v2-field-label">止损类型</label>
                  <select id="gate-e2e-stop-type" className="v2-select" value={e2eStopType} onChange={(event) => setE2eStopType(event.target.value as "PRICE" | "PERCENT" | "ATR")}>
                    <option value="PRICE">PRICE</option>
                    <option value="PERCENT">PERCENT（百分比）</option>
                    <option value="ATR">ATR（15m）</option>
                  </select>
                </div>
                <div className="v2-form-group">
                  <label htmlFor="gate-e2e-stop-value" className="v2-field-label">止损参数</label>
                  <input id="gate-e2e-stop-value" type="number" min="0" step="any" className="v2-input" value={e2eStopValue} onChange={(event) => setE2eStopValue(event.target.value)} placeholder="按所选类型填写" required />
                </div>
                <div className="v2-form-group">
                  <label htmlFor="gate-e2e-tp-type" className="v2-field-label">止盈类型</label>
                  <select id="gate-e2e-tp-type" className="v2-select" value={e2eTakeProfitType} onChange={(event) => setE2eTakeProfitType(event.target.value as "PRICE" | "PERCENT" | "ATR")}>
                    <option value="PRICE">PRICE</option>
                    <option value="PERCENT">PERCENT（百分比）</option>
                    <option value="ATR">ATR（15m）</option>
                  </select>
                </div>
                <div className="v2-form-group">
                  <label htmlFor="gate-e2e-tp-value" className="v2-field-label">止盈参数</label>
                  <input id="gate-e2e-tp-value" type="number" min="0" step="any" className="v2-input" value={e2eTakeProfitValue} onChange={(event) => setE2eTakeProfitValue(event.target.value)} placeholder="按所选类型填写" required />
                </div>
              </div>
              <div className="v2-gate-e2e-options">
                <label className="v2-warning v2-inline-warning">
                  <input type="checkbox" checked={e2eCleanup} onChange={(event) => setE2eCleanup(event.target.checked)} />
                  <span>完成后发送 reduce-only 清理，并核对远端持仓归零</span>
                </label>
                <label className="v2-warning v2-inline-warning">
                  <input type="checkbox" checked={e2eConfirm} onChange={(event) => setE2eConfirm(event.target.checked)} />
                  <span>我确认这是 Gate TestNet，不是真实资金账户</span>
                </label>
              </div>
              <div className="v2-form-actions">
                <button type="submit" className="v2-btn-primary" disabled={e2eBusy || busy || !gateTestnetSelected || !e2eConfirm}>
                  {e2eBusy ? "⏳ 执行中…" : "🧪 执行 TestNet 独立验收"}
                </button>
                <span className="v2-field-sub">不会自动执行；每次点击均生成新的幂等键。</span>
              </div>
              {e2eFeedback && (
                <div className={`v2-gate-e2e-result ${e2eFeedback.status === "COMPLETED" ? "v2-gate-e2e-result--ok" : "v2-gate-e2e-result--err"}`} aria-live="polite">
                  <strong>{e2eFeedback.message_zh || e2eFeedback.error_code || e2eFeedback.status || "UNKNOWN"}</strong>
                  <span>{e2eFeedback.run_id || "UNKNOWN"} · orders_sent={e2eFeedback.orders_sent ?? "UNKNOWN"} · local_fill_created={String(e2eFeedback.local_fill_created ?? "UNKNOWN")}</span>
                  {e2eFeedback.stages?.length ? (
                    <div className="v2-gate-e2e-stages">
                      {e2eFeedback.stages.map((stage, index) => (
                        <span key={`${stage.stage || "stage"}-${index}`} className={["COMPLETED", "PASS", "FILLED"].includes(String(stage.status || "").toUpperCase()) ? "v2-val--bull" : "v2-val--stop"}>
                          {stage.stage || "UNKNOWN"}: {stage.status || "UNKNOWN"}{stage.message_zh || stage.reason ? ` · ${stage.message_zh || stage.reason}` : ""}{stage.reason_code ? ` (${stage.reason_code})` : ""}
                        </span>
                      ))}
                    </div>
                  ) : null}
                </div>
              )}
            </form>
          </section>

          {/* Gate.io Real Trade Ledger & Fee Audit Table */}
          <section className="terminal-panel">
            <header className="v2-panel-header">
              <div className="v2-panel-header-title">
                <h2>📑 {copy.gateTrades}</h2>
                <span className="v2-badge v2-badge--neutral">
                  {gateTrades?.summary?.source ?? "Gate.io API"}
                </span>
              </div>
                <div className="v2-data-tag">
                {zh
                  ? `累计成交: ${gateTrades?.trades?.length ?? 0} 笔 · 手续费: ${gateTrades?.summary?.total_fee_cost == null ? "未知" : gateTrades.summary.total_fee_cost.toFixed(4)} USDT`
                  : `Total trades: ${gateTrades?.trades?.length ?? 0} · Fee: ${gateTrades?.summary?.total_fee_cost == null ? "UNKNOWN" : gateTrades.summary.total_fee_cost.toFixed(4)} USDT`}
                {gateTrades?.summary?.fee_status ? ` · ${gateTrades.summary.fee_status}` : ""}
              </div>
            </header>

            {gateTrades?.notice && (
              <div className="v2-warning v2-notice-banner">
                {gateTrades.notice}
              </div>
            )}

            {gateTrades?.trades && gateTrades.trades.length > 0 ? (
              <div className="v2-table-responsive">
                <table className="v2-table">
                  <thead>
                    <tr>
                      <th>{zh ? "成交 ID / 订单号" : "Fill ID / Order ID"}</th>
                      <th>{zh ? "合约标的" : "Symbol"}</th>
                      <th>{zh ? "成交时间" : "Execution Time"}</th>
                      <th>{zh ? "方向" : "Side"}</th>
                      <th>{zh ? "成交价格" : "Price"}</th>
                      <th>{zh ? "成交数量" : "Amount"}</th>
                      <th>{zh ? "总成交额 (USDT)" : "Cost (USDT)"}</th>
                      <th>{zh ? "手续费消耗" : "Fee Cost"}</th>
                      <th>{zh ? "实现盈亏" : "Realized PnL"}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {gateTrades.trades.map((t) => {
                      const isBuy = t.side.toUpperCase() === "BUY";
                      const pnl = t.pnl != null ? Number(t.pnl) : null;
                      return (
                        <tr key={t.id}>
                          <td>
                            <code>{t.id}</code>
                            <br />
                            <small className="v2-dim">{t.order_id}</small>
                          </td>
                          <td><strong>{t.symbol}</strong></td>
                          <td><time className="v2-dim" title="中国香港时间 (HKT)">{formatHktDateTime(t.datetime)} (HKT)</time></td>
                          <td>
                            <span className={`v2-badge ${isBuy ? "v2-badge--bull" : "v2-badge--bear"}`}>
                              {t.side}
                            </span>
                          </td>
                          <td>${displayObservedNumber(t.price)}</td>
                          <td>{displayObservedNumber(t.amount, 6)}</td>
                          <td>${displayObservedNumber(t.cost)}</td>
                          <td>
                            <span className="v2-val--stop">
                              {displayObservedNumber(t.fee_cost, 6)} {t.fee_currency || (zh ? "币种未知" : "currency unknown")}
                              {t.fee_evidence_status && <small className="v2-dim"> · {t.fee_evidence_status}</small>}
                            </span>
                          </td>
                          <td className={pnl == null ? "" : pnl >= 0 ? "v2-val--bull" : "v2-val--bear"}>
                            {pnl != null ? `${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)} USDT` : "—"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="v2-note">{copy.noGateTrades}</p>
            )}
          </section>

          {/* Gate.io 300+ USDT Contracts Universe Browser */}
          <section className="terminal-panel">
            <header className="v2-panel-header">
              <div className="v2-panel-header-title">
                <h2>🌐 {copy.gateMarkets}</h2>
                <span className="v2-badge v2-badge--bull">300+ 活跃全量合约</span>
              </div>
              <div className="v2-controls">
                <input
                  type="text"
                  className="v2-input v2-search-input"
                  placeholder={copy.searchContracts}
                  value={marketSearch}
                  onChange={(e) => setMarketSearch(e.target.value)}
                />
              </div>
            </header>

            <div className="v2-table-responsive v2-universe-scroll">
              <table className="v2-table">
                <thead>
                  <tr>
                    <th>{zh ? "标的合约代码" : "Symbol"}</th>
                    <th>{zh ? "基础币种 / 结算币" : "Base / Quote"}</th>
                    <th>{zh ? "最小开仓张数" : "Min Amount"}</th>
                    <th>{zh ? "最高支持杠杆" : "Max Leverage"}</th>
                    <th>{zh ? "状态" : "Status"}</th>
                    <th>{zh ? "快捷操作" : "Action"}</th>
                  </tr>
                </thead>
                <tbody>
                  {gateMarkets
                    .filter((m) =>
                      marketSearch
                        ? m.symbol.toLowerCase().includes(marketSearch.toLowerCase()) ||
                          m.base.toLowerCase().includes(marketSearch.toLowerCase())
                        : true
                    )
                    .slice(0, 100)
                    .map((m) => (
                      <tr key={m.id}>
                        <td><strong>{m.symbol}</strong></td>
                        <td>{m.base} / {m.quote}</td>
                        <td>{m.min_amount}</td>
                        <td><span className="v2-leverage-pill v2-lev--high">{m.max_leverage}x</span></td>
                        <td><span className="v2-badge v2-badge--bull">Active</span></td>
                        <td>
                          <button
                            type="button"
                            className="v2-attach-btn"
                            onClick={() => {
                              setSelected(m.symbol);
                              setOrderSymbol(m.symbol);
                            }}
                          >
                            {zh ? "选中分析 / 交易" : "Select"}
                          </button>
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          </section>
        </div>
      )}
    </div></LocalizedSurface>
  );
}
