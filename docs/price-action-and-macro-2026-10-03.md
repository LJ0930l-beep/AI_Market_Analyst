# 价格行为策略、官方宏观公布值与浅色面板修复

## 范围与验收

本次新增第五套价格行为模板，保持现有四套策略、当前选中策略及账户资金配置。新模板复用现有 Bonsai 决策和 Gate 执行链路；安装更新不自动启用新模板，也不为验收强制制造实盘订单。

同时修复浅色主题遗留深色容器，以及宏观周历没有实际公布值采集的问题。本周21项数字指标、界面显示和真实模型输入分别核验；代码测试、公共源读取、安装版回读和交易决策验收按证据独立记录，见文末。

## 价格行为是否有可核验案例

有，但不能把其他规则回测的收益当成这里的 AI 策略收益。

Deprez 与 Frömmel 的研究使用 Bitstamp BTC/USD 数据，样本外区间为 2014–2021 年，按过去一年选规则、每月更新组合，计入手续费和买卖价差。它测试了支撑阻力、通道突破等价格规则。下面使用论文表 3(c) 中按平均收益筛选的规则组合；不是本项目的单一规则，也不是 AI、Gate 合约或 100 倍杠杆结果。

| 样本外规则组合 | 原文总对数收益 | 换算约累计收益 | 换算约年复合收益 | 原文年化 Sharpe |
| --- | ---: | ---: | ---: | ---: |
| 支撑阻力 S&R | 3.11 | +2,142% | +47.5% | 0.74 |
| 通道突破 CB | 3.48 | +3,146% | +54.5% | 0.79 |
| 同期买入持有基准 | 4.15 | +6,243% | +68.0% | 0.66 |

换算为 `exp(总对数收益) - 1`；年复合收益使用 `exp(总对数收益 / 8) - 1`。原表只有两位小数，因此这些换算均为近似值，不应读为精确到个位的实盘收益。支撑阻力和通道突破的累计收益低于同期持有基准，年化 Sharpe 略高；这说明该样本里的风险收益权衡，而非价格行为必然更赚钱。原表最大对数回撤分别为 -1.01、-1.18，换算跌幅约 63.6%、69.3%，同样不能忽略。

来源：[作者所在大学提供的论文全文，表 3](https://backoffice.biblio.ugent.be/download/01HY3C3S169G1N6QNYR55NZMFB/01HY60XZGZYHNQ6188MSVJT0SG)，[期刊发表页面](https://www.sciencedirect.com/science/article/pii/S1059056024003010)。

2026 年 Moser 与 Brauneis 另有加密市场 K 线形态研究，使用 2018-07 至 2022-01 的小时 OHLC、55 类反转形态，发现部分形态有统计预测力。公开摘要没有提供足以对照的完整可交易净收益率，所以不编造一个“蜡烛图策略收益”。来源：[原论文页面](https://www.sciencedirect.com/science/article/pii/S1059056026002716)。

## 本项目的实现原则

- 新模板 `price_action_structure`，15 分钟扫描，以 1h 为背景。
- 只计算真实、连续、已收盘 K 线中的结构事实：确认摆动点、之前的区间、收盘突破、扫过关键位后收回、突破后回测。
- 摆动点必须等右侧确认 K 线实际出现才可使用；区间不包含当前触发 K 线；任何未来、合成或坏形状数据不能生成可靠结构。
- 结构摘要携带来源与确认时间，在短上下文压缩后也能引用。它不生成 OPEN 指令、置信分数或新的强制入场门槛。
- AI 比较结构、账户资金、已有系统持仓/委托、费用及新闻，自主决定开多、开空、挂单、管理持仓或等待。没有相关新闻不机械否决清晰的技术机会。
- 既有账户保证金上限、交易所实际限制、原生止盈止损、精确订单归属、幂等与真实回执继续共用。
- 本模板收益为未验证；真实开仓、工程测试通过均不等同于盈利证明。

## 宏观数据缺口

此前 `core/macro_calendar.py` 的周历导出只提供日程、前值和预期。程序明确把 `actual` 写成 `None`，状态固定 `actual_supported=False`，没有第二条官方公布值采集链路，所以重复刷新也无法取得实际数值。

修复方向是保留周历预测，按事件准确匹配官方发布，分别记录统计月份、发布时刻、获取时刻、单位和来源。已过公布时刻但未抓取成功，必须显示失败或待同步；未映射来源应显示具体覆盖缺口，不能伪装“待公布”。

AI 输入只使用当轮已经获取到的官方值。之后回填的实际值不能进入较早的历史决策，也不自动产生宏观交易放行指令。

官方核对来源：[BLS 就业报告](https://www.bls.gov/news.release/empsit.nr0.htm)、[Eurostat 欧元区通胀初值](https://ec.europa.eu/eurostat/en/web/products-euro-indicators/w/2-02102026-ap)。

## 本周官方源覆盖与数据时序

2026-10-03 的独立公共源批量回读确认：本周 31 项日程中，21 项数字指标全部有已核验公布值；10 项讲话/声明单独处理，其中 6 份官方文本、1 份官方录播已匹配。该批量回读使用独立审计缓存，没有写生产账户数据库、调用模型或制造订单。安装版的实际接口与自然 AI 请求另行验收，见下文。

| 事件 | 官方实际值 | 参考期 | 原始发布来源 |
| --- | --- | --- | --- |
| AUD Cash Rate | 4.60% | 2026-09-29 | [Reserve Bank of Australia (RBA)](https://www.rba.gov.au/media-releases/2026/mr-26-27.html) |
| CAD GDP m/m | 0.0% | 2026-07 | [Statistics Canada](https://www150.statcan.gc.ca/n1/daily-quotidien/260929/dq260929a-eng.htm) |
| USD CB Consumer Confidence | 81.9 | 2026-09 | [The Conference Board](https://www.conference-board.org/topics/consumer-confidence/) |
| USD JOLTS Job Openings | 7079K | 2026-08 | [BLS](https://www.bls.gov/news.release/archives/jolts_09292026.htm) |
| AUD CPI m/m | 0.7% | 2026-08 | [Australian Bureau of Statistics (ABS)](https://www.abs.gov.au/statistics/economy/price-indexes-and-inflation/consumer-price-index-australia/latest-release) |
| AUD CPI y/y | 4.0% | 2026-08 | [Australian Bureau of Statistics (ABS)](https://www.abs.gov.au/statistics/economy/price-indexes-and-inflation/consumer-price-index-australia/latest-release) |
| AUD Trimmed Mean CPI m/m | 0.2% | 2026-08 | [Australian Bureau of Statistics (ABS)](https://www.abs.gov.au/statistics/economy/price-indexes-and-inflation/consumer-price-index-australia/latest-release) |
| EUR German Prelim CPI m/m | 0.6% | 2026-09 | [German Federal Statistical Office (Destatis)](https://www.destatis.de/EN/Press/2026/09/PE26_348_611.html) |
| USD ADP Non-Farm Employment Change | 90000 jobs | 2026-09 | [ADP Research](https://mediacenter.adp.com/2026-09-30-ADP-National-Employment-Report-Private-Sector-Employment-Increased-by-90,000-Jobs-in-September) |
| USD Core PCE Price Index m/m | 0.2% | 2026-08 | [BEA](https://www.bea.gov/news/2026/personal-income-and-outlays-august-2026) |
| USD Final GDP q/q | 2.2% | 2026-Q2 | [BEA](https://www.bea.gov/news/2026/gdp-third-estimate-industries-corporate-profits-state-gdp-and-state-personal-income-2nd) |
| USD Final GDP Price Index q/q | 6.1% | 2026-Q2 | [BEA](https://www.bea.gov/news/2026/gdp-third-estimate-industries-corporate-profits-state-gdp-and-state-personal-income-2nd) |
| CHF CPI m/m | 0.0% | 2026-09 | [Swiss Federal Statistical Office (FSO)](https://www.efd.admin.ch/en/newnsb/wNfni1DvzKuQ) |
| USD Unemployment Claims | 197000 claims | 2026-09-26 | [U.S. Department of Labor](https://www.dol.gov/ui/data.pdf) |
| USD ISM Manufacturing PMI | 54.5 | 2026-09 | [Institute for Supply Management](https://www.prnewswire.com/news-releases/manufacturing-pmi-at-54-5-september-2026-ism-manufacturing-pmi-report-302894520.html) |
| JPY Tokyo Core CPI y/y | 2.7% | 2026-09 | [Statistics Bureau of Japan](https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-t.html) |
| EUR Core CPI Flash Estimate y/y | 2.5% | 2026-09 | [Eurostat](https://ec.europa.eu/eurostat/en/web/products-euro-indicators/w/2-02102026-ap) |
| EUR CPI Flash Estimate y/y | 3.8% | 2026-09 | [Eurostat](https://ec.europa.eu/eurostat/en/web/products-euro-indicators/w/2-02102026-ap) |
| USD Average Hourly Earnings m/m | 0.1% | 2026-09 | [BLS](https://www.bls.gov/news.release/archives/empsit_10022026.htm) |
| USD Non-Farm Employment Change | 29K | 2026-09 | [BLS](https://www.bls.gov/news.release/archives/empsit_10022026.htm) |
| USD Unemployment Rate | 4.2% | 2026-09 | [BLS](https://www.bls.gov/news.release/archives/empsit_10022026.htm) |

美国数字源覆盖 BLS、BEA、DOL、ADP、Conference Board 与 ISM；国际源覆盖 Eurostat、日本统计机构、瑞士官方公告、德国 Destatis、澳大利亚 ABS/RBA、加拿大 Statistics Canada。事件必须准确匹配标题、地区、统计期间和单位；德国全国 CPI 不会用州级或 HICP 替代，澳大利亚季调月率也保留口径。

ISM 网站在本机直接读取时转向登录页，因此使用 ISM 自己在 PR Newswire 发布的完整原始稿作为后备。程序只从固定机构频道发现当期稿件，并核对发布机构、月份、发布日期和时间；不硬编码 54.5 或当期文章地址，也不将搜索摘要作为公布值。

BLS 就业与时薪涨幅明确标为由官方水平序列计算。发布时刻若来自日历，保留 scheduled 标记；实际获取、解析核验完成和可用于 AI 的时刻分别记录。缓存跨重启保留，但安装新适配器后会对已有日程进行一次补齐，不再被旧的 15 分钟冷却静默挡住。冷却中的手动刷新会明确返回倒计时。宏观官网读取不能阻挡行情缓存发布。

## 讲话、声明与录播

讲话不具备通用的“实际公布数字”，不再显示成“公布值来源待接入”。已核对的情况包括：

- Waller、Schlegel、部分 Lagarde 发言及 RBA 声明/发布会：官方文本已匹配。
- Kashkari 2026-09-30：官方页面提供活动介绍和完整录像，没有逐字稿；标为 OFFICIAL_RECORDING_AVAILABLE，不能叫作讲话全文。根代理实读页面确认 Full Event (video) 链接。[官方活动页](https://www.minneapolisfed.org/speeches/2026/neel-kashkari-qa-at-the-council-on-foreign-relations)
- Lagarde 2026-09-29：ECB 周历明确说明不提供文本；不能用 9 月 30 日发布、在其他日期进行的访谈替代。[ECB 周历](https://www.ecb.europa.eu/press/calendars/weekly/html/index.en.html)
- Bailey 2026-10-01：官方会议页确认有开场发言，尚未找到对应的完整正文。[会议页](https://www.bankofengland.co.uk/events/2026/october/boe-future-of-money-conference-in-celebration-of-charles-goodhart)
- Trump 2026-09-30：官方有多条同日视频，无法按时间唯一对应日历事件，保持未匹配，不能随意选一条冒充。[官方视频库](https://www.whitehouse.gov/videos/)

后三项正文缺口保留明确状态；它们不计入数字源同步失败，也不会被填入假数字。

## AI 输入、引用与上下文

官方数字最多以四条最近事实占用一个 MARKET_WIDE 新闻名额。真实值、预期、来源、统计期与获取时刻均保留；不把后来回填的值带入较早历史决策，也不添加宏观方向交易指令。

Bonsai 当前核验窗口为 8192 tokens。价格结构和宏观事实使用有字典与时间索引的无损压缩；持仓/挂单必需证据不能为了容纳新功能被丢弃。若必要证据确实放不下，程序明确阻断。

11:15 旧安装版曾把一条行情引用漏掉字符，导致整轮拒绝执行。现按实际压缩后的请求生成币种和 evidence_refs 枚举，交给本机 llama.cpp 原生 JSON Schema grammar 约束。完整范围、字段长度和交易语义仍由本地合同核验；不进行模糊引用修补，也不新增模型失败后的自动交易重试。

根代理使用真实 Bonsai 服务发起非交易诊断：故意要求 WRONG_ID 与 invented_ref，生成结果只包含 DIAGNOSTIC_ONLY 和 EXACT_VISIBLE_ID，本地完整校验通过。诊断不读取账户、不写交易数据库、不创建订单。安装后还须核对自然轮次的实际请求、原始响应及模型身份，不能用这个小探针代替交易验收。

## 工程验收

- 前端 28 个文件、158 项测试通过；TypeScript 校验通过。讲话原文、录播和刷新冷却有独立显示测试。
- 后端全量运行 1040 项通过，1 项因 Windows 符号链接权限条件跳过；最后新增/调整的适配器、时序、输入、grammar 和行情刷新测试另有 83 项通过。
- 官方实读：21 项数字全部 VERIFIED，6 份文本、1 份录播；没有生产数据库手工回填。
- 浅色界面：仓位试算、保存条与证据卡片使用纸色背景和深色文字，消除对应旧深色容器。
- 价格行为策略可选，但未替换原四套或当前默认。独立结构验证使用真实 Gate 已收盘缓存；本策略收益仍未验证。

## 最新安装与自然轮次验收

2026-10-03 12:54（HKT）已更新本机桌面端与 Python sidecar，构建标识 `20261003-124945`。安装文件与本次产物 SHA-256 一致；冻结的 223 个源文件无变更，8 个必要模块全部包含在安装包中，打包清单没有 `.env`、本地 SQLite 或私有 reports 数据。

安装版 `127.0.0.1:18765` 的实际回读确认：本周 21 个数字指标全部 VERIFIED，数字源错误 0；6 份官方文本与 1 份官方录播已匹配。资讯监控室实际渲染同样显示 21 项核验状态和官方公布链接。仓位试算及保存条的计算样式为纸色 `rgb(255, 253, 247)` 与深色文字 `rgb(36, 35, 31)`，已消除截图中的灰底和黑条。

按用户此前授权恢复 `gate_live` 会话；仍使用 `aggressive_breakout` 第 5 版、15 分钟节奏及原账户资金配置，没有为验收切换到 PA 或强制制造交易。Bonsai 的真实 GGUF 身份核验通过，窗口 8192 tokens。

13:00 自然轮次 `cycle_20261003T050000063122Z_33152` 在 13:01:45 完成。实际请求含时薪 0.1%、失业率 4.2%、非农 29K、欧元区核心 CPI 2.5% 四条官方事实；统计期、来源和获取时间与核验缓存一致，且可用时刻不晚于冻结时刻。请求使用 `json_schema` 原生 grammar；币种与引用枚举精确对应实际压缩输入。本地严格合同 PASS，原始响应审计 COMPLETE，模型身份与请求哈希绑定通过，实际模型调用约 46.97 秒。6883 个估算输入 tokens 加 1024 输出预留及 256 安全余量，在 8192 窗口内。

该轮结果是模型主动 WAIT，没有系统阻断：TUT 的 15m 候选突破尚未得到 1h 趋势支持和首次回测确认。这证明本轮真实数据、宏观输入和模型合同链路通过，不能据此宣称新成交或策略盈利。账户快照中的外部持仓也不能直接视为本系统已验收保护的仓位。

12:55 的独立 PA 校验曾发现 1h 闭合窗口不连续，明确拒绝 READY；13:00 自然轮次从 Gate 公共 REST 重新获取真实 K 线后，13:01:19 的严格回读确认 BTC 15m 与 1h 各有 239 根连续已收盘真实数据，价格结构 READY，压缩前后结构一致，所有结构可用时刻均不晚于检查时刻。没有手工或合成价格回填。PA 已实装为第五套可选模板，收益验证仍需独立历史重放和前向样本。

本机验收证据保存在忽略的 `reports/price-action-macro-20261003/`：`installed-official-macro-acceptance.json`、`natural-macro-prompt-acceptance.json`、`public-macro-and-pa-acceptance.json`、`final-natural-runtime.log` 和 `installed-macro-ui.jpg`。安装文件校验见 `reports/autonomy-acceptance-20261002/desktop-release-manifest.json`。这些本机报告不上传公开仓库。
