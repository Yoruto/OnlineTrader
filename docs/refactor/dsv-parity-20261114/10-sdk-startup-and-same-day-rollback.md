# 候选 SDK 启动与同日已成交回滚

## 候选启动入口

`dsv replacement-prepare` 和 `simnow replacement-prepare` 核对已冷迁移的候选库、原库、业务配置、日历及账户范围。此入口可离线执行。候选范围用各自的 `scope --replacement` 生成，与旧 `run` 的范围分开。支持已归一的固定配置，配置品种和授予品种必须完全一致。

`dsv replacement-run` 和 `simnow replacement-run` 使用现有快期和 SimNow 连接工厂。该命令会连接指定模拟账户，应使用另行授权的窗口。参数为 `--scope`、`--profile`、`--calendar`、`--db`、`--output`、`--pipe` 和 `--credentials` 或 `--account-store`。先取得原内核兼容的同账户锁，验证原库水位及候选迁移收据，再走 SDK 3.10.4 工厂。候选库绑定该次有限范围，重启时保留已消耗次数，无法靠换范围文件重置。

同一账户的前迁移清单使用原账户、原 provider 和独立候选目录。账户锁只取得一次。旧账本与迁移收据保存原原生订单和成交的核验材料。启动及重连对仍可见的旧委托逐笔复核，旧委托变化或外部活动会阻断相应确定性结论。已有自有持仓在重启后继续做已授权柜台日内的保护；新开仓保持停止，用户必须再次发出启动命令。没有授权的新柜台日需要新的恢复范围和现场操作，当前不会自行跨出范围送单。

候选使用同用户命名管道。现有离线界面可附着到该管道并按快照区分连接模式。SDK 是唯一行情、持仓、委托和成交输入，管道保留状态查询和命令回执，拒绝注入行情与回报。停机先停止新增，确认原生空仓、无待核委托和逐笔后用 `shutdown`。SDK 断开与清理错误记录到本次状态文件。仍须现场核对 SDK 同步通知、实际柜台时间和跨日保护。

## 同日反向转换

```powershell
python -m online_trader offline-dsv rollback-same-day --source legacy.sqlite --db candidate.sqlite --input profile.json
python -m online_trader offline-dsv rollback-same-day --source legacy.sqlite --db candidate.sqlite --input profile.json --target isolated-reversal
```

工具同时核对两个账户身份及其锁、旧源水位、候选柜台日、停机状态、明确同步的四仓桶、原委托终态、逐笔及原周期分配。只有可完全核对且已有成交的同一柜台日支持转换。无法映射的状态、无原触发资料的结案、跨日成交、待核分配以及再次前迁移旧反向交接库均会报告阻断。

正式输出含旧 schema 的 `legacy-snapshot.sqlite`、新事实库的 `candidate-archive.sqlite` 和 `rollback-receipt.json`。旧库副本以追加事件保存原订单、原生成交和来源身份，原始前缀逐项保留。旧读取入口恢复当日目标数、分钟机会、日/月停、夜盘参考、延续消费、净亏、名义统计和止损冷却；交易启动授权与挂单责任均不由转换事件授予。普通历史报表将转换逐笔作为已结案只读事实。供应商乘数和费用缺失的部分继续显示未知。

当前回退读取器随本次源码交付。早于本提交的旧安装制品未实现该追加事件读取。新旧原库只读，发布仅发生在新的演练目录。需在真实账户重新核对原生事实后才能按运行手册现场接管。
