# OnlineTrader

[文档导航](docs/README.md) · [DSV 行为基准](docs/dsv-behavior-baseline-20261113.md) · [重构计划与阶段记录](docs/refactor/dsv-parity-20261114/README.md) · [当前功能状态及开放问题](docs/功能实现与待办清单_20261110.md) · [验收证据索引](docs/validation/README.md)

## Trading Runtime 独立运行入口

2026-11-21 的当前设计将普通模拟执行链封装为可脱离 Qt 启动的 **Trading Runtime**。同一台 Windows 电脑最多运行一个 Runtime；不同电脑之间不互斥，同一期货账户可按柜台规则同时登录和交易。Standalone 默认核心已冷切换为 DSV Strategy + `ExplicitOrderController` 的单一 Target Owner，使用本次启动的临时 Journal，并从期货账户同步资金、持仓、委托和成交；Legacy Kernel 仅保留显式维护路径。**Trading Workbench** 与本机接口进程只读取投影并提交命令。

后续未特别指定路径的启动、重启、验收候选和打包统一以 Standalone Trading Runtime 为默认目标。普通用户主动运行 `online-trader-desktop --runtime-root <目录>`；Runtime 配置便携版直接双击 `OnlineTrader.exe`。统一入口先附着已有 Runtime，缺失时以 `DISARMED` 启动 Owner，取得权威投影后再打开与 dev 完全相同的交易工作台。`runtime run` 与 `--runtime-pipe` 仅保留为运维及高级附着入口；CLI 不提供交易命令或无人值守调度。缺少 Runtime 配置时默认入口明确失败，不回落 Legacy。Legacy 只用于明确点名并显式指定入口的旧路径维护；默认路径选择本身不授权连接账号、执行交易、运行测试或生成制品。

便携运行目录使用相对路径配置，不依赖 Windows 凭据库。每次 Runtime 启动创建进程所有的临时执行库，结束后删除，不进行换机交接，也不把旧 `ledger.db` 当成恢复权威。长期保留的是版本默认配置、用户覆盖配置，以及可从期货账户重新下载的交易历史与账户收益缓存。旧 `runtime-control.json`、`private/operators.json` 和 handoff 文件被忽略。公开 CLI/Python 接口只保留状态、账户、行情、历史及诊断读取；交易控制只走同一 Windows 用户的 Workbench 通道。

Trading Workbench 使用只接受启动用户 SID 的本机控制通道，不显示或读取操作员 ID、令牌，也不接受 `LocalSystem` 作为交互用户。命令继续绑定账号、owner boot 和配置版本，14 项封闭操作白名单、Capability 与交易风险由 Runtime 逐次裁决；命令审计保留脱敏账号、actor、boot 及 peer PID/SID。超时只查询原 `command_id`，不自动重发。当前源码候选已把加固后的 CommandBus 接入生产 Workbench，旧直连命令线程、委托操作员管道和旧 Qt 回执兼容均已退役。后续用户级产品测试统一从 Runtime CLI只读/API只读/Qt入口执行；旧 Kernel/ControlServer rehearsal 不可据此宣称当前 Target Owner 的进程级验收。[Target Owner 冷切换合同](docs/refactor/target-execution-and-standalone-cutover-20261121.md) · [测试体系重构](docs/refactor/testing-system-redesign-20261121.md) · [测试入口迁移与固定数据](docs/refactor/standalone-trading-runtime-test-migration-20261119.md)

2026-11-21 的 Target Owner 冷切换记录了离线受影响范围 `37/37`、定向范围 `138/138` 和完整源码回归 `1919/1919`；2026-11-22 至 2026-11-23 陆续完成 Target Owner 边界加固（混合夜盘/日盘交易日推进与日历完整性保护、授权代际绑定防人工 `STOP` 后自动 ARM 复活、三档 Owner 异常分类与 `ControlServer` fail-closed、单平/全平 `target_revision` 乐观并发闭环、成交通知身份去重与合并平仓历史优先路由），并在本机 Windows `qa/test_runner.py --profile FullOffline`（含 `OfflineTargetOwnerProcess` 子进程生命周期测试）取得 `1931/1931 PASS`；这些是该候选的离线证据，不代表真实柜台、多台物理机同账号、自然订单闭环或最终制品已验收。更早 Standalone/Workbench 候选的全量复测结果 `PASS 22 / FAIL 4 / BLOCKED 6 / NOT_RUN 6` 仍作为历史保留，不能移植到新候选或与新 Owner 的离线结果混算。[Target Owner 冷切换合同](docs/refactor/target-execution-and-standalone-cutover-20261121.md) · [旧候选复测缺陷修正记录](docs/refactor/runtime-full-retest-remediation-20261120.md)

同日 P0 源码候选已经拆分策略信号、逻辑开仓 Intent、执行定价和 SDK 发送责任：正常行情更新不再要求旧候选报单价完全相等，明确未发送才允许沿原 Intent 重新定价，`UNKNOWN` 继续冻结数量；退出目标在报价变化、柜台拒单和路由变化后保留持续推进责任。该候选仅完成编译检查，自动交易仍被独立故障注入与真实 SimNow 自然开平仓闭环阻断。[P0 行情竞态与退出可靠性修正](docs/refactor/runtime-market-race-entry-exit-remediation-20261120.md)

较早 Runtime 候选的离线定向自动化由独立测试 Agent 执行 69 项，69 PASS、0 FAIL、0 SKIP；范围覆盖当时的操作员协议、版本化视图、Workbench 直接提交、公共 IPC 账户读取/平仓/回执和受影响回归。该结果不适用于本次修正候选，不能据此报告完整产品通过。

[当前 Workbench 合同总览](docs/refactor/runtime-workbench-current-20261120.md) · [Target Owner 冷切换合同](docs/refactor/target-execution-and-standalone-cutover-20261121.md) · [完整设计与操作入口](docs/refactor/standalone-trading-runtime-20261119.md) · [当前权威与单实例设计](docs/refactor/runtime-broker-authority-single-instance-20261120.md) · [复测缺陷修正记录](docs/refactor/runtime-full-retest-remediation-20261120.md) · [运维、配置与账本约定](docs/runtime-operations-and-storage-20261119.md) · [测试体系重构](docs/refactor/testing-system-redesign-20261121.md) · [测试入口迁移](docs/refactor/standalone-trading-runtime-test-migration-20261119.md) · [实现与验收记录](docs/refactor/standalone-trading-runtime-acceptance-20261119.md)

该入口当前只支持已验收的模拟账户范围，不表示 DSV replacement 链已经切换，也不表示真实资金账户或发行制品已验收。当前源码验收状态见[功能清单](docs/功能实现与待办清单_20261110.md)。

## DSV 替代链开发与验收

2026-11-18 的 dev32 Windows 便携版通过普通交易工作台和 SimNow 测试了棕榈油、塑料、沥青和菜油。前三笔各 1 手的试单完成开平。取消人为手数限制后，棕榈油自动算量并卖开成交 30 手。午休按用户要求暂停，原执行器继续管理这笔持仓。 [制品、四合约结果和重连验收](docs/refactor/dsv-parity-20261114/30-dev32-simnow-and-workbench-reconnect.md)

2026-11-16 的[上午五合约迁移与模拟盘对照](docs/refactor/dsv-parity-20261114/23-simnow-migration-and-activation-parity.md)覆盖原账本副本恢复、同柜台日迁移和激活位修复。[下午四合约真实模拟盘复跑](docs/refactor/dsv-parity-20261114/24-afternoon-four-contract-acceptance.md)完成了几何拒绝、激活位与柜台空仓对照，记录了停开后的 SQLite 提交错误及读库修正。实际发单、成交保护和平仓仍待验证。

2026-11-17 校时后的两轮四合约双跑共匹配 180 个同秒行情点和 20 条同分钟激活位拒开日志，塑料和沥青分钟缺口未复现。现有模拟柜台证据覆盖入场前判定，仍需有自然信号的开仓、成交后保护和退出闭环。 [现场复测记录](docs/refactor/dsv-parity-20261114/29-clock-stability-and-four-contract-retest.md)

截至本次文档核对，源码基线为 `ddad472235b9fa0749646d7e3fded831040ca6c3`；该提交仅更新文档，运行代码沿用 `6468b003394315ee2fd1fa919ea6b1117db4cac9`。**A01、A02、A03、B01 所列场景的记录，不代表整个替代链、全部旧账本格式、真实柜台或正式发布已经验收。**

`6468b00` 实现明确未发送减仓的同目标恢复、后台只读历史投影、策略决定与撤单异常展示，以及有序目标/轮次比较。第16篇记载作者的 Windows/Qt/IPC 复测及源码自检数量；原始报告存于本机 `output/`，本次远端文档核对未取得原件，不能把引用数字当作新增独立验证。[适用场景与复测方法](docs/refactor/dsv-parity-20261114/16-exit-recovery-and-evidence-acceptance.md)

此前调度对照在冻结 DSV 策略循环与 TqSdk 3.10.2 目标任务之间注入同一批本地输入，比较策略目标、API 调用和 `wait_update` 出站轮次。慢部分成交仅第7、9轮采用已登记的差异；其他差异不因此获得批准。[调度回放](docs/refactor/dsv-parity-20261114/15-strategy-sdk-schedule.md) · [批准范围](docs/refactor/dsv-parity-20261114/00-evidence-and-decisions.md#用户批准的行为差异)

### 当前必须保留的边界

| 项目 | 状态与依据 |
| --- | --- |
| C01 旧精简 UNKNOWN | 在 `ddad472` 基线仍为 OPEN：只含 `id/result` 的事件会阻断历史视图。修复见 [PR #2](https://github.com/Yoruto/OnlineTrader/pull/2)，候选 `018ce03` 的定向测试为15通过、2项 Windows Qt跳过。合并与 Windows 复测状态需另核，不能因本文更新而关闭。 |
| F04 月停跨日加强 | 仍需冻结原版语义或对应批准记录；慢部分成交批准不能替代此项。 |
| Qt/IPC、柜台、制品、切换 | 分别验收。离线代码、客户端替身或本地出站包不证明真实柜台交易或已交付制品通过。 |
| 交易事实 | 展示兼容不改 UNKNOWN、订单占量、成交价量、账户权限；禁止清库绕过迁移。 |

## 执行入口与开发指引

旧 `dsv run` / `simnow run` 与新 `replacement-run` 尚属不同路径。替代链使用 `DsvSession`、显式订单控制器、独立 Journal 和 SDK 适配层；共享账户排他锁不等于共享两个发送控制者。[候选启动与回滚](docs/refactor/dsv-parity-20261114/10-sdk-startup-and-same-day-rollback.md) · [AGENTS](AGENTS.md) · [skill 规范源](docs/skills/online-trader.md)

连接、报撤单、服务安装、发布和切换均按当前明确授权。旧成功登录、历史有限额度或本文的复测命令不是新的交易授权。

## 检查与证据

在完整 Windows 项目环境运行原有定向命令；这些是复现入口，不是本次文档 PR 已执行声明：

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_dsv_schedule_comparator tests.test_dsv_refactor_repairs tests.test_dsv_round_replay -v
```

历史本机报告仍由原持有人保管。可提交脱敏的候选 SHA、fixture/脚本哈希、实际命令、环境、PASS/FAIL/SKIP/NOT_RUN 和报告哈希；不要提交凭据、真实账号或未经授权的原始账本。当前可独立取得和仅作者记录的证据已分列在[机器可读索引](docs/validation/evidence-index-20261115.json)。

## 历史验收与桌面交付

[早期设计与 G0 规格归档](docs/history/early-design/README.md)收录 2026-11-09 的三份设计稿和 OT-G0-1 规格；[旧 G1 与修复前审计](docs/history/legacy-runtime/README.md)保留已被当前入口取代的早期实现记录。

`ddad472` 原 README 已**逐字节保留**为 [README.legacy-ddad472.md](README.legacy-ddad472.md)，Git blob 为 `26beae2901169b8da5e27d86cb7e11e9566c79fa`。归档仍在根目录，原相对链接保持原定位；其内容、历史测试数量与版本说明未被重新认定为当前替代链的完成状态。

原96项编号、全部逐项备注、历史实施及验收记录同样保留在[功能清单原文归档](docs/功能实现与待办清单_20261110_归档_ddad472.md)。当前入口不重算那96项的完成率。

## Windows 便携发行目录

默认交付为 Runtime 完整便携目录/ZIP，而不是只含普通工作台的 Legacy 包。配置版除 EXE、共享 `_internal` 和必需脚本外，还须按接收方带齐 `runtime.json`、`OnlineTrader-parameters.json` 和私有账户默认配置；不交付操作员授权、自动启动控制或执行账本。最终制品从双击 `OnlineTrader.exe` 的统一入口走到 Runtime Owner 与工作台，并另核对高级 `--runtime-pipe` 入口；未明确要求打包时不执行以下构建。

正式便携构建只接受 `-DeliveryMode RuntimeConfigured`，并用 `-DeliveryConfigDirectory` 提供接收方已确认的完整 Runtime 配置。构建前执行快速门禁，正式发行使用干净 PyInstaller 工作目录，随后生成完整目录和 `*-Runtime-Configured.zip`。需要验证真实 EXE、但尚未获准正式出包时，使用 `scripts/build-test-portable.ps1` 生成带配置且显式标记为 `TEST-ONLY` 的目录；它复用受控 PyInstaller 缓存且不生成 ZIP。实现与边界见[测试与便携交付流水线](docs/refactor/testing-and-portable-delivery-pipeline-optimization-20261120.md)。

安装版的 GitLab 更新入口、制品名称和发布步骤见[Windows 安装版与 GitLab 更新](docs/windows-gitlab-updates.md)。

当前源码候选已把受管理更新接入默认 Runtime 工作台：「账户与设置 → 软件更新」显示当前版本、渠道和待安装版本，工作台启动后在后台检查并下载。下载不改变 Runtime 权限，也不停止策略；安装前由稳定启动器取得安装锁、Runtime 单实例锁和已配置账号的本机进程锁，确认相关进程退出后切换版本。更新器不扫描、不解释旧执行账本。源码仅完成实现与编译检查时，不能据此声称实际更新包、回退或配置保留已经验收。[Runtime 更新合同](docs/windows-gitlab-updates.md#standalone-trading-runtime-更新接入)

既有 dev28 发行、目录布局、构建命令及托盘验收见[原发行说明](README.legacy-ddad472.md#windows-便携发行目录)。这些历史制品不能替代新替代链的制品验收；本次 PR 不构建或发布安装包。

## Windows 校时

在线执行器与桌面定时启动使用应用内 UTC。有效的外部 NTP 样本以单调时钟推进，刷新失败、样本过期或偏差超限时暂停新开仓。同一次自动策略运行仅因授时暂停时，授时连续稳定至少两秒且原授权、柜台日、订单持仓和行情都已核对后，自动恢复原开仓权限。人工停止、进程重启、换日及需要人工核对的时钟故障保持停开。

授时暂不可用时，同一次系统启动内可以用原账户、原账本保存的 UTC 锚点恢复既有持仓责任。锚点有效期最多为最近一次可信授时后的 15 分钟。超期后继续接收柜台回报和核对原责任，依赖有效 UTC 的新委托保持阻断。电脑重启或恢复凭据无法核对时，需要重新授时。

[时钟与计时器开发规格](docs/clock-and-timer-development-plan-20261117.md) · [实施说明与检查入口](docs/clock-timer-implementation-20261117.md)

原脚本和使用说明见[历史校时说明](README.legacy-ddad472.md#windows-校时)。其中原内核的跳钟核对/F5流程仅适用于所记旧路径，不应套成替代入口的额外门禁。自动校时需求、运行中系统时间修改及新链路的完整验证仍单列，不由这次文档修订授予执行权限。

即时校准前检查 W32Time 是否仍在渐进调速。已授权停机维护时，先停止服务并确认调速状态，再即时校准、恢复服务并重新同步。随后以独立样本跨自动同步周期复核偏差。 [实际操作与验证](docs/refactor/dsv-parity-20261114/29-clock-stability-and-four-contract-retest.md)

## 打开账户管理与交易工作台

运行中失联时，包含重连修复的工作台会区分执行器未响应与柜台待同步。窗口底部显示已等待时间和 30 秒重试倒计时，可点「立即重连」直接向原执行器查询状态。收到完整同步状态后自动收起提示。 [重连行为与验收](docs/refactor/dsv-parity-20261114/30-dev32-simnow-and-workbench-reconnect.md#工作台重连)

旧入口、参数编辑、报表、默认启动及维护命令完整保留在[原工作台说明](README.legacy-ddad472.md#打开账户管理与交易工作台)。使用前核对入口、所部署版本及当前授权，不能把旧制品操作记录当作替代链已切换的证明。

## 打开DSV参数中心

[原参数中心、模板与偏移计算说明](README.legacy-ddad472.md#打开dsv参数中心)保留在原文归档。预览、保存与运行中应用不是同一动作；不得由计算结果自动取得交易权限。

## 运行最小状态台

[原G1状态台与旧Kernel入口](README.legacy-ddad472.md#运行最小状态台)仅按其既有范围使用，不作为新替代控制器的入口。

## 独立检查

[原完整检查与历史范围](README.legacy-ddad472.md#独立检查)保留不变；当前验收需绑定候选完整SHA、实际入口及[证据状态](docs/validation/README.md)，源码、Qt/IPC、柜台与制品分别记录。
