import './decisionFlow.css';

const STAGES = [
  ['FLOW', '行情与新闻'],
  ['SIGNAL', '候选结构'],
  ['DECISION', '模型判断'],
  ['EXECUTE', '交易所回执'],
  ['HOLD', '持仓与委托'],
] as const;

export function DecisionFlow({ running = false }: { running?: boolean }) {
  return <section className="decision-flow" aria-label="AI 交易链路示意">
    <header className="decision-flow__header">
      <div><span className="decision-flow__eyebrow">ORCHESTRATION TOPOLOGY</span><h2>从证据到交易所</h2></div>
      <span className="decision-flow__status">{running ? '● AI 扫描运行中' : '○ 等待 AI 会话'}</span>
    </header>
    <p>显示系统处理顺序；光点仅表示流程，不代表已成交订单。</p>
    <div className="decision-flow__rail" data-running={running}>
      {STAGES.map(([name, label], index) => <div className="decision-flow__stage" key={name}>
        <div className="decision-flow__stage-head"><strong>{name}</strong><span>{label}</span></div>
        <div className="decision-flow__nodes" aria-hidden="true">
          {Array.from({ length: 12 }, (_, dot) => <i key={dot} style={{ animationDelay: `${(index * 0.21 + dot * 0.08).toFixed(2)}s` }} />)}
        </div>
      </div>)}
    </div>
  </section>;
}
