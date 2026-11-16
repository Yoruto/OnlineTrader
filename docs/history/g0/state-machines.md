# G0 状态机与事件处理规格

规格版本：`OT-G0-SM-1`。日期：2026-11-09（Asia/Shanghai）。本文件定义工程状态合同，可以据此实现 reducer、事务编排与故障用例；不构成 B01–B20 的业务批准，不表示运行验收通过。

范围、主键、序列化与事务分别与 [G0 入口](README.md)、[数据模型](data-model.md)、[IPC](ipc-protocol.md)、[保护计划](protection-plan.md)共同约束。业务条件以[待确认业务规则](business-decisions.md)关联的批准记录为准。规则未知不得取参考配置作生产默认。

## 1. 通用处理合同

### 1.1 所有权与状态向量

execution-kernel 的单一业务所有者负责本文件全部权威状态。strategy-worker、GUI、通知与守护提交请求/事实，不直接改状态。连接适配器提供来源证据，不自行开放交易权。

账户状态是以下正交维度的向量，禁止实现成一个 `running` 或 `connected` 布尔值：

| 维度 | 核心状态 | 权威范围 |
| --- | --- | --- |
| 内核生命周期 | STARTING / RECONCILING / READY / DEGRADED / MAINTENANCE / STOPPED | `account_key + kernel_boot_id` |
| 连接 | DISCONNECTED / CONNECTING / AUTHENTICATING / AUTHENTICATED / RECOVERING / AUTH_FAILED | 行情、交易分别保存，绑定 `gateway_identity + source_epoch` |
| 四类事实完整性 | UNREQUESTED / REQUESTED / IN_PROGRESS / COMPLETE / INVALIDATED / FAILED / UNSUPPORTED | 每个 reconciliation run 的资金、持仓、委托、成交分别保存 |
| 新仓权限 | DISARMED / ARMED，另有多项阻挡原因 | 指定账户、规则、参数及当前授权代次 |
| 持仓周期 | ENTRY_PENDING / HOLDING / EXIT_PENDING / FLAT_PENDING_CONFIRM / CLOSED | `position_cycle_id` |
| 发送尝试 | PREPARED / SEND_POSSIBLE / CALL_RETURNED / DEFINITELY_NOT_SENT / UNKNOWN / CONFIRMED | `attempt_id`，与柜台订单生命周期分离 |
| 柜台订单 | NOT_SEEN / WORKING / PARTIALLY_FILLED / FILLED / CANCELLED / REJECTED | `child_order_id + broker_order_key` |
| 撤单动作 | REQUESTED / SEND_POSSIBLE / CALL_RETURNED / CONFIRMED / REJECTED / UNKNOWN / NOT_NEEDED | 独立 cancel action；无记录表示尚无撤单动作，不能覆盖原单 |
| 保护模式 | NORMAL / PROTECTED；质量及提交状态独立 | `plan_id + plan_revision + position_cycle_id` |
| 策略实例 | SHADOW / ACTIVE / DRAINING / REVOKED | `strategy_epoch` |
| 接管 | TAKEOVER_PENDING / TAKEN_OVER / CANCELLED / FAILED | 无记录表示尚无接管；接管范围及接管事务身份 |
| 维护 | REQUESTED / QUIESCING / SAFE_TO_STOP / STOP_COMMITTED / UPDATING / STARTUP_RECONCILE / COMPLETE / FAILED | 无记录表示尚无维护；维护 ID、锁及更新事务 |

名称表示权威业务事实；例如 PROTECTED 与 DATA_GAP 可以同时成立，不能把缺数据当作解除保护。READY 不代表 ARMED，AUTHENTICATED 不代表查询 COMPLETE，CANCELLED 不代表从未成交。

### 1.2 统一字段、序号与期限

跨文档固定字段：`account_key`、`gateway_identity`、`source_epoch`、`kernel_boot_id`、`kernel_epoch`、`strategy_epoch`、`ingress_seq`、`event_seq`、`durable_seq`、`state_revision`、`risk_revision`、`position_cycle_id`、`plan_id`、`plan_revision`、`rule_set_id`、`config_revision`、`intent_id`、`child_order_id`、`attempt_id`、`command_id`。

所有 seq、epoch、revision 在 IPC 中使用非负十进制整数字符串，SQL 使用有符号 64 位 INTEGER 且禁止负值/溢出。数量是非负整数，价格、金额与比例是精确 Decimal 字符串，不能经二进制浮点后再用于阈值、预占或幂等比较。

入口分队列之前分配 `ingress_seq`；它仅表示本机接收顺序，不能证明交易所逐笔顺序。业务所有者为一次业务提交的 journal 包络分配 `event_seq`，包内可含多个事实，各事实仍保留独立身份、`ingress_seq` 和来源顺序，不因共用提交序号便视为同时。事务提交后才能推进连续已提交的 `durable_seq`。来源消息保留原始来源序号，缺失或不保证连续时显式记录能力限制。

`context_id` 由内核签发并保存在本次启动内存中，绑定账户、当前 boot/授权代次、相关 revision、事实屏障、允许动作及内核单调时钟截止。提案或用户命令只能引用，不能用发送者的 UTC 时间、TTL 或重试刷新延长。重启后一律失效；UTC 仅用于审计与时段语义，执行期限以本进程单调时钟判断。某条不相关行情更新不必使全部提案失效；准入时检查声明的相关 revision，发送前再查当前有效价格与风险。

### 1.3 转换原子性及 CommitAck

每个事件处理步骤必须输出 `next_state / transaction / effects / receipt`，事件处理本身不得直接绕过发送状态机调用 SDK。事务名称是本文件语义标识，实际表映射见数据模型：

| 语义事务 | 一起提交的事实 |
| --- | --- |
| TX_AUTH | 权限/禁开变化、依据、修订号、命令结果 |
| TX_INTENT | 机会消费、意图、完整计划、预算/可平预占、子单及可能发送标记 |
| TX_FILL | 成交去重、来源映射、订单累计、周期数量、费用/未知字段及预占调整 |
| TX_PROTECT | 最高有利观察、保护锁存、阶段消费、禁开原因及共享退出目标 |
| TX_RECONCILE | 查询范围/完成证据、差异、来源屏障、核对决定 |
| TX_SWITCH | 新唯一策略代次、屏障、快照/hash、旧权限撤销 |
| TX_TAKEOVER | 接管范围、旧目标冻结、授权变更、责任确认与命令结果 |
| TX_MAINTENANCE | 维护屏障、相关权限撤销、安全事实、持久停止政策 |

提交请求按业务序号有序进入唯一写入者。待提交预占立即计入内存 pending 占用；后续提案不得再次使用。普通投影必须同时标明待提交量与 durable 边界，不能把内存成功显示为持久成功。

CommitAck 是内核写入者到业务所有者的内部消息，不是可由 GUI、策略或外部管道提交的消息类型。它必须匹配事务 ID、业务序号、`kernel_boot_id`、期望 revision 和实际提交结果。乱序 Ack 只能缓存等待连续边界，不得倒退状态或越过前面未完成事务。重复同内容 Ack 幂等；同事务不同结果视为存储协议故障。过期 boot 或已撤销流程的 Ack 只归档，不能重新授予发送/恢复权。

提交失败立即阻断新增风险与依赖该事务的派生发送，保护故障单独告警。不能把所有预占清零；区分可证明未发送的事务与可能已经到达 SDK 的尝试，UNKNOWN 继续占用。恢复不得通过回滚内存快照撤销已提交成交或已锁存保护。

控制优先级用于响应速度，不取代因果屏障：暂停/撤权可以立即先收紧内存权限，随后提交 TX_AUTH；平仓数量、接管完成、解除禁开、核对完成与维护安全判断必须追平所需事实屏障。查询快照不能覆盖查询屏障之后的增量事实，保护事件缺口必须被显式标记。

## 2. 生命周期与连接状态

| 转换 ID | 原态 → 目标 | 事件与 guard | 持久化与 effects | 失败/迟到规则 |
| --- | --- | --- | --- | --- |
| K01 | STOPPED/无进程 → STARTING | 获得覆盖跨 Windows 会话的账户排他权；版本可读，创建新 boot/epoch | 先核对维护记录/未完更新；加载账本；默认 DISARMED | 不能取得锁、schema不兼容或账本损坏则拒绝连接交易，不杀占锁进程 |
| K02 | STARTING → RECONCILING | 账本及原计划可加载；明确账户/环境/通道 | 记录启动与恢复依据；仅建立获批连接、请求四类事实 | 禁止从默认本地撮合或旧配置自动选择账户 |
| K03 | RECONCILING → READY | 当前来源四类事实完整；增量追平；差异已解释；保护覆盖和元数据满足目标能力 | TX_RECONCILE，发布 readiness 明细 | 不自动 ARMED；未具备某路径只允许对应受限能力 |
| K04 | READY → DEGRADED | 来源断开、关键缺口、存储/队列故障、保护异常或未知发送 | 立即收紧新仓；记录故障/禁开；保留原事实和已有计划 | 完全断网不可写成“已强平”；保护不能可信时显示接管需要 |
| K05 | DEGRADED → RECONCILING | 故障条件恢复且单一恢复调度取得本轮执行权 | 新 run/source 记录，重取需重建的事实和路径 | “网络可达/登录成功”不能直接 READY；同一恢复请求合并，不并发重连 |
| K06 | READY/DEGRADED → MAINTENANCE | 已接受维护请求且维护权限有效 | 建立持久维护屏障；禁开；继续处理订单/保护直到满足停止条件 | 不能为了完成维护任务自动平仓，除非另有明确有效退出命令 |
| K07 | MAINTENANCE → STOPPED | M04 的停止条件满足并完成持久记录 | 确认 SDK/写入者/工作者有序结束后返回停止结果 | 未确认进程停止只显示 STOP_COMMITTED，不伪造 STOPPED |

STOPPED 是最后一次可证明的有序退出结果；崩溃、断电或被杀不会凭空产生该结果。下次启动记录非正常结束，并从 K01 开始。守护观察到进程不存在也不能把前次交易自动结案。

连接状态分别用于行情和交易：DISCONNECTED → CONNECTING → AUTHENTICATING → AUTHENTICATED；可重试瞬时故障进入 RECOVERING 后按受限退避重连；明确凭据/权限错误进入 AUTH_FAILED，等待配置更正。重连创建新 `source_epoch`；旧来源回报若可证明归属仍入账，但不能满足新来源的查询屏障或授予新权限。维护窗口只能根据有来源的状态判定，普通超时不归因为维护。

## 3. 事实完整性与权限状态

### 3.1 核对 run

一个 run 对 `funds / positions / orders / trades` 四类事实分别记录范围、请求、开始/结束证据、来源、查询屏障和增量覆盖。适配器无完整性证明能力时状态停留 IN_PROGRESS/FAILED，不因超时将缓存视作 COMPLETE。

| 转换 ID | 转换 | guard 与结果 |
| --- | --- | --- |
| Q01 | UNREQUESTED → REQUESTED → IN_PROGRESS | 当前账户、gateway、source 与请求身份匹配；记录请求和数据范围 |
| Q02 | IN_PROGRESS → COMPLETE | 适配器支持的完成证据已到；范围正确，增量连续归并到目标屏障；事务成功 |
| Q03 | REQUESTED/IN_PROGRESS → FAILED | 拒绝、期限到、数据不完整或范围不匹配；已收到事实保全，不编造空结果 |
| Q04 | COMPLETE → INVALIDATED | 断线换来源、查询期间缺口、冲突、跨日能力不满足或发现关联的新事实 | 
| Q05 | 四类 COMPLETE → 一致结论 | 总量/归属/资金与未知事实可解释，保护覆盖审查完成；TX_RECONCILE 固定证据边界 |
| Q06 | 无法提供完整性能力 → UNSUPPORTED | 记录具体缺失范围/证据能力；不授予COMPLETE，不以缓存稳定或登录成功代替；调整通道或限制依赖能力 |

“一致”只对该 run 与已追平边界有效；下一条外部事实可能产生新差异。不存在持仓相等即永久完整的状态。新来源核对不丢弃旧已确认成交，旧范围不能满足新范围。

### 3.2 新开仓授权

权限实体按账户只存在一个有效自动策略执行者。`DISARMED → ARMED` 必须由显式命令或已批准且有效的自动恢复政策触发，并同时满足：当前 boot/epoch、规则批准、参数生效、事实完整性、保护能力、行情/日历/合约元数据、预算与许可等所需条件。

ARMED是带范围的账户增仓许可，grant_policy的allowed_origins必须显式列MANUAL、STRATEGY或两者。仅人工授权可以没有ACTIVE策略；自动提案额外要求当前ACTIVE strategy_epoch。手动命令也不能绕过账户禁开、预算、保护计划和事实门禁；不把启动自动策略作为手动交易前提，也不把人工面板当成全局暂停的旁路。暂停作用于所有新增还是仅自动的最终政策仍须在B14范围内明确批准。

| 转换 ID | 事件 | 行为 |
| --- | --- | --- |
| A01 | ArmRequested | 校验 `context_id`、相关revision、角色与全体阻挡；TX_AUTH 成功后才发布 ARMED |
| A02 | PauseNew/权限失效/策略失联 | 立即拒绝新增意图并持久撤权；按批准范围处理自身开仓残单；原保护继续 |
| A03 | AddBlock(reason, scope, release_condition) | 以原因身份去重插入；多个原因并存；任何一个阻挡所需能力都不得增仓 |
| A04 | ReleaseBlock(reason, evidence) | 只解除该原因；记录证据，不删除 UNKNOWN、旧日持仓或其他原因 |
| A05 | 自动恢复请求 | 完整核对及有效恢复政策通过后执行 A01；旧包、旧会话或旧许可不作为新授权 |

同一分钟机会、延续开一次性、止损同价、冷却及日/月计数是独立业务资格，按 B07/B08/B09/B12/B13/B20 的批准结果登记；更换策略代次不重置。日切不直接清空账本、预占、锁存或未决订单。

## 4. 意图、发送与柜台订单

### 4.1 开仓意图准入与发出

| 转换 ID | 事件/阶段 | guard | 事务与 effect |
| --- | --- | --- | --- |
| E01 | DecisionProposal/人工开仓命令到达 | 当前有效context、机会身份不重复、来源/规则/计划可用、数量合法、最终风险可承受 | 计算单一候选变更；pending 预占立即冻结；TX_INTENT 登记全部持久身份及机会消费 |
| E02 | TX_INTENT 提交成功 | Ack 匹配；权限仍有效，相关 revision/当前合法报价/风险复核通过 | 仅一次把对应 attempt 交唯一适配器；事务已记录 SEND_POSSIBLE |
| E03 | 提交后、调用前禁止条件到达 | 可证明还没有交 SDK/下游队列；临发 guard 失效 | 登记 DEFINITELY_NOT_SENT 与原因，按确定范围释放；不返还已消费业务机会，除非批准规则明确允许 |
| E04 | SDK 调用/排队已开始 | 只允许 E02 对应尝试；适配器记录实际推进阶段 | API 正常返回进入 CALL_RETURNED，只是提交/排队证据；等待接受/拒绝/事实回报，不显示成交 |
| E05 | 调用结果未知/崩溃恢复 | SEND_POSSIBLE 但不能确定柜台结果 | UNKNOWN，保留数量/资金占用，发起核对；相同业务请求返回同身份，不换ID补发 |
| E06 | 柜台接受/拒绝或可证明关联事实到达 | 账户/来源/柜台身份可验证；重复/冲突分类 | attempt 归并 CONFIRMED；订单进入已观察状态；拒单与其计数按 B13 登记 |

发送前必须提交 SEND_POSSIBLE，因为在数据库提交与柜台接受之间不存在跨系统原子事务。崩溃发生在标记后、调用前也可能进入 UNKNOWN；只有明确的未调用证据才能 DEFINITELY_NOT_SENT。不能承诺端到端 exactly-once，不用本地超时推导未发送。

同一 intent 的改价是新 child/attempt，继承原机会、周期、计划和预算；UNKNOWN 原尝试未解除前不能复制其潜在数量。价格/手数合法化不能绕过批准的追价、滑点、预算或报单限制。

### 4.2 回报归并

订单状态只能从事实归并，不能由按钮或发送返回推导：NOT_SEEN → WORKING/PARTIALLY_FILLED/FILLED/CANCELLED/REJECTED；WORKING → PARTIALLY_FILLED 或已证实终态；PARTIALLY_FILLED → FILLED/CANCELLED。允许接受先收到成交、再收到接受回报的顺序，禁止晚到旧 WORKING 覆盖终态。

每条成交先按通道证明的 trade_key 去重；重复同内容无二次记账，同身份不同内容保留冲突、禁开并核对，不覆盖原事实。TX_FILL 同步更新已确认量、分配及预占；总成交不超过原单 quantity 的约束遇到冲突时隔离，不截断真实事实来“修好”约束。

柜台累计成交数与已收成交明细数分开保存；二者不一致时保存未覆盖数量和证据缺口，不制造合成成交。潜在敞口继续占用，保护覆盖的不确定部分明确告警。重启核对需要补齐明细/权威解释后才结案。

FILLED/CANCELLED 等终态回报可能先于明细到达，不能因此提前释放潜在成交量。迟到新成交仍入账；已证明重复只幂等返回。终态之间冲突、拒单后成交或超量事实进入核对，不用状态优先级简单丢弃。

### 4.3 撤单子状态

| 转换 ID | 撤单状态 | 业务含义 |
| --- | --- | --- |
| C01 | 无动作记录 → REQUESTED → SEND_POSSIBLE | 记录操作身份和原单；持久可能发送标记后才调用；仍保留原单潜在成交量 |
| C02 | SEND_POSSIBLE → CALL_RETURNED | 只证明 SDK 调用返回或排队，不证明撤成；服务端接收撤单请求的中间回报另存原始事实 |
| C03 | REQUESTED/SEND_POSSIBLE/CALL_RETURNED → REJECTED | 原单不变；可能已成交、不可撤或权限错误，按分类核对 |
| C04 | SEND_POSSIBLE/CALL_RETURNED → UNKNOWN | 未知结果保留；不能作为新平单发送依据 |
| C05 | SEND_POSSIBLE/CALL_RETURNED/UNKNOWN → CONFIRMED | 原单撤销终态、累计量/成交明细及所需核对证据完整；归并成交后才释放未成交剩余量，按最新目标计算下一子单 |
| C06 | REQUESTED/未决动作 → NOT_NEEDED | 可证明原单已完全成交/终结且无需取消，或尚未发出动作被有依据取消；不代表未知请求从未发送 |

减风险超时只触发检查/撤改流程，不直接重报原数量。B06/B13/B15 决定期限、升级和报单额度；没有批准的策略不得进入无限重试。

## 5. 持仓周期与共享退出目标

| 转换 ID | 转换 | guard、事务与 side effects |
| --- | --- | --- |
| P01 | 无周期 → ENTRY_PENDING | E01：意图/计划/预占和周期身份已提交；理论价与规则/参数冻结 |
| P02 | ENTRY_PENDING → HOLDING | 首次可证明开仓成交；TX_FILL 绑定原计划并管理实际数量，不等满仓 |
| P03 | ENTRY_PENDING/HOLDING → EXIT_PENDING | 保护/止盈/止损/时间/人工/批准应急要求减少；TX_PROTECT 收紧目标并记录全部原因，按批准范围撤残余开仓单 |
| P04 | EXIT_PENDING → EXIT_PENDING | 晚到开仓/部分退出/外部减仓；TX_FILL 后重新计算剩余目标，不另发全仓；人工减仓不补回 |
| P05 | 任意未结周期 → FLAT_PENDING_CONFIRM | 已观察管理数量为0；仍继续核对相关开平单、UNKNOWN、分配与外部差异；禁止提前封存 |
| P06 | FLAT_PENDING_CONFIRM → CLOSED | 当前事实屏障完整；相关单全部可证明终态，UNKNOWN为0，数量/归属一致；提交封存与结案证据 |
| P07 | FLAT_PENDING_CONFIRM → EXIT_PENDING/HOLDING | 原开仓残单晚到成交；原身份原计划继续管理，权限仍受原禁开约束 |

零成交周期同样只有在全部相关尝试确证未发或相关订单终态且核对完整后才能 CLOSED；记录 `NO_FILL` 结案原因，不删除原意图或返还机会。

若 CLOSED 后到达此前未知的新成交，保留原封存事件并追加纠正事件，置账户核对限制，周期显式重新进入 EXIT_PENDING/HOLDING 并提高 revision。可证明原归属时沿用原计划；归属/数量不可靠时进入接管。不得覆盖旧封存记录，也不能只因状态为 CLOSED 就丢弃真实成交；这种重新打开表示原结案证据失效，必须告警。

一个周期只有一份 `target_remaining_qty`。普通退出、保护、半仓、收盘、人工和应急给出候选目标，批准的优先级确定最终目标；同时保存全部原因。正常收紧目标不提高，晚到开仓可能提高“当前持仓量”，不能提高既定退出目标。有效退出单、撤单期间潜在成交、已确认退出量均纳入目标差额，实际可发量不得小于0或超过可靠可平量。

阶段目标/保护线按 B14/B17/B18 批准，不能因同K线机会已消费跳过保护观察。计划移除、白名单禁用、规则换版或软件许可失效均不能删除既有周期保护。

## 6. 保护模式与计划修订

保护模式 NORMAL → PROTECTED 的 guard 是获批计划的瞬时激活条件；TX_PROTECT 原子写入最高有利观察、锁存、适用禁开和退出目标。锁存依据包含观察事件/质量、计划版本与规则批准。回落、GUI退出、策略重启、策略租约过期与代码回退均不能 PROTECTED → NORMAL。

`quality` 单独为 CONTIGUOUS / DATA_GAP / UNKNOWN，`persistence` 单独为 PENDING / DURABLE / FAILED。CONTIGUOUS 只证明适配器能力声明范围内的本地事件覆盖连续，不证明上游逐Tick完整；来源或完整性不可判为UNKNOWN，已知缺口为DATA_GAP。内存已观察的更强保护不能被较早 Ack 或旧价覆盖；未持久化窗口在恢复时明确为缺口，不能声称从未漏掉瞬时触发。缺口处置/强平属于 B14/B15 的批准范围。

处于保护态时停用哪些普通出口、半仓/移动的优先级及三种普通退出依据保护计划规格和 B14/B18；本文件只固定单所有者与目标协调，不在未批状态自行改变交易策略。保护执行器自身或 OMS 不可信时，不能用“内核仍活着”宣称保护正常。

阶段状态与保护模式也正交，字段固定为 `phase1_status / phase2_status = NOT_TRIGGERED / WAITING_ENTRY_TERMINAL / TARGET_COMMITTED / EXECUTING / DONE / NO_OP / SUPPRESSED_PROTECTED`，`break_even_status = OFF / WAITING_FILL / ACTIVE / SUPPRESSED_PROTECTED`。批准的半仓触发先进入 WAITING_ENTRY_TERMINAL；残余开仓终态且核对前 `q0` 必须为 NULL，不用当前临时数量伪装已冻结基准。基准与阶段目标在事务成功后进入 TARGET_COMMITTED，再按统一退出量进入 EXECUTING；实际成交和目标确认后 DONE，无需减量时 NO_OP 且不报零手单。NORMAL 下首阶段实际减仓满足获批条件后才可使保本 WAITING_FILL → ACTIVE；请求已发或 NO_OP 均不等于实际减仓。保护激活时尚未消费的阶段与保本按批准政策进入 SUPPRESSED_PROTECTED；已在途单继续占量与入账，不能删除。具体阈值、Q0取值、取整和阶段优先级只在保护计划及B18批准后生效。

计划修订必须匹配旧 `plan_revision`、当前周期与持仓 revision、能力版本和批准记录。修订准备可失败且无副作用；提交成功才切换有效版本。原理论价、既有锁存与已发生阶段消费不得重算清零。需要内核新原语/SDK/schema的修改按 U5 处理，不伪装 U4 数据迁移。

## 7. 策略替换状态与事务

实例状态与切换事务分开：实例为 SHADOW / ACTIVE / DRAINING / REVOKED；切换事务为 PREPARING / SHADOW_READY / QUIESCING / COMMITTED / VERIFIED / ABORTED。

| 转换 ID | 事件与 guard | 原子事实及 effects | 故障处理 |
| --- | --- | --- | --- |
| S01 | 签名/协议/状态兼容候选启动 | 创建 SHADOW；只读 checkpoint N 与增量；不授予交易 context | 失败仅取消候选，不重启仍可信内核 |
| S02 | 候选追平且批准差异检查通过 | SHADOW_READY，记录state hash及输入边界 | 队列缺口、版本不支持、未批准差异即ABORTED，不伪造追平 |
| S03 | 开始切换 | 旧 ACTIVE → DRAINING，暂停新意图；设屏障B，分类已接受/待提交/UNKNOWN意图 | 原订单与保护继续；旧版有缺陷则保持禁开 |
| S04 | 候选追到B，旧意图已分类 | TX_SWITCH 原子写新epoch/快照/授权；旧实例REVOKED，新实例ACTIVE | CommitAck前候选仍无权；失败不能双授权 |
| S05 | 新版健康观察通过 | VERIFIED；按批准恢复政策再决定是否ARMed | 旧消息永远不能复活旧epoch；事实不因程序回退倒退 |

提交前断电：新epoch未生效，重启仍先核对，再处理原有效授权政策。提交后断电：新epoch是唯一持久代次，禁止重新授权旧进程。无论哪种情况本次启动的 context 全部失效。已有计划、真实成交、禁开与原意图由内核继续管理，候选不得复制生成。

## 8. 人工接管与外部活动

外部加仓、减仓、反手、双向仓及撤单事实不归类为本策略真实成交；按可证明的原归属入账，未知分配建立差异。内核不以净持仓相等证明归属正确，不以外部平仓后恢复旧目标来自动补仓。

| 转换 ID | 转换 | guard 与后置状态 |
| --- | --- | --- |
| H01 | 无接管记录 → TAKEOVER_PENDING | 显式接管请求或故障政策；即时禁开并冻结旧目标自动补量，TX_TAKEOVER登记范围；已有可信保护在未交接前继续 |
| H02 | TAKEOVER_PENDING → TAKEN_OVER | 当前事实屏障已追平；相关单均终态，或将所有仍活动/UNKNOWN单身份、方向、潜在量、保护动作及责任纳入显式确认清单；角色与context有效 |
| H03 | 接管确认事务成功 | 同一事务撤销交接范围内自动管理权，发布具体未决风险；后续事实继续记账/告警，不再恢复旧目标 |
| H04 | TAKEOVER_PENDING → FAILED | 权限/事实不完整、确认过期或清单版本改变；保持禁开和未交接状态，不擅自关闭可信保护 |
| H05 | TAKEOVER_PENDING → CANCELLED | 有效命令撤回接管且未移交管理权；记录撤回原因，仍保留其他禁开及故障，不自动恢复新仓权限 |

接管清单变更使旧确认失效，尤其在确认前又到达开仓成交/新活动单时。软件只管理本机权力，不能阻止另一客户端下单；界面必须显示其边界。由操作者在其他客户端平仓的确认不能代替柜台事实核对。

停止内核属于独立高影响命令，不能从“接管按钮已点击”或“撤单已发出”推导。保护本身故障时立即标为不可信并告警，不能声称 H01 期间仍在正确保护；需要明确的接管渠道和责任。

### 8.1 用户命令回执状态

命令回执固定为 `RECEIVED / REJECTED / ACCEPTED_PENDING_COMMIT / COMMITTED / EXECUTING / SUCCEEDED / FAILED / NEEDS_RECONCILIATION / SUPERSEDED`，与周期、订单及成交状态分离：

| 转换 | 含义与可证明结果 |
| --- | --- |
| RECEIVED → REJECTED | 结构、角色、上下文、范围或前置条件不满足；没有因此执行新交易效果；已有同身份事实不能删除 |
| RECEIVED → ACCEPTED_PENDING_COMMIT | 内存验证通过，准备事务；不是已持久成功或已发送 |
| ACCEPTED_PENDING_COMMIT → COMMITTED | 对应事务确认；重复请求返回该命令的现有进度，不再次提交交易 |
| COMMITTED → EXECUTING | 命令授权的具体操作正在推进；回执必须带关联意图/订单/核对身份 |
| COMMITTED/EXECUTING → SUCCEEDED | IPC 对该命令定义的后置断言已满足；只证明所声明阶段，提交/撤单请求完成不能显示为成交/撤成 |
| 任一可执行阶段 → FAILED | 已知失败且业务结果可解释；已发生的成交、占用、保护保留，不视作从未执行 |
| 任一可能产生外部效果的阶段 → NEEDS_RECONCILIATION | 结果不能确定；禁止以新command_id重复相同未决操作来规避未知 |
| 未完成命令 → SUPERSEDED | 被新的明确控制决定取代，不再产生该命令尚未执行的效果；已到SDK/柜台的尝试、真实成交、保护及机会消费不会被撤销 |

同一已验证请求身份和同内容重试返回最新已知回执，不能重新签发有效期或执行效果；同身份不同payload拒绝冲突。重连查询旧命令结果需要当前会话有读取该账户结果的权限，但无需让已过期的旧context重新有效。NEEDS_RECONCILIATION 只有取得可靠事实后才进入相应完成/失败状态；旧SUCCEEDED后出现新关联事实时追加更正和核对状态，保留原回执审计。

## 9. 维护、停止与日切

| 转换 ID | 转换 | guard、事务与 effect |
| --- | --- | --- |
| M01 | 无维护记录 → REQUESTED | 有效维护命令，固定目的/组件/U等级；不自动新增平仓动作 |
| M02 | REQUESTED → QUIESCING | 内核建立禁开维护屏障，撤销新增context，等待pending事务/命令分类；保护与订单处理继续 |
| M03 | QUIESCING → SAFE_TO_STOP | U5：当前完整事实显示空仓，无活动/UNKNOWN相关订单、无未完成发送/预占/接管差异；检查与维护屏障在同一所有者控制域 |
| M04 | SAFE_TO_STOP → STOP_COMMITTED | TX_MAINTENANCE持久记录安全证据、boot、停止政策和交接；无新事实使证据失效 |
| M05 | STOP_COMMITTED → UPDATING | 原进程与SDK确证退出，更新器核验制品并处理其独立更新日志；不直接改运行账本 |
| M06 | UPDATING → STARTUP_RECONCILE → COMPLETE | 新版读兼容状态/迁移，由唯一内核写入；重新核对通过，维护结果可解释 | 
| M07 | 任意准备阶段 → FAILED | 事实变化、签名/兼容/迁移失败或期限到；保留维护锁/禁开依据，按实际进度恢复，不能自动复活停止策略 |

`maintenance_locks.status` 使用上表同名状态，不将 STOP_COMMITTED 简写为 STOPPED。前者是内核已持久化停止决定，后者仅用于可证明进程/SDK实际结束的生命周期观察；守护不能据停止决定提前切换程序或宣称原保护进程已不存在。

如果操作者明确选择“带未决风险停止保护”，必须先完成 H02/H03 的显式交接；不能称为 U5 空仓安全更新窗口，也不绕过组件规定。日切重建 SDK 同样受安全条件约束；仍有仓/活动单/UNKNOWN且未经验证能跨日托管时，记录上线阻塞并接管，不能为满足固定时间强杀唯一内核。

维护屏障只能阻止本内核新增请求，不能排除外部客户端在检查后交易。发现新的外部活动或更晚相关事实立即使 SAFE_TO_STOP 证据失效；不能宣称本机锁已实现跨主机排他。

正常休市不触发断流退出，固定18:00不是全部状态的重置事件。新交易日仅改变获批准的日/月计数范围；原周期身份、旧单、未知占用与保护不变。U1/UI独立重启及U3策略切换不要求本机保护链停机。

## 10. 故障与重试规则

| 故障 | 允许的自动动作 | 禁止的推断或动作 |
| --- | --- | --- |
| GUI退出、断开或显示积压 | 清除该会话context，重连快照再接增量；后台继续 | 不因无GUI停止保护或用旧绿色快照显示正常 |
| 策略失联/候选崩溃 | 禁开/撤销新提案权，按批准范围处理残单；原保护继续 | 不删除计划，不把候选启动当授权 |
| SDK/交易连接超时 | 单一恢复调度，保留未知，重新核对 | 不自动换通道/账号补单，不称未发送或已强平 |
| 事实冲突/来源缺口 | 保全原事实与冲突，禁相关能力，补证或接管 | 不覆盖旧成交，不以净仓抵销归属错误 |
| 数据库延迟/写失败/磁盘满 | 禁开、阻断依赖发送、关键告警；仅执行已批准且验收过的降级 | 不跳过持久化“先发再补”，不清空全部预占 |
| 全机断网/断电 | 外部最小观察点报告失联；重启从K01核对 | 不声称本机已平仓，外部观察点没有交易权 |
| 守护重复拉起 | 同账户排他锁拒绝新实例；维护政策禁止复活；崩溃退避有上限 | 不按进程名杀其他程序，不用新数据根规避排他 |

一切自动重试都有已记录的分类、次数上限、下一时点和取消条件；业务报价/次数按 B06/B13/B15 批准。重新提交本地幂等命令是重取原结果，不能增加一次真实报单。SDK实际发送、柜台接受、撤单请求、撤成、成交和财务封存分别记录。

## 11. 必须实现的不变量与黄金序列

以下用例是开发验收规格，尚未执行。引用候选比例/价格的例子仅用于隔离开发事件，不能成为生产默认。每例需断言全部状态向量、交易effect数、durable边界、预占与原因链，不能只断言最后仓位。

| 用例 ID | 输入/故障序列 | 必须断言 |
| --- | --- | --- |
| G0-SM-001 | 新boot加载旧ARMED记录，连接恢复但未完成四类事实 | 新boot DISARMED；旧context无效；没有发送effect |
| G0-SM-002 | 两品种连续提案，首事务等待CommitAck | 首笔pending预占立即阻挡第二笔重复额度；无Ack不发送 |
| G0-SM-003 | TX_INTENT提交后强杀，未知实际SDK调用位置 | 重启保留SEND_POSSIBLE/UNKNOWN；原intent/attempt不换ID重发 |
| G0-SM-004 | TX_INTENT提交确认后、SDK前PauseNew；随后旧Ack重复到达 | 临发拒绝，确证未调用才释放；重复Ack不恢复权限 |
| G0-SM-005 | 第2事务Ack先于第1、旧boot Ack随后到达 | durable连续边界不越过第1；旧boot不改当前状态 |
| G0-SM-006 | 10手开仓先成3手，申请撤余单，撤单期间再成2手 | 原计划保护5手；撤单请求不释放其余潜在量；后续订单目标按事实计算 |
| G0-SM-007 | 平仓目标10，先成3，撤改单中又成2，剩余撤成 | 后续最多5，不能再发原10；各成交只计一次 |
| G0-SM-008 | 撤单ACK先到、终态后到、成交明细更晚 | 原单占用直到终态/累计/明细证据一致；不提前CLOSED |
| G0-SM-009 | 同trade_key重复相同事实，再到同身份不同数量 | 首次只计一次；冲突保全并禁开，不覆盖旧事实 |
| G0-SM-010 | 候选计划理论1000、T100；已持1手，观察1080后回1060，策略/GUI退出 | 候选保护锁存持久，原止损/移动依候选禁用；保护不依赖策略/GUI存活 |
| G0-SM-011 | 保护触及仅内存待提交，随后断电并丢观察尾部 | 重启报告未覆盖窗口；不能假称durable锁存或完整路径 |
| G0-SM-012 | 同事件触发半仓、保护、收盘/批准应急退出 | 按批准优先级单一target；在途半仓占量；不各发全仓 |
| G0-SM-013 | 已观察0仓但原开仓单仍活动/UNKNOWN，之后晚到开仓成交 | 保持FLAT_PENDING_CONFIRM或EXIT_PENDING，不CLOSED、不准核心更新；晚到量归原计划 |
| G0-SM-014 | CLOSED后发现此前未覆盖的非重复成交 | 追加纠正与证据失效告警，重进管理/接管，不丢事实或覆盖封存审计 |
| G0-SM-015 | 优先控制队列要求平仓，较早相关成交尚未归并 | 可立即禁开，但数量与完成判定必须等待事实屏障 |
| G0-SM-016 | 查询快照在B，B后有成交增量，快照完成消息晚到 | 归并后保留增量；快照不得覆盖新事实 |
| G0-SM-017 | 新旧策略同时提相同机会；分别在切换提交前后断电 | 仅一个持久epoch获权，同机会不重复；已有意图和计划保留 |
| G0-SM-018 | TAKEOVER_PENDING期间撤单无终态且又新增成交 | 原确认清单失效；未覆盖未知不能称交接完成；事实继续入账 |
| G0-SM-019 | 人工接管确认包含活动/UNKNOWN清单，外部平仓后重启 | 旧目标不补回；确认范围/风险保留；重启不恢复旧自动管理权 |
| G0-SM-020 | 维护刚通过空仓检查时到达新成交，或旧提交待SDK | 安全证据失效，不能STOP_COMMITTED/UPDATING；没有检查-发送竞态 |
| G0-SM-021 | 用户维护停止，守护发现进程不存在 | 持久停止政策阻止自动拉起，不能因“进程没了”重新开仓 |
| G0-SM-022 | 午休无行情超过候选180秒、墙钟跳变、周五夜盘进入下一柜台日 | 正常休市不误断流；本地期限不因墙钟跳变延长；不清旧日未知/保护 |
| G0-SM-023 | 许可到期、移出白名单、模板切换、策略租约失效 | 可收紧新增权限，已有计划/锁存/归属不删除 |
| G0-SM-024 | 主机全断网后反复到达候选5秒检查期限 | 报告未送达/未知并有界检查，无复制全量平单，无“已平仓”结论 |

每条状态机实现均须满足：无双自动授权；无完整计划不得增仓；同成交只累计一次；不凭撤单请求释放潜在量；没有提交确认不得派生发送；部分成交即纳管；PROTECTED不因策略/版本/界面回退；程序回退不回退事实；恢复不自动补原目标；所有范围不明/未知事实可见且继续阻挡相关动作。
