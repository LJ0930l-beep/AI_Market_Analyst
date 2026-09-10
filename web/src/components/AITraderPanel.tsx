import React, { useState, useEffect } from 'react';
import { AuthorizationWizardModal } from './AuthorizationWizardModal';
import { apiClient } from '../api/client';
import { LocalizedSurface } from '../i18n';
import { Link } from 'react-router-dom';
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

interface AuthorizationRecord {
  authorization_id: string;
  status?: string;
  expires_at?: string;
}

interface AuthorizationResponse {
  authorization?: AuthorizationRecord | null;
}

interface SessionRecord {
  state?: string;
  generation?: number;
}

interface SessionStatusResponse {
  session?: SessionRecord | null;
  latest_cycle?: CycleRecord | null;
  latest_model_cycle?: CycleRecord | null;
  latest_system_event?: CycleRecord | null;
  protection_summary?: { active_positions?: number | null };
  last_market_event_at?: string | null;
  market_freshness?: { status?: string };
  model_status?: unknown;
}

interface CycleRecord {
  cycle_id?: string;
  session_id?: string;
  generation?: number;
  latency_ms?: number;
  timestamp?: string;
  action?: string;
  reason?: string;
  rejection_code?: string;
  decision_origin?: 'MODEL' | 'SYSTEM' | string;
  operational_state?: string;
  model_called?: boolean;
  model_result?: string;
  block_stage?: string | null;
  human_message?: string | null;
  model_id?: string | null;
  stage_trace?: StageTraceRecord[];
}

interface StageTraceRecord {
  stage?: string;
  status?: string;
  started_at?: string | null;
  finished_at?: string | null;
  duration_ms?: number | null;
  reason_code?: string | null;
  human_message?: string | null;
  evidence?: Record<string, unknown>;
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
  available_margin?: number | null;
  used_margin?: number | null;
  unrealized_pnl?: number | null;
  realized_pnl?: number | null;
  balance?: { total?: number | null; free?: number | null; used?: number | null };
  positions?: unknown[];
  pending_orders?: unknown[];
  fills?: unknown[];
  error_code?: string | null;
}

interface StrategyCandidateRecord {
  candidate_id?: string;
  symbol?: string;
  strategy_id?: string;
  strategy_version?: string;
  status?: string;
  side?: string | null;
  entry_price?: number | null;
  stop_price?: number | null;
  take_profit?: number | null;
  rule_score?: number | null;
  calibrated_probability?: number | null;
  calibration_sample_size?: number | null;
  rationale?: string | null;
  conditions?: unknown[];
  trigger_completion_pct?: number | null;
  entry_zone?: Record<string, unknown> | null;
  invalidation?: string | null;
  targets?: unknown[];
  rr?: number | null;
  evidence?: unknown[];
  signal_time?: string | null;
  expires_at?: string | null;
  context_timeframe?: Record<string, unknown> | null;
  market_regime?: string | null;
  direction_bias?: string | null;
  trigger_status?: string | null;
}

interface DiagnosticsResponse {
  archive_path?: string;
  secret_scan?: string;
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

function stageLabel(stage: string | undefined): string {
  const labels: Record<string, string> = {
    ACCOUNT: '账户事实',
    AUTHORIZATION: '授权范围',
    MARKET_DATA: '行情数据',
    NEWS_EVENTS: '新闻与事件',
    KLINE_CONTEXT: 'K线上下文',
    STRATEGY_SCAN: '策略扫描',
    AI_MODEL: 'Qwen3.5-9B 判断',
    RISK: '风险引擎',
    EXECUTION: 'Gate TestNet 执行',
    RECONCILIATION: '远端对账',
  };
  return labels[String(stage || '')] || String(stage || 'UNKNOWN');
}

export const AITraderPanel: React.FC<AITraderPanelProps> = ({
  currentMode,
  activeAccount,
  onRefresh,
  onAccountChange,
}) => {
  const [accounts, setAccounts] = useState<TradingAccountSummary[]>([]);
  const [selectedAccount, setSelectedAccount] = useState<string>(canonicalAccountId(activeAccount));
  const [selectedMode, setSelectedMode] = useState<string>((currentMode || '').trim().toUpperCase());
  const [accountsLoading, setAccountsLoading] = useState(true);
  const [isWizardOpen, setIsWizardOpen] = useState(false);
  const [activeAuth, setActiveAuth] = useState<AuthorizationRecord | null>(null);
  const [loading, setLoading] = useState(false);
  const [sessionActionLoading, setSessionActionLoading] = useState(false);
  const [exportingDiag, setExportingDiag] = useState(false);
  const [diagMessage, setDiagMessage] = useState<string | null>(null);
  const [recentCycle, setRecentCycle] = useState<CycleRecord | null>(null);
  const [sessionState, setSessionState] = useState<string>('NOT_CHECKED');
  const [generation, setGeneration] = useState<number>(0);
  const [modelStatus, setModelStatus] = useState<unknown>('NOT_CHECKED');
  // A missing status is not an empty protection set.  Keep it UNKNOWN until
  // the scoped runtime reports an authoritative count.
  const [protectionCount, setProtectionCount] = useState<number | null>(null);
  const [marketFreshness, setMarketFreshness] = useState<string>('NOT_CHECKED');
  const [tradePlans, setTradePlans] = useState<TradePlanRecord[]>([]);
  const [tradePlansState, setTradePlansState] = useState<string>('NOT_CHECKED');
  const [gateRemoteAccount, setGateRemoteAccount] = useState<GateRemoteAccount | null>(null);
  const [strategyCandidates, setStrategyCandidates] = useState<StrategyCandidateRecord[]>([]);
  const [candidateState, setCandidateState] = useState<string>('NOT_CHECKED');

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
        const selected = preferred || nextAccounts[0];
        setSelectedAccount(selected?.account_id || '');
        setSelectedMode(String(selected?.mode || currentMode || '').toUpperCase());
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
  }, [activeAccount, currentMode]);

  const fetchActiveAuthAndSession = async () => {
    const accountId = selectedAccount.trim();
    if (!accountId) {
      setActiveAuth(null);
      setRecentCycle(null);
      setSessionState('UNSCOPED');
      setGeneration(0);
      setProtectionCount(null);
      setModelStatus('NOT_CHECKED');
      setMarketFreshness('NOT_CHECKED');
      setTradePlans([]);
      setTradePlansState('UNSCOPED');
      setGateRemoteAccount(null);
      setStrategyCandidates([]);
      setCandidateState('UNSCOPED');
      return;
    }
    try {
      const data = await apiClient.v2<AuthorizationResponse>(
        `/trading-authorizations/active?account_id=${encodeURIComponent(accountId)}`,
        'GET',
        undefined,
      );
      setActiveAuth(data.authorization ?? null);

      const status = await apiClient.v2<SessionStatusResponse>(
        `/ai-session/status?account_id=${encodeURIComponent(accountId)}`,
        'GET',
        undefined,
      );
      if (status.session) {
        setSessionState(typeof status.session.state === 'string' && status.session.state ? status.session.state : 'NOT_REPORTED');
        setGeneration(Number.isFinite(Number(status.session.generation)) ? Number(status.session.generation) : 0);
      } else {
        setSessionState('NOT_REPORTED');
        setGeneration(0);
      }
      setRecentCycle(status.latest_cycle ?? status.latest_system_event ?? status.latest_model_cycle ?? null);
      const reportedProtectionCount = status.protection_summary?.active_positions;
      setProtectionCount(
        Number.isFinite(Number(reportedProtectionCount)) && Number(reportedProtectionCount) >= 0
          ? Number(reportedProtectionCount)
          : null,
      );
      setMarketFreshness(typeof status.market_freshness?.status === 'string' && status.market_freshness.status ? status.market_freshness.status : 'NOT_REPORTED');
      setModelStatus(status.model_status ?? 'NOT_REPORTED');

      const plans = await apiClient.v2<{ plans?: TradePlanRecord[] }>(
        `/trade-plans?account_id=${encodeURIComponent(accountId)}&limit=20`,
        'GET',
        undefined,
      );
      const scopedPlans = Array.isArray(plans.plans) ? plans.plans : [];
      setTradePlans(scopedPlans);
      setTradePlansState(scopedPlans.length ? 'DURABLE' : 'NO_DURABLE_PLANS');

      const [remoteAccount, candidateResponse] = await Promise.all([
        apiClient.v2<GateRemoteAccount>(
          `/gate/account?account_id=${encodeURIComponent(accountId)}`,
          'GET',
          undefined,
        ).catch(() => null),
        apiClient.v2<{ candidates?: StrategyCandidateRecord[] }>(
          `/ai-session/candidates?account_id=${encodeURIComponent(accountId)}&limit=12`,
          'GET',
          undefined,
        ).catch(() => null),
      ]);
      setGateRemoteAccount(remoteAccount);
      const candidates = Array.isArray(candidateResponse?.candidates) ? candidateResponse.candidates : [];
      setStrategyCandidates(candidates);
      setCandidateState(candidateResponse ? (candidates.length ? 'PERSISTED' : 'NO_CANDIDATES') : 'UNAVAILABLE');
    } catch {
      setActiveAuth(null);
      setRecentCycle(null);
      setSessionState('UNAVAILABLE');
      setGeneration(0);
      setProtectionCount(null);
      setMarketFreshness('UNAVAILABLE');
      setModelStatus('UNAVAILABLE');
      setTradePlans([]);
      setTradePlansState('UNAVAILABLE');
      setGateRemoteAccount(null);
      setStrategyCandidates([]);
      setCandidateState('UNAVAILABLE');
    }
  };

  useEffect(() => {
    fetchActiveAuthAndSession();
    const interval = setInterval(fetchActiveAuthAndSession, 5000);
    return () => clearInterval(interval);
  }, [selectedAccount]);

  const handleSessionAction = async (action: 'start' | 'pause' | 'resume' | 'terminate') => {
    if (!selectedAccount) {
      setDiagMessage('No registered trading account is available for this action.');
      return;
    }
    setSessionActionLoading(true);
    try {
      await apiClient.v2(`/ai-session/${action}?account_id=${encodeURIComponent(selectedAccount)}`, 'POST');
      await fetchActiveAuthAndSession();
      if (onRefresh) onRefresh();
    } catch (err: unknown) {
      setDiagMessage(errorMessage(err, `AI session ${action} failed`));
    } finally {
      setSessionActionLoading(false);
    }
  };

  const handleRevoke = async () => {
    if (!activeAuth) return;
    setLoading(true);
    try {
      await apiClient.v2(
        `/trading-authorizations/${activeAuth.authorization_id}/revoke?account_id=${encodeURIComponent(selectedAccount)}&reason=USER_REVOKED`,
        'POST',
      );
      await fetchActiveAuthAndSession();
      if (onRefresh) onRefresh();
    } catch (err: unknown) {
      setDiagMessage(errorMessage(err, 'Authorization revoke failed'));
    } finally {
      setLoading(false);
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

  const getRemainingTimeStr = () => {
    if (!activeAuth || !activeAuth.expires_at) return 'None';
    const expiry = new Date(activeAuth.expires_at).getTime();
    const now = Date.now();
    const diffSec = Math.max(0, Math.floor((expiry - now) / 1000));
    if (diffSec === 0) return 'EXPIRED';
    const hrs = Math.floor(diffSec / 3600);
    const mins = Math.floor((diffSec % 3600) / 60);
    return `${hrs}h ${mins}m`;
  };

  const modelStatusLabel = typeof modelStatus === 'string'
    ? modelStatus
    : modelStatus && typeof modelStatus === 'object' && 'status' in modelStatus
    ? String((modelStatus as { status?: unknown }).status || 'UNVERIFIED')
    : 'UNVERIFIED';
  const displayMode = selectedMode || 'UNSCOPED';
  const isSystemBlocked = Boolean(
    recentCycle && (
      recentCycle.decision_origin === 'SYSTEM' ||
      recentCycle.operational_state === 'SYSTEM_BLOCKED' ||
      (recentCycle.model_called === false && (recentCycle.model_result === 'NOT_RUN' || Boolean(recentCycle.block_stage)))
    ),
  );
  const cycleActionLabel = isSystemBlocked ? 'SYSTEM_BLOCKED · AI 未运行' : (recentCycle?.action || 'UNKNOWN');
  const cycleActionColor = isSystemBlocked ? '#f85149' : recentCycle?.action === 'WAIT' ? '#e3b341' : '#3fb950';
  const stageTrace = Array.isArray(recentCycle?.stage_trace) ? recentCycle.stage_trace : [];
  const remoteStatus = String(gateRemoteAccount?.data_status || gateRemoteAccount?.capability_status || 'UNKNOWN');
  const remoteIsAvailable = remoteStatus === 'AVAILABLE';

  return (
    <LocalizedSurface><div
      style={{
        backgroundColor: '#0d1117',
        border: '1px solid #30363d',
        borderRadius: 8,
        padding: '16px 20px',
        color: '#c9d1d9',
        display: 'flex',
        flexDirection: 'column',
        gap: 16,
        marginBottom: 16,
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <h2 style={{ margin: 0, fontSize: 16, color: '#58a6ff' }}>
            🤖 AI Autonomous Trader Console (N10)
          </h2>
          <span
            style={{
              padding: '2px 8px',
              borderRadius: 12,
              fontSize: 11,
              fontWeight: 600,
              backgroundColor:
                displayMode === 'LIVE' ? '#f85149' : displayMode === 'TESTNET' ? '#1f6feb' : '#238636',
              color: '#ffffff',
            }}
          >
            {displayMode === 'LIVE' ? 'LIVE (LOCKED)' : displayMode}
          </span>
          <span
            style={{
              padding: '2px 8px',
              borderRadius: 12,
              fontSize: 11,
              backgroundColor: '#21262d',
              border: '1px solid #30363d',
              color: '#8b949e',
            }}
          >
            Account: {selectedAccount ? maskAccount(selectedAccount) : 'NO REGISTERED ACCOUNT'}
          </span>
          {accounts.length > 1 && (
            <select
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
              style={{
                padding: '3px 6px',
                backgroundColor: '#0d1117',
                border: '1px solid #30363d',
                borderRadius: 4,
                color: '#c9d1d9',
                fontSize: 11,
              }}
            >
              {accounts.map((account) => (
                <option key={account.account_id} value={account.account_id}>
                  {account.account_id} · {account.mode} · {account.venue}
                </option>
              ))}
            </select>
          )}
          <span
            style={{
              padding: '2px 8px',
              borderRadius: 12,
              fontSize: 11,
              backgroundColor: sessionState === 'RUNNING' ? '#238636' : '#21262d',
              border: '1px solid #30363d',
              color: sessionState === 'RUNNING' ? '#ffffff' : '#8b949e',
              fontWeight: 600,
            }}
          >
            Session: {sessionState} (Gen {generation})
          </span>
        </div>

        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <Link to="/ai-analysis" onClick={() => rememberTradingAccount(selectedAccount)}>查看本账户做单分析</Link>
          {sessionState === 'RUNNING' ? (
            <button
              onClick={() => handleSessionAction('pause')}
              disabled={sessionActionLoading || !selectedAccount}
              style={{
                padding: '5px 10px',
                backgroundColor: '#d29922',
                border: 'none',
                borderRadius: 6,
                color: '#000000',
                fontSize: 12,
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              ⏸️ Pause AI
            </button>
          ) : sessionState === 'PAUSED' ? (
            <button
              onClick={() => handleSessionAction('resume')}
              disabled={sessionActionLoading || !selectedAccount}
              style={{
                padding: '5px 10px',
                backgroundColor: '#238636',
                border: 'none',
                borderRadius: 6,
                color: '#ffffff',
                fontSize: 12,
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              ▶️ Resume AI
            </button>
          ) : (
            <button
              onClick={() => handleSessionAction('start')}
              disabled={sessionActionLoading || !selectedAccount}
              style={{
                padding: '5px 10px',
                backgroundColor: '#238636',
                border: 'none',
                borderRadius: 6,
                color: '#ffffff',
                fontSize: 12,
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              🚀 Launch AI Session
            </button>
          )}

          {sessionState !== 'TERMINATED' && sessionState !== 'IDLE' && (
            <button
              onClick={() => handleSessionAction('terminate')}
              disabled={sessionActionLoading}
              style={{
                padding: '5px 10px',
                backgroundColor: '#da3633',
                border: 'none',
                borderRadius: 6,
                color: '#ffffff',
                fontSize: 12,
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              ⏹️ Stop
            </button>
          )}

          <button
            onClick={handleExportDiag}
            disabled={exportingDiag}
            style={{
              padding: '5px 10px',
              backgroundColor: '#21262d',
              border: '1px solid #30363d',
              borderRadius: 6,
              color: '#8b949e',
              fontSize: 12,
              cursor: 'pointer',
            }}
          >
            {exportingDiag ? 'Exporting...' : '📦 Diagnostics'}
          </button>
          <button
              onClick={() => setIsWizardOpen(true)}
              disabled={!selectedAccount || accountsLoading}
            style={{
              padding: '5px 12px',
              backgroundColor: '#1f6feb',
              border: '1px solid rgba(240, 246, 252, 0.1)',
              borderRadius: 6,
              color: '#ffffff',
              fontSize: 12,
              fontWeight: 600,
              cursor: 'pointer',
            }}
          >
            🛡️ Scope Wizard
          </button>
        </div>
      </div>

      {diagMessage && (
        <div
          style={{
            padding: '6px 12px',
            backgroundColor: 'rgba(56, 139, 253, 0.15)',
            border: '1px solid #1f6feb',
            borderRadius: 4,
            fontSize: 12,
            color: '#58a6ff',
          }}
        >
          {diagMessage}
        </div>
      )}

      {/* Status Bar */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(4, 1fr)',
          gap: 12,
          backgroundColor: '#161b22',
          padding: '12px 16px',
          borderRadius: 6,
          border: '1px solid #21262d',
        }}
      >
        <div>
          <div style={{ fontSize: 11, color: '#8b949e' }}>AUTHORIZATION STATUS</div>
          <div style={{ fontSize: 13, fontWeight: 600, marginTop: 2 }}>
            {activeAuth ? (
              <span style={{ color: activeAuth.status === 'ACTIVE' ? '#3fb950' : '#f85149' }}>
                {activeAuth.status}
              </span>
            ) : (
              <span style={{ color: '#8b949e' }}>NO ACTIVE SCOPE</span>
            )}
          </div>
        </div>

        <div>
          <div style={{ fontSize: 11, color: '#8b949e' }}>EXPIRY COUNTDOWN</div>
          <div style={{ fontSize: 13, fontWeight: 600, marginTop: 2, color: '#c9d1d9' }}>
            {getRemainingTimeStr()}
          </div>
        </div>

        <div>
          <div style={{ fontSize: 11, color: '#8b949e' }}>PROTECTED POSITIONS</div>
          <div style={{ fontSize: 13, fontWeight: 600, marginTop: 2, color: '#3fb950' }}>
            {protectionCount == null ? 'UNKNOWN' : `${protectionCount} Active`}
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end' }}>
          {activeAuth && activeAuth.status === 'ACTIVE' && (
            <button
              onClick={handleRevoke}
              disabled={loading}
              style={{
                padding: '4px 10px',
                backgroundColor: '#da3633',
                border: 'none',
                borderRadius: 4,
                color: '#ffffff',
                fontSize: 11,
                cursor: 'pointer',
              }}
            >
              Revoke Scope
            </button>
          )}
        </div>
      </div>

      <section className="v2-ai-remote-card" data-testid="ai-remote-account-card" aria-label="Gate TestNet remote account truth">
        <div className="v2-ai-section-header">
          <div>
            <h3>Gate TestNet 账户事实</h3>
            <p>权益、保证金、持仓和待成交只显示 Gate 远端回执；本地 SQLite 仅作镜像与审计。</p>
          </div>
          <span className={`v2-badge ${remoteIsAvailable ? 'v2-badge--bull' : remoteStatus.includes('NOT_CONFIGURED') ? 'v2-badge--warning' : 'v2-badge--neutral'}`}>
            {remoteStatus}
          </span>
        </div>
        <div className="v2-ai-remote-grid">
          <div><span>账户权益</span><strong>{displayNumber(gateRemoteAccount?.equity ?? gateRemoteAccount?.balance?.total, ' USDT')}</strong></div>
          <div><span>可用保证金</span><strong>{displayNumber(gateRemoteAccount?.available_margin ?? gateRemoteAccount?.balance?.free, ' USDT')}</strong></div>
          <div><span>已用保证金</span><strong>{displayNumber(gateRemoteAccount?.used_margin ?? gateRemoteAccount?.balance?.used, ' USDT')}</strong></div>
          <div><span>远端持仓 / 待单</span><strong>{remoteIsAvailable ? `${gateRemoteAccount?.positions?.length ?? 0} / ${gateRemoteAccount?.pending_orders?.length ?? 0}` : 'UNKNOWN'}</strong></div>
        </div>
        <div className="v2-ai-remote-meta">
          <span>来源：{gateRemoteAccount?.source || 'NOT_OBSERVED'}</span>
          <span>观测时间：{gateRemoteAccount?.observed_at || 'UNKNOWN'}</span>
          {gateRemoteAccount?.error_code ? <span className="v2-ai-error-text">原因：{gateRemoteAccount.error_code}</span> : null}
        </div>
      </section>

      <section className="v2-ai-candidates-card" data-testid="ai-strategy-candidates">
        <div className="v2-ai-section-header">
          <div>
            <h3>策略候选 · 可读交易条件</h3>
            <p>Python 负责事实与约束，候选只提供给 Qwen3.5-9B 做最终判断；概率缺失保持 UNKNOWN。</p>
          </div>
          <span className="v2-badge v2-badge--neutral">{candidateState}</span>
        </div>
        {strategyCandidates.length === 0 ? (
          <p className="v2-ai-muted">暂无可核验候选。缺数据时显示 UNKNOWN，不把 NO_TRIGGER 冒充为系统成功。</p>
        ) : (
          <div className="v2-ai-candidate-list">
            {strategyCandidates.slice(0, 6).map((candidate) => {
              const conditions = Array.isArray(candidate.conditions) ? candidate.conditions : [];
              const targets = Array.isArray(candidate.targets) ? candidate.targets : [];
              return (
                <article className="v2-ai-candidate" key={candidate.candidate_id || `${candidate.symbol}-${candidate.strategy_id}`}>
                  <div className="v2-ai-candidate-heading">
                    <strong>{candidate.symbol || 'UNKNOWN'} · {candidate.strategy_id || 'UNKNOWN'}</strong>
                    <span className="v2-badge v2-badge--neutral">{candidate.trigger_status || candidate.status || 'UNKNOWN'}</span>
                  </div>
                  <div className="v2-ai-candidate-facts">
                    <span>方向：{candidate.side || candidate.direction_bias || 'UNKNOWN'}</span>
                    <span>规则分：{displayNumber(candidate.rule_score)}</span>
                    <span>校准概率：{displayNumber(candidate.calibrated_probability)}</span>
                    <span>样本：{displayNumber(candidate.calibration_sample_size)}</span>
                    <span>RR：{displayNumber(candidate.rr)}</span>
                  </div>
                  <p>{candidate.rationale || '暂无文字理由。'}</p>
                  <div className="v2-ai-candidate-contract">
                    <span>入场区：{candidate.entry_zone ? JSON.stringify(candidate.entry_zone) : 'UNKNOWN'}</span>
                    <span>止损：{candidate.invalidation || displayNumber(candidate.stop_price)}</span>
                    <span>目标：{targets.length ? targets.map((target) => JSON.stringify(target)).join(' · ') : displayNumber(candidate.take_profit)}</span>
                  </div>
                  {conditions.length ? <small>条件：{conditions.slice(0, 3).map((condition) => JSON.stringify(condition)).join(' · ')}</small> : null}
                </article>
              );
            })}
          </div>
        )}
      </section>

      {/* Durable plan contract: show the exact stored conditions and the
          consumer-visible execution state.  Missing evidence stays UNKNOWN. */}
      <div
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
                    <span style={{ color: status === 'EXECUTED' ? '#3fb950' : status === 'WAITING_TRIGGER' ? '#d29922' : '#f0883e' }}>
                      {status}
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
      </div>

      {/* Recent Cycle: system blocks and model decisions are intentionally
          rendered as different operational states. */}
      {recentCycle ? (
        <section className={`v2-ai-cycle-card ${isSystemBlocked ? 'v2-ai-cycle-card--blocked' : ''}`} data-testid="ai-cycle-card">
          <div className="v2-ai-section-header">
            <div>
              <h3>最近一次 AI 运行结果 · {recentCycle.cycle_id || 'UNKNOWN'}</h3>
              <p>{recentCycle.timestamp || 'UNKNOWN'} · 延迟 {displayNumber(recentCycle.latency_ms, ' ms')}</p>
            </div>
            <span className={`v2-badge ${isSystemBlocked ? 'v2-badge--bear' : recentCycle.action === 'WAIT' ? 'v2-badge--gold' : 'v2-badge--bull'}`}>
              {cycleActionLabel}
            </span>
          </div>
          <div className="v2-ai-cycle-summary">
            <div><span>决策来源</span><strong>{isSystemBlocked ? 'SYSTEM · 系统阻断' : 'MODEL · Qwen3.5-9B'}</strong></div>
            <div><span>模型调用</span><strong>{recentCycle.model_called ? '已调用' : '未调用'}</strong></div>
            <div><span>模型结果</span><strong>{recentCycle.model_result || (isSystemBlocked ? 'NOT_RUN' : recentCycle.action || 'UNKNOWN')}</strong></div>
            <div><span>运行状态</span><strong>{recentCycle.operational_state || (isSystemBlocked ? 'SYSTEM_BLOCKED' : 'MODEL_DECISION')}</strong></div>
          </div>
          <div className="v2-ai-cycle-reason" style={{ borderColor: cycleActionColor }}>
            <span>{isSystemBlocked ? `阻断阶段：${stageLabel(recentCycle.block_stage || undefined)}` : '最终判断理由'}</span>
            <p>{recentCycle.human_message || recentCycle.reason || 'UNKNOWN'}</p>
          </div>
          {recentCycle.rejection_code && <div className="v2-ai-error-text">拒绝码：{recentCycle.rejection_code}</div>}

          <div className="v2-ai-pipeline" aria-label="AI execution pipeline">
            <div className="v2-ai-pipeline-heading">
              <strong>端到端阶段追踪</strong>
              <span>{stageTrace.length ? `${stageTrace.length} stages` : 'NOT_RECORDED'}</span>
            </div>
            {stageTrace.length ? (
              <div className="v2-ai-pipeline-grid">
                {stageTrace.map((stage, index) => {
                  const status = String(stage.status || 'UNKNOWN').toUpperCase();
                  return (
                    <div className={`v2-ai-stage v2-ai-stage--${status.toLowerCase()}`} key={`${stage.stage || 'stage'}-${index}`}>
                      <div className="v2-ai-stage-topline">
                        <span>{String(index + 1).padStart(2, '0')} · {stageLabel(stage.stage)}</span>
                        <strong>{status}</strong>
                      </div>
                      <p>{stage.human_message || stage.reason_code || '—'}</p>
                      {stage.duration_ms != null ? <small>{displayNumber(stage.duration_ms, ' ms')}</small> : null}
                    </div>
                  );
                })}
              </div>
            ) : <p className="v2-ai-muted">阶段证据尚未记录。</p>}
          </div>
          <details className="v2-ai-raw-details">
            <summary>Developer Diagnostics · Raw JSON（折叠）</summary>
            <pre>{JSON.stringify(recentCycle, null, 2)}</pre>
          </details>
        </section>
      ) : (
        <div className="v2-ai-empty-card">
          <div>
            <strong>AI 尚未运行</strong>
            <span>当前没有可核验的 autonomous cycle；这不是模型 WAIT。</span>
          </div>
          <div>行情：{marketFreshness} · 模型：{modelStatusLabel}</div>
        </div>
      )}

      <AuthorizationWizardModal
        isOpen={isWizardOpen}
        onClose={() => setIsWizardOpen(false)}
        accountId={selectedAccount}
        mode={selectedMode === 'TESTNET' || selectedMode === 'LIVE' || selectedAccount === 'gate_testnet' ? 'TESTNET' : 'PAPER'}
        venue={accounts.find((item) => item.account_id === selectedAccount)?.venue}
        onSuccess={(auth) => {
          setActiveAuth(auth);
          if (onRefresh) onRefresh();
        }}
      />
    </div></LocalizedSurface>
  );
};
