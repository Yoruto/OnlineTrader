# Runtime Workbench 柜台核对用户流程

> 状态：`CANDIDATE`  
> 日期：2026-11-24  
> 路径：Standalone Trading Runtime / Target Owner Workbench  
> 验证：设计评审与候选实现完成；`compileall` 通过；功能、GUI、柜台和制品验收 `NOT_RUN`

## 1. 问题与第一处差异

首页目前把“柜台仍在自动同步”“Owner 已进入持久停开”“账号存在其他电脑或人工产生的持仓”以及“本 Runtime 委托结果未知”都压成黄色的“待处理 / 柜台持仓或委托待核对”。真正的 `REQUEST_RECONCILIATION` 又只位于“策略持仓 → 柜台持仓差异”。用户无法判断应该等待、点击核对、修复时钟，还是去产生外部订单的终端处理。

第一处用户可见差异位于 `desktop/workbench_layout.py` 的首页摘要。Runtime 已经分别发布 `connection.synchronized`、`connection.recovery_required`、`connection.clock_fault`、`blocks[].reason_code`、自有 `orders` 和 `external_positions`；本次不新增第二套状态机，也不改变交易门槛。

## 2. 决定的用户流程

```text
首页发现异常或账号外部事实
  ├─ 柜台未完成同步
  │    └─ 显示“自动同步中，无需操作”；不提供会注定失败的人工核对按钮
  ├─ 时钟或 Owner 持久故障
  │    └─ 显示具体原因 + 原因码；提供“重新核对”
  │         └─ Runtime 用当前完整事实重新裁决，失败时保留原故障和回执
  ├─ 本 Runtime 报单/撤单结果未知
  │    └─ 说明数量仍被保留且系统会自动对照完整柜台快照；只提供委托明细，不诱导重复提交
  └─ 账号实仓与策略目标不一致（含外部开仓 / 非配置合约 / 非策略活动委托）
       └─ Standalone 不再把未归属持仓视为异常告警；首页表格与持仓对照统一展示“账号实际持仓 vs 策略目标持仓”
       └─ DISARMED 时仅预览待收敛差异（不发单、不撤单）；点击启动策略（ARMED）后自动先撤非策略委托再以激进限价收敛（详见 runtime-account-target-convergence-20261124.md）
```

首页不再使用没有责任主体的“待处理”与“非本机策略持仓/请到原终端处理”警告。未连接时保留最近命令回执异常提示，核对故障直达“阻挡详情”且按钮严格受能力矩阵控制。

## 3. 状态、权威与数据合同

| 数据 | 权威与写入者 | 本次 Workbench 行为 |
| --- | --- | --- |
| 柜台持仓、委托、成交和完整水位 | 期货账户 / Runtime Owner | 只读解释，不修改或猜测 |
| `owner_fault_code` 与 `clock_recovery_required` | 当前 boot 的 Target Owner | 从现有 `blocks` 显示具体原因 |
| UNKNOWN、自有活动 child 和数量占用 | 当前 boot 的临时 Journal / Controller | 保留原数量责任，只提供委托明细 |
| `external_positions` | 柜台事实减去本 Runtime 已证持仓 | 明确标为非本机策略持仓，不自动认领 |
| 首页展开、页签和按钮可见性 | Workbench 进程 | 可丢弃的展示状态 |

`REQUEST_RECONCILIATION` 继续绑定原账号、Runtime boot、配置 revision 和 command ID。按钮只发出已有命令；Owner 仍必须取得完整柜台帧并验证无归属冲突后才能清除故障。超时不得重发。

## 4. 正常与故障时序

```text
Runtime -> Workbench: 发布同步状态、blocks、自有订单、external_positions
Workbench -> 用户: 给出事实分类、具体原因和责任主体
用户 -> Workbench: 点击“重新核对”（仅适用状态）
Workbench -> CommandBus: REQUEST_RECONCILIATION + 当前绑定
CommandBus -> Owner: 原 command_id
Owner -> 柜台事实: 复核完整水位、身份、持仓和委托
Owner -> Workbench: SUCCEEDED / REJECTED / NEEDS_RECONCILIATION
Workbench -> 用户: 显示回执；失败时保留原因和下一步
```

- 投影过期或断线：继续显示界面重连，不将旧数据解释成柜台冲突。
- 柜台同步未完成：系统自动推进，不让用户点击一个会返回 `E_DSV_SYNC` 的按钮。
- 另一台电脑/人工持仓：不认领、不平仓、不撤单；只显示明细和处理边界。
- Workbench 重启：重新读取当前 Runtime/柜台基线，不继承旧页面 pending。
- Runtime 重启：新 boot 保持 `DISARMED` 并重新同步，不从旧页面恢复责任。

## 5. 兼容、回退与诊断

- 不改变 `workbench/1`、IPC、CommandBus、Controller、Journal 或 SDK 调用；旧 Runtime 投影仍可由新版 Workbench 使用已有字段解释。
- 回退只需恢复 Workbench 展示文件；Owner 状态和柜台事实不迁移、不重写。
- 首页工具提示保留原 `reason_code`，命令结果继续进入“命令回执”和运行事件，可按 boot、command ID 和配置 revision 反查。
- 不记录账户明文或凭据。外部持仓摘要只显示合约、方向和数量。

## 6. 可执行验收条件

本次用户未授权功能测试，以下均为后续独立测试规格，当前为 `NOT_RUN`：

1. 未同步但无持久故障：显示“自动同步中，无需操作”，不显示“重新核对”。
2. 每个持久 Owner 故障：显示中文具体原因和原始原因码，按钮提交一次原 `REQUEST_RECONCILIATION`。
3. 核对成功：故障摘要消失；核对失败：保留原因并显示同一命令回执。
4. UNKNOWN 报单或撤单：显示“系统自动核对、数量仍保留”，只允许查看委托，不暗示已失败、已解除或允许重发。
5. 仅有外部持仓：显示合约数/手数、“无需本机处理”和“不由本机策略管理”，点击直达差异表；不得发送交易命令。
6. 外部活动委托冲突：说明产生终端的处理责任，开仓门槛保持不变。
7. Workbench 投影过期、Owner 重启和 Runtime 新 boot：不把旧快照当作新的核对结果。

