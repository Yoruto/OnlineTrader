# 原版逐轮证据与显式订单离线链路

本轮基于首阶段 `b45f353`。替代链从逐轮输入到业务决策、单账户显式子单、成交与保护，使用独立 SQLite 事实库。当前入口为离线回放，生产工作台、真实 SDK 连接和旧库仍由原入口负责。

## 原版每轮实际看到的内容

对冻结 ZIP 中 `StrategyThread._run` 的代码对象做 SHA-256 核对与反汇编。随包 SDK 3.10.2 的版本和目标任务源码指纹见基线清单；`tqsdk/api.py` SHA-256 为 `264fff3e3979a156294d8bca745c8aa6c26136c8cbe58ef2bc7ceffd8b811c96`。独立 Agent 另行复核下列顺序。 [原版证据与独立样本](../../../output/dsv-parity/round-controller-acceptance/reference-sequence.md) · [冻结指纹](../../../qa/dsv_parity/fixtures/baseline-manifest.json)

| 顺序 | 发行版定位 | 本轮离线实现 |
| --- | --- | --- |
| SDK 推进 | 5892 行 `wait_update(deadline=time.time()+0.5)`，返回值被丢弃。SDK `api.py` 2105–2136、4125–4131 把 `pack.data` 顺序合并到可见对象，之后更新 K 线表。5919 行 `is_changing(..., 'datetime')` 位于诊断分支 | 每份记录代表一次返回后的处理轮。无新报价可读取同会话上次快照并推进计时。 |
| 账户和品种 | 5950–6110 行刷新账户、日/月状态。6112–6119 按 `symbol_config` 顺序读当前 `quotes[cont_code]`，6157–6701 处理仓位同步和较早退出分支 | 先串行接受本轮原生订单和逐笔成交，再按配置顺序读每个品种的最终快照。输入保留批内报价顺序，只有最后一行进入业务。 |
| 会话出口 | 6786–6840 处理日切与行情状态；6881–6998 先判断收盘，7075–7204 为夜盘延续 | 当前单柜台日可注入最终收盘时刻。收盘路径先于待确认入场和固定保护，支持三秒刷新及 10/5 秒价格阶梯。延续和日切待后续取证。 |
| 成交和保护 | 7210–7422 处理被动等待、实际仓位确认、理论价与实际成交价分离及超时。7436–7802 处理断流、80%、半仓、固定或移动保护 | 本轮接受部分成交后可马上求保护目标。80% 激活后先判断止盈再看保护线。普通路径先半仓，再按止损、止盈、移动回撤求共同目标。3954 行的移动激活比例 0 禁用移动分支；开着 80% 但尚未触发时仍可移动止盈。累计成交价用于事实追查，固定保护样本以原理论价为基准。 |
| 普通入场 | 7858 要求原方向及 `pending_entry` 为空；7988–7991 读当前 K 线尾行；8027 共享分钟机会；8133–8139 先多后空；8313–8325 同方向一秒去重；8341–8350 消费后递交正/负目标 | 普通入场按有效快照执行一轮。批内中间触线、尾态回来时无开仓。设置的拒绝分支消费当前分钟；跨方向共享分钟，秒级重试约束仅限同方向。 |
| 子单执行 | SDK 345–347 设置目标队列；448–454 可交易时取最新目标。追价 690–697 仅价格恶化撤单；744–756 等终态和逐笔成交量一致 | 目标与子单分开保存。买涨、卖跌才因追价请求撤单。撤单返回、30 或 60 秒经过及终态先到都不释放旧量。终态明细齐备后才对剩量生成新子单。 |

以上来源是冻结代码对象和 SDK 源码控制流。现有历史归档无法还原原版真实的全部 `wait_update` 分组、批内对象变化、账户事实和每次墙钟读取。离线样本以这些源定位构造预期；柜台回报时序和原程序长链行为仍需独立对跑。

独立复核覆盖了保护参数的源码边界。3958–3959 行把显式零保护激活比例和留利比例解释为 0.8、0.5；8034 行的禁止追价共用有效激活比例。半仓首档比例仅在 `(0, 1)` 内生效。二档原始比例夹在 `[0.05, 0.95]`，换算点数后若小于或等于首档，取首档点数的 1.5 倍。实现沿用原版的计算顺序。 [保护取证](../../strategy-dsv-evidence.md#参数解释数值零与半仓数量)

## 离线入口

```powershell
.\.venv\Scripts\python.exe -m qa.dsv_parity.replay_session --input .\rounds.jsonl --db .\replay.db
```

首行 `type=session`，提供 `account_key`（以 `DEV:` 开头）、`provider=OFFLINE`、`trading_day` 和规范化 `DSV_SETTINGS/1` 的 `profile`。后续 `type=command` 或 `type=round`，每项含稳定 `id`。同 ID 同内容返回持久收据，内容冲突拒绝。

- `command` 的 `action` 支持 `START`、`STOP`、`CANCEL`、`REDUCE`。撤单需要 `child_order_id` 和已绑定原生订单身份。减仓需要 `contract_id` 和 `quantity`，与保护共享剩余目标，并在随后有效轮次检验持仓和派发。显式手工开仓及全账户撤单尚未接入。
- `round` 含持续递增 `sequence`、`session_id`、UTC `wall_time`（尾缀 `Z`）和非负 `monotonic_ms`。`market` 覆盖该配置全部品种，每行含 `contract_id`、`session_open`、`session_close`、`opening_session`、`long_qty`、`short_qty` 和有序 `ticks`。可附 `entry_close`、`liquidation_at`、合约手数边界及 `external_orders`。固定手数通常需 `available`、`margin_per_lot`，缺后者时按 `multiplier` 和最新价估算；自动手数另需资金与保证金字段。以规范十进制字符串传金额和价格。盘口保留空值、`0` 或字符串 `NaN`。
- 每条 tick 提供 `native_time`、`candle_id`、`last`、`day_high`、`day_low`、`bid1`、`ask1`、`tick_size`、`lower_limit`、`upper_limit`。`wall_reads` 可单独注入 `session`、`opening`、`entry_close`、`quote_age`、`cooldown`、`chase`、`submit`、`pending_start`、`price_context`、`pending`、`protection`、`close`；缺某读取时使用该轮固定 `wall_time`，可由回放明确选择分开提供。单调时刻只用来验证会话内输入顺序。
- `native` 为有序原生事实列表。`kind=ORDER` 提供 `child_order_id`、`native_order_id`、`status`（`ALIVE/FILLED/CANCELLED/REJECTED`）及累计 `filled_qty`。`kind=TRADE` 提供相同订单身份及 `native_trade_id`、`quantity`、`price`。附带账户、柜台日、合约、方向或开平标记时必须与该子单匹配。可通过公开 `DsvSession` API 读取首个收据的子单 ID 后构造后续回报，或用同一个离线 DB 多次回放。
- 输出逐轮决策和准备动作（含快照摘要、规则 ID、输入/配置身份、委托/撤单），末行是状态和本进程记录的端口调用。`DsvSession.prepare_shutdown()` 列出未核齐子单和持仓。CLI 记录端口不进行撮合，只接受显式注入的成交。

## 事实与恢复

事实库用独立 `application_id` 和 schema version，账户、来源、柜台日、profile 哈希写入绑定，沿用账户级 Windows 排他锁。任一轮的原生更新、业务状态、幂等收据和待发动作在同一 SQLite 事务提交。端口回执是随后单独登记的结果；空结果及 `UNKNOWN` 均保留原子单等待核对。重启不重放准备动作，旧命令或轮次重传也不会给子单新身份。撤改以同一意图、下一 `ordinal` 和原父单绑定。成交以库级账户、来源、柜台日加原委托/成交 ID 去重，原始 ID 保留，矛盾或超量回报拒绝整个输入事务。当前离线协议要求原子单在终态后到达的成交明细与声明累计数量一致；与已核齐终态矛盾的柜台修正恢复是后续工作。

所选场景是单柜台日、无外部仓的普通策略、自有原生订单，支持多配置品种。SHFE/INE 按本日仓生成 `CLOSETODAY`，其他交易所用 `CLOSE`。持仓量与原生成交不互证时不派发新子单。保留旧的定时、夜盘延续、半仓后保本、入场滑点强平、断流、业务月停延续、跨日/今昨混合仓、外部仓接管及桌面操作路径，替代链尚未承接这些行为。原版持仓重同步可能改变理论锚点，该场景未纳入本轮固定保护对照。发布制品、柜台接入、切换和旧链退役均在后续阶段。

两个区别需要在接入真实 SDK 前复查。其一，原版应用提交更新后的目标，再由下一次 SDK 推进读取并可能合并目标；当前回放在该轮提交后向记录端口派发计划。其二，当部分成交已经需要保护时，替代链收紧原开仓意图并管理已证实的自有量，同时保留开仓残单占用，待迟到成交确认后更新退出量。原版 `TargetPosTask` 的队列、平仓目标到真实子单的先后以及部分成交后的再同步需实测。当前这两项分别标注为调度差异和工程事实约束。

## 独立验收

独立 Agent 通过回放 CLI 及公开 API 验收 30 项，核对两侧成交、撤改、保护、同轮快照、账户排他、提交失败和发送边界进程中断。修复了保护零值、半仓阈值和快照引用问题，然后复测通过。新增两条常规回归覆盖输入副本和终态早于成交明细的路径。 [独立测试报告](../../../output/dsv-parity/round-controller-acceptance/test-report.md) · [验收日志](../../../output/dsv-parity/round-controller-acceptance/acceptance-7bc5197.log)
