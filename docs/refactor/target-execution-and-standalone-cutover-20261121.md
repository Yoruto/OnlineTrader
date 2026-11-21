# Target Execution 与 Standalone 单核心接管合同

> 状态：`CURRENT DESIGN / IMPLEMENTED CANDIDATE`
> 日期：2026-11-21
> 适用范围：DSV Replacement 的目标执行、Standalone Trading Runtime 的后续接管。
> 验证状态：源码实现候选；功能、回放、柜台、GUI、打包均 `NOT_RUN`。

## 1. 决策

OnlineTrader 不新增独立 Reconciler、OMS、事件总线或第二个交易服务。DSV Strategy 产生版本化业务目标，`ExplicitOrderController` 同时拥有目标数量核算、child order 状态机、撤改单、UNKNOWN 占量、平今平昨路由和 commit-before-send。柜台提供持仓、委托和成交事实；每次事实或目标变化后重新计算。

最终生产形态只有一个 Standalone Runtime Owner、一个 DSV Strategy、一个 `ExplicitOrderController`、一个 SDK sender 和一套 Workbench。当前 Legacy Kernel 在完成冷切换前仍是独立维护路径，不与 Replacement 同时控制同一账户。

## 2. 状态权威和生命周期

| 状态 | 权威/写入者 | 生命周期与重建 |
| --- | --- | --- |
| 账号持仓、委托、成交、资金 | 柜台；Runtime 同步 | 每次 boot 和重连重新同步 |
| 当前 boot 的 Target、child、UNKNOWN 和保护责任 | 当前 Runtime Owner 的临时 Journal | 只在当前 boot 存活，不交接 |
| 历史成交、委托和收益 | 可重建 history cache | 不参与发送或责任认领 |
| 配置默认值与用户覆盖 | 版本配置文件 | 按 revision 合并 |
| Workbench 草稿和 pending correlation | Workbench 进程 | 不跨进程恢复或重发 |

旧 Replacement 的冷迁移库、迁移收据和 `AccountLock` 启动合同仍只属于 Replacement 候选，不能直接成为 Standalone 接管实现。Standalone 接管必须使用机器级 `RuntimeLock`、每 boot 临时 Journal 和 DISARMED 柜台基线。

## 3. Target 的所有权边界

Target 不是整个期货账户的绝对目标，而是当前 Runtime 有证据承担的策略责任：

```text
BrokerAccountPosition
  = RuntimeProvedPosition
  + ExternalOrUnattributedPosition

TargetOwnedPosition
  只约束 RuntimeProvedPosition 及其未终态 child
```

不得按价格、数量或时间相似性认领另一台电脑或人工产生的仓位、订单。外部或无法归属的活动订单与当前目标冲突时，阻止新的冲突发送并进入柜台核对。账号级全部撤单或全部平仓只能由明确的用户操作触发。

每个 Target 最少绑定 `target_id`、`target_revision`、合约、方向、目标责任数量、原因、策略/配置 revision、柜台日、有效窗口和输入事实水位。重复计算同一 revision 必须幂等；缩小、归零或反转必须形成更高 revision。

## 4. 目标数量核算

Controller 对每个方向计算：

```text
confirmed_qty
pending_qty = Working + CancelPending + SendPossible + Unknown 的未成交余量
potential_qty = confirmed_qty + pending_qty
needed_qty = max(0, target_qty - potential_qty)
excess_pending_qty = max(0, potential_qty - target_qty)
```

`DEFINITELY_NOT_SENT` 且本地证明与 child 相符时为终态，不占 pending。Cancel Pending、`SEND_POSSIBLE`、`UNKNOWN` 和尚未终态的部分成交 child 都按最大可能剩余成交量占量。

动作集合限定为：`WAIT`、`SUBMIT`、`CANCEL`、`REPLACE_AFTER_TERMINAL`、`REDUCE`、`DONE`、`RECONCILE_BROKER`、`BLOCK_INCOMPLETE_FACTS`、`REQUIRE_MANUAL_ACTION`。

撤单请求不释放数量。目标缩小或目标已满足且仍有可能使结果偏离目标的 child 时，先撤旧 child、等待明确终态、读取新柜台事实，再重新计算；不得在 Cancel Pending 时叠加冲突订单。

## 5. UNKNOWN 与柜台核对

进入原生插单调用后发生异常只能记录 `SEND_POSSIBLE/UNKNOWN`，继续占用最大潜在数量。解除 UNKNOWN 需要明确的原生证据，例如稳定客户端引用对应的拒绝/终态，或达到协议保证完整覆盖水位的订单与成交同步。仅在当前活动委托列表中未看到订单，不足以证明未发送。

跨 boot 不继承旧 Target 或旧 Journal 的发送责任。新 boot 在柜台事实未达到完整水位时保持 `DISARMED/BROKER_FACTS_INCOMPLETE`；不得用空列表补齐，不得恢复旧 Target，不得发送可能重复覆盖的订单。

## 6. 正常和故障时序

```text
Broker Fact Frame
  -> 验证 account/day/epoch/watermark/completeness
  -> DSV Strategy 计算 Target revision
  -> Controller 核算 confirmed + pending + unknown
  -> 生成动作建议
  -> Journal 提交 child/attempt
  -> 发送前复核 Target revision 与事实水位
  -> SDK call
  -> 柜台产生新事实
  -> 重新计算
```

故障场景：

- Workbench/runtime-view 退出：Owner 和责任继续存活；不创建第二 sender。
- SDK 调用结果未知：保留数量并核对原 child；不换 ID 重发。
- Runtime 退出：临时 Journal 结束；新 boot 重新同步并保持 DISARMED。
- 另一台电脑交易：只作为账号级外部事实；不通过本地锁排除。
- 柜台事实不完整：阻止冲突动作并给出具体 completeness/watermark 诊断。
- 更新：只有 Owner 安全退出后切换版本；不通过 Journal handoff 更新。

## 7. 实施闸门

1. 冻结当前 P0：保留信号/执行价格分离、DEFINITELY_NOT_SENT、UNKNOWN 和退出责任诊断。
2. Target Contract：统一纯内存数量核算和原因码。
3. Controller：接管 Working、Cancel Pending、UNKNOWN、部分/晚到成交、缩小目标和路由。
4. DsvSession：只负责有序 Fact Frame、Strategy Target、Controller 推进和投影。
5. Standalone Boot Adapter：机器级单实例、临时 Journal、DISARMED 完整柜台基线；不依赖迁移库。
6. Workbench/CommandBus：复用现有 boot/account/config revision/command ID 合同。
7. 经明确授权的独立验收和冷切换。
8. 发行入口隔离及 Legacy Kernel 生产依赖闭包退役。

阶段 1–6 不能引入用户可选的双内核开关。回退以完整已知版本进行，不在同一 Runtime 中动态切换 sender。

## 8. 诊断与验收条件

关键记录至少关联 Runtime boot、account 摘要、Target ID/revision、事实水位、intent/child/attempt、SDK 边界阶段、native order/trade 和下一责任动作。原因至少区分：`TARGET_ALREADY_COVERED`、`TARGET_REDUCED`、`WORKING_ORDER_COVERS_TARGET`、`CANCEL_PENDING`、`SEND_RESULT_UNKNOWN`、`DEFINITELY_NOT_SENT`、`BROKER_FACTS_INCOMPLETE`、`POSITION_CHANGED`、`PRICE_REPRICE_REQUIRED`、`NO_LEGAL_PRICE`、`OFFSET_ROUTE_CHANGED`。

后续经授权的独立验收必须覆盖正常目标、部分覆盖、UNKNOWN、目标缩小、撤单晚到成交、退出目标、平今平昨、P0 行情竞态、SDK 边界崩溃、柜台事实不完整、新 boot、多电脑同账号外部事实、Workbench/runtime-view 生命周期和固定慢部分成交 round 7/9。

本文不表示上述行为已经测试或允许连接账户、发送订单、打包或发布。

## 9. 当前实现进度

截至 2026-11-21 工作区候选：

- `dsv_runtime.target_execution` 已提供单一纯内存数量核算；
- `ExplicitOrderController` 已使用该核算处理部分覆盖、UNKNOWN/Cancel Pending 占量和目标缩小；
- DsvSession 快照已提供 `target_execution` 投影以及 Target ID/revision；
- `StandaloneOwnerBinding` 和 `create_standalone_owner` 已建立机器 Runtime lock、同机迁移期账户 lease、临时 Journal、完整柜台基线及 Standalone SDK adapter 的显式边界；
- `StandaloneDsvOwner` 已复用现有 ControlServer、Workbench/View Process 投影和 CommandBus 收据合同；未新增第二套 IPC；
- Workbench 投影已提供当前 rule set、唯一 Authority/Execution policy、柜台资金摘要、目标状态和可操作命令能力；参数修改仍只允许预览，不写入当前执行权威；
- 人工减仓保留工作台选择的对手价/排队价/最新价，并校验单周期、批量持仓数量和所选 child 范围；策略产生的新退出目标会覆盖此前人工价格偏好；
- 账户页从同一 Owner 的只读原生快照发布，可重建 history cache 由有界异步 writer 写入且不参与执行决策；
- `FLATTEN_THEN_STOP` 只有在自有仓位、未终态 child 与分配冲突全部清空后才结束 Owner，并发布 `STOPPED` 证据；
- 目标 Owner 已接入版本固定的内部冷切换点，但当前常量仍选择 `LEGACY_KERNEL`，不向用户暴露双内核开关；自动 offset 也保持发送前计算合同；
- 现有桌面生产入口尚未切换到目标 Owner；独立测试、冷切换、发行隔离和旧 Kernel 退役仍未完成；
- 当前仅完成编译检查，所有行为验收保持 `NOT_RUN`。
