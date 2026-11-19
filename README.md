# OnlineTrader

[DSV 行为基准](docs/dsv-behavior-baseline-20261113.md) · [重构计划与阶段记录](docs/refactor/dsv-parity-20261114/README.md) · [当前功能状态及开放问题](docs/功能实现与待办清单_20261110.md) · [验收证据索引](docs/validation/README.md)

## Trading Runtime 独立运行入口

2026-11-19 已将普通模拟执行链封装为可脱离 Qt 启动的 **Trading Runtime**。运行时继续由原 `Kernel`、原策略规则和原每账户 SQLite 账本负责登录、行情、策略、人工命令、开平仓、保护与恢复；**Trading Workbench** 和本机接口进程只读取版本化投影并提交命令。界面卡死或退出不会释放运行时的持仓和保护责任。

便携运行目录使用相对路径配置，不依赖 Windows 凭据库，也不新增 `runtime.sqlite3`。全新目录首次启动自动建立原执行账本；换机交接携带配置和原账本的一致性副本。已经启动过的账户以“运行配置 + 原每账户 SQLite 执行账本”共同构成可恢复状态：配置描述下一次如何运行，账本保存已经发生并需恢复或审计的交易事实。无人值守启动另由 `runtime-control.json` 明确总开关。人工与策略开仓权限分别持久化，人工禁开可否决策略新增，原平仓和保护路径保持可用。公开 CLI/Python 接口支持状态、账户、行情、命令、回执、跨日历史和交接导出。

Trading Workbench 通过 Runtime 操作员接口直接提交命令：令牌只驻留当前界面进程，每次命令由 Runtime 重新鉴权；命令绑定账号、owner boot 和配置版本。超时只查询原 `command_id`，不自动重发。后续用户级产品测试统一从 Runtime CLI/API/Qt 入口执行；旧工作台直连 Kernel/ControlServer 的回放只保留为 Legacy 或组件回归，不能作为当前产品验收。 [测试入口迁移与固定数据](docs/refactor/standalone-trading-runtime-test-migration-20261119.md)

当前离线 Runtime 定向自动化由独立测试 Agent 执行 69 项，69 PASS、0 FAIL、0 SKIP；范围覆盖已实现的操作员协议、版本化视图、Workbench 直接提交、公共 IPC 账户读取/平仓/回执和受影响回归。可见 Qt 强杀恢复、完整故障矩阵、联网柜台和最终制品仍为 `NOT_RUN`，不能据此报告完整产品通过。

[完整设计与操作入口](docs/refactor/standalone-trading-runtime-20261119.md) · [运维、配置与账本约定](docs/runtime-operations-and-storage-20261119.md) · [测试入口迁移](docs/refactor/standalone-trading-runtime-test-migration-20261119.md) · [实现与验收记录](docs/refactor/standalone-trading-runtime-acceptance-20261119.md) · [账号数据与原账本查询](docs/account-data-sync-design-20261119.md)

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

[早期设计与 G0 规格归档](docs/history/early-design/README.md)收录 2026-11-09 的三份设计稿和 OT-G0-1 规格。

`ddad472` 原 README 已**逐字节保留**为 [README.legacy-ddad472.md](README.legacy-ddad472.md)，Git blob 为 `26beae2901169b8da5e27d86cb7e11e9566c79fa`。归档仍在根目录，原相对链接保持原定位；其内容、历史测试数量与版本说明未被重新认定为当前替代链的完成状态。

原96项编号、全部逐项备注、历史实施及验收记录同样保留在[功能清单原文归档](docs/功能实现与待办清单_20261110_归档_ddad472.md)。当前入口不重算那96项的完成率。

## Windows 便携发行目录

便携构建须明确选用 `-DeliveryMode Generic` 或 `-DeliveryMode Configured`。同一次构建生成本地可直接启动的便携目录和分发用 ZIP，并输出 ZIP 的 SHA-256。`-Output` 指定便携目录，ZIP 默认生成在同级目录，可用 `-Archive` 指定其他位置。配置版用 `-DeliveryConfigDirectory` 提供已确认的账户和交易参数文件。

安装版的 GitLab 更新入口、制品名称和发布步骤见[Windows 安装版与 GitLab 更新](docs/windows-gitlab-updates.md)。

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
