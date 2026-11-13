# 离线时钟、日历与自动保护调度合同（dev7）

本文为dev7归档验证记录。当前dev8新增的名义统计、停开与冷却闭环及其最终检查见[统计合同](statistics-contract.md)，不继承本页旧源码/构建身份。

日期：2026-11-09。范围：独立OnlineTrader在明确开发模式下，用显式日历及UTC/单调钟事实驱动已有1.2执行和保护链。默认内核保持禁开；本轮没有启动DSV、交易SDK、柜台、Windows服务或后台线程。本文补充[DSV静态证据](strategy-dsv-evidence.md)、[1.1保护](protection-dsv-contract.md)及[1.2撤改追单](execution-dsv-contract.md)。

当前版本`0.1.0.dev7`。最终完整检查 **531项通过，0失败、0错误、0跳过**，比dev6新增73项；见[确切结果](../output/checks/20261109T141850Z-c791ed6d/result.json)及[逐项日志](../output/checks/20261109T141850Z-c791ed6d/check.log)。编译、导入边界、冻结G0/1.1/1.2资源完整性和检查期间源码稳定性均通过，`pip check`无冲突。dev6的57份产品文件事先与458项报告逐字节核对后归档于`output/baselines/20261109T135300Z-d3a7a5fb/dev6-product.zip`，SHA-256为`5f0121adfee28b539f474ae0a7eecccf6a6f1e3770a12cd9415113597d7b638c`。

交付dev7离线wheel，SHA-256为`77a483de01cfb0aa8cea6dc62d49e5c1c837a67c6be7a01629f1ab6e16ee476b`。[构建/干净安装审计](../output/artifacts/20261109T141442Z-3025eafacc6c/wheel-audit.json)确认55份Python源码与6份资源共61份产品文件，在最终检查、wheel及全新安装中逐字节一致。无Qt/QA/交易SDK的独立core环境以`-I`、`--no-index`安装；默认激活拒绝，显式日历/开发钟激活仍禁开，授权后只记录1次OPEN、确认成交0；无新报价再经过60001ms只记录1次撤单，原arrival不变、5手潜在量仍保留。没有撮合或生成柜台成交。

## 模块、版本与所有权

| 模块 | 职责 |
| --- | --- |
| `scheduling/calendar.py` | 不可变显式日历、完整覆盖、会话和明确强平端点；不生成工作日、节假日或柜台交易日。 |
| `scheduling/clock.py` | 严格UTC/单调钟样本、漂移与倒退检测；没有网络校时、定时器或账户权限。 |
| `kernel/scheduler.py` | 唯一owner上的`ScheduledRuntime`，保存报价身份/接收年龄，按单调期限驱动收盘、断流和撤改单等待。 |
| 既有内核及执行器 | 先持久化并接受正确Ack，再进入原唯一发送链；原保护、同一退出目标、UNKNOWN及恢复门禁继续有效。 |

本轮不修改冻结G0、1.1及1.2资源，也不新增公开IPC注入、交易SDK入口或自动启动权限。带`DSV_TIME/1`的日历解析是新调度路径；原来未携带该政策的裸开发事件仍按其既有分支解释。日历、时钟或调度对象的存在不构成规则批准、账户四事实完整或自动武装。

激活描述符保存日历ID、版本、内容hash、**完整日历记录**、调度代码hash、轮询间隔和漂移阈值；纳入独立开发配置身份。它能在恢复时核对原事实内容，不只依赖一个可变文件名。保存DSV基线及B01–B20参考确认不会被改写。

## 显式日历合同

`TradingCalendar.from_record(record)`只接受`schema_version="TRADING_CALENDAR/1"`：

| 字段 | 合同 |
| --- | --- |
| `calendar_id`、`calendar_version` | 明确ID与版本；内容身份还必须检查`content_hash`，相同名称不证明内容相同。 |
| `complete`、`coverage_from_utc`、`coverage_to_utc` | 明确bool及非空半开覆盖区间`[from,to)`；UTC必须以Z结尾，最多6位小数。 |
| `contracts` | 每个`contract_id`单独声明会话列表；未列合约不是已证明休市。 |
| 会话必填字段 | `session_id`、`trading_day`、`open_utc`、`close_utc`、`is_opening_session`、`force_close_at_end`；两个开关必须明确bool。 |
| 会话边界 | `open<=now<close`；按合约排序，不得重叠、倒退、重复ID或越出覆盖。不同合约可同时交易。 |
| 柜台交易日 | 直接使用输入标签；夜盘可跨本地或UTC日期。不能用weekday、18:00或本机日期替代它。 |
| 强平端点 | 只有`force_close_at_end=true`才是该时段政策的退出端点。午间休市等普通盘段结束明确为false，不默认强平。 |

例如，已证明的09:00–10:15、10:30–11:30、13:30–15:00三段，可分别把`force_close_at_end`设为false、false、true；这是输入事实示例，不是本产品提供的真实交易所日历。夜盘终盘、CFFEX债券15:15与DSV硬编码15:00的来源差异，必须由明确日历端点解决，不能取helper名称作裁决。

`query(contract,utc)`返回完整性、`is_trading`、当前/下一会话、活跃时的明确交易日、距离开/闭盘的整数毫秒，以及日历ID/版本/hash。会话记录带同样引用。活跃时`next_forced_close_utc`只在当前明确交易日内寻找下一个强平端点；它可跳过10:15或11:30等非强平休市点。

已覆盖合约的显式空会话列表可以证明该覆盖区间休市。缺合约、覆盖外或`complete=false`返回`quality=UNKNOWN`、`is_trading=null`，不把未知改成休市。`active_milliseconds`只累计完整区间内与会话的交集；盘间空档贡献0，缺完整范围时拒绝计算。记录和查询结果均为深拷贝，不能靠修改返回对象改变已验证日历。

## UTC、单调钟与故障锁存

`ClockSample`包含`now_utc`和字符串`monotonic_ns`。UTC用于定位日历；执行经过时间、报价年龄和检查间隔由单调钟推进。纳秒以带符号64位上限内的非负整数字符串保存，毫秒边界由整数运算得到，不靠浮点累计。

`SystemClock`只读取本地UTC和单调钟。可注入具有`sample()`方法的独立开发时钟；测试使用明确推进两个时间轴的`ManualClock`，不会调系统时间或生成行情。

`ClockGuard(max_wall_drift_ms=1000)`比较相邻样本的`UTC增量−单调增量`。绝对差严格大于阈值时锁存漂移故障；任一时间轴倒退也锁存故障。阈值是可配的**工程检测参数**，不是用户新确认的交易规则、柜台时间精度或校时保证。等于阈值不单独构成超限；一旦故障锁存，后续正常样本不会自行解除。

调度入口和`Kernel.poll()`先采样检查，再接受待处理Ack。发现时钟倒退、漂移、非法样本或provider异常时，立即撤销新增权限和上下文，使原待Ack开仓不能乘该窗口派发；已有周期进入恢复门禁，随后写入故障事实。保留原计划、保护模式、退出目标、所有订单身份和未知容量，不以故障当作已取消或已平仓。

`reset_clock()`要求当前事务已接受、原管理事实已核对且无UNKNOWN。显式重置清除本地测量故障和报价/期限缓存，继续禁开；需要新的有效事实与独立授权。它不完成账户查询、重建缺失行情路径，也不重放旧发送。

## DSV时间语义与原周期期限

调度器将已解析会话绑定成`session_policy`：`schema_version="DSV_TIME/1"`、日历hash、`force_close_at_end`和新信号使用的`entry_close_utc`。该政策参与行情内容指纹；相同source身份换了日历政策会报内容冲突，不能借此刷新同K线机会。

| 场景 | 日历解析后的行为 |
| --- | --- |
| 普通开盘等待 | 09:00、09:30、21:00等明确开盘分类与保存等待参数配合；默认等待5分钟时，`elapsed<=5分钟`仍等待，边界含等号。午间恢复段不自动冒充新的开盘窗口。 |
| 临近普通休市 | 不把10:15、11:30结束当日终；午休前10分钟仍可按其余规则准入，不因该休市点套用收盘禁开。 |
| 临近明确退出端点 | 新信号按保存的`no_open_before_close_minutes`和下一个明确强平端点门禁；默认10分钟。持仓按计划`advance_seconds`推进退出，默认提前20秒。 |
| 原周期日历绑定 | 新周期冻结calendar hash、原entry time、计划hash、schedule/calendar/gap引用及该周期首个`forced_close_utc`。未来配置或新会话行情不能重写该绑定。 |
| 已错过原退出期限 | 下一盘新报价不能把旧期限顺延到下一端点。共同退出目标仍收紧至0，`close_start_elapsed_ms`锚定原应启动时刻；已经超过原收盘阶梯时直接使用MARKET限价模式，不另起20秒。 |
| 跨日或日历未知 | 当前完整日历与原绑定不匹配、来源进入其他柜台日或覆盖未知时，禁开并要求核对；不清旧日保护和UNKNOWN，也不据本机18:00复位。 |

延续开等源专用路径仍按已验证策略状态机处理，不能把普通开盘或所有盘段的门禁拼成一条臆造规则。旧未带时间政策的开发事件兼容分支不冒充此次完整日历解析。

`bind_existing_cycle(cid)`只为原1.2周期解析原日历引用。必须已有原入场时间证据、原暴露已核对、无UNKNOWN，且该入场时间有匹配合约/交易日的完整覆盖。已存在绑定时只允许同内容解析，不能替换为另一个日历hash或退出端点；需要变更既有管理政策时应另走受限迁移。手工旧周期缺原入场时间时明确`E_UNSUPPORTED`，不从当前钟或最近报价补造起点。

## 独立轮询、报价与期限

`ScheduledRuntime(..., interval_ms=500)`在已有owner线程上工作。`Kernel.poll()`驱动附着的调度器，也可显式调用`driver.poll()`；默认500ms是检查节奏，没有新增线程、服务或操作系统定时保证。owner不被调用时没有虚构已执行的动作；下次推进必须按原时刻补算期限，不能把迟到检查当成新的起点。

新source身份的真实行情才能替换缓存。原`received_ns`及相应时钟样本用于报价年龄；source时间过旧、scope不符、重复身份异内容均拒绝。相同source事件不更新arrival、不延长新鲜度，也不拖延单调检查。墙钟provider只产生时钟事实，不产生QUOTE。

无新报价的pulse使用缓存值及其**原接收时刻**，以`_observe_quote=false`进入既有执行器。它可以推进60秒入场确认、30秒撤单终态等待、收盘计价/退出目标等时间动作；不能把旧价重放成新行情，不能抬高盈利高水位、推进另一档半仓或重新锁存一次价格激活。

保存的未确认入场期限保持严格`elapsed>entry_confirm_timeout`，默认超过60000ms才请求撤余单；已有真实部分成交不按“未确认入场”强平。撤单经过30000ms仍无一致终态进入持久`WAIT_RECONCILIATION`，不释放原潜在量或盲补。发送、取消及替代订单仍先提交、接受本次正确CommitAck，再进入原端口；定时Future不表示柜台接收或成交。

报价过期时可以锁存应退出目标，但不能据缓存价派发；后来收到合法新价才继续原目标。两限价、tick、完整性、当前会话与当前授权仍分别核对；未知不是0，也不是自动无限有效。

## 活跃盘段断流

仅当前完整活跃会话检查断流。当前盘段quiet取“当前session经过时间”与“距最后新source报价接收的单调时间”的较小值，等价于从`max(session_start,last_quote_receipt)`起算。报价source年龄用于可发送新鲜度，不冒充当前接收后的断流时长。

默认断流须quiet严格超过180秒，并且本盘已经严格超过60秒宽限；整数计划分别为180001ms和60001ms。休市不触发该出口；新盘重新以其开盘起点限制quiet，不能把整个午休累加成新盘已断流。价格不变但收到新合法source更新，也不等于断流；重复旧source则不能替它续期。

无合法价格或交易不可达时，只记录`ALERT_AND_TAKEOVER`、禁开与恢复要求，不伪造可发送价格或已平仓事实。已有PROTECTED模式、原计划及UNKNOWN继续保留；一旦得到合法新价/完整事实，仍按原目标管理。可选断流日停继续要求真实平仓归零的确认，目标0或定时器到点不等于完成。

## 开发API与验证边界

```python
calendar = TradingCalendar.from_record(explicit_calendar_record)
driver = ScheduledRuntime(kernel, calendar, clock=clock_provider,
                          profile=immutable_profile,
                          interval_ms=500, max_wall_drift_ms=1000)
driver.activate()  # 显式开发激活，仍为DISARMED
kernel.wait_idle()
# 另行取得开发授权和明确行情/账户/合约事实后：
future = driver.on_market(event, account, instrument)
kernel.wait_idle()
driver.poll()  # 或由既有owner循环调用kernel.poll()
```

一个内核最多一个调度owner，且须显式DEVELOPMENT及隔离开发端口。`activate`、`on_market`、`poll`、`status`、`bind_existing_cycle`和`reset_clock`均不提供联网、撮合或事实查询。恢复后报价缓存为空；旧boot报价/上下文不恢复新鲜度，未知订单不重播发送。时钟重置和日历解析也不会自动arm。

本轮定向分组为日历23项、时钟20项、调度owner21项、会话/期限边界6项及scope3项，共73项。对应测试文件为`test_trading_calendar.py`、`test_clock.py`、`test_scheduled_runtime.py`、`test_schedule_boundaries.py`及`test_scheduler_scope.py`。owner组包含明确核对后重置仍禁开、历史报价不得复活，以及时钟故障跨重启锁存；这些用例与原458项共同纳入本文开头的531项最终全量。以上输入均为明确匿名开发事实，不是柜台查询或成交证据。

真实交易所日历和柜台交易日、时间同步质量、SDK状态/回调、今昨仓offset及在线断流恢复仍属于G2等后续验证。没有安装服务、设置自动启动或新增自动授权。本轮不改变V7冻结状态，也不把离线检查继承为线上模拟、真实交易、长稳或性能通过。
