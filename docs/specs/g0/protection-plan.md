# ProtectionPlan v1：内核持仓保护开发规格

规格标识：OT-G0-PP-1.0；日期：2026-11-09（Asia/Shanghai）。状态：**工程契约可开发；交易公式及参数仍为 PROPOSED 候选**。本轮没有运行产品、SDK、账户或在线交易。

本文件承接[架构拆解](../early-design/在线量化交易软件_架构设计与实施拆解_20261109.md)、[规则裁决表 v0.3](../early-design/在线量化交易软件_规则裁决表_v0.3.md)及 P01–P10，不改变最终功能范围。机器校验入口为 [protection-plan.schema.json](schemas/protection-plan.schema.json)，对应状态、事务及传输分别见[状态机](state-machines.md)、[数据模型](data-model.md)、[IPC](ipc-protocol.md)。需用户批准的结论集中在[业务确认表](business-decisions.md)。

## 1. 职责与三道准入

ProtectionPlan 是一次 position_cycle_id 的有限数据计划。它在原开仓意图准入、计划/预占事务提交之前准备完整；只有执行内核解释，策略及 GUI 不执行保护。首笔成交即覆盖实际数量，不等满仓；未成交时不计算假持仓收益或发送零手退出。策略租约、进程、GUI、设备许可改变都不能删除既有计划。

计划启用须依次通过：

1. JSON 语法与本地 JSON Schema：字段、版本、枚举、长度、单位表达和可选能力分支正确。
2. 语义校验：Decimal 数值、跨字段关系、合约/账户/周期绑定、计划能力、当前 revision、实际数量及行情质量正确。
3. 权限校验：实际解析 rule_set_id、approval_record_id、参数版本、账户环境及允许操作范围；批准记录覆盖所有启用 B 项且未撤销，才允许对应在线执行。

DEVELOPMENT 只允许独立开发事件执行器，不得持有生产或在线模拟交易连接。ONLINE_SIMULATION 和 PRODUCTION 必须有适用的批准记录；仅把字符串从 DEVELOPMENT 改成 PRODUCTION 不获得权限。机器 schema 验证通过不等于业务获批、持仓已保护、订单已送达或已成交。

## 2. 根字段与有限原语

所有 id 是 1–128 位 ASCII 不透明标识，允许字母、数字、点、下划线、冒号和连字符；首字符为字母或数字。seq/revision 在协议里用十进制整数字符串，SQL INTEGER 保存，语义上须不大于 9223372036854775807；plan_revision、config_revision 均从 "1" 起，0只用于明确允许的序列游标，不表示初版、缺省或批准。

价格/金额/比例使用 Decimal 规范字符串：最多 18 位整数、12 位小数，无前导零、指数、NaN、负零或小数尾零；例如 "1000"、"0.8"，拒绝 "01000"、"1e3"、"0.80"、"-0"。距离须正，比例须 0<r≤1。数量为 0–2147483647 的 JSON 整数，初始开仓目标须≥1。舍入只发生在获批的合法委托价/量边界，不改变理论阈值。

| 字段 | 类型/约束 | 含义 |
| --- | --- | --- |
| schema_version | 固定 "1.0" | 不兼容的新原语升级主版本；不能靠忽略字段兼容 |
| plan_id / plan_revision | id / revision | 同周期计划身份与不可变版本 |
| account_key / position_cycle_id / contract_id / trading_day | id / id / id / YYYY-MM-DD | 绑定原账户、周期、真实月份合约和原业务交易日 |
| direction | LONG / SHORT | 映射 d=+1/-1；不是账户净仓方向 |
| theoretical_entry_price | Decimal | 意图准入冻结的理论价 A，重报价及成交均价不改它 |
| initial_open_quantity | 正整数 | 通过最终风险闸门的原开仓目标；不当作已成交量 |
| rule_set_id / config_revision / config_hash | id / revision / SHA-256 | 已登记规则及参数；hash 不包含自身循环字段 |
| execution_scope / approval_record_id | DEVELOPMENT/ONLINE_SIMULATION/PRODUCTION；批准引用可开发态为 null | 执行器范围与外部批准记录，必须解析查证 |
| market_quality_policy_id | id | 有效行情、连续性、源/接收时间及过期判定的版本化政策 |
| price_field | LAST / LIQUIDATION_SIDE | LAST 用最新成交价；后者多仓 bid1、空仓 ask1，字段选择须批准 |
| fixed_stop_loss / fixed_take_profit | 必需启用的距离原语 | 基础 S/T 都大于 0；保护态抑制旧止损是运行态变化，不是改配置 |
| profit_protection | 显式 enabled 分支 | 激活比例、保护线比例及交易日禁开；可关闭以覆盖独立移动止盈场景 |
| trailing_stop | 显式 enabled 分支 | 激活比例、回撤模式及值；仅 NORMAL 模式有效 |
| partial_reduction | 显式 enabled 分支 | 第一阶段、可选二阶段、可选减仓后保本，数量公式有限且固定标识 |
| close_session | 显式 enabled 分支 | 日历/时段政策与提前秒数；不自行假定每天 15:00 |
| slippage_exit | 显式 enabled 分支 | 方向化不利成交滑点阈值及是否附加日禁开 |
| data_gap_exit | 显式 enabled 分支 | 活跃交易时段断流政策、期限与无合法价处理 |
| emergency_exit | 显式 enabled 分支 | 独立批准政策、归属范围及最大减仓量 |
| execution_policy | 有限报价/撤改政策 | 首价、检查间隔、子单尝试上限、升级表与今昨分配政策 |

schema 中所有对象 additionalProperties=false。关闭的可选原语只允许 {"enabled":false}，残留启用参数也拒绝；开启则要求完整字段。禁止 callback、Python/模块名、URL、eval、表达式字符串、远程下载规则或任何未知原语。id 只能解析本地已登记数据，不能按路径加载或作为可执行入口。

### 2.1 语义校验错误

| 错误码 | 判定 |
| --- | --- |
| PLAN_SCHEMA_INVALID | schema、版本、未知字段/原语/枚举或类型失败 |
| PLAN_DECIMAL_RANGE | 超过 Decimal 精度/范围、距离非正、比例非法；不取绝对值修正 |
| PLAN_BINDING_MISMATCH | 账户、合约、方向、周期、交易日与原意图不同 |
| PLAN_APPROVAL_REQUIRED | 批准不存在、撤销、未覆盖 B 项/环境/参数或仅为候选 |
| PLAN_POLICY_UNRESOLVED | 日历、行情质量、应急、报价、平今昨政策 id 不存在或版本不符 |
| PLAN_THRESHOLD_ORDER | 保护线不在 0.4–0.6 的功能范围、保护线≥激活位、阶段二≤阶段一、回撤比例不在 (0,1) |
| PLAN_CONFIG_HASH_MISMATCH | 参数实际内容与登记版本/hash 不同 |
| PLAN_REVISION_CONFLICT | 旧计划/持仓/风险 revision 不匹配，不能按新快照直接覆盖 |
| PLAN_MIGRATION_UNSAFE | 已锁存、已消费阶段、退出目标、理论基准或未知订单被不合法改写 |
| PLAN_CAPABILITY_UNSUPPORTED | 内核版本不能表达原语/政策；必须核心升级，不能降级忽略 |

0.4–0.6 是功能允许的保护线区间；80%、50% 等具体值仍是未批准示例，不是生产默认。若最终批准的规则需要超出当前原语或范围，先显式修改规格和对应验收，不能以配置绕过。

## 3. 多空公式与行情边界

定义 A=冻结理论价，S=止损距离，T=止盈距离，p=按 price_field 取得的有效价格，d=LONG 的 +1 或 SHORT 的 -1：

- 当前有利变动 x=d×(p−A)，最高有利变动 M=max(0,已记录 M,x)。
- NORMAL 固定止损：x≤−S；固定止盈：x≥T。
- 保护激活：M≥activation_ratio×T；只增不减地锁存 PROTECTED，并登记本交易日禁开原因。
- PROTECTED 普通出口：x≥T，或 x≤floor_ratio×T，或获批时段的收盘退出。
- 多头价格线：止损 A−S、止盈 A+T、激活 A+rT、保护 A+αT；空头为 A+S、A−T、A−rT、A−αT。
- 原始成交价差 Δ=成交量加权均价−A，不利滑点=d×Δ；滑点退出候选为不利滑点严格大于阈值。等于阈值不退出，0 代表零不利容忍，不是关闭。

只使用已发生且来源/交易时段/新鲜度通过的事件；无效 quote 不更新 M、不取消锁存，不触发基于假价的出口。定时事件不凭空生成价格。不能用未来的分钟 OHLC、开仓前日内极值、缺口后的最终高低点重建本周期瞬时路径。

首个 FILL_CONFIRMED 建立受保护实际数量，并对当前有效在线快照进行一次当下评估；这不证明已恢复成交后、回报到达前未观察的路径。没有当前有效快照则持仓覆盖计划仍存在，但质量为 DATA_GAP，停止新增并按已批准预案处理。相同事件重放、重复行情、重复成交不产生第二次锁存或第二次阶段目标。

已观察但未提交的 M/锁存在内存 pending 状态即阻挡回退，相关事件按业务序号入事务；只有 CommitAck 后标记持久成功并允许相关订单发送。崩溃前未提交的触及可能丢失，恢复须报告覆盖缺口，不能承诺任意上游瞬时行情永不遗漏。

## 4. NORMAL、PROTECTED 与可选分支

| 原语 | NORMAL 中的候选行为 | PROTECTED 中的行为 |
| --- | --- | --- |
| 固定止损 | x≤−S 时目标剩余量 0 | 抑制原止损，不因回落再启动 |
| 固定止盈 | x≥T 时目标 0 | 继续有效 |
| 保护锁存 | M 达激活位后持久进入 PROTECTED，并日禁开 | 不回退、不因参数重载/重启/策略换版清零 |
| 移动止盈 | 达激活位后按 M 维护回撤线，触及则目标 0 | 普通移动止盈抑制 |
| 半仓/二阶段 | 到阈值后进入阶段流程，不重复消费 | 当事件及以后抑制尚未消费阶段；已有在途单继续归账 |
| 半仓后保本 | 首阶段实际减仓确认后收紧普通止损至理论价 | 抑制保本旧止损；不是第四个普通出口 |
| 收盘退出 | 已批准时段期限到达请求目标 0 | 继续有效 |
| 滑点/断流/账户应急 | 独立批准的风险政策，统一目标与报价 | 只有批准明确允许覆盖保护三出口时才执行；不能静默增加强平规则 |
| 人工退出/接管 | 按授权范围处理，不补回旧目标 | 不改变历史锁存；管理权移交按接管事务完成 |

### 4.1 移动止盈的完整候选公式

trailing_stop.enabled=true 时，激活要求 M≥activation_ratio×T；激活事实持久一次。drawdown_mode=POINTS 时 f=M−drawdown_value；PEAK_FAVORABLE_RATIO 时 f=M×(1−drawdown_value)，该值须 0<drawdown_value<1。线只可按更高有利变动收紧，x≤f 请求目标 0。未激活不得因 f≤0 意外触发。

这是便于实现的**新增公式候选**，不是从参考软件依赖名推断的行为，须在 B18 的移动政策中确认；不可在生产默用。保护开关关闭时仍完整验收移动止盈。若同事件激活 PROTECTED，先应用保护模式，普通移动出口不生效；已发出退出单不会被“模式变化”当成不存在。

### 4.2 半仓、二阶段与 Q0

候选数量原语为 CEIL_Q0_DIV_2_AND_4：

1. NORMAL 中 M 首次达到对应 trigger_ratio×T，登记触及事件和 WAITING_ENTRY_TERMINAL；冻结新增风险、撤本周期开仓残单，保留撤单期间潜在成交。
2. 相关开仓单终态、累计成交和归属持仓核对完成后，Q0 冻结为该周期实际持仓基准量，保存 reconciliation_id。Q0 不是 initial_open_quantity，也不在等待中反复改写。
3. 第一阶段剩余目标 ceil(Q0/2)；第二阶段剩余目标 ceil(Q0/4)。目标基于同一 Q0，不按“当前剩余再除二”漂移。阶段二必须启用且 trigger_ratio>first_trigger_ratio。
4. 第一阶段确认的实际减仓量>0 后，才把保本状态从 WAITING_FILL 变成 ACTIVE；只有报单或终态零成交不能激活保本。
5. Q0=1 时两阶段均 NO_OP，不发送 0 手、不加仓凑整；Q0=2/3/5 的目标见下表。
6. 触及后等待撤单/核对期间回落，开发候选保留已登记阶段；进入 PROTECTED 则抑制未消费阶段。此等待期语义须在 B18 明确确认后才能用于在线执行。

| Q0 | 第一阶段目标/拟减仓 | 第二阶段目标/新增拟减仓 |
| ---: | --- | --- |
| 1 | 留1/减0 | 留1/减0 |
| 2 | 留1/减1 | 留1/减0 |
| 3 | 留2/减1 | 留1/减1 |
| 5 | 留3/减2 | 留2/减1 |

在无保护抢先情况下，从0.3T直接到0.7T且 Q0 已证明，两个阶段可在同一事务登记消费，只提交更严格的最终目标，不能分两笔重复占量。若从0.3T直接到0.85T，先进入 PROTECTED，两个未消费阶段均抑制；不能为了“补齐功能”追补0.4T/0.6T半仓。

合并阶段目标后的实际成交按阶段一尚未完成的拟减量优先、再阶段二分配，每手只归一个阶段；阶段一拟减量为Q0−ceil(Q0/2)，阶段二为ceil(Q0/2)−ceil(Q0/4)。外部减仓保留外部来源，不伪装成阶段成交来激活保本。已PROTECTED后的原半仓晚到成交仍归原阶段，但保本保持SUPPRESSED_PROTECTED。

## 5. 一个事件内的处理次序与数量算法

以下是工程协调次序；交易原因的具体优先标签与应急覆盖仍由批准的政策表解析：

1. 验证事件来源/身份，归并可证明的成交及订单事实，更新实际持仓、预占与相关 revision。
2. 如有可用价格且持仓>0，更新 M 并先判断保护锁存；pending 锁存已影响同事件后续分支。
3. 收集适用的全退出原因：止盈、当前模式允许的止损/保护线、收盘、以及独立获批的人工/应急政策。
4. 只有没有适用全退出且仍处 NORMAL 时，处理移动/半仓/二阶段/保本。PROTECTED 抑制未消费阶段，不能让阶段动作先于该事件锁存。
5. 将各请求归并为唯一 target_remaining_qty；全退出为0，多个部分目标取最小。尚无减仓动作时为 null，不能把初始计划量误当补仓目标。目标一旦提交不可由重试或模式切换上调。
6. 原子提交原因、保护状态/禁开、阶段消费、退出目标及预占；CommitAck 验证后计算并提交子单。

正常可发送退出量=max(0,归属实际剩余量−目标剩余量−仍有效退出单的潜在剩余成交量−UNKNOWN退出占用)，再受当前可平今昨、数量上限和批准执行政策限制。先确定不重复目标，再合法化报价与拆分子单；拆分不能扩大总量。

举例：当前归属10手，已发全退单且成交3手，当前剩余7手，原单仍可能再成7手，则可新发0手。撤单期间又成2手，实际剩余5手；只有原单剩余终态及成交核对可靠，才可为5手另建子单。UNKNOWN不靠时间释放。任何“5秒到期”只推进检查/撤改，不直接重复原量。

退出触发时禁止本周期新增并处理开仓残单；其后晚到开仓成交仍归原计划。全退出目标0不会因新填数量被改成正数。若 Q0 冻结后出现与完成证据矛盾的新开仓成交，记录差异、保持禁开和原计划覆盖，重新核对后再推进数量；不能静默重算 Q0 或复用旧可平快照。

## 6. 时间、报价、滑点及异常政策

| 能力 | v1 可表达与必须校验 |
| --- | --- |
| 时段退出 | close_session 绑定 calendar_version/schedule_policy_id/advance_seconds；由版本化日历生成唯一时间事件。休市、午休、最终盘次及日切依B07批准，不按墙钟固定18:00清状态 |
| 报价与撤改 | execution_policy 首价为合法对手限价或获批被动限价；price_steps 每项 after_check_count 严格递增，OFFSET_TICKS 需 offset_ticks；最终涨跌停价只由合约元数据解析 |
| 尝试与期限 | 检查间隔、尝试上限必须显式，业务值另批；schema 的1024尝试/16步骤仅为工程资源上限，不是推荐交易次数或无限重试许可 |
| 今昨与数量 | close_offset_policy_id 确定真实通道的平今昨分配；不支持/未知时不造可平量，不超平 |
| 滑点退出 | 每次已确认开仓成交更新实际 VWAP 后检查方向化不利滑点，触发归同一退出目标；止损/滑点指标与实际成交事实分开 |
| 断流 | active_trading_gap_ms 只在批准的活跃交易范围内计时；单调钟计时，休市与价格不变不自动等于断流；无合法价/交易不可达只保持待退出、告警与接管 |
| 应急 | emergency_exit.approved_policy_id 解析触发、权限和保护覆盖范围；v1默认能力限 OWNED_CYCLE_ONLY，超过max_reduce_quantity拒绝，账户级操作必须走独立显式命令 |
| 存储失败 | 新风险冻结；减风险仅按另外已批准、已验收的存储故障降级计划；本ProtectionPlan不自行授予绕过CommitAck的发送权 |

父 policy id 均是受限数据引用，不是 SDK 订单执行器、TargetPosTask 或任意脚本。内核唯一 OMS 管理实际尝试，不能让 SDK 的目标仓位辅助器与内核各自持续补单。

## 7. 持久状态与计划修订

计划原文与 hash 不可变，动态状态分表，保存至少：

| 状态 | 含义 |
| --- | --- |
| mode / state_revision / last_observed_event_seq | NORMAL/PROTECTED、当前修订、最后已处理来源位置 |
| max_favorable_move / protected_event_seq | 已知最高有利变动及首次保护锁存事件；持久seq进度另记录 |
| q0 / q0_reconciliation_id | 基准量在完成证据前为null，冻结后不可因重复回报漂移 |
| phase1_status / phase2_status | NOT_TRIGGERED/WAITING_ENTRY_TERMINAL/TARGET_COMMITTED/EXECUTING/DONE/NO_OP/SUPPRESSED_PROTECTED |
| phase1/phase2_confirmed_reduction_qty | 实际成交归属的阶段减仓量，不把发送量当完成量 |
| break_even_status | OFF/WAITING_FILL/ACTIVE/SUPPRESSED_PROTECTED |
| trailing_activated_event_seq / trailing_floor_move | 移动激活证据与当前有利变动坐标下的收紧线 |
| target_remaining_qty / exit_target_revision | 前者只是exit_targets.desired_remaining_qty的同事务投影，后者映射exit_targets.state_revision；无退出目标时为null，提交后单调收紧 |
| quote/path quality / coverage gap / durable_seq | 质量统一CONTIGUOUS/DATA_GAP/UNKNOWN，仅描述可证明观察区间，不保证上游每Tick；缺口和已提交覆盖不以“已连接”代替 |

阶段 NOT_TRIGGERED→WAITING_ENTRY_TERMINAL→TARGET_COMMITTED→EXECUTING→DONE；Q0=1或阶段目标已被更早阶段满足则NO_OP；PROTECTED可将尚未消费阶段变成SUPPRESSED_PROTECTED。TARGET_COMMITTED/EXECUTING已有真实订单后即使保护激活也要保留消费/订单身份继续核对，不能重置成未触发。全部状态的最终 SQL 枚举与转换以[状态机](state-machines.md)、[数据模型](data-model.md)对齐，不允许消费者自建同名另一套真相。

exit_targets是唯一退出数量目标权威；保护、半仓、人工、收盘和应急都通过其收紧目标，不能直接修改protection_state中的镜像绕过协调。写入exit_targets与镜像、相关意图/预占须在同一TX完成；恢复发现两者不一致时标记核对限制，不自行挑选更方便的数量发送。exit_target_revision是对权威行state_revision的投影名，不要求在protection_state另存第二份版本。

PlanRevision（U4）须带 expected_plan_revision、expected_state_revision、迁移批准记录、旧新计划hash及事实屏障。先禁新增，核对归属与活动/UNKNOWN订单；事务内验证并写新计划版本、迁移记录、状态和新revision，失败不发布部分生效。理论价、周期身份、原成交、已锁存保护、M、已消费阶段、Q0、已确认减仓及已提交更严格目标不得清零或回退。PROTECTED中的计划调整也不得降低已生效保护底线或重新启用旧止损/普通移动；若业务要改变这一性质，必须另立有明确风险的业务变更，不叫普通修补。

新计划不能改变既有活动子单的数量/身份；需撤改的单仍保留旧关联直到终态。内核原语不能表达、OMS/解释器不可信或迁移不能证明安全时，维持禁开并故障接管，不能声称热更新已修复保护。

## 8. 可校验的开发计划示例

下列是可通过schema的**开发夹具**；数值为候选示例，config_hash是占位夹具身份，不是实际参数批准或通道配置。完整计划规范化/hash算法只以[IPC的确定性JSON规则](ipc-protocol.md)为准，根计划没有自身plan_hash字段。

```json
{
  "schema_version": "1.0",
  "plan_id": "plan-dev-001",
  "plan_revision": "1",
  "account_key": "DEV:fixture:account-1",
  "position_cycle_id": "cycle-dev-001",
  "contract_id": "DEV.TEST001",
  "trading_day": "2026-11-09",
  "direction": "LONG",
  "theoretical_entry_price": "1000",
  "initial_open_quantity": 5,
  "rule_set_id": "rules-proposed-fixture-1",
  "config_revision": "1",
  "config_hash": "0000000000000000000000000000000000000000000000000000000000000000",
  "execution_scope": "DEVELOPMENT",
  "approval_record_id": null,
  "market_quality_policy_id": "quality-fixture-1",
  "price_field": "LAST",
  "fixed_stop_loss": {"enabled": true, "distance_points": "20"},
  "fixed_take_profit": {"enabled": true, "distance_points": "100"},
  "profit_protection": {"enabled": true, "activation_ratio": "0.8", "floor_ratio": "0.5", "block_new_for_trading_day": true},
  "trailing_stop": {"enabled": false},
  "partial_reduction": {
    "enabled": true,
    "first_trigger_ratio": "0.4",
    "quantity_rule": "CEIL_Q0_DIV_2_AND_4",
    "q0_freeze_policy": "ENTRY_TERMINAL_AND_RECONCILED",
    "second_stage": {"enabled": true, "trigger_ratio": "0.6"},
    "break_even": {"enabled": true, "anchor": "THEORETICAL_ENTRY", "activate_after": "FIRST_STAGE_CONFIRMED_REDUCTION"}
  },
  "close_session": {"enabled": true, "schedule_policy_id": "schedule-fixture-1", "calendar_version": "calendar-fixture-1", "advance_seconds": 20},
  "slippage_exit": {"enabled": false},
  "data_gap_exit": {"enabled": false},
  "emergency_exit": {"enabled": false},
  "execution_policy": {
    "policy_id": "exit-fixture-1",
    "first_price_mode": "OPPONENT_LIMIT",
    "cancel_check_interval_ms": 5000,
    "max_child_order_attempts": 3,
    "price_steps": [
      {"after_check_count": 1, "price_mode": "OFFSET_TICKS", "offset_ticks": 1},
      {"after_check_count": 2, "price_mode": "EXCHANGE_LIMIT"}
    ],
    "close_offset_policy_id": "offset-fixture-1"
  }
}
```

fixture policy ids 必须由独立开发执行器登记，正式启动器拒绝DEV合约与DEVELOPMENT计划。以下是黄金事件夹具定义，JSON只描述输入/预期，不是可执行代码或真实成交证据；执行器按case_id对上方计划作明确覆写，并使用已登记的假事件来源。

```json
{
  "fixture_version": "1.0",
  "cases": [
    {"case_id":"G0-PP-001","overrides":{"partial_reduction":{"enabled":false}},"events":[{"type":"FILL","quantity":1,"price":"1000"},{"type":"QUOTE","price":"1080"},{"type":"QUOTE","price":"1060"},{"type":"QUOTE","price":"1050"}],"expect":{"mode":"PROTECTED","max_favorable_move":"80","target_remaining_qty":0,"block_new_for_trading_day":true}},
    {"case_id":"G0-PP-002","overrides":{"direction":"SHORT","partial_reduction":{"enabled":false}},"events":[{"type":"FILL","quantity":1,"price":"1000"},{"type":"QUOTE","price":"920"},{"type":"QUOTE","price":"950"}],"expect":{"mode":"PROTECTED","target_remaining_qty":0}},
    {"case_id":"G0-PP-003","overrides":{"initial_open_quantity":1},"events":[{"type":"FILL","quantity":1,"price":"1000"},{"type":"ENTRY_TERMINAL_RECONCILED","owned_quantity":1},{"type":"QUOTE","price":"1040"},{"type":"QUOTE","price":"1060"}],"expect":{"target_remaining_qty":1,"exit_submit_count":0,"phase1_status":"NO_OP","phase2_status":"NO_OP"}},
    {"case_id":"G0-PP-004","overrides":{"initial_open_quantity":2},"events":[{"type":"FILL","quantity":2,"price":"1000"},{"type":"ENTRY_TERMINAL_RECONCILED","owned_quantity":2},{"type":"QUOTE","price":"1040"},{"type":"EXIT_FILL","quantity":1,"price":"1040"},{"type":"QUOTE","price":"1060"}],"expect":{"phase1_target":1,"phase2_target":1,"additional_phase2_exit_quantity":0,"break_even_status":"ACTIVE"}},
    {"case_id":"G0-PP-005","overrides":{"initial_open_quantity":3},"events":[{"type":"FILL","quantity":3,"price":"1000"},{"type":"ENTRY_TERMINAL_RECONCILED","owned_quantity":3},{"type":"QUOTE","price":"1040"},{"type":"EXIT_FILL","quantity":1,"price":"1040"},{"type":"QUOTE","price":"1060"}],"expect":{"phase1_target":2,"phase2_target":1,"additional_phase2_exit_quantity":1}},
    {"case_id":"G0-PP-006","overrides":{},"events":[{"type":"FILL","quantity":5,"price":"1000"},{"type":"ENTRY_TERMINAL_RECONCILED","owned_quantity":5},{"type":"QUOTE","price":"1040"},{"type":"EXIT_FILL","quantity":2,"price":"1040"},{"type":"QUOTE","price":"1060"}],"expect":{"phase1_target":3,"phase2_target":2,"additional_phase2_exit_quantity":1}},
    {"case_id":"G0-PP-007","overrides":{},"events":[{"type":"FILL","quantity":5,"price":"1000"},{"type":"ENTRY_TERMINAL_RECONCILED","owned_quantity":5},{"type":"QUOTE","price":"1030"},{"type":"QUOTE","price":"1085"}],"expect":{"mode":"PROTECTED","half_exit_submit_count":0,"phase1_status":"SUPPRESSED_PROTECTED","phase2_status":"SUPPRESSED_PROTECTED"}},
    {"case_id":"G0-PP-008","overrides":{},"events":[{"type":"FILL","quantity":5,"price":"1000"},{"type":"ENTRY_TERMINAL_RECONCILED","owned_quantity":5},{"type":"QUOTE","price":"1100"},{"type":"SESSION_CLOSE_DUE"}],"expect":{"mode":"PROTECTED","target_remaining_qty":0,"half_exit_submit_count":0,"independent_full_exit_targets":1}},
    {"case_id":"G0-PP-009","overrides":{"partial_reduction":{"enabled":false}},"events":[{"type":"FILL","quantity":1,"price":"1000"},{"type":"QUOTE","price":"1080"},{"type":"COMMIT_ACK"},{"type":"KILL_STRATEGY"},{"type":"RESTART_GUI"},{"type":"QUOTE","price":"1050"}],"expect":{"mode":"PROTECTED","target_remaining_qty":0,"plan_identity_unchanged":true}},
    {"case_id":"G0-PP-010","overrides":{"profit_protection":{"enabled":false},"partial_reduction":{"enabled":false},"trailing_stop":{"enabled":true,"activation_ratio":"0.6","drawdown_mode":"POINTS","drawdown_value":"20"}},"events":[{"type":"FILL","quantity":1,"price":"1000"},{"type":"QUOTE","price":"1060"},{"type":"QUOTE","price":"1090"},{"type":"QUOTE","price":"1070"}],"expect":{"mode":"NORMAL","trailing_floor_move":"70","target_remaining_qty":0}},
    {"case_id":"G0-PP-011","overrides":{"partial_reduction":{"enabled":false}},"events":[{"type":"FILL","quantity":1,"price":"1000"},{"type":"QUOTE","price":"1080"},{"type":"KILL_KERNEL_BEFORE_COMMIT_ACK"},{"type":"RESTART_KERNEL_WITH_GAP"}],"expect":{"new_entry_allowed":false,"missing_latch_must_not_be_claimed_durable":true,"reconciliation_required":true}},
    {"case_id":"G0-PP-012","overrides":{"partial_reduction":{"enabled":false}},"events":[{"type":"FILL","quantity":1,"price":"1000"},{"type":"QUOTE","price":"1080"},{"type":"QUOTE","price":"1050"},{"type":"CANCEL_ENTRY_REQUEST"},{"type":"LATE_ENTRY_FILL","quantity":2,"price":"1000"}],"expect":{"target_remaining_qty":0,"owned_remaining_after_new_fill":3,"all_remaining_covered_by_original_plan":true,"cancel_request_does_not_release_unknown":true}}
  ]
}
```

每个开发执行器默认按事件完成必需事务再推进下一业务事件；G0-PP-011显式覆盖未确认窗口。FILL只增加已确认数量，QUOTE不伪造成交；ENTRY_TERMINAL_RECONCILED中的owned_quantity提供当前事实屏障，不提前冻结Q0，阶段触及时验证该屏障仍有效后才冻结；EXIT_FILL须由已存在的退出子单身份归入账本。简写事件需在开发用例展开为含event_seq/source_epoch、幂等键、当前revision及CommitAck的完整输入，不得直接暴露为生产菜单。

还必须实现以下反例：

| 验收ID | 输入/故障 | 必须断言 |
| --- | --- | --- |
| G0-PP-013 | 同事件重复/同成交重复；策略换版 | 只消费一次；机会、计划、保护不清零 |
| G0-PP-014 | 平仓已成3/10，撤单途中再成2，撤单UNKNOWN | 新退出量0；终态核对后才按剩余5推进 |
| G0-PP-015 | 首笔1手、残余开仓仍活动；阶段触及后等待/回落/保护抢先 | 无裸仓；Q0不提前冻结；候选等待语义明确；保护优先抑制阶段 |
| G0-PP-016 | 半仓只报单未成交、拒单、零成交撤成 | 保本不生效；实际减仓确认才生效 |
| G0-PP-017 | 价为null/NaN/过期、正常休市超过180秒、整机断网 | 不造价、不假强平、不复用全量退出，保留锁存及未知占用 |
| G0-PP-018 | U4迁移旧revision、PROTECTED→NORMAL、M回退或旧目标放宽 | 事务整体拒绝；历史事实及权限不倒退 |
| G0-PP-019 | schema多余字段、关闭分支残留参数、缺批准/跨账户计划 | schema或语义/权限明确拒绝，零发送 |
| G0-PP-020 | 空头理论1000实际995/1005 | 原始价差−5/+5，不利滑点+5/−5；方向不混用 |
| G0-PP-021 | POINTS与PEAK_FAVORABLE_RATIO、保护开/关，M突跳 | 两种完整公式各测，线不放宽，保护生效后抑制普通移动 |
| G0-PP-022 | 阶段一已在途时激活保护、原半平晚到 | 在途仍占量，原阶段不重置，不叠加全平超量 |
| G0-PP-023 | 数据库慢/失败、迟到CommitAck/旧bootAck | pending即占量；未提交不发送；不发布持久成功 |
| G0-PP-024 | 收盘限价升级、无对手价/涨跌停、尝试用尽 | 合法报价或待处理/告警；不无限追价，不称已成交 |

以上是待实现用例规格，不是已运行的产品测试或策略正确性背书。

