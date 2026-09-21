import React, { useState, useEffect, useRef } from 'react';
import { ApiError, apiClient } from '../api/client';
import { LocalizedSurface, useI18n } from '../i18n';
import { Link } from 'react-router-dom';
import './aiStrategy.css';
import './aiTraderCockpit.css';
import { formatTradingTime } from '../tradingTime';
import { AIDecisionHistory } from './AIDecisionHistory';
import { rememberTradingAccount } from '../tradingAccountSelection';

export interface AITraderPanelProps {
  currentMode?: string;
  activeAccount?: string;
  onRefresh?: () => void;
  onAccountChange?: (accountId: string) => void;
}

interface TradingAccountSummary {
  account_id: string;
  mode: string;
  venue: string;
  status?: string;
}

interface TradePlan {
  symbol?: string;
  action?: string;
  reduce_fraction?: unknown;
  reduce_quantity?: unknown;
  leverage?: unknown;
  stop_loss?: unknown;
  take_profit?: unknown;
  entry_expires_at?: string;
  time_exit_at?: string;
  abandon_chase_condition?: unknown;
  partial_take_profits?: unknown[];
  trailing_protection?: { type?: string };
  entry_trigger?: string;
  conditions?: { entry?: { type?: string } };
  evidence?: unknown[];
  news_revision_ids?: string[];
}

interface TradePlanExecution {
  status?: string;
  condition_evaluation?: { status?: string };
  news_evidence?: { status?: string };
}

interface TradePlanRecord {
  plan_id?: string;
  status?: string;
  plan?: TradePlan;
  execution_result?: TradePlanExecution | null;
  updated_at?: string;
}

interface SessionRecord {
  state?: string;
  generation?: number;
}

interface SessionStatusResponse {
  session?: SessionRecord | null;
  ai_session?: { state?: string; session_state?: string; enabled?: boolean; worker_alive?: boolean; last_reason?: string; last_error?: string; max_symbols?: number; schedule?: { interval_minutes?: number; strategy_id?: string; strategy_name?: string; strategy_revision?: number; alignment?: string; next_scan_at?: string; last_started_at?: string; last_completed_at?: string }; dynamic_risk?: { status?: string; entry_allowed?: boolean; reasons?: string[]; blocked_until?: string | null; atr_adaptive_sizing?: { enabled?: boolean; status?: string } } };
  protection_summary?: { active_positions?: number | null };
}

interface GateRemoteAccount {
  configured?: boolean;
  account_id?: string | null;
  mode?: string;
  data_status?: string;
  capability_status?: string;
  observed_at?: string | null;
  source?: string | null;
  equity?: number | null;
  equity_basis?: string | null;
  available_margin?: number | null;
  available_margin_basis?: string | null;
  used_margin?: number | null;
  used_margin_basis?: string | null;
  unrealized_pnl?: number | null;
  realized_pnl?: number | null;
  balance?: { total?: number | null; free?: number | null; used?: number | null };
  positions?: unknown[];
  pending_orders?: unknown[];
  fills?: unknown[];
  error_code?: string | null;
}

interface DiagnosticsResponse {
  archive_path?: string;
  secret_scan?: string;
}

export function RiskLockCountdown({ blockedUntil, reasons }: { blockedUntil?: string | null; reasons?: string[] }) {
  const [now, setNow] = useState(() => Date.now());
  const unlockAt = blockedUntil ? Date.parse(blockedUntil) : Number.NaN;
  useEffect(() => {
    if (!Number.isFinite(unlockAt) || unlockAt <= Date.now()) return;
    const timer = window.setInterval(() => {
      const current = Date.now();
      setNow(current);
      if (current >= unlockAt) window.clearInterval(timer);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [blockedUntil, unlockAt]);
  const remaining = Number.isFinite(unlockAt) ? Math.max(0, unlockAt - now) : null;
  const countdown = remaining === null ? "等待权威状态" : remaining === 0
    ? "冷却时间已到，等待服务端确认解除"
    : "冷却倒计时 " + [Math.floor(remaining / 3_600_000), Math.floor((remaining % 3_600_000) / 60_000), Math.floor((remaining % 60_000) / 1000)].map(value => String(value).padStart(2, "0")).join(":");
  return <div className="ai-cockpit__risk-lock" role="group" aria-label="风控静默锁状态">
    <i />
    <div><strong>🛡️ 冷板凳静默中</strong><span>{reasons?.join(" · ") || "DYNAMIC_RISK_LOCK"} · {blockedUntil ? "解锁时间 " + formatTradingTime(blockedUntil) : ""} · <time aria-label="冷却倒计时" aria-live="off">{countdown}</time> · 开仓权限以服务端风控状态为准</span></div>
  </div>;
}

interface ModelRuntimeHealth {
  available?: boolean;
  model_available?: boolean;
  model_id?: string;
  required_model?: string;
  actual_model_id?: string;
  model_identity_source?: string;
  checked_at?: string;
  error_code?: string;
}

interface AIDecisionCycle {
  cycle_id?: string;
  action?: string;
  reason?: string;
  human_message?: string;
  operational_state?: string;
  decision_origin?: string;
  model_called?: boolean;
  timestamp?: string;
  status?: string;
  block_reason?: string;
  payload?: Record<string, unknown>;
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error) return error.message || fallback;
  if (error && typeof error === 'object' && 'message' in error) {
    return String((error as { message?: unknown }).message || fallback);
  }
  return fallback;
}

function canonicalAccountId(value: string | undefined | null): string {
  const accountId = String(value || '').trim();
  return accountId === 'gate_paper' ? 'gate_testnet' : accountId;
}

function displayNumber(value: unknown, suffix = ''): string {
  if (value === null || value === undefined || String(value).trim() === '') return 'UNKNOWN';
  const numeric = Number(value);
  return Number.isFinite(numeric) ? `${numeric.toLocaleString(undefined, { maximumFractionDigits: 4 })}${suffix}` : 'UNKNOWN';
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise(resolve => window.setTimeout(resolve, milliseconds));
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function decisionMetric(cycle: AIDecisionCycle | null, key: 'confidence' | 'trigger_completion_pct'): number | null {
  const payload = record(cycle?.payload);
  const modelOutput = record(payload.model_output);
  const extra = record(modelOutput.extra_fields);
  const strategyAnalysis = record(extra.strategy_analysis || payload.strategy_analysis);
  const raw = key === 'confidence'
    ? (extra.confidence ?? payload.confidence)
    : (strategyAnalysis.trigger_completion_pct ?? strategyAnalysis.market_readiness ?? payload.market_readiness);
  const numeric = Number(raw);
  return Number.isFinite(numeric) ? Math.max(0, Math.min(100, numeric)) : null;
}

function decisionInstrument(cycle: AIDecisionCycle | null): string {
  const payload = record(cycle?.payload);
  const output = record(payload.model_output);
  return String(output.instrument_id || payload.instrument_id || 'MARKET').toUpperCase();
}

function isBonsaiIdentity(value: unknown): boolean {
  if (typeof value !== 'string' || !value.trim()) return false;
  let basename = value.trim().replace(/\\/g, '/').split('/').pop() || '';
  if (basename.toLowerCase().endsWith('.gguf')) basename = basename.slice(0, -5);
  if (basename.toLowerCase().startsWith('ternary-')) basename = basename.slice('ternary-'.length);
  return basename.toLowerCase() === 'bonsai-2-27b-ptq1_0';
}

const MODEL_HEALTH_MAX_AGE_MS = 60_000;
const MODEL_HEALTH_FUTURE_SKEW_MS = 5_000;

function modelHealthFreshUntil(value: unknown): number | null {
  if (typeof value !== 'string') return null;
  const checkedAt = Date.parse(value);
  if (!Number.isFinite(checkedAt)) return null;
  const age = Date.now() - checkedAt;
  if (age < -MODEL_HEALTH_FUTURE_SKEW_MS || age > MODEL_HEALTH_MAX_AGE_MS) return null;
  return checkedAt + MODEL_HEALTH_MAX_AGE_MS;
}

export const AITraderPanel: React.FC<AITraderPanelProps> = ({
  currentMode,
  activeAccount,
  onRefresh,
  onAccountChange,
}) => {
  const { language } = useI18n();
  const chinese = language === 'zh-CN';
  const [accounts, setAccounts] = useState<TradingAccountSummary[]>([]);
  const [selectedAccount, setSelectedAccount] = useState<string>(canonicalAccountId(activeAccount));
  const [selectedMode, setSelectedMode] = useState<string>((currentMode || '').trim().toUpperCase());
  const [accountsLoading, setAccountsLoading] = useState(true);
  const [sessionActionLoading, setSessionActionLoading] = useState(false);
  const [exportingDiag, setExportingDiag] = useState(false);
  const [diagMessage, setDiagMessage] = useState<string | null>(null);
  const [sessionState, setSessionState] = useState<string>('NOT_CHECKED');
  const [generation, setGeneration] = useState<number>(0);
  // A missing status is not an empty protection set.  Keep it UNKNOWN until
  // the scoped runtime reports an authoritative count.
  const [protectionCount, setProtectionCount] = useState<number | null>(null);
  const [tradePlans, setTradePlans] = useState<TradePlanRecord[]>([]);
  const [tradePlansState, setTradePlansState] = useState<string>('NOT_CHECKED');
  const [gateRemoteAccount, setGateRemoteAccount] = useState<GateRemoteAccount | null>(null);
  const [latestCycle, setLatestCycle] = useState<AIDecisionCycle | null>(null);
  const requestSequence = useRef(0);
  const [cycles, setCycles] = useState<AIDecisionCycle[]>([]);
  const [runtimeDetail, setRuntimeDetail] = useState<SessionStatusResponse['ai_session']>();
  const [modelHealth, setModelHealth] = useState<ModelRuntimeHealth | null>(null);
  const [modelHealthChecked, setModelHealthChecked] = useState(false);
  const [, setModelHealthFreshnessTick] = useState(0);
  const [accountError, setAccountError] = useState('');
  const pollBusy = useRef(false);

  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let expiryTimer: ReturnType<typeof setTimeout> | undefined;
    const refreshModelHealth = async () => {
      try {
        const health = await apiClient.modelHealth();
        if (!cancelled) {
          setModelHealth(health as ModelRuntimeHealth);
          setModelHealthChecked(true);
          if (expiryTimer) clearTimeout(expiryTimer);
          const freshUntil = modelHealthFreshUntil(health.checked_at);
          if (freshUntil !== null) {
            expiryTimer = setTimeout(
              () => setModelHealthFreshnessTick(Date.now()),
              Math.max(0, freshUntil - Date.now() + 1),
            );
          }
        }
      } catch {
        if (!cancelled) {
          setModelHealth(null);
          setModelHealthChecked(true);
        }
      } finally {
        if (!cancelled) timer = setTimeout(() => void refreshModelHealth(), 15000);
      }
    };
    void refreshModelHealth();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      if (expiryTimer) clearTimeout(expiryTimer);
    };
  }, []);

  useEffect(() => {
    if (activeAccount) {
      const canonical = canonicalAccountId(activeAccount);
      setSelectedAccount(canonical);
      const acc = accounts.find((item) => item.account_id === canonical);
      if (acc) {
        setSelectedMode(String(acc.mode || '').toUpperCase());
      }
    }
  }, [activeAccount, accounts]);

  useEffect(() => {
    let mounted = true;
    const loadAccounts = async () => {
      setAccountsLoading(true);
      try {
        const data = await apiClient.v2<{ accounts?: TradingAccountSummary[] }>('/accounts');
        const nextAccounts = Array.isArray(data.accounts)
          ? data.accounts.filter((item: TradingAccountSummary) => item && typeof item.account_id === 'string')
          : [];
        if (!mounted) return;
        setAccounts(nextAccounts);
        const preferred = canonicalAccountId(activeAccount) && nextAccounts.find((item: TradingAccountSummary) => item.account_id === canonicalAccountId(activeAccount));
        const selected = preferred || nextAccounts.find((item) => item.account_id === 'gate_testnet') || nextAccounts[0];
        if (selected) {
          setSelectedAccount(selected.account_id);
          setSelectedMode(String(selected.mode || currentMode || '').toUpperCase());
          if (!activeAccount) {
            onAccountChange?.(selected.account_id);
          }
        }
      } catch {
        if (mounted && !activeAccount) {
          setAccounts([]);
          setSelectedAccount('');
          setSelectedMode('');
        }
      } finally {
        if (mounted) setAccountsLoading(false);
      }
    };
    void loadAccounts();
    return () => {
      mounted = false;
    };
  }, [activeAccount, currentMode, onAccountChange]);

  const fetchActiveAuthAndSession = async () => {
    const requestId = ++requestSequence.current;
    const accountId = selectedAccount.trim();
    if (!accountId) {
      setSessionState('UNSCOPED');
      setGeneration(0);
      setProtectionCount(null);
      setTradePlans([]);
      setTradePlansState('UNSCOPED');
      setGateRemoteAccount(null);
      setLatestCycle(null);
      return;
    }
    const valid = () => requestId === requestSequence.current;
    await Promise.allSettled([
      apiClient.v2<SessionStatusResponse>(`/ai-session/status?account_id=${encodeURIComponent(accountId)}`).then(status => {
        if (!valid()) return;
        const isPaused = status.session?.state === 'PAUSED' || status.ai_session?.state === 'PAUSED' || status.ai_session?.session_state === 'PAUSED';
        setSessionState(isPaused ? 'PAUSED' : (status.ai_session ? (status.ai_session.enabled && status.ai_session.worker_alive ? 'RUNNING' : status.ai_session.state || 'STOPPED') : status.session?.state || 'NOT_REPORTED'));
        setRuntimeDetail(status.ai_session);
        setGeneration(Number(status.session?.generation) || 0);
        setProtectionCount(status.protection_summary?.active_positions ?? null);
      }).catch(error => { if (valid()) { setSessionState('UNAVAILABLE'); setDiagMessage(errorMessage(error, '状态读取失败')); } }),
      apiClient.v2<{ plans?: TradePlanRecord[] }>(`/trade-plans?account_id=${encodeURIComponent(accountId)}&limit=20`).then(data => {
        if (valid()) { setTradePlans(data.plans || []); setTradePlansState(data.plans?.length ? 'DURABLE' : 'NO_DURABLE_PLANS'); }
      }).catch(() => { if (valid()) { setTradePlans([]); setTradePlansState('UNAVAILABLE'); } }),
      apiClient.v2<GateRemoteAccount>(`/gate/account?account_id=${encodeURIComponent(accountId)}`).then(data => {
        if (valid()) { setGateRemoteAccount(data); setAccountError(data.error_code || (data.data_status !== 'AVAILABLE' ? data.data_status || '账户状态未知' : '')); }
      }).catch(error => { if (valid()) { setGateRemoteAccount(null); setAccountError(errorMessage(error, 'Gate 账户读取失败')); } }),
      apiClient.v2<{ cycles?: AIDecisionCycle[] }>(`/ai-session/cycles?account_id=${encodeURIComponent(accountId)}&limit=20`).then(data => {
        if (valid()) { setCycles(data.cycles || []); setLatestCycle(data.cycles?.[0] || null); }
      }).catch(error => { if (valid()) setDiagMessage(errorMessage(error, '决策记录读取失败')); }),
    ]);
  };

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    requestSequence.current += 1;
    setLatestCycle(null); setCycles([]); setGateRemoteAccount(null); setAccountError(''); setRuntimeDetail(undefined);
    setSessionState('NOT_CHECKED');
    const poll = async () => {
      // Private reads can take longer than five seconds. Never overlap polls.
      pollBusy.current = true;
      try { await fetchActiveAuthAndSession(); }
      finally { pollBusy.current = false; if (!cancelled) timer = setTimeout(poll, 5000); }
    };
    void poll();
    return () => { cancelled = true; clearTimeout(timer); requestSequence.current += 1; };
  }, [selectedAccount]);

  const handleSessionAction = async (action: 'start' | 'pause' | 'resume' | 'terminate') => {
    setSessionActionLoading(true);
    setDiagMessage(null);
    try {
      const endpoint = `/ai-session/${action}?account_id=${encodeURIComponent(selectedAccount)}`;
      const mayNeedLeaseHandoff = action === 'start' || action === 'resume';
      let completed = false;
      for (let attempt = 0; attempt < (mayNeedLeaseHandoff ? 12 : 1); attempt += 1) {
        try {
          await apiClient.v2(endpoint, 'POST', undefined);
          completed = true;
          break;
        } catch (error) {
          const message = errorMessage(error, '');
          const leaseConflict = error instanceof ApiError
            ? error.status === 409 && (error.code.toLowerCase().includes('lease') || message.toLowerCase().includes('lease'))
            : message.toLowerCase().includes('lease');
          if (!leaseConflict || !mayNeedLeaseHandoff || attempt === 11) throw error;
          setDiagMessage(`正在接管上一实例的安全锁… ${attempt + 1}/12`);
          await sleep(1000);
        }
      }
      if (!completed) throw new Error('AI 启动未完成');
      setDiagMessage(action === 'start' || action === 'resume' ? 'AI 交易线程已启动，等待下一个策略扫描点。' : null);
      if (!pollBusy.current) await fetchActiveAuthAndSession();
      if (onRefresh) onRefresh();
    } catch (err: unknown) {
      const message = errorMessage(err, `AI session ${action} failed`);
      setDiagMessage(message.toLowerCase().includes('lease')
        ? '另一个桌面实例仍在运行并持有交易安全锁。请切换到已打开的窗口；本窗口没有重复启动交易线程。'
        : message);
    } finally {
      setSessionActionLoading(false);
    }
  };

  const handleExportDiag = async () => {
    setExportingDiag(true);
    setDiagMessage(null);
    try {
      const data = await apiClient.v2<DiagnosticsResponse>('/diagnostics/export', 'POST', { output_dir: 'artifacts/diagnostics' });
      setDiagMessage(`Exported: ${data.archive_path || 'UNKNOWN'} · Secret scan: ${data.secret_scan || 'NOT_VERIFIED'}`);
    } catch (err: unknown) {
      setDiagMessage(errorMessage(err, 'Export error'));
    } finally {
      setExportingDiag(false);
    }
  };

  const maskAccount = (acc: string) => {
    if (!acc || acc.length <= 6) return acc || '***';
    return `${acc.slice(0, 3)}***${acc.slice(-3)}`;
  };


  const displayMode = selectedMode || 'UNSCOPED';
  const remoteStatus = String(gateRemoteAccount?.data_status || gateRemoteAccount?.capability_status || 'UNKNOWN');
  const remoteIsAvailable = remoteStatus === 'AVAILABLE';
  const latestAction = String(latestCycle?.action || (sessionState === 'RUNNING' ? 'SCANNING' : 'IDLE')).toUpperCase();
  const isBlocked = latestCycle?.status === 'BLOCKED' || latestAction.includes('BLOCK');
  const opportunityScore = isBlocked ? null : decisionMetric(latestCycle, 'trigger_completion_pct');
  const modelConfidence = decisionMetric(latestCycle, 'confidence');
  const actionTone = latestAction.includes('LONG')
    ? 'long'
    : latestAction.includes('SHORT')
    ? 'short'
    : isBlocked
    ? 'blocked'
    : 'neutral';
  const scanInterval = runtimeDetail?.schedule?.interval_minutes;
  const scanIntervalLabel = scanInterval != null ? `${scanInterval} 分钟` : '未报告';
  const nextScan = runtimeDetail?.schedule?.next_scan_at ? formatTradingTime(runtimeDetail.schedule.next_scan_at) : '等待调度';
  const strategyName = runtimeDetail?.schedule?.strategy_name || runtimeDetail?.schedule?.strategy_id || '策略信息未报告';
  const modelCallLabel = latestCycle
    ? latestCycle.model_called ? '最近一轮已调用' : '最近一轮未调用'
    : sessionState === 'RUNNING' ? '等待首轮扫描' : '暂无调用记录';
  const modelCallTone = latestCycle?.model_called ? 'active' : latestCycle ? 'warning' : 'muted';
  const modelHealthFresh = modelHealthFreshUntil(modelHealth?.checked_at) !== null;
  const modelIdentityEvidenceValid = Boolean(
    modelHealth?.available === true &&
    modelHealth.model_available === true &&
    isBonsaiIdentity(modelHealth.model_id || modelHealth.required_model) &&
    isBonsaiIdentity(modelHealth.actual_model_id) &&
    modelHealth.model_identity_source === 'verified_manifest',
  );
  const verifiedBonsai = modelIdentityEvidenceValid && modelHealthFresh;
  const modelHealthStale = modelIdentityEvidenceValid && !modelHealthFresh;
  const modelIdentityLabel = verifiedBonsai
    ? 'Bonsai 2.27B · 身份已核验'
    : modelHealthStale
      ? 'Model identity check expired'
      : modelHealth?.available === false || modelHealth?.model_available === false
        ? `模型不可用${modelHealth?.error_code ? ` · ${modelHealth.error_code}` : ''}`
        : modelHealthChecked
          ? 'Model identity unverified'
          : '正在核验模型身份';

  return (
    <LocalizedSurface><div className="ai-cockpit" data-state={sessionState.toLowerCase()}>
      <div className="ai-cockpit__topline">
        <div className="ai-cockpit__identity">
          <span className="ai-cockpit__eyebrow">AI AUTONOMOUS TRADING / RUNTIME HEALTH</span>
          <h2>AI 交易指挥舱</h2>
          <div className="ai-cockpit__chips">
            <span data-tone={displayMode === 'LIVE' ? 'short' : 'active'}>{displayMode === 'LIVE' ? 'LIVE · LOCKED' : displayMode}</span>
            <span>账户 {selectedAccount ? maskAccount(selectedAccount) : '未选择'}</span>
            <span data-tone={sessionState === 'RUNNING' ? 'active' : 'muted'}>{sessionState} · GEN {generation}</span>
            <span data-tone={verifiedBonsai ? 'active' : 'warning'} aria-label="Bonsai model identity status">{modelIdentityLabel}</span>
          </div>
          {accounts.length > 1 && (
            <select
              className="ai-cockpit__account"
              aria-label="Trading account"
              value={selectedAccount}
              onChange={(event) => {
                const nextAccount = event.target.value;
                const account = accounts.find((item) => item.account_id === nextAccount);
                setSelectedAccount(nextAccount);
                setSelectedMode(String(account?.mode || '').toUpperCase());
                rememberTradingAccount(nextAccount);
                onAccountChange?.(nextAccount);
              }}
              disabled={accountsLoading || sessionActionLoading}
            >
              {accounts.map((account) => (
                <option key={account.account_id} value={account.account_id}>
                  {account.account_id} · {account.mode} · {account.venue}
                </option>
              ))}
            </select>
          )}
        </div>

        <div className="ai-cockpit__actions">
          <Link className="ai-cockpit__analysis-link" to="/ai-analysis" onClick={() => rememberTradingAccount(selectedAccount)}>查看完整分析 ↗</Link>
          {sessionState === 'RUNNING' ? (
            <button
              className="ai-cockpit__button ai-cockpit__button--pause"
              data-no-translate
              onClick={() => handleSessionAction('pause')}
              disabled={sessionActionLoading}
            >
              {chinese ? '⏸️ 暂停 AI 做单' : '⏸️ Pause AI trading'}
            </button>
          ) : sessionState === 'PAUSED' ? (
            <button
              className="ai-cockpit__button ai-cockpit__button--start"
              data-no-translate
              onClick={() => handleSessionAction('resume')}
              disabled={sessionActionLoading}
            >
              {chinese ? '▶️ 恢复 AI 做单' : '▶️ Resume AI trading'}
            </button>
          ) : (
            <button
              className="ai-cockpit__button ai-cockpit__button--start"
              data-no-translate
              onClick={() => handleSessionAction('start')}
              disabled={sessionActionLoading || accountsLoading || !selectedAccount || ['NOT_CHECKED', 'UNAVAILABLE', 'NOT_REPORTED'].includes(sessionState)}
            >
              {chinese ? '🚀 启动 AI 自动做单' : '🚀 Start AI trading'}
            </button>
          )}

          {sessionState !== 'TERMINATED' && sessionState !== 'IDLE' && (
            <button
              className="ai-cockpit__button ai-cockpit__button--stop"
              data-no-translate
              onClick={() => handleSessionAction('terminate')}
              disabled={sessionActionLoading}
            >
              {chinese ? '⏹️ 停止 AI 做单' : '⏹️ Stop AI trading'}
            </button>
          )}

          <button
            className="ai-cockpit__icon-button"
            onClick={handleExportDiag}
            disabled={exportingDiag}
          >
            {exportingDiag ? 'Exporting...' : '📦 Diagnostics'}
          </button>
          <button
            className="ai-cockpit__icon-button"
            onClick={() => setCollapsed((prev) => !prev)}
          >
            {collapsed ? '▼ 展开控制台' : '▲ 收起'}
          </button>
        </div>
      </div>

      <section className="ai-cockpit__run-rail" aria-label="策略运行摘要" data-testid="strategy-run-summary" data-no-translate data-running={sessionState === 'RUNNING'}>
        <div><span>ACTIVE STRATEGY / 当前策略</span><strong>{strategyName}</strong><small>{runtimeDetail?.schedule?.strategy_revision ? `生效版本 ${runtimeDetail.schedule.strategy_revision}` : '策略版本未报告'}</small></div>
        <div><span>SCAN CADENCE / 扫描节奏</span><strong>{scanIntervalLabel}</strong><small>{runtimeDetail?.schedule?.alignment || (scanInterval != null ? `按 ${scanInterval} 分钟边界调度` : '等待权威调度状态')}</small></div>
        <div><span>AI CANDIDATE CAP / 单轮候选上限</span><strong>{runtimeDetail?.max_symbols != null ? `${runtimeDetail.max_symbols} 个` : '未报告'}</strong><small>仅表示本轮深入分析上限</small></div>
        <div><span>MODEL CALL / 最近模型调用</span><strong data-tone={modelCallTone}>{modelCallLabel}</strong><small>{latestCycle?.timestamp ? formatTradingTime(latestCycle.timestamp) : '调用状态按最近决策记录展示'}</small></div>
      </section>

      {diagMessage && (
        <div className="ai-cockpit__notice" role="status">
          {diagMessage}
        </div>
      )}
      {runtimeDetail?.dynamic_risk && runtimeDetail.dynamic_risk.entry_allowed === false && (
        <RiskLockCountdown blockedUntil={runtimeDetail.dynamic_risk.blocked_until} reasons={runtimeDetail.dynamic_risk.reasons} />
      )}

      {!collapsed && (
        <>
      <section className="ai-command-deck" aria-label="AI execution overview" data-no-translate>
        <div className="ai-decision-core" data-tone={actionTone} data-testid="market-readiness" style={{ '--score': `${opportunityScore ?? 0}` } as React.CSSProperties}>
          <div className="ai-decision-core__orbit"><span /><span /><span /></div>
          <div className="ai-decision-core__center">
            <small>策略触发完成度</small>
            <strong>{opportunityScore == null ? '—' : Math.round(opportunityScore)}</strong>
            <span>
              {isBlocked
                ? (latestCycle?.block_reason ? `系统阻断 (${latestCycle.block_reason})` : '系统阻断中 · 暂无评分')
                : opportunityScore == null
                ? '暂无可信触发度'
                : latestAction === '观望' || latestAction === 'WAIT'
                ? '/ 100 · 环境就绪(等待点位)'
                : '/ 100 · 满足进场阈值'}
            </span>
            {latestCycle?.timestamp && (
              <span style={{ fontSize: '10px', opacity: 0.65, marginTop: '2px' }}>
                周期 {formatTradingTime(latestCycle.timestamp)}
              </span>
            )}
          </div>
        </div>
        <div className="ai-command-deck__decision">
          <span className="ai-command-deck__label">LATEST DECISION / 最新决策</span>
          <div className="ai-command-deck__action" data-tone={actionTone}>{latestAction}</div>
          <h3>{decisionInstrument(latestCycle)}</h3>
          <p>{latestCycle?.human_message || latestCycle?.reason || '等待下一个策略扫描点，系统会自动比较全市场候选。'}</p>
          {modelConfidence != null && <small className="ai-command-deck__confidence">AI 置信度 {Math.round(modelConfidence)} / 100 · 不代表胜率</small>}
          <div className="ai-command-deck__pulse"><i /><span>下一轮 {nextScan}</span></div>
        </div>
        <div className="ai-command-deck__telemetry">
          <div><span>EXECUTION ACCOUNT</span><strong>{selectedAccount || 'UNSCOPED'}</strong><small>{remoteIsAvailable ? 'Gate 远端事实在线' : remoteStatus}</small></div>
          <div><span>SESSION STATE</span><strong data-tone={sessionState === 'RUNNING' ? 'active' : 'muted'}>{sessionState}</strong><small>策略周期 {scanIntervalLabel}</small></div>
          <div><span>PROTECTED POSITIONS</span><strong>{protectionCount == null ? 'UNKNOWN' : `${protectionCount} Active`}</strong><small>独立风控守护</small></div>
          <div><span>MODEL CALL</span><strong data-tone={modelCallTone}>{modelCallLabel}</strong><small>模型调用不代表风控通过或订单成交</small></div>
        </div>
      </section>

      <section className="v2-ai-remote-card" data-testid="ai-remote-account-card" aria-label="Gate TestNet remote account truth">
        <div className="v2-ai-section-header">
          <div>
            <h3>Gate TestNet 账户事实</h3>
            <p>权益、保证金、持仓和待成交只显示 Gate 远端回执；本地仅保留远端快照审计缓存，不作为账户事实。</p>
          </div>
          <span className={`v2-badge ${remoteIsAvailable ? 'v2-badge--bull' : remoteStatus.includes('NOT_CONFIGURED') ? 'v2-badge--warning' : 'v2-badge--neutral'}`}>
            {remoteStatus}
          </span>
        </div>
        <div className="v2-ai-remote-grid">
          <div><span>账户权益</span><strong>{displayNumber(gateRemoteAccount?.equity, ' USDT')}</strong><small>{gateRemoteAccount?.equity_basis || 'UNKNOWN_BASIS'}</small></div>
          <div><span>可用保证金</span><strong>{displayNumber(gateRemoteAccount?.available_margin, ' USDT')}</strong><small>{gateRemoteAccount?.available_margin_basis || 'UNKNOWN_BASIS'}</small></div>
          <div><span>已用保证金</span><strong>{displayNumber(gateRemoteAccount?.used_margin, ' USDT')}</strong><small>{gateRemoteAccount?.used_margin_basis || 'UNKNOWN_BASIS'}</small></div>
          <div><span>远端持仓 / 待单</span><strong>{remoteIsAvailable ? `${gateRemoteAccount?.positions?.length ?? 0} / ${gateRemoteAccount?.pending_orders?.length ?? 0}` : 'UNKNOWN'}</strong></div>
        </div>
        <div className="v2-ai-remote-meta">
          <span>来源：{gateRemoteAccount?.source || 'NOT_OBSERVED'}</span>
          <span>观测时间：{gateRemoteAccount?.observed_at || 'UNKNOWN'}</span>
          {gateRemoteAccount?.error_code ? <span className="v2-ai-error-text">原因：{gateRemoteAccount.error_code}</span> : null}
        </div>
      {accountError && <p className="ai-cycle-error" data-no-translate>账户读取未完成：{accountError} · <Link to="/gate-live">检查 TestNet API 连接</Link></p>}
      </section>

      <section className="v2-ai-cycle-card" data-testid="ai-decision-cycle-card" data-no-translate>
        <div className="v2-ai-section-header">
          <div>
            <h3>新闻与技术面 AI 决策</h3>
            <p>按 {scanIntervalLabel} 扫描：K 线与新闻 → AI 自拟策略 → JSON 决策 → 固定风控 → 开仓或等待。启动后等待下一个策略时间点；每轮最多一个动作，可向当前账户自动提交订单。</p>
            <p>单笔风险上限 0.25% · 组合风险上限 1% · 日亏损熔断 1.5% · 金额与杠杆按生效策略执行 · 扣费后盈亏比至少 2。AI 置信分数不代表胜率。</p>
          </div>
          <span className={`v2-badge ${sessionState === 'RUNNING' ? 'v2-badge--bull' : 'v2-badge--neutral'}`}>
            {sessionState}
          </span>
        </div>
        {latestCycle ? (
          <>
            <div className="v2-ai-cycle-summary">
              <div><span>本轮结果</span><strong>{latestCycle.action || 'UNKNOWN'}</strong></div>
              <div><span>决策来源</span><strong data-testid="cycle-decision-origin">{latestCycle.decision_origin === 'MODEL' ? 'AI 决策' : latestCycle.decision_origin === 'RISK' ? '风控处理' : latestCycle.action === 'SYSTEM_BLOCKED' || (latestCycle.status === 'BLOCKED' && latestCycle.model_called === false) ? '系统阻断 · 未调用模型' : latestCycle.decision_origin === 'RULE' ? '规则评估' : '系统检查'}</strong></div>
              <div><span>模型调用</span><strong>{latestCycle.model_called ? '已调用' : '未调用'}</strong></div>
              <div><span>时间</span><strong>{formatTradingTime(latestCycle.timestamp)}</strong></div>
            </div>
            <p>{latestCycle.human_message || latestCycle.reason}</p>
            <details className="v2-ai-raw-details">
              <summary>策略、新闻、技术面与执行 JSON</summary>
              <pre>{JSON.stringify(latestCycle.payload || latestCycle, null, 2)}</pre>
            </details>
          </>
        ) : <p className="v2-ai-muted">当前账户暂无决策记录。启动 AI 自动做单后，会在下一个策略时间点分析并记录开仓、等待或具体阻断原因。</p>}
      </section>

      <AIDecisionHistory cycles={cycles} runtime={runtimeDetail} />

      {/* Durable plan contract: show the exact stored conditions and the
          consumer-visible execution state.  Missing evidence stays UNKNOWN. */}
      {tradePlans.length > 0 && <div
        aria-label="Trade plans / 交易计划"
        data-testid="trade-plans-panel"
        style={{
          backgroundColor: '#161b22',
          padding: '12px 16px',
          borderRadius: 6,
          border: '1px solid #21262d',
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, marginBottom: 8 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: '#58a6ff' }}>Trade Plans / 交易计划</div>
          <div style={{ fontSize: 11, color: '#8b949e' }}>Scope: {selectedAccount || 'UNKNOWN'} · {tradePlansState}</div>
        </div>
        {tradePlans.length === 0 ? (
          <div style={{ color: '#8b949e', fontSize: 12 }}>
            No durable plan is available / 暂无可核验的持久化计划。状态：{tradePlansState || 'UNKNOWN'}
          </div>
        ) : (
          <div style={{ display: 'grid', gap: 8 }}>
            {tradePlans.slice(0, 5).map((record) => {
              const plan = record.plan || {};
              const execution = record.execution_result || {};
              const condition = execution.condition_evaluation || {};
              const status = record.status || execution.status || 'UNKNOWN';
              const behavior = [
                plan.reduce_fraction !== undefined ? `reduce ${plan.reduce_fraction}` : null,
                plan.reduce_quantity !== undefined ? `qty ${plan.reduce_quantity}` : null,
                plan.leverage !== undefined ? `leverage ${plan.leverage}` : null,
                plan.stop_loss !== undefined ? `stop ${plan.stop_loss}` : null,
                plan.take_profit !== undefined ? `tp ${plan.take_profit}` : null,
                plan.entry_expires_at ? `entry expires ${plan.entry_expires_at}` : null,
                plan.time_exit_at ? `time exit ${plan.time_exit_at}` : null,
                plan.abandon_chase_condition ? `abandon ${plan.abandon_chase_condition}` : null,
                Array.isArray(plan.partial_take_profits) && plan.partial_take_profits.length
                  ? `partials ${plan.partial_take_profits.length}`
                  : null,
                plan.trailing_protection ? `trailing ${plan.trailing_protection.type || 'UNKNOWN'}` : null,
              ].filter(Boolean).join(' · ') || 'UNKNOWN';
              return (
                <div key={record.plan_id || `${plan.symbol || 'UNKNOWN'}-${plan.action || 'UNKNOWN'}`} style={{ borderTop: '1px solid #30363d', paddingTop: 8 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, flexWrap: 'wrap', fontSize: 12 }}>
                    <span style={{ color: '#c9d1d9', fontWeight: 600 }}>
                      {plan.symbol || 'UNKNOWN'} · {plan.action || 'UNKNOWN'} · {record.plan_id || 'UNKNOWN'}
                    </span>
                    <span style={{ color: status === 'EXECUTED' ? '#3fb950' : status === 'SUBMITTED' || status === 'WAITING_TRIGGER' ? '#d29922' : '#f0883e' }}>
                      {status === 'EXECUTED' ? '已成交' : status === 'SUBMITTED' ? '已提交待成交' : status}
                    </span>
                  </div>
                  <div style={{ color: '#8b949e', fontSize: 11, marginTop: 4 }}>
                    Trigger / 触发: {plan.entry_trigger || 'UNKNOWN'} · Condition / 条件: {condition.status || plan.conditions?.entry?.type || 'UNKNOWN'}
                  </div>
                  <div style={{ color: '#8b949e', fontSize: 11, marginTop: 3 }}>
                    Behavior / 行为: {behavior} · Evidence / 证据: {Array.isArray(plan.evidence) && plan.evidence.length ? `${plan.evidence.length} recorded` : 'UNKNOWN'}
                  </div>
                  {plan.news_revision_ids?.length ? (
                    <div style={{ color: '#d29922', fontSize: 11, marginTop: 3 }}>
                      News gate / 新闻门槛: {execution.news_evidence?.status || 'UNKNOWN'} · revision {plan.news_revision_ids.join(', ')}
                    </div>
                  ) : null}
                </div>
              );
            })}
          </div>
        )}
      </div>}
      </>
      )}

    </div></LocalizedSurface>
  );
};
