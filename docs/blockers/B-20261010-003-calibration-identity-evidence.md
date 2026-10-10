# B-20261010-003 — 远程校准身份与权重摘要契约缺失

```yaml
id: B-20261010-003
severity: S1
status: OPEN
component: model
mode: RESEARCH_OFFLINE
branch_sha: 9eb55ac19b7e8739b7500cca03b0c9d2a253c299
trigger_signature: CALIBRATION_REMOTE_WEIGHT_DIGEST_REQUIRED
first_seen_at_utc: 2026-10-10T19:43:32Z
user_authorization: DOCUMENTED_SCOPE_ONLY
external_cost_or_order_attempted: false
remote_effect_known: false
risk_to_funds: NONE
affected_sample_or_order_ids: calibration preflight only; no samples or orders
immediate_stop_or_isolation: Keep calibration NOT_READY when no verified digest exists; do not call a model provider to probe this path.
evidence_files_and_hashes: "core/trading/ai_calibration.py SHA-256 4F1794A79D4AB26A71B7FF9B5A96A5B7DEE968C1DF4D8B1FEF245E6EB0F11AC2; core/evidence.py SHA-256 520AF76610A2748703DE58972DCFA39197002F60E2195FE550C34F1A6F25470F; tests/test_ai_calibration.py SHA-256 089606DE34AA4786A064AB26061089BA96BFA686855DB4A19A1BF022EF9F7322; source/manual: AI_Market_Analyst_V2_0 development blocker handbook §7.2 B05"
reproduction: "Static inspection: ai_calibration.py calls model_weight_digest before calibration and returns NOT_READY/MODEL_DIGEST_UNAVAILABLE when absent; evidence.py returns UNKNOWN_NOT_PROVIDED without a verified SHA-256 digest. No provider request was used to reproduce this behavior."
root_cause_status: CONFIRMED
safe_fallback: Preserve the current fail-closed NOT_READY behavior; do not treat model names or local application artifact hashes as remote model-weight evidence.
patch_pr: none; implementation is deferred behind G0/G1/G2/G3 gates
negative_tests: "Required before any implementation: missing digest; wrong model receipt; forged digest; model-name hash; local artifact SHA misused as remote weights; RESEARCH/PAPER/TESTNET identity mode cannot grant LIVE permission."
unblock_criteria: Offline calibration contract separates LOCAL_ARTIFACT_SHA256 from REMOTE_RESPONSE_IDENTITY, records model_identity_evidence_level and weights_exposed=false when applicable, passes negative tests, and enforces NOT_READY for stronger immutable-weight requirements and all unauthorized LIVE paths.
needs_human_decision: true
next_owner_action: After upstream G0/G1 review and authorized G2/G3 evidence, design the offline identity-mode contract and fake-provider negative matrix; keep this issue OPEN until those gates and review are complete.
```

## Evidence classification

This is a confirmed fail-closed code-path limitation found by static review, not a provider incident or an observed calibration attempt. The manual's B05 requires distinct evidence for local application artifacts and remote model identity/weights, plus an explicit `REMOTE_WEIGHTS_NOT_EXPOSED` mode restricted to Research/Paper/TestNet. The current calibrator requires a model-weight digest and returns `NOT_READY` if it is missing; no explicit remote-identity calibration mode is present. Provider availability of an authenticated digest is unknown.

Do not weaken the guard or introduce a model call here. G4 implementation depends on upstream G0–G3 acceptance. Preserve the current `NOT_READY` fallback until the offline contract and negative tests are reviewed.
