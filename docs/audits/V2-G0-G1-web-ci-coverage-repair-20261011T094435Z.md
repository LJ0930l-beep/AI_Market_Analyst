# V2 G0/G1 frontend CI coverage repair

Captured: `2026-10-11T09:44:35Z`

## Finding

The existing `.github/workflows/offline-tests.yml` had only a Python job. The V2 acceptance criteria require frontend unit tests, type checking, lint, and production build in the integration batch. Local-only frontend checks could not establish that the candidate passed these gates in hosted CI.

## Scope and repair

Added a `web` job on `windows-latest` that uses the checked-in `web/package-lock.json`, Node 24, `npm test -- --run`, `npm run lint`, and `npm run build` (which runs `tsc --noEmit` before Vite build). The web timeout is 45 minutes: the serialized 31-file Vitest suite was still actively executing after 16 minutes at capture time. Lint and build are distinct steps to preserve each command's exit result.

Only the CI workflow and this audit record are in scope. No product/runtime code, Python behavior, production setting, Gate credential, historical sample, or prior V25–V39 report is changed.

## Evidence at capture

- Candidate PR #27: Draft/open, head `e983df58b93157a75adc29ac709eb7a927f50513`, base PR #26 head `00d1c4405a513b9e5cdee9f6fa15808b4386d8b1`.
- Existing hosted run `38128269910`: Python quick gate passed; complete Python suite was still in progress. This run predates the workflow repair and cannot validate the new web job.
- Local Node `v24.18.0`, npm `11.16.0`; `npm ci --no-audit --no-fund` installed 292 locked packages. npm printed an esbuild postinstall approval warning; the local Vite production build nevertheless completed successfully.
- `npm run lint`: passed.
- `npm run build`: passed; Vite emitted the pre-existing large-chunk advisory.
- `npm test -- --run`: still running at capture, no result claimed. Vite config sets `fileParallelism: false`; 31 test files were present.
- Workflow YAML parsed with PyYAML; `git diff --check` passed.
- PR #26 exact-head hosted run `38107795624` was successful. PR #26 and #27 remained Draft/open with no review decision at the time of the live query.

## Acceptance status and next evidence

This change closes the missing hosted-coverage definition only. Frontend-suite and repaired-workflow acceptance remain **pending** until a hosted run on the new exact PR #27 head passes both Python and web jobs. G0 remains `FIX_IN_REVIEW`; human review and an explicit integration decision are still required. No model provider, Gate account/TestNet/Live API, order, or account setting was accessed.

After pushing the repair, record the new head SHA and hosted run in the PR description and retain this timestamped record as the discovery-time audit snapshot. If the web job exposes an unrelated existing failure, classify it separately and preserve its logs; do not weaken tests or modify runtime trading behavior to obtain a green result.

## Rollback

Revert the workflow/audit commit only. Keep the earlier G0/G1 snapshots, historical PR check outcomes, all source PR refs, and `main` unchanged.
