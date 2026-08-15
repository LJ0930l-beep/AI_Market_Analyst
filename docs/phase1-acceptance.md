# Phase 1 acceptance checklist

- [x] 股票与 Crypto 共用 `Instrument` 和 `MarketProvider` 接口。
- [x] Provider、Quant、Signal、PaperTrade、Outcome、Storage 有独立模块。
- [x] 无真实下单、券商账户、交易所私钥、Telegram Bot、Celery/Redis 运行时依赖。
- [x] 离线 Fixture 可生成 AAPL / NVDA / TSLA / AMD / BTCUSDT / ETHUSDT 的快照和 Prediction。
- [x] Signal 支持 LONG / SHORT / WAIT，并校验 Entry/Stop/TP、Signal Validity、Max Hold、Confidence、Invalidation。
- [x] Prediction 与 PaperTrade 分表；用户不跟随时仍保存 Prediction。
- [x] Outcome 支持 TP1、TP2、Stop、Timeout、MFE、MAE、R。
- [x] SQLite 重启后保留 Prediction / PaperTrade / Outcome。
- [x] 提供可复现启动与测试命令。

## Commands

```powershell
cd D:\RJ\codex\ai-market-analyst
python scripts\run_phase1_demo.py --db data\phase1-demo.sqlite3 --follow
python -m unittest discover -s tests -v
```

Optional API:

```powershell
python -m pip install -e ".[api]"
python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```

Known limitation: Phase 1 uses a deterministic offline Provider. Qwen/Ollama, real news, real market data, dynamic time rules, calibration, and UI belong to later phases.

