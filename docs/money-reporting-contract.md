# DSV资金盈亏与只读报表合同（dev9）

本页保留dev9资金报表范围与原始验证身份。dev10新增外部成交对照、XLSX展示导出及受限离线费用登记，当前合同和结果见[成交对照与费用登记](trade-reconciliation-contract.md)。

日期：2026-11-10（Asia/Shanghai）。当前独立开发版本`0.1.0.dev9`。本轮完成确认自有成交的资金收益、费用归集、账户资金快照及桌面报表，沿用[DSV资金静态证据](money-dsv-evidence.md)的展示维度和来源区分。默认内核仍为DISARMED及UnavailablePort；没有运行DSV业务程序、接入SDK/柜台、安装服务或赋予新的交易权限。

最终完整检查 **701项通过，0失败、0错误、0跳过**，比dev8新增104项，见[结果](../output/checks/20261109T163433Z-2fef9210/result.json)及[逐项日志](../output/checks/20261109T163433Z-2fef9210/check.log)。报告SHA-256为`00f502df704b03a7a0cb73a20b6eb79fe946e212da009b95e9f4443302294097`。编译、导入边界、冻结资源hash与检查期间源码稳定性通过，开发环境`pip check`无依赖冲突。

## 三种统计各自的事实与单位

| 统计 | 事实来源与含义 |
| --- | --- |
| 名义策略点数 | 原理论入场价、原全量退出触发参考价及S/T；按dev8[名义统计合同](statistics-contract.md)归集，不乘手数或乘数。原停开逻辑继续使用这本账。 |
| 本地成交成本收益 | 已确认且分配到自有周期的实际成交价、实际数量和原冻结乘数；以确认顺序FIFO匹配，不从日志名义点数反推金额。币种明确为CNY。 |
| 账户资金与账户当日盈亏 | 独立明确账户快照中的权益、昨结、可用、保证金、平仓/浮动盈亏和费用，保留来源、时间、完整性及修订。不能把本地策略收益补进未知账户权益。 |

DSV发行包报表含日志点数乘数换算、日志模拟余量、按请求手数估费用和缺日持平。新产品保留“按品种、交易明细、每日盈亏”等用途，使用确认事实重建；FIFO为本产品明确的成交成本分配口径，不声称原DSV日志报表已实现FIFO或柜台结算核对。隔夜昨结持仓损益与原开仓成本浮盈亏有不同基准，报表不把它们硬凑成同一金额。

## 模块、所有权和提交边界

| 模块 | 职责 |
| --- | --- |
| `accounting/report.py` | 纯`build_report`，价量成本、费用覆盖、浮盈亏与多维表；Decimal计算，不写数据库或执行交易。 |
| `accounting/book.py` | 从原journal重放周期、成交、费用、账户事实、名义统计及报价来源。不是第二个writer或账户余额账。 |
| `kernel/money.py` | 单一owner上的已接受金融视图；正确CommitAck之后才发布，失败与执行链隔离。 |
| `reporting.py` | 只读SQLite一致性快照、连续journal与hash验证、固定列CSV及白名单JSON导出。 |
| `desktop/reports.py` | 异步读取、六表、日期筛选、来源与未知/估算状态、原生权益图及显式导出。 |

沿用原SQLite单writer和journal，无新增表、DDL、公开注入IPC或服务。`Kernel._submit`冻结本次数据，只有身份/顺序匹配Ack通过后，金融book才接受对应事实。成交附带费用与成交处于同一事务；后续费用修订和账户事实各自追加事件。错Ack、回滚或未接受的准备结果不会发布到owner金融视图。

开发API包括`Kernel.record_fill(..., trading_day=None, commission=None, estimated_commission=None, observed_at_utc=None)`、`kernel.money.record_fee(...)`、`kernel.money.record_account(...)`及只读`kernel.money.report(as_of_utc=None)`。前两类资金来源记录要求显式DEVELOPMENT与隔离端口；默认内核拒绝注入，报表CLI没有资金/成交写入命令。

金融重放错误只使报表明确失败，不改变成交事实或停止保护退出。新成交的附加手续费非法或冲突时保留价量事实、费用未知并给脱敏原因；不保存坏值原文。行情账户财务扩展字段非法时，该字段未知、来源不完整并记录字段名/错误码，既有价量保护仍继续。旧成交、旧费用身份异内容和来源修订冲突仍明确拒绝，不能借补费改变原始成交价量。

## 成本、费用与精度

LONG配对毛收益为`(平仓成交价−开仓成交价)×配对实际手数×原乘数`；SHORT反向。部分入场/退出逐笔消费原周期FIFO成本，未平余量保留；一个周期不借用另一个周期或外部持仓的成本。原乘数冻结于入场，重启或新参数不重估历史。旧开发周期没有金融元数据时，仅可从原冻结`risk_per_lot / 原S`恢复原乘数，不能使用当前boot的新乘数；证据不足则金额未知。

| 项目 | 规则 |
| --- | --- |
| 毛收益 | 从实际成交配对计算；缺乘数、无法配对或身份矛盾不能当0。 |
| 确认费用小计 | 已确认成交费用之和；部分已知仍可显示此小计。 |
| 费用合计与确定净收益 | 每笔成交费用全部已确认才给`fees`与`net_realized_pnl`；净收益为已实现毛收益减全部所选成交费用，含未平入场已发生费用。 |
| 估算费用与估算净收益 | 单列`estimated_fees`及`estimated_net_realized_pnl`，界面标≈；缺任何笔费用时估算净收益也未知。 |
| 明确0 | 只有显式确认费用0才为零费用；None/无字段不表示0。 |
| 后续确认 | 同一trade_key费用用独立证据及递增revision追加；确认替代估算，不双扣。确认不降回估算。历史报表可按原journal水印重建当时的未知/估算值。 |
| 滑点 | 入场显示方向化`sign×(实际入场价−理论价)`；平仓没有原退出报价基准时留空，不把开仓理论价当出口滑点。 |
| 胜率 | 已平且毛收益已知周期的毛收益胜率，标记`GROSS_CLOSED_CYCLES`；不混用DSV名义trade_count或净收益胜率。 |

金额用Decimal precision80计算，输出最多12位小数、ROUND_HALF_EVEN；原成交价和数量仍保留原事实。输出超有界金额域时保留未知及原因，不夹成0。费用当前为明确CNY非负金额，不从未知手续费率猜收费，也不自动导入外部账户累计金额。

Excel 日报将金额、点数和手数写为数值单元格，超出 Excel 15 位有效数字的值保留为文本。用户可以明确输入无记录的柜台日并导出表头。已结案的自有策略周期依据全部自有减仓分笔的量加权成交价及已确认入场价计算真实点数。没有前序半仓或手工减仓时，也用冻结出场参考计算平仓滑点。点数每周期计一次，归入最终减仓的柜台日。人民币毛收益继续按 FIFO 和实际成交手数计算。证据缺失的单元格留空。

## 交易日、估值和账户收益

金融成交日是明确的回调交易日，默认兼容原计划交易日；跨日实际平仓应显式传`trading_day`，同时保留原计划所属交易日。金额日报按成交日归属，后补费用归回原成交日；名义统计仍遵循原周期计划日，不因机器日期改变过去身份。`as_of_utc`是报价估值时点，不是按观察时间截断成交；报表事实范围由`checkpoint_event_seq`限定。历史回放需明确时间与交易日，不用UTC自然日推断柜台日。

未平浮盈亏需要原周期有效报价、原接收身份与明确估值交易日，按实际余量成本计算。报价超过该周期冻结新鲜度期限、无价格、数据质量不完整、时间在未来或live模式boot不匹配时未知；空仓为0。订单/费用事件与计时器重放不续报价寿命。时钟故障提交后原报价估值失效，新合法行情才提供新估值。

账户完整且正昨结可知时，DSV当日盈亏为`balance−pre_balance`，手续费已在权益差中，不再扣一次；昨结明确非正时才用DSV日志兜底`close_profit+float_profit−commission`，要求三者全部已知。昨结未知不猜0。账户收益比例为当日盈亏/正昨结。净出入金明确时另给`equity_change−net_cash_flow`，现金流未知就不把权益增量称为已扣资金变动的交易利润。不完整来源保留已知原字段及INCOMPLETE，派生账户收益保持未知。

当前资金投影要求同boot、完整且新鲜的来源；快速重启不复活旧boot权益或报价。曾发布的新鲜资金到期时，由原owner追加失效事件更新投影修订，不在相同stream修订下偷偷换金额。记录的历史账户行和权益点仍可查看；没有数据的日期不补0或沿用前日权益。

## 只读报表、GUI与导出

`read_report`以`mode=ro`、query_only和单一BEGIN读取一致性journal前缀，核对scope/schema、连续序号、规范JSON及SHA。独立打开以`DATABASE_COMMITTED`明确数据库已提交边界；状态台打开时先读取已有状态快照，再绑定同账户、原kernel_boot_id与已接受durable_seq，标记`OWNER_CHECKPOINT`。不会绕过未接受Ack，也不修改冻结G0 IPC资源。不得用同账户另一个账本副本的巧合序号替代原boot。

六页为按品种、每日盈亏、周期、成交明细、权益记录和名义风控。金额单位、FIFO成本口径、来源不完整、未知与≈各自显示。权益图只画明确来源点，缺失点断线；日期筛选仅改变有交易日的明细和当前CSV，顶部全范围/按品种汇总及完整JSON范围保持明示。表格最多显示5000行，CSV包含筛选后全部行。读取在QThread，失败保留上次记录并标过期；关闭窗口不发交易控制命令。

CSV为UTF-8 BOM、固定列与币种/口径/水印/boot元数据，未知单元格留空，真实负数保持数值而公式文本转义。JSON仅导出递归白名单列，不透传账户源附加秘密字段。导出先完成序列化再独占创建新文件，不覆盖已有文件。读取最多100000事件/128MiB、单事件16MiB；导出64MiB，超限明确失败，不截断成完整结果。

```powershell
E:\online-trader\.venv\Scripts\python.exe -m online_trader reports desktop --data-dir E:\online-trader\output\local-console --account-key DEV:console
E:\online-trader\.venv\Scripts\python.exe -m online_trader reports show --data-dir E:\online-trader\output\local-console --account-key DEV:console
E:\online-trader\.venv\Scripts\python.exe -m online_trader reports export --data-dir E:\online-trader\output\local-console --account-key DEV:console --table by_day --output E:\online-trader\output\daily-new.csv
```

也可只运行`reports desktop`，在报表窗口选择本产品账本与账户标识；没有账本或资金事实时显示未知，不创建模拟资金。当前本地报表没有在线资金查询、外部成交导入、柜台结算差异修正或自动改交易风险的权限。

## 匿名真实账本展示样本

[样本记录](../output/previews/20261109T163014Z-7ef7909bee-real-ledger/manifest.json)由原生DSV开发周期和明确匿名成交构建，记录器只记录2次请求、不撮合。3手1055入场、显式费用3元；报价1035触发STOP，3手实际1034平仓、显式费用6元，实际flat并结案核对。毛收益−630元、费用9元、净收益−639元；名义当日点数−20、净亏16分别保留。账户最终权益99361/昨结100000/出入金0是另行明确给出的开发资金源，并非本地收益自动回填。

先关闭样本内核，再由`ReportsWindow`默认真实reader只读打开账本，六表刷新和CSV导出均通过。截图为[按品种](../output/previews/20261109T163014Z-7ef7909bee-real-ledger/by-contract.png)、[成交明细](../output/previews/20261109T163014Z-7ef7909bee-real-ledger/trades.png)和[权益记录](../output/previews/20261109T163014Z-7ef7909bee-real-ledger/equity.png)，[视觉核对](../output/previews/20261109T163014Z-7ef7909bee-real-ledger/visual-review.json)确认中文、时间、负数、未知和≈可读。69份产品文件的生成时hash与最终源码对照另行记录；这份样本不包含真实账户或柜台成交。

```powershell
E:\online-trader\.venv\Scripts\python.exe -m online_trader reports desktop --data-dir "E:\online-trader\output\previews\20261109T163014Z-7ef7909bee-real-ledger" --account-key DEV:anonymous-offline-money-preview
```

## 验证与制品

修改前dev8的63份产品文件已与597项报告逐字节核对并保留于`output/baselines/20261109T160716Z-9365698a/dev8-product.zip`，SHA-256为`e9ec72e08bc8c9ec46870bab4463d2282977f16d41afb5ae1ae39b85a61f0bad`。DSV确认基线与冻结G0/1.1/1.2资源保持原字节。

新增104项检查为纯资金域33项、资金owner25项、只读报表/导出24项、报表桌面18项、实际CLI4项。覆盖真实价量/FIFO、多空分笔半仓、费用确认/估算/0/未知、修订替代、跨日归属、回滚/错Ack、重启/重复、资金过期修订、旧boot报价、时钟屏障、坏财务扩展隔离、读取一致性/原boot水印、CSV公式注入、脱敏白名单、不覆盖和窗口异步/过期/本地关闭。原597项共同纳入上述701项最终完整检查。

交付dev9离线开发wheel，SHA-256为`4594da94e9fbf638f241dd435f90e38d3370970d90dfcbbc4214b22c6c96e109`。[构建与干净安装审计](../output/artifacts/20261109T162951Z-37e67a76474a/wheel-audit.json)通过，报告SHA-256为`ba92f0ef2feffc5648a55a3438c3bbdffe5ed9ab079ca3782ba4b35309529cc0`。63份Python源码和6份资源在701项报告、wheel与全新安装中逐字节一致；构建期间源码未变，旧wheel与报告未覆盖。

全新core环境以`-I`、本地wheelhouse和`--no-index`安装，无Qt/QA/测试夹具或交易SDK。默认激活拒绝、UnavailablePort和DISARMED继续通过。安装后原生周期分笔1+2手首次入场只计一次，3手退出实际资金毛收益−630、费用1+2+6=9、净收益−639，与名义−20/S16分账；原boot水印只读重建、API/CLI CSV+JSON导出及不覆盖、重启费用证据/原成交幂等、估算替代确认0均通过。手续费或昨结未知的账户字段没有填0；原策略、非默认保护、撤改追单、调度与名义统计smoke继续通过。

真实账本GUI的[最终同源比对](../output/previews/20261109T163014Z-7ef7909bee-real-ledger/final-source-match.json)确认预览69份产品文件与701项最终报告逐项相同，预览pyproject与当前字节相同。Qt中文/布局视觉检查与core无Qt安装是不同验证范围，均有各自证据。以上不构成柜台、资金对账、长稳、性能或生产发布验收。
