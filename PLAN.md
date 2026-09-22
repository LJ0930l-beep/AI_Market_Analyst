# Execution Plan

Controlling contract: `MASTER_SPEC.md` (rewritten 2026-09-22 to describe the
autonomous trading system that actually exists). Historical V1.2 stage
specifications remain reference material for the research pipeline only.

## Current release state

- Backend API `2.0.0`, contract `desktop_backend_v1`, routers `/` (v1), `/v2`, `/v3`.
- Backend binds loopback `127.0.0.1:18765`; model server `127.0.0.1:8080`.
- Strategy pack version `ai_strategy_pack_2026_09_v6_signal_timeframe`, 4 templates, all leverage 100.
- Decision model: local `Bonsai-2-27B-PTQ1_0`, context 8192, temperature 0. No cloud token required.
- Desktop build: from `src-tauri`, `cargo +stable-x86_64-pc-windows-gnu tauri build --target x86_64-pc-windows-gnu`.
- Accepted state: **not accepted.** See the remediation log below and the Known
  gaps section of `MASTER_SPEC.md`.

## PLAN -> BUILD -> VERIFY -> REPAIR

1. **PLAN** — complete. Audited the working tree, the running session and the
   divergence between documentation and code.
2. **BUILD** — complete for the 2026-09-22 remediation pass (see log).
3. **VERIFY** — `pytest tests` is the hard gate and must be green before commit.
4. **REPAIR** — outstanding; the Known gaps in `MASTER_SPEC.md` are open work.

## Remediation log — 2026-09-22

Triggered by an audit that found uncommitted changes converting deterministic
risk and audit gates into auto-approval shims. Backed up first to
`scratch/audit-20260922/worktree-before-revert.patch` (git-ignored).

1. Terminated the running `gate_testnet` AI session via
   `POST /v2/ai-session/terminate` before touching code. 0 open positions.
2. Reverted every fabricated-evidence path:
   - `autonomous_strategy.validate_entry` — restored the `NEWS_EVIDENCE_UNAVAILABLE`
     and `NEWS_ANALYSIS_REQUIRED` rejections; removed the auto-filled
     `timeframe_analysis`, `invalidation_condition` and `entry_zone`.
   - `ai_session_coordinator` — removed `_normalize_strategy_plan_field` (it ran
     *before* `validate_schema`, so the gate could not fail), the auto-appended
     `news_revision:` evidence ref, the hardcoded `news_context` string, the
     `trigger_completion_pct = 85.0` default, and the block that **overwrote the
     model's `take_profit`** with `entry + 2.5×risk` to force the net-RR check to
     pass. Restored strict `INVALID_STRATEGY_PLAN` validation.
   - `ai_led_engine` — removed `trigger_completion_pct = min(60.0, score*0.6)`,
     which wrote a reverse-engineered number into WAIT/HOLD audit records.
   - Kept the legitimate changes: `_slim_technical_context`, output-token floor
     640→1024, confidence 0–1 → 0–100 unit normalization.
3. Restored the "every template keeps at least one 不得 prohibition" invariant
   that the loosened `decision_process` had dropped, and aligned
   `tests/test_strategy_cadence_universe.py` with the dual-core
   `candidate_strategy_ids` introduced by `aacfe6c`.
4. Net RR now has one source of truth. `validate_entry` reads
   `execution.min_net_rr`; the prompt injects that same value per strategy
   instead of hardcoding "净 RR≥2.0", and profile `minimum_net_rr` was aligned to
   execution (pullback 1.8→2.0, defense 1.8→2.2). Enforced values unchanged:
   1.6 / 1.6 / 2.0 / 2.2. Same for the completion threshold, which no longer
   says "70%" in the static prompt while profiles say 65/55.
5. `fin_dataset_collector` rewritten: fail-closed environment guard (PAPER and
   TESTNET only; LIVE, RESEARCH and unknown refused; account ids containing
   "live" refused), and the hardcoded out-of-project path
   `D:\RJ\AI Market Analyst\data\fin_tuning` replaced by
   `data/fin_tuning/` overridable via `AIMA_FIN_TUNING_DIR`. The caller no longer
   swallows exceptions at `debug` level.
6. `.gitignore` now covers `.env` / `.env.*` (keeping `!.env.example`) and
   `data/fin_tuning/`. The v1 router in `apps/api/main.py` carries the same
   `verify_local_request` Host/Origin guard that `/v2` and `/v3` already had.
7. `/health` no longer hardcodes `real_orders: false` / `private_keys: false`.
   It reports real capability plus live state
   (`authorization_enforced: false`, `credentials_stored`, `live_credentials_stored`).
   The four subsystem-level `real_orders: False` declarations in
   `core/alerts.py`, `core/monitoring.py`, `core/scheduler.py` and the
   `NOT_ATTEMPTED` provisioning response were left alone — those are accurate
   for their own scope. `execution_gateway` now states plainly that
   TradingAuthorization is not enforced.

## Open work

- Re-wire or formally retire `TradingAuthorization` (Known gap 1).
- Decide whether a LIVE release lock should exist again (Known gap 2).
- Enforce `AIMA_OWNERSHIP_TOKEN` on mutating endpoints, or remove it (Known gap 3).
- Make the default idempotency key stable across minute boundaries (Known gap 4).
- Decide whether the aggressive entry steering in `SYSTEM_PROMPT` is intended at
  100× leverage (Known gap 5). **Answered 2026-09-22 by the user: yes — this is
  an AI-driven autonomous trader in the NOFX class, and steering the model toward
  entries is the product.** What remains open is not the design but the evidence:
  the current parameter set (`required_confirmations=1`, `minimum_signal_score=2`,
  `volume_ratio_min=0.95`, 55% limit threshold, leverage 100) has no measured
  track record yet. It should be validated on TESTNET with a settled sample before
  any LIVE credential is added.
- Lint debt: `ruff` reports ~3.2k findings, including 245 blind `except
  Exception`, 51 `try/except: pass` and 20 `B023` loop-variable closures.
- The pre-existing SFT dataset (164 samples, ~3MB, all testnet/paper) is still at
  the old out-of-project path and was deliberately not moved.
