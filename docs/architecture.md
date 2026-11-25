# 系统架构与模块边界

## 1. 整体分层

OnlineTrader 采用**执行与展示分离**的单机桌面架构（Standalone Trading Runtime）：

```text
用户 (Windows 桌面)
  │
  ├─► Trading Workbench (Qt / PySide6)       ──► 只读投影展示 + CommandBus 本机命令提交
  │        ▲                        │
  │        │ (Named Pipe / IPC)     ▼
  ├─► runtime-view-process                   ──► 只读视图投影服务（不持有交易权）
  │
  └─► Standalone Runtime Owner               ──► 唯一 SDK 连接与下单控制者（单线程主循环）
           ├── DsvSession / TargetExecution  ──► 策略评估、全账户目标收敛、保护状态机
           ├── ExplicitOrderController       ──► commit-before-send、子单生命周期、UNKNOWN 占量
           ├── 临时执行 Journal (SQLite)      ──► 仅当前 boot 存活，退出即删除
           ├── DsvSdkAdapter                 ──► 绑定 TqSdk，行情/持仓/委托事实接入
           └── BrokerHistoryWriter           ──► 写入可重建的 account-history.db 缓存
```

---

## 2. 四条执行路径

| 路径 | 入口与 Owner | 适用合同 |
| --- | --- | --- |
| **Standalone Trading Runtime（默认）** | `online-trader-desktop --runtime-root`、便携版 `OnlineTrader.exe`、`runtime run` | 本机单 Runtime + 临时执行库 + 期货账户权威 + 全账户收敛；默认 `DISARMED` 启动。 |
| **Legacy（仅显式维护）** | `simulation run`、`dsv run`、`simnow run` 及旧工作台启动链 | 保留原 Kernel Owner 与历史恢复合同，禁止作为默认启动或打包路径。 |
| **Replacement candidate** | `dsv replacement-run`、`simnow replacement-run` | `DsvSession` + `ExplicitOrderController` + `Journal` + `DsvSdkAdapter`，不与 Legacy 共用发送者。 |
| **Isolated QA** | `offline-dsv`、`qa/dsv_parity/`、`qa/test_runner.py` | 离线/本地受控回放与 `FakeBroker` 仿真，严禁接入真实账户或作为在线输入端口。 |

---

## 3. 模块边界导航

| 模块领域 | 核心源码入口 | 负责职责 (`owns`) | 禁止越权 (`must_not_own`) |
| --- | --- | --- | --- |
| **桌面客户端与引导** | [entrypoint.py](file:///e:/online-trader/src/online_trader/desktop/entrypoint.py)、[runtime_bootstrap.py](file:///e:/online-trader/src/online_trader/desktop/runtime_bootstrap.py)、[workbench.py](file:///e:/online-trader/src/online_trader/desktop/workbench.py)、[client.py](file:///e:/online-trader/src/online_trader/desktop/runtime_client/client.py) | Qt 界面展示与草稿、`CommandBus` 进程内命令关联、有界客户端 I/O | SDK 下单、柜台持仓/成交裁决、写执行账本、跨 boot 命令责任 |
| **Runtime 引导与生命周期** | [standalone.py](file:///e:/online-trader/src/online_trader/runtime/standalone.py)、[launch.py](file:///e:/online-trader/src/online_trader/runtime/launch.py)、[desktop_bootstrap.py](file:///e:/online-trader/src/online_trader/runtime/desktop_bootstrap.py)、[instance.py](file:///e:/online-trader/src/online_trader/runtime/instance.py)、[shutdown.py](file:///e:/online-trader/src/online_trader/runtime/shutdown.py) | 单机单实例锁（`RuntimeLock`）、当前 boot 临时 Journal 分配、版本固定选择单一 Owner、安全停机协调 | 除 Target Owner 外的第二个发单控制者、跨设备账号互斥锁、旧账本交接 |
| **Standalone Target Owner** | [standalone_owner.py](file:///e:/online-trader/src/online_trader/dsv_runtime/standalone_owner.py)、[owner_binding.py](file:///e:/online-trader/src/online_trader/dsv_runtime/owner_binding.py)、[standalone_commands.py](file:///e:/online-trader/src/online_trader/dsv_runtime/standalone_commands.py)、[standalone_projection.py](file:///e:/online-trader/src/online_trader/dsv_runtime/standalone_projection.py)、[server.py](file:///e:/online-trader/src/online_trader/kernel/server.py) | 当前 boot 唯一目标执行与 SDK 发送控制、IPC 命令裁决与投影适配、安全平仓停机证据 | 用户可选的第二套执行核心、与 Legacy Kernel 并行发单 |
| **DSV 策略与订单执行内核** | [runner.py](file:///e:/online-trader/src/online_trader/dsv_runtime/runner.py)、[controller.py](file:///e:/online-trader/src/online_trader/dsv_runtime/controller.py)、[target_execution.py](file:///e:/online-trader/src/online_trader/dsv_runtime/target_execution.py)、[sdk_adapter.py](file:///e:/online-trader/src/online_trader/dsv_runtime/sdk_adapter.py)、[journal.py](file:///e:/online-trader/src/online_trader/dsv_storage/journal.py)、`src/online_trader/dsv_core/` | 策略信号、全账户收敛计划、子单发送前持久化、`UNKNOWN` 占量、`dsv_core` 纯逻辑计算 | `dsv_core` 严禁依赖 SDK、Qt、I/O、系统时钟及 Legacy Owner |
| **只读视图与历史缓存** | [view_process.py](file:///e:/online-trader/src/online_trader/runtime/view_process.py)、[account_cache.py](file:///e:/online-trader/src/online_trader/runtime/account_cache.py) | `runtime-view` 投影转发、`account-history.db` 柜台可重建历史与收益缓存 | 订单发送、持仓权威裁决 |
| **受管更新与发行** | [updates.py](file:///e:/online-trader/src/online_trader/updates.py)、[update_install.py](file:///e:/online-trader/src/online_trader/update_install.py)、[update_ui.py](file:///e:/online-trader/src/online_trader/desktop/update_ui.py) | GitLab/极狐版本发现与校验下载、非独立更新载荷校验、静默期 `versions/` 原子切换 | 交易权与下单控制、修改或迁移根目录 `runtime/` 配置与账本 |
| **离线 QA 与回放** | `qa/dsv_parity/`、[test_runner.py](file:///e:/online-trader/qa/test_runner.py)、[fake_broker.py](file:///e:/online-trader/qa/target_owner/fake_broker.py)、[offline_endpoint.py](file:///e:/online-trader/src/online_trader/dsv_runtime/offline_endpoint.py) | 冻结 fixture 回放、QA 专用 Owner 子进程与虚拟时钟仿真 | 接入生产在线 SDK 模式、替代实盘/实包验收 |
