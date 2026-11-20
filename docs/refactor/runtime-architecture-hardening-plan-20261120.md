# OnlineTrader Runtime / Workbench 架构收敛与安全改造计划

> 日期：2026-11-20  
> 设计基线：`90f8d0af1c493f9e8c902a2c8589b768149df4c9`（main）  
> 文档性质：**改造提案 / 待分批实施**，不是已完成声明。  
> 本 PR 只更新设计文档；**不修改交易代码、不连接账户、不执行报单、不启动 SimNow、不打包、不变更 CI**。  
> 关联：[Standalone Runtime 设计](standalone-trading-runtime-20261119.md) · [Workbench Client 重构](trading-workbench-client-refactor-20261119.md) · [Workbench 安全跟进](workbench-safety-followup-20261120.md) · [运行与持久化](../runtime-operations-and-storage-20261119.md)。

## 0. 决策摘要

OnlineTrader 继续采用**本机 Modular Monolith + 独立进程**，不引入微服务、消息中间件或新的数据库。Trading Runtime 是**唯一交易决策/SDK sender/业务状态权威**；Qt Workbench 可退出、重建、附着，不能直接修改交易事实。券商/交易所对原生委托、成交、账户持仓的事实拥有外部权威，Runtime 必须通过 Reconciliation 把外部事实与本地 Durable Evidence 对齐。

2026-11-20 的 `90f8d0a` 统一桌面启动并新增 Workbench 同 Windows SID 命名管道。这是正确的入口收敛，但**同用户 SID 认证不证明按键操作由人发起**；不能把“本机交互用户”表述成加密可信的“人工确认”或“仅 OnlineTrader.exe 可访问”。不同受托 CLI/Agent 操作继续使用 Operator 通道。新增统一启动和 Workbench 控制通道截至设计基线**未形成独立 Windows / Qt / Named Pipe / SimNow / 成品验收证据**。

执行顺序：**WP0 安全与验收门禁 → WP1 生产 Workbench 命令总线切换 → WP2 Bootstrap 异步状态机与进程生命周期 → WP3 权威状态/配置/投影收敛 → WP4 Ledger 边界和遗留目录治理**。WP0 未通过，不把本机命令通道作为正式发布的已验证交易控制能力。

### 不变式（各批次不可破坏）

1. **一账户、一个正在执行的 Owner、一个原执行账本 writer**。账号锁、账本身份和并行账本 handoff 拒绝不允许由 Desktop 绕过。
2. **commit-before-send**：已经送达/可能送达柜台的 effect 永不因为本地超时推断为失败；UNKNOWN/SEND_POSSIBLE 只有权威核对能解除。
3. **Command ID 不因超时改变**；禁止 UI 自动重发原先结果未知的交易动作；仅在协议明确承诺幂等且 Runtime 去重可验证时使用原 ID。
4. **Qt 关闭/崩溃/启动超时不终止交易责任**；新桌面启动只建立 DISARMED，不能绕过已有保护或自动启用新增开仓。
5. GUI 不直接连接 SDK / 写账本 / 编辑 effective config；不把 Capability 当授权凭证，Runtime 接受每个命令时仍重新核对。
6. 不删除 WAL/SHM、不复制热账本为可运行源、不重建缺失旧账本、不自动迁移/合并原交易事实。
7. 正式测试必须分别标注 `PASS/FAIL/SKIP/NOT_RUN`，不把模拟/Mock 测试宣称为真实 Windows Named Pipe、SimNow 或最终包验收。

## 1. 现状与差距（基线 90f8d0a）

| 领域 | 已有基础 | 需收敛的问题 |
| --- | --- | --- |
| 启动 | `desktop/runtime_bootstrap.py`：先探测，缺失时启动 DISARMED Owner，读启动日志/阶段 | `ensure_runtime()` 在 Qt 调用链中 `processEvents + sleep` 轮询；尚缺可取消/重入/并发/包内回归 |
| 权限 | `operator_api.WorkbenchEndpoint` 独立 `.workbench-control`；`named_pipe` 有当前 Windows SID ACL 与 peer SID/PID 检查 | `_operator_capabilities() - {'SCHEDULE_STRATEGY'}` 范围过宽，缺少每操作显式白名单与威胁模型；SID 不证明用户点击 |
| 命令 | `WorkbenchWindow` 用 `_RuntimeWorkbenchRequest`；`desktop/runtime_client/client.py` 有 `CommandBus`、队列和多 pending | 生产路径未接 CommandBus；旧 `runtime/pending_receipt` 单槽和新 `pending_receipts_v2` 双存储并行 |
| 回执 | 旧生产路径提交前 QSettings sync、按命令 ID fail-closed；新基础层可跨 boot 保存 pending | 新旧 pending 迁移和跨进程/跨窗口重复控制没有形成单一生产合同；读取失败、权限变化需独立测 |
| 状态 | Runtime 版本化 baseline / Cursor / Delta、Workbench 可增量消费 | 明确 State/Control/Market/Diagnostics 不同的丢弃和背压语义；避免 UI 独立推导业务事实 |
| 生命周期 | Trading Owner / View Process 分离，View 进程可恢复 | 区分服务状态、柜台同步状态和入场授权状态；启动过程不应阻塞 Qt 主线程 |
| 持久化 | 原 `ledger/store.py` 及 replacement candidate `dsv_storage/journal.py` 都有 WAL 和明确事务 | 两者属于不同执行路径，**不能直接认定同时写同一库**；需先做所有权/迁移矩阵再决定统一 |

## 2. 目标组件与依赖方向

```text
Desktop Entry / BootstrapController  (仅发现、启动、附着、提示)
                     |
WorkbenchWindow / Views / QAbstractTableModel
                     |
ClientStateStore -- CommandBus -- Runtime Query/Subscription Lanes
                     |
      Runtime IPC Boundary (view / control / workbench-control)
                     |
 Trading Runtime Owner [Strategy -> Risk -> OMS -> Gateway]
        |                         |
    one Ledger writer         CTP/TqSdk
        |
   durable facts + receipts + evidence

CLI / Scheduler / Agent --operator/token--> .control
```

- Desktop 不能成为 SDK sender，Bootstrap 不能隐式 ARM。
- Workbench 通道与 Operator 通道共享 Runtime 的最终 Command Handler 和提交互斥，而不是重复实现授权/核对/去重状态机。
- 业务事件由 Owner 串行裁决；昂贵编码、历史读、展示刷新走有界读侧队列；关键订单/成交事实不可按行情绘制规则丢弃。
- 不引入 Redis/Kafka/PostgreSQL 服务，继续保持本机可部署、可恢复与受控交接。

## 3. WP0 — 本机命令通道安全合同 + 新入口验证（P1，先实施）

### 3.1 安全模型需要明确产品决策

目前 Named Pipe SID/peer 校验表示“当前 Windows 登录身份允许连接”，**不能证明是 Qt 按钮点击**。同用户任意进程在符合协议时可能访问该通道；具有完全用户会话控制权限的恶意程序亦不能靠 GUI 会话令牌彻底防御。

决策建议：将 Workbench 接口明确标为 `LOCAL_USER` 授权，不表述为“经人工确认的请求”；受托 Agent、Dot、CLI、调度必须走 `DELEGATED_OPERATOR`，不得依赖 GUI 内部管道绕开 Operator grant。若产品要求区分可信桌面应用与同 SID 其他程序，单独评估进程身份/签名/部署保护与高风险操作二次确认的实际威胁缓解；**不宣称单纯 PID、可复制 token 或确认弹窗能解决本机已被攻陷的风险**。

### 3.2 接口约束

- Workbench 允许操作改为显式版本化 allowlist（例如用户明确的启停策略、撤单、人工平仓、参数应用等，**实际全集须逐项审查 schema 与业务影响**），不使用 `_operator_capabilities() - {'SCHEDULE_STRATEGY'}` 作为永久授权策略。
- 在日志/账本事实中区分 `actor_kind=LOCAL_USER`、`DELEGATED_OPERATOR`、`STRATEGY`，记录必要的 `account_key` / `command_id` / `boot_id` / `config_revision` / correlation，而非伪造“人工点击已证明”。
- 服务端仍逐命令校验操作白名单、账本权威状态、账户、boot、配置版本、入场权限、持仓/订单前置条件；预览不是保存授权。
- 旧 Workbench 的 `qt-*` 令牌命令回执兼容只能在原始账本证据明确、账号与 ID 关联可证时开放，**禁止单凭 qt- 前缀跨权限读取无关命令**；规定兼容期、迁移检测和退出条件。
- 明确 `E_AUTH`、`E_CAPABILITY`、`E_CONTEXT_EXPIRED`、`E_OWNERSHIP`、`E_TIMEOUT`、`E_STORAGE` 的客户端结果映射：不把传输失败推断成命令被拒绝；无法证明“未受理”时继续 UNKNOWN。

### 3.3 必测矩阵

| 案例 | 期望 |
| --- | --- |
| 当前 SID 的工作台使用合法 schema、合法能力 | 服务端复核，有限授权下执行，账本 actor 可追踪 |
| 其他 Windows SID、恶意/错误帧、错误账号/boot/config | 拒绝；无入账发送副作用 |
| 同 SID 的非 GUI 客户端 | 证明现行身份边界实际可访问性，按最终安全决策限制/记录；不得声称仅 GUI 可访问 |
| 禁止操作、不允许开仓、恢复未完成、旧快照 | 服务端明确拒绝，保护责任不受影响 |
| 提交超时、reply 丢失、接口进程强杀 | 原 ID 仍可查；不产生第二笔 |
| 无法读 ledger、回执未找到、错误账号、旧 actor 回执 | 不清 UNKNOWN；不能以“找不到”代替“没发过” |
| 并发 Workbench + CLI 同时请求 | 共用提交门禁，不冲突、不重复 |
| 关闭 Qt / 重启 View Process / Owner boot 切换 | 老命令身份保留，新启动仍遵守 DISARMED / reconcile |

产出：安全模型 ADR、白名单与来源映射、基于真实 Windows Named Pipe 的回归证据、危险操作的 SimNow **获授权** 验收计划。若当前环境无法执行 Windows/SimNow，记录 NOT_RUN，不伪报 PASS。

**WP0 Exit Gate：**授权模型有明确决定；白名单覆盖所有实际可调用操作；Windows 的 SID/peer/并发/异常回执用例通过；无人值守与 GUI 生命周期边界无回归。若未完成，不宣布本机新通道已通过生产验收。

## 4. WP1 — 生产 Workbench 切换单一 CommandBus（P1）

### 4.1 具体改造

- 在 `WorkbenchWindow` 统一接入 `RuntimeClientCoordinator`、`ClientStateStore`、`CommandBus`；保留原 `WorkbenchWindow/WorkbenchLayout` 视觉外壳，不创建第二套 UI。
- 将 `_RuntimeWorkbenchRequest` 的 submit/receipt/preview 路径逐步替换为 CommandBus，对旧刷新/查询接口进行单向退役；仅在过渡期保留兼容 adapter，不允许两条新命令链同时激活。
- 单一 Workbench 命令执行队列、受控取消与等待全部 worker 完成；UI 销毁前必须验证 QThread 已 finished，关闭 pipe 并中断阻塞 read。
- 保留每次明确用户操作生成的 command ID、提交前 durable pending、服务端回执与核对；重连与 boot 更换只查询原 ID。
- 任何命令回执，必须匹配 `account_key + command_id`；终态应以服务端可靠证据判断，绝不能因为 UI 异常/查询失败删除 pending。

### 4.2 Pending 迁移

一次性、幂等地把旧 `runtime/pending_receipt` 导入多命令持久化存储（仅本地 metadata cache，不变成交易权威）。先写入新记录并 `sync` 成功，核验后再安全清理旧槽；任一步异常应保留旧记录并阻止新提交。兼容其他账号 pending，不可覆盖。对保存结构加入 `schema_version`、账号身份、`command_id`、`operation`、`scope_hash`、`source`，不要保存秘密；不凭客户端本地记录推导命令已完成。对超过当前客户端容量的 pending **不得悄悄截断**，必须 fail-closed 或由 Runtime 可查询 registry 重建。

工作台每次附着从 Runtime 获取 unresolved/receipts 权威列表（需要先设计分页/水位/状态语义），本地 QSettings 只用于离线恢复原 ID 提示。无法读取权威 unresolved 时进入待核状态。

### 4.3 验收

- 生产 UI 真正调用 CommandBus；旧请求线程不再发起交易控制。
- 旧 v1 pending、多账号 pending、崩溃中途迁移、损坏配置、写入失败、不同 boot、receipt 不可用均 fail-closed。
- Submit 排队失败不会遗留误导性“已经提交”状态；请求确实可能送达时永不映射为无条件 REJECTED。
- 强杀 Qt，保留 UNKNOWN；重启后仅查原 ID，绝不发送新 ID 重试。
- 最终可删除旧 `runtime/pending_receipt` 运行读写链路（需要旧版本升级兼容验收后才删除）。

## 5. WP2 — Bootstrap 异步状态机与运行生命周期（P2）

当前 `desktop/runtime_bootstrap.py:ensure_runtime` 使用同步 `QApplication.processEvents() + time.sleep(.2)`；替换为异步状态机和独立 I/O Worker，GUI 线程不主动 sleep、不开嵌套事件循环。

状态：`DISCOVERING → ATTACHING → (LAUNCHING) → WAITING_BASELINE → READY / FAILED / CANCELLED_UI`；已有 Owner 的附着、无 Owner 时 DISARMED 启动、账户锁竞争后的再次附着为显式分支。

生命周期**拆分三个独立维度**：

1. Service: `STARTING/READY/DEGRADED/STOPPING/STOPPED`。
2. Broker/Reconciliation: `DISCONNECTED/CONNECTING/SYNCHRONIZING/RECONCILIATION_REQUIRED/SYNCHRONIZED`。
3. Entry Authority: `DISARMED/ARMED/PAUSED`（实际枚举需与现有 Runtime 原生值兼容）。

`DISARMED` 只禁止新增开仓，**不代表无柜台连接、不取消保护责任**；关闭 scheduler 总开关也不停止已有 Owner。

- 多进程同时双击先附着，启动竞争由原账户锁裁决，不创建第二个 sender。
- 启动失败携带稳定 error code、启动关联 ID、诊断路径；不会自动 copy / checkpoint / delete WAL。
- UI 等待超时或关闭不发送 stop、不 kill 已起 Owner；仅终止本地探测/控制 UI。
- `--runtime-pipe` 永远仅附着；`runtime run` / `runtime schedule` 保持运维入口。
- 保留当前包的相邻 `runtime/` 识别和用户已有 Legacy 显式路径，不静默切换账户。
- 退出、双击两次、Owner 慢启动、View 进程挂起、结构化诊断缺失、便携打包与升级更新全部列为回归案例。

**WP2 Exit Gate：** Qt UI 在探测期间仍可响应且无重入副作用；Owner 存活责任与 Windows 便携包独立验收通过。

## 6. WP3 — 权威状态、配置与投影合同（P2）

### 6.1 五种状态所有权

| 状态 | 权威 |
| --- | --- |
| Desired Config（用户希望如何运行） | 版本化配置文件，提交/生效必须由 Runtime 审批 |
| Effective Config（当前真实生效） | Runtime + Ledger durable applied revision |
| Execution history、pending、effects、audit | 当前账户原 Ledger |
| Native orders/trades/account positions | 券商柜台观察，需 Runtime 核对、记录覆盖率 |
| Client UI selection / layout / filters | Workbench preference，仅 presentation |

禁止在 Config/QSettings 写入被用于恢复交易的 `has_position`、原生成交结论、未知发送终态等事实。

### 6.2 ConfigSnapshot

配置加载 → schema 校验 → 内容 hash → Runtime preview → 预期 revision 的原子应用 → Ledger `CONFIG_APPLIED` / effective revision → 发布新快照。运行策略轮次只读取一个不可变 ConfigSnapshot，不在中途直接 `open(config.json)`；对日历、时钟、行情来源与合约元数据也要记录参与决策的版本/时间依据。

### 6.3 四条逻辑通道（可复用既有管道，无须立即拆四个 OS 进程）

- **Control**：命令 ID、提交 ACK、回执、可靠有序与失败可查询；不可丢。
- **State**：baseline + 增量 + boot/cursor/sequence，丢 gap 必须 resync，不允许 UI 猜订单终态。
- **Market**：报价展示可按合约合并为 latest wins，**不影响 Owner 消费的每个必要行情与交易门禁**。
- **Diagnostics**：有界队列、可降级/脱敏，日志写失败不反向阻塞交易 Owner。

采用显式队列容量和 overflow 语义；订单/成交/权限变化不能与高频行情同样无条件丢帧。延迟验收记录 Owner pump P50/P95/P99、业务下单关键路径与 UI 大帧编解码，参数预算为工程目标而非交易 SLA。

## 7. WP4 — Ledger/Journaling 与旧代码目录治理（P2/P3，后实施）

**不直接删除 `dsv_storage/journal.py`。** `ledger/store.py` 服务当前 Runtime 原执行账本；`dsv_storage/journal.py` 属于 replacement candidate 的事实存储。代码同时存在不等于相同账户两 writer 已发生。先创建路径矩阵：运行命令 → SDK owner → account lock → ledger location/schema → writer → handoff → recovery/test。对每一条生产、replacement、QA 路径明确数据不互通及准入条件。

在确认替代链转正、账本 schema、冷交接/增量迁移、旧版恢复兼容等全部验收后，再评估把数据库连接与事务/锁/备份等基础机制抽成单一 Infrastructure Ledger；**不要为了统一类名破坏现有 Journal 的 commit-before-send 语义**。先治理单账户唯一 writer 和数据库 lineage 检查，后治理目录命名。

模块切分依赖建议：`domain`（纯业务规则）、`application`（owner 编排/命令/reconcile）、`infrastructure`（TqSdk/CTP/SQLite/IPC）、`client`（Qt 与 projection）、`contracts`（schema/errors/version）。`kernel/engine.py`、`kernel/control.py`、`desktop/workbench.py` 和 `desktop/workbench_layout.py` 按行为迁移分步削薄，**不做一次性巨型移动/改名 PR**。

## 8. 推荐 PR 拆分与交付顺序

| PR | 内容 | 依赖 | 风险/回退 |
| --- | --- | --- | --- |
| PR-A | Workbench 安全模型 ADR、操作白名单、审计 actor、协议单测/Windows 实测 | 本文 | 仅协议与权限边界；无窗口重写 |
| PR-B | 生产 CommandBus 接入、旧 pending 的幂等迁移、旧命令线程退役 | PR-A | 保留非交易只读旧路径；命令 fail-closed |
| PR-C | BootstrapController 异步状态机、服务/柜台/授权三维状态 | PR-A | 回退到 `runtime run` + `--runtime-pipe`，绝不迁移账本 |
| PR-D | ConfigSnapshot/权威 State 与 channel/backpressure 规范化 | PR-B、PR-C | 版本兼容协议，客户端重新 baseline |
| PR-E | 存储所有权矩阵/账本 lineage doctor、遗留模块渐进治理 | PR-D | 先纯检测与文档，禁止自动修账本 |

每个 PR 单独包含 `Change Surface`、升级/回退、无交易副作用测试、真实环境 NOT_RUN 清单与准入门禁；不把多个 PR 的历史结果合并宣称最新候选通过。

## 9. 测试与交付报告模板

| 级别 | 必须验证 |
| --- | --- |
| Unit | 请求 schema、allowlist、错误映射、状态迁移、配置冲突、pending 迁移断点 |
| Integration | 同账号单 Owner、不同 SID、同 SID 非 GUI、双客户端竞争、View crash/重启、命令超时与原 ID 查询 |
| Windows Qt | 可见启动/关闭/强杀、GUI 不阻塞、线程退出、无第二个 Owner、管道重连 |
| SQLite | 读写竞争、冷热 WAL/SHM、writer 锁、原账本不变、备份/受控交接 |
| SimNow（必须另获批准） | 报单/撤单/平仓/部分成交/UNKNOWN/保护/无人值守恢复闭环 |
| Delivery | 干净 Windows 便携包双击入口，配置 + 原账本完整、SHA-256、升级与冷回退 |

最低报告字段：`git_sha`、OS/Python/PySide6/SQLite/TqSdk 版本、运行入口、`account_key` 脱敏身份、配置 hash、测试名称、PASS/FAIL/SKIP/NOT_RUN、日志和制品摘要、未测清单。无环境或权限时明确 NOT_RUN；**不得自行连接交易账户或构建最终包**。

## 10. Done 定义与明确暂缓项

完成不是文件移动完毕，而是：
- 生产交易界面只有一套命令编排，所有未知回执可查且跨 boot 可靠；
- UI/控制接口故障不改变已有订单、仓位和保护责任；
- 启动入口在干净 Windows 机器完成真实验收，普通用户不需手动启动两个进程；
- 每个业务事实有唯一权威，配置与数据库职责可从测试验证；
- 存储改造有受控冷交接、零未授权交易、可退回原版本的可执行方案；
- 发布说明不再把已合并代码等同已经验证的交易产品。

**暂缓**：无业务需求的微服务、Redis/Kafka、任意跨机器多 owner、全面 ECS 化、因“统一”而直接删除 replacement Journal、未有可复验前的自动热更新交易策略。自动更新不得在未知委托/保护责任期间未经安全门禁替换正在执行的 Owner。

## 11. 未决决策（实施 PR-A 前在 ADR 固化）

1. **本机权限模型**：是接受“同 SID 任意进程可访问 `.workbench-control`”的显式风险，还是设计应用绑定/确认/分权机制？默认建议先以最小显式白名单和可审计 actor 限权，未经威胁模型批准不把它叫“真实人工操作认证”。
2. **危险操作集合**：哪些必须额外确认，哪些允许本机通道执行，哪些只允许 Operator token？必须逐项审查当前 `OperatorCommand` 枚举。
3. **旧 `qt-*` 回执宽限**：由哪些原账本证据关联旧 operator actor，何时关闭兼容？
4. **权威 unresolved registry**：Runtime 查询字段、水位和跨 boot 语义，以及本地旧 pending 超容量时的失败策略。
5. **替代执行链归并时机**：是否先继续保持独立候选及独立账本 schema，还是在正式切换方案审查通过后实施持久化抽象共用？

> 此文档是一份分批改造路线图，所有“应”“验收”均为后续待完成项；不代表 `90f8d0a` 已通过新工作台协议或打包验收。
