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
