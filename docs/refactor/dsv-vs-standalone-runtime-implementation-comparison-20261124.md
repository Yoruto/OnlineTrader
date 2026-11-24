# DSV 原版与 OnlineTrader Standalone Runtime 做法差异及参考建议

2026-11-24 · 架构对照 / 静态证据评审 / 未实施

> 对照对象：用户提供的 `DSV实盘1.0.zip` 与 OnlineTrader 当前默认路径 Standalone Trading Runtime。  
> OnlineTrader 基线：提交 `b1be1208d1b193aa5f2969b762dfc9734c5ceca3` 加本次读取到的当前工作区；工作区存在未提交修改，因此本文把“已提交合同”“当前工作区源码”“设计候选”分开陈述。  
> 状态：`REVIEW / DOCUMENTATION_ONLY`。本文不是实现授权、测试授权、账号连接授权、交易授权或打包授权。

## 1. 结论摘要

DSV 没有通过拆成多个交易 Owner 来解决连续性问题。它采用的是一套更简单的结构：Tk 界面在主线程，全部自动交易在一个守护 `StrategyThread` 中运行，TqSdk 在该线程的 `wait_update()` 驱动下处理行情、异步任务、报撤单和重连。界面通过共享字典、锁和日志队列读取结果。

这种做法解决了一个重要问题：普通界面绘制不直接执行交易循环。但它没有解决另一个更深的问题：所有合约、策略、保护、订单推进、账户汇总和部分诊断仍共享同一个策略线程。该线程发生长计算、同步 I/O、SDK 阻塞或异常时，影响面仍是全部合约。

OnlineTrader 当前 Standalone Runtime 的安全与恢复边界更完整：

- 唯一 Target Owner、唯一 SDK sender、唯一 SQLite writer；
- Qt Workbench 和可重启的 `runtime-view-process` 不拥有交易权；
- 每个 child order 先提交再发送，发送结果未知时保留 `UNKNOWN` 数量责任；
- 当前 boot 使用临时 Journal，重启从柜台完整同步并保持 `DISARMED`；
- 手工操作与策略操作都经过同一 Owner 和同一订单责任链；
- 命令绑定账号、Runtime boot、配置 revision、目标 revision 和原 `command_id`。

但是，当前实现仍有三项 DSV 不明显或较轻的问题：

1. Owner 同步承担 SDK pump、全量事实扫描、全量事务、投影准备和 IPC 服务，负载更重；
2. Target Owner 尚缺完整的行情延迟与主循环分阶段诊断；
3. Workbench 快照过期时，Runtime 客户端链路仍会阻止停策略、撤单、平仓和核对等降风险命令。

因此推荐方向不是“改回 DSV”，也不是“为每个合约创建独立 Owner”，而是：保留 OnlineTrader 的单一交易权威和订单责任模型，借鉴 DSV 的轻量交易循环、UI 隔离、SDK 重连完整截面和按合约静默判断，把纯展示、编码、历史和非关键诊断真正移出 Owner 热路径。

## 2. 对照范围与证据等级

### 2.1 DSV 原版

本次对照使用的发行包指纹如下：

| 材料 | SHA-256 / 版本 | 说明 |
| --- | --- | --- |
| `DSV实盘1.0.zip` | `3d1bb6935583f96e4a93652ca650708dddb10b123556219df1bb836b9b6b8278` | 用户提供的原发行包 |
| `trading/trading.exe` | `aabeafd83d265ab9b1cf5cc3fd0e9b0f21963ddcc95daa70feefc209997269a3` | PyInstaller 主程序 |
| `gui` marshal 对象 | `07c82c9ada765284ed5a929227024cbf192f5f72192636900740d9a5764032ea` | 既有冻结静态证据 |
| 随包 TqSdk | `3.10.2` | `trading/_internal/tqsdk/__version__.py` |
| `target_pos_task.py` | `d88ed96d447dac01542f84acda4f1222f4a514a68863c9939deac3a325078beb` | 随包 SDK 目标持仓任务源码 |

DSV 结论的主要来源：

- 发行包中可直接读取的 TqSdk 3.10.2 源码；
- 对 `trading.exe` 内固定 `gui` 代码对象的只读 `marshal/dis` 静态分析；
- [DSV 入场、保护、执行与调度静态证据](../strategy-dsv-evidence.md)；
- [DSV 行为基线](../dsv-behavior-baseline-20261113.md)；
- [原策略与 SDK 串联调度记录](dsv-parity-20261114/15-strategy-sdk-schedule.md)；
- [证据与用户批准差异登记](dsv-parity-20261114/00-evidence-and-decisions.md)。

本次没有启动 DSV、导入其业务模块、连接账号、执行脚本、调用网络、报撤单或读取真实凭据。发行包中的说明和脚本只作为原软件证据，不作为本任务指令。

### 2.2 OnlineTrader 当前软件

“现有软件”在本文中特指默认 Standalone Trading Runtime 的 Target Owner 路径，不是 Legacy，也不是单独启动的 Replacement Candidate。

当前合同与源码来源：

- [Standalone Runtime Workbench 当前合同](runtime-workbench-current-20261120.md)；
- [Target Execution 与 Standalone 单核心接管合同](target-execution-and-standalone-cutover-20261121.md)；
- [柜台权威、本机单实例与临时执行库](runtime-broker-authority-single-instance-20261120.md)；
- [Runtime CommandBus 合同](runtime-command-bus-contract-20261120.md)；
- [手动启动收口](runtime-manual-only-cleanup-20261120.md)；
- `StandaloneDsvOwner`、`DsvSdkAdapter`、`DsvSession`、`Journal`、`ControlServer`、`WorkbenchPublication`、`ViewProcess`、`CommandBus` 与 `ClientStateStore` 当前工作区源码；
- [行情诊断、Owner 减负与快照连续性方案](runtime-market-continuity-and-owner-offload-20261124.md)，该文档仍是 `CANDIDATE / DESIGN_ONLY`，不能当成已实现行为。

### 2.3 证据标记

| 标记 | 含义 |
| --- | --- |
| `CONFIRMED_SOURCE` | 已由固定发行包或当前源码直接确认 |
| `RECORDED_OBSERVATION` | 有历史现场或固定回放记录，但不自动代表当前候选已通过 |
| `DESIGN_ONLY` | 仅存在于方案或当前合同，尚未由当前源码及验收共同证明 |
| `NEEDS_REVIEW` | 证据不完整、合同冲突或需要用户决定 |

## 3. 总体架构对照

### 3.1 DSV 原版

```text
trading.exe 单进程
├─ Tk 主线程
│  ├─ 页面与图表
│  ├─ 每 400ms 读取日志队列
│  ├─ 约每 1s 读取账户/持仓共享快照
│  └─ 定时启动、按钮和通知
│
├─ daemon StrategyThread
│  ├─ TqApi.wait_update(deadline = now + 0.5s)
│  ├─ 全部合约行情、K线、账户、订单和成交
│  ├─ 全部策略、日停、止盈止损和断流处理
│  ├─ 每合约 TargetPosTask
│  ├─ 当日状态、账户展示数据和日志
│  └─ 异常后重试或结束
│
├─ ManualTradeThread
│  └─ 手工通道直接调用 insert_order / cancel_order
│
└─ TqSdk 内部 asyncio 任务
   ├─ WebSocket 行情/交易连接
   ├─ 重连请求重放与完整截面等待
   └─ TargetPosTask / InsertOrderTask 报撤单推进
```

核心特点是“一进程、两类前台线程、一个自动策略线程”。界面和自动交易不在同一线程，但仍共享进程、GIL、内存、文件系统和进程生命周期。

### 3.2 OnlineTrader 当前 Standalone Runtime

```text
OnlineTrader 统一桌面入口
├─ WorkbenchWindow / WorkbenchLayout（Qt 客户端）
│  ├─ ClientStateStore
│  ├─ CommandBus
│  └─ 本机同用户控制通道
│
├─ runtime-view-process（可丢弃、可有界重启）
│  ├─ 快照订阅和增量 ViewHistory
│  ├─ 历史、运行日志和账户只读接口
│  └─ Workbench 命令入口及身份检查
│
└─ Standalone Runtime Owner 进程
   ├─ StandaloneDsvOwner（唯一交易 Owner）
   ├─ DsvSdkAdapter（唯一 SDK 访问者）
   ├─ DsvSession（有序事实与策略状态）
   ├─ ExplicitOrderController（目标、child、撤改单、UNKNOWN）
   ├─ Journal（当前 boot 唯一 SQLite writer）
   ├─ ControlServer / WorkbenchPublication
   └─ AccountPublication / BrokerHistoryWriter
```

当前结构已经把“交易权威”和“界面/历史读取”做了进程级分离。问题不在于缺少分层，而在于 Owner 进程内部的同步热路径仍过重，部分名义后台任务只把编码移出线程，投影准备仍发生在 Owner 线程。

## 4. 逐项做法差异

### 4.1 交易权威和发送者

| 项目 | DSV 原版 | OnlineTrader 当前做法 | 判断 |
| --- | --- | --- | --- |
| 自动策略发送 | `StrategyThread` 内每合约 `TargetPosTask` | `StandaloneDsvOwner -> DsvSession -> ExplicitOrderController -> DsvSdkAdapter` | OnlineTrader 责任链更显式 |
| 手工发送 | `ManualTradeThread` 直接 `insert_order`，与自动目标任务语义不同 | Workbench 命令进入同一 Owner 和同一 Controller | OnlineTrader 避免第二发送旁路 |
| UI 是否可发送 | Tk 回调可启动线程或手工线程 | Qt、runtime-view、CLI 均不能直接访问 SDK sender | OnlineTrader 隔离更强 |
| 同机单实例 | 应用默认单实例；可选守护按进程名检查 | 机器级 Runtime lock；再次启动附着/唤醒 | OnlineTrader 身份更精确 |
| 多机同账号 | 无跨机统一责任合同 | 明确允许各机独立 Runtime，同步同一柜台账号，不用本地文件跨机互斥 | OnlineTrader 合同更清楚 |

DSV 的自动通道在逻辑上也是单 Owner，但手工通道直接报单，自动任务与手工任务的数量、撤改和持仓归属没有统一为一个耐久状态机。OnlineTrader 不应为了模仿 DSV 界面操作而恢复手工 SDK 旁路。

### 4.2 SDK pump 和循环推进

| 项目 | DSV 原版 | OnlineTrader 当前做法 |
| --- | --- | --- |
| 空闲等待 | 每轮 `wait_update(deadline=time.time()+0.5)` | 无活动 timer 时默认 0.5 秒；有 timer 时由 `poll_slice()` 缩短 |
| `wait_update=False` | 忽略返回值，继续处理 pending、收盘、断流和状态 | 仍构造 frame、执行 `apply_round()`，继续计时和状态推进 |
| SDK 后台任务 | `wait_update()` 同时实际发网络请求、推进 TargetPosTask、收包并合并数据 | 通过 `DsvSdkAdapter.step()` 驱动当前连接及显式 Controller |
| 轮次成本 | 业务逻辑集中，但没有每轮完整 SQLite 事务 | 每轮可能执行事实扫描、深拷贝、序列化、FULL 同步事务、dispatch 和投影 |

两边都证明了一点：不能把“没有新 Tick”解释为“本轮什么也不做”。订单终态、撤单、退出刷新、时钟和用户命令必须继续推进。

OnlineTrader 当前真正需要优化的是空转轮次成本，而不是简单把 500ms 改成 50ms。若同步事务和投影仍在每轮执行，缩短等待只会放大 CPU、深拷贝和 fsync 压力。

### 4.3 网络断线、重连和完整截面

DSV 随包 TqSdk 3.10.2 已包含以下机制：

1. WebSocket 断开后随机退避重连；
2. 行情侧记录并重放 `subscribe_quote`、`set_chart`；
3. 交易侧重放登录和结算确认；
4. 重连期间暂缓把零碎上游数据交给应用；
5. 行情订阅、K线定位或交易数据完整条件满足后，才把重连累积数据作为完整截面交给 `TqApi`；
6. DSV 外层 `StrategyThread.run` 对部分网络异常另有约 5 秒级重试，并恢复当日状态。

OnlineTrader 当前额外维护：

- `source_epoch`，用于区分连接代次；
- `connection.complete()` 完整性事实；
- 断线时 `DsvSession.disconnect()` 和 `synchronized=False`；
- 重连后按完整柜台订单快照核对 `UNKNOWN`；
- 当前 epoch、柜台日、日历、时钟和新鲜行情共同决定是否恢复新开仓；
- 人工 STOP/PAUSE 通过 `authority_generation` 撤销未完成的自动 ARM，瞬断不能覆盖人工停止。

可参考点是 TqSdk 已有的“请求重放 + 完整截面后再放行”，而不是再造一套并行行情连接管理器。当前方案中的 TqSdk 3.10.5 升级应是独立兼容性和断线行为验证，不能替代 Owner 负载诊断，也不能在没有证据时被描述为必然解决全合约滞后。

### 4.4 行情新鲜度、静默和断流

| 项目 | DSV 原版 | OnlineTrader 当前做法 |
| --- | --- | --- |
| 新报价身份 | `q.datetime` 非空且与上次不同才刷新本地到达时钟 | 记录原生时间、quote identity、source epoch、盘段、age quality 和覆盖状态 |
| 开仓行情年龄 | `max_quote_age_seconds > 0` 且严格超限才阻止；0 关闭该过滤 | `age_quality == VALID`、当前盘段、native coverage 和策略年龄条件共同参与 |
| 单合约静默 | 每合约保存最后新报价到达时间 | 已有每合约 observation，但尚未形成完整 `symbol_health` 投影 |
| 断流退出 | 正阈值、当前交易盘段、距开盘大于 60 秒且 quiet 严格超限后进入 `force_close_pending` | 保护和退出目标由 Owner 维持；统一 feed health 与清晰 UI 分级仍是设计候选 |
| 休市 | 不进入活跃盘段断流分支 | 日历显式给出盘段，但 UI 仍可能把部分非行情异常显示成“行情滞后” |
| 时钟 | 墙钟与单调时钟混合，部分分支仍以墙钟决定断流 | 有成对时钟、连续性、参考状态与 clock health，但 Target Owner 诊断尚未完整接入 |

DSV 值得参考的是“单合约静默”和“全盘断流”不要混为一谈，以及断流判断必须受当前交易盘段和开盘宽限约束。

DSV 不应照搬的部分包括：

- 配置为 0 时无提示地关闭开仓行情年龄过滤；
- 主要用墙钟计算断流时长；
- 睡眠/跳钟提示后没有静态证据证明同轮一定停止强平判断；
- 断流后直接把目标设为 0，却不能单靠目标调用证明撤单、报单或成交已经完成。

OnlineTrader 的 `QUIET` 只能用于诊断和避免把单合约无成交误判成全局行情源故障，不能用于放宽该合约的新开仓新鲜度门槛。

### 4.5 策略、保护和故障影响面

DSV 的全部合约在一个 `StrategyThread._run` 中顺序处理。即使每个合约有独立 `TargetPosTask`，这些 SDK task 也依赖同一线程持续调用 `wait_update()`。因此：

- 一个合约的慢诊断、异常数据、同步文件写入或长计算可能延迟其他合约；
- 策略线程阻塞会同时延迟行情接收、保护判断、撤改和界面共享状态更新；
- Tk 界面通常不会直接阻塞策略循环，但进程级崩溃、GIL、共享锁和资源耗尽仍是共同故障域。

历史现场曾记录 DSV 收盘后主力诊断导致约 13.9 秒和 14.3 秒的慢循环。这说明“UI 与交易线程分开”并不等于“局部故障不会影响全部合约”。

OnlineTrader 当前同样保持单交易 Owner，这是为了对行情、目标、订单、成交和持久化建立全序，而不是性能缺陷本身。需要缩小的是 Owner 内的非交易工作，不是拆出第二个可发送线程。

建议保留在 Owner 的职责：

- SDK pump 和当前连接对象访问；
- 时钟、盘段和行情身份校验；
- 策略目标和保护目标；
- child order、撤改单、UNKNOWN 和数量占用；
- commit-before-send；
- 用户命令与柜台事实的顺序裁决。

建议移出 Owner 热路径的职责：

- Workbench 复杂投影、格式化和体积校验；
- K线 Decimal 转换和图表字典组装；
- 历史分页、日报、对照报告和日志检索；
- 有界诊断文件编码与写盘；
- 可从已提交快照重建的账户展示材料。

### 4.6 订单、撤改、部分成交和 UNKNOWN

DSV 自动通道把目标净持仓交给 `TargetPosTask`：

- `set_target_volume()` 只把目标写入 channel，必须继续 `wait_update()` 才可能发单；
- 买单只有新价格更高、卖单只有新价格更低时才触发撤单；
- 子单终结后按 `本次手数 - volume_left` 扣减剩余目标；
- `InsertOrderTask` 等待订单 `FINISHED`，并等待委托累计成交量与逐笔 `trade_records` 数量一致；
- task 退出时如订单仍 ALIVE 会请求撤单；30 秒是退出清理等待上限，不是“订单一定未发送”或“可以释放数量”的证明。

优点是正常在线过程中，SDK 已处理了大量追价、今昨仓和逐笔一致性细节。缺点是这些任务主要是当前进程内存对象：

- SDK 调用抛异常不天然证明未发送；
- 本地订单对象不证明柜台受理、成交或重连核对完成；
- 进程退出后不能把 TargetPosTask 内存状态当成恢复事实；
- 原版慢部分成交样本中，策略层曾把自有部分成交误判成手工减仓，随后产生补开再反向平仓。

OnlineTrader 的主要差异是显式保存每个 Target、revision、child、attempt、prepared effect、send state 和最大潜在数量：

- 每个 child 在 SDK 调用前提交；
- 进入 SDK 不确定边界后的异常记录为 `SEND_POSSIBLE/UNKNOWN`；
- `UNKNOWN`、Cancel Pending 和未终态部分成交继续占用最大潜在数量；
- 撤单请求不释放数量；
- 只有队列前同步拒绝等可证明情形才进入 `DEFINITELY_NOT_SENT`；
- 重连后完整柜台快照和新的 source epoch 可提供负向核对证据；
- 用户批准的慢部分成交差异保留 5 手目标，不复制原版额外反向交易。

因此应参考 DSV 的价格回调和终态/逐笔等待语义，但不能用 `TargetPosTask` 的当前内存对象替换 OnlineTrader 的 child durability、UNKNOWN 占量和柜台核对。

### 4.7 持久化和重启恢复

| 项目 | DSV 原版 | OnlineTrader 当前合同 |
| --- | --- | --- |
| 配置 | 程序目录 `config.json`，另有备份和 DPAPI 凭据 | 版本默认值、用户覆盖、Runtime 配置和凭据隔离 |
| 当日策略状态 | `state/daily_*.json` 等本地文件，重连/重启尝试恢复 | 当前 boot 临时 Journal；不跨 boot 交接执行责任 |
| 订单任务 | 当前 TqSdk/TargetPosTask 内存对象 | 当前 boot 的显式 child/attempt/UNKNOWN |
| 重启权威 | 本地当日状态加重新连接后的 SDK 事实，带仓完整性仍有待核范围 | 柜台持仓、委托、成交、资金为权威；新 boot 完整同步后仍保持 DISARMED |
| 历史 | 日志、状态和本地输出 | `account-history.db` 为可从柜台重建的只读缓存 |
| 异常残留 | 可选守护重新拉起程序 | 旧临时 ledger 只读历史，不成为新 boot 启动权威 |

DSV 的同日状态恢复对策略连续性有吸引力，例如日停、保护阶段、最高点、分钟消费和次数。但其静态证据没有证明所有带仓、活动委托、迟到成交和跨日组合都能安全恢复。

OnlineTrader 当前合同明确拒绝把旧执行账本交给新 boot，这是正确的订单安全边界。不过，以下“业务连续性状态”需要单独决定，不能被订单账本禁交接原则顺带忽略：

- 当日停开及原因；
- 月度停止和业务计数；
- 已消费分钟/信号去重；
- 保护阶段、峰值、保本线和名义策略统计；
- 故障期间人工 STOP/PAUSE 的持续效果。

这些状态若能从柜台历史、当前持仓和明确配置重建，应按可复核来源重建；不能可靠重建的，应在新 boot 保持 DISARMED 并明确提示缺口。不得直接加载 DSV JSON 或旧 Runtime ledger 恢复发送责任，也不得静默清零后自动开仓。此项应标记为 `NEEDS_REVIEW` 并在后续当前合同中定稿。

### 4.8 界面、快照和控制可用性

DSV 没有跨进程快照协议。策略线程在锁内更新共享 `account_info`，Tk 主线程约每秒复制并显示；日志通过队列约每 400ms 消费。因此它没有“5 秒快照协议过期后四层禁用命令”的问题，但也没有可靠的快照版本、boot、账号、配置 revision 和命令并发控制。

OnlineTrader 当前优点：

- `runtime-view-process` 可崩溃并有界重启，不重启交易 Owner；
- Workbench 通过版本化快照取得账号、boot、checkpoint 和配置 revision；
- 命令到 Owner 后再次按当前实态校验；
- `MANUAL_CLOSE` / `MANUAL_CLOSE_ALL` 使用 `target_revision` 乐观并发控制；
- 旧快照不能自行成为交易事实。

当前缺陷：

- `WorkbenchPublication` 虽有后台 `_encoder`，但 `project()` 在 Owner 线程先同步执行；当前 `prepare_workbench_publication()` 已完成 `snapshot()`、`projection_batch()` 和 `workbench_view()`，后台只调用闭包取结果并编码；
- `workbench_view()` 仍读取活 `adapter`，包括 K线和行情展示转换；
- `ClientStateStore.capability()` 仅在 `READY` 时允许操作；
- `CommandBus.submit()` 同样要求 `READY`；
- Workbench 按钮启用逻辑在 Runtime 模式仍受 `enabled` 和 capability 限制；
- 因而快照过期时，虽然 Owner 还在运行，用户也可能无法提交停策略、撤单、平仓和重新核对。

这不是 DSV 机制可以直接复制解决的问题，应按当前 Runtime 的命令绑定合同修复：旧快照只用于确定目标 Owner 和原命令身份，降风险请求到达 Owner 后必须用 Owner 当前实态重新校验；开仓、ARM、改参和认领外部仓继续禁止。

### 4.9 诊断和运维

DSV 提供的运维手段包括：

- 中文运行日志和连接状态；
- 网络自检脚本；
- NTP 时钟核对及人工触发 `w32tm /resync`；
- 约 60 秒行情及慢轮次诊断；
- 钉钉通知与同品种/同类型节流；
- 可选的 Windows 启动目录 + 批处理守护，每 60 秒按 `trading.exe` 进程名检查并用 `--autostart` 拉起。

这些手段对人工排障有价值，但边界有限：

- 外部网络和时钟脚本是诊断，不证明交易线程当时看到的同一批原生数据；
- 同步日志和慢诊断本身仍可能进入同一进程的资源竞争；
- 按进程名检查不能证明正确实例、账号、Owner、订单责任或柜台同步状态；
- 自动拉起不能证明活动委托、迟到成交和保护已经安全重建。

OnlineTrader 已有更强的关联身份：Runtime boot、source epoch、配置 revision、Target/child/attempt、native order/trade、command ID 和失败阶段。但 Target Owner 尚未把这些身份完整接入行情延迟与 Owner 分阶段耗时。

建议借鉴 DSV 的“用户能直接运行网络/时钟诊断”和“关键状态变化通知”，但实现必须满足：

- 诊断只观察，不自动改变交易授权；
- 记录与实际 SDK round、source epoch 和合约身份关联；
- 有界队列、轮转、写入失败降级，不能阻塞 Owner；
- 不恢复 DSV 的无人值守守护。Standalone 当前合同是当前 Windows 用户手动启动和人工监督。

## 5. 典型故障场景对照

| 故障 | DSV 原版表现 | OnlineTrader 当前表现 | 推荐目标 |
| --- | --- | --- | --- |
| UI 页面渲染变慢 | Tk 与策略线程分开，通常不直接停止交易；同进程仍争用 GIL/锁 | Qt 客户端与 Owner 分离 | 保持当前进程隔离 |
| UI 进程崩溃 | `trading.exe` 进程级故障可能同时结束守护策略线程 | Owner 独立；Workbench/runtime-view 可重启 | OnlineTrader 更优 |
| 投影/编码变慢 | 账户汇总仍在策略线程，Tk 展示在主线程 | 投影准备仍同步在 Owner，编码在线程 | PR2 将纯投影真正后台化 |
| 日志/报告变慢 | 部分工作可能在策略线程或同进程资源域 | 已有部分后台 writer，但 Target 行情诊断缺失 | 所有非关键写入有界异步化 |
| SQLite 慢 | DSV 没有每轮完整执行 Journal | 每轮深拷贝、序列化、FULL 事务可能扩大延迟 | 先测量，再跳过无变化空轮 |
| 单合约无成交/不跳价 | 按该合约最后新报价计时；可触发断流退出 | 有 per-symbol observation，但 UI 健康分类不足 | 单合约 `QUIET/STALE` 与全局 feed 分开 |
| 全合约无行情 | TqSdk 重连 + DSV 断流计时；原因分段不足 | complete/source epoch 可阻断，但缺统一可见诊断 | PR1 黑匣子区分源、SDK、Owner、时钟、UI |
| SDK 断线后重连 | SDK 重放订阅并等待完整截面 | 另有 epoch、完整性和 UNKNOWN 核对 | 保留并验证，不重复造轮子 |
| Owner 循环阻塞 | 所有合约、保护和 UI 共享数据同时延迟 | 所有交易责任仍会延迟，但 UI 可独立显示旧快照 | 减少热路径；诊断 `sdk_pump_gap` 和阶段耗时 |
| 工作台快照过期 | 无版本化快照门禁；可能继续用共享旧数据 | 当前会阻止部分降风险命令 | P0 放行 Owner 重核的降风险请求 |
| 进程崩溃后自动拉起 | 可选批处理 60 秒拉起，恢复安全未证明 | 明确不使用无人值守 watchdog；新 boot DISARMED 同步 | 保持当前合同，不照搬 |
| 未知发送结果 | 依赖 SDK 对象和后续事实，缺应用级耐久分类 | `UNKNOWN` 保留最大潜在数量并等待柜台事实 | OnlineTrader 更优 |
| 多电脑同账号 | 无完整跨设备合同 | 各机独立 Runtime；柜台事实为权威 | 保持并加强设备/boot 诊断 |

## 6. 可以直接参考的点

### 6.1 UI 不执行交易循环

保留 DSV 的基本原则：界面负责展示和用户输入，不访问 SDK 活对象、不写执行账本、不成为 sender。OnlineTrader 已经做得更强，应继续用进程隔离，不退回共享可变字典。

对应现有方案：PR2 的投影真后台化，但后台只处理纯数据；Owner 线程只复制最小、闭合、不可变的投影输入。

### 6.2 `wait_update` 超时后仍推进责任

没有新行情不等于没有订单、计时器、退出、命令或重连工作。优化空轮时必须保留：

- SDK task 推进；
- 到期 timer；
- 撤单、UNKNOWN 和柜台完整性核对；
- 收盘和断流责任；
- IPC 用户命令及时处理。

对应现有方案：PR2 只能跳过“无任何有效状态变化的持久化工作”，不能跳过 SDK pump 或命令服务。

### 6.3 重连后等待完整截面

复用并验证 TqSdk 的订阅重放与完整截面能力。应用层继续保留 source epoch、账号、柜台日、完整水位和 UNKNOWN 核对，不在连接刚恢复时立即重新 ARM 或发送冲突单。

对应现有方案：PR1 先记录 3.10.4 基线，再对照 3.10.5；升级与 Owner 减负分开提交和判断。

### 6.4 单合约静默与全局断流分开

每个合约保存最后一个有效原生报价身份和本机接收单调时间；全局 feed health 只用于判断共同来源问题和告警，不能替代单合约新鲜度门槛。

对应现有方案：PR3 的 `symbol_health` 与 `feed_health`，其中 `QUIET` 只改变解释，不允许陈旧行情开仓。

### 6.5 价格回调和终态/逐笔一致性

保留原 DSV 已证的价格回调语义、价格恶化时撤改和“订单终态 + 逐笔数量核齐”边界。把这些作为显式 Controller 的输入和验收参考，不把 SDK task 变成第二套 sender 或持久权威。

对应现有实现：`ExplicitOrderController`、固定调度回放和慢部分成交 round 7/9。

### 6.6 用户可理解的外部诊断

网络、DNS、443、时钟偏差和当前连接状态应提供简单只读入口，结果写明时间、来源和限制。诊断失败只改变健康提示，不自行停止保护、平仓、校时或重新授权。

对应现有方案：PR1 结构化摘要与异常黑匣子，后续可在 Workbench 中提供脱敏读取入口。

## 7. 需要改造后才能参考的点

| DSV 做法 | 可参考价值 | OnlineTrader 必须增加的约束 |
| --- | --- | --- |
| 每合约 `TargetPosTask` | 成熟的目标调仓和追价语义 | 继续由显式 Controller 统一 child、UNKNOWN、数量和提交前证据 |
| 当日 JSON 状态 | 保留日停、保护阶段和业务计数连续性 | 只从柜台/历史/配置可证明来源重建；不能恢复旧发送责任 |
| 断流后目标 0 | 明确处理长时间无行情 | 使用可信单调时间、合法价格、当前盘段和柜台事实；是否自动退出须有明确产品合同 |
| 异常后重建 TqApi | 可从不可恢复 SDK 状态脱离 | 只有诊断证明 SDK 死锁且无活动发送责任时才设计；不得产生双 sender |
| 钉钉/声音通知 | 便于人工监督 | 后台有界、脱敏、去重；通知失败不能影响交易 |
| 主力、K线、账户汇总 | 用户界面需要 | 从已提交快照后台构造，不在 SDK pump 热路径做全量格式化 |

## 8. 明确不能照搬的点

1. **同进程 daemon 交易线程。** Qt 或主进程结束时不能连带删除 Owner 已接管的订单和保护责任。
2. **手工线程直接调用 SDK。** 手工开平、全平、撤单必须进入同一 Owner 和同一数量责任模型。
3. **按进程名守护并自动 `--autostart`。** 它不能证明实例、账号、boot、柜台完整性和未决订单安全，也违反当前手动启动合同。
4. **把目标调用当成订单结果。** `set_target_volume(0)` 不证明撤单、平仓或成交。
5. **把 SDK 本地订单对象当柜台确认。** 本地对象只能证明当前进程内调用返回和身份，不证明受理、成交或重连核对。
6. **0 值无提示关闭行情新鲜度。** OnlineTrader 应显式区分禁用、缺省和非法配置，并保持陈旧行情禁止新开仓。
7. **墙钟直接承担技术经过时间。** 断流、重试和 timeout 使用单调时间；UTC、盘段和柜台日用于业务定位。
8. **吞异常后继续使用部分状态。** 关键策略和订单状态异常应形成明确故障分类及后续责任，不能只写日志继续。
9. **全量账户和展示工作继续堆在一个策略循环。** 单 Owner 是顺序权威，不等于所有读侧工作必须同步执行。
10. **用本地状态猜测另一设备订单归属。** 多电脑同账号时只能使用稳定原生身份和柜台事实。

## 9. 对现有行情连续性方案的具体修订建议

### 9.1 阶段 0：保留为最高优先级，并先更新当前合同

[当前 Workbench 合同](runtime-workbench-current-20261120.md) 仍写着快照过期时“停用新的 UI 控制请求”，而候选方案要求放行停策略、撤单、平仓和重新核对。这是一个明确的 `CONFLICT / NEEDS_REVIEW`。

实施前应把当前合同改成操作分级：

- 快照过期仍可尝试：`STOP_DSV_STRATEGY`、`PAUSE_NEW_ENTRIES`、`CANCEL_SELECTED`、`CANCEL_OWNED`、`MANUAL_CLOSE`、`MANUAL_CLOSE_ALL`、`FLATTEN_THEN_STOP`、`REQUEST_RECONCILIATION`；
- 继续禁止：`ARM_ENTRIES`、`MANUAL_OPEN`、`SAVE_DSV_PARAMETERS`、`ADOPT_EXTERNAL_POSITION`、`SET_ENTRY_POLICY`；
- Owner 必须重新检查账号、boot、配置 revision、目标 revision、当前持仓和活动委托；
- 旧快照只用于路由到原 Owner，不用于决定实际平仓数量或重发。

当前工作区的 `ClientStateStore.capability()` 和 `CommandBus.submit()` 仍只接受 `READY`，所以 P0 尚未完成，不能仅因 `OWNER_RECHECK_OPERATIONS` 常量已经存在就宣称已放行。

### 9.2 PR1：拆成“诊断”和“SDK升级”两个可独立判断的提交

先在当前 TqSdk 3.10.4 上接入：

- `sdk_pump_ns` / `sdk_pump_gap_ns`；
- 原生事实提取、事务、dispatch、投影准备和编码耗时；
- 每合约原生报价身份、source age、接收 age、盘段和 age quality；
- source epoch、complete、disconnect/resync 变化；
- 有界摘要与异常前后环形缓冲。

只有取得基线后，再升级 3.10.5 并对比断线、正常关闭、订阅恢复、内部 Hook 和队列前拒绝识别。否则即使升级后现象消失，也无法判断是 SDK 修复、重启副作用还是 Owner 负载变化。

### 9.3 PR2：作为解决局部负载扩大为全局延迟的核心工作

优先顺序建议：

1. 停止无订阅读端时的 `_commit_views` 全量快照堆叠；
2. Owner 只复制最小纯数据，后台构造 `projection_batch()` 和 `workbench_view()`；
3. `workbench_view()` 不再读取活 `adapter` 或 DataFrame；
4. 对已吸收的终态订单和成交做增量索引；
5. 测量无变化轮次后，决定是否跳过 checkpoint/receipt 写入；
6. 最后才比较 500ms、100ms、50ms poll slice。

后台投影必须使用闭合快照，不能在后台线程访问 TqApi、当前 Session 可变状态或 SQLite writer。

### 9.4 PR3：全局健康只负责解释和告警，单合约状态负责开仓门槛

建议状态关系：

```text
SDK/Owner 全局状态
  HEALTHY / DEGRADED / RECONNECTING / RESYNCHRONIZING /
  CLOCK_UNCERTAIN / OWNER_STALLED / FEED_SUSPECTED_STALLED

每合约状态
  FRESH / QUIET / STALE / WAITING_FIRST_QUOTE /
  MARKET_CLOSED / INVALID_QUOTE / SESSION_MISMATCH
```

全局状态不能把一个合约的陈旧行情变成可开仓行情。`QUIET` 表示“其他合约正常，因此不一定是全局源断流”，不表示该合约的新报价事实已经更新。

### 9.5 新增一项：业务状态重启连续性决策

现有方案重点处理当前 boot 内的行情和 Owner 连续性，但对 DSV 同日状态中哪些应从柜台重建、哪些应重置并保持 DISARMED，尚未完整定稿。

建议新增单独 ADR，逐项回答：

- 日停、月停、次数和净点数能否从柜台历史可靠重建；
- 带仓重启时保护阶段、峰值和保本线的合法来源；
- 已消费分钟/信号如何避免重启后重复；
- 数据不完整时是拒绝 ARM、要求人工确认，还是使用明确的保守重建规则；
- 这些业务状态如何与“旧执行 ledger 不交接、柜台为订单权威”同时成立。

这不是恢复旧订单 sender，也不是 Runtime handoff；它是业务策略连续性的来源决策。

## 10. 推荐落地优先级

| 优先级 | 工作 | 目的 | 与 DSV 的关系 |
| --- | --- | --- | --- |
| P0 | 快照过期放行降风险请求，Owner 实态重核 | 故障时用户仍能止损、撤单、停止和核对 | DSV 无跨进程快照问题，需按当前架构解决 |
| P0 | Target Owner 轻量常驻诊断 | 先区分源、SDK、Owner、时钟和 UI | 补足 DSV 诊断粒度不足 |
| P0 | 真后台投影与冗余快照清理 | 防止展示负载拖慢 SDK pump | 延续 DSV UI/交易分离原则并做得更彻底 |
| P0 | SQLite 空轮与全量扫描测量/优化 | 降低 Owner 固定成本 | DSV 没有同等每轮 FULL 事务负担 |
| P1 | per-symbol / global market health | 不让单合约静默扩大为全局故障 | 借鉴 DSV 每合约断流基点 |
| P1 | TqSdk 3.10.5 对照升级 | 验证正常关闭和重连修复 | 继承 SDK 重连模型，不替代应用诊断 |
| P1 | 业务状态重启 ADR | 定义日停、保护和计数连续性 | 吸收 DSV 同日状态价值，但不复制发送责任 |
| P2 | 经授权的多合约故障注入和完整日夜盘验收 | 证明故障影响面和恢复条件 | 用证据判断，而非凭架构名称判断 |

## 11. 建议验收条件

后续只有在用户明确授权测试时执行。文档建议的验收判断如下：

1. 慢 UI、慢投影、runtime-view 崩溃和历史查询不改变同输入下的策略目标、child 顺序和已提交业务结果；
2. 投影线程阻塞 20 秒时，Owner SDK pump、保护和降风险命令仍能推进；
3. 单合约静默不会误报全局 feed 故障，但该合约陈旧时始终禁止新开仓；
4. 多合约共同停更能区分行情源停更、SDK 未推进、Owner 阻塞、时钟异常和 UI 快照过期；
5. 重连后必须先取得当前 source epoch 的完整柜台事实和当前盘段新鲜报价，再按当前人工授权决定是否允许开仓；
6. 故障期间人工 STOP/PAUSE 后，恢复不能自动 ARM；
7. 快照过期时降风险请求能到达当前 Owner 并得到成功或具体拒绝码，开仓和改参仍被阻止；
8. SDK 调用未知、撤单晚到成交和部分成交期间，数量占用不提前释放，不出现重复或反向补单；
9. 新 boot 不读取旧执行 ledger 恢复 sender；柜台事实不完整时保持 DISARMED；
10. 业务状态重建缺口明确可见，不能通过静默清零日停/保护状态后自动开仓；
11. 诊断队列满、日志目录不可写或通知失败不阻塞 Owner，也不改变交易结论；
12. 所有结论记录候选完整 SHA、入口、环境、场景、预期、实际和 `PASS / FAIL / SKIP / NOT_RUN`。

## 12. 尚未由本文证明的事项

- DSV 在真实断线、带仓、活动委托和迟到成交组合下能否完整恢复；
- DSV 的可选守护拉起后是否会自动恢复开仓，以及恢复时的柜台完整性；
- 当前 OnlineTrader 在目标 Windows 机器上的 SDK pump、事务、投影和端到端 P95/P99；
- TqSdk 3.10.5 是否确实解决本次观察到的全合约滞后；
- 当前工作区 P0、PR1、PR2、PR3 的任何功能是否已经完成或通过；
- 真实柜台、多电脑同账号、自然订单、最终制品和完整日夜盘行为。

这些项目保持 `NOT_RUN / NOT_PROVED`，不能由静态代码、历史测试数量或本比较文档替代。

## 13. 最终建议

保留当前 Standalone Target Owner 架构，不退回 DSV 的单进程 daemon 策略线程，也不按合约拆分多个 sender。实施重点应放在缩小唯一 Owner 的同步热路径、恢复精确诊断和保障故障期间降风险控制可达。

DSV 的参考价值主要是四条：

1. UI 不直接承担交易循环；
2. `wait_update` 无更新时仍推进订单和保护责任；
3. 重连后重放订阅并等待完整截面；
4. 单合约静默、全局断流和休市需要分别解释。

OnlineTrader 必须继续保留的增强是：显式 child、commit-before-send、UNKNOWN 占量、柜台权威、新 boot DISARMED、统一手工/自动 sender、runtime-view 可丢弃、CommandBus 身份绑定和当前 Owner 实态复核。

这两类原则结合后，才能真正减少“一个局部展示、存储或行情故障扩大成全部合约、策略、保护和界面同时失效”的风险。
