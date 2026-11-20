# Runtime Workbench 本机命令安全合同

> 状态：PR-A 已固化合同（2026-11-20）。本文定义生产迁移的权限上限，不声明新 CommandBus 已完成生产验收。

## ADR：本机身份边界

决定接受当前 Windows 本机部署模型：`.workbench-control` 以 Owner SID 为普通访问边界，并保留 `LocalSystem` 这一更高权限操作系统主体。其安全含义是：

- SID、pipe peer 和 PID 只能证明调用进程身份，不能证明某一次请求来自真实鼠标点击；
- 已控制当前用户会话、且能实现协议的同 SID 进程可能调用 Workbench 白名单操作；
- `LocalSystem` 已高于当前用户权限，必须在连接/命令审计中保留其 peer 事实，不能描述成“仅当前登录用户”；
- 本通道不向 CLI、调度器或 Agent 发放隐式权限；这些入口继续使用有账户、操作集合和过期时间约束的 `DELEGATED_OPERATOR` grant；
- 若未来要求抵御同 SID 恶意进程，应另行设计服务端二次授权、部署保护或分权，不以确认弹窗、可复制 token 或 PID 白名单伪装成人工认证。

本决定不改变 Kernel 的最终复核。Capability 是展示事实，allowlist 是通道上限，grant、boot、配置版本、账户范围、交易时段、行情、委托、持仓、风险和账本状态仍由 Owner 在提交时裁决。

## Actor 与历史兼容

| 来源 | 新写入 actor | 读取兼容 | 退出条件 |
| --- | --- | --- | --- |
| Runtime Workbench endpoint | `LOCAL_USER / workbench-user` | 接受同 ID 的新 `LOCAL_USER`；兼容旧 `OPERATOR`；迁移期旧 `qt-*` 回执按既有规则可查询 | PR-C 完成 pending 迁移、旧直连命令链退役且宽限数据可证明不再需要 |
| Operator endpoint | `DELEGATED_OPERATOR / operator-<id>` | 接受同 ID 的新 kind 和旧 `OPERATOR` | operator 历史回执保留期结束后另行评审 |
| 旧 Workbench/CLI 直连 | 继续 `OPERATOR / <legacy instance>` | 原读取路径 | PR-C 前不改写历史语义 |
| 策略自动动作 | `STRATEGY`（由所属策略事实记录） | 不经 Workbench allowlist | 不适用 |

actor 只是审计来源，不代替授权。历史事件不回写、不批量改 kind。

## 生产 Workbench 操作矩阵

以下 14 项来自当前生产 Qt Workbench 的实际 `send(...)` 入口，也是 `.workbench-control` 的完整封闭集合：

| UI 动作 | Capability / 命令 | 主要 scope | 风险 | Actor | 额外 Owner 门槛摘要 |
| --- | --- | --- | --- | --- | --- |
| 暂停新增开仓 | `PAUSE_NEW_ENTRIES` | ACCOUNT | 高 | LOCAL_USER | 账本可写、当前 boot/revision |
| 停止 DSV 策略 | `STOP_DSV_STRATEGY` | ACCOUNT | 高 | LOCAL_USER | 账本可写；恢复模式另有事实门槛 |
| 允许策略开仓 | `ARM_ENTRIES` | ACCOUNT | 高 | LOCAL_USER | 同步健康、DISARMED、配置/授权策略匹配 |
| 全平后停止 | `FLATTEN_THEN_STOP` | ACCOUNT | 高 | LOCAL_USER | 柜台连接、已有持仓/委托责任复核 |
| 切换品种开仓策略 | `SET_ENTRY_POLICY` | ACCOUNT | 高 | LOCAL_USER | 配置/authority revision 与策略范围 |
| 保存在线 DSV 参数 | `SAVE_DSV_PARAMETERS` | ACCOUNT | 高 | LOCAL_USER | `live_parameter_save`、expected revision、Profile hash、账本事务 |
| 人工开仓 | `MANUAL_OPEN` | CONTRACT | 极高 | LOCAL_USER | manual grant、ARMED、合约、报价、风险和保护计划 |
| 人工平指定周期 | `MANUAL_CLOSE` | CYCLE | 高 | LOCAL_USER | 周期归属、剩余退出目标和柜台连接 |
| 人工全部平仓 | `MANUAL_CLOSE_ALL` | ACCOUNT | 极高 | LOCAL_USER | 全账户自有周期、柜台连接和退出责任 |
| 撤选中委托 | `CANCEL_SELECTED` | ORDER/CYCLE | 高 | LOCAL_USER | 委托归属和当前状态 |
| 撤全部自有委托 | `CANCEL_OWNED` | OWNED_ORDERS | 高 | LOCAL_USER | 自有委托集合和当前状态 |
| 柜台账户撤单 | `CANCEL_NATIVE_ACCOUNT` | ACCOUNT | 极高 | LOCAL_USER | manual grant、完整柜台快照和 review evidence |
| 请求核对 | `REQUEST_RECONCILIATION` | ACCOUNT/CYCLE | 中 | LOCAL_USER | 账本可写、目标责任和当前事实 |
| 接管外部持仓 | `ADOPT_EXTERNAL_POSITION` | CONTRACT | 极高 | LOCAL_USER | external-allocation grant、时段、同步、新鲜度、无活动/未知委托、evidence hash；批次歧义需审核证据 |

Capability 必须发布上述每一项。不存在的行必须 fail-closed 为 `CAPABILITY_UNAVAILABLE`；存在但暂不可执行的行使用具体业务 reason。接管能力只有在至少一条 `eligible` 或 `lot_review_available` 柜台差异存在时为 true，Owner 仍会重新生成快照并核对证据哈希。

## 明确排除的 Kernel 操作

以下操作虽然存在于通用 `OperatorCommand` schema，但当前生产 Qt Workbench 没有对应发送入口，因此不得因 schema 扩展自动进入本机白名单：

| 操作 | Workbench 决定 |
| --- | --- |
| `BEGIN_TAKEOVER` | 拒绝；属于显式接管/恢复协议，不是现有按钮 |
| `CONFIRM_TAKEOVER` | 拒绝；属于显式接管/恢复协议 |
| `ENTER_MAINTENANCE` | 拒绝；只由受控运维流程决定 |
| `CANCEL_ACCOUNT_CONFIRMED` | 拒绝；当前 UI 使用证据化的 `CANCEL_NATIVE_ACCOUNT` |
| `RESUME_ORIGINAL_EXITS` | 拒绝；恢复原退出责任需独立恢复流程 |
| `SCHEDULE_STRATEGY` | 拒绝；仅受托 operator/scheduler 合同 |

新增 schema enum、Kernel 命令或 Operator grant 不会扩大 `WORKBENCH_OPERATIONS`。新增 UI 操作必须先更新本 ADR、Capability 和操作矩阵，再改白名单。

## 失败与诊断合同

- 通道接受与拒绝均记录 operation、actor kind/id、账号身份摘要、boot、peer PID/SID 和结果码，不记录 token、口令或完整账户秘密；`LocalSystem` 可由 peer SID 复查。
- `E_AUTH` 表示通道/actor/operation 不匹配；`CAPABILITY_UNAVAILABLE` 表示 Runtime 没发布该操作；业务不可用必须返回具体原因。
- receipt 查询必须同时匹配 actor 兼容规则和操作白名单；知道 command ID 不构成读取权限。
- PR-C 前保留旧生产直连路径作为回退。新 CommandBus 未满足 pending、队列、身份、错误分类和关闭合同前不得切换生产 UI。

## Owning source

- 本机 endpoint、allowlist 和回执兼容：[operator_api.py](../../src/online_trader/runtime/operator_api.py)
- Kernel actor 写入和最终命令裁决：[control.py](../../src/online_trader/kernel/control.py)
- Capability 投影：[workbench.py](../../src/online_trader/kernel/workbench.py)
- 外部持仓接管证据和复核：[external_positions.py](../../src/online_trader/kernel/external_positions.py)
- Windows pipe SID/DACL：[named_pipe.py](../../src/online_trader/transport/named_pipe.py)
- Qt 实际发送入口：[workbench.py](../../src/online_trader/desktop/workbench.py) 与 [workbench_layout.py](../../src/online_trader/desktop/workbench_layout.py)

