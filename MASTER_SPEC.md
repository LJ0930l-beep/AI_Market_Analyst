# AI Market Analyst — Master Spec

This file is the execution contract for the repository. It describes what the
system **actually is**, verified against code on 2026-09-22. Where it conflicts
with older documents, this file wins; record the conflict in `DECISIONS.md`
rather than silently rewriting history.

> **Superseded scope.** Everything below the line "Historical V1.0 contract" at
> the end of this file is retained for provenance only. The V1.0/V1.2 boundary
> "no real order execution, no broker APIs, no exchange secrets, no fund
> management, no fine-tuning/LoRA" **no longer holds** and was withdrawn by
> incremental commits (`e5cb6b3`, `fc89215`, `12f065e`, `aacfe6c`) without any
> document being updated. Do not cite the old boundary as a current guarantee.

## Product objective

Deliver a local-first, **AI-driven autonomous crypto trading system** in the
same class as [`NoFxAiOS/nofx`](https://github.com/NoFxAiOS/nofx): a local LLM
reads the market and decides what to trade, while Python retains every
financial calculation and every risk limit. It trades crypto perpetual futures
on Gate, unattended, in TESTNET or LIVE mode.

The design contract, in one line: **the model proposes, Python disposes.** The
LLM may be aggressive, opinionated and wrong; it may not be unclamped.

Three layers are live in this codebase:

1. **Autonomous trading loop (the product).** `core/trading/ai_session_coordinator.py`
   runs on a strategy cadence, scans candidates, builds an evidence bundle,
   asks the local model for one decision per cycle, validates it deterministically
   in `core/trading/autonomous_strategy.validate_entry`, clamps it through
   `RiskEngine`, and submits through `core/trading/execution_gateway.py`.
   `core/trading/position_guardian.py` manages stops and trailing protection
   between cycles.
2. **NOFX interoperability.** `core/trading/nofx_indicators.py` and
   `nofx_strategy_adapter.py` import NOFX strategy configs. The importer
   deliberately refuses credentials (`indicators.nofxos_api_key` is listed under
   `unsupported_sources`) — a NOFX config can bring strategy shape, never keys.
3. **Research/paper pipeline (V1.0–V1.2 heritage).** Public market/news data,
   deterministic quant rules, auditable LONG/SHORT/WAIT Predictions, PaperTrade,
   Outcome settlement and calibration. Still present, still used, and the source
   of the audit-trail discipline the trading loop is supposed to inherit.

## Current system facts

| Aspect | Value |
| --- | --- |
| Backend | FastAPI, loopback `127.0.0.1:18765`, routers `/` (v1), `/v2`, `/v3` |
| Decision model | Local `Bonsai-2-27B-PTQ1_0` (llama.cpp server on `:8080`), context 8192, temperature 0 |
| Venue | Gate futures; `TradingMode` ∈ {RESEARCH, PAPER, TESTNET, LIVE} |
| Credentials | Windows DPAPI vault, `secure_account_credentials`, masked keys only in public metadata |
| Strategy packs | 4 templates in `core/trading/ai_strategy_book.py`, **all at leverage 100** |
| NOFX interop | Strategy configs importable from `NoFxAiOS/nofx`; credentials are never imported |
| Cloud models | Not required at runtime |

## Non-negotiable boundaries (current)

- **Python owns all financial mathematics.** Indicators, R:R, position sizing,
  stops, time rules and risk limits are computed in Python. The LLM proposes;
  Python disposes.
- **The LLM never sees or emits credentials**, and its output never reaches the
  exchange without deterministic validation and RiskEngine clamping.
- **Loopback only.** The API must not bind a non-loopback interface.
- **WAIT is a normal outcome.** A rejected or waiting cycle is valid data. It
  must never be rewritten into an entry, and its recorded metrics must never be
  reverse-engineered to look consistent with a rejection.
- **Audit records are truthful.** Persisted evidence, `news_context`,
  `timeframe_analysis`, `invalidation_condition`, `entry_zone`,
  `trigger_completion_pct` and `take_profit` must be what the model actually
  produced or what Python actually measured. Fabricating any of them to satisfy
  a validator is a defect, not an optimization — see `DECISIONS.md`.
- **No future bars/news or post-hoc revision** in replay or model input.
- **No deletion of losses, timeouts or ignored Predictions** to improve reported
  performance.
- **Training data collection is environment-gated.** `fin_dataset_collector`
  collects from PAPER and TESTNET only and fails closed on LIVE or unknown
  mode, because a sample embeds the full decision prompt including
  `account_truth`.
- **A prompt may not state a threshold that Python does not enforce.** Any
  number shown to the model (net RR, trigger completion) is injected from the
  same config the validator reads.

## Known gaps — stated plainly

These are real and currently unfixed. They are recorded so nobody mistakes
silence for safety.

1. **`TradingAuthorization` is advisory only.** `submit_intent` takes no
   authorization argument (`core/trading/execution_gateway.py`, `active_auth =
   None`), so authorization expiry, revocation and its leverage/risk/instrument
   limits **never gate an order**. RiskEngine defaults are the only caps that
   apply. Re-wiring it means threading authorization through 8 call sites in 6
   modules — including `position_guardian` protection cycles, where a missing
   authorization must **not** block a protective close. `/health` reports
   `authorization_enforced: false`.
2. **No separate LIVE release lock.** A correctly scoped Gate credential is the
   execution identity; `gate_accounts.py` rewrites persisted `LIVE_LOCKED`
   markers as stale. Saving a LIVE key is the only ceremony.
3. **The API has no per-request authentication.** `/v2` and `/v3` enforce a
   loopback Host + Origin allowlist (`core/security/local_guard.py`), which does
   block DNS rebinding and cross-site calls. The v1 router now carries the same
   guard. But `is_allowed_host(None)`/`is_allowed_origin(None)` return true, so
   **any local process can drive the whole API**, including order placement.
   `AIMA_OWNERSHIP_TOKEN` is computed in `/health` and never enforced.
4. **Idempotency keys rotate per minute** for callers that omit one
   (`apps/api/v2.py`, `int(time.time()/60)`), so a retry across a minute
   boundary gets a new key and bypasses gateway dedupe. Mitigated by
   `create_order` never being auto-retried.
5. **Prompts steer hard toward entry — intentional, but currently untuned.**
   `autonomous_strategy.SYSTEM_PROMPT` tells the model it holds "独立最高决策权",
   must "果断发起 OPEN", and that candidate `NO_TRIGGER` is "绝不是禁止开仓禁令".
   This is deliberate product design for an AI-driven autonomous trader, **not**
   a defect: the invariant is that steering may change *how often* the model
   proposes, never *whether Python clamps it*. That invariant currently holds —
   risk fraction, leverage, notional and net RR are all enforced downstream.
   What is unresolved is tuning: with all four templates at leverage 100 and the
   aggressive template at `required_confirmations=1`, `minimum_signal_score=2`,
   `volume_ratio_min=0.95` and a 55% limit-entry threshold, the expected failure
   mode is churn and death-by-a-thousand-cuts rather than one large loss. There
   is no measured evidence yet that this parameter set is net-positive; treat it
   as a hypothesis under test on TESTNET, not as a validated configuration.
6. **Large modules.** `ai_session_coordinator.py` (~182KB),
   `execution_gateway.py` (~152KB), `trader_capabilities.py` (~142KB),
   `ledger.py` (~125KB). Defects hide in these; review diffs, not summaries.

## Development operating model

- Loop: `PLAN -> BUILD -> VERIFY -> REVIEW -> ACCEPT/REPAIR -> NEXT`.
- **One task at a time in the shared working tree.** Concurrent editors have
  already produced interleaved changes to the same risk-critical files; check
  `git status` and file mtimes before editing `core/trading/`.
- **`pytest tests` must pass before any commit.** The suite is the gate; a
  commit that leaves it red is a defect regardless of intent.
- Each task pack defines objective, read-first files, allowed and forbidden
  paths, constraints, acceptance commands and Definition of Done.
- Two failures with the same root cause trigger replanning, not a third attempt.

## Acceptance

`FINAL ACCEPTED` requires: the full Python suite green; frontend
build/lint/typecheck/test green; no fabricated field in any audit record; every
prompt-stated threshold traceable to the enforcing config; the Known gaps above
either fixed or explicitly accepted in writing by the user; and a final report
that lists limitations and makes no profit promise.

## Stop conditions requiring user direction

Stop for: any change that would let an LLM decide position size, leverage or
price without Python clamping; enabling LIVE credentials; uploading training
data off-machine; irreversible destructive migration; unresolved legal or
data-source issues; unrecoverable workspace permission failure; or evidence
that a prior accepted phase did not actually exist.

---

# Historical V1.0 contract (superseded — provenance only)

Retained because `MASTER_SPEC.md` must never silently discard history. **These
boundaries are no longer true of the code.**

- Original objective: a local-first US-stock and crypto *research* application
  emitting auditable LONG/SHORT/WAIT proposals, persisting Predictions,
  creating PaperTrade only when the user follows, and settling Outcomes from
  later real data.
- Original boundaries, now withdrawn: no real order execution; no broker
  private APIs; no exchange secrets; no account or fund management; no model
  arena or multi-agent runtime; **no fine-tuning, LoRA, RLHF or online
  training**.
- Original phase gates 0–7 and the `FINAL ACCEPTED` checklist referenced the
  research product. Phase 0–3 baselines were recorded as PASS on 2026-08-15
  (41 tests) and remain valid *for that pipeline*, not for the trading system.
- The LoRA/SFT pipeline (`core/trading/fin_dataset_collector.py`,
  `scripts/cloud_qlora_train_unsloth.py`, `scripts/train_crypto_lora_guide.py`)
  directly contradicts the old "no fine-tuning" boundary. It is gated to
  PAPER/TESTNET but is not authorized by any current spec text other than this
  acknowledgement.
