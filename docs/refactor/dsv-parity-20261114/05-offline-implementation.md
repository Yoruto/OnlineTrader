# 首阶段实现与执行选型

基线代码为 `af8b53b`，方案提交为 `e148ab2`。本阶段建立独立规则目录，并把现有生产入口中的一组纯计算移入该目录。在线运行器仍由原账户 owner 和账本 writer 驱动。候选源码保持原有输入契约、处理顺序和交易行为，供后续逐项对照冻结 DSV。

## 可复核的原版与现有入口

- 原发行 ZIP、EXE、GUI 字节码、嵌入及随包 SDK、现有 SDK 目标任务的摘要见 [基线清单](../../../qa/dsv_parity/fixtures/baseline-manifest.json)。留档的历史脱敏业务配置仍以保存值为准，未取得本轮原版运行时生效值。
- `python -m qa.dsv_parity.verify_reference --archive <DSV原ZIP> --sdk-source <已安装的3.10.4目标任务源码>` 在独立 Python 3.14 下核对文件和指定代码对象指纹。只读 ZIP 成员和 PyInstaller 归档，不提取文件、导入原应用或运行原业务初始化。
- 在 Python 3.14 下运行 `python -m qa.dsv_parity.dsv_reference_entry_probe --archive <DSV原ZIP>` 取得四个原版纯函数的边界预期。在产品 Python 3.13 下运行 `python -m qa.dsv_parity.sdk_reference_probe --archive <DSV原ZIP> --sdk-source <已安装的3.10.4目标任务源码>` 复现两版目标任务的调用顺序、后到成交和撤单差异。两个探针均使用离线注入输入，未运行原应用或完整 SDK。
- [行为基准目录](../../dsv-behavior-baseline-20261113.md#逐项记录) 作为账户、行情、策略、手工、保护、统计、参数、日志、运行生命周期和桌面入口的起点。各项原版出处和现状沿用该基准。首阶段接入路径及验收见下表；其余路径继续使用旧入口，后续需补目标路径和独立黄金样本。

| 功能与规则 | 原版出处 | 当前调用链及首阶段去向 | 用例族及范围 |
| --- | --- | --- | --- |
| 触发线、振幅、双价追价和延续方向 D01–D04、D07 | `_run` 7993–8027、8212–8221、`chase_within_cap` 1695、`hysteresis_ready` 1743、`decide_continuation` | `kernel.dsv_strategy` → `strategy.entry` → `dsv_core.entry_rules`。机会消费和盘次状态仍由现有入口管理 | C005–C009、C012、C017–C018 的基本公式及上一版回归 |
| 自动与固定手数 D05 | `calc_volume` 5246–5338，`risk_based_volume` 5173 | 内核及工作台 → `strategy.risk` → `dsv_core.sizing_rules`。数据完整性和金额预占仍由现有入口管理 | C004、C013–C016 的现有精度及分支边界 |
| 半仓剩余量、普通移动止盈 D08–D09 | `half_close_lots`、`_run` 7596–7659、7663–7716 | `kernel.execution` → `protection.reducer` → `dsv_core.protection_rules`。原阶段及成交核齐逻辑仍由原归约器管理 | C025–C032 的半仓整手和回撤计算子集 |
| 净点数日停、连续交易日月停及延续限制 D10 | `_run` 6007–6040、7079、7750–7765 | `kernel.statistics` 和入场复核 → `strategy.stops` → `dsv_core.business_statistics` | C033–C036 的停开状态计算子集 |
| 自动报价、收盘剩余时间切价 D06、D11 | `_price_fn` 4850–4910，`_run` 6897–6915 | `kernel.execution` → `execution.policy` → `dsv_core.price_rules`。报价上下文、时效、合法价检查、撤单和发送仍由原链管理 | C022、C037–C039 的现有报价及切价边界 |

数字规则仍有取证差距。原版 `opening_jump_block(0.3, 0.2, 0.1, 1)` 的二进制浮点比较返回 `False`，现有 Decimal 实现返回 `True`。原版 `chase_within_cap` 还返回超出量，现有调用使用是否通过。原版报价在缺盘口和最新价时还可能尝试涨跌停限价。未改变这些现状；转入 C004、C037 的有状态同输入对照，勿用已有单测替代原版预期。

## ADR02、ADR03 与 ADR08

**显式子单控制器作为替代架构的执行方案。** 保留每个子单发送前的必要事实及提交确认，保留同账户唯一执行控制者。未批准降低到仅持久化目标。当前实现继续复用原 `Kernel` 执行链；新控制器尚未接入。

| 闸门 | 观察和决定 |
| --- | --- |
| G1-A | 原 `StrategyThread._run` 用 `make_price_fn` 创建目标任务，支持最小开仓手数，初始化有兼容回退。需要继续用批次输入核对实际回调和生命周期。 |
| G1-B | 3.10.2 `InsertOrderTask._run` 第 746–753 行先创建 SDK 子单 ID 并执行 `insert_order`，之后才发出订单副本。3.10.4 顺序一致。公开目标任务接口能设置目标并观察成交，未见能在子单提交前接收完整子单并等待持久化的接口。该项已足以决定采用显式控制器。 |
| G1-C/D | 原版默认目标覆盖账户合约净仓。`ManualTradeThread.run` 在 9240/9270 行直接报单，9177–9198 行处理撤单队列。策略 `_run` 本身也有直报出口：在 6436 行请求目标归零，6443–6465 行的重试分支构造极限价并直接 `insert_order`，其 `offset` 为 `CLOSE`。薄控制器需统一承接这些出口，避免同时留有未决目标单及第二条直报链。分支可达性、多空毛仓、手工交接和外部仓仍需按 C048 等场景核对。 |
| G1-E | 当前 CPython 3.13.15 与 SDK 3.10.4 保持原锁定环境。原发行包归档使用 Python 3.14 和 SDK 3.10.2。对照两版固定源文件，3.10.4 的重报价撤单新增 `ALIVE` 和交易所订单 ID 要求。缺 ID 时两个版本动作不同。完整依赖组合、发布制品与真实连接兼容性仍待验。 |
| G1-F/G | SDK 普通等待直到终态与成交数量吻合，退出清理才使用 30 秒等待上限。完整回报归属、停止、重启和柜台换日仍需事件链验证。旧的全局超时机制按计划在替代路径接管并复测后退役。 |

持久化边界分三段。已记录目标、SDK 尚未取出目标时，存储只有目标身份。SDK 创建子单至 `insert_order` 调用返回之间，完整子单尚未由任务通知外部。调用返回至柜台事实齐全之间，原生动作的结果可能未知。目标记录无法提供后两段的逐子单恢复身份。独立离线探针对固定的 SDK 方法注入报价、缺交易所 ID、订单终态先到、成交迟到及调用边界异常，确认上述调用顺序。真实 SDK 发包窗口和柜台恢复尚未验证。[官方目标持仓接口](https://doc.shinnytech.com/tqsdk/latest/reference/tqsdk.lib.html) · [官方事件循环说明](https://doc.shinnytech.com/tqsdk/latest/usage/framework.html)

原版逐轮顺序的静态定位已明确主循环的几个入口。`_run` 5892 行驱动 `wait_update`，5919 行检查对象变化，随后是账户更新和月/日业务状态、各品种仓位同步和收盘管理。早盘延续判定在 7120 行，未确认入场处理在 7210–7422 行，保护在 7436–7802 行，普通入场在 7859–8362 行。上述为源码位置，事件样本还需保留各自分支可达性、各次墙钟读取及批内原生更新，现有逐 Tick 归档无法补回这些信息。下一阶段先以可复查的批次样本验证 `BusinessFrame` 到决策及原生回报的接口，再由同一控制器串行执行自动目标、手工命令和退出。

## 原生产链及退役依赖

| 职责 | 入口与依赖 |
| --- | --- |
| 账户控制 | 工作台 F5 → `runtime.desktop_bootstrap` → `runtime.dsv_simulation`；CLI `dsv run` / `simnow run` 进入同一类运行器，独立 CLI `simulation run` 有手工模拟入口。策略、手动和保护经同账户 `AccountLock` 和 `Kernel`。`serve` 默认用 `UnavailablePort`。 |
| 连接与发送 | TqKq 和 SimNow 懒加载各自网关。原 `_poll_commits` 核验账本提交确认，再由 `Kernel._dispatch` 调网关 `send`。策略/手工共用，保护退出和替换重走子单提交。撤单经过 `Kernel.request_cancel` 或 `kernel.control` 与 `kernel.native_control` 的柜台撤单审核和事务。最终 SDK 调用落在 `gateway.tqkq` / `gateway.tqkq_dsv`；启动元数据的只读包装拒绝交易调用。 |
| 状态与事实 | `Kernel`→`Ledger._work` 为交易 writer。报表、启动检查、操作日志回补使用只读连接。对账费用登记是持 owner 排他锁、复查旧账本后使用 `Ledger` 的独立维护入口。日停/月停/分钟消费、目标、原生事实和占量的迁移字段仍需逐项核对。 |
| 当前附加机制 | X01 依赖 `execution.progress`、`cancel_times`、`recovery_required` 和状态投影。X02 依赖 `runtime.dsv_simulation`、`kernel.scheduler`、`_price_only` 派发与保护恢复。X03 依赖启动范围、`kernel.online_dsv`、`strategy.risk`、最终 `_dispatch`。X04 依赖原生行情、盘段和观察检查点；X05 依赖旧 epoch 及 IPC；X06 依赖比较、操作日志注册、归档扫描及展示。旧链的完整删除闭包待迁移和对照后确认。 |
| 源码与包入口 | `pyproject.toml` 的 CLI/GUI → `cli.py` / `desktop.entrypoint.py`；便携配置 `release/OnlineTrader.spec` 从 `release/entry.py` 进入。候选的策略配置审计记录新增纯规则源文件摘要，便携配置声明相同源码。 |

首次生产接管仍以 G3 注入事实、独立故障验证和迁移演练为前置。G2 尚缺完整 `BusinessFrame` 和原版逐轮 `DecisionBatch` 取证。新控制器、新存储、原版对跑、校时、制品和冷切换均待完成。当前记录仅覆盖源文件拆分和已验证的执行选型依据。

## 验证记录

候选生产源码由独立 Agent 执行标准离线检查，1532 项通过，无失败、错误或跳过。独立公共入口探针的输出与基线逐字节一致。在与原版纯助手归一比较的 28 组结果中，27 组一致，剩余一组为上文跳空浮点边界。既有历史归档回放复现 2,784 条开仓时段样本、1,178 次 SDK 报价变化、棕榈油 30 手建议及白银 4 手保护归零。该归档仅包含部分原生行情路径。[独立报告](../../../output/dsv-parity/independent/test-report.md) · [标准检查](../../../output/checks/20261114T091224Z-de1e26d2/result.json) · [归档回放](../../../output/dsv-parity/candidate-archive-replay.json)
