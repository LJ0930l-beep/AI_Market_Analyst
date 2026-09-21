import { expect, it } from 'vitest';
import { formatTradingTime } from './tradingTime';

it('renders equivalent UTC and offset timestamps in the same HKT timeline', () => {
  expect(formatTradingTime('2026-09-13T09:15:00Z')).toBe(formatTradingTime('2026-09-13T17:15:00+08:00'));
  expect(formatTradingTime('2026-09-13T09:15:00Z')).toContain('17:15:00');
  expect(formatTradingTime(null)).toBe('未记录');
});
