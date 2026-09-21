import { useState } from 'react';
import { formatTradingTime } from '../tradingTime';

interface Cycle {
  cycle_id?: string;
  action?: string;
  decision_origin?: string;
  model_called?: boolean;
  operational_state?: string;
  status?: string;
  block_reason?: string;
  timestamp?: string;
  reason?: string;
  human_message?: string;
  scheduled_at?: string;
  started_at?: string;
  completed_at?: string;
  duration_ms?: number;
  payload?: Record<string, unknown>;
}

interface Runtime {
  enabled?: boolean;
  last_error?: string;
  schedule?: {
    next_scan_at?: string;
    last_started_at?: string;
    last_completed_at?: string;
  };
}

type LooseRecord = Record<string, unknown>;

function asRecord(value: unknown): LooseRecord {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as LooseRecord
    : {};
}

function orderPreferenceLabel(value: unknown): string {
  const preference = String(value || '').toUpperCase();
  if (preference === 'LIMIT') return '限价单';
  if (preference === 'MARKET') return '市价单';
  return '自动选择';
}

function decisionSource(cycle: Cycle | undefined): { label: string; tone: 'model' | 'risk' | 'system' } {
  if (cycle?.decision_origin === 'MODEL') return { label: '来自模型自主决策', tone: 'model' };
  if (cycle?.decision_origin === 'RISK') return { label: '风控拦截或调整', tone: 'risk' };
  const action = String(cycle?.action || '').toUpperCase();
  const status = String(cycle?.status || '').toUpperCase();
  if (action === 'SYSTEM_BLOCKED' || (status === 'BLOCKED' && cycle?.model_called === false)) {
    return { label: '系统阻断 · 未调用模型', tone: 'risk' };
  }
  if (cycle?.decision_origin === 'RULE' || cycle?.decision_origin === 'STRATEGY') {
    return { label: '规则评估', tone: 'system' };
  }
  if (cycle?.decision_origin === 'SYSTEM') return { label: '系统检查', tone: 'system' };
  return { label: '决策来源未报告', tone: 'system' };
}

function cycleFallbackMessage(cycle: Cycle): string {
  if (cycle.model_called) return '已调用大模型完成推理';
  if (String(cycle.action || '').toUpperCase() === 'SYSTEM_BLOCKED' || String(cycle.status || '').toUpperCase() === 'BLOCKED') {
    return '系统阻断 · 本轮未调用模型';
  }
  return '系统检查';
}

export function AIDecisionHistory({
  cycles,
  runtime,
}: {
  cycles: Cycle[];
  runtime?: Runtime;
}) {
  const [expandedCycleId, setExpandedCycleId] = useState<string | null>(null);

  const recorded = cycles.find(
    (cycle) => cycle.decision_origin === 'MODEL' && cycle.payload?.strategy_plan
  );
  const plan = recorded?.payload?.strategy_plan as
    | { name?: string; thesis?: string; entry_conditions?: string[]; exit_conditions?: string[] }
    | undefined;
  const output = recorded?.payload?.model_output as
    | {
        entry_price?: number;
        stop_price?: number;
        take_profit?: number;
        instrument_id?: string;
        requested_leverage?: number;
        extra_fields?: {
          position_size_usdt?: number;
          position_sizing?: {
            notional_usdt?: number;
            estimated_margin_usdt?: number;
            estimated_loss_usdt?: number;
            leverage?: number;
            binding_limit?: string;
          };
        };
      }
    | undefined;
  const sizing = output?.extra_fields?.position_sizing;
  const config = (
    recorded?.payload?.strategy_instructions as
      | { execution?: { leverage?: number; max_notional_usdt?: number; order_preference?: string }; profile?: { order_preference?: string } }
      | undefined
  )?.execution;

  const recordedInstructions = asRecord(recorded?.payload?.strategy_instructions);
  const recordedProfile = asRecord(recordedInstructions.profile);
  const recordedOutput = asRecord(recorded?.payload?.model_output);
  const recordedAction = String(recorded?.action || recordedOutput.action || '').toUpperCase();
  const isOpeningAction = recordedAction === 'OPEN_LONG' || recordedAction === 'OPEN_SHORT';
  const configuredPreference = String(
    recordedProfile.order_preference || config?.order_preference || 'AUTO',
  ).toUpperCase();
  const effectivePreference = isOpeningAction
    ? String(recordedOutput.order_preference || configuredPreference).toUpperCase()
    : configuredPreference;
  const limitPrice = recordedOutput.limit_price;
  const entryPrice = recordedOutput.entry_price;

  const inProgress = Boolean(
    runtime?.schedule?.last_started_at &&
      (!runtime.schedule.last_completed_at ||
        runtime.schedule.last_started_at > runtime.schedule.last_completed_at)
  );

  const latestCycle = cycles[0];
  const currentAction = latestCycle?.action || recorded?.action || 'WAIT';
  const latestSource = decisionSource(latestCycle);
  const isBuy = currentAction.includes('BUY') || currentAction.includes('LONG');
  const isSell = currentAction.includes('SELL') || currentAction.includes('SHORT');
  const resolveStopPrice = (outputPrice?: number, planObj?: LooseRecord) => {
    if (outputPrice !== undefined && outputPrice !== null && !isNaN(Number(outputPrice)) && Number(outputPrice) > 0) {
      return Number(outputPrice).toFixed(2);
    }
    const stopLoss = planObj?.stop_loss;
    if (stopLoss !== undefined && stopLoss !== null && !isNaN(Number(stopLoss)) && Number(stopLoss) > 0) {
      return Number(stopLoss).toFixed(2);
    }
    const invalidation = planObj?.invalidation;
    if (invalidation !== undefined && invalidation !== null && !isNaN(Number(invalidation)) && Number(invalidation) > 0) {
      return Number(invalidation).toFixed(2);
    }
    const exitConds = Array.isArray(planObj?.exit_conditions) ? planObj.exit_conditions.join(' ') : '';
    const text = `${exitConds} ${typeof invalidation === 'string' ? invalidation : ''}`;
    const match = text.match(/(?:support\d*|resistance\d*|stop|低于|高于|跌破|突破|止损)[\D]*?([\d]+(?:\.[\d]+)?)/i);
    if (match && match[1] && !isNaN(Number(match[1]))) {
      return Number(match[1]).toFixed(2);
    }
    return '结构防线+推损';
  };

  return (
    <section data-no-translate aria-label="AI 交易计划与时间线" className="terminal-panel ai-decision-panel">
      {/* Panel Header */}
      <header className="v2-panel-header">
        <div className="v2-panel-header-title">
          <h2>⚡ AI 交易计划与决策时间线</h2>
          <span
            className={`v2-badge ${
              runtime?.enabled
                ? inProgress
                  ? 'v2-badge--gold'
                  : 'v2-badge--bull'
                : 'v2-badge--neutral'
            }`}
          >
            {runtime?.enabled
              ? inProgress
                ? '● 分析推理中'
                : '● 调度就绪'
              : '○ 线程未启动'}
          </span>
        </div>
        <span className="v2-data-tag">已记录 {cycles.length} 次执行周期</span>
      </header>

      {/* 3-Factor Fixed Overview Banner (Similar to Macro 3-factor grid) */}
      <div className="ai-factors-grid">
        {/* Factor 1: Current Phase & Next Round */}
        <div className="ai-factor-card">
          <span className="ai-factor-label">调度阶段 · 下一轮</span>
          <strong className="ai-factor-val">
            {runtime?.enabled
              ? inProgress
                ? '同步数据 / 推理中'
                : '待调度'
              : '未启动'}
          </strong>
          <small className="ai-factor-sub">
            {runtime?.schedule?.next_scan_at
              ? formatTradingTime(runtime.schedule.next_scan_at)
              : '等待策略引擎'}
          </small>
        </div>

        {/* Factor 2: Latest Policy & Action */}
        <div className="ai-factor-card">
          <span className="ai-factor-label">最新提案 · 动作</span>
          <div className="ai-factor-action-row">
            <span
              className={`ai-action-badge ${
                isBuy
                  ? 'ai-action-badge--buy'
                  : isSell
                  ? 'ai-action-badge--sell'
                  : 'ai-action-badge--wait'
              }`}
            >
              {currentAction}
            </span>
            <span className="ai-factor-strategy-name">
              {plan?.name || (recorded?.payload?.strategy_name as string) || 'EMA_Trend_Reversal'}
            </span>
          </div>
          <small className="ai-factor-sub">
            {latestSource.label}
          </small>
        </div>

        {/* Factor 3: Risk & Position Constraints */}
        <div className="ai-factor-card">
          <span className="ai-factor-label">开仓杠杆 · 名义上限</span>
          <strong className="ai-factor-val">
            {sizing?.leverage ?? output?.requested_leverage ?? config?.leverage ?? '10'}x
            <span className="ai-factor-val-note">
              / {sizing?.notional_usdt ?? config?.max_notional_usdt ?? '500'} U
            </span>
          </strong>
          <small className="ai-factor-sub">
            {sizing?.binding_limit || '严格执行单笔风控上限'}
          </small>
        </div>
      </div>

      {runtime?.last_error && (
        <p role="alert" className="v2-warning" style={{ margin: '10px 0' }}>
          {runtime.last_error}
        </p>
      )}

      {/* Plan Card (if available) */}
      {plan && (
        <div className="ai-active-plan-card">
          <div className="ai-active-plan-head">
            <div className="ai-active-plan-title">
              <span className="v2-badge v2-badge--gold">AI 执行方案</span>
              <strong>{plan.name}</strong>
              <span className="ai-plan-action-tag">目标: {recorded?.action}</span>
              <span className="ai-plan-order-tag">
                {isOpeningAction
                  ? `执行：${orderPreferenceLabel(effectivePreference)}`
                  : `本轮无订单 · 策略偏好：${orderPreferenceLabel(effectivePreference)}`}
              </span>
            </div>
            <time className="v2-news-time">{formatTradingTime(recorded?.timestamp)}</time>
          </div>

          {plan.thesis && <p className="ai-plan-thesis">{plan.thesis}</p>}

          <div className="ai-plan-price-grid">
            <div className="ai-plan-price-col">
              <span className="ai-price-label">{isOpeningAction ? (effectivePreference === 'LIMIT' ? '计划限价' : '计划入场') : '本轮入场'}</span>
              <strong className="ai-price-val ai-price-val--entry">
                {!isOpeningAction
                  ? '本轮未生成订单'
                  : effectivePreference === 'LIMIT'
                  ? (limitPrice ? Number(limitPrice).toFixed(2) : '限价位未生成（已拦截）')
                  : (entryPrice ? Number(entryPrice).toFixed(2) : '市场报价待确认')}
              </strong>
            </div>
            <div className="ai-plan-price-col">
              <span className="ai-price-label">安全止损</span>
              <strong className="ai-price-val ai-price-val--stop">
                {resolveStopPrice(output?.stop_price, plan)}
              </strong>
            </div>
            <div className="ai-plan-price-col">
              <span className="ai-price-label">目标止盈</span>
              <strong className="ai-price-val ai-price-val--tp">
                {output?.take_profit ? Number(output.take_profit).toFixed(2) : '动态跟踪'}
              </strong>
            </div>
            <div className="ai-plan-price-col">
              <span className="ai-price-label">预估保证金</span>
              <strong className="ai-price-val">
                {sizing?.estimated_margin_usdt ? `${sizing.estimated_margin_usdt.toFixed(2)} U` : '待成交计算'}
              </strong>
            </div>
          </div>

          <div className="ai-plan-cond-row" style={{ color: '#34d399' }}>
            <span className="ai-cond-tag" style={{ color: '#34d399' }}>🛡️ 移动止损:</span>
            <span>已启用机构级追踪（浮盈 +1.0% 自动激活 · 回撤 1.2% 锁利出场 · 严防回吐）</span>
          </div>

          {plan.entry_conditions && plan.entry_conditions.length > 0 && (
            <div className="ai-plan-cond-row">
              <span className="ai-cond-tag">入场判定:</span>
              <span>{plan.entry_conditions.join('； ')}</span>
            </div>
          )}

          {plan.exit_conditions && plan.exit_conditions.length > 0 && (
            <div className="ai-plan-cond-row">
              <span className="ai-cond-tag">退出/失效:</span>
              <span>{plan.exit_conditions.join('； ')}</span>
            </div>
          )}
        </div>
      )}

      {/* Scrollable Timeline List (Fixed Box) */}
      <div className="ai-timeline-container">
        <div className="ai-timeline-scroll" role="region" aria-label="决策历史时间轴">
          {cycles.length > 0 ? (
            cycles.map((cycle, idx) => {
              const isExpanded = expandedCycleId === (cycle.cycle_id || String(idx));
              const source = decisionSource(cycle);
              const action = cycle.action || 'CHECK';
              const cycleStatus = String(cycle.status || '').toUpperCase();
              const time = formatTradingTime(cycle.completed_at || cycle.scheduled_at || cycle.timestamp);

              return (
                <div key={cycle.cycle_id || idx} className="ai-timeline-node">
                  <div className="ai-timeline-node__main">
                    <span
                      className={`ai-timeline-badge ${
                        source.tone === 'model'
                          ? 'ai-timeline-badge--model'
                          : source.tone === 'risk'
                          ? 'ai-timeline-badge--risk'
                          : 'ai-timeline-badge--sys'
                      }`}
                    >
                      {source.tone === 'model' ? '🧠 AI模型' : source.tone === 'risk' ? (cycle.decision_origin === 'RISK' ? '🛡️ 风控' : '🛑 系统阻断') : '⚙️ 系统检查'}
                    </span>
                    <strong className="ai-timeline-action">{action}</strong>
                    {cycleStatus && (
                      <span className={`v2-badge ${cycleStatus === 'EXECUTED' ? 'v2-badge--bull' : cycleStatus === 'SUBMITTED' ? 'v2-badge--gold' : 'v2-badge--neutral'}`}>
                        {cycleStatus === 'EXECUTED' ? '已成交' : cycleStatus === 'SUBMITTED' ? '已提交待成交' : cycleStatus}
                      </span>
                    )}
                    <span className="ai-timeline-msg">
                      {cycle.human_message || cycle.reason || cycleFallbackMessage(cycle)}
                    </span>
                    <time className="ai-timeline-time">{time}</time>
                    <button
                      type="button"
                      className="ai-timeline-toggle-btn"
                      onClick={() =>
                        setExpandedCycleId(isExpanded ? null : (cycle.cycle_id || String(idx)))
                      }
                    >
                      {isExpanded ? '收起 ▴' : '详情 ▾'}
                    </button>
                  </div>

                  {isExpanded && (
                    <div className="ai-timeline-detail">
                      <div className="ai-timeline-detail-meta">
                        <span>计划: {formatTradingTime(cycle.scheduled_at)}</span>
                        <span>开始: {formatTradingTime(cycle.started_at)}</span>
                        {cycle.duration_ms != null && <span>耗时: {cycle.duration_ms}ms</span>}
                      </div>
                      <pre className="ai-timeline-json">{JSON.stringify(cycle.payload, null, 2)}</pre>
                    </div>
                  )}
                </div>
              );
            })
          ) : (
            <p className="v2-note" style={{ padding: '16px', textAlign: 'center' }}>
              尚无执行记录，等待策略调度启动
            </p>
          )}
        </div>
      </div>
    </section>
  );
}

