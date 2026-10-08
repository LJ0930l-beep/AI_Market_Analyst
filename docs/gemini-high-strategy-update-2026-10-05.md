# Gemini High model and strategy update

The active desktop client now requests `gemini-3.8-flash-high` through the local Antigravity Tools relay at `127.0.0.1:8045`. Requests use `reasoning_effort=high`. Availability requires a manifest match plus a successful inference probe; each completed decision must have a matching response identity. The relay credential stays in the user's local configuration and is excluded from configuration serialization.

Google's [Gemini 3 guide](https://ai.google.dev/gemini-api/docs/gemini-3) recommends clear, concise instructions with High thinking rather than long forced reasoning chains. This update follows that pattern while preserving local validation and exchange authority.

| Strategy | Cadence | Updated focus |
| --- | --- | --- |
| 闪电动量 | 5m | Compare momentum continuation, acceleration and liquidity-sweep recovery; distinguish initiation from chasing an exhausted move. |
| 趋势回测 | 15m | Compare expansion, first retest and false-breakout evidence; manage existing orders before re-entry. |
| 顺势回踩 | 15m | Use observed 1h structure and 15m acceptance/rejection; evaluate net target space and trend invalidation. |
| 多维防守 | 15m | Compare range rejection, VWAP reversion and crowded-position failure; missing derivatives remain unknown. |
| 价格结构 | 15m | Use point-in-time confirmed swings, BOS, sweeps and retests; avoid future pivots and late-arriving evidence. |

All five use one decision discipline: check account facts and system-owned positions/orders; compare the best opportunity with its strongest counterevidence; choose invalidation and target prices; then choose notional and leverage from available capital, pending-order reservations, authorized limits and exchange rules. LIMIT remains preferred; a valid pending-price setup can be submitted before the price touches it. MARKET remains available when justified. System positions require TP/SL; new evidence may justify dynamic protection changes. WAIT must identify verifiable missing conditions and a next observation condition or grounded price.

Strategy prompt version: `ai_strategy_pack_2026_10_v9_gemini_high`. Cycle prompt version: `ai_news_technical_strategy_v10_gemini_high`. Existing unmodified built-in instructions upgrade on read; operator-authored text and saved capital limits remain intact. Earlier decisions keep their original model identity and cannot be reported as Gemini trades.

Antigravity 4.9.4 merges object `anyOf` branches and stringifies enum values in its schema adapter. The old llama.cpp empty-array enum and conditional projection therefore produced malformed WAIT responses. The Gemini projection keeps real array types, concrete required confidence and a top-level action condition. The unchanged full local schema still enforces conditional fields and bounds. Relay schema normalization is not a proof of native grammar enforcement. Calls with a cycle deadline do not retry with another full timeout.

Verification: 156 targeted backend tests, 160 frontend unit tests, TypeScript check, production build, installed binary hash verification and the installed `/health/model` endpoint passed. The installed endpoint returned `available=true`, `actual_model_id=gemini-3.8-flash-high`, `model_identity_source=completion_probe`.

`python scripts/verify_gemini_strategy_relay.py` completed five real High calls using clearly labeled missing-market fixtures. Every result passed the full local WAIT contract, disclosed missing data and avoided invented trigger prices. These are inference/contract checks, not real-market profit tests or exchange execution acceptance. No trade was submitted by this verification. Strategy profitability remains unverified; the old technical proxy results are not Gemini returns.

Local credential-free evidence is in `reports/gemini-antigravity-connection-20261005/`; reports remain ignored by Git.
