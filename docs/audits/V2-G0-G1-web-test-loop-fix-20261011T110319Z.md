# V2 G0/G1 web test loop repair

Captured: `2026-10-11T11:03:19Z`

## Finding

The hosted web job on PR #27 head `6d0745c9e02a53b4e8820876016a5d6272f61e83` reached its 45-minute timeout while running the Vitest suite. The exact-head Python job passed; the overall run `38129959206` is recorded as cancelled because the web job timed out. The previous audit snapshot remains unchanged.

Local sequence isolation reproduced an account-selection loop in the Gate mock page. In `gate-live` / `mock` mode, the page's Gate-data refresh already pins the selected account to `gate_testnet`. The generic workspace refresh independently cleared that selection whenever `/accounts` had no registered entries. These two state paths repeatedly changed the selected account and retriggered workspace refreshes. The affected regression used an empty registered-account response and exposed the loop after the microstructure-page test.

## Repair

- `V2WorkspacePage.tsx`: apply the same Gate TestNet mock-account selection rule in the generic refresh as in Gate-data refresh. The callback dependencies now include the selection context that controls this rule.
- `V2WorkspacePage.test.tsx`: await the loaded risk-cockpit state and assert `/workspace?account_id=gate_testnet` is requested once for an empty account list. The file explicitly unmounts rendered pages before restoring API mocks.
- `offline-tests.yml`: emit per-test Vitest progress in hosted CI so a future stalled test is visible in logs.

The change does not submit, retry, or alter orders. It does not change the production trading engine, risk limits, saved account configuration, credentials, or historical research data.

## Verification

- Focused Gate-account interaction (microstructure page followed by incomplete account cockpit): 2 passed.
- Full `V2WorkspacePage.test.tsx`: 22 passed in 6.02 seconds.
- Full web unit suite: 29 files, 161 tests passed in 57.68 seconds.
- `npm run lint`: passed with zero warnings.
- `npm run build` (`tsc --noEmit` and Vite build): passed. Vite retained the existing advisory for the 615.51 kB main JavaScript chunk.
- `git diff --check`: passed.

Hosted verification for the repaired exact head is pending. The earlier web timeout remains recorded as a historical failure; it is not reclassified as a pass. The prior Python hosted job on `6d0745c` passed but does not validate the later repair commit.

## Safety and acceptance

All web tests use mocked API clients. No model/provider, Gate, TestNet, or Live API call occurred; no order was sent; no production/account setting was changed. Historical reports and research ledgers were not edited.

This closes the local frontend-loop finding only. V2 G0/G1 acceptance remains pending the new exact-head hosted checks and independent human review. PR #27 remains Draft; G0 integration requires an explicit human decision, and G2 remains unauthorized.

## Rollback

Revert the repair commit if it introduces a regression. Preserve the discovery-time timeout record and all earlier V2/V25–V39 evidence.
