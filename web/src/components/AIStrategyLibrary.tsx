import { useEffect, useId, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { apiClient } from '../api/client';
import './aiStrategy.css';

type Sections = Record<string, string>;
export interface ExecutionSettings {
  symbols: string[]; universe_mode: string; scan_interval_minutes: number; direction: string; sizing_mode: string; fixed_notional_usdt: number;
  equity_notional_pct: number; max_notional_usdt: number; risk_per_trade_pct: number;
  leverage: number; max_positions: number; max_margin_pct: number; min_confidence: number;
  min_net_rr: number; cooldown_minutes: number; order_preference: string;
  atr_adaptive_sizing: boolean; consecutive_loss_lock_enabled: boolean; us_open_defense_enabled: boolean;
}
interface NofxIndicatorRuntime { enabled?: boolean; periods?: number[]; status?: string }
interface NofxRuntime {
  version?: number;
  source?: string;
  signal_timeframe?: string;
  context_timeframes?: string[];
  indicators?: Record<string, NofxIndicatorRuntime>;
  candidate_sources?: string[];
  excluded_symbols?: string[];
  unsupported_sources?: string[];
}
interface Strategy { account_id: string; name: string; revision: number; template_id?: string; style?: string; profile?: StrategyProfile; nofx_runtime?: NofxRuntime; sections: Sections; digest: string; execution: ExecutionSettings }
interface StrategyProfile { [key: string]: unknown }
interface Library { active: Strategy; templates: { id: string; name: string; sections: Sections; style: string; scan_interval_minutes: number; order_preference?: string; profile?: StrategyProfile; execution_defaults?: Partial<ExecutionSettings> }[] }
interface Capital { data_status?: string; equity?: number | null; available_margin?: number | null; used_margin?: number | null; observed_at?: string; error_code?: string }
interface StrategyMemory { strategy_template_id?: string; outcome_status?: string | null }
interface StrategyPreview {
  preview_kind: string;
  valid: boolean;
  validation_errors: string[];
  summary: Record<string, string | number | boolean | null>;
  strategy_system_instruction: string;
  model_called: boolean;
  execution_called: boolean;
  limitations: string[];
}
interface StrategyPreviewState { fingerprint: string; data?: StrategyPreview; error?: string }
interface StrategyPreviewRequest { fingerprint: string; requestId: number }
interface NofxImportResult { active: Strategy; imported_fields: string[]; truncated_fields?: string[]; ignored_fields: string[] }
interface NofxImportFeedback { kind: 'success' | 'error'; message: string; importedFields?: string[]; truncatedFields?: string[]; ignoredFields?: string[] }
const labels: Record<string, string> = { role: '交易角色与目标', frequency: '频率与持仓纪律', entry_standards: '新闻与技术面的入场标准', decision_process: '决策、仓位管理与退出流程', custom_prompt: '补充指令' };
const strategyTextLimits: Record<string, number> = { role: 240, frequency: 480, entry_standards: 1500, decision_process: 1000, custom_prompt: 800 };
const nofxConfigMaxBytes = 200_000;
const tabs = ['资金与风控', '交易范围', 'AI 决策指令', '配置预览'];
const money = (value: number | null) => value === null ? '等待账户同步' : `${value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })} USDT`;
const orderLabel = (value: string) => value.toUpperCase() === 'LIMIT' ? '限价单' : value.toUpperCase() === 'MARKET' ? '市价单' : '自动选择';
const pythonTextSlice = (value: string, limit: number) => Array.from(value).slice(0, limit).join('');
const nofxIndicatorLabels: Record<string, string> = {
  raw_klines: 'K 线', ema: 'EMA', macd: 'MACD', rsi: 'RSI', atr: 'ATR',
  bollinger: '布林带', volume: '成交量', open_interest: '未平仓量 OI', funding_rate: '资金费率',
};
const nofxIndicatorOrder = ['raw_klines', 'ema', 'macd', 'rsi', 'atr', 'bollinger', 'volume', 'open_interest', 'funding_rate'];
const runtimeValue = (value: unknown): string => typeof value === 'string' && value.trim() ? value.trim() : '';
const runtimeList = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && Boolean(item.trim())).map(item => item.trim()) : [];
function formatIndicatorPeriods(periods: unknown): string {
  return Array.isArray(periods) && periods.every(value => Number.isFinite(value)) ? ` · ${periods.join('/')}` : '';
}
function indicatorStatus(status: unknown): string {
  const value = runtimeValue(status).toUpperCase();
  if (value === 'CONFIGURED') return '已启用 · 每轮核验数据';
  if (value === 'AVAILABLE') return '数据可用';
  if (value === 'UNAVAILABLE') return '数据不可用';
  if (value === 'DISABLED') return '数据源已关闭';
  return value || '未报告数据状态';
}
function StrategyRuntimeCard({ strategy }: { strategy: Strategy }) {
  const headingId = useId();
  const runtime = strategy.nofx_runtime;
  const profile = strategy.profile;
  const localSignalTimeframe = runtimeValue(profile?.signal_timeframe);
  const signalTimeframe = runtime
    ? runtimeValue(runtime.signal_timeframe) || '未报告'
    : localSignalTimeframe || `${strategy.execution.scan_interval_minutes}m`;
  const hasContextTimeframes = Array.isArray(runtime?.context_timeframes);
  const contextTimeframes = runtime
    ? runtimeList(runtime.context_timeframes)
    : runtimeList(profile?.context_timeframes);
  const indicators = runtime?.indicators || {};
  const indicatorKeys = [
    ...nofxIndicatorOrder.filter(key => Object.prototype.hasOwnProperty.call(indicators, key)),
    ...Object.keys(indicators).filter(key => !Object.prototype.hasOwnProperty.call(nofxIndicatorLabels, key)),
  ];
  const candidateSources = runtime
    ? runtimeList(runtime.candidate_sources)
    : runtimeList(profile?.candidate_strategy_ids);
  const hasCandidateSources = Array.isArray(runtime?.candidate_sources);
  const hasExcludedSymbols = Array.isArray(runtime?.excluded_symbols);
  const excludedSymbols = runtimeList(runtime?.excluded_symbols);
  const hasUnsupportedSources = Array.isArray(runtime?.unsupported_sources);
  const unsupportedSources = runtimeList(runtime?.unsupported_sources);

  return <section className="ai-runtime-card" aria-labelledby={headingId} data-mode={runtime ? 'nofx-import' : 'local-profile'}>
    <header>
      <div><small>STRATEGY RUNTIME / PROVENANCE</small><h3 id={headingId}>策略运行与来源</h3></div>
      <span>{runtime ? 'NOFX 参数映射 · 本地执行' : '本地策略档案'}</span>
    </header>
    <p className="ai-runtime-card__explanation">{runtime
      ? '此策略包含从 NOFX 导入的运行参数；候选筛选、Bonsai 决策与 Gate 委托仍由本项目链路处理，不代表运行 NOFX 原生策略引擎。'
      : '当前策略按本地 profile 与 Gate 配置运行；未附带 NOFX runtime 元数据，因此这里不宣称使用 NOFX 原生策略引擎。'}</p>
    <div className="ai-runtime-card__summary">
      <div><small>AI 评估节奏</small><strong>{strategy.execution.scan_interval_minutes} 分钟</strong></div>
      <div><small>信号主周期</small><strong>{signalTimeframe}</strong></div>
      <div><small>背景周期</small><strong>{contextTimeframes.length ? contextTimeframes.join(' · ') : runtime ? hasContextTimeframes ? '未配置' : '未提供字段' : '未报告'}</strong></div>
      <div><small>策略来源</small><strong>{runtime ? runtimeValue(runtime.source) || 'NOFX_IMPORT' : '本地策略 profile'}</strong></div>
    </div>
    <div className="ai-runtime-card__detail-grid">
      <section aria-label="指标开关">
        <h4>指标开关与数据状态</h4>
        {indicatorKeys.length ? <ul className="ai-runtime-indicators">{indicatorKeys.map(key => {
          const indicator = indicators[key] || {};
          const enabledLabel = indicator.enabled === true ? '已启用' : indicator.enabled === false ? '已关闭' : '开关未报告';
          return <li key={key} data-enabled={indicator.enabled === true ? 'true' : indicator.enabled === false ? 'false' : 'unknown'}>
            <span className="ai-runtime-indicator__mark" aria-hidden="true" />
            <strong>{nofxIndicatorLabels[key] || key.replace(/_/g, ' ')}</strong>
            <span>{enabledLabel}{formatIndicatorPeriods(indicator.periods)}</span>
            {indicator.status && <small>{indicatorStatus(indicator.status)}</small>}
          </li>;
        })}</ul> : <p className="ai-runtime-card__empty">{runtime ? '后端未提供指标开关数据。' : '当前策略未附带 NOFX 指标开关；本地 profile 没有在此处声明具体指标。'}</p>}
      </section>
      <section aria-label="候选来源与排除项">
        <h4>{runtime ? 'NOFX 候选来源与排除项' : '本地候选来源与范围'}</h4>
        <div className="ai-runtime-list-block"><small>候选来源</small>{candidateSources.length
          ? <ul>{candidateSources.map(value => <li key={value}>{value}</li>)}</ul>
          : <p>{runtime ? hasCandidateSources ? '未报告候选来源。' : '后端未提供候选来源字段。' : '本地 profile 未配置候选策略 ID。'}</p>}</div>
        <div className="ai-runtime-list-block"><small>排除合约</small>{runtime
          ? hasExcludedSymbols ? excludedSymbols.length ? <ul>{excludedSymbols.map(value => <li key={value}>{value}</li>)}</ul> : <p>未配置排除合约。</p> : <p>后端未提供排除合约字段。</p>
          : <p>未提供 NOFX 排除合约数据；当前交易范围由上方本地账户配置决定。</p>}</div>
      </section>
    </div>
    <section className="ai-runtime-unsupported" aria-label="NOFX 不支持或未映射的来源">
      <div><h4>NOFX 不支持 / 未映射来源</h4><small>保持原始边界，不会静默当作已接入</small></div>
      {runtime
        ? hasUnsupportedSources ? unsupportedSources.length ? <ul>{unsupportedSources.map(value => <li key={value}>{value}</li>)}</ul> : <p>没有已报告的不支持来源。</p> : <p>后端未提供不支持来源字段。</p>
        : <p>未知：当前策略未附带 NOFX runtime 对象，无法判断原始配置中有哪些来源不支持或未映射。</p>}
    </section>
  </section>;
}
function stableJson(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableJson);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, stableJson(item)]));
  return value;
}
function readTextFile(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => typeof reader.result === 'string' ? resolve(reader.result) : reject(new Error('无法读取所选文件内容。'));
    reader.onerror = () => reject(new Error('读取所选文件失败，请重试。'));
    reader.readAsText(file);
  });
}
function resolvePromptTemplate(draft: Strategy, templates: Library['templates']) {
  return templates.find(template => template.id === draft.template_id)
    || templates.find(template => template.name === draft.name)
    || templates[2]
    || null;
}
function normalizePromptSections(template: Library['templates'][number], sections: Sections): Sections {
  const normalized = { ...sections };
  const expectedInterval = Number(template.scan_interval_minutes || 15);
  const legacyPhrase = expectedInterval === 15 ? '每 5 分钟' : '每 15 分钟';
  if (!String(normalized.frequency || '').includes(legacyPhrase)) return normalized;
  const canonical = { ...template.sections };
  const customPrompt = String(normalized.custom_prompt || '').trim();
  if (customPrompt) canonical.custom_prompt = customPrompt;
  return canonical;
}

interface LocalCheck { blockers: string[]; passed: string[] }
interface LocalCheckState { fingerprint: string; result: LocalCheck }
function inspectStrategyDraft(draft: Strategy): LocalCheck {
  const blockers: string[] = [];
  const passed: string[] = [];
  const sections = draft.sections || {};
  for (const key of ['role', 'frequency', 'entry_standards', 'decision_process']) {
    const effectiveText = pythonTextSlice(String(sections[key] || '').trim(), strategyTextLimits[key]);
    if (!effectiveText.trim()) blockers.push(`${labels[key]}在后端字符上限内为空`);
    else passed.push(`${labels[key]}已填写`);
  }
  if (!draft.name.trim()) blockers.push('策略名称不能为空');
  else passed.push('策略名称已填写');

  const execution = draft.execution;
  if (![5, 15].includes(Number(execution.scan_interval_minutes))) blockers.push('扫描间隔仅支持 5 或 15 分钟');
  else passed.push(`扫描周期为 ${execution.scan_interval_minutes} 分钟`);
  if (execution.universe_mode === 'CUSTOM' && execution.symbols.length === 0) blockers.push('自选范围至少需要一个合约');
  else passed.push(execution.universe_mode === 'ALL' ? '交易范围为全品种' : `已选择 ${execution.symbols.length} 个合约`);
  const orderPreference = String(draft.profile?.order_preference || execution.order_preference || 'AUTO').toUpperCase();
  if (!['AUTO', 'LIMIT', 'MARKET'].includes(orderPreference)) blockers.push('订单偏好不是支持的选项');
  else passed.push(`订单偏好：${orderLabel(orderPreference)}`);
  if (!Number.isFinite(execution.leverage) || execution.leverage < 1 || execution.leverage > 100) blockers.push('杠杆配置需在 1–100 倍范围内');
  else passed.push('杠杆配置数值有效');
  if (!Number.isFinite(execution.max_notional_usdt) || execution.max_notional_usdt <= 0) blockers.push('单笔名义金额上限必须大于 0');
  else passed.push('单笔名义金额上限有效');
  if (!Number.isFinite(execution.risk_per_trade_pct) || execution.risk_per_trade_pct <= 0 || execution.risk_per_trade_pct > 0.25) blockers.push('单笔止损风险需大于 0 且不超过 0.25%');
  else passed.push('单笔止损风险在配置范围内');
  return { blockers, passed };
}

export function AIStrategyLibrary({ accountId }: { accountId: string }) {
  const [library, setLibrary] = useState<Library | null>(null);
  const [draft, setDraft] = useState<Strategy | null>(null);
  const [capital, setCapital] = useState<Capital | null>(null);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [busyAction, setBusyAction] = useState<'save' | 'import' | null>(null);
  const [nofxImportFeedback, setNofxImportFeedback] = useState<NofxImportFeedback | null>(null);
  const [tab, setTab] = useState(tabs[0]);
  const [markets, setMarkets] = useState<{ symbol: string }[]>([]);
  const [marketError, setMarketError] = useState('');
  const [search, setSearch] = useState('');
  const [stopDistance, setStopDistance] = useState(2);
  const [reloadKey, setReloadKey] = useState(0);
  const [memories, setMemories] = useState<StrategyMemory[] | null>(null);
  const [localCheckState, setLocalCheckState] = useState<LocalCheckState | null>(null);
  const [serverPreviewState, setServerPreviewState] = useState<StrategyPreviewState | null>(null);
  const [serverPreviewRequest, setServerPreviewRequest] = useState<StrategyPreviewRequest | null>(null);
  const serverPreviewRequestSequence = useRef(0);
  const nofxImportRequestSequence = useRef(0);
  const currentAccountId = useRef(accountId);
  currentAccountId.current = accountId;
  const nofxFileInput = useRef<HTMLInputElement>(null);
  const tabIdPrefix = useId();
  useEffect(() => {
    let current = true;
    setLibrary(null); setDraft(null); setCapital(null); setMessage(''); setBusy(false); setBusyAction(null); setNofxImportFeedback(null);
    setMarkets([]); setMarketError(''); setMemories(null);
    if (accountId) {
      apiClient.v2<{ markets: { symbol: string }[] }>(`/gate/markets?account_id=${encodeURIComponent(accountId)}`).then(data => { if (current) setMarkets(data.markets || []); }).catch(() => { if (current) setMarketError('交易所合约列表读取失败，请刷新重试。'); });
      apiClient.v2<Library>(`/ai-strategy?account_id=${encodeURIComponent(accountId)}`).then(data => {
        if (current) { setLibrary(data); setDraft(data.active); }
      }).catch(error => { if (current) setMessage(String(error)); });
      apiClient.v2<Capital>(`/gate/account?account_id=${encodeURIComponent(accountId)}`).then(data => { if (current) setCapital(data); }).catch(() => { if (current) setCapital({ data_status: 'UNAVAILABLE' }); });
      apiClient.v2<{ items?: StrategyMemory[] }>(`/ai-session/memory?account_id=${encodeURIComponent(accountId)}&limit=20`).then(data => { if (current) setMemories(Array.isArray(data.items) ? data.items : []); }).catch(() => { if (current) setMemories(null); });
    }
    return () => { current = false; nofxImportRequestSequence.current += 1; };
  }, [accountId, reloadKey]);
  async function save() {
    if (!draft || draft.account_id !== accountId) return;
    setBusy(true); setBusyAction('save'); setMessage(''); setNofxImportFeedback(null);
    try {
      const result = await apiClient.v2<{ active: Strategy }>(`/ai-strategy?account_id=${encodeURIComponent(accountId)}`, 'PUT', { name: draft.name, sections: draft.sections, execution: draft.execution, template_id: draft.template_id, nofx_runtime: draft.nofx_runtime ?? null, expected_revision: draft.revision });
      setDraft(result.active); setLibrary(previous => previous && { ...previous, active: result.active });
      setMessage(`已启用版本 ${result.active.revision}，下一轮分析使用新配置。`);
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(false); setBusyAction(null); }
  }
  async function importNofxJson(file: File | undefined) {
    if (!file || !draft || draft.account_id !== accountId || !accountId) return;
    if (file.size > nofxConfigMaxBytes) {
      setNofxImportFeedback({ kind: 'error', message: 'NOFX 策略 JSON 超过 200 KB 上限，请导出精简后的 AI 策略配置。' });
      return;
    }
    const requestId = ++nofxImportRequestSequence.current;
    const requestAccountId = accountId;
    setBusy(true); setBusyAction('import'); setMessage(''); setNofxImportFeedback(null);
    try {
      const text = await readTextFile(file);
      let configuration: unknown;
      try {
        configuration = JSON.parse(text);
      } catch {
        throw new Error('文件不是有效的 JSON，请检查语法后重试。');
      }
      if (!configuration || typeof configuration !== 'object' || Array.isArray(configuration)) {
        throw new Error('NOFX 配置必须是 JSON 对象。');
      }
      const result = await apiClient.v2<NofxImportResult>(
        `/ai-strategy/import-nofx?account_id=${encodeURIComponent(requestAccountId)}`,
        'POST',
        { configuration, expected_revision: draft.revision },
      );
      if (requestId !== nofxImportRequestSequence.current || currentAccountId.current !== requestAccountId) return;
      if (!result?.active || result.active.account_id !== requestAccountId || !Number.isFinite(result.active.revision)) {
        throw new Error('导入接口返回的策略账户或版本无效，界面未应用该结果。');
      }
      const importedFields = Array.isArray(result.imported_fields) ? result.imported_fields.filter((field): field is string => typeof field === 'string') : [];
      const truncatedFields = Array.isArray(result.truncated_fields) ? result.truncated_fields.filter((field): field is string => typeof field === 'string') : [];
      const ignoredFields = Array.isArray(result.ignored_fields) ? result.ignored_fields.filter((field): field is string => typeof field === 'string') : [];
      setDraft(result.active);
      setLibrary(previous => previous && { ...previous, active: result.active });
      setNofxImportFeedback({ kind: 'success', message: `NOFX 策略已导入为版本 ${result.active.revision}。`, importedFields, truncatedFields, ignoredFields });
    } catch (error) {
      if (requestId !== nofxImportRequestSequence.current || currentAccountId.current !== requestAccountId) return;
      setNofxImportFeedback({ kind: 'error', message: error instanceof Error ? error.message : String(error) });
    } finally {
      if (requestId === nofxImportRequestSequence.current && currentAccountId.current === requestAccountId) {
        setBusy(false);
        setBusyAction(null);
      }
    }
  }
  const execution = draft?.execution;
  function setSetting<K extends keyof ExecutionSettings>(key: K, value: ExecutionSettings[K]) { if (draft) setDraft({ ...draft, execution: { ...draft.execution, [key]: value } }); }
  const equity = capital?.data_status === 'AVAILABLE' && capital.equity != null ? Number(capital.equity) : null;
  const memoryCount = memories?.length ?? 0;
  const memoriesForStrategy = (templateId: string) => memories?.filter(item => item.strategy_template_id === templateId) ?? [];
  const riskBudget = equity !== null && execution ? equity * execution.risk_per_trade_pct / 100 : null;
  const amountTarget = execution && equity !== null && riskBudget !== null ? (execution.sizing_mode === 'FIXED_NOTIONAL' ? execution.fixed_notional_usdt : execution.sizing_mode === 'EQUITY_PERCENT' ? equity * execution.equity_notional_pct / 100 : riskBudget / (stopDistance / 100)) : null;
  const marginRoom = execution && equity !== null && capital?.available_margin != null && capital.used_margin != null ? Math.max(0, Math.min(Number(capital.available_margin), equity * execution.max_margin_pct / 100 - Number(capital.used_margin))) : null;
  const estimate = execution && amountTarget !== null && riskBudget !== null && marginRoom !== null && stopDistance > 0 ? Math.min(amountTarget, execution.max_notional_usdt, riskBudget / (stopDistance / 100), marginRoom * execution.leverage) : null;
  const previewPayload = draft ? { name: draft.name, sections: draft.sections, execution: draft.execution, template_id: draft.template_id } : null;
  const draftFingerprint = previewPayload ? JSON.stringify({ account_id: accountId, ...previewPayload }) : '';
  const currentServerPreview = serverPreviewState?.fingerprint === draftFingerprint ? serverPreviewState : null;
  const currentLocalCheck = localCheckState?.fingerprint === draftFingerprint ? localCheckState.result : null;
  const serverPreviewBusy = serverPreviewRequest?.fingerprint === draftFingerprint;
  async function requestServerPreview() {
    if (!draft || !accountId || !previewPayload) return;
    const fingerprint = draftFingerprint;
    const requestId = ++serverPreviewRequestSequence.current;
    setServerPreviewRequest({ fingerprint, requestId });
    try {
      const result = await apiClient.v2<StrategyPreview>(`/ai-strategy/preview?account_id=${encodeURIComponent(accountId)}`, 'POST', previewPayload);
      setServerPreviewState({ fingerprint, data: result });
    } catch (error) {
      setServerPreviewState({ fingerprint, error: String(error) });
    } finally {
      setServerPreviewRequest(current => current?.requestId === requestId ? null : current);
    }
  }
  const promptTemplate = draft && library ? resolvePromptTemplate(draft, library.templates) : null;
  const effectivePromptSections = draft && promptTemplate ? normalizePromptSections(promptTemplate, draft.sections) : null;
  const promptProfile = promptTemplate?.profile || draft?.profile || {};
  const promptText = (key: string) => pythonTextSlice(String(effectivePromptSections?.[key] || ''), strategyTextLimits[key] || 0);
  const strategyPromptPreview = !promptTemplate
    ? '模板定义尚未读取完成。为避免预览与后端归一化规则不一致，请生成后端静态预览后查看。'
    : draft && (Object.keys(draft.sections || {}).length > 0 || Object.keys(promptProfile).length > 0)
    ? [
      `当前策略：${draft.name || ''}（${promptTemplate.id} / ${promptTemplate.style}）`,
      `策略参数：${JSON.stringify(stableJson(promptProfile))}`,
      `角色：${promptText('role')}`,
      `频率与纪律：${promptText('frequency')}`,
      `入场标准：${promptText('entry_standards')}`,
      `决策与退出：${promptText('decision_process')}`,
      `补充规则：${promptText('custom_prompt')}`,
      '策略执行：只按收盘的 signal_timeframe 识别，再用 context_timeframes 确认。required_confirmations 是最低独立证据数；达标且无重大反向新闻时，主动给最佳 OPEN。按本轮 ATR 计算止损、R 倍止盈；每轮重算置信度。profile 优先于旧文案。limit_priority=true 时合理回踩用 LIMIT；只有触发确认、价格在 entry_zone、盘口成本合格且完成度达 profile 门槛才用 MARKET。列出匹配/缺失条件及触发完成度。',
    ].join('\n')
    : '当前策略段为空；后端不会附加自定义策略块。';
  const numeric = (key: keyof ExecutionSettings, label: string, min: number, max: number, step = 1, hint = '') => <label>{label}<input type="number" min={min} max={max} step={step} required value={Number.isFinite(execution?.[key]) ? execution?.[key] as number : ''} onChange={event => setSetting(key, event.target.value === '' ? NaN : Number(event.target.value))} /><small>{hint}</small></label>;
  return <section className="ai-strategy-library" data-no-translate>
    <header className="ai-strategy-intro"><div><span className="ai-strategy-eyebrow">STRATEGY STUDIO / BONSAI 2 27B</span><h2>让策略驱动 AI 找机会。</h2><p>全市场候选先按流动性、上涨动量、下跌动量与波动率分层，再由 AI 结合新闻和多周期 K 线择优。仓位与风控仍由代码执行。</p></div><Link to="/">交易指挥舱 →</Link></header>
    <ol className="ai-strategy-flow"><li>配置与指令</li><li>新闻 + {draft?.execution.scan_interval_minutes === 5 ? '5m / 15m / 1h' : '15m / 1h'}</li><li>AI 方案与金额</li><li>风控 → 下单 → 回执</li></ol>
    <p className="ai-strategy-scope">账户 <strong>{accountId || '尚未选择'}</strong> · 生效版本 {library?.active.revision ?? '—'} · 每次决策保留配置快照</p>
    <section className="ai-opportunity-engine" aria-label="机会捕捉流程">
      <div><span>01 / LIQUIDITY</span><strong>高流动性</strong><small>降低冲击成本</small></div>
      <div><span>02 / MOMENTUM+</span><strong>强势候选</strong><small>发现顺势做多</small></div>
      <div><span>03 / MOMENTUM−</span><strong>弱势候选</strong><small>发现顺势做空</small></div>
      <div><span>04 / RANGE</span><strong>波动候选</strong><small>捕捉突破与回归</small></div>
      <div><span>05 / ROTATION</span><strong>全市场轮换</strong><small>避免只盯 BTC</small></div>
    </section>
    {library && draft && execution ? <div className="ai-strategy-layout"><aside><h3>策略模板</h3><p>2 套激进 · 2 套保守。模板带入扫描节奏、限价优先规则、金额、杠杆和风险基线，保存前仍可调整。</p><p className="ai-memory-scope">账户级 AI 经验池 · {memories === null ? '读取状态不可用' : `${memoryCount} 条决策记录`}；策略归属来自决策时保存的模板版本。</p>{library.templates.map(template => {
        const isActive = library.active.template_id === template.id;
        const isDraft = draft.template_id === template.id;
        const profileExecution = { ...draft.execution, ...(template.execution_defaults || {}) };
        const templateOrderLabel = template.profile?.limit_priority === true
          ? '限价优先 · 阈值达标才市价'
          : orderLabel(template.order_preference || String(template.profile?.order_preference || draft.execution.order_preference));
        const stateLabel = isDraft
          ? isActive ? '当前生效 · 已选中' : '草稿已载入 · 尚未生效'
          : isActive ? '当前已生效' : '点击载入';
        return <button key={template.id} disabled={busy} aria-pressed={isDraft} onClick={() => { setDraft({ ...draft, template_id: template.id, style: template.style, profile: template.profile, nofx_runtime: undefined, name: template.name, sections: { ...template.sections }, execution: { ...draft.execution, ...(template.execution_defaults || {}), scan_interval_minutes: template.scan_interval_minutes, order_preference: (template.order_preference || (template.profile?.order_preference as string) || draft.execution.order_preference) } }); setMessage('模板已载入，保存后生效。'); }} className="ai-template-card" data-style={template.style} data-active={isActive} data-draft={isDraft && !isActive}>
          <small>{template.style === 'AGGRESSIVE' ? '激进策略' : '保守策略'} · {template.scan_interval_minutes} 分钟 · {templateOrderLabel}</small>
          <strong>{template.name}</strong>
          <em className="ai-template-memory">AI 经验池 · {memories === null ? '暂不可读' : `${memoriesForStrategy(template.id).length} 条策略决策${memoriesForStrategy(template.id).some(item => item.outcome_status) ? ` · ${memoriesForStrategy(template.id).filter(item => item.outcome_status).length} 条已复盘` : ''}`}</em>
          <span className="ai-template-card__state">{stateLabel}</span>
          <em>单笔风险 ≤ {profileExecution.risk_per_trade_pct}% · 名义金额 ≤ {profileExecution.max_notional_usdt} USDT · 用户杠杆 ≤ {profileExecution.leverage}×</em>
        </button>;
      })}<StrategyRuntimeCard strategy={draft} /><div className="ai-strategy-risk"><h3>当前配置</h3><dl><dt>策略模板</dt><dd>{draft.template_id || 'custom'}</dd><dt>扫描周期</dt><dd>{execution.scan_interval_minutes} 分钟</dd><dt>交易范围</dt><dd>{execution.universe_mode === 'ALL' ? '交易所全品种' : `${execution.symbols.length} 个自选`}</dd><dt>订单路径</dt><dd>{draft.profile?.limit_priority ? '限价优先 / 市价确认' : orderLabel(execution.order_preference)}</dd><dt>用户杠杆上限</dt><dd>{execution.leverage}×</dd><dt>单笔风险</dt><dd>≤ {execution.risk_per_trade_pct}%</dd><dt>仓位金额上限</dt><dd>{execution.max_notional_usdt} USDT</dd><dt>同时持仓</dt><dd>≤ {execution.max_positions}</dd><dt>组合风险硬上限</dt><dd>1%</dd><dt>日亏损熔断</dt><dd>1.5%</dd></dl><p>名义金额 = 数量 × 合约乘数 × 价格。保证金约等于名义金额 ÷ 杠杆；杠杆不会放大允许的止损损失。</p></div></aside>
      <form onSubmit={event => { event.preventDefault(); void save(); }}><fieldset disabled={busy} className="ai-studio-fields"><label>策略名称<input value={draft.name} maxLength={80} required onChange={event => setDraft({ ...draft, name: event.target.value })} /></label>
        <section className="ai-config-rehearsal" aria-labelledby={`${tabIdPrefix}-nofx-import-title`}>
          <header><div><small>NOFX STRATEGY MIGRATION</small><h3 id={`${tabIdPrefix}-nofx-import-title`}>导入 NOFX 策略 JSON</h3></div><span>配置迁移</span></header>
          <p>选择 NOFX 导出的策略配置 JSON。兼容的策略文本、5m/15m 节奏、多周期、指标开关和排除项会进入当前策略；原始文件不会留存。</p>
          <p>每轮本地扫描器计算 K 线指标并生成候选，Bonsai 结合新闻、技术面和仓位独立决定开仓或等待；风险检查与 Gate 委托仍由本项目处理，不运行 NOFX 原生执行器。</p>
          <p>NOFX 外部选币源会明确标为未映射；策略使用 Gate 活跃 USDT 永续合约池并轮换分析。杠杆、金额和其他风控继续取当前账户配置及 Gate 合约规则。</p>
          <input ref={nofxFileInput} type="file" accept=".json,application/json" aria-label="选择 NOFX 策略 JSON 文件" hidden onChange={event => { const file = event.currentTarget.files?.[0]; event.currentTarget.value = ''; void importNofxJson(file); }} />
          <button type="button" disabled={busy} onClick={() => nofxFileInput.current?.click()}>{busyAction === 'import' ? '正在导入…' : '选择 JSON 并导入'}</button>
          {nofxImportFeedback && <div className="ai-config-check-result" data-ok={nofxImportFeedback.kind === 'success'} role={nofxImportFeedback.kind === 'success' ? 'status' : 'alert'}>
            <strong>{nofxImportFeedback.kind === 'success' ? '导入完成' : '导入失败'}</strong>
            <p>{nofxImportFeedback.message}</p>
            {nofxImportFeedback.kind === 'success' && <>
              <p>仅后端报告的兼容字段已映射；Gate 风控与金额配置仍按当前账户策略执行。</p>
              {Boolean(nofxImportFeedback.truncatedFields?.length) && <div className="ai-config-import-warning" role="note"><strong>部分内容已按提示长度上限截断</strong><ul>{nofxImportFeedback.truncatedFields?.map(field => <li key={field}>{labels[field] || field}</li>)}</ul><small>请检查下方已保存的策略文本，确认关键条件没有被截掉。</small></div>}
              <strong>已导入字段</strong><ul>{nofxImportFeedback.importedFields?.length ? nofxImportFeedback.importedFields.map(field => <li key={field}>{field}</li>) : <li>后端未报告可导入字段</li>}</ul>
              <strong>未导入字段</strong><ul>{nofxImportFeedback.ignoredFields?.length ? nofxImportFeedback.ignoredFields.map(field => <li key={field}>{field}</li>) : <li>后端未报告忽略字段</li>}</ul>
            </>}
          </div>}
        </section>
        <div className="ai-studio-tabs" role="tablist" aria-label="策略配置分类">{tabs.map((value, index) => <button key={value} id={`${tabIdPrefix}-tab-${index}`} type="button" role="tab" aria-selected={tab === value} aria-controls={`${tabIdPrefix}-panel`} tabIndex={tab === value ? 0 : -1} onClick={() => setTab(value)} onKeyDown={event => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
          event.preventDefault();
          const tabButtons = Array.from(event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]') || []);
          const currentIndex = tabButtons.indexOf(event.currentTarget);
          const nextIndex = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (currentIndex + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
          setTab(tabs[nextIndex]);
          tabButtons[nextIndex]?.focus();
        }}>{value}</button>)}</div>
        <div id={`${tabIdPrefix}-panel`} role="tabpanel" aria-labelledby={`${tabIdPrefix}-tab-${tabs.indexOf(tab)}`} tabIndex={0}>
        {tab === '资金与风控' && <><div className="ai-studio-grid"><label>仓位计算方式<select value={execution.sizing_mode} onChange={event => setSetting('sizing_mode', event.target.value)}><option value="RISK_BASED">按止损风险计算</option><option value="FIXED_NOTIONAL">固定 USDT 名义金额</option><option value="EQUITY_PERCENT">账户权益比例</option></select><small>最终仓位同时受止损风险、金额上限与保证金约束。</small></label>
        {execution.sizing_mode === 'FIXED_NOTIONAL' && numeric('fixed_notional_usdt', '每笔名义金额（USDT）', 10, execution.max_notional_usdt, 10, '是合约价值，不是投入保证金。')}
        {execution.sizing_mode === 'EQUITY_PERCENT' && numeric('equity_notional_pct', '每笔名义金额 / 权益（%）', .1, 100, .1)}
        {numeric('leverage', '用户允许的杠杆上限（倍）', 1, 100, 1, '实际杠杆取 AI 请求、此上限、Gate 合约上限和止损距离风控上限中的最低值。')}
        {numeric('max_notional_usdt', '单笔名义金额上限（USDT）', 10, 1000000, 10)}
        {numeric('risk_per_trade_pct', '每笔止损风险 / 权益（%）', .01, .25, .01, '已知费用和滑点计入实际风险预算。')}
        {numeric('max_positions', '最大同时持仓数', 1, 5)}
        {numeric('max_margin_pct', '总保证金占用上限（%）', 1, 80)}
        {numeric('min_net_rr', '最低扣费后盈亏比', 1.5, 10, .1)}
        {numeric('min_confidence', '最低 AI 置信分数', 60, 100, 1, '置信分数不是胜率，也不使用固定占位分。')}
        {numeric('cooldown_minutes', '同币种重入冷却（分钟）', 0, 1440)}
        <fieldset className="ai-risk-switches"><legend>机构级动态风控与防插针体系</legend>
          <div className="ai-risk-badge-note">🛡️ <strong>结构止损与限价距离闸门已激活</strong>：ATR 仓位按所选模板计算，挂单距离和 TTL 由代码复核，浮盈后由保护计划收紧风险。</div>
          <label><input type="checkbox" checked={execution.atr_adaptive_sizing} onChange={event => setSetting('atr_adaptive_sizing', event.target.checked)} /><span>ATR / 止损距离自适应仓位，保持单笔亏损预算恒定</span></label>
          <label><input type="checkbox" checked={execution.consecutive_loss_lock_enabled} onChange={event => setSetting('consecutive_loss_lock_enabled', event.target.checked)} /><span>2 连损触发 2 小时冷板凳静默锁</span></label>
          <label><input type="checkbox" checked={execution.us_open_defense_enabled} onChange={event => setSetting('us_open_defense_enabled', event.target.checked)} /><span>美股常规开盘前 15 分钟禁止新增风险（自动处理夏令时）</span></label>
        </fieldset>
        </div><section className="ai-sizing-preview"><h3>仓位试算</h3><p>使用 Gate TestNet 当前账户权益。仅作配置预览，实际以本轮价格、止损和交易所精度重新计算。</p><label>假设止损距离（%）<input type="number" min={.1} max={50} step={.1} value={stopDistance} onChange={event => setStopDistance(Number(event.target.value))} /></label><div className="ai-studio-preview-grid"><div><small>账户权益</small><strong>{money(equity)}</strong></div><div><small>名义仓位估算</small><strong>{money(estimate)}</strong></div><div><small>按用户上限估算保证金</small><strong>{money(estimate !== null ? estimate / execution.leverage : null)}</strong></div><div><small>止损风险上限</small><strong>{money(riskBudget)}</strong></div></div><p>此试算尚未扣除交易费用及滑点，最终数量可能更小。账户观测：{capital?.observed_at || capital?.error_code || capital?.data_status || '同步中'}</p></section></>}
        {tab === '交易范围' && <div className="ai-studio-grid"><fieldset className="ai-symbols"><legend>交易所合约范围</legend><label>扫描范围<select value={execution.universe_mode || 'ALL'} onChange={event => setSetting('universe_mode', event.target.value)}><option value="ALL">全部可交易 USDT 永续合约</option><option value="CUSTOM">自选合约</option></select></label><p>已发现 {markets.length} 个当前环境的合约。{execution.universe_mode === 'ALL' ? '全量活跃 USDT 永续合约均可进入候选池；每轮优先检查持仓，再从流动性、涨跌幅和波动榜轮换最多 3 个交给 AI 深度分析。' : '仅在自选合约中筛选；每轮优先检查持仓，再轮换最多 3 个交给 AI 深度分析。'}不等于 AI 每轮逐个分析全部合约。</p>{marketError && <p role="status">{marketError}</p>}{execution.universe_mode === 'CUSTOM' && <><label>搜索合约<input value={search} placeholder="输入币种，例如 DOGE" onChange={event => setSearch(event.target.value.toUpperCase())} /></label><div className="ai-market-options">{markets.filter(item => item.symbol.includes(search)).slice(0, 80).map(({ symbol }) => <label key={symbol}><input type="checkbox" checked={execution.symbols.includes(symbol)} onChange={event => setSetting('symbols', event.target.checked ? [...execution.symbols, symbol] : execution.symbols.filter(item => item !== symbol))} />{symbol}</label>)}</div><small>显示前 80 个匹配结果，可搜索全部合约。已选 {execution.symbols.length} 个。</small></>}</fieldset><label>交易方向<select value={execution.direction} onChange={event => setSetting('direction', event.target.value)}><option value="BOTH">允许做多与做空</option><option value="LONG_ONLY">仅做多</option><option value="SHORT_ONLY">仅做空</option></select></label><label>订单偏好<select value={String(draft.profile?.order_preference || execution.order_preference).toUpperCase()} disabled={Boolean(draft.profile?.order_preference && String(draft.profile.order_preference).toUpperCase() !== 'AUTO')} onChange={event => setSetting('order_preference', event.target.value)}><option value="AUTO">自动选择</option><option value="LIMIT">限价单</option><option value="MARKET">市价单</option></select><small>{draft.profile?.order_preference && String(draft.profile.order_preference).toUpperCase() !== 'AUTO' ? `模板已锁定为${orderLabel(String(draft.profile.order_preference))}，模型不会改成另一种开仓方式。` : '市价单仍需通过流动性和滑点检查。'}</small></label><div><h3>策略扫描周期</h3><label>扫描间隔<select value={execution.scan_interval_minutes || 15} disabled={Boolean(draft.profile?.signal_timeframe)} onChange={event => setSetting('scan_interval_minutes', Number(event.target.value))}><option value={5}>5 分钟</option><option value={15}>15 分钟</option></select></label><p>{draft.profile?.signal_timeframe ? `模板信号周期固定为 ${draft.profile.signal_timeframe}，执行器会在对应的整点对齐。` : `对齐每小时的 ${execution.scan_interval_minutes === 5 ? '00 / 05 / 10 / 15 …' : '00 / 15 / 30 / 45'} 分钟。`}启动或恢复后等待下个时间点；不补跑错过的轮次、不重叠分析。仅使用已收盘数据。</p></div><div><h3>退出与保护</h3><p>开仓必须包含止损、止盈和失效条件。AI 可收紧止损、减仓或平仓；不能放宽已有止损。保护单由交易网关和独立守护器执行。</p></div></div>}
        {tab === 'AI 决策指令' && <div className="ai-studio-prompts ai-prompt-workbench">
          <div className="ai-prompt-editor">
            <p>实际触发周期由「交易范围」中的扫描间隔决定。文本预览按所选模板和后端归一规则生成；固定系统规则由决策服务注入，这里不冒充完整运行时模型请求。</p>
            {Object.entries(labels).map(([key, label]) => {
              const value = draft.sections[key] || '';
              const effectiveText = effectivePromptSections?.[key] ?? value;
              const limit = strategyTextLimits[key];
              const effectiveLength = Array.from(effectiveText).slice(0, limit).length;
              const overLimit = Math.max(0, Array.from(effectiveText).length - limit);
              const normalizedByTemplate = Boolean(promptTemplate && effectiveText !== value);
              return <label key={key}>
                {label}
                <textarea required maxLength={limit} rows={key === 'entry_standards' || key === 'decision_process' ? 6 : 4} value={value} onChange={event => setDraft({ ...draft, sections: { ...draft.sections, [key]: event.target.value } })} />
                <span className="ai-prompt-counter"><span>{promptTemplate ? `送入策略提示：${effectiveLength}/${limit} 字` : `等待模板 · 当前文本 ${Array.from(value).length} 字`}</span>{normalizedByTemplate && <strong>周期与模板冲突：后端会使用模板文案</strong>}{overLimit > 0 && <strong>超出 {overLimit} 字不会送入</strong>}</span>
                <span className="ai-prompt-meter" aria-hidden="true"><i style={{ width: `${Math.min(100, effectiveLength / limit * 100)}%` }} /></span>
              </label>;
            })}
          </div>
          <aside className="ai-prompt-sidecar">
            <section className="ai-prompt-preview">
              <header><div><small>STRATEGY INSTRUCTION / LOCAL PREVIEW</small><h3>本地策略片段预览</h3></div><span>草稿即时预览</span></header>
              <p>按后端模板回退、频率冲突修复及字符截取规则显示用户策略部分；不会包含本轮行情证据，也不是完整运行时模型请求。</p>
              <pre>{strategyPromptPreview}</pre>
              <div className="ai-server-preview-action"><button type="button" disabled={serverPreviewBusy} onClick={() => void requestServerPreview()}>{serverPreviewBusy ? '生成后端静态预览…' : '生成后端静态预览'}</button><small>只读接口 · 不调用 Bonsai · 不读取行情 · 不下单</small></div>
              {currentServerPreview?.error && <p className="ai-server-preview-error" role="alert">后端预览失败：{currentServerPreview.error}</p>}
              {currentServerPreview?.data && <div className="ai-production-preview">
                <header role="status" aria-live="polite" aria-atomic="true"><strong>{currentServerPreview.data.valid ? '后端配置校验通过' : '后端发现配置问题'}</strong><span>{currentServerPreview.data.preview_kind}</span></header>
                <div className="ai-server-summary">{Object.entries(currentServerPreview.data.summary).map(([key, value]) => <div key={key}><small>{key}</small><strong>{String(value ?? '—')}</strong></div>)}</div>
                {currentServerPreview.data.validation_errors.length > 0 && <ul className="ai-server-preview-errors">{currentServerPreview.data.validation_errors.map(error => <li key={error}>{error}</li>)}</ul>}
                <h4>生产策略 System Instruction</h4><pre>{currentServerPreview.data.strategy_system_instruction}</pre>
                <h4>边界说明</h4><ul className="ai-server-preview-limitations">{currentServerPreview.data.limitations.map(item => <li key={item}>{item}</li>)}</ul>
                <small>模型调用：{currentServerPreview.data.model_called ? '是' : '否'} · 交易执行：{currentServerPreview.data.execution_called ? '是' : '否'}</small>
              </div>}
            </section>
            <section className="ai-config-rehearsal">
              <header><div><small>DRY RUN / NO ORDERS</small><h3>配置安全演练</h3></div><span>本地检查</span></header>
              <p>只校验策略草稿字段完整度，不调用 AI、不读取实时行情、不访问 Gate，也不会下单。</p>
              <button type="button" onClick={() => setLocalCheckState({ fingerprint: draftFingerprint, result: inspectStrategyDraft(draft) })}>运行本地配置检查</button>
              {currentLocalCheck && <div className="ai-config-check-result" role="status" data-ok={currentLocalCheck.blockers.length === 0}><strong>{currentLocalCheck.blockers.length === 0 ? '配置字段检查通过' : `发现 ${currentLocalCheck.blockers.length} 项需修正`}</strong><ul>{(currentLocalCheck.blockers.length ? currentLocalCheck.blockers : currentLocalCheck.passed).map(item => <li key={item}>{item}</li>)}</ul><small>此结果不代表 Bonsai、行情、新闻或交易所连接已就绪。</small></div>}
            </section>
          </aside>
        </div>}
        {tab === '配置预览' && <><p>下面的配置随每次模型输入和决策记录保存。新闻属于证据，不能覆盖资金规则或策略指令。</p><pre className="ai-studio-json">{JSON.stringify({ name: draft.name, execution: draft.execution, sections: draft.sections }, null, 2)}</pre></>}
        </div></fieldset><footer><span>保存后下一轮生效。当前轮次使用已冻结的配置。</span><button type="submit" disabled={busy || (execution.universe_mode === 'CUSTOM' && execution.symbols.length === 0)}>{busy ? busyAction === 'import' ? '导入中…' : '保存中…' : '保存并启用'}</button></footer></form></div> : !accountId ? <p>请先选择 Gate TestNet 账户。</p> : message ? <div className="ai-strategy-error-box" role="alert"><p className="ai-strategy-message">{message}</p><button type="button" onClick={() => setReloadKey(k => k + 1)} className="ai-strategy-retry-button">重试读取策略</button></div> : <p role="status">正在读取账户策略…</p>}
    {library && message && <p className="ai-strategy-message" role="status">{message}</p>}
  </section>;
}
