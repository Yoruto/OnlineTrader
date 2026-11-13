# DSV 非默认保护合同（dev5 / ProtectionPlan 1.1）

本页保留dev5的385项检查及1.1保护记录。dev6已经补齐独立1.2执行政策和撤单/重报价/追单，见[DSV执行合同](execution-dsv-contract.md)；本文末尾的撤改未实现范围已由该文更新，原候选和报告不改写。

日期：2026-11-09。范围：独立 OnlineTrader 的纯离线保护实现、计划编译和回归验收。本文补充 [DSV 静态证据](strategy-dsv-evidence.md)，不替换 G0 的 1.0 合同。DSV 归档只作静态分析；本轮未运行参考程序、柜台或 SDK，未验证在线交易结果。

当前版本为 `0.1.0.dev5`。最终完整检查 **385项通过，0失败、0错误、0跳过**，比dev4新增69项；确切证据见[结果](../output/checks/20261109T122558Z-95b49b6d/result.json)及[逐项日志](../output/checks/20261109T122558Z-95b49b6d/check.log)。检查期间产品源码稳定，编译、导入边界、冻结G0资源与1.1扩展资源完整性均通过，`pip check`无依赖冲突。dev4源码事先归档于`output/baselines/20261109T102218Z-fd88ab0a/dev4-source.zip`，SHA-256为`d3a451bc4a43b49595b82f9901bb5f1bc7e19c0f5730f4ac3850902ce4dde492`。

交付dev5离线wheel，SHA-256为`1b946728240fb01092a1e013c3fb54ec99c651bd45943bf926ba8888cd9e5637`。[构建及干净安装审计](../output/artifacts/20261109T122640Z-e4d3b7c5801d/wheel-audit.json)确认48份Python源码与5份资源在最终检查、wheel及全新安装中逐字节一致。无Qt、QA或交易SDK的独立core环境以`-I`、`--no-index`安装；默认策略激活被拒绝，显式开发变体激活仍禁开，授权后仅记录1次STRATEGY请求、确认成交为0。纯解释器以独立匿名事件验证5手两档剩余目标`3→1`及真实减仓后保本，没有撮合或生成柜台成交。

## 版本、身份与批准边界

- 新 DSV 编译器生成 `schema_version="1.1"`、`execution_scope="DEVELOPMENT"`。1.0 计划仍按原 schema 和原 reducer 分支执行，不原地改变已有持仓的算法。
- 计划绑定账户、持仓周期、合约、交易日、方向、理论入场价、配置修订及内容哈希。候选的品种 O/S/T 必须与所选不可变 `DsvProfile` 一致；缺失、跨范围或非法输入不能靠 DSV 的吞异常默认值补齐。
- 保存设置的用户确认是参考配置来源。完全相同的基线可保留参考 `approval_record_id`；隔离开发变体的计划该字段为 `null`，描述符标记 `ISOLATED_DEVELOPMENT_VARIANT`，另存基线哈希及参考确认来源。参考确认不授权变体，也不授予柜台权限。
- 本轮不增加 IPC 指令、在线行情/日历调度、账户连接、自动启动或交易授权。默认内核不能因持有此计划而启用交易。

## 记号与参数编译

`A` 为计划理论入场价，`T>0` 为固定止盈点数，`S>0` 为固定止损点数；多头 `pf=P−A`，空头 `pf=A−P`。`H=max(0,历次有效 pf)`。价格与比率按精确十进制处理；仅明确标注的半仓数量采用 ties-to-even。

| 保存字段 | 1.1 编译及零值语义 |
| --- | --- |
| `half_close_enabled` | 半仓总开关。关闭时二阶段与半仓保本子开关休眠，不可独立触发。 |
| 品种 `half_close_ratio` | 首档触发比 `r1`，不是数量比例。只有 `0<r1<1` 才启用；显式 0 保留并关闭该品种半仓。严格 profile 已含每个品种值，全局值不覆盖它。 |
| `half_stage2_trigger_ratio` | 先夹取至 `[0.05,0.95]`；显式 0 得 0.05。若结果 `r2<=r1`，改为 `1.5*r1`，允许结果大于 1；固定 TP 可先于该档退出。 |
| `half_breakeven` | 通过显式保存开关编译；启用时锚定 `THEORETICAL_ENTRY`，激活条件为 `FIRST_STAGE_CONFIRMED_REDUCTION`。 |
| `protect_80_enabled`、`protect_80_ratio`、`protect_keep_ratio` | 保护激活比显式 0 回退 0.8，保留比显式 0 回退 0.5。允许保留比高于激活比，可能在激活观察当轮满足退出。 |
| `trailing_activate_ratio` | `<=0` 关闭普通 trailing，不按 0 当轮激活。正值启用。 |
| `trailing_pullback_ratio` | 显式 0 保留。普通 trailing 激活后，零回撤阈值可在当轮触发全退。 |
| `stale_force_close_seconds` | 0 关闭断流退出；正数编译为 `floor(seconds*1000)+1` 毫秒，用整数 `>=` 表达源程序严格 `>`。默认 180 秒对应 180001ms。 |

`half_stage2_ratio` 的旧兼容变量不替代实际二档触发字段。旧 `half_pullback_close` 的迁移及回落分支不在本轮 profile/计划中，不能据字段名自动开启。

保存值保持原12位小数上限；只有派生二档比率允许13位，以准确表达`1.5*r1`，例如`0.900000000001 → 1.3500000000015`。1.1的普通回撤比率限定在`[0,1]`，负值不能经手工构造计划绕过schema或解释器检查。原1.0数值合同不改。

## 半仓状态、数量与成交屏障

| 行为 | 保留的 1.0 语义 | DSV 1.1 语义 |
| --- | --- | --- |
| 数量 | 冻结 `Q0` 后，首档剩余目标 `ceil(Q0/2)`，次档 `ceil(Q0/4)`。 | `quantity_rule="DSV_ROUND_HALF_CURRENT"`；每档按当前有效剩余量计算。 |
| 触发依据 | 历史最大有利移动 `H`。 | 尚未触发的新阶段按当前 `pf`；历史高点不能使回落后的下一阶段新触发。 |
| 单次观察 | 可同时锁存、提交两档。 | 一个新行情观察最多消费一档；超过两档线的首次观察只消费首档。 |
| 待终态阶段 | 保留原有屏障。 | 已锁存的 `WAITING_ENTRY_TERMINAL` 不因随后回落丢失，屏障满足后按该已锁存阶段推进。 |

1. 首档条件为 `pf>=T*r1`；首档已消费、二阶段启用后，下一次有效观察才可按 `pf>=T*r2` 触发次档。等号成立。无效、缺口或重放行情不能制造下一次机会。
2. 开仓余单必须终态、成交明细与累计量一致，并完成显式持仓核对，才可冻结 `Q0` 和提交半仓目标。`Q0` 在 1.1 保留为成交归属/屏障证据，不作为固定二分之一、四分之一的算术基数。
3. 首档基数为当时自有实际量，并受已有剩余目标约束；次档基数再取首档已提交目标的较小值。因此首档仍在途时可以收紧次档目标，不能把未成交量当作已成交。
4. 对基数 `V>=2`，每档 `close=min(max(round_half_even(V/2),1),V−1)`，目标为 `V−close`；`V<2` 不提交零量委托，也不消费该档。

| 初始实际量 | 首档平 / 余 | 次档平 / 余 |
| --- | --- | --- |
| 1 | 0 / 1 | 0 / 1 |
| 3 | 2 / 1 | 0 / 1 |
| 5 | 2 / 3 | 2 / 1 |
| 7 | 4 / 3 | 2 / 1 |

阶段可经历 `NOT_TRIGGERED → WAITING_ENTRY_TERMINAL → TARGET_COMMITTED → EXECUTING → DONE`。提交目标代表阶段已消费；发送返回、撤单请求或 UNKNOWN 均不代表成交。确认量只由去重后的真实自有减仓成交累计；外部减仓不证明计划首档完成。

新阶段只能由新的有效行情触发；开仓终态核对只能物化已有`WAITING_ENTRY_TERMINAL`阶段，重复核对不得用同一报价消费二档。相同来源身份的行情及时间倒退行情也不再转发为新保护事件，不更新可执行报价的时效。

保本从 `WAITING_FILL` 转为 `ACTIVE` 只需首档归属确认减仓量 `>0`，不必等整档全部成交。保本线为理论 `A`，多空均在 `pf<=0` 时请求全退。若成交使保本刚激活，只能用所有者提供的仍有效新鲜行情立即复核；该成交复核不能顺便消费二阶段。

## 保护优先与普通 trailing

每次有效观察先更新 `H`，再判断利润保护。`H>=T*activation_ratio` 时锁存 `PROTECTED`；保护线为 `A±T*floor_ratio`。锁存不因回落、重启、策略暂停或更换 GUI 消失。

- 同一观察同时满足保护与半仓时，先保护，未消费半仓阶段标为 `SUPPRESSED_PROTECTED`，保本也受抑制；已经提交的半仓目标、在途单和真实确认量继续保留。
- 保护模式抑制普通 trailing、原始固定 SL 和保本退出。固定 TP 继续有效；保护线在 `pf<=T*floor_ratio` 时全退。即使普通 trailing 更早激活，后续保护锁存也会抑制它。
- 普通模式先考虑固定 SL、固定 TP、有效保本等全退原因，再判普通 trailing。1.1 半仓可以先提交目标，同观察的全退随后把共同目标收紧至 0，不能产生两套退出数量。
- 普通 trailing 在 `H>=T*trailing_activate_ratio` 时激活，模式为 `DSV_MAX_TP_AND_PEAK_RATIO`：`pb=max(T*q,H*q)`，`floor=H−pb`，`pf<=floor` 即退出，含等号；记录的 floor 只向更保护的方向收紧。这不是单独使用 `H*q` 的回撤算法。

例：`T=50,q=0.1,H=42`，`pb=5,floor=37`；多头 `P=A+37`、空头 `P=A−37` 均触发，尚差半 tick 不触发。

## 日停与断流事实

日停是账户/合约/交易日的新开门禁；周期 `block_new`、暂停/撤销入场上下文和恢复核对是独立工程门禁。某个日停开关为 false，只表示该原因不额外锁日停，不清除其他原因或自动重新武装。

| 开关 | 锁日停时机 |
| --- | --- |
| `protect_stop_day` | 利润保护首次激活时；false 仍锁保护与保护线。 |
| `stop_day_on_trailing` | 普通 trailing 退出条件触发时；不等发送返回或成交。 |
| `entry_slip_abort_stop_day` | 启用滑点退出，且不利滑点严格超过 `max(2,S*(ratio or 0.4))` 时；等号不退。 |
| `stale_close_day_stop` | 断流退出触发时仅锁存 `gap_day_stop_pending`；必须真实减仓成交使自有量归零后才锁日停。目标为 0、委托发出或发送返回均不足。 |

1.1 `DATA_GAP` 必须证明 `active_trading=true`，给出非负整数 `gap_ms` 和 `session_elapsed_ms`，且 `gap_ms<=session_elapsed_ms`；不能用超出已证明会话时长的静默时间触发退出。

触发需同时满足计划开启、已有自有量、`session_elapsed_ms>=60001`（源严格超过 60 秒宽限）、`gap_ms>=active_trading_gap_ms`、正确的策略标识和显式授权。编译器把保存的秒数乘以 1000、向下取整再加 1。因此配置 180 秒的首个可触发值是 180001 毫秒，正好 180000 毫秒仍不触发。授权的断流退出可以覆盖 `PROTECTED`，不再要求另行设置保护覆盖标志；此项不改变 1.0。

没有合法价格时，只记录 `ALERT_AND_TAKEOVER`、阻断新增并要求恢复核对，不虚构价格、退出委托或已平事实。缺口观察会清除旧可发送报价；即使离线事实称有合法价，也需新的有效报价经过所有者校验后才派发原退出目标。

## 所有者、持久化与故障不变量

- 单所有者修改状态，单 writer 写 SQLite。目标/订单意图必须先提交并收到本次正确 Ack，才可调用端口；事务回滚、旧 boot Ack、错误 Ack 均不得派发。
- 每个周期只有一个 `target_remaining_qty`，只可取更小值；所有半仓、TP、SL、保本、trailing、收盘及断流退出共享它。
- 可派发新增减仓量为 `max(0,owned−target−所有未终态减仓潜在量)`。UNKNOWN、在途和撤单请求继续扣占；只有终态证据及一致的累计成交明细才能释放占量，禁止靠超时释放。
- 先取消开仓余单，再用终态与持仓核对屏障推进半仓。屏障后晚到开仓使核对失效并进入恢复门禁，但原计划、已提交目标和 UNKNOWN 占量保留；重核后按原目标补足，不能放宽目标。
- 重启恢复原计划修订、阶段/确认量、保护模式、trailing floor、剩余目标和潜在占量；默认 disarmed，不重放未知发送。策略生命周期或新配置不得删除、替换既有保护。
- 已激活的保护/trailing 或已消费的阶段不可经配置更新重新定义。参数变体影响后续周期；旧周期仍按原不可变计划执行。

## 当前离线输入 API

`DsvStrategyRuntime(kernel, profile=isolated_variant)` 接受不可变 `DsvProfile`。必须显式创建开发内核并配置隔离开发端口；测试使用 `RecordingPort`。`activate()` 注册独立开发配置/策略 epoch，保持 disarmed；测试需显式 `arm_development()`，再通过 `on_market(event,account,instrument)` 进入正式信号、风控、计划和持久化链。变体不会伪造“保存设置已确认”。

`Kernel.observe(cycle_id,last,bid1=...,ask1=...)` 提交有效报价；`record_fill`、`confirm_terminal` 和 `reconcile_development_cycle` 提交隔离开发成交/终态/核对证据。生产 IPC 不暴露这些测试注入能力。

`Kernel.observe_development_event(cycle_id,event)` 仅允许以下两类事实，仍走相同持久化与 Ack 后推进链。它要求开发模式及可用隔离端口，不提供墙钟、日历推断或定时器。

| `event.type` | 调用方事实 |
| --- | --- |
| `DATA_GAP` | `active_trading`、`gap_ms`、`session_elapsed_ms`、`policy_authorized=true`、`policy_id=plan.data_gap_exit.data_quality_policy_id`、`legal_price_available`。仅“有合法价”布尔值不能代替可发送报价。 |
| `SESSION_CLOSE_DUE` | 已由外部日历证明到期；显式传匹配的 `schedule_policy_id`。当前 API 不实现在线日历调度。 |

所有者填入事件序号、唯一事件身份及账户/合约/持仓周期绑定，不接受调用方借此获得其他周期权限。异步调用后使用 `wait_idle()` 接受提交结果；记录端口请求只是离线派发证据。

## 与参考程序的显式工程差异

| 静态证据边界 | 本实现处理 |
| --- | --- |
| `HALF_BREAKEVEN` 只见读取全局、未见定义；异常可能在目标已发送后被吞掉。 | 使用已保存 `half_breakeven` 开关及真实首档确认减仓屏障，保留 capability note；不声称源程序实跑已通过。 |
| 普通退出 `close_booked` 早退可能遮蔽 trailing 日停分支。 | 按已保存 `stop_day_on_trailing` 在明确退出触发时落实日停，记录修复说明。 |
| DSV 发目标后立即推进虚拟 stage/volume；超时账户同步可把 volume/target 改回 actual。 | 保留按已提交虚拟剩余进行次档数量计算的语义，同时分离真实成交；终态屏障、单调 target、UNKNOWN/晚到保护始终有效，不复制松回目标。 |

以上定位见静态证据文档的“非默认保护扩展证据”。`capability_notes(profile)` 留存兼容边界；未复制原业务源码、字节码、账户或交易数据库。

## 回归范围与后续边界

| 回归层 | 当前文件与重点 |
| --- | --- |
| 纯 reducer | `tests/test_dsv_protection.py`：36 项；多空/奇数数量、一次一档、当前 pf、真实成交保本、保护抑制、max 回撤、零值、非正报价、日停、60 秒/严格 gap、旧版本与持久化。 |
| 内核整合/故障 | `tests/test_dsv_protection_runtime.py`：14 项；Ack 前无发单、回滚/错误 Ack、UNKNOWN 占量、晚到开仓、在途次档、撤单累计不一致、重启、正式策略链及真实 flat 后断流日停。 |
| 计划编译 | `tests/test_dsv_plan.py`：21 项；1.1 参数/零值/严格边界、二阶段修正与派生比率13位精度、负回撤拒绝、原计划与变体批准隔离。 |
| 开发变体 | `tests/test_dsv_profile_variants.py`：4 项；开发端口权限、实现/基线身份绑定、改参不能刷新已消耗 bar，旧profile重建回执不能证明新profile路径完整。 |
| 行情身份 | `tests/test_dsv_market_protection_identity.py`：2 项；重复或时间倒退行情不推进二阶段、不刷新可执行报价时效。 |
| 终态屏障 | `tests/test_dsv_protection_barriers.py`：2 项；重复核对不消费新阶段，已等待的阶段在回落后仍可核对提交。 |

上述项目数是当前各专项文件的总数，其中编译文件包含历史10项；本轮净增69项。最终全量385项的确切报告见本文开头，测试输入均为明确的匿名开发事实，不是柜台成交证据。

本轮未实现隐藏旧字段 `half_pullback_close` 的迁移/回落配置、在线断流/会话调度、原 DSV 自适应撤改及追单算法；当前仅支持合法对手限价首请求，5000ms 撤单检查为开发执行策略。未提供在线性能、柜台成交、跨进程故障或实际交易兼容验收结论。
