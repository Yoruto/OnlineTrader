# DSV资金盈亏与报表静态证据

核对日期：2026-11-10（Asia/Shanghai）。本页只解释用户提供DSV发行包中的计算和展示，不构成账户事实、资金核验、在线连接或交易授权。原生策略的名义点数另见[统计与停开证据](strategy-dsv-evidence.md#统计与停开名义点数确认边界及源缺陷)；资金报表不能把该点数直接改名为已实现账户金额。

源文件为`C:/Users/xyc/Desktop/trading/trading.exe`，本次重新核对SHA-256为`aabeafd83d265ab9b1cf5cc3fd0e9b0f21963ddcc95daa70feefc209997269a3`。只读提取`gui`原始marshal数据995361字节，SHA-256为`07c82c9ada765284ed5a929227024cbf192f5f72192636900740d9a5764032ea`，在独立Python3.14下递归读取代码对象并用`dis`定位。未执行DSV业务代码、启动GUI、调用SDK、访问账户或连接柜台；没有读取或输出账户配置、凭据及真实成交数据。

分析入口是`output/tools/money-index-20261110.py`及`output/tools/money-windows-20261110.py`；后者参数为精确`co_qualname`、起止源行，只反汇编指定窗口。下文源行号和offset均指该`gui.py`代码对象，不是新产品源码行号。

## 账户资金与逐合约持仓

| 项目 | 已核对源行为 | 定位 |
| --- | --- | --- |
| 资金字段来源 | 从SDK账户取得`balance/available/margin/risk_ratio/pre_balance/commission/close_profit/position_profit/float_profit`写入`account_info`；其中若干`getattr`缺省0，再经`safe_num`处理。不是以每品种策略`daily_pnl`换算。 | `StrategyThread._run.<locals>.update_account_info`5498–5519，offset48–1008 |
| 当日盈亏 | 界面在`pre_balance>0`时用`balance-pre_balance`，否则显示数值0；收益比例在同一前提下用`today_pnl/pre_balance`，否则0。`position_profit`单独显示为持仓盈亏；名为`lbl_float_profit`的标签实际显示`today_pnl`。 | `App.update_account_display`14197–14215，offset1254–2032 |
| 账户日志兜底 | 若pre_balance有效且>0，日志当日盈亏也用`balance-pre_balance`；否则用`safe_num(close_profit)+safe_num(float_profit)-safe_num(commission)`。日志累计盈亏为`balance-start_balance`，start_balance是本次运行起点。 | `StrategyThread._run.<locals>.log_acc`5810–5820，offset76–484 |
| 权益解释 | 启动日志声明动态权益包含当日盈亏，静态权益为昨结，并写出“当日盈亏=权益−昨结=平仓盈亏+持仓盈亏−手续费”。此为源程序说明，不能据此证明其已正确处理出入金、跨日结算、外部交易及全部费用。 | `_run`4592，offset8524 |
| 保证金占用 | 界面`usage=margin/balance`，要求balance>0，否则0；与SDK风险度分别显示。 | `App.update_account_display`14197，offset1254–1288 |
| 持仓价差与浮盈 | SDK`open_price_long/short`作为实际开仓参考；价差为LONG的`last-open_price_long`、SHORT的`open_price_short-last`。货币浮盈直接取SDK`float_profit_long/short`，保证金取SDK`pos.margin`。 | `update_account_info`5569–5577、5591–5598，offset2242–2570、2948–3276 |
| 持仓浮盈比例 | `float_profit/margin*100`，margin>0才显示带正负百分比，否则显示破折号。此比例不是账户当日收益率。 | `App._refresh_positions.<locals>.values_fn`14272–14276，offset2–200 |
| 市值兜底 | SDK方向市值为0且last>0时，用`last*该方向实际持仓量*volume_multiple`估算；volume_multiple缺省1。不是已实现盈亏。 | `update_account_info`5557、5561–5563、5583–5585，offset1910–2134、2720–2840 |

`safe_num`2014–2023把None、NaN和转换异常返回其default；大量调用default为0。它只显式检测`f!=f`，本段没有inf过滤。新产品保留已有“未知为未知”的工程边界，不把缺失、过期或不完整账户字段强行填0，也不能用未知费率制造零手续费。没有原始资金和出入金事实时，应说明无法计算账户权益及收益率。

## 手续费来源、去重与估算

| 项目 | 已核对源行为 | 定位 |
| --- | --- | --- |
| 单合约归集 | 遍历`api.get_trade().items()`，成交ID不在该品种`tracked_tids`且`tr.instrument_id==trade_sym`时，加`tr.commission`并记录ID。不是对下单请求计费。 | `track_commission`5445–5453，offset96–384 |
| 全量轮询归集 | 先由策略state的`trade_symbol`建映射，遍历SDK成交，匹配instrument_id后，对未追踪ID加`safe_num(tr.commission)`并标记。 | `track_all_commissions`5461–5477，offset6–462；`update_account_info`5783调用 |
| 实际优先 | `_comm_display`先读取`commission_total`，**仅当>0**时直接显示该累计值。 | `_comm_display`5487–5489，offset2–72 |
| 源估算 | 实际累计不大于0时，先取`quote.commission`，该值为0等假值时再取`st.real_commission`；per_lot>0时返回`per_lot*st.total_volume`，否则0。`total_volume`为源首次确认入场累加的请求目标量，不是完整开平双边真实成交量。 | `_comm_display`5490–5494，offset74–282 |
| 估算标志 | position的`commission_est`由`commission_total<=0`产生；展示时若标志true且金额>0，前缀`≈`。因此源中的显式零手续费也会被当作可估算，而非一个独立的已确认0。 | `update_account_info`5578–5579、5599–5600；`App._refresh_positions.<locals>.values_fn`14277–14279，offset202–386 |
| 成交表 | SDK每笔成交直接展示trade_id、instrument_id、direction、offset、volume、price、trade_date_time、commission。 | `update_account_info`5766–5774，offset7002–7272 |

工程对应：已确认成交的费用事实应按成交身份去重并分别保留“缺失”与“确认为0”。估算需可追溯费率、原请求/真实成交量依据和`ESTIMATED`质量，不能记进已确认费用或确定净收益。DSV此处只有每手费率兜底，没有在该函数中区分按金额费率、开/平/平今或期货公司加价；不能从该分支推断完整柜台费率模型。

## 报表的实际维度和计算

`write_daily_report`921–1210使用openpyxl生成`报表_<day>.xlsx`，包含三个工作表。它只读取内存品种汇总、日志文本和历史点数记录，不能证明完整真实成交台账。

| 工作表/入口 | 源列与行为 | 定位 |
| --- | --- | --- |
| 按品种 | 品种、合约、交易次数、总手数、止损、止盈、当日盈亏（点·净额）、累计净亏（点·负=净盈）、手续费（元）、滑点笔数、累计滑点（点）、平仓滑点（点）、真实盈亏（点）。pnl直接取per_symbol的pnl；合计直接相加各品种点数，**没有构成跨品种统一资金收益**。 | `write_daily_report`934，offset108–114；964–974，offset950–1772 |
| 平仓核对附列 | 读取日志`[平仓核对]`，按合约提取并累加“平仓滑点”和“真实盈亏”两个点数。此处不是直接读取SDK金额。 | 948–959，offset404–858；972–973，offset1568–1676 |
| 交易明细 | 时间、品种、合约、买/卖、开/平、开仓点位、成交价、手数、平仓盈亏（点）、金额（元）。 | 980–982，offset1932–2002 |
| 明细价格和点数 | 日志regex提取理论开仓/入场、真实均价/出场、手数/已成交/平手数；平仓点数来自“单笔…点”，半仓则来自“浮盈…点”。无法据这些字段证明逐成交配对、真实成本或已实现金额。 | `_parse_trade_line`1027–1054，offset638–1598 |
| 明细金额 | pnl、vol和mult_map均有值时，`round(float(pnl)*float(mult_map[code])*int(vol),2)`；否则留空。乘数来自per_symbol.mult，不满足>0则不加入映射。 | `write_daily_report`992–995，offset2082–2248；`_parse_trade_line`1072–1077，offset2026–2304 |
| 明细去重和剩量 | 初始过滤“补发中/（重试）”文本；开仓用`(code,time,vol)`元组去重。_pos_sim按日志`[成交]`增加、平/半减少；平量超过日志剩量时过滤或裁减。它按合约模拟，不具备订单、周期、真实成交身份的严格绑定。 | `_parse_trade_line`1013–1014、1057–1068；`write_daily_report`1089–1134，offset2390–3342 |
| 明细汇总 | 平/半列手数累加；对数值点数和金额分别求和；_pos_sim非0则添加“收盘未平仓”行。 | `write_daily_report`1145–1174，offset3458–4648 |
| 每日盈亏 | 交易日、当前per_symbol各合约、合计；从_save_pnl_daily持久记录读取每合约pnl，缺失按0，四舍五入1位后展示。数据是前述品种点数历史，不是账户权益曲线。 | `write_daily_report`1185–1201，offset4900–5800 |

源`[平仓核对]`的“真实盈亏”仍是价差点数：在账户已flat且存在booking退出信息时，调用`last_fill_price(trade_sym,side,'CLOSE',within=300.0)`取得一个最近平仓价；实际入场价优先`entry_fill_price`，无值回退理论`entry_price`。LONG的_real=`最近平仓价−实际/理论入场价`，SHORT反向，并累加到`real_pnl_sum`；不乘实际分笔手数或乘数。见`_run`6353–6374，offset30590–31306。该辅助日志不能代替分笔成交、加权成本、部分平仓与手续费的资金账本。

独立实现可沿用“按品种、交易明细、每日盈亏”的展示维度及币值两位/点数一位/百分比两位的视觉惯例，但已实现资金毛利应基于自有已确认开平价格、实际配对数量和原合约乘数。使用FIFO或加权成本等明确一致的配对算法时，应把它记录为独立工程合同，不宣称DSV的日志模拟已经实施了该算法。部分真实平仓可产生对应部分毛利，不能等整个周期结案才把资金事实全部隐藏；其与名义结案统计的确认节奏不同。

## 品种规则表、保证金和手续费展示

2026-11-12 补查原 `gui` 代码对象，保持同一 DSV 发行文件。品种规则表和期货公司加价用于桌面显示。

| 项目 | 原程序行为 | 定位 |
| --- | --- | --- |
| PDF 行结构 | 读取前 16 页的可提取文本。识别品种名与品种代码，可跨行。自第一个含“元”或“点”的单位字段之后读取合约乘数和最小变动，取第 2、3 个百分比作交易所与期货公司保证金。 | `parse_rule_pdf` 2749–2881 |
| 导入 | 先显示解析预览，确认后按品种代码合并并保留来源文件名和日期。 | `App.on_import_rules` 13269–13334 |
| 实时和规则表 | 正值行情保证金除以最新价与乘数的乘积。行情有值时优先显示，同时计算公司保证金和手续费加价。无行情保证金时采用已导入的表值。PDF 原解析结果仅含保证金，不提供手续费。 | `update_account_info` 5635–5646，`App._refresh_symbol_info_page` 13377–13395 |
| 差异阈值 | 行情与已导入的交易所保证金率相差严格超过 0.5 个百分点时提示。 | `App._refresh_symbol_info_page` 13399–13434 |

在线交易仍使用原生柜台和已有风控事实。OnlineTrader 品种信息页保存的 PDF 参考数据及展示加价不会参与委托、资金或成交统计。离线用相同文本核对棕榈油、塑料、沥青和菜油的原函数字段，另用 Qt PDF 读取真实 PDF 格式的本地测试样本。当前未核验具体期货公司发行的规则表。

## 权益曲线与每日权益导出

| 项目 | 源行为 | 定位 |
| --- | --- | --- |
| 日内采样 | 每次update_account_info若距上次采样>=5秒，将当前本机datetime和SDKbalance附加到equity_data；同时调用record_daily_equity，day来自st.trading_day或cur_td，equity来自balance。 | `update_account_info`5785–5798，offset7350–7980 |
| 每日记录 | record_daily_equity忽略空day/假值equity，覆盖当日equity，按历史最早正equity计算cum_pnl=equity−first；金额round2位。该函数正文没有写入daily_pnl字段。 | `record_daily_equity`1249–1267，offset4–726 |
| 历史回填 | 从日志最后一个“权益/起始权益”补缺记录，标记filled=log；用推断交易日补缺并沿前日equity，标记filled=flat。随后daily_pnl=eq−prev_eq，cum_pnl=eq−first_eq，首日daily_pnl缺省0。 | `backfill_daily_equity_from_logs`1301–1324、1337–1362，offset220–824、1196–1906 |
| 图表 | 提供“收益曲线”模式，按每日记录cum_pnl或equity−first绘制；权益模式用原equity。只采集正equity；有日期范围。历史每日与当日日内采样组合。 | `App.update_equity_chart`13995–14046，offset462–1798 |
| 日权益CSV | 日期、收盘权益、当日盈亏、累计盈亏、累计收益率、备注；按日期筛选；收益率=eq/所选区间第一条eq−1。filled=flat备注“无数据，沿用前日”，filled=log备注“日志回填”；CSV为utf-8-sig。 | `App.on_export_equity`15795–15834，offset42–1196 |

源权益差不显式扣除出入金变化，不能在未知现金流时把权益增量标成已验证交易利润；源日志回填和缺日持平也不能用于证明该日真实账户无变化。新产品本轮只有明确开发成交、费用和行情事实时，应以本地自有成交毛利/费用/净利与可证浮盈形成报表；账户权益、昨结、可用、保证金、账户收益率等继续要求明确账户事实，缺失留未知，不能伪造初始资金、补造无数据日或假定手续费为0。

## 兼容和修复边界

1. 保留DSV区分账户资金、合约浮盈、名义策略点数及多维报表的意图；三者范围和单位分别展示。
2. 账户资金事实、已确认自有成交、已确认费用与估算值保留来源/质量；未知不转0，估算不进入确定净收益。
3. 修复源报表从日志名义点数推金额、最近单价冒充完整平仓成本、按请求量估费用和缺日持平可能产生的误导。修复不更改已确认B01–B20参数、止损S/止盈T及名义停开规则。
4. 本页只证明上述静态源行为；不证明新实现、打包、GUI布局、离线测试、柜台事实或财务对账通过。对应实现与验证由本轮独立合同和准确源码身份记录。
