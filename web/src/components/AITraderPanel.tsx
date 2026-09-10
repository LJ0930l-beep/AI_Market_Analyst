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
  protection_summary?: { active_positions?: number | null };
  last_market_event_at?: string | null;
  market_freshness?: { status?: string };
  model_status?: unknown;
}

interface CycleRecord {
  cycle_id?: string;
  latency_ms?: number;
  timestamp?: string;
  action?: string;
  reason?: string;
  rejection_code?: string;
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

export const AITraderPanel: React.FC<AITraderPanelProps> = ({
  currentMode,
  activeAccount,
  onRefresh,
  onAccountChange,
}) => {
  const [accounts, setAccounts] = useState<TradingAccountSummary[]>([]);
  const [selectedAccount, setSelectedAccount] = useState<string>((activeAccount || '').trim());
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
        const preferred = activeAccount && nextAccounts.find((item: TradingAccountSummary) => item.account_id === activeAccount);
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
      setRecentCycle(status.latest_cycle ?? null);
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

      {/* Recent Cycle Drill-down or NOT_RUN */}
      {recentCycle ? (
        <div
          style={{
            backgroundColor: '#161b22',
            padding: '12px 16px',
            borderRadius: 6,
            border: '1px solid #21262d',
          }}
        >
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              borderBottom: '1px solid #21262d',
              paddingBottom: 8,
              marginBottom: 8,
            }}
          >
            <div style={{ fontSize: 12, fontWeight: 600, color: '#58a6ff' }}>
              Latest Autonomous Cycle: {recentCycle.cycle_id}
            </div>
            <div style={{ fontSize: 11, color: '#8b949e' }}>
              Latency: {recentCycle.latency_ms ?? 0}ms | Time: {recentCycle.timestamp}
            </div>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '120px 1fr', gap: 10, fontSize: 12 }}>
            <span style={{ color: '#8b949e' }}>Verdict Action:</span>
            <span style={{ fontWeight: 600, color: recentCycle.action === 'WAIT' ? '#e3b341' : '#3fb950' }}>
              {recentCycle.action}
            </span>

            <span style={{ color: '#8b949e' }}>Decision Reason:</span>
            <span style={{ color: '#c9d1d9' }}>{recentCycle.reason}</span>

            {recentCycle.rejection_code && (
              <>
                <span style={{ color: '#f85149' }}>Rejection Code:</span>
                <span style={{ color: '#f85149' }}>{recentCycle.rejection_code}</span>
              </>
            )}
          </div>
        </div>
      ) : (
        <div
          style={{
            backgroundColor: '#161b22',
            padding: '12px 16px',
            borderRadius: 6,
            border: '1px dashed #30363d',
            color: '#8b949e',
            fontSize: 12,
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
          }}
        >
          <div>
            Autonomous Engine Status: <strong style={{ color: '#8b949e' }}>NOT_RUN</strong> (No autonomous cycles recorded in session database)
          </div>
          <div style={{ fontSize: 11 }}>
            Market Feed: <span style={{ color: marketFreshness === 'HEALTHY' ? '#3fb950' : '#d29922' }}>{marketFreshness}</span> | Model: <span style={{ color: '#58a6ff' }}>{modelStatusLabel}</span>
          </div>
        </div>
      )}

      <AuthorizationWizardModal
        isOpen={isWizardOpen}
        onClose={() => setIsWizardOpen(false)}
        accountId={selectedAccount}
        mode={selectedMode === 'TESTNET' || selectedMode === 'LIVE' ? selectedMode : 'PAPER'}
        venue={accounts.find((item) => item.account_id === selectedAccount)?.venue}
        onSuccess={(auth) => {
          setActiveAuth(auth);
          if (onRefresh) onRefresh();
        }}
      />
    </div></LocalizedSurface>
  );
};
