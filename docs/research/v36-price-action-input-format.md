# V36 Price Action Research Input Format

The V36 runner consumes frozen JSON and writes a new report path using exclusive file creation. It does not connect to a model provider, Gate, or the production execution path.

## Decision input

The top-level object has a decision_points array and optional cache_rows array. Each decision point contains a stable decision_id, timezone-aware decision_time, bars_by_timeframe for 15m/5m/1h/4h, and a current state_snapshot. Optional risk_mode and risk_inputs provide the frozen V35 economics and venue snapshot.

Each bar carries bar_start, bar_end, available_at, is_closed, quality_status, source, OHLCV values, and optionally timeframe. A bar is usable only when both its close and availability timestamps are strictly earlier than the decision time. Gaps, duplicates, out-of-order rows, invalid geometry, stale feeds, and incomplete four-frame coverage fail closed. The model sees at most 8 recent 15m bars, 6 recent 5m bars, and 4 bars per higher timeframe; the full causal history only contributes bounded objective summaries. Serialized A1/A2 payloads are limited to 32 KB.

The model-study state snapshot accepts only current equity, available margin, allowed instruments, open positions, and working orders. It excludes realized outcomes and unknown fields. Position and order IDs are omitted; deterministic references are derived from the allowed current-state fields and used to bind management proposals to an item present in that snapshot. Ambiguous duplicate references fail closed. A0 cache rows must include an identity that the runner independently recomputes from preserved original prompt, model input, current state, model ID, prompt version, and decision time.

Management proposals are analysis-only. REDUCE_POSITION requires a fraction in (0, 1], CLOSE_POSITION requires a known position reference, CANCEL_ORDER requires a known working-order reference, and TIGHTEN_STOP/UPDATE_PROTECTION must reference a known current position. A proposed stop must strictly tighten the current stop for that position's side. The lifecycle remains NOT_OBSERVED for gateway acceptance, venue fill, and complete close; no management proposal is executed.

risk_inputs uses the existing V35 diagnose_trade_proposal argument names. It must contain point-in-time equity, margin, fees, slippage, contract size, amount step, price tick, venue amount/notional limits, and maximum leverage. The study reports an unchanged proposal; it never resizes it.

## Outcome and review input

The review CLI accepts an array of records or an object with decision_records. Outcome and review rows must explicitly include both experiment_id (A0–A3) and decision_id. Outcome metrics require complete_close=true, a valid closed_at after the decision, and finite net_pnl_usdt. Rows without an exact arm key are ignored rather than copied across experiment arms.

The V25 sample option is descriptive coverage only. It verifies the supplied aggregates and emits NOT_RUN for A0–A3 when exact prompts, causal OHLCV, and point-in-time economics snapshots are absent. A prior V35 9-of-10 net-RR finding may be cited as its own proxy-economics result and must not be described as a V36 decision-quality finding.
