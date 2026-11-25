# G0 IPC 1.0：消息、权限、幂等与状态流规格

规格版本：`OT-G0-IPC-1`。日期：2026-11-09（Asia/Shanghai）。状态：工程开发规格；没有授予账户、业务规则或交易权限。

本文件是帧、编码、消息与确定性哈希的唯一归属；字段状态由[状态机](state-machines.md)、事务由[数据模型](data-model.md)、计划由[保护计划](protection-plan.md)约束。机器可读合同为 [ipc.schema.json](schemas/ipc.schema.json)，其中计划通过相对引用连接 [protection-plan.schema.json](schemas/protection-plan.schema.json)。schema 的 HTTPS `$id` 只标识文档，开发与生产必须使用本地 registry，禁止校验时联网下载。

## 1. 端点、身份与可调用能力

内核提供本机 Windows 命名管道，管道名包含产品实例 ID，不使用账户明文或秘密。服务端创建时设明确 ACL、拒绝远程客户，并验证实际连接进程的 Windows 身份与已登记的启动实例；不能按请求自报的 `requested_role` 授权。服务身份、跨会话及 Qt 互通属于 W03/W06 的实机检查项，本文件不把进程分离当成安全沙箱。

| 已认证角色 | 接受的入站消息 | 明确禁止 |
| --- | --- | --- |
| DESKTOP | ContextRequest、OperatorCommand、GetCommandStatus、SnapshotRequest、HeartbeatAck | 直接上报成交、保护锁存、数据库确认或发送 SDK 请求 |
| STRATEGY | ContextRequest、DecisionProposal、StrategyStateProposal、SnapshotRequest、HeartbeatAck | 直接改权威资金/持仓、授予自身 epoch、控制原保护 |
| SHADOW | SnapshotRequest、HeartbeatAck；差异结果通过独立开发/更新接口 | DecisionProposal 产生真实意图；SHADOW 即使复制 ACTIVE 的字段也无权 |
| NOTIFIER | NotificationResult、HeartbeatAck | 交易/许可/人工确认；通知送达不能解除风险 |
| SUPERVISOR | ContextRequest、SwitchStrategy、GetCommandStatus、SnapshotRequest、HeartbeatAck | 通过自身独立通道直接下单、伪造维护安全窗口 |
| DIAGNOSTIC | SnapshotRequest、HeartbeatAck | 任意变更命令或获取凭据 |

输出消息仅由内核构造；pipe dispatcher 不接收外部 `CommitAck`、AccountFact 或成交回报。SDK 回调走受控适配器内部入口，数据库确认走内核内部队列。这些内部消息可使用相同校验器，不能因此成为外部可调用 API。

权限最终是“已认证 peer + account_key + capability + context + 当前状态”。同一管道支持单一绑定账户；选换账户先完成原范围退出/核对与权限释放，再建立新连接。不能通过重连、改管道名或 gateway_identity 获得第二份账户执行权。

## 2. 二进制帧与限制

字节布局固定为 `magic[4] | major[u16] | minor[u16] | json_length[u32] | UTF8_JSON[json_length]`，所有整数为无符号 little-endian。`magic` 为 ASCII `OTIP`；当前 major=1、minor=0；头长12字节，长度仅计算后面的 JSON 字节，不含头。管道以字节流处理，读取端必须循环收满头与正文，支持任意拆包与多帧粘连；不能假设一次 read 等于一条消息。

以下数值是协议1.0的工程边界，不是业务风控参数：

| 项目 | 固定值/处理 |
| --- | --- |
| 单帧 JSON | 1–1,048,576 字节；长度0或超限在分配正文前拒绝并断开 |
| JSON 最大深度 | 32层；单个字符串和数组还受各自 schema 上限约束 |
| 握手完成时限 | 服务端接受后5秒，采用服务端单调时钟；超时关闭连接 |
| 控制队列 | 每peer至多256帧，总字节至多8MiB，先达到者生效 |
| 可靠事件队列 | 每peer至多1,024帧，总字节至多16MiB；不能反压SDK泵 |
| 快照 | canonical明文最多64MiB；每块raw≤262,144字节，最多256块 |
| 心跳 | 每5秒；15秒未收到标记peer失联，另检查业务进度 |
| context有效期 | 由内核决定且≤30秒；不会延长策略/订单/柜台业务期限 |

在读取长度后到收完正文的进度超时同样使用5秒且不随零字节读取重置；这些链路超时只影响IPC连接，不代表报单未发送、持仓已平或行情已断流。SDK断流和180秒/5秒业务政策仍按 B15 另定。

先校验magic/版本/长度，再严格UTF-8解码、解析受限JSON，最后做schema与语义校验。拒绝BOM、重复对象键、孤立surrogate、非Unicode scalar、非法UTF-8、NaN/Infinity、浮点JSON数、小数/指数JSON数与`-0`。JSON数值仅用于非负整数数量、长度、计数及毫秒，最大2,147,483,647；价格/金额/比例必须是规范十进制字符串。协议不压缩、不支持pickle、不识别函数或可执行表达式。错误原因只含脱敏字段名和错误码，不回显秘密正文。

## 3. 统一envelope与类型

envelope所有字段必填；握手前允许null的字段由schema明确限制，不用省略字段表达未知。

| 字段 | 类型/含义 |
| --- | --- |
| protocol_version | 固定字符串`1.0`，与二进制头一致 |
| type | schema枚举；未知类型拒绝，无“尽力兼容” |
| message_id | 1–128 ASCII标识；仅一次传输关联，不是业务幂等身份 |
| connection_id | Hello为null，Welcome后为服务端签发的连接身份 |
| correlation_id | 响应引用请求message_id，主动流消息为null |
| sent_at_utc | RFC3339 UTC Z时间，仅审计，不用于授时或延长有效期 |
| kernel_boot_id | Hello为null，其余必须等于当前已绑定内核启动身份 |
| account_key | Hello为null，其余为当前绑定账户范围的opaque标识 |
| body | 按type选择严格schema，所有对象拒绝未知属性 |

所有 `*_seq`、epoch、revision及单调纳秒值在线为十进制整数字符串，无前导零；语义校验上限为9,223,372,036,854,775,807。持久epoch/revision从1起，只有明确的流/入口/持久游标允许0；revisionSet及已有实体引用不得用0表达缺省。SQLite存INTEGER，转换由边界适配层承担。日期、时间需真实可解析，`format`注解不能替代运行校验。

### 3.1 消息清单

| 消息 | 方向 | 主要合同 |
| --- | --- | --- |
| Hello / Welcome | peer→kernel / kernel→peer | 客户端版本集合、请求角色、构建身份；返回协商版本、peer/connection、账户、capabilities及限额 |
| ContextRequest / RequestContext | 双向 | 申请指定purpose/operation/scope的短期上下文，返回内核登记的context_id与相关revision |
| OperatorCommand / CommandReceipt | 双向 | 幂等业务command_id、scope、operation、期望revision、hash；回执明确持久化与动作完成阶段 |
| GetCommandStatus / CommandReceipt | 双向 | 以command_id+hash取当前已有结果；不重新执行命令 |
| DecisionProposal / ProposalReceipt | strategy↔kernel | proposal/decision身份、ACTIVE代次、based_on_seq、计划、数量和受限入场状态候选 |
| StrategyStateProposal / StateProposalReceipt | strategy↔kernel | 不含订单的路径/滞回状态候选；同样验证ACTIVE代次、context、因果依据和持久化，不消耗或重置开仓机会 |
| SnapshotRequest / Begin / Chunk / End | peer请求，kernel输出 | 确定快照边界、分块、完整性哈希和增量起点 |
| Delta / Gap | kernel→peer | 连续订阅stream_seq、事实event_seq、durable_seq与不可变投影；缺口必须重建 |
| MarketEvent | kernel→strategy/SHADOW | 规范行情、来源/时段/质量、接收单调时间、观察持久化状态；不是可由worker回写的行情 |
| Heartbeat / HeartbeatAck | kernel→peer / peer→kernel | 前者报告内核进度，后者只回报peer最后看到的游标及自身推进；不允许peer覆盖内核健康状态 |
| SwitchStrategy / SwitchReceipt | supervisor↔kernel | 旧/新epoch、屏障、候选身份、状态hash、批准记录与切换进度 |
| NotificationTask / NotificationResult | kernel↔notifier | 持久delivery/event身份、有限投递租约、脱敏内容与投递结果 |
| ProtocolError | kernel→peer | 脱敏错误码、相关消息、KEEP/RESYNC/CLOSE动作 |

`event_seq`严格表示一笔业务提交的journal包络序号，与数据模型及状态机一致，不是每条原始quote的自然序号。`MarketEvent`只用于会进入策略/保护依据的关键规范化观察：它的event_seq必须绑定到包含该观察的有序journal事务；若一个事务包络含多个观察，按包络内记录的ingress_seq排序，context另外绑定具体观察位置。允许先发`observation_durability=OBSERVED_PENDING`，但该序号不能永久只存在于内存中再被后续durable_seq跨过；成功提交后才计入连续已提交高水位，失败/缺口阻断相关派生发送并核对，不能跳号伪称持久成功。提案准入、保护锁存和计划发送必须提交所依赖的观察/路径事实，旧boot的pending context重启后失效。

纯UI可合并行情、不参与策略或保护的原始quote不生成新的业务event_seq，只使用ingress/stream游标或引用已知的业务边界；不能给未登记quote虚构可追溯的journal身份。该区分不要求建设全市场每tick仓库。普通账户/订单投影Delta只发布已持久化状态，`event_seq≤durable_seq`；待提交进度由命令回执或健康视图另报。保护计划的protected_event_seq等引用必须能定位实际journal包络及其中的观察证据，不能指向被永久丢弃的观察。

### 3.2 握手与连接重建

1. 服务端完成OS身份检查后只接收Hello；其envelope的connection/account/kernel_boot均为null。
2. 验证构建身份、登记实例及协议交集。当前只实现1.0，没有共同版本则E_VERSION并关闭；不能降级到未审查协议。
3. 服务端构造Welcome，授予的角色/capabilities由服务端策略决定，绑定账户、peer与connection_id。客户端核对预期服务身份和实例；仅管道同名不够。
4. 客户端先取Snapshot/Context，之后才能提交对应变更消息。握手前的业务消息拒绝；更换boot/账户不能复用旧context。
5. 连接断开不清理持久意图、预占、保护或待核对事实。重连使用新connection_id；原command_id用GetCommandStatus追踪，不自动换ID重报。

新增消息、字段或状态需增加双方明确支持的协议版本并协商；1.0不接受未知字段。消息升级不改业务机会身份，不把协议升级当作规则批准。

## 4. context、相关revision与期限

context_id为内核使用CSPRNG生成、至少128位随机熵的不可预测opaque ID，服务器登记：account_key、kernel_boot_id、认证peer、purpose、允许operation/scope、kernel/strategy epoch、相关revision、issued_event_seq与本进程monotonic截止。发送者只回传ID；schema可通过不表示该ID真实存在。

`RequestContext.valid_for_ms`和`expires_at_utc`只供显示与诊断，服务端校验始终查询原登记的单调截止；重发、换message_id、改客户端墙钟、改TTL或转交另一个peer均不能延长或转让。服务端重启后所有context失效。拒绝也不得将context自动续期。

内核按operation标记相关revision，关联范围存于context并与请求expected_revisions对照。无关合约的普通行情更新不会让提案过期；相关合约价格/路径、position/risk/config/权限变化则必须按本操作的依赖复核。`based_on_seq`是因果依据，不能单独作为“仍有效”证明；临发送时再次确认当前权限、价格、数量、预占与期限。

减风险命令同样要认证、范围和幂等校验。暂停/撤权可先收紧内存权限；涉及平仓数量、接管、恢复或维护结案的步骤仍等待相关事实屏障与必要持久化。context到期不删除已有保护、不释放UNKNOWN，也不取消已提交订单。

## 5. 确定性JSON及payload_hash

### 5.1 Canonical JSON OT-CJ-1

为避免不同语言序列化差异，严格使用以下算法，不以“sort_keys即可”替代：

1. 按第2节解析并拒绝重复键、非法标量与不允许的数字。允许null、bool、Unicode scalar字符串、非负整数、数组和对象；价钱比例在进入算法前必须已经是schema认可的规范十进制字符串。字符串不做Unicode normalization。
2. null/bool输出ASCII `null/true/false`；整数输出无前导零的十进制ASCII，0只写`0`。禁止小数、指数、负JSON整数或`-0`。
3. 字符串以双引号包围：`"`输出反斜杠双引号，反斜杠输出双反斜杠；U+0000至U+001F一律输出小写六字节`\u00xx`，包括换行也不能改用`\n`；其余Unicode scalar直接UTF-8编码，不转义斜杠、U+2028或非ASCII文字。
4. 数组保留原顺序，元素用ASCII逗号连接；对象按键的Unicode scalar码点序列字典序升序排列，键与值用ASCII冒号，成员用逗号。空数组/对象分别为`[]/{}`。
5. 全文不含空白、BOM、末尾换行或末尾逗号。对这些UTF-8字节取SHA-256，输出64位小写十六进制。

例如对象键原顺序为b、a，值分别为字符串`中`和整数1，其canonical文本是 `{"a":1,"b":"中"}`。字符串内部真正的换行须编码成`\u000a`，不能写成两个字符反斜杠+n或以`\n`短转义代替canonical规则。解码后的等价转义得到相同hash；不做normalization的不同Unicode标量序列不保证相同hash。


可直接纳入开发测试的OT-CJ-1字节向量（UTF-8，无末尾换行）：

| 值 | canonical文本 | SHA-256 |
| --- | --- | --- |
| null | `null` | `74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b` |
| 键为b/a，值为中/1 | `{"a":1,"b":"中"}` | `2831299868169bc527f55f88ebbdcd8b785d78d9e7dc64e6887dfbd2825dd247` |
| 字符串a、换行、b | `"a\u000ab"` | `b1a5ad2996a5f67a85d8398185b98b8819c580bfb3ce2e2f2c887cd3ccd6d93e` |

### 5.2 哈希范围与幂等顺序

OperatorCommand、DecisionProposal、StrategyStateProposal、SwitchStrategy统一构造material：`{"account_key":envelope.account_key,"message_type":envelope.type,"payload":body移除context_id和payload_hash后的完整对象}`，按OT-CJ-1求hash。message_id、correlation_id、connection_id、sent_at_utc、传输kernel_boot_id及context_id均不进入material；body中的业务ID、epoch、期望revision、操作、范围、参数、批准记录和完整计划均进入。

服务器重新计算而非信任客户端hash。对已有业务ID，先验证当前peer有权读取该账户记录，再查持久幂等表：同hash返回现有最新结果且不执行副作用；不同hash报E_DUPLICATE_CONFLICT。对新ID才继续验证context、当前权限与全部业务guard，再以唯一约束提交。这样旧context到期后可查询/回放原结果，但不能产生新操作。并行重复请求在同一owner中合并；待提交记录也立即占住该ID。

Command主键为account_key+command_id，Proposal为account_key+proposal_id，并另核验decision_key未被消费；策略换版不会重置业务机会。认证principal、读取权限和context绑定另查，不因它们未入hash而省略。所有请求冲突保留原记录和冲突证据，不覆盖首个内容。日志不得记录带凭据参数。

计划/配置/状态清单的完整内容哈希同样使用OT-CJ-1；如果一个未来类型包含自身hash字段，必须另定精确排除范围，不能自行猜测。当前ProtectionPlan不含plan_hash自身字段，完整计划对象可直接哈希。

## 6. 用户命令、回执与状态

| operation | 参数与结果边界 |
| --- | --- |
| PAUSE_NEW_ENTRIES | cancel_owned_entry_remainders显式；先禁开，按自身归属处理残单，保护继续 |
| ARM_ENTRIES | rule_set_id/config_revision/authority_policy_id；全部完整性、批准与阻挡通过才赋权 |
| FLATTEN_THEN_STOP | execution_policy_id；只有持仓0、相关单终态、UNKNOWN消解且完整核对后SUCCEEDED |
| BEGIN_TAKEOVER / CONFIRM_TAKEOVER | takeover_id；确认需核对run及接管manifest hash，列明活动/UNKNOWN单，不能以发撤单完成 |
| ENTER_MAINTENANCE | maintenance_id、可空handover_record_id；无接管且不满足安全窗口不得停核 |
| MANUAL_OPEN | 真实合约、方向、正数量、限价、报价政策及完整计划；与自动开仓共用最终硬闸门 |
| MANUAL_CLOSE | 原周期、正数量、报价政策；外部仓须有显式接管ID，不按同合约偷认领 |
| CANCEL_SELECTED / CANCEL_OWNED | 选定子单ID或自身范围；include_exits显式，出口保护不能无说明撤掉 |
| CANCEL_ACCOUNT_CONFIRMED | 二次确认ID、完整核对run、订单范围manifest hash；不默认扩大到外部单 |
| REQUEST_RECONCILIATION | FULL_ACCOUNT或ORIGINAL_CYCLE；后者须有周期ID，不能拿局部查询当完整账户 |
| RESUME_ORIGINAL_EXITS | 当前完整核对、原周期列表与recovery_authorization_id；仅恢复原退出，不产生新开仓权 |

MANUAL_OPEN必须具有明确允许MANUAL来源的manual grant，并通过账户全部增仓闸门，不要求同时启动自动策略；自动提案仍须允许STRATEGY来源且存在ACTIVE strategy_epoch。ARM_ENTRIES的authority_policy_id解析到AUTHORITY类型的policy_revisions，并核验allowed_origins、账户/环境及规则范围，字符串存在本身不授予权限。

scope_ids必须与operation参数、context和实际归属一致：ACCOUNT/OWNED_ORDERS的数组为空；CONTRACT/CYCLE/SELECTED_ORDERS不可为空并与参数逐项匹配。schema检查类型，内核另做该交叉校验。仅关GUI是桌面本地动作，不发交易命令。

| receipt.status | 精确含义 |
| --- | --- |
| RECEIVED | 已读入与分配接收身份，不证明通过guard/持久化 |
| REJECTED | 新请求校验或准入失败，没有因本请求新增交易副作用；保留具体原因 |
| ACCEPTED_PENDING_COMMIT | owner已受理并占住幂等/预占，必需事务尚未确认 |
| COMMITTED | 指定事务持久成功；不代表SDK已经调用、柜台接受或成交 |
| EXECUTING | 动作在途，有相关订单/查询/接管等身份可追踪 |
| SUCCEEDED | 操作特定后置条件有证据成立；“撤单请求已提交”不能作为撤单成功 |
| FAILED | 操作失败，reason/evidence说明可能遗留的订单和风险，不自动释放潜在占用 |
| NEEDS_RECONCILIATION | 有未知或证据不足，命令保持可追踪，不用新ID重试交易 |
| SUPERSEDED | 操作被有身份的后续控制决定替代；原事实和风险不删除 |

回执通过receipt_revision单调推进；同一command重复查询不得制造新的执行。终态后若出现迟到真实事实，追加审计/风险事件并触发核对，不改旧回执来伪装当时已有证据；投影显示当前风险与原完成记录。一般拒绝的event_seq/tx_id可为null；COMMITTED及后续应能回查已提交事务和durable_seq。

内部CommitAck严格字段为tx_id/event_seq/durable_seq/kernel_boot_id/state_revision/outcome/error_code；匹配owner登记的pending事务后才生效。outcome=FAILED不能推进durable_seq、成功回执或权限。延迟确认跨boot或与预期revision不符时先查真实事务结果，禁止直接授权发送。其schema位于`$defs/CommitAck`，未列入公共type枚举。

## 7. 策略提案与切换

DecisionProposal必须匹配context绑定的ACTIVE strategy_epoch、账户、合约、相关revision和已观察event_seq。quantity>0且计划里的account/cycle/contract/direction/initial quantity/config/rule与提案一致；内核生成/确认的业务机会、计划与预算才是权威。worker不能通过报更大的数量、重置路径、改理论价或换版本绕过准入。

entry_state_updates目前仅允许OBSERVATION与HYSTERESIS两种受限候选。前者包含盘次、观察代次、固定窗口、覆盖边界和质量；后者包含方向、触发线、观察代次、WAIT_REVERSAL/WAIT_APPROACH/READY/UNKNOWN阶段及依据事件。它们须匹配当前事件路径和已批准B项后才登记，不是worker直接改权威状态的写端口。止损计数、机会消费、冷却、禁开和真实最高浮盈由内核根据已确认事实/批准规则派生；不得从提案自报值覆盖。

状态更新可与入场事务一起提交；只更新观察而不产生订单时使用StrategyStateProposal，由worker产生受限状态候选、内核验证后登记，不能伪造quantity=0的DecisionProposal。该消息使用STRATEGY_STATE_UPDATE context，既不产生订单，也不消费或归还开仓机会。若新增策略状态无法由当前两种契约表达，在引入该能力前升级状态schema与协议；不得塞任意字典或Python序列化对象。G1的保护不依赖worker路径提案可用。

SwitchStrategy只能由获权supervisor请求；new_epoch须由内核分配且大于旧值，不信任请求自行选中的任意更大值。候选先SHADOW接快照/增量，追至barrier并校验state_hash；内核撤销旧新增权、分类已接受/待提交/已发送/UNKNOWN意图，保护仍继续。TX_SWITCH原子提交代次与快照后才授新权；失败保持禁开或保留仍可信旧版。SwitchReceipt阶段为PREPARING/SHADOW_READY/QUIESCING/COMMITTED/VERIFIED/ABORTED/BLOCKED/NEEDS_RECONCILIATION，不能把SHADOW_READY展示成ACTIVE。

## 8. 快照、增量、缺口与背压

订阅身份为kernel_boot_id+account_key+subscription_id+projection_schema+view。策略订阅的MarketEvent也携带subscription_id和stream_seq，与同订阅Delta共用连续序号；不能用可能合并/过滤的源序号代替流完整性。`stream_seq`在每个订阅中从1连续递增；`event_seq`是全局事实位置，过滤后允许跳号，不能据event_seq跳号断言掉消息。相同stream_seq重复同内容可忽略，内容不同则冲突并失效该订阅。

1. 内核在一个已提交边界N构建不可变projection/1，选择与N对应的last_stream_seq=B，并保留B之后的增量/策略行情。B不能盲取当前最后已发送序号：未提交观察及未反映进快照的消息必须位于B之后并可重放，相关保留区间不足则废弃候选。pending观察保留其原事务/ingress身份及stream顺序；它提交后可更新持久进度，不能当成第二条新行情再执行一遍路径/保护。若pending事务失败或内核换boot，旧观察与context不得变成新实例的持久依据，须按journal恢复和缺口规则重新核对。
2. 发送SnapshotBegin，含snapshot_id、checkpoint_event_seq=N、durable_seq、B、完整canonical字节数与块数。快照数据是`$defs/projectionBatch`认可的`records`对象，用OT-CJ-1编码后分块；按原始字节切分，不要求UTF-8字符正好在块边界。
3. SnapshotChunk逐块携带0起index、raw_bytes、strict Base64和raw SHA-256。接收端先检查索引/大小/块hash，再有界缓冲；重复块只允许相同字节，冲突失败。
4. SnapshotEnd给完整canonical raw SHA-256与相同B；收齐后校验长度、块数、hash、严格JSON、投影schema及实际账户范围。任一失败丢弃整个候选快照，保留旧视图并标过期。
5. 原子替换界面/策略视图，之后只应用stream_seq=B+1起的连续Delta/MarketEvent；策略先回到快照状态再重放，不能保留已被快照替换的推测状态。构建期间积压溢出就废弃候选并发Gap，不能漏掉中间增量仍宣称完成。

projection/1的记录类型为ACCOUNT/CYCLE/ORDER/BLOCK，精确字段在schema；CycleView.confirmed_open_quantity/confirmed_close_quantity/remaining_quantity分别映射position_cycles的confirmed_open_qty/confirmed_close_qty/remaining_qty，target_remaining_quantity只映射exit_targets.desired_remaining_qty，尚无退出目标时为null，不能填初始计划数量。OrderView.origin与order_intents.origin使用同一STRATEGY/MANUAL/PROTECTION/SESSION/EMERGENCY枚举；外部/未归属订单保留在核对证据，不伪造本产品intent/cycle身份套进ORDER记录。通知severity与账本统一INFO/ATTENTION/TAKEOVER_REQUIRED。状态枚举与状态机一致。记录包含record_revision及稳定record_id，重复同revision同内容幂等；新revision不得覆盖更新记录。已关闭周期/终结订单保留为事实，不使用“删除记录”掩盖成交或风险。G4增加视图必须扩充明确投影schema并协商，不能给现有字段夹带任意JSON。

重连仅在boot、账户、视图和schema一致且服务端仍保留所需区间时，从resume_stream_seq+1补发；否则Gap/完整快照。peer必须先标旧视图STALE，不能直接拼接两个boot的增量。流保留上限等于可靠队列1,024帧/16MiB，恢复超限明确RETENTION_EXPIRED。

普通展示可以在进入可靠Delta序号前合并同一记录，合并后形成一个完整新记录；分配stream_seq后不得静默丢弃。控制队列满时在未受理前返回E_QUEUE_OVERFLOW；无法发送错误则断开，客户端以原command_id查状态。可靠输出溢出时撤销该订阅、发Gap或断开；策略关键输入缺口立即撤销新增提案context并禁开，内核保护与SDK推进仍独立。不能让慢UI/worker阻塞保护或交易事实。

## 9. 通知租约与故障码

NotificationTask仅来自已持久化outbox，delivery_id表示事件/渠道的一次逻辑投递，delivery_attempt表示重试次数；每次领取有lease_id和内核单调截止。结果需匹配当前lease及attempt，旧结果只作审计，不能覆盖新投递。超时或网络返回不明确记UNKNOWN，重试由内核政策决定；外部服务未提供幂等保证时可能重复通知，不能承诺外发exactly-once。

NOTIFIER仅拿自身凭据引用及被允许的通知内容，credential_ref不是密钥；取密钥流程按服务/通知身份和ACL配置，普通IPC快照不含秘密。NotificationResult的DELIVERED仅表示提供方结果，人工确认与风险解除通过不同事件登记。Webhook失败不得阻塞交易。

| 错误码 | 含义及动作 |
| --- | --- |
| E_FRAME / E_VERSION / E_SCHEMA | 帧、版本或消息不合法；帧/编码错误关闭，语义字段错误可拒单并KEEP |
| E_AUTH / E_SCOPE | peer或范围无权限；不暴露其他账户内容，必要时关闭 |
| E_CONTEXT_EXPIRED / E_BOOT_MISMATCH / E_EPOCH_REVOKED | 上下文/启动/代次失效；不自动改ID重试交易 |
| E_REVISION_CONFLICT / E_DUPLICATE_CONFLICT | 相关状态已变或同ID不同内容；前者取新视图后重新决策，后者保留冲突原件 |
| E_NOT_READY / E_RULE_UNAPPROVED / E_RISK / E_OWNERSHIP | 完整性、批准、预算或归属不满足；列出真实阻挡 |
| E_FACT_BARRIER / E_STORAGE / E_UNKNOWN_SEND | 事实未追平、持久化失败或发送未知；保持限制/占用并核对 |
| E_QUEUE_OVERFLOW / E_STREAM_GAP / E_SNAPSHOT | 有界队列或流失效；重建快照，不补造事实 |
| E_UNSUPPORTED / E_INTERNAL | 未支持能力或内部异常；禁用受影响操作并脱敏诊断，不转为成功 |

## 10. 示例与开发验收

以下为静态协议fixture，身份和context均为假值，不可用于账户授权。payload_hash按第5节material计算；将哈希字符串放入请求后不再对完整envelope求hash。

```json
{
  "protocol_version": "1.0",
  "type": "OperatorCommand",
  "message_id": "msg-fixture-001",
  "connection_id": "conn-fixture-001",
  "correlation_id": null,
  "sent_at_utc": "2026-11-09T05:00:00Z",
  "kernel_boot_id": "boot-fixture-001",
  "account_key": "acct-fixture-001",
  "body": {
    "command_id": "cmd-fixture-pause-001",
    "context_id": "ctx-fixture-001",
    "kernel_epoch": "1",
    "expected_revisions": {
      "state_revision": "1",
      "risk_revision": "1",
      "config_revision": "1",
      "authority_revision": "1"
    },
    "operation": "PAUSE_NEW_ENTRIES",
    "scope": {
      "scope_type": "ACCOUNT",
      "scope_ids": []
    },
    "parameters": {
      "cancel_owned_entry_remainders": true
    },
    "payload_hash": "410e714c98c53559b78467065e6e8c6a2b72370adeae49f3d2e8df7a3e1b0a74"
  }
}
```

| 用例 | 输入与必须断言 |
| --- | --- |
| G0-IPC-001 | 头被拆为1/3/8字节，正文多次读取及两帧粘连；只能还原两条正确消息 |
| G0-IPC-002 | 长度0/超限、错magic/字节序/BOM/重复key/孤立surrogate/浮点数；在业务受理前拒绝 |
| G0-IPC-003 | Hello自报SUPERVISOR但OS身份不符；不能获得切换或交易能力 |
| G0-IPC-004 | 同ID同hash在pending、已提交、重连、跨boot查询；每次只返回已有结果，最多一次业务受理 |
| G0-IPC-005 | 同ID不同scope/quantity/计划/expected revision；hash冲突不覆盖原记录 |
| G0-IPC-006 | context跨peer/账户/boot、到期、改客户端时间或TTL；不能取得新副作用 |
| G0-IPC-007 | 无关合约行情变动不机械拒提案；相关价格/风险/权限变化必须临发复核 |
| G0-IPC-008 | 快照中发生新成交、块重排/重复/冲突/溢出；只原子采用完整快照并连续接B+1 |
| G0-IPC-009 | event_seq过滤后跳号而stream_seq连续；不得误报丢失；真正stream缺口必须STALE/禁开 |
| G0-IPC-010 | 相同语义换message_id/context/sent_at，或JSON键顺序/转义不同；OT-CJ-1的material hash一致；业务字段改变则不同 |
| G0-IPC-011 | 外部pipe提交CommitAck或伪造AccountFact；schema/type/角色三层拒绝，无状态写入 |
| G0-IPC-012 | COMMITTED之后SDK未调用/调用未知；回执不能变成已成交，也不能据超时释放占用 |
| G0-IPC-013 | 慢UI/通知worker队列溢出；其订阅/租约失效，SDK与保护不被反压 |
| G0-IPC-014 | 撤单仅请求、平仓部分成交、TAKEOVER_PENDING；均不能回SUCCEEDED冒充最终后置条件 |
| G0-IPC-015 | 旧epoch晚到提案/切换前后断电；仅已提交权威epoch有新增权 |
| G0-IPC-016 | 已过期通知lease返回DELIVERED；只审计，不覆盖当前attempt、不解除风险 |

检查分三层：schema结构校验（含本地$ref）、跨字段/权限/时间/哈希语义校验、真实Windows IPC与故障执行。schema通过仅证明结构；本轮规格和静态fixture不证明后两层已实现或验收。
