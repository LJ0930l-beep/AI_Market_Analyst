"""A standalone, source-backed comparison report for isolated AI replay runs."""
from __future__ import annotations

import html
import json
from pathlib import Path


def render_ai_template_report(report: dict, output: str | Path) -> Path:
    def esc(value):
        return html.escape(str(value), quote=True)

    def pct(value):
        return "—" if value is None else f"{float(value)*100:+.2f}%"

    def amount(value, *, digits=2, signed=False):
        if value is None:
            return "—"
        return format(float(value), f"+,.{digits}f" if signed else f",.{digits}f")

    rows = []
    participation_rows = []
    for item in report.get("results", []):
        closed = int(item.get("closed_trade_count", 0))
        rate = item.get("win_rate")
        win = "无已平仓样本" if rate is None else f"{float(rate)*100:.1f}%"
        roi = item.get("roi", 0)
        valid_economics = item.get("economic_eligible", True)
        warning = f"<small class=negative>{esc(item.get('halted_reason') or '经济路径未覆盖')}</small>" if not valid_economics else ""
        rows.append(f"<tr><td><strong>{esc(item.get('name', item['template_id']))}</strong>"
                    f"<small>{esc(item['template_id'])}</small>{warning}</td>"
                    f"<td class={'positive' if roi is not None and float(roi) >= 0 else 'negative'}>{pct(roi)}</td>"
                    f"<td>{esc(win)}</td><td>{closed}</td>"
                    f"<td>{pct(None if item.get('max_drawdown') is None else -float(item.get('max_drawdown',0)))}</td>"
                    f"<td>{amount(item.get('ending_equity',0))}</td>"
                    f"<td>{amount(item.get('fees',0), digits=4)}</td>"
                    f"<td>{amount(item.get('funding_pnl',0), digits=4, signed=True)}</td>"
                    f"<td>{int(item.get('pending_order_count',0))}</td></tr>")
        actions = item.get("action_counts") or {}
        calls = int(item.get("decision_count", 0))
        proposals = sum(int(actions.get(key, 0)) for key in ("OPEN_LONG", "OPEN_SHORT"))
        waiting = sum(int(actions.get(key, 0)) for key in ("WAIT", "HOLD"))
        accepted = int(item.get("accepted_entry_order_count", 0))
        filled = int(item.get("filled_entry_order_count", 0))
        proposal_rate = "—" if not calls else f"{proposals / calls * 100:.1f}%"
        fill_rate = "无已接受开仓委托" if not accepted else f"{filled / accepted * 100:.1f}%"
        participation_rows.append(f"<tr><td>{esc(item.get('name', item['template_id']))}</td>"
            f"<td>{calls}</td><td>{waiting}</td><td>{proposals} / {proposal_rate}</td>"
            f"<td>{accepted}</td><td>{filled} / {esc(fill_rate)}</td>"
            f"<td>{int(item.get('expired_orders',0))}</td></tr>")
    status = str(report.get("status", "UNKNOWN"))
    complete = bool(report.get("complete_window"))
    qualified = bool(report.get("comparison_eligible"))
    state_text = "历史区间已跑完" if complete else "测试进行中，当前数值仅为中途快照"
    if status == "STOPPED_FOR_INPUT_REFERENCE_REPAIR":
        state_text = "基线已因输入参照缺陷停止，当前数值不是完整收益结果"
    if complete and not qualified:
        state_text += "；存在数据、调用或账本缺口，尚不能作完整比较"
    reasons = [f"{item.get('template_id')}: {item.get('error')}" for item in report.get("errors", [])]
    assumptions = report.get("assumptions", {})
    if isinstance(assumptions, dict):
        assumptions = [f"{key}: {value}" for key, value in assumptions.items()]
    assumptions = [*assumptions, *report.get("limitations", [])]
    safe_json = html.escape(json.dumps(report, ensure_ascii=False, indent=2), quote=False)
    page = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>五套 AI 策略 · 历史收益比较</title>
<style>
:root{{color-scheme:light;--paper:#f3f0e8;--ink:#263831;--line:#cfcabc;--green:#237452;--red:#af4141}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.65 system-ui,'Microsoft YaHei',sans-serif}}
main{{max-width:1400px;margin:auto;padding:44px 28px}}header{{border-bottom:2px solid var(--ink);padding-bottom:24px;display:flex;gap:24px;justify-content:space-between;align-items:flex-end}}
h1{{font-size:32px;line-height:1.25;letter-spacing:-.8px;margin:8px 0}}p{{margin:8px 0}}.eyebrow,small,.meta{{font:12px/1.6 ui-monospace,monospace;letter-spacing:.8px}}small{{display:block;color:#647067;letter-spacing:0;margin-top:5px}}
.badge{{padding:6px 12px;border:1px solid var(--line);border-radius:4px;font-size:12px;white-space:nowrap;background:#e6ecdf}}
.notice{{margin:24px 0;padding:18px 22px;border-left:4px solid #a97b27;background:#ece5d4}}.metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);border:1px solid var(--line);margin:24px 0}}
.metric{{background:#faf7f0;padding:16px 20px}}.metric strong{{font-size:22px;display:block}}.table-box{{overflow-x:auto;border:1px solid var(--line);background:#faf8f1}}table{{width:100%;border-collapse:collapse;white-space:nowrap}}
th{{font-size:12px;background:#e9e6dd;text-align:left;color:#59675c}}th,td{{padding:17px 18px;border-bottom:1px solid var(--line)}}td:not(:first-child){{font-family:ui-monospace,monospace}}tr:last-child td{{border-bottom:0}}
.positive{{color:var(--green)}}.negative{{color:var(--red)}}section{{margin-top:28px}}h2{{font-size:18px;border-bottom:1px solid var(--line);padding-bottom:10px}}
details{{border:1px solid var(--line);padding:14px 18px;margin-top:18px;background:#faf8f1}}summary{{cursor:pointer}}pre{{white-space:pre-wrap;word-break:break-word;font-size:12px;max-height:500px;overflow:auto}}
li{{margin:7px 0}}.source{{overflow-wrap:anywhere;color:#667065;font:12px/1.7 ui-monospace,monospace}}@media(max-width:700px){{main{{padding:24px 14px}}header{{display:block}}h1{{font-size:26px}}.metrics{{grid-template-columns:repeat(2,1fr)}}}}
</style><main>
<header><div><div class="eyebrow">AI MARKET ANALYST / FIVE STRATEGY REPLAY</div>
<h1>五套 AI 策略，放在同一段行情里比较</h1><p>真实 Gemini 决策 · 独立模拟账户 · 原生扫描周期</p></div>
<span class="badge">历史模拟 / {esc(status)}</span></header>
<div class="notice"><strong>{esc(state_text)}</strong><p>收益率含已实现与期末未实现盈亏及已计费用。胜率仅统计扣费后的完整平仓；零成交时不显示虚构的 0% 胜率。历史结果不等于实盘收益。</p>
<p>比较范围：内置策略模板及冻结的默认配置。每套独立初始资金 {amount(report.get('common_initial_equity_usdt'))} USDT，不代表当前实盘账户收益。</p></div>
<div class="metrics"><div class="metric"><small>已处理决策</small><strong>{int(report.get('decision_count',0))}</strong></div>
<div class="metric"><small>历史行情完整</small><strong>{'是' if report.get('complete_data') else '否'}</strong></div>
<div class="metric"><small>模型调用错误</small><strong>{len(reasons)}</strong></div>
<div class="metric"><small>决策来源</small><strong>{esc(report.get('decision_source','UNKNOWN'))}</strong></div></div>
<p class="meta">区间：{esc(report.get('window_start',''))} → {esc(report.get('window_end',''))}<br>
已评估至：{esc(report.get('evaluated_through',''))}</p>
<div class="table-box"><table><thead><tr><th>策略</th><th>账户收益率</th><th>平仓胜率</th><th>平仓笔数</th><th>分钟收盘回撤</th><th>期末权益 USDT</th><th>手续费 USDT</th><th>资金费率损益</th><th>待成交挂单</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<section><h2>开单频率与成交转化</h2><p>把模型等待、开仓提案、网关接受和撮合成交分开统计。部分成交按订单 ID 去重计一笔；提案率分母包含错误轮次，成交率分母为已接受开仓委托。挂单成交不等于完整平仓。</p>
<div class="table-box"><table><thead><tr><th>策略</th><th>决策轮次</th><th>WAIT / HOLD</th><th>开仓提案 / 比例</th><th>接受开仓委托</th><th>有成交订单 / 比例</th><th>过期委托</th></tr></thead><tbody>{''.join(participation_rows)}</tbody></table></div></section>
<section><h2>计算口径</h2><p>收益率 = 期末账户权益 ÷ 初始权益 − 1；胜率 = 净盈利完整平仓笔数 ÷ 全部完整平仓笔数。未平仓仓位计入账户权益，但不计入胜率。</p>
<p>每套策略分别维护资金、挂单、持仓及保护；5m 与 15m 扫描周期分别执行，不把扫描次数当成交易次数。</p></section>
<p>回撤基于分钟收盘权益，不能表示盘中最大回撤。样本较少时，收益与胜率仅作初步比较；不支持的强平或持仓模式路径会停止并标明，不补造完整收益。</p>
<details><summary>数据与成交假设</summary><ul>{''.join('<li>'+esc(value)+'</li>' for value in assumptions)}</ul></details>
<details><summary>模型或账本错误记录（{len(reasons)}）</summary><ul>{''.join('<li>'+esc(value)+'</li>' for value in reasons)}</ul></details>
<details><summary>完整结果 JSON</summary><pre>{safe_json}</pre></details>
<section class="source">run_id: {esc(report.get('run_id',''))}<br>history_sha256: {esc(report.get('manifest_sha256',''))}<br>
configuration_sha256: {esc(report.get('config_sha256',''))}<br>source: {esc(report.get('source',''))}</section></main></html>"""
    target = Path(output).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")
    return target
