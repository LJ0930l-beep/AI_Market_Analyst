# Five-strategy AI account replay

This experiment measures the five built-in AI strategy templates with the actual
local Bonsai model. It is a historical account simulation, not a proof of Gate
settlement or future profitability.

## Frozen experiment

- Window: 2026-10-02 00:00 UTC through 2026-10-03 00:00 UTC, selected before
  observing replay returns.
- Universe: BTC, ETH, SOL, XRP, BNB and DOGE USDT perpetual contracts. This is a
  declared liquid-contract benchmark, not the whole exchange universe.
- Five separate accounts, initially 1,000 USDT each, using the frozen built-in
  template defaults. This does not estimate a user's actual account profit.
- Native cadence: one 5-minute template and four 15-minute templates, totaling
  672 model decisions for the complete day. WAIT windows are retained.
- Public Gate closed 1m/5m/15m/1h candles, with 240 warm-up bars for the signal
  frames; timestamped historical funding. Historical contract rules are not
  available, so observed current public rules are explicitly a proxy.
- Only archived news whose publication and knowledge timestamps precede a
  decision may enter that decision. Missing historical OI, order-book and macro
  radar feeds are marked unavailable; today's feeds are never injected.

## Run

```powershell
python scripts/freeze_ai_template_history.py --start 2026-10-02T00:00:00Z --end 2026-10-03T00:00:00Z --output reports/ai-five-strategy-replay-20261003 --news-database 'D:\RJ\AI Market Analyst\data\market_analyst.sqlite3'

python scripts/run_ai_template_replay.py --manifest reports/ai-five-strategy-replay-20261003/history.json --db reports/ai-five-strategy-replay-20261003/pilot/results.sqlite3 --output reports/ai-five-strategy-replay-20261003/pilot/results.json --max-decisions 5 --model-budget-seconds 70 --runtime-status-url http://127.0.0.1:18765/v2/ai-session/status --allow-stopped-runtime

python scripts/continue_ai_template_replay.py --manifest reports/ai-five-strategy-replay-20261003/history.json --directory reports/ai-five-strategy-replay-20261003/full --model-budget-seconds 70 --runtime-status-url http://127.0.0.1:18765/v2/ai-session/status --allow-stopped-runtime
```

The last switch permits research only when the backend connection is refused.
Timeouts, unreadable status and a busy model slot still pause the experiment.
When the client is running, its next AI scan has priority. No trading session is
started or stopped by these commands.

Do not rerun the first command after a replay starts. Its frozen history hash,
strategy configuration, relevant source hashes and actual model weight identity
must remain stable. The pilot and full run use separate databases. Add `--resume`
to the pilot command to continue the same database rather than recall completed
decisions. An interrupted MODEL_STARTED claim requires explicit inspection; it
is never silently recalled.

A completed pilot may be promoted to a separate full-run database with SQLite's
read-only backup API, after verifying all five responses, source/history/model
hashes, native timestamps and input visibility. Preserve the pilot snapshot and
record the promotion. The full run then resumes those five completed decisions
rather than paying for duplicate model calls. Do not promote an incomplete or
unverified claim.

## Evidence and calculations

The isolated SQLite records each input context, raw model response, model
identity, input time, complete call duration, submission time and execution
receipt. Model duration advances the execution clock; new orders cannot match a
bar from before the model's decision. Existing orders and protection remain
active while the model thinks.

The HTML/JSON reports distinguish model waiting, OPEN proposals, accepted
entries, orders with at least a partial fill, closed trades, fees, funding and
pending orders. Orders with multiple fills are counted once by order ID.

- Return: ending account equity / initial equity - 1, including unrealized PnL.
- Win rate: net-profitable fully closed trades / all fully closed trades.
- No fully closed trades: win rate is undefined, not zero percent.
- Drawdown: sampled minute-close equity drawdown, not observed intraminute
  maximum drawdown.
- Unknown liquidation or unsupported account paths invalidate full-return
  comparison; later price rebounds cannot restore an invalid account result.
- Same-instrument entries use one-way netting and one common cross-leverage
  setting. Maintenance rates are observed public proxies, not historical Gate
  liquidation settlement. Ambiguous liquidation-before-target paths halt.

Fill price, quantity, partial fills and slippage are separate simulation
assumptions, consistent with the distinctions in
[QuantConnect's fill-model documentation](https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/trade-fills/key-concepts).
These assumptions are reported explicitly rather than treated as exchange
execution proof.

Outputs are local under ignored `reports/`. The replay never calls private Gate
APIs and its training-sample sink cannot append to the production SFT dataset.
Injected unit providers are labelled TEST_PROVIDER/TEST_INJECTED and never
qualify as actual Bonsai strategy evidence.

Small samples permit an initial comparison of observed outcomes; they do not
establish a dependable long-term win rate. Repeated scans of the same setup are
not independent completed trades.

## Participation diagnosis and repaired evaluation route

Production scan totals mix strategy versions, completed decisions and blocks
before inference. Report these separately from OPEN proposals, accepted orders,
fills and closed trades. A calibration sample failure is not a model WAIT.

Closed-bar inputs include the prior 20-bar high/low, excluding the trigger bar,
with separate breakout facts and reference timestamps. The existing current
range includes that bar and cannot serve as a same-bar close-above-own-high
requirement. These facts inform the AI without forcing an OPEN decision.

Gate WAIT generation requires model-authored missing conditions and a trigger
price or entry condition. Local validation also rejects whitespace-only text
and invalid numeric bounds. The pinned native converter ignores the Unicode
whitespace regex, so its projection omits that exact unsupported regex while
retaining required fields and minimum string/array lengths; Unicode whitespace
remains a local semantic check. Invalid responses cannot enter the SFT sink.

Preserve interrupted baselines and do not resume them after source changes.
The repaired route is evaluated in a new directory, with the same frozen
history. Debugging this window makes it an engineering evaluation, not an
untouched out-of-sample test. A subsequent new interval is needed before using
these results to select a production default.

## One-year proxy and preregistered AI segments (2026-10-04)

The user selected a one-year technical-rule proxy followed by actual-model
segment validation. The proxy uses BTCUSDT/ETHUSDT Binance USD-M monthly public
archives for 2025-10-01 through 2026-10-01 UTC. It has zero model calls and cannot
be described as Bonsai or Gate performance. Its five fixed rule translations,
1000 USDT independent accounts, 250 USDT notional trades and 10x research
leverage were frozen before inspecting annual returns. Maker/taker fees,
adverse protective-exit slippage and historical funding are included.

`reports/btc-eth-year-proxy-20261004` retains the 50 checksum-verified archives,
dataset, fixed configuration, all closed trades, independent Decimal ledger
audit and deterministic complete rerun. All five proxy net returns were
negative. Profitability diagnostics also show negative gross returns before
fees; increasing activity alone is not a demonstrated remedy.

The separate actual Bonsai protocol preregisters three Gate BTC/ETH calendar
windows: September 28 00:00-02:00 UTC, September 30 12:00-14:00 UTC and October 3
00:00-02:00 UTC. Each window runs all five native schedules (56 calls, 168 total)
with independent accounts and no carry across gaps. Report aggregate returns
against the sum of these independent initial balances, not an annualized rate
or a geometric compounded account that was never simulated. Pool wins by
closed-trade counts; never average five or three percentage win rates. No
closed trades means undefined win rate. News, OI and the historical radar are
unavailable in this technical-only protocol and must remain explicit.

The first three-window baseline was stopped after discovering that normalized
execution bars did not carry the frozen Gate contract rules into the simulator.
BTC bar base volume was consequently divided by a default multiplier of 1
instead of the frozen 0.0001, undercounting fill capacity by 10000x. This
invalidates that baseline's execution-performance interpretation, despite
consistent fee arithmetic. Preserve its records and source snapshots under
`reports/btc-eth-ai-segments-20261004`; do not resume it with repaired source.
The annual proxy uses a separate base-volume simulation and is unaffected.

Repaired AI runs require bars bound to their frozen instrument rules, distinct
result directories, the same actual model weight/context pin across all
windows, an OS-held single-driver lock, all three registered windows and all
168 scheduled decisions. Independent audits recompute fees from fill price,
quantity, contract multiplier and frozen maker/taker rates, rather than merely
checking that totals agree. Consistently erasing fees, omitting a window or
changing model weights must not manufacture a passing comparison.
