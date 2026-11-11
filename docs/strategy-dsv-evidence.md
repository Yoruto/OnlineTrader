# DSV入场与风控：静态兼容证据

日期：2026-11-09。用途：按用户“全部按DSV为准”的确认，为正式入场与风控的独立实现提供可复核行为定位。本文件只记录参数含义、表达式、边界和定位，不包含原程序源码或业务字节码。

## 来源、方法与边界

- 参考程序：`C:/Users/xyc/Desktop/trading/trading.exe`，SHA-256 `aabeafd83d265ab9b1cf5cc3fd0e9b0f21963ddcc95daa70feefc209997269a3`。
- PyInstaller业务归档入口为`gui`，995361字节；入口序列化内容SHA-256 `07c82c9ada765284ed5a929227024cbf192f5f72192636900740d9a5764032ea`。
- 只读CArchive并在内存中以独立CPython 3.14.7执行标准库marshal/dis解析；分析解释器位于output/tools，不改变产品CPython 3.13环境。没有exec/import业务代码，没有运行DSV、SDK或柜台，没有将原字节码写入产品或归档分发。
- 函数、源行与指令offset取自该入口代码对象；这是静态控制流证据，**不是运行时交易验收**。外部全局注入、异常数据、SDK事件顺序和实际成交效果未经运行验证。
- 当前安全配置及确认来源仍见[DSV设置快照](references/dsv-settings-20261109.json)、[用户确认](b01-b20-recommendation-confirmation.md)。配置比日志最后一些行更新；旧日志的临时参数不能替代当前保存值。

## 可直接实现的规则表达式

记号：O=本品种开仓偏移，S=止损偏移，T=止盈偏移，A=候选理论触发价，E=权益，P=最新价，mpl=每手保证金。原程序用float；新产品应使用精确数值并保留已说明的比较、取整语义。

| 项目 | 静态证据结论 | 定位 |
| --- | --- | --- |
| 多空触发与几何 | 多触发=日低+O，空触发=日高−O；多止损≥空触发、空止损≤多触发，等号允许。几何提示没有在该段写day_stopped，按当前数据重评；“今日不建仓”文字不构成永久锁定证据 | `StrategyThread._run` 行7993–8027；日志行8/9/11/12 |
| 跑过激活位 | act_off=T×(保护开启时protect_80_ratio，否则trailing_activate_ratio)。日高≥多触发+act_off标long_used；日低≤空触发−act_off标short_used。对应方向实际触线时拒绝，并写当前last_signal_bar | `_run` 行8034–8062，offset72540–73502 |
| 滞回0 | 品种h=`safe_num(hysteresis_points) or safe_num(stop_offset)`，当前品种0因此回退本侧S；不是关闭 | `_run` 行8112–8113，offset75078–75184；行8108同样用于提示 |
| 滞回helper | h≤0或armed为真：返回ready=true和原armed；未armed，多P≤A−h或空P≥A+h时返回(ready=true,armed=true)，否则ready=false。触线还需后面的入场条件 | `hysteresis_ready` 行1743–1760，关键offset46–242 |
| 跳空helper | 当T/A/P为正时，`abs(P−A)≥T×ratio`为真；是绝对距离，含等号。无效/异常输入原helper返回false | `opening_jump_block` 行1732–1740，offset92–200 |
| 普通信号方向 | 优先多：P≥多触发且long_ok；否则空：P≤空触发且short_ok；不是必须发生一次真实穿越 | `_run` 行8133–8139 |
| 60秒K线 | 订阅`get_kline_serial(trade_sym,60,data_length=500)`；lb来自当前最后一根K线的datetime | `_run` 行4915–4916，offset11386–11454；行7989，offset71392–71456 |
| 同K线去重 | 每个品种state只有一个last_signal_bar，选direction前先比较其与lb相等则跳过。因此多空共用同一分钟机会，不按方向拆key | `_run` 行8027，offset72482–72514 |
| 机会消耗 | 追价拒绝、资金不足、未更新触发价等路径也写last_signal_bar；发送前再写last_signal_bar、last_order_bar与last_order_direction。不是仅“成功准入才消耗” | `_run` 行8202、8244、8287、8295、8302、8309、8324、8332、8341–8344 |
| 触发更新开关 | 开关开启且本次方向等于stop_last_dir、旧触发价为正、abs(旧触发−当前A)<1e−6时拒绝，并消耗当前bar。保存开关false时跳过此门禁 | `_run` 行8187–8203，offset77084–77604 |
| 3%和5% | 处于开盘CHASE_WINDOW_MINUTES窗口内用chase_slip_ratio，否则entry_slip_ratio；当前分别0.05/0.03。普通入场cap=`max(1.0,round(O×ratio))`，round无ndigits。CHASE_BASE_NOW在初始化直接置false | `_run` 行4004；行8212–8221，offset77664–77780 |
| 双价有利豁免 | 多用ask与last；adverse为>A+cap的价，favourable为0<P≤A的价。adverse为空**或**favourable非空即通过；否则拒绝，over=max(adverse)−(A+cap)。空用bid与last，adverse为0<P<A−cap，favourable为P≥A，同样规则 | `chase_within_cap` 行1695起，关键offset162–368和472–678 |
| 双价边界 | 等于A±cap不算超限。任一价有利可以覆盖另一价不利超限，因此不是简单“任一超限拒绝”的OR，也不是只检查两价同时超限的AND。原helper缺价按0，异常默认通过；新产品不可把这个默认当完整行情证据 | 同上 |
| 成交不利滑点 | 多=实际成交价−理论entry_level；空=理论entry_level−实际成交价 | `_run` 行7314–7315，offset54238–54332 |
| 滑点退出阈值 | cap=`max(2.0,S×entry_slip_abort_ratio)`；开关真、滑点来源有效且slip严格>cap才退出。保存ratio0.4，但有2点下限；等于不退 | `_run` 行7347–7350，offset55240–55366 |

## 手数与风险：真实分支优先级

| 项目 | 静态证据结论 | 定位 |
| --- | --- | --- |
| 合计风险参数 | EFF_RISK_CAP=`min(max_risk_ratio,total_margin_cap,1.0)`；当前为min(0.85,0.7,1)=0.7 | `_run` 行4131–4133，offset6378–6458 |
| 动态品种份额 | tot=len(symbol_config)或1；state中day_stopped为真，或net_loss_points≥该品种S×MAX_NET_LOSS_STOPS且阈值>0的品种计为stop；active=max(1,tot−stop)，份额比=EFF_RISK_CAP/active | `sym_margin_ratio` 行4009起，offset6–510 |
| 每手保证金 | 优先quote.margin正值；缺失或非正时原程序估算P×volume_multiple×0.15。后者是DSV兼容估值，不是实际账户保证金率证明 | `calc_volume` 行5250–5255、5270–5280 |
| 每手风险与风险手数 | risk_per_lot=S×volume_multiple；budget=E×risk_per_trade_ratio；risk_vol=int(budget/risk_per_lot)，当前比例0.002。无额外2tick或费用储备 | `risk_based_volume` 行5173起，offset240–406 |
| 固定量先执行 | order_volume>0时，先返回max(min(固定量,MAX_VOLUME,floor(available/mpl)),0)。该早返回绕过后续risk_ratio、合计/品种预算、风险手数与live手数cap。该分支异常处理甚至可返回min(固定量,MAX_VOLUME) | `calc_volume` 行5246–5263，offset238–860及异常handler2734–2810 |
| 自动量风险度 | 固定分支之后，若account.risk_ratio≥EFF_RISK_CAP则返回0；激进模式仍经过此门禁 | `calc_volume` 行5264–5265，offset862–916 |
| 合计已用 | used=account.margin+account.frozen_margin+pending_margin_total | `calc_volume` 行5282–5285，offset1154–1314 |
| 品种已用 | 取api.get_position(state.trade_symbol or sym).margin；sym_budget=E×动态品种份额−该品种margin | `calc_volume` 行5288–5293，offset1318–1524 |
| 非激进预算 | budget=min(sym_budget,E×EFF_RISK_CAP−used)；allow=min(account.available,budget)，margin_qty=int(allow/mpl)，vol=min(margin_qty,MAX_VOLUME) | `calc_volume` 行5299–5308，offset1548–1796 |
| 激进预算 | budget仅为sym_budget，忽略all_budget；allow仍min(available,budget)，不受后面的风险手数缩量 | `calc_volume` 行5294–5297、5310 |
| 风险手数缩量边界 | 只有非激进、风险比例>0、risk_vol>0且risk_vol<vol时才vol=risk_vol。**risk_vol=0不把vol缩成0或1**，会保留保证金口径数量 | `calc_volume` 行5310–5323，offset1798–2244 |
| 保底一手 | 在margin_qty≤0或最终vol≤0时调用；开关开启且available≥mpl、E×EFF_RISK_CAP−used≥mpl才返回1。它不再复核单品种份额 | `_min_one_lot` 行5195起，offset398–654；调用5305、5325 |
| 临时保证金预占 | 原程序pending_margin超过30秒会删除。这不能作为新内核释放UNKNOWN或未终态预占的证据 | `_pending_margin_total` 行5146起，offset128–178 |

上述原分支可能绕过某些风险限制；它们应被如实记录为DSV兼容输出，不能悄悄换成旧推荐规则。独立内核的未知占用、账户事实完整性、实际可用/合法价量、唯一发送与提交屏障属于另外的工程合同，新增实现需分别留证，不能声称原EXE已有这些合同。

## 实盘保护、到期与月停

| 项目 | 静态证据结论 | 定位 |
| --- | --- | --- |
| 实盘保护总开关 | LIVE_PROTECT=保存live_protect AND account_mode为live；当前保存false，因此2手/5000元不生效 | `_run` 行4043–4046，offset4740–4846 |
| 实盘手数cap | calc_volume末尾仅LIVE_PROTECT真时生效；LIVE_ONE_LOT真取1，否则max(int(live_max_lots),1)，缩量后返回。固定量早返回分支绕过这一段 | `calc_volume` 行5328–5338，offset2280–2672 |
| 日亏口径 | LIVE_PROTECT且limit>0时，pre_balance>0则today=balance−pre_balance；否则today=close_profit+float_profit−commission | `_run` 行6050–6057，offset22802–23120 |
| 日亏动作 | today≤−abs(limit)锁定_live_daily_stop，只停止当日新开；原持仓照常止盈/止损/收盘。不是自动全账户强平 | `_run` 行6058–6068，offset23122–23434 |
| 到期阈值 | left=(expire_datetime前10字符解析成当天00:00−datetime.now()).days；left≤min_days_to_expiry即no_new_open。是自然时间差截整，含当天时刻，不是交易日计数 | `_run` 行7939–7944，offset70144–70348 |
| 月停定义 | 当日net_loss_points达到S×MAX_NET_LOSS_STOPS时把交易日记入stop_days（保留尾12项）；从当日向前找连续N个is_trading_day日期，若它们全部在stop_days，month_stop=当前年月并品种日停。不是月内任意累计4日 | `_run` 行6019–6040；行6037生成器offset14–68 |
| 月停解除 | 当前交易日年月与已存month_stop不同则清旧month_stop；只在原day_stopped_reason等于“本月连续停机停开”时一并清该日停原因 | `_run` 行6007–6018 |

## 首分钟、滞回调用顺序与静态缺陷

1. 普通入场开盘等待检查位于行7876–7890，比首分钟观察块更早；保存5分钟等待开启时，符合等待窗口会直接跳过本轮，不能假称当前EXE已经“等待与观察并行”。
2. 首分钟观察块在direction仍为None、选多空之前。其预期条件为0≤开盘经过秒数≤60且not need_hyst；满足时先对**两条触发线分别调用绝对距离跳空helper**，任一为真会设置整个品种day_stopped。不是仅对最终候选方向检测。
3. 同块half条件相对触发线：P≥多触发+0.5T或P≤空触发−0.5T则open_half_reached=true；未达且abs(P−任一触发线)≤1.5tick则need_hyst=true。
4. need_hyst真时，按P<(多触发+空触发)/2选择long探测，否则short；调用hysteresis_ready。若不ready，写当前last_signal_bar并跳过；ready后才在8133开始选择真实信号方向。
5. hyst_armed只在会话观察key改变时初始化false，helper返回后更新；静态扫描没有独立的触发线变化重置。会话key包含trading_day与session起始时刻。
6. **静态潜在缺陷**：8070把经过秒数存为局部`_secs_open`（offset73764），8080却读取全局`_secs_o`（73990）。在gui全部代码对象中未发现_secs_o赋值。该读取由exception表[73824,74084)保护，异常handler85916在8131捕获Exception，随后跳过首分钟/滞回代码块，直接到普通信号选择。当前没有执行DSV来证明或排除外部全局注入，故只能报告静态潜在缺陷。

`hysteresis_ready`在gui中仅找到上述行8116一处调用。新产品若采用正确的显式经过秒数输入来执行这些已定义helper，应把这项修复列为工程差异，并测试绝对距离双边检查、开盘等待顺序和同bar消耗；不得把吞异常或未定义全局作为新产品默认容错。

## 日志与state交叉证据

`logs/trade_2026-11-09.log` SHA-256 `7cf899075adf122f551c75bd8d8b2bf80b94d73f3a6791b455bba340e33661e6`。第8/11行打印“振幅>2×开仓偏移−止损”，第9/12行打印多触发=日低+偏移、空触发=日高−偏移，与静态几何一致。该日志中没有可用的滞回、追价、滑点、保底一手、风险度/保证金公式运行解释；不能用缺日志证明未实现。

daily state只读取键结构，未复制资金、订单或交易身份。每品种存在daily_pnl、net_loss_points、stop_count、profit_count、trade_count、total_volume、daily_order_count、commission_total、day_stopped、day_stopped_reason、stop_streak、cont_day。state结构不能单独证明各计数更新算法。日志曾显示白银非零参数，但早于当前配置修改时刻，不能据此把当前白银0/0/0补成非零。

## 尚未由本证据确定的范围

名义损益各退出原因的完整更新表、连续止损复位、全部半仓/移动/保本执行分支、收盘撤改全流程和多条异步实际成交归并，没有在本次限定读取中完整追踪。对这些行为不得从字段名或旧候选猜测；本文件也不提供在线、性能或交易可用性结论。

## 非默认保护扩展证据（2026-11-09，dev4之后）

本节继续使用上列同一个归档及独立3.14解析器，只读标准库静态解析，没有执行业务代码。本节补足上节原先未追踪的半仓、普通移动止盈、保本及相关日停分支；收盘撤改全流程、SDK实际成交事件顺序仍不在已验证范围。以下“源意图”和“源状态写入”分别记录，不能把发出目标或本地字段变化当成成交。

### 参数解释、数值零与半仓数量

| 项目 | 精确规则 | 定位 |
| --- | --- | --- |
| 半仓总开关 | `half_close_enabled`控制整个半仓段；二阶段或保本子开关不能独立绕过总开关 | `_run`3964、7608–7610，offset3198–3250、61956–62116 |
| 首档触发比 | 先取品种`half_close_ratio`；仅值为None时回退全局。非None直接float，品种0保留0；0<ratio<1才进入半仓段。该ratio是相对T的浮盈触发比例，**不是减仓数量比例** | `_run`7598–7610，offset61710–62116 |
| 全局首档回退 | 初始化使用全局字段，缺省0.4；零值保留0，转换异常才回退0.5。当前严格profile的每个品种都已显式给值，全局不是其强制覆盖值 | `_run`4059–4061，offset5082–5154、81314–81342 |
| 二档触发比 | 优先`half_stage2_trigger_ratio`，缺省回退旧`half_stage2_close_ratio`，再缺省0.6；显式0先保留0，随后夹取到[0.05,0.95]，故有效值为0.05；转换异常回退0.6 | `_run`4081–4085，offset5292–5440 |
| 两档点数 | p1=T×品种首档比，p2=T×有效二档比；若p2≤p1，改p2=1.5×p1，等号也修正。不是简单保证二档比大于首档比的拒绝策略 | `half_stage_points`1833–1845，关键offset24–170 |
| 旧二档字段 | 另有`half_stage2_ratio`初始化变量HALF_STAGE2_RATIO，但归档中只见STORE，无LOAD；不能据这个旧字段替换实际二档触发规则 | `_run`4070–4074，offset5158–5234、81374–81382 |
| 每次半仓数量 | 当前volume<2或数量ratio≤0时返回0；否则n=int(round(volume×ratio))，返回min(max(n,1),volume−1)。业务调用把数量ratio固定传0.5，使用Python无ndigits的ties-to-even，且至少保留一手 | `half_close_lots`1820–1830，offset88–228；`_run`7630–7632，offset62544–62612 |
| 数量例子 | 当前3手先平2余1；当前5手先平2余3，若第二档再平2余1；当前7手先平4余3，第二档再平2余1。该结果与固定ceil(Q0/2)、ceil(Q0/4)不同 | 同上，按精确整数与0.5推导 |

源helper对异常有吞异常/默认零返回；独立内核仍须拒绝非法身份、价格、配置及数量，不能以这些回退伪造合法输入。

### 阶段触发、一次观察与保本

| 项目 | 精确规则 | 定位 |
| --- | --- | --- |
| 前置条件 | 有direction、当前volume≥2、entry_price>0，且st.protect_on不为真；source即使half总开关为真，进入保护后也抑制半仓 | `_run`7608–7610，offset61956–62116 |
| 阶段兼容 | stage=int(safe_num(half_stage))；若stage≤0但旧half_closed为真，则视为stage1 | `_run`7618–7620，offset62200–62344 |
| 首档 | stage≤0、p1>0、当前浮盈pf≥p1时触发。使用当前pf，不是曾经的最高浮盈 | `decide_half_close`1867–1870，offset232–278 |
| 二档 | 仅stage==1且half_stage2真时考虑。先判断p2>0且pf≥p2；满足则触发第二档 | `decide_half_close`1871–1875，offset280–342 |
| 回落旧分支 | 二档未触发时才考虑pullback_close且p1>0且pf≤p1；在stage2开关为false时提前返回，因此该回落不独立于二阶段 | `decide_half_close`1872–1877，offset294–388 |
| 回落迁移限制 | `_run`读取旧`half_pullback_close`字段，缺省False；`load_config`若发现该字段严格为True且没有half_pullback_migrated，会把它改为False并写迁移标记。不能据helper存在就把当前配置默认开启回落功能；当前DsvProfile也不包含该字段 | `load_config`2486–2491，offset1584–1748；`_run`4087，offset5442–5494 |
| 一轮最多一档 | 每个品种本轮只调用一次decide_half_close，无循环重评；即使pf同一轮超过p1和p2，stage0只做首档，下一轮才可能做二档 | `_run`7621–7637，offset62346–62738 |
| 源推进时机 | 调用set_target_volume(±left)返回后立刻half_closed=True、half_stage=旧stage+1、volume=left、last_order_time=当前墙钟；没有在此处等订单终态或减仓成交 | `_run`7633–7638，offset62614–62786 |
| 保本意图 | 半仓目标之后，若HALF_BREAKEVEN为真且尚未break_even，则break_even=True、stop_price=本轮entry_price；此处的锚不是实际出场价或浮盈零点重算 | `_run`7640–7642，offset62792–62884 |
| 保本静态潜在缺陷 | gui全部代码对象中，HALF_BREAKEVEN只见上述一次LOAD_GLOBAL，没有STORE、参数或对应配置初始化。读取在目标和阶段字段已修改之后，外层Exception处理可能吞掉未定义异常。未运行DSV或验证外部全局注入，不能声称保存开关已在实际运行生效 | `_run`7640；半仓异常handler7658–7659，offset85558–85582 |

本轮浮盈pf在7540先按direction相对entry_price计算，半仓/保本后没有重算pf。普通止损检查位于半仓后；因此保本意图一旦落实，后面的stop_hit读取更新后的stop_price。阶段≥2或当前剩余量<2时helper不再发半仓目标。

独立实现应把配置开关显式绑定到已冻结profile，将“提交阶段目标”和“真实减仓确认”分开：目标可按源数量口径收紧，但成交、保本生效证据及剩余可平量必须由内核唯一事实源管理。固定q0、入场终态屏障、唯一剩余目标和未知预占不得因源程序先改volume而取消。这是工程约束/静态缺陷修复，不能说成源EXE已具备相同成交屏障。

### 账户同步对虚拟剩余的影响

`_run`6322–6329先按st.direction和st.volume计算expected多空数量。actual数量不同，且now−last_order_time≥POSITION_ACT_GRACE才进入同步；宽限= max(3.0,position_sync_seconds)，当前2秒配置得到3秒（4418–4420，offset6816–6892）。

半仓刷新last_order_time，所以宽限内可继续用刚写入的虚拟剩余，在下一轮触发二档。宽限届满仍不一致时，source的多头分支6508–6514重写volume=actual_long、entry_price=账户开仓均价并重算SL/TP，空头6533–6556相同。数量差异被标为手动改仓，分支还会把target设置回actual（6528–6530、6547–6549）。因此源程序并没有一个稳定的“二档一定按首档已成交剩余”合同；未成交超时可能恢复数量/目标，不能把这种恢复当作新内核允许放宽已提交退出目标的依据。

### 保护与普通移动止盈的顺序

1. 7540计算pf，多为last−entry_price，空为entry_price−last；7543–7544仅当pf更高时更新H=highest_profit_points。
2. 7551–7557先处理保护激活：总开关真、尚未protect_on、T>0、H≥T×protect_ratio，则锁protect_on并固定保护线A±T×keep_ratio。protect_ratio显式0回退0.8，keep_ratio显式0回退0.5（3958–3959）。
3. protect_stop_day只在**新激活的这一轮**决定是否写day_stopped；不是每轮重写，也不是等待保护线退出再停开。False仍照常锁保护和保护线（7566–7568，offset61056–61090）。
4. 7576计算protect_active=bool(总开关且protect_on)。随后先跑半仓（其自身直接检查!st.protect_on），再跑普通trailing激活。普通trailing激活要求!protect_active、!TRAILING_DISABLED、尚未激活且max(pf,H)≥T×trailing_activate_ratio（7663–7667，offset63264–63402）。trailing_activate_ratio≤0初始化为disabled（3951–3954，offset2588–2650）。
5. 退出判定7680–7718：保护模式先判固定TP，再判保护线，抑制原SL；非保护先判SL。仍无退出原因才判固定TP，再无原因且普通trailing已激活、未被保护抑制、未disabled，才判移动回撤。
6. 普通回撤阈值pb=max(T×pullback_ratio,H×pullback_ratio)，当H−pf≥pb时退出，**含等号**（7713–7716，offset64594–64734）。pullback_ratio初始化没有or默认，显式0保留0，因此激活后H−pf≥0可当轮满足（3955，offset2652–2704）。

上述抑制不要求两个激活比例相等：普通trailing可以在较低激活比先锁上，但后续保护一旦激活就抑制其退出判断。两个比例相等时，保护先处理使普通trailing从一开始就不激活。半仓也可能先提交剩余目标，随后本轮固定TP/SL/普通trailing提交全退；最终退出目标必须只向更小剩余收紧。

### 普通移动日停、静态早退与断流日停

| 项目 | 精确规则/限制 | 定位 |
| --- | --- | --- |
| 普通移动日停意图 | is_trail且stop_day_on_trailing真时写day_stopped，原因“移动止盈触发”；false的提示为可继续开仓。独立于保护激活日停 | `_run`7814–7823，offset67092–67320 |
| 静态可达性限制 | 普通er路径7731先写close_booked=True；7795再次检查同字段，为真时重复target0并在7802跳回循环，可能遮蔽7803之后状态清理和7814日停。STOP_DAY_ON_TRAILING全归档执行读取仅7814一处。无运行/并发状态验证，不能把该意图分支描述为已验证必达 | `_run`7731，offset64974–64982；7795–7802，offset66778–66974 |
| 其他止盈日停旧字段 | STOP_DAY_ON_PROFIT读旧stop_day_on_profit，缺省True；7825只匹配原因含“止盈”且不含“移动止盈”。“80%保护”不匹配该字符串门禁；该段同受上述可达性限制，当前DsvProfile未覆盖此旧字段 | `_run`3977，offset3498–3550；7825–7828，offset67324–67422 |
| 断流零开关 | stale_force_close_seconds直接float；≤0跳过该断流触发段，没有or180回退 | `_run`3991，offset4004–4024；7439，offset57576–57586 |
| 断流时段/宽限 | 阈值>0、当前session存在、尚未force_close_pending才检查；quiet=now−max(session_start,last_quote_ts或session_start)，quiet严格>配置阈值，并且now−session_start严格>60秒才创建强平状态 | `_run`7439–7464，offset57576–58462；模块591固定STALE_SESSION_GRACE_SECONDS=60 |
| 毫秒严格边界 | 对整数毫秒的G0≥比较，编译floor(秒数×1000)+1，180秒即180001毫秒；0仍关闭。低于60秒的非默认值还需显式会话宽限事实，不能只依这一个阈值字段 | 同上，对整数域表达式推导 |
| 断流日停快照 | 触发时将stale_close_day_stop存入force_close_day_stop，但此时不写day_stopped | `_run`7465–7472，offset58468–58602 |
| 断流日停落实 | force_close_pending处理先查实际多空；两者均≤0确认平仓后清持仓状态，再根据force_close_day_stop写day_stopped，原因为“行情断流平仓”。false则不因该断流完成额外日停 | `_run`6244–6269，offset27864–28394 |

HALF_BREAKEVEN未定义、重复close_booked早退以及虚拟量超时被实际量覆盖均属于静态潜在缺陷/语义边界；新产品若落实明确配置意图，应在source_notes和测试中留下工程修复说明，不复制吞异常，不伪称实际交易已通过兼容验收。缺失合法价格继续走告警接管，禁止为了模仿日志“市价强平”文字而虚构可成交价格或连接柜台。

## 撤单、重报价与追单的静态证据

本节继续只读同一发行包。`trading.exe`回读SHA-256仍为`aabeafd83d265ab9b1cf5cc3fd0e9b0f21963ddcc95daa70feefc209997269a3`，`gui`入口仍995361字节、SHA-256 `07c82c9ada765284ed5a929227024cbf192f5f72192636900740d9a5764032ea`。另在内存读取其嵌入`PYZ.pyz`的`tqsdk.lib.target_pos_task`：解压后的原marshal内容53388字节，SHA-256 `5e5ab0e0727cc5f6467b7a95852781466ca7e2d790ed7a17cbab0ef7accbf7fe`。原marshal重新编码的摘要可能不同，不能混作原件hash。

方法仍为独立CPython 3.14.7标准库`marshal/dis`静态解析和只读PyInstaller归档工具；没有import SDK模块、执行归档业务代码或运行DSV。辅助解析脚本在`output/tools`，未提取原业务字节码到产品或交付包。以下为源控制流，不是柜台运行、实际撤单成功或撮合结果。

### 策略价格回调与有效期

`StrategyThread._run`创建`make_price_fn(symbol)`，再把返回的函数交给`TargetPosTask(price=fn,support_open_min_volume=True)`。遇TypeError逐级降到仅`price=fn`，最终甚至无自定义price；因此不能宣称所有原SDK兼容版本必走自定义函数。定位：`_run`4933–4940，offset11582–11630及81984–82112。

令买方向基价`p=ask1 or last`，卖方向基价`p=bid1 or last`；上下文方向必须与实际订单方向相同，各模式单选：

| ctx模式 | 源函数返回规则 | 定位 |
| --- | --- | --- |
| `passive` | `now−ctx.ts<=90s`且level>0时直接返回理论level；超期回后续默认逻辑 | `_price_fn`4863–4867，offset324–596 |
| `aggr1` | `now−ctx.ts<=60s`时BUY `p+tick`、SELL `p−tick`；tick的0/缺失通过`or1.0`回1；结果>0才返回 | 4869–4873，offset598–940 |
| `market` | `now−ctx.ts<=120s`时BUY选正upper_limit、SELL选正lower_limit；缺该限价时BUY `p+10*tick`、SELL `p−10*tick` | 4877–4886，offset942–1462 |
| `cap` | `now−ctx.ts<=90s`且cap>0时BUY `min(p,cap)`、SELL `max(p,cap)`；仍走下面正数返回 | 4887–4894，offset1464–1792 |
| 默认或过期 | p>0返回p；否则依次尝试正last，再BUY正upper_limit/SELL正lower_limit；仍没有则None；异常也None | 4897–4910，offset1794–2094 |

所有有效期比较含等号。`market`是极端限价价格选择，不能把名称解释成已发出市价单。源回调本身没有统一tick对齐或把`aggr1`夹回涨跌停区间；新产品的合法价格、有效合约规格、时效及价格界限合同独立保留，不以源回调回退替代完整事实。

### 首次入场、被动等待与确认超时

| 项目 | 源控制流 | 定位 |
| --- | --- | --- |
| 首次入场模式 | `_passive=PASSIVE_ENTRY_FIRST and not near_open and direction is not None`。near_open用active，非near_open被动开则passive，否则cap。cap为理论entry±`max(1,round(O*当前3%/5%比例))`；passive的level为理论entry | `_run`8251–8271，offset78348–78670 |
| near_open精确范围 | 先取得当前合约session，start的时分必须是09:00、09:30或NIGHT_OPEN_HM（本发行包21:00），且`0<=now−start<=timedelta(minutes=window)`，两端含等号；午间恢复盘不因刚开盘就归此窗口，异常false | `session_open_recent`1676–1692，offset82–328 |
| 被动等待 | `passive_wait_seconds`直接float，保存缺失用3；首次pending保存`passive_until=ts+wait`。这是源隐藏保存字段，不等于已有profile已支持该字段或任意用户自填值 | 4114–4115，offset5968–6074；8352–8359，offset约80500–80742 |
| 到时转价 | pending的passive_until非0且`now>=until`时重新按当前开盘窗口计算cap，更新ctx为cap并刷新ctx.ts，清passive_until。当前实际该方向量<=0时先target0再target原volume；随后继续确认实际持仓 | 7222–7263，offset51886–53172 |
| 已有成交确认 | `confirm_entry`得确认结果后，登记实际量/价格及保护状态，并把pending_entry设None；不是必须目标全部成交才清pending | 7261–7299，offset53040–53982及后续正常确认分支 |
| 未确认超时 | 保存`entry_confirm_timeout`缺失用60；`now−pending.ts>timeout`严格大于才`set_target_volume(0)`，马上清pending_entry，并增加失败提示次数。提示“已撤单”不证明柜台终态 | 4106，offset5848–5900；7391–7422，offset56548–57320 |
| 更长TTL清理 | 尚未确认pending超过180秒直接丢弃该本地对象，不能作为新产品释放未知订单预占的证据 | 7210–7216，offset51532–51836 |
| 激活位追价开关 | `NO_CHASE_AFTER_ACT`在本代码对象中仅3982初始化及8037/8040新信号资格判断读取；未发现挂单期间用此变量持续取消的分支 | 3982及8037–8040 |

原程序目标`0→原volume`连写没有在GUI同步等待旧单撤单终态；SDK目标通道还可能合并待处理值。独立实现必须使用自有订单身份和终态/成交一致屏障，不能复制“目标已改”即重发。已有实际部分成交已退出pending确认路径，故该60秒分支不是已确认仓位强平规则，也不能据此声明所有开仓残单均在60秒后自动消失。

### 收盘与其他退出的追价时钟

| 项目 | 源规则 | 定位 |
| --- | --- | --- |
| 固定时钟参数 | `CLOSE_RETRY_SECONDS=3`、`CLOSE_EXTRA_TICK_AT=10`、`CLOSE_MARKET_AT=5`为常量；后两者并非“第10/5次撤单” | 4108–4110，offset5902–5912 |
| 收盘模式选择 | first_try初始化close_start_ts；elapsed=now−start；secs_left=CLOSE_BEFORE_SECONDS−elapsed。secs_left<=5用market，else<=10用aggr1，否则active | 6897–6912，offset43828–44140 |
| 目标刷新节奏 | first_try或`now−close_retry_at>=3s`才在该收盘分支再次更新ctx/target0，更新close_retry_at；这不是SDK订单撤单的固定3秒间隔 | 6913–6915，offset44142–44290；6984–6991，offset45928–46100附近 |
| 常规SL/TP/BE/半仓 | 退出方向通常与entry上下文方向相反，回调落默认对手价；该普通退出段没有采用收盘3/10/5秒时钟。不得把收盘阶梯套到所有退出 | 7596–7659及7723–7802；`_price_fn`方向门禁 |
| 强平pending | 开市期间、尚未实际平仓的force_close_pending在距last_try>=5s时更新market上下文并target0；休市等待下一盘，不把休市当可发送窗口 | 6244–6307，特别6294–6307，offset28900–29330 |
| 同步补发 | 源另有`close_unconfirmed_at>0`、距其<60秒、st.direction为None但账户仍有实际仓的分支，直接market并target0；继承上一节close_booked状态可达性限制，不能据日志把每轮计数当已发送或成交 | 6409–6437，offset31808–32716 |

### SDK内部撤单与重发

本表来自**发行包自身**的`tqsdk.lib.target_pos_task`，未依据另一台机器安装的SDK版本或互联网最新实现推断：

| 方法 | 核对行为与边界 |
| --- | --- |
| `InsertOrderUntilAllTradedTask._check_price`690–698 | 每个更新通知重算自定义价格；BUY仅`new_price>order_price`、SELL仅`new_price<order_price`才调用cancel_order并退出价格监视任务。等价或有利方向变化不撤。关键offset120–348。没有固定撤单次数、阶梯或间隔。 |
| `_run`623–651 | 取价后发送当前剩余this_volume；等待InsertOrderTask完成，按`this_volume−order.volume_left`减少剩余总量，再下一轮重挂。关键offset142–1354。 |
| `_run`652–654 | 有剩余且价格监视任务尚未done时抛“错单”异常；即未证明是价格监视主动撤回时，不无限重试剩余拒单。关键offset1364–1554。 |
| `_run`656–667 | finally若订单仍ALIVE则请求cancel，关闭价格通知并等待；最终等待InsertOrderTask最多30秒，超时抛异常。30秒是异常等待上限，不是确认撤单、释放数量或允许盲重发的TTL。关键offset1556–2026。 |
| `InsertOrderTask._run`744–755 | 先insert_order；完成屏障需订单status为FINISHED，且`volume_orign−volume_left == sum(trade_records.volume)`。关键offset50–234和586–800。没有把cancel调用返回当成交/终态。 |
| `TargetPosTask.set_target_volume`345–347 | 仅检查目标任务未结束，再将整数目标投递到_pos_chan；返回本身不证明旧单取消或新单成交。目标任务452还会recv_latest合并目标更新。 |

此处没有静态证实柜台订单编号归属、断线回调完整性、真实交易所撤单错误码或成交先后；新owner必须继续保留自有子订单/尝试身份、UNKNOWN容量预占、提交后派发和重启核对，不沿用SDK本地对象作为新产品的持久化交易事实。

### 手工通道的不同语义

`close_price_mode`只在`ManualTradeThread`手工价格选择中使用，未发现`StrategyThread._run`读取该保存字段：`对手价`为SELL bid1/BUY ask1，`排队价`为SELL ask1/BUY bid1，`最新价`为last；手工开仓自动取价为BUY ask1/SELL bid1。手工价格等待最多4秒，每轮wait_update deadline=now+0.3s（8888–8956）。无价兜底`_fallback_limit_price`8858–8886优先BUY upper/SELL lower；缺限价回last；有极限价但缺正last则拒绝，`abs(limit−last)/last > limit_fallback_ratio`也拒绝，等号允许。该兜底参数当前为1%，不是策略market模式±10tick的规则。

手工run通过api.insert_order直接发单（9240/9270）；本轮扫描未见其自动撤改追单循环。手工撤单/撤全部走独立队列（9177–9198），停止时原程序还有遍历账户订单撤单的分支；新产品只管理原归属订单，不能据用户要求“套用DSV”把外部单纳入撤单范围。手工自动取价、策略SDK追价、收盘目标刷新须分别描述，不互相移植规则。

## 时间、会话与调度的静态证据

本节仍来自同一DSV EXE及`gui`归档，只在独立3.14解释器中读取代码对象及指令。没有运行GUI、SDK、账户、墙钟调度任务或交易会话。以下时刻是源程序本地时刻的字面规则，不是对未来交易所日历、节假日安排或柜台交易日的联网确认；新产品应由明确完整的日历事实承载时段及交易日身份。

### 会话边界与日历来源

| 项目 | 已核对源规则 | 定位 |
| --- | --- | --- |
| 通用日盘段 | 模块固定为09:00–10:15、10:30–11:30、13:30–15:00；夜盘默认21:00–23:00。 | 模块581–583，offset548–562 |
| 夜盘品种终点 | 名称与代码小写拼接；先匹配02:30关键字，再匹配01:00关键字，未匹配用保存默认或23:00。该字符串推断不能作为新产品合约元数据证明。 | `night_close_hm`1526–1541，offset40–224 |
| 通用会话拼接 | 先要求`is_trading_day(day)`；仅当`next_trading_day(day)-day`的自然日数`<=3`才加夜盘。夜盘收盘hour>=21则同日结束，否则在次日结束；`current_session`同时检查本日及前一自然日。 | `session_intervals`1587–1615，offset194–574；`current_session`1618–1626，offset64–370 |
| 交易日启发式 | `weekday()<5`且日期字符串不在`_HOLIDAYS`；异常返回false。`_HOLIDAYS`模块初始化为空set，后由设置写入。这不证明交易所完整放假/夜盘安排。 | `is_trading_day`613–618，offset4–104；模块597 |
| CFFEX专用段 | `current_session_for`先识别CFFEX；09:30–11:30、13:00–15:00，债券前缀T/TF/TS/TL且排除IC/IM/IF/IH则下午15:15。非CFFEX走通用会话；专用分支异常回退通用。 | `current_session_for`1554–1584，offset4–58、480–802、826–850 |
| 半开边界 | 有效盘段均为`start<=now<end`。收盘瞬间不再属于已结束盘段，不把休市区间当有效行情窗口。 | `current_session`1624，offset302–324；`current_session_for`1580，offset746–768 |
| 报价交易日推断 | 从报价字符串截日期及小时；小时>=18取下一启发式交易日，否则把日期向后推进到启发式交易日；异常/缺值返回None。不能据此替代柜台明确交易日标签。 | `get_trading_day`2115–2133，offset24–386 |
| 开盘recent | 必须位于有效会话，起点(hour,minute)为09:00、09:30或21:00；`0<=now-start<=配置minutes`，两端含等号。10:30或13:30休市后恢复不属于该开盘窗口。 | `session_open_recent`1676–1692，offset82–328 |

### 入场等待、收盘禁开与实际平仓窗口

| 路径 | 源行为与边界 | 定位 |
| --- | --- | --- |
| 普通开盘后等待 | 开关开启且分钟数>0时调用`session_open_recent`，因此只适用09:00/09:30/21:00对应窗口，包含等待终点等号，不在午间恢复后重新套用普通5分钟等待。 | `_run`7876–7890，offset68586–68972 |
| 普通收盘前禁开 | 调用`is_before_session_close`；它先取得通用当前会话，只在结束hour==15、>=21或<3时检查`0<=end-now<=分钟数`。10:15与11:30休市前不触发此普通门禁。 | `is_before_session_close`1882–1894，offset66–318；`_run`7894–7902，offset68978–69338 |
| 延续专用等待 | 夜盘延续路径另有门禁：开关开启且`continuation_ignore_no_open=false`时，使用`current_session_for`的任意当前盘段，`0<=elapsed<minutes*60`，上界为严格小于；没有普通recent的开盘hour筛选。不能把两个分支擅自统一成一个比较符或同一适用范围。 | `_run`7136–7153，offset49826–50250，特别50066–50120 |
| 实际收盘平仓 | `_near_close_tot`只判`[night_close-advance,night_close)`及`[15:00-advance,15:00)`，忽略10:15与11:30休市；夜盘下界若为负被夹到0。该处15:00硬编码，与债券会话helper的15:15存在源差异。 | `_run.<locals>._near_close_tot`4529–4545，offset122–262 |
| 使用何种当前时钟 | 平仓near_close为`is_near_close(q.datetime) OR is_near_close_by_local(datetime.now())`。因此旧报价字符串仍落窗口时可能继续为真，不能把旧报价时间当作新产品当前时间证明。 | `is_near_close`4547–4551、`is_near_close_by_local`4553–4556；`_run`6881–6886，offset43462–43648 |

独立实现应区分盘段结束、最终清仓终点及禁开终点，并明确每个会话的用途；不能仅因有一个`session_close_utc`就推定每次午间休市需要强平。对显式柜台/权威日历给出的不同终点，新产品需记录来源差异，不沿用源代码的品种名/weekday猜测作为批准。

### 报价新鲜度、无行情循环与断流时钟

| 项目 | 已核对源行为 | 定位 |
| --- | --- | --- |
| 真正刷新报价到达时钟 | 取非空`str(q.datetime)`，且与`last_quote_dt`不相等才写新`last_quote_dt`、`last_quote_ts=time.time()`和`last_quote_mono=time.monotonic()`。重复同datetime不刷新；源仅比较`!=`，没有在此验证时间严格前进。 | `_run`6133–6139，offset25082–25440，特别25324、25390、25438 |
| 未使用的字段 | 对`gui`全部代码对象的指令引用扫描未见`last_update_time`；本段不得改称依赖该SDK字段。 | `gui`静态名称/常量扫描 |
| 周期推进 | 每轮`api.wait_update(deadline=time.time()+0.5)`，调用返回值直接丢弃，随后继续本轮处理；不是只有新报价才处理pending、收盘或断流。异常则记录连续超时/提示，不能从异常次数证明柜台维护或成交结果。 | `_run`5892–5895，offset18310–18418；异常分支5897–5905 |
| 活跃断流基点 | 必须存在当前`current_session_for`且断流阈值>0、尚无force_close_pending。`ref=max(session_start_ts,last_quote_ts or session_start_ts)`，`quiet=time.time()-ref`；需`quiet>threshold`及`time.time()-session_start_ts>60`双重严格比较。休市不进入该分支，新盘以新起点截断上一段静默时间。 | `_run`7436–7465，offset57430–58474；常量`STALE_SESSION_GRACE_SECONDS`模块591 |
| 睡眠/校时提示的静态缺口 | 源另算`wall_gap>300 AND mono_gap>0 AND mono_gap<wall_gap*0.5`的sleeping标志并提示暂不强平；但该提示分支之后仍落入quiet判断，未见明确continue/return阻止同轮退出。不能据日志措辞宣称已正确处理休眠或墙钟跳变。 | `_run`7444–7463，offset57752–58462 |

新产品的计时事件不能更新报价接收身份或延长旧报价可发送寿命；需要分别保留UTC/盘次定位与单调经过时间，并把回退、跳变、重启缺口列为明确核对边界。重复调度不是新报价，也不证明新的账户/委托/成交完整性。源程序的wall-clock/monotonic启发式和异常吞掉路径不替代该工程合同。

### GUI定时启动与权限边界

`App._schedule_tick`以`root.after(30000,...)`每30秒检查；关闭定时开关仍预约下一次检查。它取本地当前日期和分钟数，读取day/night两个设置时刻；catch_up_minutes先`int`并夹到非负，缺失/异常为60。命中`target_minute<=current_minute<=target_minute+catchup`后按`YYYY-MM-DD_day/night`构造key，与内存`last_triggered`比较；先写key，再看策略是否在运行，未运行则调用`on_start(auto=True)`。这些本地字段不是持久启动回执或实际开仓权限（16585–16616，offset2–1224）。

启动后的`main._auto_start_if_session`另检查`_user_stopped`，已运行线程等待；只要任一品种当前处于会话，或下一开盘在30分钟以内，就可能按300秒调用间隔发起`on_start(auto=True)`；临近开盘采用1/2秒或60秒的下一次检查延迟（16890–16941）。没有在本轮继续追踪`on_start`全部登录/资格路径，也没有运行验证GUI自动启动。保存的auto_start_on_launch、schedule.enabled和定时时刻只能作为参考设置，不能使新内核自动获得恢复开仓、连接柜台或安装服务的权力。

## 统计与停开：名义点数、确认边界及源缺陷

本节静态核对同一发行包`trading.exe`（SHA256 `aabeafd83d265ab9b1cf5cc3fd0e9b0f21963ddcc95daa70feefc209997269a3`）的`gui`代码对象；`gui`原始归档大小995361字节，SHA256 `07c82c9ada765284ed5a929227024cbf192f5f72192636900740d9a5764032ea`。使用独立Python3.14的`marshal/dis`只读定位，没有启动GUI、SDK、柜台、网络或执行其业务代码。以下是源程序行为及明确的工程修复边界，不能把源状态、目标、日志或账户汇总当成完整订单/成交事实。

### 每品种作用域与记账单位

源状态按`state[cont_code]`逐品种保存，初始字段在`_run`4627–4664，offset9186–9622；不是全账户跨品种求和。`daily_pnl`在普通自动平仓中为**每单位价格点数**，未乘手数或合约乘数；`net_loss_points`允许负值，代表此前盈利抵扣的缓冲。`apply_close_to_net_loss`1789（offset4–96）仅返回`float(net_loss or0)-float(pts or0)`，没有夹到0。成交金额、手续费、名义风控点数不得混作同一种指标。

确认入场时的`st.entry_price`取pending中的理论`entry_level`；实际价格另存`entry_fill_price`（7275–7276，offset53396起）。普通退出参考价为当轮`lp`；原因恰为`80%保护`时改取`protect_line`（7733–7735，offset64986–65066）。令`pf=退出参考价−理论入场价`（LONG），SHORT反向，后续公式中的`pf`均指此值，不能称为实际成交损益。手数不改变下表普通退出点数，半仓也不按已平比例加权。

| 源事件/退出原因 | `daily_pnl`变化 | `net_loss_points`变化 | 计次与连续止损 | 源定位 |
| --- | --- | --- | --- | --- |
| 首次确认入场 | 不变 | 不变 | `trade_count+=1`，`total_volume+=pending.volume`；原函数不是逐成交累加，pending请求量可能大于首次实际确认量 | 7261–7285，offset53040–53630 |
| `止损`，含同一路径触发的保本止损 | `+=pf` | `+=配置S`，不按实际跳空亏损 | `trade_count+=1`，`stop_count+=1`，`stop_streak+=1` | 7743–7761，offset65238–65738 |
| `移动止盈` | `+=pf` | `-=pf` | `trade_count+=1`，`profit_count+=1`，`stop_streak=0` | 7743–7744、7763–7765，offset65762–65854 |
| `80%保护` | `+=pf`，以保护线作参考价 | `-=pf` | `trade_count+=1`，`profit_count+=1`，`stop_streak=0` | 7767–7770，offset65856–65970 |
| 普通`止盈` | `+=pf` | `-=配置T`，不按超价pf | `trade_count+=1`，`profit_count+=1`，`stop_streak=0` | 7771–7774，offset65972–66098 |
| 半仓阶段 | 此阶段不改 | 此阶段不改 | 此阶段不改上述计次/streak；后续剩余仓普通结案仍按一次完整上述pf记点 | 7596–7659，统计字段全代码对象引用扫描 |
| 收盘退出 | `+=pp`，`pp`为lp对理论入场的方向点差 | `-=pp` | 不改`trade_count/stop_count/profit_count/stop_streak` | pp定义6916–6918，offset44314–44410；记账6954–6955，offset45216–45300 |
| 入场滑点超限退出 | 不变 | 触发时`+=不利滑点_slip` | 不改上述计次/streak；入场已确认的那一次计数仍存在 | 7358–7376，特别7368 offset55768–55808 |
| 断流强平 | 不变 | 不变 | 实际持仓flat后仅清理force-close状态及可设置当日停开，不产生普通结案统计 | 6244–6260，offset27864–28204 |
| 外部/手动平仓归集 | 源代码`+=_money6`，此处为货币值，存在单位混用 | `-=_pts6`，算法另见下文 | 每次`trade_count+=1`、**无条件`stop_count+=1`，含正收益**；不改`profit_count/stop_streak` | 6635–6672，offset36736起 |

收盘仅`first_try`归集；若账户已没有该方向持仓，源明确只同步状态而“不记账”（6931–6942），重试不重复记点。入场滑点和断流均会进入`force_close_pending`实际flat确认段；该段6248–6260不再追加统计，不能随后又套普通止损公式重复记点。

### 外部归集的符号与单位缺陷

源`_money6=当前(close_profit_long+close_profit_short)−入场时基值`（6581–6589）。设`m=volume_multiple`、`v=max(int(st.volume),1)`，则`_pts_money=_money6/(m*v)`（m>0时）。理论入场及报价均正时先按方向价差估算`_pts6`；否则用`-_pts_money`。只要`abs(_money6)>1e-6`，6604–6605又无条件覆盖为`-_pts_money`（offset35980–36018）。这使账户正平仓盈利被反号后再从净亏中相减；同时6671把货币直接加到普通分支的点数变量中。6860之后的延迟账户补差也采用相同负号。此处只能证明源实现不一致，不能将其包装成确定的财务结算规则。

外部点数先经`cap_close_points`，上限`max(abs(S)*10,200)`，超过上限返回`(0,True)`（1794–1808，offset4–144）；随后另以`max(abs(S)*3,abs(T)*1.2)`限制绝对值（6607–6626）。异常时还把`_money6`置0。`_pts6>0`只控制`manual_tp_pending=True`；6639的false跳转offset36826的数字目标36958恰为6648的`stop_count+=1`，true分支也自然落到该指令，因此不是“只有亏损手动平仓才加止损次数”。

独立实现应把策略自有手动结案的已确认每单位方向点差作为名义点数，另记与上述反号/货币混加缺陷的修复差异。账户累计平仓汇总不是自有成交归属证明；本轮不导入它作为已实现金额、外部单事实或统计补账来源。

### 一次性、真实确认与源`close_booked`限制

普通退出先在7723检查`close_booked`；首次设true后7733–7774仍执行上表记点，再设置target0。此前已true则7730直接continue。另一个7795–7802重复检查发生在第一次记点之后，会遮蔽后续清理/部分日停处理；**不会遮蔽第一次7743普通记点块**。因此源在下达平仓目标时已计点，不能把其计数时间解释成真实平仓确认。

独立owner应分别持久登记一次`ENTRY_CONFIRMED`和一次完整`CYCLE_CLOSED`：前者以首个自有真实成交确认计次并区分请求量与实际量；后者冻结第一次有效退出原因及其名义参考价，待自有关闭尝试全部终态、成交量与订单剩余量一致、该保护实际仓位归零后归集。部分平仓、目标0、派发回执、撤单请求、重复/迟到回调及UNKNOWN均不能提前产生结案统计或重复归集。上述是修复源确认边界的工程合同，并非静态证实原DSV已经具备这些保证。

### 交易日复位、续开次数与冷却

| 项目 | 源规则 | 定位 |
| --- | --- | --- |
| 日切身份 | `st.trading_day`与`cur_td`不同才复位；先尝试一次恢复本交易日状态，恢复命中则continue。`cur_td`来自上一节报价日历启发式，不能据此证明真实柜台日。 | 6786–6795 |
| 日切清零 | `daily_pnl/net_loss_points/stop_count/profit_count/trade_count/total_volume/daily_order_count/commission_total`归零，`tracked_tids`新set，`stop_streak=0`、`day_stopped=False`、reason清空；同时重置last_signal_bar、收盘/追踪、夜盘参考、pending_entry与滑点日统计。源清pending不能替代新产品未知单核对。 | 6796–6825，特别6810 offset41500–41508 |
| 跨日保留 | 本日复位段不清`stop_cool_until/stop_last_dir/stop_last_trigger`、`cont_day`、`stop_days/month_stop`。全`_run`对应字面字段引用已核对；不能擅自按自然日0点清冷却。 | 初始化4631–4642；日切6786–6825及全代码对象引用 |
| 续开每日一次 | `cont_day!=trading_day`为资格；建立续开pending时就写`cont_day=trading_day`，不等成交确认。这是机会消费标记，独立于入场统计。 | 7077、7193，offset47932–47986、51262–51282 |
| 连续止损作用范围 | 续开另要求`stop_streak<CONT_STOP_STREAK_LIMIT`；普通止盈/移动止盈/80%保护及日切复位streak，收盘/滑点/断流/外部归集不复位。源没有独立`continuation_stopped`字段；`CONT_STOP_DAY_STOP`且最后入场tag为`延续开`时写普通day_stopped及相应reason。 | 7079、7750–7765 |
| 冷却默认与写入 | `stop_cooldown_enabled`缺失false；`stop_cooldown_minutes`缺失5。普通止损命中后记最后方向/理论触发价，仅开关true且分钟数>0才设`until=time.time()+minutes*60`。预填5不等于功能默认启用。 | 4036–4037，offset4522–4628；7699–7703，offset64204–64372 |
| 冷却门禁 | 仅开关开启且候选方向等于最后止损方向时，`time.time()<until`阻断；等于到期允许，反方向不因此阻断。阻断会写`last_signal_bar=lb`并continue，消费该信号bar。 | 8170–8186，offset76490–77080附近 |

源冷却使用wall clock；独立调度需承载明确的重启/单调经过时间边界。跨日重置不得抹掉未结订单、成交归属、仍有效保护、持久去重身份或本月停开。

### 净亏日停与连续交易日月停

普通新入场前，`阈值=S*MAX_NET_LOSS_STOPS`且阈值>0时，`net_loss_points>=阈值`即跳过；等号已达限（7844–7855，offset67706–68192）。源普通结案后的日停赋值7831–7835位于上述`close_booked`早退之后，不能据此宣称该写入总可达。独立实现应在确认统计归集后立即派生日停，并在新入场资格重查。

月停检查逐品种进行（6005–6046）：仅阈值>0且该品种净亏已达阈值时，把当前`trading_day`去重加入`stop_days`，仅保留最后12条（6021–6025，offset21762–22032）。**不是任意日停原因都计亏损停机日**。从当前交易日向前遍历，最多40自然日，按`is_trading_day`选最近`MONTH_STOP_DAYS`个交易日；若日期数足够且全部在`stop_days`内，写`month_stop=YYYY-MM`及月停day_stopped/reason（6028–6040，offset22088–22594）。该串连续性可跨周末/节假日，也可引用前月日期；源是否节假日正确取决于其启发式日历，独立实现应使用明确给出的日历事实。

检测到已存`month_stop`与当前月份不同时，6011–6015清该月标记，且只在day_stopped_reason恰为`本月连续停机停开`时解除日停，不覆盖其他停开原因。源存在另一缺口：6026若month_stop已为本月就直接跳过检查，而日切6809会清`day_stopped`；全`_run`未见入场门禁独立读month_stop。因此源的月停可能在下一日被复位削弱。独立实现必须用持久月份身份单独持续阻断至新月，并在文档明确这是闭环修复，不能将它描述成原发行包已验证的持续行为。
