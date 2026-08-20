# Post-V1.0 Qwen Consult completion report

Status: DEVELOPER_COMPLETE; supervisor acceptance is not claimed. The accepted V1.0/Phase 7 evidence remains historical and unchanged.

## Scope and architecture

`qwen_consult_v1` is a local, versioned, read-only research consultation contract. `POST /consult/stream` accepts only `language`, bounded `user`/`assistant` messages and an optional symbol already present in the durable instrument registry. The server owns the system prompt, Ollama loopback URL and fixed model. It streams NDJSON `meta`, incremental `delta`, terminal `done`, or sanitized `error` events. Startup and read-only release health do not contact the model; `/health/model` performs the existing explicit local health probe and adds a redacted consultation capability.

The process-local service permits one active consultation. Connection, first-token, idle and total timeouts; message/body/history/output budgets; and at-most-one pre-output retry are bounded. No arbitrary model or base URL is accepted from a browser. Ollama must use loopback HTTP and there is no cloud or API-key fallback.

For an optional symbol, the server reads the latest existing live Prediction and a bounded allowlist of saved context fields. The response identifies `as_of`, freshness, provider/context sources and missing/degraded reasons. It never runs analysis or requests fresh provider data. Saved evidence is treated as untrusted prompt data and cannot override the server's safety/time instructions.

The bilingual React page performs actual incremental rendering with `AbortController`, Enter-to-send, Shift+Enter newline, stop, explicit clear confirmation, availability/error states and an accessible message log. Conversation messages are held in React and the browser tab's `sessionStorage` only. Qwen/backend free text and technical evidence are excluded from presentation translation. Asset Detail links to the consultation route with the current symbol.

## Safety boundary

- Research assistance only; not investment advice and no claimed real-time freshness beyond returned evidence.
- No consultation path writes chat text or responses to SQLite, logs the complete prompt, creates Prediction/Outcome/PaperTrade/alert/memory rows, follows a signal, starts a scan/settlement, changes confidence/calibration, or accesses a broker/order/private-key path.
- Ollama failures, missing models, timeout, malformed stream, interruption, output limits, cancellation and serial-concurrency conflicts return stable, redacted states. There is no fixture or synthetic answer fallback in production.
- The selected UI language controls the server-owned default response-language instruction. Symbols, URLs, timestamps, numeric values and backend evidence remain unchanged.

## Automated evidence

The consolidated developer Gate passed:

- `python -m pytest -q`: 127 passed, 1 skipped, 1 warning and 20 subtests in 23.64s.
- `python -B -m unittest discover -s tests -v`: 128 tests OK, 1 skipped.
- `python -B -m compileall -q apps core tests scripts` and `python -m pip check`: PASS; no broken requirements.
- Frontend `lint`, `typecheck` and production `build`: PASS (59 transformed modules); Vitest: 14 files / 67 tests PASS.
- Full and production npm audits: 0 vulnerabilities.
- Standalone Playwright: 12/12 PASS in 45.3s using Chrome 151.0.7922.140. The real built React app and FastAPI routes used temporary SQLite plus an explicitly injected deterministic consultation transport. Ten product routes passed 20 desktop/mobile (390x844) axe and horizontal-overflow checks.
- Browser consultation evidence covers Asset Detail symbol handoff, actual NDJSON incremental assembly, Enter/Shift+Enter, refresh restoration, explicit clear and unchanged Prediction/Outcome/PaperTrade/calibration/alert/memory counts.

Deterministic tests use injected local transports and explicitly do not prove a live Qwen model.

## Live local smoke

After restoring the owned launcher, the real `GET /health/model` reached the loopback Ollama service (`provider=ollama`, service available) but reported configured model `qwen3.5:4b` as `model_available=false` and returned no installed models. A real generation smoke therefore was not attempted. The deterministic fixture answer is not presented as live evidence. Launcher status was `running`, `ownership_errors=[]`, scheduler default disabled; UI and API both returned HTTP 200.

## Known limitations

- Consultation context is the latest saved live Prediction/context, not a new market-data fetch; unavailable or stale evidence remains explicitly marked.
- Browser-tab session storage is intentionally not durable history and is removed by Clear or when the browser session ends.
- Process-local serial concurrency does not coordinate multiple independent API processes; the supported launcher runs one API process.
- Markdown-specific rendering is intentionally not enabled; Qwen output is rendered as bounded plain text.
