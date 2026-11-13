# DSV撤单、重报价与追单合同（dev6 / ProtectionPlan 1.2）

dev19在原owner/OMS链新增[原生日证明、逐周期目标/桶预占与最终发送复核](crossday-native-close-20261110.md)；仍保留原同日授权及跨日门禁。本文dev6历史证据不改写。

本页保留dev6的458项检查与1.2执行记录。dev7已增加显式日历、UTC/单调钟及无新报价时的离线保护调度，见[调度合同](scheduling-contract.md)；本文末尾的时钟驱动未实现范围由该文更新。原候选、资源与验收报告不改写。

日期：2026-11-09。范围：独立OnlineTrader的离线执行政策、owner链和故障验证。本页补充[静态证据](strategy-dsv-evidence.md)与[1.1保护合同](protection-dsv-contract.md)，使用本地匿名事实和只记录请求的开发端口；没有运行DSV、交易SDK或柜台。

当前版本`0.1.0.dev6`。最终完整检查 **458项通过，0失败/错误/跳过**，比dev5新增73项；见[确切结果](../output/checks/20261109T133923Z-2c637c60/result.json)及[逐项日志](../output/checks/20261109T133923Z-2c637c60/check.log)。编译、冻结G0/1.1及新1.2资源完整性、导入边界、源码稳定性均通过，`pip check`无冲突。旧dev5源码先归档于`output/baselines/20261109T131054Z-8f4d8650/dev5-source.zip`，SHA-256为`55d70846e32d65025fa686c12a5056230ce98ea199e13e95b095e17f1f9f2bfb`。

交付dev6离线wheel，SHA-256为`027d7d95bcd2bba68219a40bb382be71fcca245064786e0deac0d0443db9efd1`。[构建/干净安装审计](../output/artifacts/20261109T133433Z-e5e9e4f9b32b/wheel-audit.json)确认51份Python源码与6份资源共57份产品文件，在最终检查、wheel及全新安装中逐字节一致。独立core环境以`-I`、`--no-index`安装，无Qt/QA/交易SDK；默认激活被拒绝，显式开发激活仍禁开，授权后只记录1次STRATEGY请求、确认成交为0。纯规则另验CAP1057、恶化撤单、终态残量重挂、收盘阶梯及1.2半仓保护，没有撮合或生成柜台成交。

## 版本与职责

`build_development_plan(..., execution_version="1.2")`显式生成新政策；缺省调用继续生成1.1。正式DSV开发运行器显式选择1.2。1.0/1.1的资源、已有计划内容和执行分支保持原义，新周期不能重写旧持仓计划。1.2的半仓、保本、移动止盈、断流及日停原语继承1.1。

| 所有者 | 职责 |
| --- | --- |
| `strategy/plan.py` | 将不可变profile编译为带来源参数和时间界限的1.2 DEVELOPMENT计划；不创建时钟或端口。 |
| `execution/policy.py` | 纯计价、收盘上下文、手工取价与WAIT/CANCEL/REPLACE/DONE/STOP建议；不读取系统时钟、不调用SDK。 |
| `kernel/execution.py` | owner处理明确事件、保存执行上下文、撤单原因与重报价链，通过既有事务和唯一发送链落地。 |
| `kernel/engine.py` | 自有订单/成交账本、CommitAck屏障、最终权限与剩余量检查、UNKNOWN和恢复管理。 |
| `kernel/dsv_strategy.py` | 用已校验市场/账户/合约事实驱动1.2执行，并为每次增仓替换重新签发上下文和风控复核。 |

新schema为`resources/protection-plan-v1.2.schema.json`，来源manifest见[扩展资源记录](spec-extension-dsv-execution.json)。它不扩张冻结IPC 1.0的手工/策略批准面，也不授权连接柜台。

## 计划执行政策

`execution_policy.algorithm="DSV_TARGET_POSITION"`。继承的标识包括`policy_id`、`close_offset_policy_id`，用于绑定原计划；`price_steps=[]`，不把DSV时间阶梯编造为“第几次撤单”。

| 字段 | 1.2编译值及含义 |
| --- | --- |
| `first_price_mode` | 被动入场保存开关为真时PASSIVE_LIMIT，否则OPPONENT_LIMIT；实际开盘上下文仍可按下表选择ACTIVE。 |
| `entry_confirm_timeout_ms` | 保存的整数秒×1000，显式0保留；仅尚无任何已确认开仓成交时检查严格超时。 |
| `passive_entry_first`、`passive_wait_ms` | 保存开关，等待固定3000ms。隐藏旧字段`passive_wait_seconds`当前不在profile开放配置中，3000ms是该发行包缺省值。 |
| `close_retry_ms`、`cancel_check_interval_ms` | 均为3000ms，表示收盘目标/上下文刷新门；SDK价差撤单按新事实驱动，不受其固定周期限制。 |
| `extra_tick_remaining_ms`、`exchange_limit_remaining_ms` | 收盘剩余10000/5000ms的价位选择界限，含等号。 |
| `force_retry_ms` | 强平上下文刷新5000ms；不代表忽略在途单重复发送。 |
| `passive_ttl_ms`、`aggr_ttl_ms`、`market_ttl_ms`、`cap_ttl_ms` | 依次90000、60000、120000、90000；上下文有效期含等号。 |
| `open_offset`、`entry_slip_ratio`、`chase_slip_ratio` | 精确保存品种O和普通/开盘比例，供当前窗口重新计算cap。0比例保留，cap仍至少1点。 |
| `chase_window_ms` | `floor(保存分钟数×60000)`，在整数毫秒域表达原闭区间；0保留。 |
| `max_child_order_attempts` | null；原SDK没有已证实的固定总尝试次数，不自拟上限或将null当释放未知量的依据。 |

持续时间超出内部有界整数域时明确拒绝，不截断成另一个策略。非基线参数变体仍标记ISOLATED_DEVELOPMENT_VARIANT，计划不继承保存配置的批准记录；基线参考确认与实际交易权始终分开。

## 入场价格、期限与部分成交

令A为原理论触发价，O为本品种开仓偏移，p为BUY的ask1或last、SELL的bid1或last。计划A与原机会身份冻结，重挂不采用新的触发价或制造第二次入场机会。

| 场景 | 初始与后续上下文 |
| --- | --- |
| 开盘near_open | 当前盘次起点为09:00、09:30或21:00，且`0<=since_open_ms<=chase_window_ms`。初始ACTIVE，原开盘入场链保持ACTIVE；午间恢复盘不因刚恢复就自动视为开盘窗口。 |
| 非near_open且被动开启 | 自入场经过时间<3000ms使用PASSIVE，价格为A；仍未确认入场时达到3000ms转CAP并刷新该上下文起点。已在PASSIVE阶段确认入场则保留该价格上下文，按其原90秒TTL自然到期。 |
| 非near_open且被动关闭 | 使用CAP，BUY cap=A+max(1,round(O*r))，SELL cap=A−max(1,round(O*r))；整数round采用ties-to-even。当前窗口决定r取普通或开盘比例。 |
| CAP有效时 | BUY min(p,cap)、SELL max(p,cap)。方向不符或已过TTL不继续采用旧cap，按源回调回到ACTIVE；合法性与最终风控仍独立复核。 |

普通初始CAP的出生时间是入场起点0，不能在首次后续execution_tick延迟到达时重新获得90秒寿命；被动等待结束后新建的CAP按实际转换时刻计龄。首笔非near_open也实际应用CAP，不能只靠“另一口价有利”的资格豁免发送超cap的对手价。
| 无已确认开仓且超时 | `elapsed_ms>entry_confirm_timeout_ms`才申请撤单并持久停止该链新增；等号尚未超时。原程序180秒丢pending的行为不释放本内核预占。 |
| 已有部分开仓确认 | 60秒未确认期限不再作为强平已确认持仓的理由。已成量立即沿原保护计划管理；余量、当前权限及重挂须另行核对。 |

原程序被动转价时的`target0→target原量`不等于旧单已经撤回。本实现先持久化撤单原因，再等待终态与一致成交明细；新子单只覆盖仍需的剩余量。`no_chase_after_act`仍属于新信号资格筛选，不擅自改成原程序不存在的持续挂单撤销规则。

## 价格模式、收盘和手工取价

| 模式/场景 | 计价与边界 |
| --- | --- |
| ACTIVE | BUY ask1或last，SELL bid1或last。缺价沿源回调候选回退计算，但不因此获得发送资格。 |
| PASSIVE | 方向相符且age<=90000ms时取正level。 |
| AGGR1 | 方向相符且age<=60000ms时取对手价±1tick。 |
| MARKET | 方向相符且age<=120000ms时BUY取涨停限价、SELL取跌停限价；源缺限价时的±10tick仅作为raw候选，完整性不足仍拒发。名称不等于市价单类型。 |
| 收盘阶梯 | `remaining_ms=advance_seconds×1000−elapsed_close_ms`；remaining<=5000用MARKET，else<=10000用AGGR1，否则ACTIVE。首次或距上次上下文刷新>=3000ms才刷新。该阶梯只用于收盘退出。 |
| 常规保护退出 | SL、TP、保本、半仓等沿默认ACTIVE及SDK价差撤单规则，不套用收盘的10/5秒阶梯。 |
| 强平 | 明确force_close事实建立强平起点，MARKET上下文首次或每>=5000ms刷新；仍受相同订单终态、潜在量和合法价门禁约束。 |
| 手工直单 | 对手价：BUY ask/SELL bid；排队价：BUY bid/SELL ask；最新价：last。缺对应价先等待明确经过时间，elapsed>=4000ms才检查源兜底。 |

手工兜底BUY用涨停、SELL用跌停；有正last且`abs(limit−last)/last<=limit_fallback_ratio`才可选该限价，默认1%；缺限价可保留last候选，缺正last不接受极限限价。最终发送仍要求两边限价、tick与质量证明。`select_manual_price`返回`automatic_reprice=false`，只为一次明确手工请求选价，不创建定时器或自动追单任务。保存的手工`close_price_mode`不覆盖策略SDK价格回调。

所有模式均保留`raw_price`供说明，但只有complete/fresh/is_trading均明确为真、quality为CONTIGUOUS、tick及上下限已知、正价对齐tick且位于合法范围内才返回可发送price。原回调没有统一夹价/对齐，本实现拒绝非法结果，不悄悄换成另一价格。未知限价/tick不能靠“沿用DSV回退”变成已知事实。

## 撤单、重挂与订单身份

仅当BUY新合法价>原挂价、SELL新合法价<原挂价时，SDK价格恶化逻辑才申请撤单；等价或有利变化保留原单。余量减少、开仓权限撤销、无确认入场超时也能提出各自明确的撤单原因。无合法新价时等待，不为获得报价而创建订单。

每次撤单原因与请求先提交并收到本次正确CommitAck，再调用端口。撤单调用返回、CALL_RETURNED、撤单超时和cancel_status存在均不等于终态，不释放数量。只有终态证据与已记录成交量一致，且源主动重价/目标缩减的原因仍成立，才能准备重挂。

明确时钟证明自撤单请求经过30000ms仍没有终态时，持久记录`WAIT_RECONCILIATION/CANCEL_TERMINAL_TIMEOUT`、禁开并要求核对，保留预占。`DEFINITELY_NOT_SENT`只能在新的显式执行事实后再申请，不能由自己的SEND_ABORTED回调无界重试；不同tick规格在入口即拒绝，不能拿新报价规格替代原合约规格。

- 同一重价链保留原`intent_id`；每次重挂创建新的`child_order_id`和`attempt_id`，`ordinal`递增，`parent_child_order_id`指向上一子单。
- 可新增量扣除当前所有未终态潜在量与确认成交。退出链额外受同一`target_remaining_qty`限制，多个原因只收紧共同目标。
- OPEN重挂要求当前Context、epoch、账户revision、余量、报价时效、合约tick/乘数与配置身份重新校验。策略运行器为新子单准备新的授权和最终gate；旧授权不自动覆盖新尝试。
- REJECTED或未证明属于主动撤改的意外终态停止原执行链；不能改用另一个新intent绕过而盲目重试。
- 旧单终态后又出现相矛盾晚到成交，先去重记录真实成交，置恢复/禁开，再申请撤回replacement；不丢成交、不重播旧发送。矛盾未解决前保留全部可能量。
- UNKNOWN发送/撤单、累计成交不一致、存储失败或错误Ack都阻断替换派发；重启仍保留原计划、链关系及未知容量，不能自动补播开仓。

执行状态包含当前source身份/hash、elapsed时钟、价格上下文与起点、撤单原因、entry_stopped、收盘/强平起点和最近动作，与周期一起持久化并纳入策略交接checkpoint。改变执行阶段会影响checkpoint，不能只比较保护目标而丢失撤改状态。

## 明确离线事件API

首选既有`DsvStrategyRuntime.on_market(event,account,instrument)`：对已管理的1.2周期，用已校验源时间计算elapsed、当前盘次elapsed、near_open和close_due，以合约元数据构造market后交给owner的执行入口。重复来源行情或时间倒退不成为新的保护/执行事件。

直接开发调用为`Kernel.execution_tick(cycle_id,event)`，仅显式DEVELOPMENT且有隔离端口可用。event必含：

| 字段 | 合同 |
| --- | --- |
| `event_id`、`source_epoch`、`sequence` | 明确证据身份与正整数来源序号；同来源身份同内容返回重复回执，异内容拒绝，epoch/sequence不得倒退。 |
| `trading_day`、可选account/cycle/contract绑定 | 必须匹配原计划范围，不能跨账户、合约、周期或交易日。 |
| `elapsed_ms` | 非负整数，明确原周期起点以来经过时间，不得倒退；不是引擎自行读取的柜台时钟。 |
| `market` | canonical价格last/bid1/ask1/tick_size/lower_limit/upper_limit及complete/fresh/is_trading/quality；OPEN另需since_open_ms，near_open须由明确盘次事实解释。 |
| `close_due`、`force_close` | 若给出必须是明确bool；建立对应退出起点和共同目标，不证明已平仓。 |
| `context_id`、`peer_id` | 明确手工开发增仓替换时所需的当前授权；策略链由运行器内部准备替换Context。 |

调用先取得Future；正确持久Ack后才发布状态和推进撤改，Future不是柜台接收/成交证明。原`record_fill`、`confirm_terminal`与`reconcile_development_cycle`继续提供独立匿名开发证据，源回调或发单返回不代替它们。没有新增公开IPC行情注入或远程下单入口。

## 验证范围与剩余工作

本轮新增计划14项、纯执行政策31项、owner整合/故障18项及收口回归10项，共73项。定向文件为`test_dsv_execution_plan.py`、`test_dsv_execution_policy.py`、`test_dsv_execution_runtime.py`和`test_dsv_execution_regressions.py`，覆盖计划版本、计价/TTL边界、终态剩量重挂、Ack/回滚、UNKNOWN、晚到成交、重启与正式策略驱动；旧385项与新项一起纳入本文开头的458项全量结果。

当前仅处理明确开发事件；没有自动墙钟服务、在线行情/日历调度或柜台连接。SDK真实交易所开平及今昨仓offset、真实账户查询屏障、交易所拒撤错误码和网络回调顺序属于后续G2范围。隐藏`passive_wait_seconds`仍未作为可编辑profile字段开放，不把默认3秒写成用户已批准的新保存参数；源码中可丢pending或批量账户撤单的行为不削弱本内核持久归属/UNKNOWN合同。
