# AI Market Analyst — Phase 6

Phase 6 在已验收的 Phase 0-5 纸面研究边界上增加 Benchmark Context、point-in-time 多源事件/TimePolicy 和 leakage-safe Market Memory：

`Market Provider → QuantSnapshot → News/Events/Benchmark/Memory Context → Ollama/Qwen → SignalProposal → Prediction → PaperTrade → Outcome`

系统只做研究与 Paper Tracking，不包含真实下单、券商账户、私钥或用户资金链路。

## 快速验证

```powershell
cd D:\RJ\codex\ai-market-analyst
python -B -m unittest discover -s tests -v
python -B scripts\run_phase2_demo.py --mode fixture --llm mock --news fixture --db data\phase2-demo.sqlite3 --follow --settle
```

Fixture Demo 不依赖网络或 Ollama，覆盖 AAPL、NVDA、TSLA、AMD、BTCUSDT、ETHUSDT，并会保存 Prediction、PaperTrade 和 Outcome。

## Phase 4 browser Gate

```powershell
cd D:\RJ\codex\ai-market-analyst\web
npm ci
npm run e2e:preflight
npm run e2e
```

The E2E command builds the React app, starts the real FastAPI routes against a disposable SQLite database under the OS temporary directory, serves the build locally, and removes the temporary run directory after completion. It never uses the formal Phase 3 database. The default browser is the installed Chrome channel; use `npm run e2e:install`, then `$env:P4_E2E_BROWSER_CHANNEL = "chromium"` before `npm run e2e` to run the pinned Playwright Chromium instead. `npm run e2e:preflight` also accepts `P4_E2E_BROWSER_CHANNEL = "edge"` for installed Edge.

## 真实 Provider 烟测

```powershell
python -B scripts\smoke_real_providers.py --output data\phase2-real-smoke.json
```

股票优先走 `YFinanceProvider`；未安装 `yfinance` 时，同一 Provider 使用 Yahoo 公网 Chart 接口。Crypto 优先走 Binance Public REST，CoinGecko 与 Fixture 为降级路径。所有结果都带 `provider`、`data_as_of`、`stale` 和 `error_code`。

## Ollama / Qwen

安装并启动 Ollama 后，将模型配置放在环境变量或 `.env` 中：

```powershell
$env:OLLAMA_BASE_URL = "http://127.0.0.1:11434"
$env:OLLAMA_MODEL = "qwen3.5:4b"
$env:OLLAMA_QUANTIZATION = "Q4_K_M"
$env:OLLAMA_CONTEXT_LENGTH = "8192"
$env:OLLAMA_TEMPERATURE = "0.2"
$env:OLLAMA_MAX_TOKENS = "700"
python -B scripts\run_phase2_demo.py --mode real --llm ollama --news rss --db data\phase2-real-qwen.sqlite3
```

默认调用只通过 Ollama HTTP API；超时、重试、响应长度、严格 JSON 解析和一次 repair 都有上限。模型不可用时系统继续展示 Quant/News，并保存 `WAIT + MODEL_UNAVAILABLE`。

## Phase 3 历史回放与指标

正式回放只使用真实 `qwen3.5:4b`，按 `as_of` 截止点隔离输入，使用 `--resume` 支持中断后继续。`--mode mock` 仅用于测试编排，不计入正式验收。

```powershell
python -B scripts\run_phase3_replay.py `
  --symbols AAPL NVDA TSLA AMD BTCUSDT ETHUSDT `
  --timeframes 1h 4h `
  --samples 300 `
  --model qwen3.5:4b `
  --seed 42 `
  --mode real `
  --require-model `
  --resume `
  --db data\phase3-real-qwen-formal-v2.sqlite3 `
  --output data\phase3-real-qwen-formal-v2.json `
  --manifest data\phase3-real-qwen-formal-v2.manifest.json
```

回放完成后生成绩效快照、Beta(5,5) 置信度校准和汇总报告：

```powershell
python -B scripts\build_performance_snapshot.py --db data\phase3-real-qwen-formal-v2.sqlite3 --replay-run-id replay-20260815042457-71ec330d --output data\phase3-formal-v2-snapshots.json
python -B scripts\fit_confidence_calibration.py --db data\phase3-real-qwen-formal-v2.sqlite3 --replay-run-id replay-20260815042457-71ec330d --output data\phase3-formal-v2-calibration.json
python -B scripts\report_phase3_metrics.py --db data\phase3-real-qwen-formal-v2.sqlite3 --replay-run-id replay-20260815042457-71ec330d --output data\phase3-formal-v2-report.json --markdown docs\phase3-formal-v2-metrics.md
```

Phase 3 API 增加：`/performance/summary`、`/performance/by-symbol/{symbol}`、`/performance/buckets`、`/calibration/current`、`/replay/runs`、`/replay/runs/{run_id}` 和 `/predictions/{prediction_id}/calibration`。

## 最小 API

当前 API 合同为 Phase 6 / 0.6.0；Phase 6 上下文接口保持 GET 只读，明确区分 response_time/data_as_of，并保留 Phase 5 的 Radar、默认关闭 scheduler、settlement 和本地 Alert Center 边界。

```powershell
python -m pip install -e ".[api,market]"
$env:MARKET_DATA_MODE = "real"
$env:NEWS_MODE = "real"
$env:LLM_MODE = "ollama"
python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```

主要接口：

- `GET /instruments/{symbol}/snapshot`
- `GET /instruments/{symbol}/news`
- `GET /instruments` 返回六个 canonical instruments 与已验证注册项；`POST /instruments/register` 只接受 `symbol` 和 `asset_type`，对 equity 使用安全公开 ticker，对 crypto 接受明确的 `BASE` 或 `BASEUSDT` alias 并规范化为 `BASEUSDT`。
- `POST /analysis/{symbol}`，body 可传 `{"timeframe":"1h","limit":120}`
- `GET /predictions`
- `POST /predictions/{id}/follow`
- `GET /watchlist`, `POST /watchlist`, `PUT /watchlist/{symbol}`, `DELETE /watchlist/{symbol}`
- `GET /settings`, `GET/PUT/DELETE /settings/{key}` for the four typed local scheduler-resource defaults; these endpoints do not activate background work.
- `GET /health/providers`
- `GET /health/model`
- `GET /health/context` 返回 benchmark/event/memory/time-policy capability 与只读边界。
- `GET /instruments/{symbol}/context`、`/events`、`/memory` 返回带 `response_time`、`data_as_of`、provider/capability provenance 的 Phase 6 上下文；这些 GET 不保存 Prediction、Outcome、PaperTrade、alert 或 Memory feature。
- `POST /memory/materialize` 是唯一显式的本地 Memory feature materialization 边界，需要带时区的 `as_of`；普通 GET 不会物化历史样本。

注册项会先经 Yahoo 公共 market data（equity）或 Binance 公共 USDT spot ticker（crypto）验证；超时/重试有界，失败或 provider 不可用不会写入 SQLite。扩展项的 exchange/sector 等元数据明确标记为 inferred/unknown，不使用 Fixture 作为兼容性证明。

API 同时返回 `data_as_of` 与 `response_time`，并明确标记真实、Fixture、模型不可用等状态。

## 代码边界

- `core/providers/`：统一 Bar/Quote、真实 Provider、Fixture、News RSS。
- `core/quant/`：EMA、RSI、MACD、ATR、Volume Ratio、Support/Resistance、Regime。
- `core/context.py`：不含原始 bars 的压缩 Structured Market Context 与 `input_hash`。
- `core/benchmarks.py`：显式公共 benchmark mapping 与 Python 相对表现计算。
- `core/events.py`：typed event evidence、point-in-time 选择、版本化来源可信度与多源 cluster。
- `core/memory.py`：版本化固定特征距离、as_of 隔离、可选显式 materialize 的 Market Memory。
- `core/ai/`：Ollama、Mock、Prompt、JSON Parser、一次 repair、Signal Validator。
- `core/analysis_service.py`：完整 Phase 2 编排。
- `core/storage/`：SQLite 增量迁移、canonical Watchlist/AppSetting、Prediction、News、ProviderSnapshot、ModelRun、PaperTrade、Outcome。

完整验收记录见 [docs/phase2-completion-report.md](docs/phase2-completion-report.md)。

Phase 3 正式回放及限制见 [docs/phase3-completion-report.md](docs/phase3-completion-report.md)，指标明细见 [docs/phase3-formal-v2-metrics.md](docs/phase3-formal-v2-metrics.md)。
