import { useEffect, useState } from "react";
import { apiClient } from "../api/client";
import { formatTradingTime } from "../tradingTime";
import "./decisionExperience.css";

interface DecisionMemory {
  memory_id: string;
  symbol?: string;
  action?: string;
  cycle_status?: string;
  decision_at?: string;
  summary_zh?: string;
  lesson_zh?: string | null;
  outcome_status?: string | null;
  outcome_pnl?: number | null;
  strategy_name?: string;
  strategy_template_id?: string;
  outcome_evidence?: { basis?: string | null; position_id?: string | null; gross_realized?: number | null; fees?: number | null; fill_count?: number | null };
}

export function DecisionExperiencePanel({ accountId }: { accountId: string }) {
  const [items, setItems] = useState<DecisionMemory[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    setItems(null);
    setError("");
    if (!accountId) return () => { active = false; };
    const load = () => apiClient.v2<{ items?: DecisionMemory[] }>(`/ai-session/memory?account_id=${encodeURIComponent(accountId)}&limit=20`)
      .then(result => { if (active) setItems(Array.isArray(result.items) ? result.items : []); })
      .catch(reason => { if (active) { setError(reason instanceof Error ? reason.message : "经验记录暂不可用"); setItems([]); } });
    void load();
    const timer = window.setInterval(load, 30000);
    return () => { active = false; window.clearInterval(timer); };
  }, [accountId]);

  const closed = (items || []).filter(item => ["WIN", "LOSS", "FLAT"].includes(String(item.outcome_status || "").toUpperCase()));
  return <section className="terminal-panel decision-experience" aria-label="AI 平仓复盘与长期经验">
    <header className="v2-panel-header">
      <div><span className="decision-experience__eyebrow">AI MEMORY / CLOSED TRADE REVIEW</span><h2>🧾 平仓复盘与长期经验</h2></div>
      <span className="v2-data-tag">{items === null ? "读取中" : `${closed.length} 笔已结算 · ${items.length} 条记忆`}</span>
    </header>
    {error && <p className="v2-warning" role="status">经验接口不可用：{error}</p>}
    {items === null ? <p className="v2-note">正在读取当前账户的决策记忆…</p> : items.length === 0 ? <p className="v2-note">当前账户暂无 AI 决策记忆。平仓后会根据可核验的成交账本回填净盈亏与经验。</p> : <div className="decision-experience__list">
      {items.map(item => {
        const outcome = String(item.outcome_status || "PENDING").toUpperCase();
        const settled = ["WIN", "LOSS", "FLAT"].includes(outcome);
        return <details key={item.memory_id} className="decision-experience__item" data-outcome={outcome}>
          <summary><span className="decision-experience__symbol">{item.symbol || "未知标的"}</span><span className="decision-experience__action">{item.action || "UNKNOWN"}</span><span className="decision-experience__strategy">{item.strategy_name || item.strategy_template_id || "自定义策略"}</span><span className="decision-experience__pnl">{settled && item.outcome_pnl != null ? `${Number(item.outcome_pnl) >= 0 ? "+" : ""}${Number(item.outcome_pnl).toFixed(2)} USDT` : settled ? outcome : "等待平仓结算"}</span><time>{item.decision_at ? formatTradingTime(item.decision_at) : "时间未提供"}</time></summary>
          <div className="decision-experience__body"><p>{item.summary_zh || "该周期没有摘要。"}</p>{settled ? <><strong>结算经验</strong><p>{item.lesson_zh || `账本结算结果：${outcome}。`}</p>{item.outcome_evidence?.position_id && <small>持仓 {item.outcome_evidence.position_id} · 毛盈亏 {item.outcome_evidence.gross_realized == null ? "—" : `${Number(item.outcome_evidence.gross_realized).toFixed(2)} USDT`} · 费用 {item.outcome_evidence.fees == null ? "—" : `${Number(item.outcome_evidence.fees).toFixed(2)} USDT`} · 成交 {item.outcome_evidence.fill_count ?? "—"} 笔</small>}<small>结果来自本地成交镜像的净盈亏归集；当前记忆未提供止损/止盈触发诱因或独立滑点归因。</small></> : <p className="decision-experience__pending">该项尚无已结算持仓回执，不推断盈亏或复盘结论。</p>}</div>
        </details>;
      })}
    </div>}
  </section>;
}
