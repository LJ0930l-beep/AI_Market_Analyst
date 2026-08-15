# AI Market Analyst — Phase 2 最终完成报告

**验收结论：`PHASE 2 PASS`**  
**验收日期：2026-08-15（Asia/Shanghai）**  
**项目目录：`D:\RJ\codex\ai-market-analyst`**  
**范围：仅 Phase 2；未加入 Phase 3 功能。**

## 1. 最终结论

Phase 2 已完成并通过最终验收：

- Ollama Windows 客户端已安装，版本 `0.32.11`；Qwen3.5-4B 已真实注册并完成 GPU 推理。
- 六标的真实链路 `Provider → Quant → News → Structured Context → Qwen → Validator → Signal` 已严格跑通，`6/6` 成功、`failures=0`。
- 真实 Qwen 原始 JSON `6/6` 通过字段、类型、WAIT 无价格级别和动作级别校验。
- `LONG/SHORT/WAIT` 合同级 schema 测试通过；真实当前市场窗口实际产生 `SHORT` 与 `WAIT`，没有人为注入 LONG。
- NVDA/BTCUSDT 已完成真实历史回放；BTCUSDT 真实 SHORT 被跟随到 PaperTrade，并用下一根真实 K 线产生 Outcome。
- `compileall` 通过，完整测试集 `32/32` 通过。
- 未加入实盘 Broker、下单、密钥、Telegram/Discord、Celery/Redis、Calibration/ML、多 Agent、云端模型强依赖或 Phase 3 UI。

## 2. Ollama / Qwen / 硬件验收

| 项目 | 实测结果 |
|---|---|
| OS | Windows 11 Professional，`10.0.26200`，64-bit |
| CPU | AMD Ryzen 5 5600，12 logical processors |
| RAM | `34,300,493,824` bytes，约 31.94 GiB / 32 GB |
| GPU | NVIDIA GeForce RTX 4060，8,188 MiB VRAM，WDDM |
| NVIDIA driver | `610.62`；`nvidia-smi` 报告 CUDA Version `13.3` |
| Python（真实烟测） | `3.12.10`，`C:\Users\baicha\AppData\Local\Programs\Python\Python312\python.exe` |
| Ollama | `0.32.11`，CLI 已加入用户 PATH |
| Ollama model path | `D:\RJ\ollama-models`（用户变量 `OLLAMA_MODELS`） |
| 服务地址 | 验收使用隔离本地服务 `http://127.0.0.1:11436`；默认项目配置仍支持 `11434` |
| 模型 tag | `qwen3.5:4b`，实际返回 `qwen3.5:4b` |
| 模型格式/参数量 | GGUF，Qwen35，4.7B |
| 实际量化 | `Q4_K_M` |
| 注册模型大小 | `3,389,983,735` bytes（约 3.157 GiB） |
| 模型层 SHA256 | `81fb60c7daa80fc1123380b98970b320ae233409f0f71a72ed7b9b0d62f40490` |
| Ollama manifest digest | `2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd` |
| Phase 2 推理配置 | context `8192`，temperature `0.2`，max tokens `700`，`OLLAMA_THINK=false` |
| `ollama ps` 实测 | `qwen3.5:4b`，`3.3 GB`，`100% GPU`，context `8192` |

真实烟测运行采样共 `19` 个样本：`nvidia-smi` 显存使用约 `1,183–5,441 MiB / 8,188 MiB`，GPU 利用率 `2–100%`；Qwen 模型完整进入 GPU。第一次资源竞争期间的失败不计入最终验收，ComfyUI GPU 负载释放后重跑结果通过。

冷启动单请求复核：请求前 `/api/ps` 为 0 个已加载模型；请求 `qwen3.5:4b` 后模型为 Q4_K_M、context `8192`，墙钟 `4002 ms`，Ollama 返回 `total_duration_ms=3997.662`、`load_duration_ms=3788.883`、`prompt_eval_duration_ms=113.633`、`eval_duration_ms=91.621`。同一请求前后 `nvidia-smi` 显存为 `1316 → 5353 MiB / 8188 MiB`，GPU 利用率为 `5% → 30%`，证明模型实际装载并执行了 GPU 推理。

### 2.1 服务、PATH 与依赖现场复核

- `ollama --version`：客户端 `0.32.11`；服务停止时仅显示无法连接的提示，不影响客户端版本核验。
- 服务运行期间 `ollama list`：实际列出 `qwen3.5:4b`，digest 前缀 `2a654d98e6fb`，大小 `3.4 GB`。
- 服务运行期间 `GET /api/tags` 与 `GET /api/show`：HTTP 成功，返回同一 `qwen3.5:4b`、GGUF、Q4_K_M、4.7B 和 manifest digest。
- 用户 PATH 已包含 `C:\Users\baicha\AppData\Local\Programs\Ollama`；`OLLAMA_MODELS=D:\RJ\ollama-models`。
- 项目核心依赖保持标准库优先；当前 bundled Python 环境未安装 FastAPI/Uvicorn/yfinance/requests，API 与可选 market adapter 依赖不纳入核心真实烟测，股票链路使用 Yahoo Chart public fallback，核心脚本与测试已通过。

## 3. 六标的严格真实烟测

执行命令：

```powershell
python -B scripts\smoke_real_providers.py --require-model --strict --output data\phase2-real-smoke-qwen.json
```

最终结果：`phase=2`、`strict=true`、`records=6`、`failures=0`。所有股票行情来自 `yfinance` provider 的 Yahoo Chart public fallback，未安装 yfinance 包时仍保留真实 provider 身份；加密资产来自 Binance Public REST；新闻来自 Google News RSS。

| 标的 | Provider | data_as_of（UTC） | price | stale | Quant regime / trend | News | Model / parse | latency ms | Action / confidence |
|---|---|---|---:|---|---|---:|---|---:|---|
| AAPL | yfinance | 2026-08-14 18:01:44 | 305.9923 | false | bear / -0.1904 | 10 / available | qwen3.5:4b / valid | 7880.963 | WAIT / 0.000 |
| NVDA | yfinance | 2026-08-14 18:01:54 | 225.3100 | false | bull / 0.3866 | 10 / available | qwen3.5:4b / valid | 4371.890 | WAIT / 0.000 |
| TSLA | yfinance | 2026-08-14 18:02:09 | 341.4450 | false | bull / 0.3984 | 10 / available | qwen3.5:4b / valid | 4394.507 | WAIT / 0.000 |
| AMD | yfinance | 2026-08-14 18:02:20 | 506.3200 | false | bull / 0.2333 | 10 / available | qwen3.5:4b / valid | 4045.060 | WAIT / 0.000 |
| BTCUSDT | binance_public | 2026-08-14 18:02:06 | 62993.8500 | false | bear / -0.1092 | 10 / available | qwen3.5:4b / valid | 6938.700 | SHORT / 0.725 |
| ETHUSDT | binance_public | 2026-08-14 18:02:17 | 1876.2200 | false | bear / -0.0526 | 10 / available | qwen3.5:4b / valid | 4083.109 | WAIT / 0.000 |

最终真实 SHORT 的 BTCUSDT 级别为：entry `62400–63100`，stop `63250`，TP1 `61800`，TP2 `60900`。其余 WAIT 均为 `entry/stop/tp1/tp2=null`。平均模型延迟 `5285.705 ms`，中位数 `4383.199 ms`，范围 `4045.060–7880.963 ms`。

Quant 原始指标已保存在 JSON；最终烟测中抽查的字段包括 EMA20、EMA50、RSI14、MACD、MACD signal、ATR14、Volume Ratio、Support、Resistance、Regime、Trend Score 和 Momentum Score，均由 Python 计算，不由模型生成。

## 4. Schema、Validator 与失败隔离

执行命令：

```powershell
python -B scripts\audit_qwen_schema.py --input data\phase2-real-smoke-qwen.json --output data\phase2-qwen-schema-audit.json
```

结果：`errors=0`、`pass=true`、六条 raw response 全部具备完整字段并可再次解析。真实窗口观测动作是 `SHORT, WAIT`，缺少 LONG；没有为了覆盖率伪造 LONG。合同级测试 `test_model_schema_covers_long_short_and_wait` 已验证 LONG、SHORT、WAIT 三种合法 payload，且三者都通过 Python Validator。

已覆盖的安全规则：

- WAIT 的 `entry_zone/stop/tp1/tp2` 必须为 `null`。
- LONG/SHORT 必须有完整 entry、stop、TP1、TP2、非空 invalidation。
- Python Policy 独占时间值、entry 距离、stop 距离和 R:R 校验。
- 只允许一次 repair；repair 仍失败时生成 `WAIT + repair_failed`，不会变成可执行 LONG/SHORT。
- 失败 JSON 不会绕过 Validator 进入可执行 Prediction；该行为由 `test_failed_json_never_becomes_an_executable_prediction` 覆盖。
- `input_hash`、model version、prompt version、latency、raw response 和 parse status 均持久化。

本次最终 prompt 版本为 `phase2-json-v6`，新增了 WAIT null 约束、数组字段约束、LONG/SHORT 级别顺序约束和 Python-owned `python_decision_gate`；该 gate 只使用已计算的 regime、trend_score、event_risk、stale/news 状态，不伪造价格或未来行情。

## 5. 真实 PaperTrade / Outcome 回放

执行命令：

```powershell
python -B scripts\real_paper_smoke.py --symbols NVDA BTCUSDT --db data\phase2-real-paper.sqlite3 --output data\phase2-real-paper.json
```

最终 1h 回放结果：

| 标的 | Action | 是否跟随 | Outcome | realized R | 说明 |
|---|---|---:|---|---:|---|
| NVDA | WAIT | 否 | — | — | 真实高影响新闻触发 WAIT，不创建 PaperTrade |
| BTCUSDT | SHORT | 是 | TIMEOUT | -0.26402 | 使用 provider 返回的下一根真实 1h K 线结算，未命中 TP/Stop |

数据库计数：`predictions=2`、`paper_trades=1`、`outcomes=1`、`news_events=20`、`provider_snapshots=2`、`model_runs=2`。未对 WAIT 创建 PaperTrade，也未伪造 Outcome。

另有 4h 历史回放证据 `data/phase2-real-paper-4h-v7.json`：BTCUSDT SHORT 被真实跟随，并由下一根真实 4h K 线结算为 `STOP`、`realized_r=-1.0`；该结果同样写入独立 SQLite 数据库。

## 6. 自动化测试与验收清单

```text
python -B -m compileall -q core apps scripts       PASS
python -B -m unittest discover -s tests -v         32 tests, PASS
real strict smoke                              6/6, failures=0, PASS
real Qwen schema audit                          6/6, errors=0, PASS
real PaperTrade / Outcome                         1/1, PASS
```

| 验收项 | 结果 |
|---|---|
| 真实股票/加密 Provider、data_as_of、stale、error_code | PASS |
| 真实 RSS 新闻、10 条/标的、available/provider | PASS |
| Python QuantSnapshot 与 Structured Context | PASS |
| Ollama + Qwen3.5-4B 实际加载、Q4_K_M、GPU、latency | PASS |
| LONG/SHORT/WAIT schema 合同测试 | PASS |
| WAIT 不带价格级别 | PASS |
| 一次 repair 与失败 JSON 隔离 | PASS |
| Signal → Prediction → PaperTrade → Outcome | PASS |
| SQLite migration / ModelRun / raw response | PASS |
| Phase 3 禁止项未引入 | PASS |

## 7. 交付物

- [真实六标的 Qwen 烟测 JSON](../data/phase2-real-smoke-qwen.json)
- [Qwen Schema 审计 JSON](../data/phase2-qwen-schema-audit.json)
- [真实 NVDA/BTCUSDT PaperTrade/Outcome JSON](../data/phase2-real-paper.json)
- [真实 1h PaperTrade SQLite](../data/phase2-real-paper.sqlite3)
- [真实 4h PaperTrade/Outcome JSON](../data/phase2-real-paper-4h-v7.json)
- [真实 4h PaperTrade SQLite](../data/phase2-real-paper-4h-v7.sqlite3)
- [Schema 审计脚本](../scripts/audit_qwen_schema.py)
- [真实 PaperTrade 回放脚本](../scripts/real_paper_smoke.py)

官方安装参考：[Ollama Windows](https://docs.ollama.com/windows)。

## 8. Phase 2 边界声明

本次交付只完成 Phase 2 的真实数据、新闻、Quant、Structured Context、本地 Qwen、Schema/Validator、Signal、Prediction、PaperTrade、Outcome、SQLite、最小 API/CLI 和验收脚本。未开始 Calibration、统计评估平台、ML、自学习、多模型路由、Agent 编排、Broker/Exchange 下单、密钥管理、用户系统、消息推送、Celery/Redis 或云端模型依赖。
