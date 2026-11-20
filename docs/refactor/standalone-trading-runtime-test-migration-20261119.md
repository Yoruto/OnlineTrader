# Trading Runtime 测试入口迁移与固定数据

> 2026-11-20 更新：Runtime Workbench 已改为“界面按钮即当前 Windows 交互用户操作”的独立本机通道，不再要求操作员 ID或令牌。下文 2026-11-19 的操作员界面用例和结果只保留为历史证据，不能验收新通道；CLI、调度和受托工具的操作员协议不变。

2026-11-19 · 当前任务：后续用户级产品测试统一从 Trading Runtime 公共入口执行。

## 决定

旧工作台或 QA 进程直接持有 `Kernel`、直接建立 `ControlServer` 的方式不再作为当前产品验收入口。它们可以继续验证尚未退役的 Legacy 路径或 Kernel、策略、订单、账本不变量，但不能证明 Trading Runtime、Runtime Workbench、进程隔离或操作员接口通过。

Runtime 产品测试从以下入口执行：

- Qt：`online-trader-desktop --runtime-pipe <pipe>`。
- 状态：`RuntimeViewClient`、`runtime status` 和 `runtime watch`。
- 控制：工作台经 `ONLINE_TRADER_RUNTIME_WORKBENCH_COMMAND/1` 本机同用户通道提交；CLI、调度和受托工具经 `RuntimeCommandClient` 与 `ONLINE_TRADER_RUNTIME_COMMAND/1` 鉴权后提交。
- 账户与历史：Runtime account、history 和 report 公共入口。

纯离线不能冒充真实 `runtime run`。生产入口固定连接所选 SDK，不增加测试事实注入参数。离线事实只进入 `qa/` 中不随 wheel 发布的宿主；测试者仍只从 Runtime 公共客户端和 Qt 操作。

## Runtime Workbench 控制合同

Runtime Workbench 可以直接提交命令。用户点击按钮并完成既有界面确认即构成交互用户操作，Qt 不再输入或保存操作员 ID和令牌；接口以本机同一 Windows 用户边界受理，并继续核对账号、能力、owner boot 和配置版本。账号或 owner boot 改变时重新取得完整基线后才能提交。

提交体绑定当前 `account_key`、`kernel_boot_id` 和 `config_revision`。一个用户确认生成一个 `command_id`。连接超时或回复丢失时保留该 ID，只允许查询原回执，禁止自动重发。工作台只持久化账号、命令 ID、操作类型和来源等非秘密待核信息。

## 固定测试数据

机器可读边界位于 `qa/fixtures/runtime/runtime-boundaries.json`。离线宿主位于 `qa/runtime_fixture.py`，使用非白银合约、固定柜台日、虚拟时钟和原生形状事实，不连接账户。

覆盖的数据类包括：

- 操作员：完整能力、仅平仓、缺开仓、过期和错账号。
- 业务状态：空仓、活动开仓单、5手目标部分成交3手、持有5手、UNKNOWN send及外部仓。
- 视图：基线、连续差量、重复、乱序、缺失、generation切换和摘要冲突。
- 命令：同 ID同正文、同 ID不同正文、旧 boot、旧配置和错账号。
- 故障：提交前断线、提交后回复丢失、接口进程退出、慢订阅、畸形帧、历史读锁、账本只读和 UNKNOWN send。

## 覆盖清单

独立测试按 RT-01 至 RT-23 检查 Runtime 基线、Qt附着、操作员鉴权、直接提交、人工开平、撤单、停开、停止策略、确认期间版本变化、重复点击、幂等、超时回执恢复、Qt/ViewProcess故障、差量补齐、慢客户端、单 owner、账户截面、历史水位、诊断反查和文档一致性。

实际柜台受理、网络时序、真实成交、真实保护触发、TqKq、真实资金账户和最终制品不由离线替身证明，保持 `NOT_RUN`，需分别授权。

## 入口退役表

| 入口 | 后续身份 |
| --- | --- |
| `qa/workbench_rehearsal.py` | Legacy 组件回归；不再作为当前产品验收 |
| 原 `tests/test_workbench_processes.py` | Legacy 维护覆盖；当前产品进程测试由 `tests/test_runtime_processes.py` 承担 |
| 原先借用 `OnlineDsvTests` 的 `qa/runtime_replay.py` | 已改为使用独立 QA-only Runtime fixture，并只从 Runtime 公共客户端断言 |
| `tests/test_kernel.py` 等直接 Kernel 测试 | 保留为组件不变量，不升级成产品验收 |
| DSV parity/replacement 回放 | 保持自己的参考和候选链范围 |

## 执行与证据

开发完成后开发方只运行编译检查。经本次授权，未参与实现的独立测试 Agent执行 Runtime 定向测试和离线回放，报告完整候选 SHA、Windows/Python/PySide版本、fixture与脚本 SHA-256、Runtime/ViewProcess/owner身份及 `PASS/FAIL/SKIP/NOT_RUN`。

不联网，不打包。旧历史证据保留原版本和原入口说明，不改写为新 Runtime 证据。

## 2026-11-19 独立离线结果

独立测试 Agent `/root/runtime_independent_test` 在 Windows 11 `10.0.26200`、Python `3.13.15`、PySide6 `6.8.2.1` 上执行当前 Runtime 定向自动化。测试候选工作树 SHA-256 为 `b76780695081a8f23d37b5694c836111af20ab9aae27a21d945e102bb9d12a71`；固定边界数据 SHA-256 为 `548b40f0a3f3c60dbfec0d8ee9ced23f0ef932f2d088c2434a51dcd98bca15c9`。

已执行的新 Runtime 模块 13 项和受影响既有回归 56 项，共 69 PASS、0 FAIL、0 SKIP。离线回放取得 `READY` 附着账户截面，人工平仓回执为 `SUCCEEDED`，相同命令 ID同正文返回原回执，不同正文返回 `E_DUPLICATE_CONFLICT`。回放结果 SHA-256 为 `cf10393edd57e2c90cc83be782bcf46a0c66cc505700953ac9684c85e0e5b765`。

以下仍为 `NOT_RUN`：真实可见桌面完整点击、Qt/runtime-view 强杀恢复、慢订阅和畸形帧、界面开仓/撤单/停止策略完整矩阵、UNKNOWN/外部仓/只读账本故障矩阵、真实 `runtime run` 联网、柜台订单成交保护、ZIP/安装器和最终制品。因此结论仅为“已实现的离线 Runtime 定向自动化与受影响回归通过”，不是完整 Runtime 产品或发行验收。
