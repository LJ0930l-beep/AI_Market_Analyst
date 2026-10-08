"""Run a frozen one-year proxy; outputs are explicitly not AI/Gate returns."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.replay.technical_proxy import canonical, frozen_proxy_config, run_proxy, sha


def render_proxy_report(report, output):
    esc = html.escape
    rows = []
    for item in report["results"]:
        roi = "未定义" if item["roi"] is None else f'{item["roi"]*100:+.2f}%'
        win = "无完整平仓样本" if item["win_rate"] is None else f'{item["win_rate"]*100:.1f}%'
        rows.append(f'<tr><td>{esc(item["name"])}</td><td>{roi}</td><td>{win}</td>'
                    f'<td>{item["closed_trade_count"]}</td><td>{item["max_drawdown"]*100:.2f}%</td>'
                    f'<td>{item["fees"]:.2f}</td><td>{item["funding_pnl"]:+.2f}</td>'
                    f'<td>{item["counts"].get("proposals",0)} / {item["counts"].get("fills",0)}</td></tr>')
    rules = ''.join(f'<li><strong>{esc(t["name"])}</strong>：{esc(t["rules"])}</li>' for t in report["config"]["templates"])
    limitations = ''.join(f'<li>{esc(t)}</li>' for t in report["limitations"])
    body = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>一年 BTC/ETH · 五套技术规则代理回测</title><style>
body{{background:#f2efe6;color:#293c32;font:15px/1.65 system-ui,'Microsoft YaHei',sans-serif;margin:0}}main{{max-width:1300px;margin:auto;padding:36px 24px}}
h1{{font-size:28px;border-bottom:2px solid #314438;padding-bottom:18px}}.notice{{background:#ebe2cd;border-left:4px solid #a77a23;padding:18px;margin:24px 0}}
.box{{overflow:auto}}table{{border-collapse:collapse;white-space:nowrap;width:100%;background:#fcf9f2}}td,th{{padding:16px;border:1px solid #cbc6b7;text-align:right}}td:first-child,th:first-child{{text-align:left}}
th{{background:#e5e3d8}}small{{font-family:monospace}}li{{margin:12px 0}}details{{border:1px solid #cbc6b7;padding:18px;margin-top:24px}}
</style><main><small>RESEARCH / RULE PROXY / AI CALLS: 0</small><h1>一年 BTC、ETH，五套技术规则代理比较</h1>
<div class="notice"><strong>这是固定规则代理结果，不是 Bonsai 自主交易胜率，也不是 Gate 实盘收益。</strong>
<p>区间：{esc(report["window_start"])} → {esc(report["window_end"])}。数据：币安 USD-M 永续官方档案。</p>
<p>每套独立 1000 USDT，同时比较 BTC/ETH 并选择当轮一笔机会；每笔固定 250 USDT 名义价值、10x 研究杠杆。参数在查看全年收益前冻结，未做收益优化。</p>
<p>收益率含期末浮动盈亏、手续费、资金费率；胜率只统计扣费后的完整平仓。</p></div>
<div class="box"><table><tr><th>策略代理</th><th>收益率</th><th>净平仓胜率</th><th>平仓笔数</th><th>分钟收盘回撤</th><th>手续费 USDT</th><th>资金费率损益</th><th>提案 / 成交</th></tr>{''.join(rows)}</table></div>
<details open><summary>冻结规则翻译</summary><ul>{rules}</ul></details>
<details><summary>重要边界与成交假设</summary><ul>{limitations}</ul></details>
<p>来源：<a href="https://github.com/binance/binance-public-data">Binance 官方公共档案说明</a>。50 个 ZIP 已通过官方 SHA256 校验，分钟数据及资金费率覆盖见 manifest.json。</p>
<small>configuration_sha256: {esc(report["config_sha256"])}<br>dataset_sha256: {esc(report["data_manifest"]["dataset_sha256"])}</small></main></html>'''
    output.write_text(body, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    # Be polite to the GPU model's host threads while processing historical rows.
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        if not kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x4000):
            raise OSError(ctypes.get_last_error(), "cannot lower research process priority")
    directory = args.directory.resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    database = directory / "research.sqlite3"
    if hashlib.sha256(database.read_bytes()).hexdigest() != manifest["dataset_sha256"]:
        raise ValueError("PROXY_DATASET_HASH_MISMATCH")
    config_path = args.config or directory / "proxy-config.json"
    if args.config:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        config = frozen_proxy_config()
        if config_path.exists() and config_path.read_text(encoding="utf-8") != canonical(config):
            raise ValueError("PROXY_EXISTING_CONFIG_CHANGED_CREATE_NEW_RUN")
        config_path.write_text(canonical(config), encoding="utf-8")
    print(json.dumps({"status": "STARTED", "config_sha256": sha(config), "ai_calls": 0}, ensure_ascii=False), flush=True)
    report = run_proxy(database, manifest, config, progress=lambda item: print(json.dumps(item), flush=True))
    report["observed_at"] = datetime.now(timezone.utc).isoformat()
    report["proxy_source_sha256"] = hashlib.sha256(Path(__file__).resolve().parents[1].joinpath("core/replay/technical_proxy.py").read_bytes()).hexdigest()
    (directory / "proxy-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    render_proxy_report(report, directory / "proxy-comparison.html")
    print(json.dumps({"status": report["status"], "results": [{k:r[k] for k in ("template_id", "roi", "win_rate", "closed_trade_count", "fees", "funding_pnl")} for r in report["results"]]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
