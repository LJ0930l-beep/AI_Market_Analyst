# AI Market Analyst Decisions

## ADR-001 - Verified repository state supersedes stale phase status in source document

Date: 2026-08-15

The v2.0 source document says Phase 3 is not implemented. The repository now contains a completed Phase 3 implementation, tests and a 300-sample real-Qwen replay generated after that baseline. Phase 3 is therefore treated as PASS with recorded errors, and execution starts at Phase 4. Product intent and all Phase 3 integrity constraints remain authoritative.

## ADR-002 - Sol supervises; one persistent luna-max thread implements

Date: 2026-08-15

All code implementation and repair from Phase 4 onward is delegated serially to `luna-max`. Sol owns planning, bounded research, acceptance commands, review, decisions and final acceptance. No parallel writers are allowed in the shared working tree.

## ADR-003 - Preserve FastAPI backend and add React + TypeScript frontend

Date: 2026-08-15

Phase 4 will extend the existing optional FastAPI application and add a React + TypeScript frontend. Backend financial rules remain in Python. The UI does not duplicate validator, time, risk, performance or calibration logic.

## ADR-004 - No broad Phase 1-3 refactor

Date: 2026-08-15

New work should extend existing providers, storage and services. Broad rewrites for framework or directory preference are rejected unless verified evidence shows the existing architecture cannot satisfy a Gate.

## ADR-005 - Phase 3 model errors remain visible

Date: 2026-08-15

The 45 final `repair_failed` replay samples remain part of the audit trail and invalid-count reporting. They are excluded from valid performance calculations but are not deleted or relabeled. Future quality work may improve new runs without rewriting historical results.

## ADR-006 - Stable API contract before frontend implementation

Date: 2026-08-15

Phase 4 uses an injectable FastAPI app factory while preserving existing successful endpoint shapes. New failures use structured error codes. UI read paths are backed by explicit SQLite query helpers, and Follow remains paper-only, idempotent and limited to non-expired LONG/SHORT Predictions. This contract is accepted before creating the React frontend.

## ADR-007 - Research-ledger frontend identity

Date: 2026-08-15

The local UI uses a restrained research-ledger identity rather than a broker terminal or generic dashboard. Mineral/paper surfaces, ruled structure, local system fonts and a reusable time/provenance rail make data age and signal validity visible. The interface avoids fake data, gradients, trading language and decorative motion; responsive and reduced-motion behavior are part of the foundation.

## ADR-008 - Operational health and performance evidence remain separate

Date: 2026-08-15

Dashboard panels query backend, provider, model, database counts and performance independently so one failure does not erase unrelated evidence. Backend connectivity never implies model or provider availability. For performance, `metrics.resolved_actionable` is the only resolved-sample count; total Prediction or actionable counts cannot be presented as resolved outcomes. Missing or zero resolved samples remain visibly degraded and PRELIMINARY.

## ADR-009 - Asset reads cannot create Predictions

Date: 2026-08-15

`GET /instruments/{symbol}/snapshot` is a strictly read-only provider-plus-quant path. It may return actual OHLCV, quote, deterministic indicators and provider freshness, but it cannot invoke news, a model, Signal construction or persistence. Only the explicit `POST /analysis/{symbol}` workflow may create a Prediction, including a WAIT Prediction.
