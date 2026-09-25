import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { AIDecisionHistory } from './AIDecisionHistory';

it('labels system blocks as blocked before model invocation, not a rule scan', () => {
  render(
    <AIDecisionHistory
      cycles={[{
        action: 'SYSTEM_BLOCKED',
        decision_origin: 'SYSTEM',
        model_called: false,
        status: 'BLOCKED',
        reason: 'SMART_MODEL_UNAVAILABLE',
      }]}
      runtime={{ enabled: false }}
    />,
  );

  expect(screen.getByText('系统阻断 · 未调用模型')).toBeInTheDocument();
  expect(screen.queryByText('底层规则扫描')).not.toBeInTheDocument();
  expect(screen.getByText('🛑 系统阻断')).toBeInTheDocument();
});

it('does not label a WAIT plan as a real-time market entry', () => {
  render(
    <AIDecisionHistory
      cycles={[{
        action: 'WAIT',
        decision_origin: 'MODEL',
        timestamp: '2026-09-15T00:45:00Z',
        payload: {
          strategy_plan: {
            name: '限价回踩',
            thesis: '等待关键位',
            entry_conditions: ['触及支撑'],
            exit_conditions: ['结构失效'],
          },
          model_output: { action: 'WAIT', order_preference: 'AUTO' },
          strategy_instructions: {
            profile: { order_preference: 'LIMIT' },
            execution: { order_preference: 'LIMIT' },
          },
        },
      }]}
      runtime={{ enabled: false }}
    />,
  );

  expect(screen.getByText('本轮未生成订单')).toBeInTheDocument();
  expect(screen.getByText('本轮无订单 · 策略偏好：限价单')).toBeInTheDocument();
  expect(screen.queryByText('实时市价')).not.toBeInTheDocument();
});

it('shows a blocked model opening as an unsubmitted proposal', () => {
  render(
    <AIDecisionHistory
      cycles={[{
        action: 'OPEN_LONG',
        decision_origin: 'MODEL',
        model_called: true,
        block_stage: 'RISK',
        reason: 'AI_LIMIT_WOULD_CROSS_QUOTE',
        timestamp: '2026-09-24T16:36:49Z',
        payload: {
          strategy_plan: { name: '闪电动量', thesis: '等待限价成交', entry_conditions: ['突破'], exit_conditions: ['结构失效'] },
          model_output: { action: 'OPEN_LONG', order_preference: 'LIMIT', entry_price: 338.95, stop_price: 333.33 },
          strategy_instructions: { execution: { order_preference: 'AUTO' } },
        },
      }]}
      runtime={{ enabled: true }}
    />,
  );

  expect(screen.getByText('AI 决策提案')).toBeInTheDocument();
  expect(screen.getAllByText(/RISK 拦截 · 未下单：AI_LIMIT_WOULD_CROSS_QUOTE/)).toHaveLength(2);
  expect(screen.getByText('拟用：限价单')).toBeInTheDocument();
  expect(screen.queryByText(/移动止损.*已启用/)).not.toBeInTheDocument();
  expect(screen.queryByText('AI 执行方案')).not.toBeInTheDocument();
});
