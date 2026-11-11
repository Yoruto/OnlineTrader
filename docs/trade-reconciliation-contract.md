# DSV成交对照与匹配费用登记合同（dev10）

日期：2026-11-10（Asia/Shanghai）。独立开发版本`0.1.0.dev10`。范围承接v0.3的V05/X09和dev9[资金报表](money-reporting-contract.md)：读取外部CSV、逐行对照自有确认成交、生成CSV/JSON/XLSX展示，并经明确审查登记匹配费用。默认内核仍禁开、无交易适配器，本轮没有启动DSV业务程序、连接柜台、安装服务或改变已确认B01–B20规则。

最终完整检查 **810项通过，0失败、0错误、0跳过**，比dev9新增109项，见[结果](../output/checks/20261109T173802Z-b6ef659b/result.json)与[逐项日志](../output/checks/20261109T173802Z-b6ef659b/check.log)。报告SHA-256为`e2ea92f5a726ed627964c28953758216746aa2a6990afb22a2be98533265086d`。编译、导入边界、冻结G0/1.1/1.2资源和检查期间源码稳定性均通过，`pip check`无依赖冲突。

## DSV来源与工程边界

[DSV静态证据](trade-reconciliation-dsv-evidence.md)核实原`成交对照.bat`调用`trading.exe --compare-fills CSV`，根据开仓日志与合约/最近180秒匹配并写XLSX。源工具不核账号、交易日、方向、开平或成交ID，缺量猜1，解码忽略坏字节，且漏掉多余外部成交；没有外部CSV费用归集。本产品保留独立入口及表格对照用途，不把近时猜配当成交归属证明，也不从源行为虚构已存在的费用登记保证。

导入对象是**对照文件与费用证据**。外部文件不创建本地成交、持仓或保护计划，不通过文件填补未知发送或自动授予交易权。没有原自有成交，外部行只能保持`EXTERNAL_ONLY`。

## 模块与所有权

| 模块 | 职责 |
| --- | --- |
| `reconciliation.py` | 有界CSV解析、规范事实、精确匹配及审查预览；无数据库写入。 |
| `statement_workflow.py` | 原文件/本地前置版本/原boot水印绑定，重读重算及受限登记入口。 |
| `reconciliation_export.py` | 固定列展示导出、CSV公式防护、白名单JSON、标准库OOXML三页XLSX。 |
| `desktop/reconciliation.py` | 异步文件/账本选择、scope、双侧结果、明确费用确认及过期禁写。 |
| 原`Ledger`/`MoneyBook` | 原单writer的一次费用事务、持久回执与冷重建；无新表或DDL。 |

预览读取dev9已验证的只读一致性journal。经已有内核连接打开时，绑定账户、原kernel_boot_id与正确Ack接受后的durable_seq，只允许预览/导出。独立离线登记由主机账户锁及文件writer锁排他持有原Ledger，**不启动Kernel、SDK、管道服务或撮合器**。

## 文件与字段合同

支持严格UTF-8（含BOM）或GB18030，逗号CSV或制表符；不使用`errors=ignore`。最多8MiB、20000行、64列、单元格4096字符，越界或不完整CSV明确拒绝。表头按完整别名、英文不区分大小写识别，不做substring首命中；重复指向同一字段的两列拒绝。未知列不进入规范事实、展示或费用动作，原文件只保留SHA-256指纹。

| 规范字段 | 支持的表头示例 |
| --- | --- |
| `account_key` | 账户、账号、资金账号 |
| `environment` | 环境；值为ONLINE_SIM/模拟/simulation或LIVE/实盘/live |
| `trading_day` | 交易日、交易日期；严格YYYY-MM-DD或YYYYMMDD |
| `trade_key` | trade_id、成交编号、成交序号 |
| `contract_id` | symbol、合约、合约代码、代码 |
| `side` | 买卖、买卖方向；BUY/买/买入或SELL/卖/卖出 |
| `effect` | 开平、开平标志；开/OPEN或平/平今/平昨/REDUCE等 |
| `quantity` | volume、手数、成交量；正整数，无默认1手 |
| `price` | 成交价、价格；有界正十进制文本 |
| `fee` | commission、手续费；非负金额，明确0有效，空值未知 |
| `time` | 成交时间、时间；可为明确UTC或合法本地时分秒，仅展示，不借时间推归属 |
| `currency` | 币种、货币；可省略于已绑定CNY账本，显式非CNY/RMB/人民币阻断 |

DSV原时间/合约/量/价四列可解析预览，但缺账户/环境/交易日/成交ID/方向/开平时标不完整，不能登记费用。账号默认与本产品账户档案相同；来源账号不同必须显式给出`source_account`绑定，逐行来源账号仍必须存在并一致。预期scope不补造文件中缺失的scope。合约身份不猜主连、月份或交易所别名；成交时间不充当柜台交易日。

```csv
account_key,environment,trading_day,trade_key,contract_id,side,effect,quantity,price,fee
DEV:example,ONLINE_SIM,2026-11-09,owned-entry,DCE.p2701,BUY,OPEN,3,1055,3
DEV:example,ONLINE_SIM,2026-11-09,owned-exit,DCE.p2701,SELL,REDUCE,3,1034,6
```

该表只是格式示例，只有账本确有上述精确自有成交时才会匹配；文件本身不会生成示例成交。

## 逐行结果与费用资格

匹配先按原`trade_key`找唯一自有成交，再同时核账户、环境、交易日、合约、买卖、开平、实际数量和成交价。价格比较使用Decimal，买卖从原方向和开平推导；不按时间近、价量相似或猜测订单号自动关联。

| 状态 | 处理 |
| --- | --- |
| `MATCHED_FEE_NEW` | 原费用未知、源费用明确，形成确认费用提案。 |
| `MATCHED_ESTIMATED_REPLACEMENT` | 形成原修订+1的确认提案，替代估算、不双扣。 |
| `MATCHED_FEE_UNKNOWN` | 成交一致但源费用为空，不生成费用。 |
| `ALREADY_CONFIRMED` | 已确认费用同金额，不写新费用。 |
| `FEE_CONFLICT` | 与已确认费用不同，阻断；不借文件自动修正已确认金额。 |
| `EXTERNAL_ONLY` / `LOCAL_ONLY` | 双侧缺失分别展示；外部行不成为自有成交，本地缺行费用继续未知。 |
| `DUPLICATE_EXACT` | 同一规范经济行重复可见，费用只提出一次。 |
| `DUPLICATE_CONFLICT` / `AMBIGUOUS` / `CONFLICT` | 身份多义或同身份不同价量/字段，阻断整批。 |
| `INCOMPLETE` / `INVALID_ROW` / `SCOPE_MISMATCH` | 来源不足、坏值或scope不符，阻断整批。 |

仅合法的两侧缺失及完全重复不自动阻断其他精确费用；登记对话框明确列出**仅登记N条精确费用及合计**。任何阻断项使整个文件不能登记，不会挑出其余动作偷偷部分成功。未提供费用修订证明的估算费用不能替换，修订耗尽也拒绝。

`FILL_RECONCILIATION/1`预览含原始文件hash、规范去重事实hash、账户/环境/日/来源账号、账本boot、水印、成交及费用版本hash、所有行、缺失、拟登记动作与总计。`preview_hash`覆盖除自身外全部规范预览。改文件、换scope、变原费用或推进账本均需新预览；文件改名/编码差异不改变相同规范经济事实身份。

## 明确登记与一次事务

费用登记仅针对现有ONLINE_SIM/development账本；LIVE只读对照，不从本维护入口写入。原内核必须已退出，且所有已入场周期CLOSED/实际平0，订单终态累计成交量一致、没有活动或UNKNOWN发送/撤单；全局账户锁阻止同账号在另一路径的运行owner。ExistingOnly模式不能新建账本、schema或缺失的账户scope。

GUI需先成功预览，再显示本次N条、Decimal合计、完整文件SHA和预览hash，由操作者明确确认。源输入改变、预览失败、登记完成或连接内核的只读模式都会禁用旧预览登记。命令行`register --preview-hash`是独立明确登记动作；`preview`、`export`或导出的JSON均不是登记指令。

登记重新读取**原文件**和原账本，在账户/文件writer锁内重算相同预览，核每笔`fill_binding_hash`和`before_fee`，再于BEGIN IMMEDIATE核原checkpoint。一个TX_COUNTER事务同时写入费用、原`fee_observations`的STATEMENT来源、批次回执、已结束的non-trading维护boot和DISARMED权限；不调用普通内核bootstrap，不把已确认订单改UNKNOWN，也不改原fill/分配/周期/订单/计划/保护等表。旧策略权限只撤销，不获新开仓授权。

回执保存来源scope/文件/规范事实hash、原boot/账本前像、费用行号及绑定/前费用证明。返回必须核对tx、boot、event_seq、durable_seq、state_revision、COMMITTED全部身份。写入中途失败则整批回滚；响应无法确认时不得宣称未写，应核对原import_id。重试相同规范来源返回原持久回执而不追加费用/boot；已确认同费或源费用全未知可无变化返回。文件被更改成无效scope/币种时不能借旧回执背书成功。

冷启动`MoneyBook`从同一journal重建批量费用与导入回执，报表仍按实际成交资金/名义点数/账户资金分账。费用后补归回原成交日；登记不变原S/T、停开规则或原始价量。源文件真实性由离线操作者确认，匹配及登记不冒充柜台事实查询或资金对账验收。

## 展示与导出

界面从资金报表“成交对照/导入费用”进入，或独立运行`reconcile desktop`。读取、导出及登记用QThread，失败保留过期记录；关闭窗口不停止内核或发送交易命令。外部行和仅本地行分表显示，最多显示5000行，导出含完整有界结果。

CSV为UTF-8 BOM，固定列包含双侧结果、scope/hash、水印及费用差；未知为空，公式文本转义。JSON为白名单展示副本`FILL_RECONCILIATION_EXPORT/1`，保留`source_preview_hash`而不声称经过脱敏副本本身就是原登记预览。登记不读取此JSON或其中actions。

XLSX用标准库OOXML，无产品openpyxl/pandas依赖；含“成交对照”“本地缺失”“概览”三页、中文表头、冻结首行、筛选和列宽。金额/成交ID用inlineStr保精度，不写公式、宏或外部链接。导出先完整序列化，再独占创建新文件；不覆盖已有结果，格式或容量失败不遗留伪完整文件。导出上限64MiB，格式/关系/XML/单元格内容有离线检查；不以此宣称已完成人工Microsoft Excel验收。

```powershell
E:\online-trader\.venv\Scripts\python.exe -m online_trader reconcile desktop
E:\online-trader\.venv\Scripts\python.exe -m online_trader reconcile preview --data-dir E:\online-trader\output\local-console --account-key DEV:console --input E:\statements\fills.csv --trading-day 2026-11-09
E:\online-trader\.venv\Scripts\python.exe -m online_trader reconcile export --data-dir E:\online-trader\output\local-console --account-key DEV:console --input E:\statements\fills.csv --trading-day 2026-11-09 --output E:\statements\comparison-new.xlsx
```

登记需另行执行`reconcile register`并提供同次预览的`--preview-hash`；本产品不自动选择真实账户文件、改账号、停运行内核或把LIVE转模拟。

## 实际账本与窗口样本

[最终匿名样本](../output/previews/20261109T173644Z-ece936d657-statement-final/manifest.json)先用原生DSV开发周期记录3手1055入场、3手1034退出并完成结案，两笔费用均未知，再关闭内核。真实外部CSV含精确两行费用3/6、一条完全重复及一条仅外部成交；默认窗口使用真实`preview_file/register_fees`，仅QA代替操作者明确确认一次，不替换登记实现。

样本[登记前](../output/previews/20261109T173644Z-ece936d657-statement-final/before-registration.png)显示精确两条/9元、重复和外部多余；[登记后](../output/previews/20261109T173644Z-ece936d657-statement-final/after-registration.png)重新预览为已确认/无费用动作。另一份部分文件只做只读预览，[本地缺失](../output/previews/20261109T173644Z-ece936d657-statement-final/local-only.png)仍列出原入场成交。真实CSV/JSON/XLSX导出均保留，原2笔成交及12类订单/计划/保护等受保护表的查询快照不变；毛收益−630、费用9、净收益−639，权限为DISARMED。

[视觉检查](../output/previews/20261109T173644Z-ece936d657-statement-final/visual-review.json)已核中文、交易日、完整来源hash、未知及金额；截图QA仅隐藏与页首scope重复的两个列，原数据及导出没有删除这些字段。此目录含原文件、部分文件、账本、前后预览、确认记录和登记回执；不包含真实账户或柜台数据。

```powershell
E:\online-trader\.venv\Scripts\python.exe -m online_trader reconcile desktop --data-dir "E:\online-trader\output\previews\20261109T173644Z-ece936d657-statement-final" --account-key DEV:anonymous-offline-statement-preview
```

在窗口选择同目录`statement.csv`、模拟环境和`2026-11-09`重新预览，可见已登记费用且不重复登记；`statement-partial.csv`展示本地缺失。

## 验证与制品

修改前dev9的69份产品文件与701项报告逐字节核对后归档于`output/baselines/20261109T170830Z-83d842c6/dev9-product.zip`，SHA-256为`e44168c4ef0d57cec0dcd8ad14eab6a18ccdbaf24a81ced23756fae367f165ce`。确认基准及冻结G0/1.1/1.2资源不变。

新增109项为CSV/纯对照40项、三格式导出21项、对照桌面19项、真实费用工作流23项及CLI6项。原701项共同纳入上述810项全量。测试覆盖行级坏值、编码与别名、价量scope/身份、重复与歧义、缺费/0/估算替代/已确认冲突、前像与源文件改变、全表回滚、三类错误Ack、冷恢复、活动/未知风险阻挡、LIVE只读、只用既有schema/scope及CSV公式/JSON脱敏/XLSX包关系和精确文本。

交付[dev10离线开发wheel](../dist/20261109T173101Z-029da8818ecd/online_trader-0.1.0.dev10-py3-none-any.whl)，SHA-256为`3fa8e119bb2f5315b6979bafe8d1f9a3425595e7fdea6423a0a0555da750617c`。[构建与干净安装审计](../output/artifacts/20261109T173101Z-029da8818ecd/wheel-audit.json)通过，报告SHA-256为`4e5bc825ffc65cf2b0b65b051de8471582daf28579e28bde742bbb80fd4e645a`。67份Python源码和6份资源在810项最终报告、wheel与全新安装中逐字节一致，源码保持稳定；未覆盖旧制品。

全新core环境以`-I`、本地wheelhouse及`--no-index`安装，不含Qt、QA、测试夹具或交易SDK。原默认禁开/激活拒绝、策略/保护/撤改/调度/名义/资金smoke继续通过。新增原生未知费用样本在关闭内核后精确登记3/6元：单事件、1份维护boot、2笔STATEMENT fee_observations及1份费用回执，净收益−639，原成交/分配未变、DISARMED；同文件、改名及GB18030同义文件重复不追加事件或费用，外部多余行不生成fill。API/CLI三格式导出、不覆盖、旧hash/错scope/LIVE拒绝、展示JSON不可登记等检查均通过。

[GUI最终同源比对](../output/previews/20261109T173644Z-ece936d657-statement-final/final-source-match.json)确认实际窗口样本的73份产品文件与810项最终报告、当前源码一致，预览pyproject与当前字节匹配。以上只证明匿名、明确离线开发范围，不构成柜台连接、真实成交归属查询、生产发布、人工Excel或长期耐久验收。
