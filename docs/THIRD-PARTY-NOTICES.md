# Third-party notices for AI Market Analyst V1.2

This inventory covers the direct runtime/build dependencies used to produce the V1.2 Windows package. It is an attribution and review aid, not legal advice. Transitive dependencies retain their own upstream licenses; the lockfiles and package metadata are the source of truth for a complete license review.

## Frontend

- `lightweight-charts` `5.2.1` — Apache License 2.0, copyright TradingView, Inc. The package license is present at `web/node_modules/lightweight-charts/LICENSE`. It is used only as a frontend renderer for bars supplied by this application. The product does not call TradingView as a market-data backend and does not ship a proprietary TradingView chart product.
- `react` `18.3.1` and `react-dom` `18.3.1` — MIT License.
- `react-router-dom` `7.18.2` — MIT License.
- `@tauri-apps/api` `2.11.1` — Apache-2.0 OR MIT.
- `@tauri-apps/plugin-notification` `2.3.3` — MIT OR Apache-2.0.
- `@tauri-apps/plugin-autostart` `2.5.1` — MIT OR Apache-2.0.

Development-only packages include Vite, TypeScript, ESLint, Vitest, Testing Library, Playwright and axe-core. Their licenses remain in `web/node_modules` and they are not required by the installed production runtime.

## Windows shell and Rust

- `tauri` `2.11.5`, `tauri-build` `2.6.3`, `tauri-plugin-notification` `2.3.3`, `tauri-plugin-autostart` `2.5.1`, `tauri-plugin-shell` `2.3.5` and `tauri-plugin-single-instance` `2.4.3` — Apache-2.0 OR MIT (as reported by Cargo metadata). The autostart plugin uses the transitive `auto-launch` `0.5.0` crate and Windows `winreg` registry access; their upstream terms remain in the Cargo lock/metadata.
- `serde` `1.0.229` and `serde_json` `1.0.151` — MIT OR Apache-2.0.

Cargo transitive crates are resolved by `src-tauri/Cargo.lock` and are not relicensed by this project. The application itself does not claim a third-party license for the product code.

## Python sidecar

- PyInstaller `6.21.0` — GPLv2-or-later with the PyInstaller exception permitting bundled non-free/commercial programs. The installed Python distribution contains the corresponding `COPYING.txt` license text under its `.dist-info/licenses` directory. The packaged sidecar is the generated application executable, not a redistribution of the PyInstaller development tool as a user-facing feature.
- FastAPI, Uvicorn, Pydantic, HTTPX and `websocket-client` are used by the local sidecar. Their upstream license files/metadata remain applicable; the current environment reports incomplete license metadata for some Python distributions, so this document does not make a blanket transitive-license claim.

## External services and data

- Ollama and the `qwen3.5:4b` / `qwen3.5:9b` model weights are external local dependencies. They are not bundled by the installer; their upstream model and software terms apply separately.
- Binance public REST/WebSocket and Google News RSS are public network sources used at runtime. No account credential, secret, proprietary data feed or authenticated exchange connection is shipped.

No paid account, secret, private key, broker connector, real-order code or proprietary chart library is included in V1.2.
