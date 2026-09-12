import React, { useState, useEffect, useRef } from 'react';
import { apiClient } from '../api/client';
import { LocalizedSurface, useI18n } from '../i18n';
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

interface SessionRecord {
  state?: string;
  generation?: number;
}

interface SessionStatusResponse {
  session?: SessionRecord | null;
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

interface AIDecisionCycle {
  cycle_id?: string;
  action?: string;
  reason?: string;
  decision_origin?: string;
  model_called?: boolean;
  timestamp?: string;
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

  const [collapsed, setCollapsed] = useState(false);

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
    try {
      const status = await apiClient.v2<SessionStatusResponse>(
        `/ai-session/status?account_id=${encodeURIComponent(accountId)}`,
        'GET',
        undefined,
      );
      if (requestId !== requestSequence.current) return;
      if (status.session) {
        setSessionState(typeof status.session.state === 'string' && status.session.state ? status.session.state : 'NOT_REPORTED');
        setGeneration(Number.isFinite(Number(status.session.generation)) ? Number(status.session.generation) : 0);
      } else {
        setSessionState('NOT_REPORTED');
        setGeneration(0);
      }
      const reportedProtectionCount = status.protection_summary?.active_positions;
      setProtectionCount(
        reportedProtectionCount != null && Number.isFinite(Number(reportedProtectionCount)) && Number(reportedProtectionCount) >= 0
          ? Number(reportedProtectionCount)
          : null,
      );

      const plans = await apiClient.v2<{ plans?: TradePlanRecord[] }>(
        `/trade-plans?account_id=${encodeURIComponent(accountId)}&limit=20`,
        'GET',
        undefined,
      );
      if (requestId !== requestSequence.current) return;
      const scopedPlans = Array.isArray(plans.plans) ? plans.plans : [];
      setTradePlans(scopedPlans);
      setTradePlansState(scopedPlans.length ? 'DURABLE' : 'NO_DURABLE_PLANS');

      const remoteAccount = await apiClient.v2<GateRemoteAccount>(
        `/gate/account?account_id=${encodeURIComponent(accountId)}`,
        'GET',
        undefined,
      ).catch(() => null);
      if (requestId !== requestSequence.current) return;
      setGateRemoteAccount(remoteAccount);
      const cycles = await apiClient.v2<{ cycles?: AIDecisionCycle[] }>(`/ai-session/cycles?account_id=${encodeURIComponent(accountId)}&limit=1`, 'GET', undefined).catch(() => null);
      if (requestId !== requestSequence.current) return;
      setLatestCycle(cycles?.cycles?.[0] || null);
    } catch {
      if (requestId !== requestSequence.current) return;
      setSessionState('UNAVAILABLE');
      setGeneration(0);
      setProtectionCount(null);
      setTradePlans([]);
      setTradePlansState('UNAVAILABLE');
      setGateRemoteAccount(null);
      setLatestCycle(null);
    }
  };

  useEffect(() => {
    setLatestCycle(null);
    setSessionState('NOT_CHECKED');
    fetchActiveAuthAndSession();
    const interval = setInterval(fetchActiveAuthAndSession, 5000);
    return () => { clearInterval(interval); requestSequence.current += 1; };
  }, [selectedAccount]);

  const handleSessionAction = async (action: 'start' | 'pause' | 'resume' | 'terminate') => {
    setSessionActionLoading(true);
    try {
      const endpoint = `/ai-session/${action}?account_id=${encodeURIComponent(selectedAccount)}`;
      await apiClient.v2(endpoint, 'POST', undefined);
      await fetchActiveAuthAndSession();
      if (onRefresh) onRefresh();
    } catch (err: unknown) {
      setDiagMessage(errorMessage(err, `AI session ${action} failed`));
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
              data-no-translate
              onClick={() => handleSessionAction('pause')}
              disabled={sessionActionLoading}
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
              {chinese ? '⏸️ 暂停 AI 做单' : '⏸️ Pause AI trading'}
            </button>
          ) : sessionState === 'PAUSED' ? (
            <button
              data-no-translate
              onClick={() => handleSessionAction('resume')}
              disabled={sessionActionLoading}
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
              {chinese ? '▶️ 恢复 AI 做单' : '▶️ Resume AI trading'}
            </button>
          ) : (
            <button
              data-no-translate
              onClick={() => handleSessionAction('start')}
              disabled={sessionActionLoading || accountsLoading || !selectedAccount || ['NOT_CHECKED', 'UNAVAILABLE', 'NOT_REPORTED'].includes(sessionState)}
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
              {chinese ? '🚀 启动 AI 自动做单' : '🚀 Start AI trading'}
            </button>
          )}

          {sessionState !== 'TERMINATED' && sessionState !== 'IDLE' && (
            <button
              data-no-translate
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
              {chinese ? '⏹️ 停止 AI 做单' : '⏹️ Stop AI trading'}
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
            onClick={() => setCollapsed((prev) => !prev)}
            style={{
              padding: '5px 10px',
              backgroundColor: '#21262d',
              border: '1px solid #388bfd',
              borderRadius: 6,
              color: '#58a6ff',
              fontSize: 12,
              cursor: 'pointer',
              fontWeight: 600,
            }}
          >
            {collapsed ? '▼ 展开控制台' : '▲ 收起'}
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

      {!collapsed && (
        <>
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
          <div style={{ fontSize: 11, color: '#8b949e' }}>EXECUTION ACCOUNT</div>
          <div style={{ fontSize: 13, fontWeight: 600, marginTop: 2 }}>
            <span style={{ color: selectedAccount ? '#3fb950' : '#8b949e' }}>{selectedAccount || 'UNSCOPED'}</span>
          </div>
        </div>

        <div>
          <div style={{ fontSize: 11, color: '#8b949e' }}>SESSION STATE</div>
          <div style={{ fontSize: 13, fontWeight: 600, marginTop: 2, color: '#c9d1d9' }}>
            {sessionState}
          </div>
        </div>

        <div>
          <div style={{ fontSize: 11, color: '#8b949e' }}>PROTECTED POSITIONS</div>
          <div style={{ fontSize: 13, fontWeight: 600, marginTop: 2, color: '#3fb950' }}>
            {protectionCount == null ? 'UNKNOWN' : `${protectionCount} Active`}
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', color: '#8b949e', fontSize: 11 }}>
          凭证、远端事实与风险引擎生效
        </div>
      </div>

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
      </section>

      <section className="v2-ai-cycle-card" data-testid="ai-decision-cycle-card" data-no-translate>
        <div className="v2-ai-section-header">
          <div>
            <h3>Qwen3.5-9B · 新闻与技术面决策</h3>
            <p>每 15 分钟：K 线与新闻 → AI 自拟策略 → JSON 决策 → 固定风控 → 开仓或等待。启动后会在下一个周期运行，可向当前所选账户自动提交订单。</p>
            <p>单笔风险上限 0.25% · 组合风险上限 1% · 日亏损熔断 1.5% · 杠杆上限 3 倍 · 扣费后盈亏比至少 2。AI 置信分数不代表胜率。</p>
          </div>
          <span className={`v2-badge ${sessionState === 'RUNNING' ? 'v2-badge--bull' : 'v2-badge--neutral'}`}>
            {sessionState}
          </span>
        </div>
        {latestCycle ? (
          <>
            <div className="v2-ai-cycle-summary">
              <div><span>本轮结果</span><strong>{latestCycle.action || 'UNKNOWN'}</strong></div>
              <div><span>决策来源</span><strong>{latestCycle.decision_origin === 'MODEL' ? 'AI 决策' : '系统检查'}</strong></div>
              <div><span>模型调用</span><strong>{latestCycle.model_called ? '已调用' : '未调用'}</strong></div>
              <div><span>时间</span><strong>{latestCycle.timestamp || 'UNKNOWN'}</strong></div>
            </div>
            <p>{latestCycle.reason}</p>
            <details className="v2-ai-raw-details" open>
              <summary>策略、新闻、技术面与执行 JSON</summary>
              <pre>{JSON.stringify(latestCycle.payload || latestCycle, null, 2)}</pre>
            </details>
          </>
        ) : <p className="v2-ai-muted">当前账户暂无决策记录。启动 AI 自动做单后，下一周期会记录开仓、等待或具体阻断原因。</p>}
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
      </>
      )}

    </div></LocalizedSurface>
  );
};
