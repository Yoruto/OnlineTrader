# Standalone Runtime Workbench 当前合同

> 状态：`CURRENT` · 2026-11-20。本文是 Standalone Trading Runtime 工作台的当前总览；详细权威分别由本文链接的单实例、CommandBus、手动启动及更新合同持有。历史设计和旧验收记录只作为证据，不得覆盖这些合同。

## 用户入口与可见结果

- 源码/开发入口：`online-trader-desktop --runtime-root <目录>`。
- 配置完整的便携版：双击 `OnlineTrader.exe`。
- 统一入口先发现并附着本机 Runtime；没有 Runtime 时，以 `DISARMED` 创建唯一 Owner，取得权威基线后打开同一个 `WorkbenchWindow` / `WorkbenchLayout`。
- 同一 Windows 主机最多一个 Runtime、一个附着的 Workbench。再次启动只唤醒已有工作台，不创建第二个控制面。
- `runtime run` 和 `--runtime-pipe` 仅是运维/高级入口。CLI、公开 API 和委托工具只读；没有无人值守调度或 CLI 交易入口。
- 缺少有效 Runtime 配置时明确失败，不回退 Legacy。默认选中 Standalone 不授权连接账号、交易、测试或打包。

## 进程、状态与写入权

| 事实或状态 | 权威/写入者 | Workbench 行为 | 重启与恢复 |
| --- | --- | --- | --- |
| 持仓、委托、成交、资金、收益 | 期货账户；Runtime 同步并裁决 | 只读投影 | 每次启动重新同步；其他电脑可独立使用同一账户 |
| SDK、订单、保护、交易决策 | 当前 Runtime Owner | 只经同用户命令通道请求 | 不转移给 Qt、runtime-view 或另一台电脑 |
| 当前启动执行事实 | Runtime 进程所有的临时 Kernel/SQLite store | 不直接读写 | 启动创建、结束删除，不跨启动交接 |
| 历史与收益缓存 | `account-history.db` 等可重建缓存 | 分页只读 | 可从柜台重建，不成为交易权威 |
| 页面草稿、选择、展示状态 | 当前 Workbench 进程 | 本地持有 | 可丢弃，不推断柜台事实 |
| 未决命令关联 | 当前 Workbench 的 `CommandBus` | 最多 64 条，超时查原 ID | 不写 QSettings/注册表，不跨进程恢复或重发 |
| 版本默认值与用户覆盖 | 配置文件的合并结果 | 展示、编辑并带 revision 提交 | 显式处理版本默认升级与用户覆盖冲突 |

旧 `runtime-control.json`、`private/operators.json`、watchdog、handoff 文件和历史执行账本均不是新启动依赖；历史账本只可只读审计，不能阻塞启动或承担恢复。

## 正常命令时序

```text
用户确认操作
  -> 页面/Controller 检查当前快照、Capability 和输入
  -> CommandBus 生成 command_id 并在进程内保留关联
  -> 同一 Windows 交互用户的专用本机通道
  -> Runtime 重新核对账号、owner boot、配置 revision、能力和风险
  -> Owner 执行或明确拒绝
  -> 回执按原 command_id 返回并更新页面
```

UI 不显示或读取操作员 ID/令牌。服务端只接受启动 Runtime 的交互用户 SID，拒绝其他 SID（包括 `LocalSystem`），并在脱敏审计中保留 actor、peer PID/SID、账号、boot、revision、operation、command ID 和结果。

超时、断线或未知结果不等于失败，也不允许自动重发。当前进程只查询原 `command_id`；WorkBench 或 Owner 重启后先取得新的 Runtime/柜台基线，不从旧本地 pending 或旧执行库猜测结果。

## 生命周期与故障流程

- **再次启动：**发现现有 Runtime 和 Workbench 后只唤醒原窗口；不得绕过单实例锁另建 Owner 或控制面。
- **Workbench 退出/崩溃：**点窗口 `X` 默认最小化到托盘，Owner 继续承担已确认订单与保护责任；当用户显式从托盘选「退出 OnlineTrader」（或关闭托盘驻留后点 `X`）时，经由受控安全停机核对（无持仓与活动委托自动停机；有持仓/在途委托需用户确认「全部平仓并停止」），确认目标 Owner 与 `runtime-view-process` 完全退出后才结束 Qt 进程；若停机失败或用户取消，保持工作台打开与原保护不变。Workbench 进程崩溃时，Owner 仍独立保持既有订单与保护责任。
- **runtime-view 失败：**按有界策略重启并重新获取完整基线；不得因此重启 Owner 或创建第二个 sender。
- **Workbench 投影过期：**客户端只显示“界面未更新/正在重连”并停用新的 UI 控制请求；不得把旧快照重新判定为“行情滞后”，也不得将 Qt/runtime-view 生命周期作为 Owner 开仓门槛。Owner 仍以原始行情到达、盘段、source epoch 和临发单校验独立决策。
- **Owner 行情滞后：**只有当前 Owner 投影明确给出 quote-age 失败或 `DSV_QUOTE_AGE_EXCEEDED` 时，Workbench 才显示“行情已滞后，暂停开新仓”；该业务拒绝与界面投影过期分别诊断。
- **Owner 退出：**本次临时执行库结束并删除。下次人工启动以新 boot、新临时库和柜台同步重建状态，不接管旧 boot。
- **另一台电脑启动：**只受该电脑的本机单实例约束，不使用账号级文件锁互斥；两台 Runtime 分别同步同一期货账户并标识各自 boot/设备事实。
- **配置或账号变化：**旧快照与命令能力失效；工作台取得新完整基线后才允许控制。
- **更新：**Workbench 可后台检查/下载，但不能停止策略、杀 Owner、撤单或平仓。安装切换只在用户确认且相关进程安全退出后执行，不扫描旧账本作交易裁决。

## 实际源码入口

- 桌面引导：`src/online_trader/desktop/entrypoint.py`、`desktop/runtime_bootstrap.py`、`runtime/desktop_bootstrap.py`。
- 工作台：`src/online_trader/desktop/workbench.py`、`workbench_layout.py`。
- 客户端状态与命令：`src/online_trader/desktop/runtime_client/`。
- Runtime Owner：`src/online_trader/runtime/standalone.py`、`launch.py`、`operator_api.py`。
- 状态/历史：`src/online_trader/runtime/view_process.py`、`account_history.py`、`account_cache.py`。
- IPC 合同：`src/online_trader/resources/runtime-ipc.schema.json`、`transport/named_pipe.py`。

改动前先用 [`architecture-status.yaml`](../ai/architecture-status.yaml) 按 `path + scope` 判断合同状态，再用 [`module-boundaries.yaml`](../ai/module-boundaries.yaml) 定位入口。源码与 `CURRENT` 合同冲突时报告 `CONFLICT / NEEDS_REVIEW`，不得静默沿用历史设计。

## 诊断与验收边界

关键日志必须能按账号（脱敏）、设备/Runtime boot、command ID、配置 revision 和外部调用阶段关联。至少区分：UI 触发、发送前拒绝、IPC 入队/拒绝、Owner 接收、SDK 调用前、SDK 调用结果未知、柜台回报、成交与保护责任。状态拒绝要记录具体失败谓词及输入依据；日志不得包含凭据或账户明文。

本候选当前只有源码编译检查记录。旧候选的 Qt/IPC、操作员协议或账本交接测试不能证明当前合同。经明确授权的用户级验收应从真实入口验证正常流程、重复启动、错误 SID、配置缺失、断线/未知、Workbench 退出、runtime-view 重启、Owner 新 boot 与柜台重同步；连接柜台、交易、功能测试、打包和制品验收仍分别需要本次授权。

## 关联权威

- [柜台权威、本机单实例与临时执行库](runtime-broker-authority-single-instance-20261120.md)
- [CommandBus 进程内关联合同](runtime-command-bus-contract-20261120.md)
- [手动启动与本机用户控制收口](runtime-manual-only-cleanup-20261120.md)
- [桌面生命周期用户流程](desktop-lifecycle-user-flow-20261120.md)
- [Runtime 更新接入](../windows-gitlab-updates.md#standalone-trading-runtime-更新接入)
- [测试入口迁移](standalone-trading-runtime-test-migration-20261119.md)

