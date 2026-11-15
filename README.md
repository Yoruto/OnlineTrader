# OnlineTrader

[DSV 行为基准、源码落点与验收交接](docs/dsv-behavior-baseline-20261113.md) · [F01–F17 修复与验收摘要](docs/dsv-audit-repair-20261114.md)

## DSV 替代链开发与验收

截至 2026-11-15，`refactor/dsv-parity` 的源码基线为 `f6a2bdd`。离线对照直接运行固定版 DSV 策略循环及 TqSdk 3.10.2 目标任务，对比同一批输入下的策略决定、委托回报和 `wait_update` 出站轮次。第 7、9 轮慢部分成交按用户确认保留 OnlineTrader 逐笔核对行为，固定回归要求补足原目标后保持 5 手。[当前状态与证据](docs/功能实现与待办清单_20261110.md#当前替代链状态) · [调度回放与复现命令](docs/refactor/dsv-parity-20261114/15-strategy-sdk-schedule.md)

[逐轮回放与显式执行](docs/refactor/dsv-parity-20261114/06-round-replay-and-controller.md)记录冻结原版的轮次顺序、替代控制器、独立事实库及离线运行入口。

[离线工作台](docs/refactor/dsv-parity-20261114/07-offline-workbench-and-recovery.md)可附着离线执行器做启停、减仓、撤单与跨日恢复核验。「策略事件」查看本地拒发、到期阻断和保护线名义点数，「逐笔成交」查看事实库记录的成交价。[独立 Qt 验收](output/validation/20261115-offline-evidence-independent/test-report.md)

## 历史验收与桌面交付

11 月 13 日完成 DSV 与 OnlineTrader 的定向离线联合验证。两边用同一份旧格式配置载入的五合约字段及归档偏移重算结果一致。国债尾盘、首分钟零比例及缺资金字段的处理仍有差异，原 DSV EXE 的完整启动和两边在线交易待验。[联合验证报告](output/validation/20261113-dsv-onlinetrader-joint/test-report.md)

11 月 13 日源码继续对齐 DSV 的普通入场缺价回退、整手边界、账户选用、权益累计、日报和日志归档筛选。剩余差异及 Qt 单进程诊断均有独立复测证据。本轮保持离线，未打包。[剩余 20 项复测](output/validation/20261113-dsv-remaining-fixes/test-report.md) · [首轮优先修复复测](output/validation/20261113-dsv-priority-fixes/test-report.md) · [原 96 项验收](output/validation/20261113-dsv-item-acceptance/test-report.md)

**0.1.0.dev28 · DSV 参数同步版（2026-11-13）**有独立桌面验收记录。在桌面 `OnlineTrader` 文件夹双击 `OnlineTrader.exe`。原验收从资源管理器双击对应 EXE，并对照工作台和本机 DSV 的 117 项业务参数。11 月 15 日的替代链对照是源码验收，未出制品。[便携 ZIP](dist/OnlineTrader-0.1.0.dev28-Windows-x64-Portable-20261113-DSV-Parameters.zip) · [独立测试报告](output/portable-acceptance/20261113-dsv-parameters/qa/test-report.md)

工程状态见[功能实现与待办清单](docs/功能实现与待办清单_20261110.md)。旧版桌面交付见[历史记录](docs/history/releases/README.md)。`output/` 和 `dist/` 的链接指向本机生成的验收证据和制品。

11 月 13 日 **dev28 棕榈油行情路径修复与托盘版**包含 DSV 参数同步版的改动。源码复测覆盖旧账本恢复、盘段切换和缺失 Tick 回补。[新版便携 ZIP](dist/OnlineTrader-0.1.0.dev28-Windows-x64-Portable-20261113-PalmPath-Tray.zip) · [解压文件校验](output/portable-acceptance/20261113-palm-path-tray/archive-evidence.json) · [源码独立复测](output/validation/20261113-parallel/independent-fixed-test-report.md)

## Windows 便携发行目录

便携发行使用 `release/OnlineTrader.spec` 和 `release/portable/`。工作台与控制台执行器共享 `_internal`，把整个目录一起移动。双击主程序打开工作台。关闭到托盘默认开启，可在系统设置中关闭。托盘图标提示策略状态，双击恢复窗口，右键可退出工作台。

```text
OnlineTrader/
  OnlineTrader.exe
  OnlineTraderCLI.exe
  _internal/
  成交对照.bat
  核对本机时钟.bat
  核对本机时钟.ps1
  网络自检.bat
  网络自检.ps1
  使用说明.txt
  钉钉推送使用说明.txt
  可选_自启动守护/
```

成交对照支持拖入 CSV。时钟和网络工具把报告写到同目录。自启动守护由用户自行启用，任务按当前 Windows 用户和程序目录登记，只负责打开工作台。迁移目录前先运行原目录的停用脚本。[随包使用说明](release/portable/使用说明.txt) · [守护说明](release/portable/可选_自启动守护/说明.txt)

明确要求出便携包时，从已装好项目锁定依赖、PySide6、TqSdk 和 PyInstaller 6.22.3 的 CPython 3.13 x64 构建环境运行 `scripts/build-portable.ps1 -Python <构建环境的 python.exe> -Output <新目录>`。入口核对控制台版本和 SDK 身份后发布目录。开发检查仍走 `scripts/check.ps1`。[便携构建入口](scripts/build-portable.ps1)

## Windows 校时

在管理员 PowerShell 中执行 `& 'E:\online-trader\scripts\sync-clock-once.ps1' -Watch`，每 60 秒检查一次。偏差超过脚本使用的 1 秒提示阈值时，脚本等待 DSV 程序和 OnlineTrader 交易执行器退出，再调用一次 Windows Time 同步，复测并退出。省略 `-Watch` 可在开盘前执行一次检查，时钟对齐时直接返回。加 `-WhatIf` 可查看拟执行的校时操作。测量和同步记录保存在 `output/clock-sync/`。DSV 策略运行时使用 Windows 时间。启动及运行中每小时进行 NTP 诊断，结果可在操作日志查看。Windows 时间跳变后，在「交易详情 → 请求完整核对」核对原持仓和委托。核对完成后，按 F5 恢复策略开仓。

独立 Windows 交易软件的开发候选。当前实现G1内核底座、DSV全业务参数编辑、离线偏移计算、交易模板/品种CSV、正式入场/风控、半仓/二阶段/保本、撤改追单、显式日历与保护调度、名义点数停开闭环、确认成交资金报表，以及外部CSV成交对照与经审查的匹配费用登记。dev13新增独立显式授权的一手快期在线模拟路径；默认运行仍禁开，核心环境没有交易SDK。资金未知就显示未知，业务确认与实际交易权分别管理。

工程和虚拟环境位于 `E:\online-trader`，与 V7/DSV 的代码、环境、凭据、账户数据和发布身份分开。当前开发源码 `0.1.0.dev28`，使用 CPython 3.13.15 x64。精确依赖见 `requirements-core.lock`、`requirements-desktop.lock`、`requirements-build.lock`。G0 来源见[规格来源](docs/spec-source.json)，G1 基线见[G1状态](docs/g1-status.md)。[DSV启动自动计算](docs/auto-offsets-completion-20261110.md) · [SimNow与双侧对比](docs/simnow-comparison-completion-20261110.md) · [账户入口](docs/account-workbench-history-20261110.md) · [策略接入](docs/dsv-online-strategy-20261110.md) · [dev15人工在线记录](docs/product-online-simulation-20261110.md)

## 打开账户管理与交易工作台

```powershell
E:\online-trader\.venv\Scripts\python.exe -m online_trader workbench --account-store "$env:LOCALAPPDATA\OnlineTrader\accounts"
```

主控显示品种列表和策略持仓。账号、资金与连接设置在「账号信息」，参数模板、交易与定时参数在「交易参数」。主控品种表显示待启动的配置，执行器已绑定的参数在「实时监控 → 执行参数」查看。[当前桌面主控](output/portable-acceptance/20261113-dsv-parameters/qa/main.png) · [当前交易参数页](output/portable-acceptance/20261113-dsv-parameters/qa/trade.png)

「账号信息」支持 SimNow 和快期模拟两个提供者。本机默认账户目录 `%LOCALAPPDATA%\OnlineTrader\accounts` 已从用户提供的资料保存两份档案，初次选择 SimNow；后续保留用户手动选择。当前源码和便携版使用同一账户目录。自定义空目录仍可从「账户管理」新增档案。SimNow 同时使用独立交易凭据和快期行情凭据，均只保存在 Windows 当前用户凭据库，账户与凭据不包含在 ZIP 中。切换账号会清空上一账号的资金、行情和曲线显示，原持仓、委托或运行锁仍阻止不安全切换。账户选择不登录。关闭工作台不停止内核或删除保护。

当前工作台默认读取 Windows 桌面 `trading\config.json` 中的交易、定时、偏移计算及品种设置。也可在「策略主控」选择其他 DSV 配置路径并重读。F5 启动前核对源文件，发现业务参数变化会提示核对后重试。找不到配置时，历史基准仅供查看，可选择 DSV 文件或导入已核对的业务参数。主控的「查看执行参数」显示原执行器绑定的品种与偏移。

「保存预览参数」生成的业务 JSON 在参数中心用「导入参数预览」读回，包括原样保存的定时开关和候选合约。旧版原始配置可选「导入原版 DSV 配置」应用 DSV 的加载迁移。「偏移计算」页应用所选结果后可回主控核对并保存预览。

普通工作台点击「启动策略」或 F5，使用当前账号、品种和参数启动。主界面显示进度及失败原因，准备期间阻止重复点击。已连接的策略沿原内核启用。「交易参数」可安排本窗口内的一次定时启动，F6 可取消等待。启动时读取原生交易日历、合约时段与乘数，每次绑定一个柜台交易日。已结案历史保留在原账本并可继续查询，未结案周期和未知回报须沿原范围恢复。F6 停止开仓并按设置撤单，原执行器继续管理自有持仓，柜台仓位和委托核清后结束会话。在线登录及策略成交仍待独立验证。

运行日志按账户和执行器启动标识核对归属。命令行每次运行使用新的输出路径，原账本和历史记录可继续用于核对及恢复。

“历史与交易日历”支持本地日线缓存/导出及带来源日历核对，不更改执行行情或旧计划。10000条原生窗口、跨日屏障和今昨仓核对均已有离线检查；dev20已接多日范围的今昨混合子单、旧单撤改和原周期恢复；更长历史、权威日历及缺失原单查询源仍待补，详见[当前边界](docs/dsv-runtime-completion-20261110.md)。

用户已确认B01–B20全部以DSV为准，见 [DSV业务确认表](docs/b01-b20-recommendation-confirmation.md)及[确认记录](docs/decisions/dsv-confirmation-20261109.json)。DSV保存值和开关原样确认；上一版非DSV改值建议已撤回，未取证算法转开发侧技术核实。现已提供显式离线登记工具，将确认和参数写入NON_TRADING参考版本；保存草稿不切换执行配置。

2026-11-10上午按用户单独授权，通过产品外的快期在线模拟探针完成`DCE.m2701`一手开仓、成交后约60.20秒平仓及独立重新登录核对，最终持仓/活动委托为0。见[上午探针记录](docs/online-simulation-smoke-20261110.md)。下午13:30已通过已安装dev15主程序及原账本完成塑料2701一手买入8339、61.385秒后卖出8341，成交毛盈亏10元；独立重新登录确认本次持仓/活动委托为0，其他账户持仓未操作。见[主程序实际测试结案](docs/product-online-simulation-20261110.md#1330实际测试结案)。dev15仅验证一次受限人工开平。dev17已实现独立DSV执行路径并离线联调，但本轮未连接账户，真实自动策略及在线界面验收尚未覆盖。

## 打开DSV参数中心

dev17新增 `dsv prepare` / `dsv run` 产品入口：前者只检查独立scope、规范profile和显式calendar并输出禁开预览，不登录、不校时、不读取凭据；后者只供将来另行明确授权的TqKq策略范围。旧 `simulation run` 保持原一手人工边界。1.3计划复用原DSV算法和单owner/writer，相关联调全为离线外部事实，详见[dev17接入合同](docs/dsv-online-strategy-20261110.md)。

可以独立打开参数窗口，无需先启动内核：

```powershell
E:\online-trader\.venv\Scripts\python.exe -m online_trader parameters desktop
```

也可从状态台点击“DSV参数”。窗口显示已确认基准、5个品种、原开关和0值；导入后可核对差异，再导出仅包含业务设置的新JSON。账号、口令、通知密钥不会进入参数档案，导出不会覆盖已有文件。窗口以预览为主，不修改持仓计划；版本登记和历史使用配置合同中的命令行入口。

参数窗口现支持品种CSV整批替换/合并预览、导出，以及选择本地目录后保存、应用、覆盖和删除DSV交易参数模板。模板按原软件保存33项交易参数，应用保留当前品种清单及未收录参数。CSV兼容原8列，并扩展候选换月列；错误显示行号，整批拒绝，保留上一份预览。模板操作追加本地历史，覆盖和删除需要明确选择；预览和文件保存均不会切换运行配置。操作示例及DSV兼容差异见[模板与CSV合同](docs/parameter-workspace-contract.md)。

“编辑全部业务参数”覆盖69项交易/风控、4项计算设置、4项定时设置和每个品种的9列，可筛选、增删品种、对选中行批量设置。校验通过后才更新参数中心预览，取消编辑保持原预览。“离线偏移计算器”支持DSV的6种公式、品种系数、振幅上限/下限、当前值对比、缓存重算和结果复制/导出；输入明确交易日和本地日线，选择成功结果后才填入预览。用完整业务JSON保存全部修改；DSV模板仍只保存原33项交易参数。格式和口径见[编辑与计算合同](docs/parameter-editing-offset-contract.md)。

主控「添加品种」使用独立单品种窗口，输入带月份的合约代码可补全名称，确认和取消固定在底部。选中编辑或双击品种均打开相同表单；高级参数编辑器的新增、编辑也复用该表单。确认后更新当前列表，取消不产生空行或部分修改；「保存预览参数」另存为新的业务 JSON。窗口按可用屏幕大小定位，内容超出时只滚动表单，操作按钮保持可见。

## 运行最小状态台

先在一个终端启动内核，参数使用本产品独立的账户档案标识，不能指向其他产品数据目录：

```powershell
E:\online-trader\.venv\Scripts\python.exe -m online_trader serve --data-dir E:\online-trader\output\local-console --account-key DEV:console --pipe OnlineTrader.G1 --shadow-worker
```

再在另一个终端打开窗口：

```powershell
E:\online-trader\.venv\Scripts\python.exe -m online_trader desktop --pipe OnlineTrader.G1
```

窗口可刷新内核/持仓保护状态、暂停新增权限；关闭窗口只关闭状态台。命令行也可使用 `status --pipe OnlineTrader.G1` 或 `pause --pipe OnlineTrader.G1`。当前默认内核保持 DISARMED，提示未接交易适配器，不能通过该界面执行开仓或平仓。没有安装 Windows 服务，也不修改开机启动项。

只读策略进程由内核登记其实际进程身份，只能读取快照；终止它不会删除内核保护。DSV信号与风控由原owner处理，支持显式开发实例及dev17独立有限模拟范围；该只读进程仍是G1骨架，没有在线提案、签名更新或跨进程追平权限。

DSV策略已接入多空触发、几何/时段、共享K线机会、原追价豁免、固定/自动/激进数量、保证金、日/月停及最终发送复核；实际规则依据见[静态证据](docs/strategy-dsv-evidence.md)。默认启动仍禁开，参数窗口不启动交易；当前只用独立开发端口和明确事件验证，没有公开行情注入或柜台入口。

DSV的1.1保护原语支持按当前虚拟剩余手数ties-even半仓、每条新行情最多一档、二阶段和真实减仓后的保本；普通移动止盈使用DSV的max回撤公式，并落实保护/移动/断流各自日停开关。原1.0计划保持原数量与门禁语义。非默认设置通过明确的隔离开发profile测试，不改变已确认保存基线或默认启用状态。

正式开发运行器现显式生成1.2计划，在1.1保护基础上加入首笔CAP、被动等待、只在价格恶化时撤单、终态/成交一致后按剩量重报，以及收盘剩余10/5秒的升级计价。替代单沿原intent建立新child/ordinal，资金/权限/epoch/行情复核与UNKNOWN占量继续有效。原1.1编译调用及已有计划仍可读取；手工直单计价不自动进入策略追单。

`ScheduledRuntime`在同一owner的`Kernel.poll()`中驱动明确日历和单调时钟；即使没有新报价，也能推进入场/撤单等待、收盘目标和活动盘次断流检查。日历必须明确交易日、覆盖和最终收盘端点，午休不会冒充日终；原日历绑定/退出期限不被新盘报价改写。计时器保留原报价身份与接收时间，不制造QUOTE、重复半仓或延长旧价寿命。时钟故障先撤权限并跨重启锁存，核对/重置不自动武装。支持显式开发实例及dev17独立有限DSV模拟范围，默认serve不启用调度或交易。

新建原生DSV周期从首个真实成交登记入场统计，全部退出且订单/成交一致核对后，将结案与退出统计写入同一事务。名义点数按DSV的S/T及原退出参考价计入，不按手数加权；请求手数与实际成交量分别保留。日/月停、延续开连损和同向止损冷却进入提案、替代单及最终发送复核；重启和换profile不会清零历史统计。手动退出修复源程序的反号与金额/点数混用，月停保持至换月。旧未登记名义周期不自动补造统计；迟到成交保留事实并要求修正核对。

资金报表从已确认自有成交重建FIFO成本、毛收益、费用、净收益和可证浮盈亏；分笔、半仓、跨日与原乘数单独处理。已确认费用、估算费用和缺费分列，明确0才是零费用；账户权益/可用/保证金与账户DSV当日盈亏来自独立来源事实，不能由名义点数或本地成交收益补造。报表窗口提供按品种、每日、周期、成交明细、权益记录和名义风控六页、权益图、日期筛选及CSV/JSON导出。

从状态台点击“盈亏报表”，选择本产品的`ledger.db`。经状态台打开时绑定同账户、原内核boot和已接受水印；也可独立打开只读历史账本：

```powershell
E:\online-trader\.venv\Scripts\python.exe -m online_trader reports desktop --data-dir E:\online-trader\output\local-console --account-key DEV:console
```

`reports show`输出JSON，`reports export --output 新文件.csv`导出逐笔CSV；可用`--table by_day`等选择表，JSON导出完整报告。离线账本与当前柜台状态分开标识；关闭窗口不影响持仓保护，读取或导出不取得交易权。例子及精确口径见[报表合同](docs/money-reporting-contract.md)。

在资金报表窗口点击“成交对照/导入费用”，或独立运行：

```powershell
E:\online-trader\.venv\Scripts\python.exe -m online_trader reconcile desktop
```

选择本产品账本、外部CSV及明确账户/环境/交易日，先查看精确匹配、两侧缺失、重复和冲突；可导出CSV、JSON或XLSX。DSV原四列文件可检查，但缺账户/交易日/成交身份时不能登记费用。只有精确绑定自有成交的未知/估算费用可明确确认登记；登记要求原内核已退出、账本无持仓或活动/未知订单，整批原子提交并保持禁开。文件、费用版本或水印变化需重新预览，不修改原成交价量或保护计划；重放不重复扣费。真实操作范围及来源修复见[对照合同](docs/trade-reconciliation-contract.md)。

内核控制端已处理全部13种G0命令，包含持久幂等回执、接管风险清单和维护事实屏障；规则或事实不足时明确拒绝或等待核对。开发政策和成交事件仅由独立QA登记，不通过IPC开放。接口见[控制契约](docs/control-contract.md)。长期只读客户端 `transport.subscription.LiveSubscription` 支持 Snapshot/Delta/Gap、心跳和断线保留重放，见[状态流契约](docs/stream-contract.md)；最小窗口仍提供快照刷新和暂停入口。

## 独立检查

```powershell
Set-Location E:\online-trader
.\scripts\check.ps1
```

完整检查覆盖既有内核/协议/保护/参数/实际进程，以及DSV信号、风险、停开、计划编译、持久准入、非默认保护、调度、名义统计、资金报表、桌面展示、外部成交对照/费用登记、模板/品种CSV、全业务参数编辑和离线偏移计算。dev19补原生日期/日切事实消费、逐周期目标与桶预占/最终发送复核；dev18账户/工作台、dev17原生策略链和dev16状态查询回归保持。dev19的1206项及制品作为[历史证据](docs/crossday-native-close-20261110.md)保留；dev20只做必要定向检查，未全量回归或构建/安装，见[当前记录](docs/dsv-runtime-completion-20261110.md)；dev3参数证据见[配置合同](docs/configuration-contract.md)，原G1故障证据见[故障矩阵](docs/g1-fault-matrix.md)。QA和开发事件不进入产品 wheel；`qa/recording_port.py`只记录请求，不撮合成交，测试成交由独立事件明确注入。

这批检查不代替在线模拟开平、Windows跨服务身份/跨会话、断电、长期耐久或性能验收。B01–B20的DSV业务基准已确认；DSV未取证执行语义、目标通道和新产品在线操作范围仍需各自落实。V7 rc.41 与原云端CI/CD状态保持不变。
