# Phase 6 completion report

Date: 2026-08-20
Contract: API phase 6 / version 0.6.0
Gate status: developer PASS, ready for Sol/supervisor acceptance. Phase 7 is not implemented.

## Scope and architecture boundary

Phase 6 adds three deterministic, local-first context capabilities around the accepted Phase 0-5 paper-only pipeline:

- `core/benchmarks.py` owns explicit versioned equity/crypto benchmark mappings and Python relative-return/strength calculations. Public provider routing remains injected through the existing market provider boundary. Benchmark metadata is durable in additive SQLite migration v9.
- `core/events.py` owns typed event evidence, point-in-time selection, source credibility, primary-source preference, deterministic clustering and disagreement/provenance preservation. Existing news providers are adapted without claiming historical known-time support they do not provide. `core/time_rules.py` consumes typed major-event evidence and keeps validity, holding and re-evaluation rules in Python.
- `core/memory.py` owns `market_memory_v1` fixed feature representation, distance ranking, stable tie-breaking, as-of eligibility, sample gating and explicit materialization. SQLite retains bounded feature evidence; GET context queries do not materialize it.

`AnalysisService` supplies these structures to the existing `MarketContext` and model prompt as evidence only. It does not delegate numeric, time, risk or memory ranking logic to the LLM. Asset Detail and Settings/Health expose typed loading/degraded/read-only states. API reads are available at `/health/context`, `/instruments/{symbol}/context`, `/instruments/{symbol}/events` and `/instruments/{symbol}/memory`; `POST /memory/materialize` is the explicit, documented persistence boundary.

The Phase 6 browser harness uses a temporary SQLite database and real FastAPI routes with a production React build. It injects deterministic market/event/model/resource dependencies for repeatability and does not mock browser network responses. No cloud notifier, Redis, Celery, broker, private key, real-order, automatic-calibration, raw-confidence or PaperTrade mutation path was added.

## Automated evidence

Commands and exact results from the final verification pass:

| Command | Result |
| --- | --- |
| `python -m pytest -q` | PASS — 105 tests, 10 subtests; one known Starlette/httpx deprecation warning |
| `python -B -m unittest discover -s tests -v` | PASS — 105 tests |
| `python -m pytest -q tests/test_phase6_context.py` | PASS — 6 focused tests |
| `python -B -m compileall -q apps core tests scripts` | PASS |
| `python -m pip check` | PASS — `No broken requirements found.` |
| `npm run lint` | PASS |
| `npm run typecheck` | PASS |
| `npm run test -- --run` | PASS — 12 files / 55 tests |
| `npm run build` | PASS — Vite 6.4.3 |
| `npm audit --audit-level=high` | PASS — 0 vulnerabilities |
| `npm audit --omit=dev --audit-level=high` | PASS — 0 vulnerabilities |
| `npm run e2e:preflight` | PASS — Playwright 1.62.1; Chrome and Edge candidates found |
| `npm run e2e` | PASS — 10/10 tests, one worker, 59.6s, real built React/FastAPI/SQLite harness |
| `git diff --check` | PASS — only expected LF/CRLF conversion warnings |

The final browser run used installed Chrome `151.0.7922.140` (Edge candidate `151.0.4129.93` also available). It covered nine SPA routes at `1280x900` and `390x844`: 18 axe scans and 18 page-level overflow checks. User-flow assertions include context capability/read-only counts, explicit analysis write boundary, existing Watchlist/Radar/scheduler/settlement/Alert Center behavior, no PaperTrade/confidence/calibration mutation, deep-link HTML and JSON `/api/health`.

Focused Phase 6 tests cover migration/reopen persistence, benchmark as-of/fallback and crypto capability fallback, event publication/known/revision filtering, deterministic multi-source clustering/credibility/disagreement, major-event TimePolicy effects, provider failure capability, Memory future bars/data/outcome/self/incomplete exclusion, repeatability, API GET no-domain-mutation and explicit analysis/materialization boundaries.

## Manual and capability evidence

- SQLite migration v9 is additive and idempotent. Existing Phase 0-5 tables and records remain intact; benchmark/event/memory evidence survives a new `SQLiteStore` over the same database.
- Read-only context responses include `response_time`, `data_as_of`, provider snapshots, `as_of`, versions, capability and provenance. Provider failures become unavailable/degraded evidence; no fallback is presented as verified data.
- Memory queries exclude predictions generated at/after the query boundary, stored data/context boundaries after it, outcomes settled after it, self-matches and records without sufficient deterministic features. A minimum resolved sample gate produces preliminary evidence instead of an AI-generated score.
- The explicit materialization endpoint is documented and bounded; normal GET routes never call it. Materialization does not retrain models, activate calibration or modify confidence, Outcomes or PaperTrades.
- The harness database is created under the OS temporary directory and cleaned by the existing guarded E2E teardown. The formal Phase 3 database is not opened.

## Limitations and residual risks

- The existing RSS adapter has no historical known-time contract, so it conservatively infers `known_at` from publication time and marks historical event capability limited. A full earnings/macro calendar and revision feed is not claimed.
- Benchmark mappings are deliberately narrow and explicit. Crypto context uses a BTCUSDT baseline; total-market and dominance values are explicitly unavailable. E2E providers are injected fixtures and do not prove live Yahoo/Binance freshness.
- Market Memory is a fixed-feature SQLite implementation, not a vector database. It requires the configured resolved-sample threshold and does not automatically materialize or calibrate.
- The accepted equity session behavior still lacks a full exchange holiday calendar. Production Qwen/ComfyUI contention and distributed deployment are not measured by the fixture E2E; accepted non-invasive scheduler/resource boundaries remain unchanged.
- Phase 7 work, including release/configuration hardening and backup/restore tooling, is intentionally not included.
