# DSV名义统计与停开合同

本页归档dev8名义统计范围。dev9新增自有成交资金收益与只读报表展示，当前验证见[资金报表合同](money-reporting-contract.md)；本页旧源码和wheel身份继续保留。

适用版本：`0.1.0.dev8`。本合同约束离线开发中的原生DSV策略周期统计，依据[源程序静态证据](strategy-dsv-evidence.md#统计与停开名义点数确认边界及源缺陷)及当前`strategy/statistics.py`、`kernel/statistics.py`实现。用户确认的DSV业务口径继续适用；本页列明真实成交确认边界及源缺陷修复，不扩大柜台或资金结算授权。

最终完整独立检查 **597项通过，0失败、0错误、0跳过**，比dev7新增66项；见[结果](../output/checks/20261109T154208Z-2ddd73da/result.json)与[逐项日志](../output/checks/20261109T154208Z-2ddd73da/check.log)。报告SHA-256为`e70777bdfe4e0aeb41abaa086509380136703fb6962404b5eb807a354b901afe`。编译、产品导入边界、冻结G0/1.1/1.2资源完整性与检查期间源码稳定性通过；`pip check`无依赖冲突。新增测试分别为纯域30项、运行器20项、owner边界16项。

dev7的61份产品文件在修改前与531项报告逐字节匹配后归档到`output/baselines/20261109T150703Z-c0be488a/dev7-product.zip`，SHA-256为`cffb8a278ee10b864075e3f8f59a5d2b01bcb84821328b1d575b6e92a201b266`。旧报告、wheel和原始DSV确认资源继续保留。

## 接口与作用域

纯域接口位于`online_trader.strategy.statistics`，不读写数据库或生成成交：

| 接口 | 输入与结果 |
| --- | --- |
| `initial_state(account_key, contract_id, trading_day)` | 生成`DSV_STATISTICS/1`聚合。账户、合约、显式`YYYY-MM-DD`交易日共同确定作用域。 |
| `apply_event(profile, state, event)` | 验证原配置、计划、周期及证据身份，归集`ENTRY_CONFIRMED`或`CYCLE_CLOSED`；返回新state、receipt、duplicate及source_notes。 |
| `rollover_day(state, trading_day)` | 只对显式推进的交易日重置日计数，保留原方向冷却；反向推进报错，同日不重置。 |
| `cooldown_gate(state, direction, now_utc, elapsed_ms=None)` | 解释明确的UTC/单调经过时长事实；返回阻断原因及剩余时长，本身不授予开仓权限。 |

事件共同携带账户、合约、交易日、周期ID、计划ID及hash、原profile hash、方向、普通/延续入场类型、理论入场价、S和T。入场另外要求原请求量及首笔确认量；结案另外要求实际flat、订单终态核对完成、退出原因及结案时刻，相关原因还要求冻结参考价、原退出触发时刻或不利滑点点数。金额和点数使用有界十进制定点文本，数量为有界整数；禁止NaN、隐式浮点换算和未声明字段。

owner接口位于`online_trader.kernel.statistics.StatisticsOwner`：`enroll`核对周期原配置，`prepare_entry/prepare_close`准备事务内派生结果，`freeze_exit`冻结首次全量退出的原因和参考价，`accept`只发布已接受提交回执对应的结果，`entry_reasons`合并本地停开/冷却/核对屏障，`snapshot`返回已提交统计。它们由单一内核owner调用，不能从GUI或行情线程直接写统计。

## 周期纳入与配置身份

只纳入本轮新建、`entry_origin=STRATEGY`、其原配置修订为`DSV_DEVELOPMENT_EXECUTION`且保存完整profile的原生周期；candidate的profile hash必须与配置吻合。周期保存`statistics_profile`，后续结案仍使用该原profile与原计划距离，不能用当前profile覆盖S/T或历史退出原因。

既有未纳入周期、外部账户持仓、普通手工开仓、导入数据库和不完整旧状态不会自动补账。统计snapshot的scope为`LOCAL_ENROLLED_OWNED_DSV_CYCLES`。同账户/合约/交易日共享日聚合；切换`NEXT_CYCLE`profile不新建一份空白日额度，也不解除已锁定的当日或本月停开。策略热切换业务状态包含统计及周期身份，重启或换profile不能把它们丢弃。

## 首次真实入场与数量

订单目标、派发返回、记录器收单和pending均不产生入场统计。首笔已验证属于本周期的真实`OPEN`成交，在与成交事实相同的事务中产生一次`ENTRY_CONFIRMED`：`trade_count+=1`，`total_volume+=原请求量`。后续部分成交只更新实际持仓数量，不再增加该周期的入场统计。

为保留DSV源口径，`total_volume`是每周期首次确认时累计的请求量，不能标注为成交量。snapshot另给`actual_confirmed_entry_quantity`和`actual_confirmed_exit_quantity`，从纳入周期的真实成交累计量计算；例如请求3手、首笔1手时，`total_volume=3`，真实入场量为1。真实成交价、成交分配和订单剩余数量继续由原成交台账管理。

## 退出原因、点数公式与结案

普通名义收益`pf=方向符号×(冻结退出参考价−理论入场价)`，LONG符号为+1，SHORT为−1。普通退出用首次有效全量退出触发时的价格；`PROTECTED_FLOOR`用原计划保护比例对应的保护线。随后报价或实际平仓价不得改写该首次原因与参考价。名义点数不乘数量或合约乘数；半仓阶段不独立计次或按平仓比例加权最终普通点数。

| 结案原因 | 当日名义收益点数变化 | 净亏点数变化 | 计数变化 |
| --- | --- | --- | --- |
| `STOP_LOSS`、`BREAK_EVEN` | `+pf` | `+S` | trade、stop、stop_streak各+1 |
| `TAKE_PROFIT` | `+pf` | `−T` | trade、profit各+1，stop_streak=0 |
| `TRAILING_STOP`、`PROTECTED_FLOOR` | `+pf` | `−pf` | trade、profit各+1，stop_streak=0 |
| `SESSION_CLOSE` | `+pf` | `−pf` | 不改trade/stop/profit/streak |
| `ADVERSE_SLIPPAGE` | 0 | `+已冻结不利滑点` | 不改上述计数 |
| `DATA_GAP_EXIT` | 0 | 0 | 不改上述计数 |
| `MANUAL_EXIT` | `+自有已确认平仓均价对理论入场的方向点差` | 上述点差取负 | trade、stop各+1，profit/streak不变 |
| `NO_FILL` | 0 | 0 | 不改计数；只记录未成交周期终结回执 |

净亏允许为负，盈利形成后续亏损缓冲，不夹到0。`daily_pnl_points`与`net_loss_points`不是互为相反数：止损按配置S、普通止盈按配置T，名义当日收益仍按冻结pf。已确认入场不能作为`NO_FILL`结案；没有原入场确认的周期不得产生其他结案统计。

全量退出触发只冻结口径，不立即记结案。`reconcile_development_cycle(close=True)`要求所有相关自有订单已证实终态、报告成交量与本地成交明细一致、无UNKNOWN、实际自有持仓为0且无待复核矛盾，才准备一次`CYCLE_CLOSED`。flat但尚未核对、部分退出、撤单请求、未确认终态及退出重报价均不得提前加点或计次。

## 单writer事务与幂等

沿用现有SQLite journal和单writer，无新表、DDL或第二个统计写入通道。入场派生结果与真实成交、结案派生结果与周期关闭事实，分别随同原`TX_FILL`/`TX_RECONCILE`事务写入`event_journal`的`nominal_statistics`载荷。提交失败不发布派生结果；只有owner接受身份匹配的提交回执后，committed snapshot才可见。结果准备、事务排队和内存staged状态不等于提交完成。

归集身份为`position_cycle_id + 事件阶段`，与传输event_id/evidence_id名称无关。相同内容重复返回原回执，改变同一业务身份的内容报冲突；改名不能多算一次。纯日state有4096条回执容量边界，owner从完整journal恢复持久周期/阶段回执，不能因日切、重启或内存缓存更新遗忘历史去重身份。重复已关闭核对返回既有结果，不生成第二份统计。

确认成交的派生统计计算、计数或容量出错时，仍提交真实成交及其数量/保护变化，记录`statistics_review_required/statistics_error`并撤销开仓资格。数量事实完整时，保护退出继续受原内核控制；不能因统计错误删除保护或拒收真实成交。已结案周期出现新成交、或成交与原已确认终态矛盾时，保留新事实并进入原事实核对屏障，不能悄悄重开统计回执或自动补旧账。当前没有自动历史统计修正接口；需后续显式、可审查的修正合同，结案错误也不能靠重新提交任意价格掩盖。

## 日停、月停与源缺陷修复

显式交易日推进后日点数、日计次、streak、延续止损日标记及普通日停复位；保留冷却原起点/时长/方向。已结旧日周期仍按原计划交易日归集，不以机器自然日0点重标身份。原续开`cont_day`是每日一次机会消费标记，由原策略机会状态保存，不能用入场是否成交来重新发放机会。

确认归集后，以`net_loss_points>=S×max_net_loss_stops`派生净亏日停，等号已达限；入场前仍重新核对。只有这种净亏阈值触发日进入去重stop_days，保留最近12条；其他日停原因不自动记成亏损停机日。月停按明确完整的交易日序列检查最近N个交易日全部已达限；覆盖不足时报告`TRADING_CALENDAR_COVERAGE_INCOMPLETE`并保留已达限日停，不能确认新的连续月停，也不用weekday猜节假日。

已锁定本月身份在后续交易日独立阻断开仓，直到显式交易日进入新月；换profile不能清除该锁。解除旧月标记不覆盖其他当日停开原因。这修复源DSV日切可能清day_stopped、而同月month_stop检查直接跳过导致月停削弱的缺陷。

策略自有`MANUAL_EXIT`只根据已分配本周期的平仓成交均价计算每单位名义点数；保留源无条件增加stop/trade计数的规则，在回执写`MANUAL_NOMINAL_SIGN_AND_UNIT_REPAIRED`。不复制源外部累计close_profit反号和把货币加到点数变量的缺陷，不接入外部账户汇总补账。

## 冷却原起点与时钟

默认开关false、预填5分钟；关闭时保存分钟数不激活冷却。普通止损/保本止损结案归集后，冷却以**原退出触发时刻**起算，不以晚到成交、关闭核对或重启时刻重新计5分钟。只阻断最后止损同方向，反方向不因此阻断；到期相等允许。仅保留一个源方向，后一次有效止损替换前一次方向冷却。

同boot、相同调度单调时钟来源且有效时优先按原触发单调经过时长判断；重启后恢复持久UTC触发点和deadline，不使用旧boot单调值、不延长冷却。跨交易日仍保留原起点/时长/方向。时钟有效性与恢复资格继续受既有调度/开仓屏障约束，冷却到期本身不使DISARMED、未知单或恢复中的内核获得开仓权限。

## 开发API的时间事实约定

涉及纳入统计周期的开发API应明确本次事实发生/观察时间。省略可选时间时，owner读取**本次当前时钟**；若已绑定调度器则使用其当前有效sample，其他情况读取当前UTC及本boot单调时钟。不得沿用周期原入场或上次报价保存的旧时间。历史离线回放必须显式传入该事实的UTC，不能依赖开发机当前时间，也不能把时刻隐含在成交ID或交易日字符串中。

| 开发入口 | 时间参数与语义 |
| --- | --- |
| `Kernel.observe(...)` | 可选`observed_at_utc`，标记本次价格观察。冻结退出原因时以该次观察时间起算，不能回用旧入场时间。 |
| `Kernel.observe_development_event(...)` | 可选`observed_at_utc`，标记本次明确的开发保护事件。 |
| `Kernel.record_fill(...)` | 可选`observed_at_utc`，标记本次自有确认成交观察；首笔成交可能直接触发滑点退出，必须使用该次事实的时间。 |
| `Kernel.manual_close(...)`、`Kernel.flatten(...)` | 可选`statistics_at_utc`，标记本次人工退出触发；如已冻结首次全量退出则不改写原起点。 |
| `execution_event` | 载荷中的`observed_at_utc`经统一`ClockSample`严格验证，执行事件不能用宽松时间解析绕过其他入口的时钟合同。 |
| `Kernel.reconcile_development_cycle(..., close=True)` | 可选`statistics_at_utc`为本次关闭核对时刻；省略时取本次owner当前UTC，不沿用周期旧入场、旧成交或旧quote时刻。 |

显式UTC必须满足严格UTC格式和可解析日期边界，不能使用无时区文本或非UTC偏移代替。退出原触发与关闭核对是两个不同事实；关闭时刻不得早于退出触发时刻。晚核对不重启冷却，早于触发的历史核对也不能通过篡改时间完成结案。

owner恢复的完整历史receipt与纯域receipt执行相同内容冲突检查：即使内存日state已滚动，原周期/阶段的经济内容改变仍报`E_DUPLICATE_CONFLICT`；仅更换传输event_id/evidence_id不能绕过或产生第二份统计。该检查与时间验证修复均须纳入最终源hash匹配的独立验证结果，不能沿用变更前的测试报告或wheel审计。

## 离线验证与交付边界

本版本仅使用匿名、显式development事实；本地记录器只记录请求，不生成成交或柜台结果。默认内核保持`UnavailablePort`、DISARMED，确认DSV参考配置不激活交易。本合同不覆盖在线柜台接入、真实账户资金对账、手续费、税费、资金曲线或财务盈亏报表；snapshot的`currency_pnl=None`，不能将名义点数乘一个乘数后称为已验证人民币收益。

交付[dev8离线开发wheel](../dist/20261109T153510Z-32ab9701045c/online_trader-0.1.0.dev8-py3-none-any.whl)，SHA-256为`cefba85b687796fae00844b25364fcf10a09d8ddf496ebdf26643837cac27932`。[构建与干净安装审计](../output/artifacts/20261109T153510Z-32ab9701045c/wheel-audit.json)通过，报告SHA-256为`a886cc08504eb8867c297f131f4a89ad0c394ef6e3995e6a1bb29f41e24a0199`。57份Python源码及6份资源在最终597项报告、wheel和全新安装中逐字节一致，构建期间源码保持不变；未覆盖旧wheel或报告。

全新core环境使用`-I`和本地wheelhouse以`--no-index`安装，无Qt、QA、测试夹具或交易SDK。默认`UnavailablePort`、DSV激活拒绝和DISARMED检查通过，原dev7显式日历/单调调度边界继续通过。新增安装后真实内核检查使用匿名显式开发事件：入场分笔1+2手只计一次，源请求量3手与实际量分别显示；STOP触发参考价1035、实际平仓1034，在flat未核对时stop仍0，核对后stop为1、当日名义点数−20、净亏16；2份持久回执在重启和重复原成交后不变，`currency_pnl=None`。记录器没有生成成交；全部成交均由检查显式给出。

此开发wheel与源码检查不构成Windows安装包、在线模拟/实盘、财务报表、长稳、性能或生产发布验收。后续仍需实现完整统计展示、受控修正/外部成交对照及资金费用账本，并按新授权另行验证柜台事实。
