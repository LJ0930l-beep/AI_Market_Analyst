import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { apiClient } from '../api/client';
import { AIStrategyLibrary } from './AIStrategyLibrary';

afterEach(() => vi.restoreAllMocks());

it('persists notional, leverage and prompt configuration in the selected account revision', async () => {
  const active = { account_id: 'gate_testnet', revision: 2, name: '事件策略', digest: 'saved', sections: { role: '交易角色', frequency: '纪律', entry_standards: '标准', decision_process: '流程', custom_prompt: '自定义' }, execution: { symbols: ['BTCUSDT'], direction: 'BOTH', sizing_mode: 'RISK_BASED', fixed_notional_usdt: 1000, equity_notional_pct: 5, max_notional_usdt: 5000, risk_per_trade_pct: .25, leverage: 3, max_positions: 3, max_margin_pct: 20, min_confidence: 70, min_net_rr: 2, cooldown_minutes: 30, order_preference: 'AUTO' } };
  const call = vi.spyOn(apiClient, 'v2').mockImplementation(async (path, method, body) => {
    if (path.startsWith('/gate/account')) return { data_status: 'AVAILABLE', equity: 10000, available_margin: 10000, used_margin: 0 } as never;
    if (method === 'PUT') return { active: { ...active, ...(body as object), revision: 3 } } as never;
    return { active, templates: [] } as never;
  });
  render(<MemoryRouter><AIStrategyLibrary accountId="gate_testnet" /></MemoryRouter>);
  fireEvent.change(await screen.findByLabelText(/仓位计算方式/), { target: { value: 'FIXED_NOTIONAL' } });
  fireEvent.change(screen.getByLabelText(/每笔名义金额（USDT）/), { target: { value: '500' } });
  fireEvent.change(screen.getByLabelText(/用户允许的杠杆上限/), { target: { value: '2' } });
  fireEvent.click(screen.getByRole('button', { name: '保存并启用' }));
  await waitFor(() => expect(call).toHaveBeenCalledWith('/ai-strategy?account_id=gate_testnet', 'PUT', expect.objectContaining({ expected_revision: 2, execution: expect.objectContaining({ sizing_mode: 'FIXED_NOTIONAL', fixed_notional_usdt: 500, leverage: 2 }) })));
  expect(await screen.findByText(/已启用版本 3/)).toBeInTheDocument();
});

it('imports NOFX JSON through the revision-checked endpoint and synchronizes the active strategy', async () => {
  const execution = { symbols: ['BTCUSDT'], universe_mode: 'ALL', scan_interval_minutes: 15, direction: 'BOTH', sizing_mode: 'RISK_BASED', fixed_notional_usdt: 1000, equity_notional_pct: 5, max_notional_usdt: 5000, risk_per_trade_pct: .2, leverage: 3, max_positions: 3, max_margin_pct: 20, min_confidence: 70, min_net_rr: 2, cooldown_minutes: 30, order_preference: 'LIMIT' };
  const sections = { role: '旧角色', frequency: '每 15 分钟扫描', entry_standards: '旧入场', decision_process: '旧退出', custom_prompt: '' };
  const active = { account_id: 'gate_testnet', revision: 5, name: '原策略', sections, execution };
  const imported = { ...active, revision: 6, name: 'NOFX 导入策略', sections: { ...sections, role: 'NOFX 策略角色' } };
  const configuration = { name: 'NOFX 原始策略', strategy: 'mean_reversion', interval: '15m' };
  const deferred = <T,>() => {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>(done => { resolve = done; });
    return { promise, resolve };
  };
  const pendingImport = deferred<unknown>();
  const call = vi.spyOn(apiClient, 'v2').mockImplementation(async path => {
    if (path.startsWith('/gate/markets')) return { markets: [] } as never;
    if (path.startsWith('/gate/account')) return { data_status: 'UNAVAILABLE' } as never;
    if (path.startsWith('/ai-session/memory')) return { items: [] } as never;
    if (path.startsWith('/ai-strategy/import-nofx')) return pendingImport.promise as never;
    return { active, templates: [] } as never;
  });
  render(<MemoryRouter><AIStrategyLibrary accountId="gate_testnet" /></MemoryRouter>);

  const strategyName = await screen.findByLabelText('策略名称');
  const importButton = screen.getByRole('button', { name: '选择 JSON 并导入' });
  const fileInput = screen.getByLabelText('选择 NOFX 策略 JSON 文件') as HTMLInputElement;
  fireEvent.change(fileInput, { target: { files: [new File([JSON.stringify(configuration)], 'strategy.json', { type: 'application/json' })] } });

  await waitFor(() => expect(call).toHaveBeenCalledWith(
    '/ai-strategy/import-nofx?account_id=gate_testnet',
    'POST',
    { configuration, expected_revision: 5 },
  ));
  expect(importButton).toBeDisabled();
  expect(screen.getByRole('button', { name: '导入中…' })).toBeDisabled();
  expect(fileInput.value).toBe('');

  await act(async () => { pendingImport.resolve({ active: imported, imported_fields: ['sections.role', 'sections.frequency'], truncated_fields: ['entry_standards'], ignored_fields: ['leverage', 'coin_pool', 'external_model'] }); });
  expect(strategyName).toHaveValue('NOFX 导入策略');
  expect(await screen.findByRole('status')).toHaveTextContent('NOFX 策略已导入为版本 6');
  expect(screen.getByText('sections.role')).toBeInTheDocument();
  expect(screen.getByText('coin_pool')).toBeInTheDocument();
  expect(screen.getByRole('note')).toHaveTextContent('新闻与技术面的入场标准');
  expect(screen.getByText(/不运行 NOFX 原生执行器/)).toBeInTheDocument();
  expect(screen.getByText('leverage')).toBeInTheDocument();
  expect(screen.getByText(/Gate 风控与金额配置仍按当前账户策略执行/)).toBeInTheDocument();
});

it('shows local-profile provenance when no NOFX runtime metadata is present', async () => {
  const execution = { symbols: [], universe_mode: 'ALL', scan_interval_minutes: 5, direction: 'BOTH', sizing_mode: 'RISK_BASED', fixed_notional_usdt: 1000, equity_notional_pct: 5, max_notional_usdt: 5000, risk_per_trade_pct: .2, leverage: 3, max_positions: 3, max_margin_pct: 20, min_confidence: 70, min_net_rr: 2, cooldown_minutes: 30, order_preference: 'LIMIT' };
  const sections = { role: '角色', frequency: '纪律', entry_standards: '标准', decision_process: '流程', custom_prompt: '' };
  const profile = { signal_timeframe: '5m', context_timeframes: ['15m', '1h'], candidate_strategy_ids: ['liquidity_sweep', 'ema_trend'] };
  vi.spyOn(apiClient, 'v2').mockImplementation(async path => {
    if (path.startsWith('/gate/markets')) return { markets: [] } as never;
    if (path.startsWith('/gate/account')) return { data_status: 'UNAVAILABLE' } as never;
    if (path.startsWith('/ai-session/memory')) return { items: [] } as never;
    return { active: { account_id: 'gate_testnet', revision: 1, template_id: 'aggressive_impulse', name: '本地激进策略', sections, execution, profile }, templates: [] } as never;
  });

  render(<MemoryRouter><AIStrategyLibrary accountId="gate_testnet" /></MemoryRouter>);

  expect(await screen.findByRole('heading', { name: '策略运行与来源' })).toBeInTheDocument();
  expect(screen.getByText('本地策略档案')).toBeInTheDocument();
  expect(screen.getByText(/不宣称使用 NOFX 原生策略引擎/)).toBeInTheDocument();
  expect(screen.getByText('5m')).toBeInTheDocument();
  expect(screen.getByText('15m · 1h')).toBeInTheDocument();
  expect(screen.getByText('liquidity_sweep')).toBeInTheDocument();
  expect(screen.getByText('ema_trend')).toBeInTheDocument();
  expect(screen.getByText(/无法判断原始配置中有哪些来源不支持或未映射/)).toBeInTheDocument();
  expect(screen.getByText(/未附带 NOFX 指标开关/)).toBeInTheDocument();
});

it('renders imported NOFX runtime timeframes, switches, exclusions and unsupported sources accurately', async () => {
  const execution = { symbols: [], universe_mode: 'ALL', scan_interval_minutes: 5, direction: 'BOTH', sizing_mode: 'RISK_BASED', fixed_notional_usdt: 1000, equity_notional_pct: 5, max_notional_usdt: 5000, risk_per_trade_pct: .2, leverage: 3, max_positions: 3, max_margin_pct: 20, min_confidence: 70, min_net_rr: 2, cooldown_minutes: 30, order_preference: 'LIMIT' };
  const sections = { role: '角色', frequency: '纪律', entry_standards: '标准', decision_process: '流程', custom_prompt: '' };
  const nofxRuntime = {
    version: 1,
    source: 'NOFX_IMPORT',
    signal_timeframe: '5m',
    context_timeframes: ['15m', '1h'],
    indicators: {
      raw_klines: { enabled: true },
      ema: { enabled: true, periods: [20, 50] },
      macd: { enabled: false },
      open_interest: { enabled: true, status: 'UNAVAILABLE' },
      funding_rate: { enabled: false, status: 'DISABLED' },
    },
    candidate_sources: ['NOFX · ema_trend', 'NOFX · liquidity_sweep'],
    excluded_symbols: ['SCAM_USDT'],
    unsupported_sources: ['NOFX private account stream', 'external model endpoint'],
  };
  vi.spyOn(apiClient, 'v2').mockImplementation(async path => {
    if (path.startsWith('/gate/markets')) return { markets: [] } as never;
    if (path.startsWith('/gate/account')) return { data_status: 'UNAVAILABLE' } as never;
    if (path.startsWith('/ai-session/memory')) return { items: [] } as never;
    return { active: { account_id: 'gate_testnet', revision: 4, template_id: 'aggressive_impulse', style: 'AGGRESSIVE', name: 'NOFX 映射策略', sections, execution, profile: { signal_timeframe: '15m', context_timeframes: ['1h'] }, nofx_runtime: nofxRuntime }, templates: [] } as never;
  });

  render(<MemoryRouter><AIStrategyLibrary accountId="gate_testnet" /></MemoryRouter>);

  expect(await screen.findByText('NOFX 参数映射 · 本地执行')).toBeInTheDocument();
  expect(screen.getByText(/不代表运行 NOFX 原生策略引擎/)).toBeInTheDocument();
  expect(screen.getByText('5m')).toBeInTheDocument();
  expect(screen.getByText('15m · 1h')).toBeInTheDocument();
  expect(screen.getByText('NOFX · ema_trend')).toBeInTheDocument();
  expect(screen.getByText('NOFX · liquidity_sweep')).toBeInTheDocument();
  expect(screen.getByText('SCAM_USDT')).toBeInTheDocument();
  expect(screen.getByText('NOFX private account stream')).toBeInTheDocument();
  expect(screen.getByText('external model endpoint')).toBeInTheDocument();
  expect(screen.getByText('已启用 · 20/50')).toBeInTheDocument();
  expect(screen.getAllByText('已关闭')).toHaveLength(2);
  expect(screen.getByText('数据不可用')).toBeInTheDocument();
  expect(screen.getByText('数据源已关闭')).toBeInTheDocument();
});

it('shows NOFX import errors and rejects invalid JSON without calling the endpoint', async () => {
  const execution = { symbols: [], universe_mode: 'ALL', scan_interval_minutes: 15, direction: 'BOTH', sizing_mode: 'RISK_BASED', fixed_notional_usdt: 1000, equity_notional_pct: 5, max_notional_usdt: 5000, risk_per_trade_pct: .2, leverage: 3, max_positions: 3, max_margin_pct: 20, min_confidence: 70, min_net_rr: 2, cooldown_minutes: 30, order_preference: 'LIMIT' };
  const sections = { role: '角色', frequency: '纪律', entry_standards: '标准', decision_process: '流程', custom_prompt: '' };
  const call = vi.spyOn(apiClient, 'v2').mockImplementation(async path => {
    if (path.startsWith('/gate/markets')) return { markets: [] } as never;
    if (path.startsWith('/gate/account')) return { data_status: 'UNAVAILABLE' } as never;
    if (path.startsWith('/ai-session/memory')) return { items: [] } as never;
    if (path.startsWith('/ai-strategy/import-nofx')) throw new Error('策略版本冲突，请刷新后重试');
    return { active: { account_id: 'gate_testnet', revision: 2, name: '现有策略', sections, execution }, templates: [] } as never;
  });
  render(<MemoryRouter><AIStrategyLibrary accountId="gate_testnet" /></MemoryRouter>);
  const fileInput = await screen.findByLabelText('选择 NOFX 策略 JSON 文件') as HTMLInputElement;

  fireEvent.change(fileInput, { target: { files: [new File(['{oops'], 'bad.json', { type: 'application/json' })] } });
  expect(await screen.findByRole('alert')).toHaveTextContent('文件不是有效的 JSON');
  expect(call.mock.calls.some(([path]) => String(path).startsWith('/ai-strategy/import-nofx'))).toBe(false);

  fireEvent.change(fileInput, { target: { files: [new File(['{}'], 'valid.json', { type: 'application/json' })] } });
  expect(await screen.findByRole('alert')).toHaveTextContent('策略版本冲突，请刷新后重试');
  expect(call).toHaveBeenCalledWith('/ai-strategy/import-nofx?account_id=gate_testnet', 'POST', { configuration: {}, expected_revision: 2 });
});

it('rejects oversized NOFX files in the browser before reading or uploading them', async () => {
  const execution = { symbols: [], universe_mode: 'ALL', scan_interval_minutes: 15, direction: 'BOTH', sizing_mode: 'RISK_BASED', fixed_notional_usdt: 1000, equity_notional_pct: 5, max_notional_usdt: 5000, risk_per_trade_pct: .2, leverage: 3, max_positions: 3, max_margin_pct: 20, min_confidence: 70, min_net_rr: 2, cooldown_minutes: 30, order_preference: 'LIMIT' };
  const sections = { role: '角色', frequency: '纪律', entry_standards: '标准', decision_process: '流程', custom_prompt: '' };
  const call = vi.spyOn(apiClient, 'v2').mockImplementation(async path => {
    if (path.startsWith('/gate/markets')) return { markets: [] } as never;
    if (path.startsWith('/gate/account')) return { data_status: 'UNAVAILABLE' } as never;
    if (path.startsWith('/ai-session/memory')) return { items: [] } as never;
    return { active: { account_id: 'gate_testnet', revision: 1, name: '当前策略', sections, execution }, templates: [] } as never;
  });
  render(<MemoryRouter><AIStrategyLibrary accountId="gate_testnet" /></MemoryRouter>);
  const fileInput = await screen.findByLabelText('选择 NOFX 策略 JSON 文件') as HTMLInputElement;
  const oversizedFile = new File(['{}'], 'large.json', { type: 'application/json' });
  Object.defineProperty(oversizedFile, 'size', { value: 200_001 });

  fireEvent.change(fileInput, { target: { files: [oversizedFile] } });

  expect(await screen.findByRole('alert')).toHaveTextContent('超过 200 KB 上限');
  expect(call.mock.calls.some(([path]) => String(path).startsWith('/ai-strategy/import-nofx'))).toBe(false);
});

it('loading the five minute template saves its cadence and all-exchange scope', async () => {
  const execution = { symbols: [], universe_mode: 'ALL', scan_interval_minutes: 15, direction: 'BOTH', sizing_mode: 'RISK_BASED', fixed_notional_usdt: 1000, equity_notional_pct: 5, max_notional_usdt: 5000, risk_per_trade_pct: .25, leverage: 3, max_positions: 3, max_margin_pct: 20, min_confidence: 70, min_net_rr: 2, cooldown_minutes: 30, order_preference: 'AUTO' };
  const sections = { role: '角色', frequency: '纪律', entry_standards: '标准', decision_process: '流程', custom_prompt: '限制' };
  const active = { account_id: 'gate_testnet', revision: 1, name: '原策略', sections, execution, nofx_runtime: { version: 1, source: 'NOFX_IMPORT', signal_timeframe: '15m', context_timeframes: ['1h'], candidate_sources: ['gate_active_usdt_perpetuals'], excluded_symbols: [], unsupported_sources: [], indicators: {} } };
  const call = vi.spyOn(apiClient, 'v2').mockImplementation(async (path, method, body) => {
    if (path.startsWith('/gate/markets')) return { markets: [{ symbol: 'DOGEUSDT' }, { symbol: 'NEWUSDT' }] } as never;
    if (path.startsWith('/gate/account')) return { data_status: 'UNAVAILABLE' } as never;
    if (method === 'PUT') return { active: { ...active, ...(body as object), revision: 2 } } as never;
    return { active, templates: [{ id: 'impulse', name: '事件脉冲', sections, style: 'AGGRESSIVE', scan_interval_minutes: 5 }] } as never;
  });
  render(<MemoryRouter><AIStrategyLibrary accountId="gate_testnet" /></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', { name: /事件脉冲/ }));
  fireEvent.click(screen.getByRole('tab', { name: '交易范围' }));
  expect(screen.getByLabelText('扫描间隔')).toHaveValue('5');
  expect(screen.getByLabelText('扫描范围')).toHaveValue('ALL');
  fireEvent.click(screen.getByRole('button', { name: '保存并启用' }));
  await waitFor(() => expect(call).toHaveBeenCalledWith('/ai-strategy?account_id=gate_testnet', 'PUT', expect.objectContaining({ template_id: 'impulse', nofx_runtime: null, execution: expect.objectContaining({ scan_interval_minutes: 5, universe_mode: 'ALL', fixed_notional_usdt: 1000 }) })));
});

it('labels account-wide memories honestly and distinguishes active from unsaved strategy drafts', async () => {
  const execution = { symbols: [], universe_mode: 'ALL', scan_interval_minutes: 15, direction: 'BOTH', sizing_mode: 'RISK_BASED', fixed_notional_usdt: 1000, equity_notional_pct: 5, max_notional_usdt: 5000, risk_per_trade_pct: .2, leverage: 3, max_positions: 3, max_margin_pct: 20, min_confidence: 70, min_net_rr: 2, cooldown_minutes: 30, order_preference: 'LIMIT' };
  const sections = { role: '角色', frequency: '纪律', entry_standards: '标准', decision_process: '流程', custom_prompt: '限制' };
  const templates = [
    { id: 'aggressive_breakout_5m', name: '突破脉冲', sections, style: 'AGGRESSIVE', scan_interval_minutes: 5, order_preference: 'LIMIT', execution_defaults: { risk_per_trade_pct: .2, max_notional_usdt: 5000, leverage: 4 } },
    { id: 'aggressive_momentum_15m', name: '趋势加速', sections, style: 'AGGRESSIVE', scan_interval_minutes: 15, order_preference: 'LIMIT' },
    { id: 'conservative_pullback', name: '稳健回踩', sections, style: 'CONSERVATIVE', scan_interval_minutes: 15, order_preference: 'LIMIT' },
    { id: 'conservative_range', name: '区间确认', sections, style: 'CONSERVATIVE', scan_interval_minutes: 15, order_preference: 'LIMIT' },
  ];
  vi.spyOn(apiClient, 'v2').mockImplementation(async (path, method, body) => {
    if (path.startsWith('/gate/markets')) return { markets: [] } as never;
    if (path.startsWith('/gate/account')) return { data_status: 'UNAVAILABLE' } as never;
    if (path.startsWith('/ai-session/memory')) return { items: [{ memory_id: 'm1', strategy_template_id: 'aggressive_breakout_5m', outcome_status: 'WIN' }, { memory_id: 'm2', strategy_template_id: 'conservative_pullback' }, { memory_id: 'm3', strategy_template_id: 'conservative_pullback', outcome_status: 'LOSS' }] } as never;
    if (method === 'PUT') return { active: { ...(body as object), account_id: 'gate_testnet', revision: 2, digest: 'saved' } } as never;
    return { active: { account_id: 'gate_testnet', revision: 1, template_id: 'conservative_pullback', name: '稳健回踩', sections, execution }, templates } as never;
  });

  render(<MemoryRouter><AIStrategyLibrary accountId="gate_testnet" /></MemoryRouter>);

  expect(await screen.findByText(/账户级 AI 经验池 · 3 条决策记录/)).toBeInTheDocument();
  expect(screen.getByText(/策略归属来自决策时保存的模板版本/)).toBeInTheDocument();
  const active = await screen.findByRole('button', { name: /稳健回踩/ });
  expect(active).toHaveAttribute('aria-pressed', 'true');
  expect(active).toHaveTextContent('当前生效 · 已选中');
  const aggressive = screen.getByRole('button', { name: /突破脉冲/ });
  expect(aggressive).toHaveTextContent('单笔风险 ≤ 0.2%');
  expect(aggressive).toHaveTextContent('用户杠杆 ≤ 4×');
  expect(aggressive).toHaveTextContent('AI 经验池 · 1 条策略决策 · 1 条已复盘');
  fireEvent.click(aggressive);
  expect(aggressive).toHaveTextContent('草稿已载入 · 尚未生效');
});

it('provides a linked, keyboard-navigable tab set for strategy settings', async () => {
  const execution = { symbols: [], universe_mode: 'ALL', scan_interval_minutes: 15, direction: 'BOTH', sizing_mode: 'RISK_BASED', fixed_notional_usdt: 1000, equity_notional_pct: 5, max_notional_usdt: 5000, risk_per_trade_pct: .2, leverage: 3, max_positions: 3, max_margin_pct: 20, min_confidence: 70, min_net_rr: 2, cooldown_minutes: 30, order_preference: 'LIMIT' };
  const sections = { role: '角色', frequency: '纪律', entry_standards: '标准', decision_process: '流程', custom_prompt: '限制' };
  vi.spyOn(apiClient, 'v2').mockImplementation(async path => {
    if (path.startsWith('/gate/account')) return { data_status: 'UNAVAILABLE' } as never;
    return { active: { account_id: 'gate_testnet', revision: 1, name: '策略', sections, execution }, templates: [] } as never;
  });
  render(<MemoryRouter><AIStrategyLibrary accountId="gate_testnet" /></MemoryRouter>);

  const firstTab = await screen.findByRole('tab', { name: '资金与风控' });
  const panel = screen.getByRole('tabpanel', { name: '资金与风控' });
  expect(firstTab).toHaveAttribute('aria-controls', panel.id);
  expect(panel).toHaveAttribute('aria-labelledby', firstTab.id);
  expect(firstTab).toHaveAttribute('tabindex', '0');

  fireEvent.keyDown(firstTab, { key: 'ArrowRight' });
  const nextTab = screen.getByRole('tab', { name: '交易范围' });
  expect(nextTab).toHaveAttribute('aria-selected', 'true');
  expect(nextTab).toHaveAttribute('tabindex', '0');
  expect(screen.getByRole('tabpanel', { name: '交易范围' })).toHaveAttribute('aria-labelledby', nextTab.id);
});

it('previews the exact strategy text limits and runs a local-only configuration check', async () => {
  const tooLongRole = '甲'.repeat(241);
  const sections = { role: tooLongRole, frequency: '每轮按周期扫描并管理已有仓位', entry_standards: '新闻与技术面共同确认入场区域', decision_process: '先检查持仓，再比较机会并设置止损止盈', custom_prompt: '不得放宽风控' };
  const execution = { symbols: [], universe_mode: 'ALL', scan_interval_minutes: 15, direction: 'BOTH', sizing_mode: 'RISK_BASED', fixed_notional_usdt: 1000, equity_notional_pct: 5, max_notional_usdt: 5000, risk_per_trade_pct: .2, leverage: 3, max_positions: 3, max_margin_pct: 20, min_confidence: 70, min_net_rr: 2, cooldown_minutes: 30, order_preference: 'LIMIT' };
  const template = { id: 'aggressive_momentum_15m', name: '顺势策略', sections, style: 'AGGRESSIVE', scan_interval_minutes: 15, order_preference: 'LIMIT', profile: { signal_timeframe: '15m', limit_priority: true } };
  const calls: string[] = [];
  const call = vi.spyOn(apiClient, 'v2').mockImplementation(async (path, method) => {
    calls.push(`${method || 'GET'} ${path}`);
    if (path.startsWith('/gate/markets')) return { markets: [] } as never;
    if (path.startsWith('/gate/account')) return { data_status: 'UNAVAILABLE' } as never;
    if (path.startsWith('/ai-session/memory')) return { items: [] } as never;
    return { active: { account_id: 'gate_testnet', revision: 1, template_id: 'aggressive_momentum_15m', style: 'AGGRESSIVE', name: '顺势策略', sections, execution, profile: template.profile }, templates: [template] } as never;
  });
  render(<MemoryRouter><AIStrategyLibrary accountId="gate_testnet" /></MemoryRouter>);

  fireEvent.click(await screen.findByRole('tab', { name: 'AI 决策指令' }));
  expect(screen.getByText('超出 1 字不会送入')).toBeInTheDocument();
  const preview = screen.getByText((_, element) => element?.tagName === 'PRE' && element.textContent?.includes('当前策略：顺势策略'));
  expect(preview).toHaveTextContent(`角色：${'甲'.repeat(240)}`);
  expect(preview).not.toHaveTextContent(`角色：${tooLongRole}`);
  fireEvent.click(screen.getByRole('button', { name: '运行本地配置检查' }));
  expect(screen.getByRole('status')).toHaveTextContent('配置字段检查通过');
  expect(screen.getByText(/不代表 Bonsai、行情、新闻或交易所连接已就绪/)).toBeInTheDocument();
  expect(calls.every(item => !item.startsWith('PUT '))).toBe(true);
  expect(call).toHaveBeenCalledTimes(4);
});

it('requests the production static instruction preview and labels its limitations', async () => {
  const sections = { role: '角色', frequency: '纪律', entry_standards: '标准', decision_process: '流程', custom_prompt: '补充' };
  const execution = { symbols: [], universe_mode: 'ALL', scan_interval_minutes: 15, direction: 'BOTH', sizing_mode: 'RISK_BASED', fixed_notional_usdt: 1000, equity_notional_pct: 5, max_notional_usdt: 5000, risk_per_trade_pct: .2, leverage: 3, max_positions: 3, max_margin_pct: 20, min_confidence: 70, min_net_rr: 2, cooldown_minutes: 30, order_preference: 'LIMIT' };
  const result = { preview_kind: 'STRATEGY_SYSTEM_INSTRUCTION_ONLY', valid: true, validation_errors: [], summary: { signal_timeframe: '15m', order_preference: 'AUTO', limit_priority: true }, strategy_system_instruction: '当前策略：后端生成的策略 system instruction', model_called: false, execution_called: false, limitations: ['不是本轮运行时完整模型请求。', '未调用 Bonsai 或交易所。'] };
  const call = vi.spyOn(apiClient, 'v2').mockImplementation(async path => {
    if (path.startsWith('/gate/account')) return { data_status: 'UNAVAILABLE' } as never;
    if (path.startsWith('/ai-session/memory')) return { items: [] } as never;
    if (path.startsWith('/ai-strategy/preview')) return result as never;
    if (path.startsWith('/gate/markets')) return { markets: [] } as never;
    return { active: { account_id: 'gate_testnet', revision: 1, template_id: 'conservative_pullback', style: 'CONSERVATIVE', name: '后端预览测试', sections, execution }, templates: [] } as never;
  });
  render(<MemoryRouter><AIStrategyLibrary accountId="gate_testnet" /></MemoryRouter>);

  fireEvent.click(await screen.findByRole('tab', { name: 'AI 决策指令' }));
  fireEvent.click(screen.getByRole('button', { name: '生成后端静态预览' }));
  expect(await screen.findByText('生产策略 System Instruction')).toBeInTheDocument();
  expect(screen.getByText('不是本轮运行时完整模型请求。')).toBeInTheDocument();
  expect(screen.getByText(/STRATEGY_SYSTEM_INSTRUCTION_ONLY/)).toBeInTheDocument();
  expect(call).toHaveBeenCalledWith('/ai-strategy/preview?account_id=gate_testnet', 'POST', expect.objectContaining({ name: '后端预览测试', template_id: 'conservative_pullback', sections, execution }));
  expect(call.mock.calls.some(([path]) => ['/ai-session/start', '/orders', '/gate/order'].some(executionPath => String(path).includes(executionPath)))).toBe(false);
  fireEvent.change(screen.getByLabelText(/交易角色与目标/), { target: { value: '修改后的策略角色' } });
  expect(screen.queryByText('生产策略 System Instruction')).not.toBeInTheDocument();
});

it('normalizes the local preview like the production template and preserves only the custom supplement', async () => {
  const sections = { role: '由模板提供的角色规则', frequency: '由系统每 15 分钟扫描并遵守已收盘K线', entry_standards: '由模板提供的入场规则', decision_process: '由模板提供的决策规则', custom_prompt: '默认补充' };
  const conflictingSections = { role: '旧策略角色', frequency: '由系统每 5 分钟扫描，旧周期文案', entry_standards: '旧入场文案', decision_process: '旧退出文案', custom_prompt: '  保留用户补充  ' };
  const template = { id: 'conservative_pullback', name: '回踩策略', style: 'CONSERVATIVE', sections, scan_interval_minutes: 15, profile: { signal_timeframe: '15m', context_timeframes: ['1h'], limit_priority: true } };
  const execution = { symbols: [], universe_mode: 'ALL', scan_interval_minutes: 15, direction: 'BOTH', sizing_mode: 'RISK_BASED', fixed_notional_usdt: 1000, equity_notional_pct: 5, max_notional_usdt: 5000, risk_per_trade_pct: .2, leverage: 3, max_positions: 3, max_margin_pct: 20, min_confidence: 70, min_net_rr: 2, cooldown_minutes: 30, order_preference: 'AUTO' };
  vi.spyOn(apiClient, 'v2').mockImplementation(async path => {
    if (path.startsWith('/gate/account')) return { data_status: 'UNAVAILABLE' } as never;
    if (path.startsWith('/ai-session/memory')) return { items: [] } as never;
    if (path.startsWith('/gate/markets')) return { markets: [] } as never;
    return { active: { account_id: 'gate_testnet', revision: 2, template_id: template.id, name: template.name, style: template.style, profile: template.profile, sections: conflictingSections, execution }, templates: [template] } as never;
  });
  render(<MemoryRouter><AIStrategyLibrary accountId="gate_testnet" /></MemoryRouter>);

  fireEvent.click(await screen.findByRole('tab', { name: 'AI 决策指令' }));
  const preview = screen.getByText((_, element) => element?.tagName === 'PRE' && element.textContent?.includes('当前策略：回踩策略'));
  expect(preview).toHaveTextContent('角色：由模板提供的角色规则');
  expect(preview).toHaveTextContent('频率与纪律：由系统每 15 分钟扫描并遵守已收盘K线');
  expect(preview).toHaveTextContent('入场标准：由模板提供的入场规则');
  expect(preview).toHaveTextContent('补充规则：保留用户补充');
  expect(preview).not.toHaveTextContent('旧策略角色');
  expect(preview).not.toHaveTextContent('旧入场文案');
  expect(screen.getAllByText('周期与模板冲突：后端会使用模板文案')).toHaveLength(5);
});

it('isolates pending server previews by account and request fingerprint', async () => {
  const sections = { role: '角色', frequency: '纪律', entry_standards: '标准', decision_process: '流程', custom_prompt: '补充' };
  const execution = { symbols: [], universe_mode: 'ALL', scan_interval_minutes: 15, direction: 'BOTH', sizing_mode: 'RISK_BASED', fixed_notional_usdt: 1000, equity_notional_pct: 5, max_notional_usdt: 5000, risk_per_trade_pct: .2, leverage: 3, max_positions: 3, max_margin_pct: 20, min_confidence: 70, min_net_rr: 2, cooldown_minutes: 30, order_preference: 'LIMIT' };
  const deferred = <T,>() => {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>(done => { resolve = done; });
    return { promise, resolve };
  };
  const firstPreview = deferred<unknown>();
  const secondPreview = deferred<unknown>();
  const result = (instruction: string) => ({ preview_kind: 'STRATEGY_SYSTEM_INSTRUCTION_ONLY', valid: true, validation_errors: [], summary: { account: instruction }, strategy_system_instruction: instruction, model_called: false, execution_called: false, limitations: ['静态策略预览'] });
  const call = vi.spyOn(apiClient, 'v2').mockImplementation(async path => {
    if (path.startsWith('/gate/account')) return { data_status: 'UNAVAILABLE' } as never;
    if (path.startsWith('/ai-session/memory')) return { items: [] } as never;
    if (path.startsWith('/gate/markets')) return { markets: [] } as never;
    if (path.startsWith('/ai-strategy/preview?account_id=gate_testnet')) return firstPreview.promise as never;
    if (path.startsWith('/ai-strategy/preview?account_id=gate_other')) return secondPreview.promise as never;
    const account = path.includes('account_id=gate_other') ? 'gate_other' : 'gate_testnet';
    return { active: { account_id: account, revision: 1, template_id: 'conservative_pullback', style: 'CONSERVATIVE', name: `策略-${account}`, sections, execution }, templates: [] } as never;
  });
  const view = render(<MemoryRouter><AIStrategyLibrary accountId="gate_testnet" /></MemoryRouter>);

  fireEvent.click(await screen.findByRole('tab', { name: 'AI 决策指令' }));
  fireEvent.click(screen.getByRole('button', { name: '生成后端静态预览' }));
  await waitFor(() => expect(call).toHaveBeenCalledWith('/ai-strategy/preview?account_id=gate_testnet', 'POST', expect.any(Object)));
  expect(screen.getByRole('button', { name: '生成后端静态预览…' })).toBeDisabled();

  view.rerender(<MemoryRouter><AIStrategyLibrary accountId="gate_other" /></MemoryRouter>);
  const otherAccountButton = await screen.findByRole('button', { name: '生成后端静态预览' });
  expect(otherAccountButton).toBeEnabled();
  fireEvent.click(otherAccountButton);
  await waitFor(() => expect(call).toHaveBeenCalledWith('/ai-strategy/preview?account_id=gate_other', 'POST', expect.any(Object)));
  expect(screen.getByRole('button', { name: '生成后端静态预览…' })).toBeDisabled();

  await act(async () => { firstPreview.resolve(result('旧账户预览')); });
  expect(screen.getByRole('button', { name: '生成后端静态预览…' })).toBeDisabled();
  expect(screen.queryByText('旧账户预览')).not.toBeInTheDocument();

  await act(async () => { secondPreview.resolve(result('新账户预览')); });
  expect(screen.getByText('新账户预览', { selector: 'pre' })).toBeInTheDocument();
  expect(screen.queryByText('旧账户预览', { selector: 'pre' })).not.toBeInTheDocument();
  const status = screen.getByRole('status');
  expect(status).toHaveAttribute('aria-live', 'polite');
  expect(status).toHaveTextContent('后端配置校验通过');
});

