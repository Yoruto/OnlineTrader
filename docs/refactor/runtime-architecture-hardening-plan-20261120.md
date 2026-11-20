# OnlineTrader Runtime / Workbench 架构收敛与安全改造计划

> **2026-11-20 权威更新：** 本文保留架构评审和旧候选实施历史，不再单独作为当前 Standalone 持久化、恢复或 Workbench pending 的实施依据。当前合同以[本机单实例与柜台权威设计](runtime-broker-authority-single-instance-20261120.md)和[CommandBus 进程内关联合同](runtime-command-bus-contract-20261120.md)为准；本文中每账户长期账本、跨设备 handoff 和 durable pending 的旧结论均已替代。具体作用域状态见[AI 文档状态清单](../ai/architecture-status.yaml)。

> 日期：2026-11-20  
> 运行源码事实基线：`90f8d0af1c493f9e8c902a2c8589b768149df4c9`（main）
>
> 原计划合并提交：`9817f11a4b03203753df30134d5a3d807ab24f07`（PR #21，仅文档）
>
> 文档性质：**已评审设计 + 当前工作区源码实施记录**，不是测试或发布完成声明。
>
> 当前工作区已实施 PR-A 至 PR-F 的当前源码范围，并补齐多工作台 pending 串行化、peer 审计、Bootstrap 用户取消和完整 Capability 控件绑定；**未连接账户、不执行报单、不启动 SimNow、不运行功能测试、不打包、不变更 CI**，只执行源码编译检查。
>
> 关联：[Standalone Runtime 设计](standalone-trading-runtime-20261119.md) · [Workbench Client 重构](trading-workbench-client-refactor-20261119.md) · [Workbench 安全跟进](workbench-safety-followup-20261120.md) · [运行与持久化](../runtime-operations-and-storage-20261119.md)。

## 0. 修订结论

PR #21 的总体方向保留，但原文不能直接作为实施依据。当前代码事实要求先完成架构审计和安全合同，再修复非生产 CommandBus 原型，之后才允许生产 Workbench 切换。现有 Ledger、`config_revisions`、`authority` 和 Profile Hash 已经具备 ConfigSnapshot 的核心机制，后续应规范和补缺，不重新建立平行的配置权威。

OnlineTrader 继续采用**本机 Modular Monolith + 独立进程**，不引入微服务、消息中间件或新的数据库。Trading Runtime 是唯一交易决策、SDK sender 和本地业务事实裁决者；Qt Workbench 可退出、重建、附着，不能直接修改交易事实。券商/交易所对原生委托、成交和账户持仓具有外部权威，Runtime 通过 Reconciliation 将外部事实与本地 Durable Evidence 对齐。

实施顺序调整为：

1. **PR-A：架构事实审计与安全合同**；
2. **PR-B：CommandBus 基础层安全整改，暂不切换生产**；
3. **PR-C：生产 Workbench 接管与旧命令链退役**；
4. **PR-D：Bootstrap 异步化和运行生命周期优化**；
5. **PR-E：现有配置、权威状态与投影合同规范化**；
6. **PR-F：遗留模块与持久化抽象渐进收敛**。

所有架构重构都以三项最高约束为准：**不改变现有 DSV 策略行为、不扩大交易权限、不削弱恢复能力**。

## 1. 不变式

1. **一账户、一个正在执行的 Owner、一个原执行账本 writer**。Desktop、View Process、Operator 和 Replacement Candidate 都不能绕过账号锁、账本身份或 handoff 拒绝。
2. **commit-before-send**：已经送达或可能送达柜台的 effect 不因本地超时推断为失败；`UNKNOWN` / `SEND_POSSIBLE` 只能由权威核对解除。
3. **Command ID 不因超时改变**。UI 不自动重发结果未知的交易动作；只有协议明确承诺幂等且 Runtime 去重可验证时才复用原 ID 和原正文。
4. Qt 关闭、崩溃或等待超时不终止交易责任；新桌面启动保持 `DISARMED`，不能绕过已有保护或自动启用新增开仓。
5. GUI 不直接连接 SDK、写账本或编辑 effective config。Capability 是说明性事实，不是授权凭证；Runtime 仍逐命令复核。
6. 不删除 WAL/SHM、不复制热账本为可运行源、不重建缺失旧账本、不自动迁移或合并原交易事实。
7. 新增 Kernel 操作默认**不能**通过 Workbench 通道调用；必须经过显式白名单、风险分级和协议审查。
8. 任何测试、账号连接和打包都需要当前任务明确授权。报告分别标明 `PASS/FAIL/SKIP/NOT_RUN`，不把 Mock、源码检查、实际 Windows 管道、SimNow 或最终制品混写。

## 2. 实施前已核实事实与差距（运行源码基线 `90f8d0a`）

| 领域 | 当前事实 | 必须处理的差距 |
| --- | --- | --- |
| 统一启动 | `desktop/runtime_bootstrap.py:ensure_runtime()` 先探测，缺失时启动 `DISARMED` Owner，并读取结构化阶段和失败记录 | Qt 调用链中使用 `QApplication.processEvents() + time.sleep(.2)` 同步轮询；存在重入、不可取消和生命周期边界未验证问题 |
| Workbench 身份边界 | Named Pipe DACL 和服务端 peer 检查允许当前 Owner SID 与 `LocalSystem`；不能证明连接来自 Qt 按钮或真实人工点击 | 文档必须准确说明边界；是否接受同 SID 任意进程调用高风险操作需要 ADR 决定 |
| Workbench 操作权限 | `WorkbenchEndpoint` 使用 `_operator_capabilities() - {'SCHEDULE_STRATEGY'}` | 这是开放式集合；未来新增 Kernel 命令可能自动进入 Workbench。必须改为显式、版本化、默认拒绝的 allowlist |
| 生产命令路径 | `WorkbenchWindow` 仍使用 `_RuntimeWorkbenchRequest` 和单槽 `runtime/pending_receipt` | 旧路径只能作为迁移前基线，不应长期保留第二套交易命令编排 |
| CommandBus 原型 | `runtime_client/client.py` 已有队列、多 pending、boot 变化和回执恢复骨架 | 当前会静默截断最后 64 条、未 `QSettings.sync()`、损坏记录静默忽略、enqueue 失败可能遗留假 pending、回执未先严格匹配原账号和 command ID；**不具备生产切换条件** |
| Capability 投影 | `ClientStateStore.capability()` 对缺失操作正确 fail-closed；Runtime 已发布部分说明性 Capability | Capability 集合未覆盖现有生产 Workbench 的全部业务动作。先切 CommandBus 会造成按钮被 `CAPABILITY_UNAVAILABLE` 拒绝 |
| 配置权威 | Ledger 已有 append-only `config_revisions`、`authority.config_revision`、Profile Hash；运行中保存参数先提交 Ledger，再替换内存 Profile/Strategy；重启从原账本恢复有效 Profile | 需要正式定义并补齐现有 ConfigSnapshot 合同，而不是增加平行 `CONFIG_APPLIED` 权威或第二套存储 |
| 状态事实 | `broker_connected`、`synchronized`、`recovery_required`、`storage_failed`、`clock_fault` 等可以同时成立；Entry Authority 数据库枚举是 `ARMED/DISARMED` | 不应压缩成会丢事实的单线性 Broker 状态，也不应为了架构整齐新增 `PAUSED` |
| 持久化路径 | 原 `ledger/store.py` 与 replacement `dsv_storage/journal.py` 属于不同执行路径 | 在任何抽象归并前，先完成 Legacy、Standalone、Replacement、QA 的 Owner/锁/账本/writer/恢复矩阵 |
| 验收状态 | 统一启动与新 Workbench 通道仍缺当前候选的完整 Windows/Qt/Named Pipe/SimNow/制品证据 | 历史 69 项或其他候选结果不能移植为本计划的通过结论 |

### 2.1 当前工作区实施状态

| 阶段 | 源码状态 | 本次证据 |
| --- | --- | --- |
| PR-A | 已完成 E0 矩阵、安全 ADR、14 项封闭 allowlist、Capability 补齐、actor 兼容及 peer SID/PID 脱敏审计 | owning source 走读；源码编译通过；行为测试 `NOT_RUN` |
| PR-B | 已完成 pending v2 schema、跨进程锁内合并、sync/readback、容量 fail-closed、enqueue 未送达、回执身份和失败分类 | 源码编译通过；持久化/多工作台/队列/管道测试 `NOT_RUN` |
| PR-C | Standalone Workbench 已由 `RuntimeClientCoordinator + ClientStateStore + CommandBus` 单向接管；旧 one-shot sender 删除；旧单槽一次性迁移；全部交易控件消费 Runtime Capability | 源码编译通过；Qt/Named Pipe/强杀恢复 `NOT_RUN` |
| PR-D | 主线程同步轮询已替换为 `BootstrapController + BootstrapWorker` 状态机，进度框可取消本地等待且不终止 Owner | 源码编译通过；慢启动/双击竞争/取消 `NOT_RUN` |
| PR-E | 已发布 Ledger 派生的 `ONLINE_TRADER_CONFIG_SNAPSHOT/1`，配置完整性失败时阻止新增风险；逻辑通道合同固化 | 源码编译通过；配置恢复/运行参数保存 `NOT_RUN` |
| PR-F | ConfigSnapshot 与 Capability 投影从大 Workbench 模块拆出；确认原 Ledger 与 Replacement Journal 不合并、不删 | 源码编译通过；存储迁移不适用且未执行 |

上述“已完成”只指当前工作区源码实现，不代表源码合并门禁、真实柜台门禁或发布门禁通过。

## 3. 目标组件与依赖方向

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
- Workbench 与 Operator 通道可以复用接口进程和提交互斥，但最终业务授权、上下文校验、去重和提交仍由原 Runtime Owner 裁决。
- 业务事件由 Owner 串行裁决；历史读、展示刷新和昂贵编码走有界读侧队列。订单、成交、权限和恢复事实不能按行情绘制规则丢弃。
- 不引入 Redis、Kafka 或 PostgreSQL 服务，保持本机部署、原账本恢复和受控交接。

## 4. PR-A — 架构事实审计与安全合同（P1，首先实施）

### 4.1 E0 执行路径与 Ledger Lineage 矩阵

源码核对后的完整矩阵、owning source 和不变量见 [Runtime 执行路径与 Ledger Lineage 事实矩阵](runtime-execution-ledger-lineage-20261120.md)。E0 只建立只读事实基线；自动修库、合并账本或目录搬迁不在 PR-A 范围。

### 4.2 本机身份与威胁模型

当前管道服务端实际允许 Owner SID 和 `LocalSystem`。`LocalSystem` 本身已具备高本机权限，这不自动构成新的权限提升漏洞，但“仅当前登录用户可以访问”不是精确描述。

Workbench 接口定义为本机身份边界下的 `LOCAL_USER` 通道：

- SID/peer/PID 检查证明连接进程身份，不证明真实用户点击；
- 同 SID 的其他进程在理解协议时可能连接；
- `LocalSystem` 属于更高权限的受信操作系统边界，需在威胁模型和日志中明确；
- CLI、调度和受托 Agent 使用 `DELEGATED_OPERATOR`，不得借 Workbench 管道绕过 grant；
- 策略自动动作记录为 `STRATEGY`；
- 不宣称 PID、可复制 token 或确认弹窗能抵御已经控制本机用户会话的恶意程序。

若产品不能接受同 SID 进程调用高风险操作，需另行评审应用签名、部署保护、分权或服务端二次授权；未经 ADR 决定，不把 Workbench 通道称为“真实人工操作认证”。

### 4.3 UI 操作、Capability 与权限矩阵

14 项实际生产按钮、最终封闭 allowlist、明确排除的通用 Kernel 操作、Capability 语义和 actor 历史兼容已固化在 [Runtime Workbench 本机命令安全合同](runtime-workbench-security-contract-20261120.md)。

约束：

- Workbench allowlist 是显式、版本化的封闭集合；新增 Kernel 操作默认拒绝。
- Capability 投影必须覆盖允许的 UI 操作，并区分 `allowed=false` 的业务原因与“未发布该操作”。
- Capability 只解释当前状态；服务端仍复核账号、boot、配置版本、权限、持仓、订单和行情前置条件。
- 迁移前后每个生产按钮保持相同或更明确的权限语义。新增禁用必须说明是经批准的安全收紧，还是缺字段造成的功能回退。
- `workbench-user` 旧事件当前以 `kind=OPERATOR` 记录；引入 `LOCAL_USER` 时需要事件 schema/读取兼容，不能只改字符串并导致旧回执不可查。

### 4.4 PR-A Exit Gate

- [x] E0 路径矩阵完成并指向 owning source；
- [x] 本机 SID、`LocalSystem`、同 SID 非 GUI、Delegated Operator 的边界经 ADR 固化；
- [x] 所有生产 UI 动作进入操作矩阵；
- [x] 最终 Workbench allowlist 明确，未来操作默认拒绝；
- [x] `ADOPT_EXTERNAL_POSITION` Capability 缺口和 actor 历史兼容方案完成；
- 已授权的检查逐项记录，未获授权或缺环境的项目为 `NOT_RUN`。

未满足上述门禁前，不宣布新本机命令通道已经通过生产验收，也不切换生产 CommandBus。

## 5. PR-B — CommandBus 基础层安全整改（P1，暂不切换生产）

实施前 CommandBus 是非生产原型。PR-B 先修复基础合同并建立可验收边界，PR-C 再单向切换生产 Workbench，避免同时承担两类风险。

具体状态、数据、时序、失败分类、关闭和回退合同见 [Runtime CommandBus 持久化与失败合同](runtime-command-bus-contract-20261120.md)。

### 5.1 Pending 持久化合同

- 每条记录包含 `schema_version`、`account_key`、`command_id`、`operation`、`scope_hash`、`source` 和必要的本地恢复状态，不保存秘密。
- 写入必须 `QSettings.sync()` 并检查状态；持久化失败时禁止发送。
- `--allow-multi` 下所有进程必须使用同一跨进程锁，在锁内重新读取、合并并写回；不得用进程启动时的整表快照覆盖其他工作台新增的 command ID。
- 不得以 `[-64:]` 等方式静默截断未知责任。超容量时 fail-closed，或由已设计的 Runtime unresolved registry 分页重建。
- 损坏、未知 schema、跨账号冲突和部分写入不能静默忽略；保留原证据并阻止产生新命令。
- 本地 pending 只保存“需要查询的原 ID”，不推导已发送、成交或终态。

### 5.2 提交、队列和回执合同

- pending durable 成功后才能进入队列；enqueue 失败必须进入可证明的“未送达本地 worker”状态，不能留下虚假 `SUBMITTING`，也不能误删另一命令。
- 发送可能发生后的断线、超时或内部异常保留原 ID，并映射为 `SUBMISSION_UNKNOWN/OUTCOME_UNKNOWN`。
- 回执先匹配原请求的 `account_key + command_id`，再更新状态；不能使用当前窗口账号替代请求时账号。
- `E_AUTH`、`E_CAPABILITY`、`E_CONTEXT_EXPIRED`、`E_OWNERSHIP`、`E_TIMEOUT`、`E_STORAGE` 分别定义提交前拒绝、可能送达和查询失败语义。
- 只有服务端可靠终态 `SUCCEEDED/FAILED/REJECTED/SUPERSEDED` 能清理对应 pending；查询失败或“未找到”不能证明原命令未发送。
- Owner boot 改变保留全部旧 ID，只把状态降级为待核；重新取得基线后查询原 ID。

### 5.3 PR-B Exit Gate

- 基础层自身通过本次授权范围内的持久化、损坏、容量、队列、身份、boot 和关闭合同验收；
- 没有生产 Workbench 切换、账号连接或订单操作；
- 当前未执行的 Windows/Qt/真实管道场景如实记为 `NOT_RUN`；
- 不以旧 25 项或其他候选结果自动证明本次版本通过。

## 6. PR-C — 生产 Workbench 接管与旧命令链退役（P1）

PR-C 依赖 PR-A 和 PR-B 全部源码门禁。实施前先补齐 Runtime Capability 与最终 allowlist，再迁移 UI。

- 在原 `WorkbenchWindow/WorkbenchLayout` 接入 `RuntimeClientCoordinator`、`ClientStateStore` 和修复后的 `CommandBus`，不创建第二套 UI。
- `_RuntimeWorkbenchRequest` 的 submit/receipt/preview 单向退役；过渡 adapter 只能有一个激活的交易命令出口。
- 一次性、幂等地把旧 `runtime/pending_receipt` 导入新多命令存储。先写新记录、同步并核验，再清理旧槽；任何异常保留旧记录并阻止新提交。
- 多账号 pending 互不覆盖；旧版本升级、崩溃中途迁移、损坏设置、写入失败、不同 boot 和 receipt 不可用均 fail-closed。
- 顶层按钮、人工交易页和在线参数保存控件都消费同一 Capability；状态流过期时除原 ID 回执查询外不得保留可发送控件。
- 所有实际按钮通过 PR-A 矩阵逐项做迁移前后对照；`CAPABILITY_UNAVAILABLE` 不能作为未审查功能回退的掩盖。
- UI 销毁前请求 worker 中断、关闭活动 pipe 并等待全部 QThread 结束；Qt 退出不停止 Owner。

**PR-C Exit Gate：**生产 UI 只有一个命令编排；旧线程不再发起交易控制；强杀 Qt 后 UNKNOWN 和原 ID 可恢复；所有按钮权限语义逐项核对；旧槽只在升级兼容得到当前授权验收后退役。

## 7. PR-D — Bootstrap 异步状态机与运行生命周期（P2）

已将原 `ensure_runtime()` 的同步 `processEvents + sleep` 替换为异步 BootstrapController 和独立 I/O Worker；流程图、时序图、线程/进程所有权、取消语义、错误合同和回退入口见下述合同。

已固化的状态、线程/进程所有权、正常/故障时序、诊断和回退合同见 [Runtime Desktop Bootstrap 与生命周期合同](runtime-bootstrap-lifecycle-contract-20261120.md)。

Bootstrap 状态：

`DISCOVERING → ATTACHING → (LAUNCHING) → WAITING_BASELINE → READY / FAILED / CANCELLED_UI`

显式分支包括：已有 Owner 附着、无 Owner 时 `DISARMED` 启动、账户锁竞争后重新附着、Owner 慢启动、View Process 暂不可用、UI 关闭。

运行状态不压成一个万能枚举，保持正交事实：

- Service 生命周期可使用 `STARTING/READY/DEGRADED/STOPPING/STOPPED` 摘要；
- Broker/Reconciliation 保留 `broker_connected`、`synchronized`、`recovery_required`、`fault`、`storage_failed`、`clock_fault` 等可并存事实，再派生 UI 健康摘要；
- Entry Authority 沿用原生 `ARMED/DISARMED`；暂停某来源或品种由 Entry Policy 表达，不新增 `PAUSED` 数据库枚举。

其他约束：

- 多进程同时双击先附着，启动竞争由原账户锁裁决，不产生第二个 sender；
- UI 超时或关闭只终止本地等待，不发送 stop、不 kill 已起 Owner；
- 启动进度框提供“取消等待”，取消后等待 worker 安全退出并明确提示已经启动的 Owner 保持独立运行；
- 启动失败提供稳定 error code、correlation、阶段和诊断路径，不 copy/checkpoint/delete WAL；
- `--runtime-pipe` 只附着，`runtime run` / `runtime schedule` 保持运维入口；
- 相邻 `runtime/` 识别和用户明确的 Legacy 路径不被静默改写。

**源码合并门禁：**异步状态、重入、取消、锁竞争和 Owner 存续合同在当前授权范围内得到证据。

**发布/制品门禁：**只有另获打包和目标环境授权后，才执行 Windows 便携包双击、升级和冷回退验收；它不是每个源码 PR 自动获准的动作。

## 8. PR-E — 现有配置、权威状态与投影合同规范化（P2）

### 8.1 状态所有权

ConfigSnapshot 字段、生效/恢复边界、正交状态与当前四类逻辑通道的规范见 [Runtime ConfigSnapshot 与逻辑通道合同](runtime-config-snapshot-contract-20261120.md)。

| 状态 | 权威 |
| --- | --- |
| Desired Config | 版本化便携配置文件；描述未来意图，生效仍需 Runtime 审批 |
| Effective Config | 原 Ledger 的 `authority.config_revision` + `config_revisions` + Profile Hash |
| Execution history、commands、pending、effects、audit | 当前账户原 Ledger |
| Native orders/trades/account positions | 券商柜台观察，由 Runtime 核对并记录覆盖率 |
| Client selection/layout/filters/drafts | Workbench preference，仅 presentation |

Config/QSettings 不保存被用于恢复交易的 `has_position`、原生成交结论或未知发送终态。

### 8.2 规范现有 ConfigSnapshot

当前正确链路是：严格 Profile 校验 → 新 `config_revision` / hash → Ledger 事务更新配置与 authority → 提交完成 → 替换内存 Profile/Strategy → 发布新快照；重启从 authority 指向的原 Ledger 配置恢复并校验 hash。

PR-E 应把这条链路正式定义为 ConfigSnapshot 合同，并补齐：

- 配置版本、Profile Hash、rule set、有效时点和账号/boot 绑定；
- Runtime preview 与提交时 expected revision 一致性；
- 启动恢复、旧仓按原配置保护和新参数生效边界；
- 日历、行情来源、时钟依据和合约元数据的决策版本记录。

除非发现现有事件无法表达且有迁移方案，不新增平行 `CONFIG_APPLIED` 权威，不把 portable 文件重新定义为已经执行的交易事实。

### 8.3 逻辑通道与背压

- **Control**：命令 ID、提交 ACK、回执，可靠、有序、失败可查询，不可丢；
- **State**：baseline + delta + boot/cursor/sequence，出现 gap 必须 resync；
- **Market**：仅展示报价可按合约 latest-wins，不影响 Owner 的每个必要行情和交易门禁；
- **Diagnostics**：有界、可降级、脱敏，日志写失败不反向阻塞 Owner。

先测量现有 Owner pump、帧大小、队列水位和 UI 延迟，再决定是否拆分协议或进程。不要为架构对称而增加新的服务或线程。

## 9. PR-F — 遗留模块与持久化抽象渐进收敛（P3，最后实施）

不直接删除 `dsv_storage/journal.py`。`ledger/store.py` 服务当前原执行账本，`dsv_storage/journal.py` 属于 replacement candidate；代码同时存在不等于同一账户已经出现两个 writer。

只有在 E0 矩阵、正式切换决策、账本 schema、冷交接、增量迁移和旧版恢复兼容全部有证据后，才评估共用数据库连接、事务、锁或备份基础设施。不得为了统一类名破坏任一路径的 commit-before-send 语义。

模块依赖可逐步朝 `domain`、`application`、`infrastructure`、`client`、`contracts` 收敛，但 `kernel/engine.py`、`kernel/control.py`、`desktop/workbench.py`、`desktop/workbench_layout.py` 不做一次性巨型移动或改名。每次拆分必须对应实际所有权问题、可诊断性缺口或可验证的用户流程收益。

## 10. 推荐 PR 拆分与依赖

| PR | 内容 | 依赖 | 回退/边界 |
| --- | --- | --- | --- |
| PR-A | E0 路径/账本矩阵、安全 ADR、操作权限矩阵、allowlist 与 Capability 缺口 | 本文 | 先事实与合同；不切生产命令链 |
| PR-B | CommandBus pending、队列、身份、错误映射、关闭与恢复整改 | PR-A | 不接生产 UI；失败时旧生产路径保持不变 |
| PR-C | Capability 补齐、pending 迁移、生产 Workbench 接管、旧命令链退役 | PR-A、PR-B | 单向迁移；任何不明责任 fail-closed |
| PR-D | BootstrapController、异步附着/启动、生命周期与诊断 | PR-A | 回退到 `runtime run` + `--runtime-pipe`；不迁移账本 |
| PR-E | 现有 ConfigSnapshot、正交状态事实、逻辑通道和背压规范 | PR-C、PR-D | 复用原 Ledger/authority；不建平行权威 |
| PR-F | 遗留目录、Kernel/Workbench 削薄、持久化基础设施评估 | PR-E + 切换决定 | 渐进拆分；不自动合库或删 Journal |

每个 PR 编码前补齐 `Change Surface`、正常/故障时序、状态和数据合同、兼容迁移、回退、日志/调试点及可执行验收条件。PR-A 是路线总合同，不替代 PR-B 至 PR-F 各自的详细设计评审。

## 11. 测试、合并与发布门禁

本文定义应验证的范围，不授予测试、账号连接或打包权限。

| 门禁 | 目标 | 要求 |
| --- | --- | --- |
| 源码合并门禁 | 证明本 PR 满足其行为合同 | 仅按当次明确授权执行测试；记录候选 SHA、环境、入口、PASS/FAIL/SKIP/NOT_RUN、脱敏证据和缺口 |
| 发布/制品门禁 | 证明最终文件可由接收方运行 | 另获打包、目标 Windows、配置、账本和必要 SimNow 授权；独立全局测试后构建，再从最终制品独立验收 |

建议覆盖范围在获准后按 PR 选择，不得通过综合入口附带执行：

- Unit：schema、allowlist、Capability、错误映射、pending 断点、状态派生、配置冲突；
- Integration：单 Owner、同/异 SID、`LocalSystem` 边界、Workbench + CLI 并发、超时原 ID、View crash/boot 切换；
- Windows Qt：可见启动/关闭/强杀、线程退出、管道重连、按钮权限迁移前后对照；
- SQLite：冷热 WAL/SHM、writer 锁、原账本不变、受控 handoff；
- SimNow：必须另获批准后覆盖报单、撤单、平仓、部分成交、UNKNOWN、保护和恢复；
- Delivery：必须明确要求打包后，从最终 ZIP/安装器的 `OnlineTrader.exe` 入口核对配置、原账本和 SHA-256。

Mock、Fake Runtime、源码自动化、真实 Named Pipe、SimNow 和最终制品是不同证据层级。环境或权限不足时写 `NOT_RUN`，不能以历史总数补齐。

## 12. 完成定义与未决决策

### 完成定义

- 生产交易界面只有一套命令编排，未知回执跨 boot 可查询且不自动重发；
- Workbench allowlist 封闭、Capability 完整、所有按钮迁移无未批准回退；
- UI、接口和启动故障不改变订单、持仓和保护责任；
- 现有 ConfigSnapshot 权威可从配置、Ledger、内存和 UI 逐层验证；
- 各执行路径的 Owner、锁、writer、handoff 和恢复归属可追踪；
- 源码合并与最终发布证据分开，不把已合并代码等同已验收产品。

### PR-A 已固化及后续必须固化的决策

1. **已固化：**接受当前同 SID 本机进程边界并显式记录 `LocalSystem`；详见安全合同 ADR。
2. **已固化：**14 项现有生产动作允许 `LOCAL_USER`，其余 schema 操作默认拒绝；受托入口继续使用独立 grant。
3. **已固化：**新写入区分 `LOCAL_USER` / `DELEGATED_OPERATOR`，读取兼容旧 `OPERATOR`；宽限退出由 PR-C 的 pending 迁移证据决定。
4. **已固化：**pending v2 注册表最多 64 条，不截断；损坏、冲突、未知 schema 和超容量全部 fail-closed，跨 boot 只恢复为待查询原 ID。
5. **已固化：**连接、同步、恢复、存储、时钟和 Entry Authority 保持正交事实，UI 只派生可行动摘要，不新增数据库万能状态。
6. **已固化当前边界：**Replacement Candidate 尚不具备生产切换审查证据；继续保持独立候选和独立 Journal schema，未来切换需另行决定和授权。

**暂缓：**无业务需求的微服务、Redis/Kafka、跨机器多 Owner、全面 ECS 化、为统一而删除 Replacement Journal、没有可复验证据的自动热更新，以及任何未经门禁在未知委托或保护责任期间替换 Owner 的机制。

> 本文是已合入 PR #21 的后续修订与源码实施依据。当前工作区已完成所列源码改造和编译检查；所有功能/集成/Windows Qt/Named Pipe/SimNow/最终制品验收仍为 `NOT_RUN`，不代表 `90f8d0a`、`9817f11` 或当前未提交工作区已通过发布门禁。
