# OnlineTrader 对齐 DSV 行为基准

取证日期为 2026-11-13（北京时间）。唯一业务参考是本机 [trading.exe](C:/Users/xyc/Desktop/trading/trading.exe)，SHA-256 为 `aabeafd83d265ab9b1cf5cc3fd0e9b0f21963ddcc95daa70feefc209997269a3`。归档 `gui` 对象 SHA-256 为 `07c82c9ada765284ed5a929227024cbf192f5f72192636900740d9a5764032ea`。行号指该代码对象。采用发行版可复查结果、同版日志与状态、静态代码和随包说明。记录发生冲突的输入、版本及可观察结果。产品起点为 `944ab1115e4ca09e48c05e98de00b5522bbaf18f` 加原有棕榈油回补及默认托盘等工作区修改。仅使用业务配置，凭据留在原位置。

[原 96 项](功能实现与待办清单_20261110.md) · [旧版差异审计及 B01–B20](dsv-onlinetrader-parity-audit-20261112.md) · [策略、保护、执行及统计取证](strategy-dsv-evidence.md) · [资金及日报取证](money-dsv-evidence.md) · [成交对照取证](trade-reconciliation-dsv-evidence.md)

## 已确认的根因

- **日志。** F5 把目录交给 `StrategyStarter`。旧日志页只认对话框或配对目录，并按文件是否存在抑制内部原因。实际 F5 归档已有中文触发行。空日志、重开、附着和跨账户都需要检查绑定。 [F5 实际记录](../output/validation/20261113-parallel/independent-package-night-account/desktop-starts/desktop-0e606d605fbf495db491bd4f2a98bff9/run/strategy-log.jsonl)
- **延迟。** 旧工作台每 2.5 秒拿快照后计算最旧新鲜合约的报价年龄。DSV 用策略线程发布值，新报价分支用 Windows 时间减截到秒的源时间，静默分支用距最近见到新报价的最大间隔，界面每秒读。13:30:00 参考钟快 964.599 毫秒。4.3 秒样本缺同 Tick 分段证据，真实瓶颈待核。 [时钟归档第 3052 行](../output/validation/20261113-parallel/independent-package-night-account/desktop-starts/desktop-0e606d605fbf495db491bd4f2a98bff9/run/comparison-e2d988bd9f52/events.jsonl)
- **监控。** DSV 的实时保证金率只附于活动行，取同份行情的每手保证金÷（最新价×乘数），正值才显示。强平待确认优先于停开，几何只是当前过滤。塑料、沥青 09:16:45 锁存 80% 保护日停，当前柜台日状态与日志相符。 [DSV 日状态](C:/Users/xyc/Desktop/trading/state/daily_2026-11-13.json) · [原版日志第 69 行](C:/Users/xyc/Desktop/trading/logs/trade_2026-11-13.log:69)
- **执行。** 旧夜盘棕榈油的空仓保证金缺字段、塑料和沥青的补观察抢占分钟已有源码修复。沥青整秒日志缺子秒触发证据。已结案历史也被旧 F5 的启动检查拒绝。棕榈油旧检查点实机重建有证，最终候选自然缺口未触发。上午版允许自然入场但无信号，因此报单到保护的真实路径仍待验。 [棕榈油复测](../output/validation/20261113-parallel/independent-fixed-test-report.md) · [上午报告](../output/validation/20261113-parallel/independent-new-package-morning-report.md) · [暂停回执](../output/validation/20261113-parallel/user-pause-133636-receipt.json)

## 逐项记录

下表是开发前基线。状态列是本次验收状态，历史样本和开发单测在末尾登记。每项按输入和分支记录通过、失败、受阻或未执行。静态可疑分支保留待核。策略、手数、风险、派发和实际成交分别取证。

### 账户、行情与时间

| 编号 | 原版行为与证据 | 产品原行为及差异 | 修改位置 | 验收用例 | 状态 |
| --- | --- | --- | --- | --- | --- |
| A01 | _build_trade_account、主控。快期模拟及实盘券商入口 | 已有快期/SimNow 模拟，真实券商入口缺 | desktop/accounts、gateway | 模拟连接及认证失败，真实资金另行授权 | 受阻（真实券商） |
| A02 | App.on_save_account/on_switch_account/on_use_account。新增保存、切换、双击 | 独立档案、账本及有仓切出门槛 | accounts、desktop/accounts | A/B 新建、重开、双击切换、删档、在途切出 | 未执行 |
| A03 | credentials 模块 DPAPI 及旧口令迁移，主控显示与记住 | Windows 用户凭据库，主控/手工页操作不同 | accounts、desktop/accounts | 用假凭据测试显示、取消、保存、忘记、脱敏导出 | 未执行 |
| A04 | update_account_info 5523–5547、update_account_display 14247。状态及每秒显示 | 连接/同步/权限已显示，缺行情状态和延迟分支 | kernel/workbench、desktop/workbench_layout | 注入正常、延迟、无行情、休市、连接中和恢复 | 未执行 |
| A05 | update_account_info 5498–5605。柜台资金及方向持仓 | 柜台资金、自有周期、外部仓分开，冻结和费用缺口 | kernel/workbench、kernel/money | 双向仓、冻结、0 费用、缺费用逐列核对 | 未执行 |
| A06 | StrategyThread.run 3689–3747。网络约 5 秒重试、认证失败停止，重连回当日状态 | 空仓恢复有样本，带仓、维护和日切待核 | runtime/dsv_simulation、kernel/day_recovery | 认证失败、维护、恢复、带仓及权限反馈 | 未执行 |
| A07 | main 16830。默认应用单实例 | 产品按账户排他，可开多个桌面 | desktop/entrypoint、runtime/instance | 同账号/异账号第二窗口启动与附着 | 未执行 |
| A08 | App.on_save_live_settings/on_test_live_connection。券商、资格和测试 | 固定 SimNow、TqKq，目录/线路和临时实盘反馈缺 | desktop/accounts、gateway | 有效/无效券商，测试失败与收尾 | 受阻（真实券商） |
| M01 | load_contracts_async、ManualTradePanel.on_load_contracts。手工加载目录 | 主要检索已绑定合约 | desktop/workbench_layout、gateway/tqkq_dsv | 中文、代码、月份、无结果、加载失败 | 未执行 |
| M02 | App.on_add/on_edit_row/on_delete/on_clear_symbols。双击编辑与保存 | 已有主控操作，运行订阅变更分离 | desktop/symbol_editor、kernel/control | 增删改、重名、清空确认、运行停用/恢复和保护 | 未执行 |
| M03 | _run 4779、6112。SDK 最新价、盘口、日高低 | 33,139 条共同 Tick 已比对盘口和高低，刷新及缺值待核 | gateway/dsv_facts、kernel/workbench | 同合约同 Tick，缺边价、静默及重连首笔 | 未执行 |
| M04 | _run 4915、6829–6840、7989。K 线、高低和夜盘参考 | 原生 Tick 路径及回补已有，超窗与自然缺口待核 | gateway/dsv_facts、gateway/tqkq_dsv、runtime/dsv_simulation | 盘前、盘中、午休恢复、漏一笔、超历史窗 | 未执行 |
| M05 | session_intervals/current_session_for/get_trading_day。日夜盘、长假和 CFFEX | 绑定原生日历，柜台日和原版推断可能不同 | scheduling/calendar、runtime/startup_metadata | 周五夜盘、白银跨午夜、长假和国债收盘 | 未执行 |
| M06 | _run 6133–6139、7436–7465、7909–7918。重复报价、断流、年龄严格超限 | 普通策略 0 关闭，执行另有 5 秒；正阈值时钟及精度有差异 | kernel/scheduler、kernel/dsv_strategy | 零、阈值两侧、重复时间戳、跳钟和重连 | 未执行 |
| M07 | _run 5250–5255、8281–8309。合约规格、限价和保证金 | 空仓缺持仓保证金已回退，带仓缺失待核 | gateway/dsv_facts、strategy/risk | 乘数、tick、限价、最小量、零值、未知和非零仓 | 未执行 |
| M08 | check_main_contracts 4218–4402、load_config 2566–2575。候选及下次加载应用 code_new | 已有候选，需经换月草稿 | configuration/rollover、runtime/desktop_bootstrap | 成交量/持仓量边界、到期、旧仓及下次加载 | 未执行 |

### 入场、保护、风控与统计

| 编号 | 原版行为与证据 | 产品原行为及差异 | 修改位置 | 验收用例 | 状态 |
| --- | --- | --- | --- | --- | --- |
| S01 | _run 7993–8139。低点加 O，高点减 O，触线先多 | 公式已有，自然发送时点待验 | strategy/entry | 只多、只空、两者同时，核对理论价和首报 | 未执行 |
| S02 | _run 7993–8027。振幅大于 2O−S 暂停，本日提示一次 | 状态分类已修，F5 日志漏读 | strategy/entry、runtime/strategy_log | 阈值两侧及等号，确认未写日停 | 未执行 |
| S03 | _run 7876–7890、7136–7153。普通等待含终点，延续等待不含，午间区别 | 分支已有，参考钟与本机墙钟及子秒待比 | strategy/entry、kernel/dsv_strategy | 4:59.999、5:00.000、5:00.001、午休复盘 | 未执行 |
| S04 | _run 8070–8131 存 _secs_open 却读 _secs_o。实际日停待核 | 显式经过时间及连续路径可触发整日停 | strategy/entry、gateway/dsv_facts | 关闭等待，双线、等号、60 秒、缺路径 | 未执行 |
| S05 | hysteresis_ready、8112–8116。零回退 S，单一 armed | 按方向存状态，换线和反向待比 | strategy/entry | 零、反向等号、换线、方向变更、换盘段 | 未执行 |
| S06 | _run 8034–8062。已跑过激活位且触线跳过并消费分钟 | 已实现，真实逐 Tick 顺序待验 | strategy/entry、runtime/strategy_log | 未触线、激活等号、双向和关闭开关 | 未执行 |
| S07 | chase_within_cap、8212–8309。3%/5%、有利豁免、限价和严格 1% | 主要分支已有，缺价、发送时效及合法价更严格 | strategy/entry、execution/policy | 双价一好一坏、等号、缺边价、距限价恰好 1% | 未执行 |
| S08 | _run 8187–8203。同向止损后价差小于 1e−6 跳过 | 已持久化，原版重启行为待核 | strategy/entry、kernel/statistics | 同向同线、临界价差、反向、重启 | 未执行 |
| S09 | _run 7989、8027、8341。原生分钟 K 线，多空共享，部分拒绝也消费 | 回放抢占已修，批量尾笔与机会持久化待比 | gateway/dsv_facts、kernel/dsv_strategy | 同分钟、拒绝、迟到补齐、批量尾笔、重启 | 未执行 |
| S10 | decide_continuation、7075–7204。夜盘参考、延续信号每日一次 | 已实现，适用盘段与实际日夜切换待验 | strategy/entry、kernel/dsv_strategy | 夜盘末价两侧/等号、无方向、零成交重启 | 未执行 |
| S11 | _run 7079、7750–7765。连续止损限制延续开，可日停 | 已有统计和替代单复核 | strategy/stops、kernel/statistics | 普通与延续止损、开关关、止盈复位 | 未执行 |
| S12 | _run 4036–4037、8170–8186。同向冷却，等于到期放行，持仓品种限额 | 计入外部及在途，缺事实可多挡 | strategy/risk、kernel/dsv_strategy | 两品种、外部仓、在途、到期等号、日切 | 未执行 |
| S13 | is_before_session_close、_near_close_tot。尾盘禁开及平仓，月停另计 | 尾盘样本已有，带仓和特殊终点待验 | scheduling/calendar、kernel/scheduler | 10 分钟、午休、白银 02:30、国债末段带仓 | 未执行 |
| S14 | _run 连续重评品种、停开及恢复 | 有限时窗/总量、历史账本 F5 门槛 | runtime/dsv_simulation、runtime/desktop_bootstrap | 同日结案后再开、重启、到期、单品种停用、下一日 | 未执行 |
| P01 | _run 7275–7276、6508–6556。理论价/实际均价，账户同步可重算 | 冻结计划，原版重算时点待比 | kernel/simulation、protection/reducer | 首笔滑点、追加成交、外部改仓 | 未执行 |
| P02 | _run 7680–7718。按方向和模式判固定止盈/损 | 有单测和部分成交保护，真实退出待验 | protection/reducer、kernel/execution | 两方向等号、跳价、首笔部分成交、有余单 | 未执行 |
| P03 | _run 7540–7568。80% 激活当轮可日停 | 有保护锁存及白银回放，当前版本自然成交未证 | protection/reducer、kernel/statistics | 79.9%、80%、回落、关日停开关 | 未执行 |
| P04 | _run 7551–7576、7680–7688。保护态先 TP 后留利线 | 冻结保护线及统一目标 | protection/reducer、execution/policy | 两方向线等号、越 TP、回踩及成交量 | 未执行 |
| P05 | _run 7663–7716。移动回撤 max(T×比,峰值×比)，含等号 | 回撤已有，显式移动日停与原早退待核 | protection/reducer、kernel/statistics | 零比例、等号、保护抢占、退出后下一信号 | 未执行 |
| P06 | half_close_lots、7596–7659。虚拟剩余一半，ties-to-even 留 1 手 | 按实际成交和在途分别推进 | protection/reducer、kernel/execution | 1/3/5/7 手、部分成交、拒单 | 未执行 |
| P07 | decide_half_close、7618–7659。一轮一档，保本原变量可达性待核 | 真减仓后保本，第二档已有 | strategy/plan、protection/reducer | 同 Tick 跨两档、首档未成、重复回报、回到入场价 | 未执行 |
| P08 | _run 6897–6915。收盘剩 10/5 秒升档，每 3 秒目标刷新 | 有终态门槛及无 Tick 推进 | kernel/scheduler、kernel/execution | 20/10/5/0 秒，撤未确认及部分成交 | 未执行 |
| P09 | _run 7439–7472。断流严格超限，盘段宽限严格超 60 秒，平仓确认后按开关日停 | 180001ms 口径已有。短配置、睡眠、反复失败待比 | strategy/plan、kernel/scheduler | 重复 Tick、静默、休市、拒单及恢复 | 未执行 |
| P10 | _run 7633、7731–7802。同轮可半减再全退 | 共用原目标和在途占量，先后时点待验 | kernel/execution、protection/reducer | 同轮半仓、手动、保护、收盘，累计量不超原仓 | 未执行 |
| R01 | calc_volume 5246–5338。固定、自动、激进、保底一手分支 | 算法已有，有限授权和完整事实影响输出 | strategy/risk、kernel/online_dsv | 同账户同盘口逐分支，核对数量和过滤原因 | 未执行 |
| R02 | risk_based_volume 5173。权益比例除以 S×乘数，零手不缩；缺每手保证金用最新价估 15% | 已有最新价估算，价量单独验 | strategy/risk、gateway/dsv_facts | 理论/委托/最新价不同，零手及可开一手边界 | 未执行 |
| R03 | calc_volume 5264–5308。柜台风险度、已用/冻结/暂占及品种份额 | 现用柜台风险度，完整事实与预占有差异 | strategy/risk、kernel/dsv_strategy | 风险度等号，空仓零与缺字段、有仓缺字段、活动单 | 未执行 |
| R04 | _run 4043–4046、5328–5338。实盘开关、自动量缩限 | 有模式单测，券商实盘待验 | strategy/risk、gateway | 开关、固定/自动、实盘首次一手 | 受阻（真实券商） |
| R05 | _run 7180、8350 发起普通/延续目标计数，7864–7867 达上限日停 | 原生委托与子单尝试会提前触顶 | gateway/dsv_facts、kernel/dsv_strategy、kernel/statistics | 199/200 次目标，重挂、手工、撤单和拒单 | 未执行 |
| R06 | _run 7733–7774、7844–7855。名义点数、目标时点、净亏等号 | 真结案记点，外部平仓反号及单位已有修正 | strategy/statistics、kernel/statistics | 四种退出在目标/成交/结案三个时刻核对 | 未执行 |
| R07 | _run 6005–6046、6786–6825。连续亏损交易日及月停 | 持久月停跨日，原版日复位后持续性待核 | strategy/stops、kernel/statistics | 周末/假日、连续止损、日切及跨月 | 未执行 |
| R08 | _run 6050–6068。实盘日亏，优先权益减昨结，缺值用盈亏费用 | 同公式，另有事实质量与预算 | strategy/risk、kernel/online_dsv | 阈值、缺昨结、未知费、模拟、原仓继续管理 | 未执行 |
| R09 | _run 7261–7376。首次确认均价，严格超过 max(2,S×比) 才退出 | 先到部分先保护，首个完整回报组加权 | kernel/simulation、kernel/engine | 部分成交、明细后补、滑点等号、重复回报 | 未执行 |
| R10 | App.on_start 及 _run。按连接、时段、品种和资格执行 | 账号/时窗/预算、时钟、未知单及比较写入门槛 | runtime/desktop_bootstrap、kernel/online_dsv、kernel/control | 同输入每次改变一项，分别检查策略结果和实际派发 | 未执行 |

### 订单、账本和恢复

| 编号 | 原版行为与证据 | 产品原行为及差异 | 修改位置 | 验收用例 | 状态 |
| --- | --- | --- | --- | --- | --- |
| O01 | _run 4933–4940 自动 TargetPosTask，ManualTradeThread.run 手工直报 | 统一执行器及提交后派发 | kernel/execution、gateway/tqkq_dsv | 方向、开平、数量、限价、顺序及回报 | 未执行 |
| O02 | 随包 target_pos_task 623–667、744–755。价格恶化才撤，终态与逐笔量一致 | 有子单和剩量重报，30 秒待核 | execution/policy、kernel/execution | 恶化/等于/改善、部分成交、撤后成交、终态早到 | 未执行 |
| O03 | SDK 逐笔一致屏障，_pending_margin_total 30 秒只清暂占。未知恢复待核 | UNKNOWN 持续占量，原生迟到事实回补 | kernel/simulation、kernel/historical_orders | 乱序、重复、断线丢回报、空占位和跨日 | 未执行 |
| O04 | ManualTradeThread._resolve_close_offsets 8798 和 SDK 平今/昨规则 | 已实现分桶，柜台混仓待验 | execution/close_routes、kernel/day_recovery | SHFE/INE 分桶和 DCE/CZCE 合并，跨日余单 | 未执行 |
| O05 | ManualTradePanel 快捷开平、选仓、全平；_resolve_close_price 最多等 4 秒 | 主要按钮已有，手工范围、外部仓及取价反馈不同 | desktop/workbench_layout、kernel/control、execution/policy | 快捷量、三种价格、无价超时、全平、选错仓 | 未执行 |
| O06 | ManualTradePanel.on_cancel_order/on_cancel_all_orders、停止撤单 | 自有及审核全撤已有，外部单权限与回执不同 | kernel/native_control、desktop/workbench | 单撤、全撤、柜台变化、拒撤及外部单选择否 | 未执行 |
| O07 | _run 6322–6706 实际多空同步，宽限 max(3,position_sync_seconds) | 按原生批次及归属，缺证据待核 | kernel/external_positions、kernel/equal_lot_review | 加减、双向、反手、等量换仓、宽限两侧 | 未执行 |
| O08 | App.on_stop、StrategyThread.run 3758–3823。停止断开并提示剩仓 | 已禁开及撤单，待接归零结束。已确认有仓继续保护 | kernel/native_control、runtime/dsv_simulation、desktop/workbench | 空仓、带仓、撤单开关、未知、成交归零、关窗进托盘 | 未执行 |
| O09 | _run 7222–7422。默认被动 3 秒，确认超过 60 秒才撤 | 3 秒固定，隐藏值、首笔部分后的剩量待比 | configuration/dsv、strategy/plan、kernel/execution | 3/60 秒等号、部分成交、等待和重报 | 未执行 |
| D01 | persist_daily_state/save_daily_state、原日志。原版逐笔持久恢复待核 | SQLite 单 writer、预占及去重 | ledger/store、kernel/simulation | 重复乱序后重启，对 ID、总量和占量 | 未执行 |
| D02 | persist_daily_state 保存当日状态，带仓高点/保本恢复待核 | 持久保护、阶段、高点和原目标 | kernel/engine、protection/reducer | 保护锁存、半仓未确认，重启核对 | 未执行 |
| D03 | 原状态按品种与柜台日，策略及手工会话独立 | 显式账号/环境/柜台日/boot | accounts、kernel/simulation、kernel/day_recovery | 切账户、换日、旧窗口和迟到回报 | 未执行 |
| D04 | --compare-fills 与平仓核对日志，跨日原单能力待核 | 同日可关联，跨日 SDK 空占位 | kernel/historical_orders、runtime/comparison | 完整原单、空占位、跨午夜 | 未执行 |
| D05 | StrategyThread.run 重连恢复当日状态 | 空仓恢复样本，F5 已结案账本被拒；带仓跨日待验 | runtime/desktop_bootstrap、kernel/simulation | 已结案、在途、UNKNOWN、跨日迟到成交 | 未执行 |
| D06 | _run 6508–6556 聚合仓同步和目标重定，批次级接管待核 | 人工接管、批次证据和独立核对 | kernel/external_positions、kernel/equal_lot_review | 已有仓、混仓、反手、等量换仓、缺历史 | 未执行 |
| D07 | Logger.log、save_config 异常路径，磁盘满后行为待核 | 账本失败阻挡发送，比较写入失败可禁开 | ledger/store、runtime/comparison、runtime/strategy_log | 分别注入账本、日志、慢 IO，检查发送及保护 | 未执行 |
| D08 | config.json.bak 和 state。完整恢复入口待核 | SQLite 一致备份 API，用户恢复工作流待补 | ledger/store、desktop/data | 配置备份、账本快照、缺文件、错账户、旧版本 | 未执行 |

### 参数、报表、通知和桌面工具

| 编号 | 原版行为与证据 | 产品原行为及差异 | 修改位置 | 验收用例 | 状态 |
| --- | --- | --- | --- | --- | --- |
| C01 | App.on_add/on_edit_row/on_delete。同页编辑和保存 | 已接同页，错误及生效边界待比 | desktop/symbol_editor、configuration/editor | 单行、多行、空、零、坏月份、重复、取消和重开 | 未执行 |
| C02 | App.on_save、load_config/save_config。保存、备份、坏文件 | 预览、修订和业务导出已有，恢复反馈不同 | configuration/files、desktop/workbench_layout | 修改保存、重开、坏文件、恢复备份 | 未执行 |
| C03 | App.on_import_symbols/on_export_symbols。八列 Excel | 已有 XLSX/CSV，解析和整批操作待比 | configuration/symbol_xlsx、configuration/symbol_csv | 八列往返、缺列、坏行、零值、取消 | 未执行 |
| C04 | App.on_save_template/on_apply_template/on_delete_template。33 项 | 主控/交易参数支持，按预览及修订 | configuration/templates、desktop/workbench_layout | 保存、覆盖、应用、删除、取消、运行生效 | 未执行 |
| C05 | App.on_auto_calc/on_auto_apply/on_auto_copy、offset_calc.calc_one | 五合约同输入一致，联网按钮及直接应用待齐 | configuration/offsets、desktop/calculator、runtime/auto_offsets | 同日线和振幅、无/未完日线、缓存、所选应用 | 未执行 |
| C06 | parse_rule_pdf、on_import_rules/on_apply_markup。前 16 页，行情优先，严格超过 0.5 个百分点提示 | 导入和加价已有，真实表和缺值待核 | configuration/instrument_rules、desktop/instrument_rules | 文本/扫描、错行、重复、缺行情、偏差等号 | 未执行 |
| C07 | load_config 2460–2580 有迁移，App.on_start 读取当时配置 | 部分迁移，冻结旧计划 | configuration/dsv、runtime/desktop_bootstrap | 旧配置、保存、启动、更改再启动、旧保护 | 未执行 |
| V01 | App.update_account_display/update_orders_display。资金、品种、持仓、委托成交及筛选 | 保证金率位置、三栏、计数及筛选差异 | kernel/workbench、desktop/workbench_layout | 同快照逐列，品种/方向/盈亏筛选，部分平仓 | 未执行 |
| V02 | App.update_kline_chart 60 根/6 秒，权益图 3 秒，区间及 PNG/CSV | 原生 48 根，采样回退，权益历史与导出口径待比 | gateway/tqkq_dsv、kernel/workbench、desktop/workbench_layout | 60 根 OHLC、空缓存、区间、日收益和累计 | 未执行 |
| V03 | write_daily_report 921–1210。按品种、交易明细、每日盈亏 | 账本 XLSX 已有，数据源和三表列不同 | reporting、desktop/reports | 同柜台日和空日逐表核对、重复及缺值 | 未执行 |
| V04 | _comm_display、平仓核对。柜台与估算、价差点数 | FIFO、确认/估算分开，零值回退不同 | accounting/book、kernel/money、reporting | 明确零费、缺费、分笔、半平、跨日和滑点 | 未执行 |
| V05 | 成交对照.bat、--compare-fills。按合约及近 180 秒 | 严格匹配已有，原对照结果待齐 | desktop/entrypoint、reconciliation、reconciliation_export | 四列、缺量、并列、跨午夜、重复、外部多余 | 未执行 |
| V06 | Logger.log、App.poll_queue/_filtered_log_rows。分类、清空、复制和 Excel | F5 漏路径，缓冲和事件覆盖不齐 | runtime/strategy_log、runtime/comparison、desktop/workbench_layout | 开/平/半仓/保护/撤改/错误/手工，筛选复制清空导出 | 未执行 |
| N01 | App._build_tab_settings/_notify_callback。总开关、四类和测试 | 有 Windows 提示音，默认及触发时点待比 | desktop/notifications | 音频替身验证四类、测试、关闭 | 未执行 |
| N02 | App.on_test_dingtalk/_notify_callback。加签、通知号码及测试 | 签名、保存已有，真实发送待授权 | desktop/dingtalk | 固定时钟、发送替身、号码、错误 | 受阻（真实外发） |
| N03 | App._notify_callback。策略保证金提醒的阈值在 _run 4422 置 0 | 产品启用时可能多提醒 | desktop/notifications、kernel/workbench | 部分成交、保护、半平、保证金边界及通知时点 | 未执行 |
| N04 | App._notify_callback 15681–15697。同品种同类型节流，启动清空 | 后台发送与节流已有，持久重试待核 | desktop/dingtalk | 同/异品种、不同类型、边界、失败、重开 | 未执行 |
| N05 | StrategyThread.run、_run 6244–6307。断线、强平重试及告警 | 待核对及部分拒单告警已有 | runtime/dsv_simulation、desktop/notifications | 断流、拒单、未知及恢复的内容、次数 | 未执行 |
| U01 | 随包启停守护。当前用户启动目录 | 当前用户计划任务脚本 | release/portable/可选_自启动守护 | 用替身验证启用、停用、重复及路径错误 | 未执行 |
| U02 | DSV守护.bat。60 秒检测进程，--autostart 拉起 | 按进程路径及会话，策略启动链有差异 | release/portable/可选_自启动守护、desktop/entrypoint | 进程缺失、托盘、同名异路径、停用 | 未执行 |
| U03 | App._schedule_tick。每 30 秒检查，窗口两端含等号，开软件自动另走入口 | 配对可定时，普通 F5 和计划启动未贯通 | desktop/startup、desktop/session、runtime/launch | 08:55/20:55、末端、重复、F6、无授权 | 未执行 |
| U04 | 原版网络/时钟脚本，_run 6081–6103 分段性能 | 有脚本和门槛，缺逐 Tick 关联 | runtime/dsv_simulation、transport/status_client | 替身时钟、缺样本、慢点、重连 | 未执行 |
| U05 | 原版热更新、工作台附着入口待核 | 产品可附着同一执行器，签名和带仓切换待证 | desktop/workbench、runtime/launch | 窗口重开原执行器，验证订单、保护和日志 | 未执行 |
| U06 | 原版 A/B、运行计划纠错和回退入口待核 | 计划版本及恢复原语 | kernel/configuration、kernel/day_recovery | 旧计划和恢复；原版入口取证后补用例 | 未执行 |
| U07 | EXE、_internal 及随包工具，签名和干净机待核 | 已有旧便携包，源码工作区版本独立 | release | 获授权出包后核对解压、资源及正常入口 | 受阻（待打包指令） |
| L01 | main 16842–16870。首启协议接受或退出，守护有独立分支 | 版本、帮助和说明已有，首启流程待补 | desktop/entrypoint、desktop/workbench_layout | 接受、拒绝、重开、守护启动 | 未执行 |
| L02 | license 模块和 App.on_license/_license_blocked。授权状态、到期及开平限制 | 许可入口缺，原版服务结果待核 | desktop/entrypoint、desktop/workbench_layout | 本地替身有效/过期/拒绝/不可用，分别查开平撤 | 受阻（原版远端服务） |
| L03 | App.bind_hotkeys/on_change_font_size/on_close_window。F5/F6、适配和托盘 | 默认托盘已修，字号、键位及窗口几何有差异 | desktop/tray、desktop/workbench_layout | 新/旧设置、双击恢复、菜单退出、F5/F6 | 未执行 |
| L04 | 随包协议、推送、守护、网络和时钟说明 | 有说明，首次、迁移与故障流程待对齐 | README.md、release/portable/使用说明.txt | 按文档及替身走首次、迁移、故障定位 | 未执行 |

### 按原版入口补项

| 编号 | 原版行为与证据 | 产品原行为及差异 | 修改位置 | 验收用例 | 状态 |
| --- | --- | --- | --- | --- | --- |
| A07.1 | main 16762、16830。--allow-multi | 仅有按账户排他。对齐默认及分账户并行入口 | desktop/entrypoint、runtime/instance | 默认第二实例、显式异账户、同账户不重复执行 | 未执行 |
| M01.1 | App.on_subscribe_quotes 15408。一次刷新主力、规格及保证金 | 主要随执行器取得 | desktop/workbench_layout、gateway | 启动前订阅，进度、失败、应用和关闭 | 未执行 |
| M06.1 | _run 5023、6001、6147、update_account_info 5511、App._update_quote_lag。双分支、秒级及状态阈值 | 旧界面算最旧报价年龄 | runtime/dsv_simulation、kernel/server、desktop/workbench_layout | 两合约、静默、负差、无值、3/10/120 秒 | 未执行 |
| M06.2 | _run 5909–5942、6081–6103。60 秒行情及慢轮次诊断 | 原配置未接实际显示 | runtime/dsv_simulation、transport/status_client | 开关、60 秒边界、慢点、匹配身份和恢复 | 未执行 |
| S03.1 | is_in_auction 2138–2155、_run 7870。含 09:00、21:00 整分钟 | 保存开关，盘段门槛有差异 | strategy/entry、scheduling/calendar | 08:55、09:00、09:01 和夜盘镜像 | 未执行 |
| P06.1 | load_config 2486–2491、decide_half_close 1872–1877。旧半仓回落迁移 | 当前配置缺旧分支 | configuration/dsv、strategy/plan、protection/reducer | 迁移标记有/无、二档开关、等号 | 未执行 |
| R05.1 | update_account_info 5520。逐品种策略目标计数求和 | 按已匹配柜台开仓订单计数 | kernel/workbench、kernel/statistics | 同一目标多子单、混手工单、跨日 | 未执行 |
| O05.1 | ManualTradePanel.on_connect/on_disconnect/on_live_login。独立手动会话 | 依附所选范围 | desktop/workbench_layout、runtime/launch | 并行打开、加载、失败、关闭，核对策略侧状态 | 未执行 |
| C07.1 | load_config 2460–2580。年龄、手数、次数、滑点、追价窗口及开关迁移 | 显式导入只迁移部分值 | configuration/dsv、runtime/desktop_bootstrap | 旧配置、显式零/关、缺省、重复加载 | 未执行 |
| C07.2 | _run 4112–4117 隐藏 resume_gap_seconds/passive_wait_seconds，CHASE_BASE_NOW 固定 false，另两参数只找到初始化 | 部分字段只保存，部分固化 | configuration/dsv、strategy/plan | 变更各字段并观察实际消费者，参考权益只用于建议手数 | 未执行 |
| V01.1 | update_account_info 5635–5646、App._refresh_symbol_status 14331–14338。活动栏实时保证金率 | 品种信息已有，状态栏漏接 | configuration/instrument_rules、kernel/workbench、desktop/workbench_layout | 正值、零/缺值、静默、断线、恢复、账户比率 | 未执行 |
| V01.2 | update_account_info 5675–5715。强平待确认优先，其次停开，最后活动 | 普通减仓也可进待确认且与日停重叠 | kernel/workbench、desktop/workbench_layout | 几何、净亏、到期、上限、收盘、强平/日停并存 | 未执行 |
| V02.1 | App.on_toggle_kline_chart 15713、on_toggle_equity_chart 15724。K 线重启生效、权益即时 | 产品图表可见性和订阅待对齐 | desktop/workbench_layout、gateway/tqkq_dsv | 切两开关，重启后检查订阅与可见性 | 未执行 |
| V06.1 | DSV 直接用当前日志队列 | F5、旧定时及配对使用不同目录 | runtime/dsv_simulation、kernel/workbench、desktop/workbench_layout | F5、定时、重开、附着、换账户/new boot、错误目录 | 未执行 |
| U03.1 | main 16845–16953。--autostart、协议、开市前检查 | 守护和主控触发链未通 | desktop/entrypoint、desktop/startup | 协议有/无、开市/临近/休市、手动停止、授权过期 | 未执行 |
| L02.1 | main 16764、16792–16813。--license 写授权状态.txt 后退出 | 缺可比诊断入口 | desktop/entrypoint | 本地授权替身、字段、退出码及无交易连接 | 未执行 |
| C06.1 | App._build_tab_info 13210–13234。银行转账时间表展示占位 | 缺原版的占位显示，业务只计展示 | desktop/instrument_rules | 打开并核对静态表与提示 | 未执行 |

## 开发验证与待核

- 既有同输入离线对照覆盖 2,784 个开盘样本、1,178 次 SDK 价格比较及白银 80% 保护片段。归档不包含完整原生路径。实施前入场、手数、报撤价格 82 个定向单测通过。 [归档结果](../output/validation/dsv-runtime-replay-20261112/result.json)
- 首分钟和保本的原版变量、月停早退、外部平仓点数/金额、平仓日志筛选重载均待实机核对。按已确认选择复现可见行为，涉及错账、重复报单和失去保护的分支单列。F6 有仓或未决时禁开并继续保护，归零及终态核对后结束会话。
- 取证和源码开发使用离线定向单元测试。独立验收由用户安排另一 Agent。获准恢复后先只读对时、对齐数据并核对两个模拟账户均为零仓及零活动委托。塑料和沥青原日停自然解除后与棕榈油补测，未触发分支保留未覆盖。恢复在线、系统校时和打包按单独指令执行。

## 本轮源码落点与验收交接

基线提交 `944ab1115e4ca09e48c05e98de00b5522bbaf18f`。交接对象是该提交加当前工作区差异。原棕榈油补观察和默认关窗进托盘的改动均在工作区。上表的「当前」记录修复前状态，「未执行」指本轮独立验收。

被测工作区的 SHA-256 为 `3348fd8c2f2fea892e52baa516e4c5b8ea7dc833ced76c31f7df365eee2da47a`。范围是 281 个已跟踪或未忽略的新增文件，包含 `src`、`qa`、`tests`、`release`、`scripts/check.ps1`、`pyproject.toml`、三个 `requirements-*.lock` 和 `docs/spec-*.json`。按 UTF-8 相对路径字节排序，逐项拼接路径、NUL、原文件字节的 SHA-256 小写十六进制和 LF，再计算总哈希。

Windows 源码的正常入口是现有虚拟环境内的桌面入口函数。可选参数 `--dsv-config` 用于显式读取本机 DSV 业务配置。现有账本、账户档案和凭据继续按工作台的选择绑定。

```powershell
Set-Location -LiteralPath 'E:\online-trader'
.\.venv\Scripts\python.exe -B -c "from online_trader.desktop.entrypoint import main; raise SystemExit(main())"
```

| 条目 | 本轮实现及证据 | 仍需核对 |
| --- | --- | --- |
| V06 / V06.1 | 当前所有者通过 `workbench/1` 发布运行目录。启动阶段用回执，旧执行器按账户和 boot 匹配归档。400 毫秒增量读取，按事件分类及过滤后的行复制、导出。归档保留原始柜台、策略与本地执行身份。见 `test_dsv_workbench_parity`、`RuntimeLogUnitTests`、`test_simnow_comparison`。 | 当前 DSV 对部分止盈、止损和半仓标签在实时追加、切换筛选和 Excel 导出的差别仍待实机核对。 |
| A04 / M06.1–2 / U04 | 报价改变时按配置顺序用 Windows 时刻减秒级源时刻更新延迟，静默时使用最大到达间隔。工作台约每秒请求所有者缓存。启用 `tick_diag` 后，执行器写 `market-latency.jsonl`，各工作台写 `workbench-latency-*.jsonl`，每个文件有定量轮转。`qa/replay_dsv_archive.py --executor-latency` 可合并同一桌面的 `--workbench-latency`，按合约、柜台日、盘次、来源轮次与原生 Tick 匹配，输出样本量、匹配率及各段毫秒分布。开盘和复盘取新盘段首次匹配且距开盘不超过 60 秒的样本，重连取恢复后首个匹配，静默汇总以活动盘段为限。见 `MarketMonitorTests`、`WorkbenchClientTests`。 | 4.3 秒缺同 Tick 证据，原版探针需现场取证。SDK 首次可观察时间取 `wait_update` 返回。没有匹配源时间的批量历史 Tick 保留为缺样本。 |
| V01 / V01.1–2 | 同份 SDK 报价提供最新价、乘数和每手保证金。保证金率两位百分比，仅在活动栏追加。强平待确认、日停及活动互斥，空仓也按最后盘段的收盘保护时点分类。普通平仓、半仓和未成交终态异常留在持仓或阻挡详情。资金使用率和 SDK 风险度分别显示。 | 等待实机核对状态切换、交易日边界与断线后首笔刷新。 |
| M01 / M01.1 | 「手动下单 → 加载合约」从所选账号的行情连接查询 `FUTURE` 目录，可按代码、品种别名和月份搜索。失败时保留已加载目录或已配置合约。主控「订阅行情（新主力）」在无执行器时读取所选合约及主力、规格、保证金和手续费，查询完成后关闭。活动执行器在场时主力订阅使用其已接受快照。见 `test_startup_metadata`、`test_desktop_startup`。 | 真实目录、内置兜底表、无行情重试和手动独立交易会话待验。 |
| O02–O03 / O08 / D05 | F6 按原范围持久记录停开，柜台仓、活动单、UNKNOWN 和成交终态齐全后退出。已结案历史可随同一账本开启新范围，旧计划和柜台成交归属保留。迟到且矛盾的旧回报禁开、记原始证据并要求恢复原范围。见 `test_dsv_stop_command`、`test_online_dsv` 和 `test_crossday_native`。 | 原范围恢复时如需原单回补，仍走原归属链。自动执行真实撤改、部分成交、保护和平仓等待双端对照。 |
| R05 / R05.1 / R10 | 普通及延续策略目标按品种、柜台日去重。达到上限前已接受的同目标撤改不重复占业务次数。手工单、报撤、原生子单和范围尝试预算分别保留。`SEND_ABORTED` 保存最终校验原因。`qa/replay_dsv_archive.py --trace` 输出比较归档的决策、报撤、逐笔和保护状态，最多最近 10000 行。 | 风险预算、范围时窗、5 秒行情时效、1 秒时钟、当前盘次与路径、事实完整性和比较写入约束仍会影响派发。实际差异待用相同 Tick、账户事实和回报复查。 |
| S03.1 / P06.1 / O09 / C07.1–2 / M08 | 按原报价分钟处理集合竞价开关。原版 JSON 导入应用已证迁移、缺省和 `code_new`。业务档案直接加载保留已保存值。支持被动等待、旧半仓回落和断档恢复提示阈值。见 `test_configuration`、`test_dsv_entry`、`test_dsv_protection`。 | 盘前实际报单能力仍受当前盘次和行情事实限制。首分钟变量、保本可达性和月停早退仍待核。 |
| U03 / U03.1 / A07.1 | 普通工作台可安排一次定时，30 秒检查，复用 F5 的账号、参数和启动回执，行情日历复核通过后启动。当前安排保存在窗口内，到本地午夜过期。F6 取消等待。守护 `--autostart` 打开工作台。主入口支持默认单实例和显式 `--allow-multi`。 | 守护重拉后的自动策略启动、已接受协议的启动分支及跨重开定时安排尚未对齐。 |
| V02 / V02.1 / V03 / N03 / L02.1 / C06.1 | 原生 K 线 60 根、6 秒缓存，图表开关分别按重开和即时生效。日报提供按品种、交易明细、每日盈亏及原资金总览，金额取确认账本、点数按名义统计。策略运行的保证金通知阈值按发行版为零。新增本地授权状态与银期转账占位表。 | 隐藏 K 线时的 SDK 订阅行为、权益历史回填、原版日报日志附列和真实许可服务仍有差异。 |

### 验收状态与范围边界

- **开发单测通过** 688 项。策略、保护、风控、柜台替身和账本 369 项，日志、监控、IPC、启动、参数与报表 212 项，计划协议、回补边界和换月 107 项。已检查 7 个协议资源的校验值、Python 语法和守护脚本语法。**未执行**包括当前版本的独立桌面验收和在线两端操作。**受阻**包括等待在线授权的对跑和等待另行授权的制品验收。未触发分支保持未覆盖。
- **待核或待实现**包括原版内置合约兜底表、真实券商连接、独立手动会话、首启协议、远端许可、通知实发、历史权益回填和其余表中差异。名义点数与 FIFO 资金金额、账本归属和未决数量、F6 持仓保护仍各自保留其证据。
- 上述落点使用当前 DSV 发行版及原随包 SDK。涉及 `Order.is_error` 的拒单识别使用 `_internal/tqsdk/objs.py:460` 的明确错误标志和一致的终态逐笔量。无错误标志的终态按已知回报处理，原始回报和本地解释可复查。
