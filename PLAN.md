# AI Market Analyst V1.2.1 Execution Plan

The supplied V1.2 DOCX remains the stage specification, with Sol's V1.2.1 repair findings as the controlling acceptance delta. Repository history and user changes are preserved; no V1.3, broker, account, secret, order or funds scope is authorized.

## PLAN -> BUILD -> VERIFY -> REPAIR

1. PLAN — COMPLETE. Confirmed the dirty V1.2 workspace, retained V1.1/V1.2 architecture, and mapped every production-usability finding to code and real Windows evidence.
2. BUILD — COMPLETE. Added the sidecar-owned resident monitoring runtime, dynamic tray/close lifecycle, official OS autostart, owned-backend watchdog/restart/degraded state, safe alternate-port startup, restricted WebView capabilities, always-mounted background notification bridge, schema-13 hydration audit, numeric-guard repair and current documentation.
3. VERIFY — COMPLETE. Re-ran the full Python/V1.1/V1.2 regression, required frontend build/lint/typecheck/test order, E2E/axe/overflow, audits, Cargo check, official GNU Tauri production build, packaged sidecar smoke, real public/provider/model live smoke and installed Windows lifecycle.
4. REPAIR — COMPLETE. Fixed every reproducible defect discovered during verification, including inactive-X not exiting, stale autostart UI state, idempotent missing-registry disable and English month-name numeric-guard equivalence, then repeated affected/full gates.

## Release state

- Contract: API/package `1.2.1`, schema `13`, `monitoring_runtime_v1`, `trigger_policy_v2`.
- Defaults: Monitoring off, auto-start off, resume off; no trigger/model scan at startup.
- Build entrypoint: from `D:\RJ\codex\ai-market-analyst\src-tauri`, run `cargo +stable-x86_64-pc-windows-gnu tauri build --target x86_64-pc-windows-gnu`.
- Installer: `src-tauri\target\x86_64-pc-windows-gnu\release\bundle\nsis\AI Market Analyst_1.2.1_x64-setup.exe`.
- Acceptance state: developer-complete; Sol independent acceptance pending.

The implementation remains loopback-only, Python-owned for triggers/risk/validation, 9B-only for Smart opportunity analysis, 4B-only for explicit news translation, and public-data-only. TradingView Lightweight Charts is a redistributable frontend renderer, never a market-data backend. Exit/restart acts only on the exact owned sidecar and never manages Ollama, ComfyUI or unrelated processes.
