# Autonomous trading closure plan and acceptance

## 最新验收结论：2026-10-03（北京时间）

本轮源码修复、完整测试、桌面打包和新自然扫描已完成验收。客户端保留授权的
`gate_live` 会话，Bonsai 27B 身份已验证。该结论不等同于四套策略已证明盈利。

| 项目 | 结果与证据边界 |
| --- | --- |
| 后端 | 最终完整测试 950 passed、1 skipped；跳过的是当前 Windows 账户无法创建符号链接的用例 |
| 前端与桌面 | 前端 154 项测试、生产构建和 5 项 Windows 进程恢复测试通过；相关 88 个源文件未变 |
| 客户端包 | `20261003-061914` 已安装；应用与后端哈希匹配构建产物，218 个生产源文件保持冻结；未打入本地凭证、SQLite 或交易报告 |
| 新自然扫描 | 06:30:00.018 启动，06:31:22.642 完成；整轮 82.6 秒，真实模型请求 30.6 秒；输入 4014 tokens，输出预留 1024，安全余量 256 |
| 实际模型输入 | BTC 的 15m 保留 4 根 K 线和必要指标，1h 保留最新 K 线及 EMA/ATR；实际 user JSON 与冻结 packet 一致，两帧均可用 |
| 取数与过期时间 | 源技术档案 BTC/WLD/BMT 均 READY；取数时刻在校准之后，提案过期仍锚定原扫描起点加 240 秒 |
| 本轮交易结果 | AI 自主 WAIT，没有系统阻断、没有新订单；量能约为均值 0.20 倍，下一触发价为 84,869 |
| 入模宽度 | 本轮实际仅分析 BTC；WLD/BMT 因 5,500-token 延迟预算延后，不能把三份 READY 档案说成模型分析了三个币 |
| 原生 TestNet | 已验证限价挂单/撤单/成交、全仓 100x、保护覆盖/替换、部分减仓/全部平仓、重启去重、精确 ID 清理及完整成本结算 |

尚未完成的原生场景是**部分开仓成交**与**止盈止损实际触发**，其软件回归已通过，
不能替代交易所真实观察。四策略的历史重放仍为 `NOT_RUN`，前向盈利样本不足；
不据此选择“盈利最优”默认策略。单轮技术刷新仍最多覆盖 8 个托管标的，这是规模边界。

本轮修复的真实缺陷包括：校准后新 K 线被旧取数时刻过滤、READY 的 1h 数据被
预算压空、深度压缩丢失原交易状态、Gate/CCXT 别名使托管行情上下文被误延后。
末两项经过全量测试/独立复核返修，并增加了进入项目测试库的别名回归。

## Scope and ownership

The supervising agent defines the implementation boundaries, reviews the code,
and independently runs acceptance. Each GPT-6 Luna Max developer owns one bounded
milestone. Disjoint execution and strategy-evidence milestones may run in parallel;
shared interfaces are agreed before editing. Existing authorized changes are the baseline and must be
preserved. Runtime databases, exchange credentials, and account reports remain
local and untracked. Development tests use isolated stores.

The active product route is Gate USDT perpetual trading with Bonsai-led decisions.
The program supplies exchange facts, managed inventory and account-wide margin;
AI chooses opportunities, size, leverage, entries and dynamic stop/target prices.
All four strategies use the same execution and settlement infrastructure.
This work does not introduce new discretionary entry thresholds.

## M1: Observed fills, settlement, and decision memory

1. Give system entries an immutable local accounting episode linked to the
   account, environment, entry intent, Gate order ID and strategy revision.
   An accounting episode is not proof of ownership of a remote net position.
2. Read paginated private trade/order history with an explicit coverage result.
   Deduplicate by immutable venue trade identity and resume safely after restart.
3. Attribute owned entry fills and proven exits, including manual closing of a
   system position when the remote history establishes an unambiguous match.
   Foreign entries, mixed exposure, missing history, or unsupported hedge facts
   must remain unresolved rather than inventing a settlement.
4. Preserve actual contract multiplier, fee amount/currency/source and timestamps.
   Missing fees or funding remain explicit; never manufacture a zero cost.
5. Separate account equity change from strategy realized profit. Only reconciled
   system trades contribute to strategy win/loss and realized-profit statistics.
6. Reconcile a complete close into the original AI decision's memory with evidence
   and a cost-aware outcome. WAIT decisions do not receive trade outcomes.

Acceptance: entry followed by partial and full close, duplicate history pages,
restart, manual close, foreign same-symbol exposure, unknown fees, long/short
and contract multiplier all receive meaningful tests. An isolated replay of
observed private exchange records must reproduce the closed trade and its memory.

## M2: Model-facing management scope

Separate proven system positions and orders from foreign account exposure in the
model packet. Entry limits, native protection orders and foreign orders have
distinct roles. Account equity/available/used margin still include all exposure.
Preserve scope and identities through context-budget compression. Make the
zero-managed-position and zero-managed-order case explicit. Do not force OPEN
or relax economic/exchange/protection checks to make an acceptance scenario pass.

Acceptance: external native protective orders cannot appear as actionable system
entry orders, owned orders remain manageable by exact ID, and 8K context budgeting
retains the facts required by the existing gateway ownership check.

## M3: Execution lifecycle and installed client

Independently inspect the shared route for LIMIT submission/cancellation,
partial fills and protective quantity, replacement ordering, full close and orphan
protection cleanup, crash/restart recovery and duplicate-submission prevention.
TestNet mutation acceptance is confined to owned verification orders, using
real exchange market data and exact remote IDs. Live acceptance uses natural
AI decisions and read-only private evidence; there are no forced Live trades.
Unavailable credentials or a scenario not actually observed is reported as
not verified, never as a pass inferred from fixtures.

Acceptance: run relevant regressions, full backend suite, frontend tests/build,
and desktop package checks. Install the accepted client in an idle scan window,
verify its binary hash, account binding and model/session health. Report every
lifecycle scenario as verified, needs repair, or not yet observed.

## M4: Strategy evidence

Freeze the four strategy definitions and compare point-in-time historical replay
and forward observations using net realized returns, fees, drawdown and sample
coverage. Data-availability and execution validity are separate from investment
performance. A profitable guarantee is never an acceptance criterion.
The default strategy is changed only when comparable outcome evidence supports it.

## Evidence and release gate

Each milestone returns changed files, executed checks and their outputs, residual
limitations, and a reproducible acceptance procedure. The supervisor returns
failed work to the same developer. Current runtime facts and private order evidence
are stored under ignored local reports; source documentation contains no secrets.
The platform is not marked fully accepted while a required lifecycle or settlement
scenario remains unverified. An autonomous real-time forward test continues to
require elapsed market time and cannot be replaced with a short fixture replay.

## Supervisor checkpoints

- Baseline source diff and file hashes are saved in ignored local reports.
- A real system entry and manual close were read back with complete native trade
  history and a matching position-close result. The independent arithmetic is
  reserved for acceptance; the developer does not receive a hardcoded outcome.
- Native conditional-order cleanup in the old TestNet acceptance runner used
  ordinary order cancellation without validating its result. M3 repaired that
  false-positive path; conditional cleanup now requires native exact-ID readback.
- Installed-client health and process state must be rechecked after packaging;
  an earlier RUNNING snapshot is not evidence that the current process is alive.
- The supervisor's observed-record M1 replay reproduces the Gate full-cost
  close and original decision memory in an isolated store. Twelve independent
  adversarial checks passed, including interrupted memory writes, late memory
  links, foreign exposure, incomplete costs and restart deduplication.
- Fresh private Gate GETs through the production history-sync service also
  reproduced the observed close in an isolated store. No remote orders were
  sent and the installed runtime database was not modified by that check.
  Durable page-cursor crash recovery also passed an independent isolated test.
  Production scheduling still requires the installed-client verification.
- M3 inspection also identified partial-fill cancellation/reservation state,
  tick-normalized protective replacement verification, and same-intent
  idempotency-conflict receipt preservation as required repairs. A conflict
  must never overwrite an accepted remote order's durable identity.
- M1 source acceptance passed 53 related backend tests and all 154 frontend
  tests. The observed manual close's net result matches native private accounting;
  private amounts and order IDs remain in ignored local reports. This financial
  result is not evidence of an autonomous AI exit. Compatibility
  with an older compacted bundle was verified against its exact persisted native
  snapshot, without inventing an AVAILABLE component status.
- The five Windows desktop process/recovery tests also passed independently.
- M2 passed 156 related tests in the supervisor run and eight isolated management
  challenges. Owned entry/protection IDs survived foreign orders and more than
  one thousand terminal intents; a verified prior settlement excluded a later
  same-symbol reopen from system ownership. A verified lesson survived twenty-four
  newer WAIT records. The production-sized 8K challenge retained order roles,
  scope, amounts and prices while preserving account-wide margin facts.
- Strategy qualification must distinguish financially settled system entries
  from autonomous exits. A user/manual or unverified external close can produce
  valid financial memory but cannot establish autonomous strategy performance.
  Native protection qualification requires the exact parent conditional order,
  its triggered execution order and actual exit fills, all in the same scope.
  Gate documents conditional `trade_id` as the execution order created after
  triggering; it must not be confused with the my-trades fill ID. Reference:
  https://www.gate.com/docs/developers/apiv4/en/futures/ .
- The supervisor's M3 fixtures separately challenge accepted-receipt preservation,
  canceled partial entries, cancellation/fill races, nonterminal cancellation
  readback, sub-tick protective replacement, replacement restart, conditional
  execution identities and exact-ID orphan cleanup. These isolated fixtures verify
  software behavior; native exchange acceptance remains a separate gate.
- Before release, the installed older client still reports an active Live session,
  while recent scheduled model calls time out. A healthy model endpoint alone is
  insufficient. Release acceptance requires a fresh completed natural model cycle
  from the newly installed binary, with scoped inputs and provider provenance.

## Native TestNet checkpoint, 2026-10-03

The supervisor used an isolated accounting store and the production gateway with
real TestNet market data. The native exchange evidence verified a passive limit
order, idempotent restart without duplicate submission, exact-ID cancellation,
a filled limit entry, cross margin with the requested 100x limit, native stop and
target coverage, tick-normalized target replacement, partial reduction followed
by full reduction, and exact-ID conditional cleanup. Fresh private readback
confirmed zero positions and zero open orders in that verification account.
Private order IDs and raw account records are retained only in ignored reports.

Native trade history and position-close accounting also reconcile to one full-cost
settlement. Gate can round a position's opening timestamp to whole seconds while
individual fills retain milliseconds. The matching repair tolerates only that
bounded precision difference; account, contract, side, size, costs and ambiguous
lifecycle checks remain authoritative. Independent observed-record replay and
fresh production history synchronization both passed.

Actual canceled protection readback uses `initial.is_reduce_only`, standalone
`me_order_id=0`, and a truncated client label. Terminal capture and attribution
support that native shape without requiring an invented association or label
suffix. Conflicting reduce flags, identities, accounts, contracts, sides and
recognizably contradictory leg labels remain rejected. Synthetic succeeded
derivatives of that saved wire shape passed independent challenges; they are
software tests, not evidence that a native stop or target actually triggered.

The actual native partial reduction was verified. A partial **entry** fill and a
native stop/target **trigger** have not been observed in this run. An ambiguous
protection creation timeout with no returned ID remains fail-closed; the system
does not blindly retry an order whose remote creation cannot be disproved.

## Inference and strategy-evidence release requirements

The pre-update installed client repeatedly exceeded its 120-second provider
timeout. Its observed packet contained 5,560 input tokens and reserved 2,048
output tokens. The revised fitter targets a 5,500-token working window, reserving
1,024 output tokens and 256 safety tokens; the exact stored packet measured 4,177
input tokens after fitting. All three selected symbols survived. Proven owned
inventory is mandatory: it can use the verified 8K fallback rather than lose
order identities, amounts, prices or account-wide margin facts.

The provider uses one request bounded by the cycle's remaining monotonic deadline.
Hidden JSON self-repair and a second Gate decision call are disabled. Invalid or
late output produces an explicit failed-cycle audit. These changes require a
fresh naturally scheduled completed Bonsai decision from the installed package;
unit tests and a healthy model endpoint alone do not establish completion.

Strategy evidence is separated into financial settlements and proven autonomous
exits. The supervisor verified that a real manual close preserves its financial
memory but cannot qualify as autonomous strategy performance. Comparison cohorts
retain the actual strategy configuration hash and revision. Historical replay is
explicitly `NOT_RUN`; the minimum 100 qualifying trades and 30 observation days
are comparison thresholds, not new entry gates. No default winner or profitability
claim is inferred from the current insufficient observations.

Before the candidate-refill follow-on, the supervisor backend run passed **915 tests**, with one skipped test and
one existing Starlette/httpx deprecation warning. The strategy-evidence suite was
also rerun after the final native-shape repair: **39 passed**. Independent
challenges using observed financial facts and explicitly synthetic completed
decision actors qualified the complete valid actor chain, excluded missing or
foreign/revised exit actors, and preserved the genuine financial settlement.
The observed manual close remained financially valid and autonomously unqualified.
The final 8K owned-inventory challenge also passed after the inference repair.
Frontend acceptance passed **154 tests** and the production build; Windows desktop
process/recovery acceptance passed **five tests**. Desktop installation and fresh
scheduled model completion remain separately recorded release checks.

## Installed release checkpoint

The fresh PyInstaller sidecar, Tauri release executable and NSIS installer built
successfully and the local desktop installation was updated. Installed app and
sidecar SHA-256 values matched the build artifacts. The package manifest contains
the new settlement and exit-attribution modules, and does not contain the local
trading database, exchange credential files or acceptance account reports.

The installed client restarted against its configured data root, restored the
authorized `gate_live` session and verified the actual Bonsai artifact. The next
naturally scheduled cycle started at 02:00 Asia/Shanghai on 2026-10-03 and completed
at 02:02:20. Its verified model packet contained 4,149 input tokens with a 1,024-token
output reserve; the model request took 32.8 seconds and the complete data/decision
cycle took 140.8 seconds. Raw completed response, request hash, prompt binding,
schema validation and durable model-completion provenance all passed independent
read-only checks. Background native settlement synchronization reported AVAILABLE
with no errors.

That natural model result was WAIT, with an explicit missing-condition explanation
and next trigger price. It was not a SYSTEM_BLOCKED result and did not create an
order. This proves the installed scheduled inference route completed; it does not
prove a new autonomous filled entry or profitable strategy performance. The native
TestNet execution and financial acceptance above remain separate evidence.

## Candidate-input follow-on found during installed acceptance

The natural model cycle exposed a real mismatch: initial screening accepted one
fresh closed bar, while the later model technical context required a fresh,
contiguous history of at least 32 valid closed bars in every requested frame.
Missing history could therefore consume a scarce deep-analysis slot without
returning to the existing candidate refill loop.

The follow-on uses the same `technical_context` readiness contract at screening,
before quote acceptance. The existing capped refill can then try the next ranked
new-opportunity candidate. Proven system positions and pending orders stay exempt;
their identities cannot be discarded because market history is missing. This is
an input-availability correction, not an additional technical entry threshold.
Per-frame failures remain in the selection audit.

Independent supervisor challenges passed eleven cases, including empty history,
31/32-bar boundary, gaps, stale/future/invalid data, configured 5m frames, owned and
pending inventory preservation, and bounded all-unavailable selection. A separate
read-only check against the native public bar archive agreed with the production
helper: the observed stale B2 signal frame failed and the complete WLD frames
passed. Developer focused and adjacent suites passed 31 and 130 tests. The final
post-follow-on package and whole-suite checks are recorded after this checkpoint.

## Final input-quality and offline-budget acceptance

Offline tokenizer fallback exposed a prompt-projection loss of NOFX candidate
sources and excluded-symbol metadata. The fitter now retains those fields and
compact indicator-snapshot identity/status. Its contract test explicitly forces
the conservative tokenizer path, instead of relying on the local model service
being available during source tests.

The technical-context and recent-volume readers now share strict closed-bar shape
validation: actual configured duration, explicit boolean/SQLite closed flags, and
no synthetic or malformed synthetic flags. Invalid adapter rows are skipped
without discarding a complete valid history. Twenty independent isolated cases
passed, including per-bar duration, string flags, historical synthetic rows and
ordinary SQLite integer flags. These are explicitly synthetic software fixtures,
not native market acceptance evidence.

The native-bootstrap writer now rejects synthetic-marked or malformed quality
metadata before hashing, caching, audit insertion or any derived market/radar
write. The supervisor independently verified all eight derived evidence tables,
native-shaped replay idempotence, rejection without overwriting existing facts,
and acceptance of a genuine-shaped copy after rejection of an otherwise identical
synthetic input. No runtime database was used in those tests. There is no claim
that synthetic data was observed in the actual account's scan history.

The existing per-cycle technical refresh cap remains eight distinct managed
symbols. Complete owned order/account facts survive prompt projection, but more
than eight managed symbols cannot all receive technical refresh in the same
cycle. This is a documented scalability boundary, not an inventory-pruning
exception or a verified all-symbol management claim.

## Source and installed package checkpoint before the final input repairs

After the input-quality and offline-budget repairs, the final supervisor backend
run exited successfully: **937 passed, one skipped**, with one existing
Starlette/httpx deprecation warning, in 734.49 seconds. The previously failing
NOFX metadata, PAPER model-call and runtime-lease tests pass. The PAPER fixtures
now provide explicitly labeled complete closed-bar histories and a fresh local
quote; their original model-call and lease-fence assertions remain intact.

Final sidecar build `20261003-025739`, the Tauri executable and a fresh NSIS
installer were built successfully. Installed app SHA-256 is
`EC4640BE5787D79494A9DEAFF82BEA2A548974E8B03E6C9F8F8D198F10D5714C`;
installed backend SHA-256 is
`18A61305BF6B681B77372D32D5399F5D53421C8C6F6F7FC8EC61CE8C6823F4F0`.
Both match their build artifacts. The supervisor verified the new financial
modules in the package and found no packaged credential files, local trading
database or acceptance account reports. All 218 captured production source files
were unchanged between source freeze and package verification.

That installed release started at 03:00:35 Asia/Shanghai on 2026-10-03 using
`D:\RJ\AI Market Analyst`. Its backend ownership, actual verified Bonsai artifact,
8K context, restored `gate_live` session and persisted explicit Live unlock were
checked through read-only endpoints. Its 03:15 natural scan completed as a verified
Bonsai WAIT. Subsequent input inspection failed the stronger input-quality gate
below; a completed inference alone is therefore not final M2 acceptance.

## Actual prompt defects found in the final natural-cycle inspection

Read-only inspection of the 03:15 cycle reproduced an as-of race. Calibration
refreshed WLD bars at 03:15:40–41, but the scanner and final technical reader still
used the pre-calibration 03:15:37 instant. Those genuinely available bars were
filtered out, yielding a BLOCKED instrument despite successful initial screening.
The repair captures one post-calibration as-of instant shared by the scanner,
news reader and final context; it does not backdate availability or move the
original scheduled scan. A deterministic isolated regression verifies both READY
frames and preservation of the original scheduled timestamp.

Inspection of the actual frozen user message from the 05:15 cycle found a second
defect: its 1h frame remained labeled READY while every candle and indicator had
been removed by budget compression. The real model explicitly cited empty 1h
background in its WAIT explanation. The full source-evidence copy was not empty;
checking that copy alone would have missed this failure.

The same exact-user-message check was run against the latest eight completed
natural cycles (04:00 through 05:45). All eight had unusable compacted background;
four model explanations explicitly mentioned the empty 1h data. These WAITs are
not sufficient evidence to blame the investment strategy or declare M2 accepted.

The release gate now compares the exact serialized user message with the fitted
packet and checks the necessary signal/context frames for real candles, last-close
time, EMA and ATR. The saved pre-repair packet fails that gate as expected. Final
acceptance requires a new installed release and a fresh naturally scheduled
decision whose actual message passes, rather than counting an earlier WAIT.

The full regression and independent review then found two further compression
issues. Entering a deeper budget step dropped the original execution `status`
from settled memory; the existing three-symbol contract test caught that loss.
The repair preserves the existing status rather than weakening the assertion.
Owned symbol protection also compared raw Gate/CCXT aliases with canonical scan
symbols. An isolated probe reproduced a managed `EXTRA4/USDT:USDT` row surviving
while its `EXTRA4USDT` technical context was wrongly deferred. A shared symbol
normalization now protects all six position/order collections. The supervisor's
explicitly synthetic 24-case matrix verifies context, native row aliases, order
IDs, quantities and 8K bounds without account or network access.

## Accepted final source, package and natural-cycle checkpoint

The repaired source passed the complete supervisor backend run: **950 passed,
one skipped**, one existing Starlette/httpx warning, exit code zero, in 297.95
seconds. Twelve project regression tests cover six owned inventory roles and
two native symbol aliases. Independent read-only final review and the supervisor's
24-case alias matrix passed. Source documentation and ignored acceptance scripts
were the only supervisor-written artifacts; production repairs stayed with their
assigned developer.

Package `20261003-061914` built a fresh PyInstaller backend, Tauri executable and
NSIS installer. Installed app SHA-256 is
`44E2C912376218A13338A7366C8988B17BE3D4E7B11227C442549116D6D57BEC`;
backend SHA-256 is
`C902C42BE59552C67E547E73D05A5C4C100C753F99A93A099C67A4D95480F397`.
Both match their build artifacts. The installed release started at 06:23:15
against its configured data root and restored the authorized Live session.

The fresh 06:30 naturally scheduled cycle completed a verified Bonsai WAIT. Its
actual frozen user message, completed raw response, request hash, local schema
validation and durable completion receipt all passed. The fitted BTC context has
four 15m bars, one 1h bar, and genuine EMA/ATR values; it no longer labels an empty
context READY. The full source has three READY symbols, while WLD and BMT are
explicitly deferred from model input under the latency budget. That distinction
is part of acceptance, not hidden by the source-evidence status.

The model cited low volume and missing breakout/retest confirmation, named the
next trigger, and did not create an order. Dynamic risk reported MARGIN_ONLY and
entry allowed; no program risk block or schema/model failure occurred. This is
valid inference-route evidence, not proof of a new autonomous Live fill or of
investment performance. No artificial Live orders were created for acceptance.
