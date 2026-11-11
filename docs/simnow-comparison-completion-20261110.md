# OnlineTrader SimNow接入与DSV双侧对比功能完成记录

版本：`0.1.0.dev21`。日期：2026-11-10。工程：`E:\online-trader`。

后续进展：用户随后授权实际校时及完成今晚本地准备，已完成并保持未连接；当前状态见[晚间准备与校时记录](night-readiness-20261110.md)。下文保留本页生成时点的开发事实及限制。

本轮已补上SimNow适配器、双侧对比记录、系统时钟偏差门禁和源码启动入口。沿用原DSV策略、风控、保护、单owner/writer和CommitAck后发送链。当前没有连接账户、读取现有账户密码、发起NTP请求或实际报撤单。SimNow真实登录和交易回报的首轮确认仍待运行时取得。

## 已完成的功能

| 功能 | 当前实现 |
| --- | --- |
| SimNow交易通道 | 固定使用`TqAccount("simnow", …)`，行情使用独立`TqAuth`。固定SDK版本3.10.4，不接受任意真实期货公司、前置地址或回退模拟器。 |
| 独立账户与范围 | 新增`SIMNOW_DSV_SIM/1`，账户键、gateway、账本broker身份、成交命名空间与快期分离；参数、日历、柜台日列表、时间和额度绑定原范围。旧KQ范围不升级、不复用。 |
| 原策略执行 | 复用原DSV信号、自动手数、资金风控、多空、今昨分桶退出、半仓/保本/保护、原单撤改与恢复。原送单UNKNOWN保持预占，不重放。 |
| 凭据准备 | 新增交互式Windows凭据保存及无密码引用文件；本地保存不登录。也支持明确外部四字段JSON，密码不进入scope、argv、比较文件或错误消息。本轮没有调用凭据入口。 |
| 当前DSV参数核对 | 只读提取原`config.json`四个业务区段；启动前、启用前和发送前核对，运行中定期复核。参数变化暂停新增，原保护继续。 |
| 原生事实记录 | 自动记录原账本已提交事件、信号/风控输入和结果、来源时间/epoch、订单、分笔成交、初始及定时账户快照。每条JSONL有内容hash及原账本序号。 |
| DSV日志采集 | 只读增量跟踪原日志，保留原文件名、字节位置和行hash；识别开仓委托、开仓均价、过滤及平仓触发。部分行等待完整后读取；截断/替换留缺口记录。 |
| 双侧对照 | 按合约、明确交易日、方向、事件类型和时间窗口形成唯一候选。输出时间差、量差、理论价一致性、价格差；双侧缺失及歧义分别可见。 |
| 完整开平成交总量 | 可追加DSV快期原生成交CSV，按柜台日/合约/方向/开平比较总手数与加权均价，输出`native-totals.csv`；严格核来源账户/模拟环境/合约/交易日和重复成交ID。 |
| 采集故障处理 | 对比文件/状态文件写入失败暂停新增，原owner继续管理已有退出；不会把采集器变成第二交易写入者。 |
| 独立终态核对 | 关闭原sender后，按同一SimNow provider另建只读核对连接，复用原订单、成交、数量与归属证明。 |

接入方式对照了[TqSdk 3.10.4官方SimNow示例](https://doc.shinnytech.com/tqsdk/latest/reference/tqsdk.api.html)。本轮采用SDK交易服务，无需另写CTP驱动或配置一组可能过期的前置IP。

## 时钟与今晚的输入

每秒记录Windows UTC、NTP/单调参考UTC、两者偏差、单调时间和来源epoch；行情、信号、原生成交各自保留来源时间。NTP参考仍由后台只读刷新，接收结果由原owner处理。

连接前、开启新增及原生开仓发送前要求Windows与参考时钟偏差不超过1000ms。运行中偏差越界暂停新开；持仓退出继续使用有效参考和原保护。该1000ms为工程时钟门槛，不修改DSV业务参数。程序不在交易循环中调整Windows时钟。

本机只读检查发现`W32Time`为`Stopped / Manual`，`w32tm /query /status /verbose`返回`0x80070426`。目前没有取得当前NTP偏差，也没有实际校时。盘前可在管理员PowerShell显式使用：

```powershell
E:\online-trader\scripts\clock.ps1 -Sync -Output E:\online-trader\output\simnow\clock-before-run.json
```

该入口启动已有Windows Time服务、请求系统重同步，再独立测量偏差；失败返回非零。单独运行`clock.ps1`只查本地服务状态，`-Measure`只测NTP、不调整系统时钟。本轮没有执行`-Sync`或`-Measure`。

已生成[今晚输入包](../output/simnow/20261110-night-inputs/preparation.json)：

- [当前DSV业务参数](../output/simnow/20261110-night-inputs/dsv-profile.json)，hash为`de5e27b563b21bada8431967d06a56c98723b411ad2f2c6eca7b8a186bb6c145`。5个品种的数量设置均为0，保留DSV自动风控手数语义；与昨天打包参考不同。
- [明确盘次日历](../output/simnow/20261110-night-inputs/calendar.json)：11月10日21:00开始，柜台日标签为11月11日；棕榈油、塑料、沥青、菜油到23:00，白银到次日02:30。包含11日日盘，以维持其他品种夜盘结束后同一柜台日的完整参考。
- [系统时间服务现状](../output/simnow/20261110-night-inputs/w32time-preparation.json)。

时段依据包括[上期所交易时间](https://www.shfe.com.cn/services/calenderandholidays/tradinghours/)、[休市安排](https://www.shfe.com.cn/services/calenderandholidays/holiday/)、[郑商所菜油业务细则](https://www.czce.com.cn/cn/uploadfile/2024/02/07/20240207144928864.pdf)及[大商所夜盘时间调整资料](https://www.dce.com.cn/dalianshangpin/resource/cms/2019/04/2019042612023697006.pdf)，并与工程内已核对的DSV盘次规则一致。这里是显式日历参考，不冒充已收到的SimNow柜台登录交易日。

原DSV若在20:55自动计算后更新保存参数，应重新提取业务profile、重新生成绑定范围，再开始本次对照；运行中不会偷偷替换已存在持仓计划。原DSV界面实际加载的配置仍应与保存值一致。

## 启动入口

当前候选可直接由源码运行，无需修改固定SDK环境或安装新wheel。只读准备和报表使用核心解释器；未来连接使用已有SDK解释器，`-I`源码入口明确加载本工程dev21。

本地凭据保存入口，不进行交易登录：

```powershell
E:\online-trader\.venv\Scripts\python.exe -I E:\online-trader\scripts\run_online_trader.py simnow credentials --output E:\online-trader\output\simnow\credential-reference.json
```

`simnow scope --help`提供范围生成参数：无密码引用文件、profile、calendar、独立ledger、合约、柜台日、UTC起止、单笔上限、总开仓尝试数、风险预算及授权引用。额度和身份均需明确填写；没有从当前账户余额猜值，也没有复用已消耗的下午一次性范围。本轮未生成实际账户scope。

`scripts\simnow.ps1`默认只执行`simnow prepare`。只有显式`-Run`才连接，首次新增另需`-Arm`；重启仍只管理原退出。参数为`-Scope -Profile -Calendar -Output`，另可指定`-Credentials -DsvConfig -DsvLogDir`。对照应传入原DSV的`config.json`和`logs`目录。未来在线owner管道名为`OnlineTrader.SimNow.<账户指纹>`，原工作台可用`workbench --pipe`附着。

运行目录自动生成`comparison-<id>\manifest.json`和`events.jsonl`。记录可边运行边读，输出目录需是新的目录：

```powershell
E:\online-trader\.venv\Scripts\python.exe -I E:\online-trader\scripts\run_online_trader.py compare report --manifest $manifestPath --output $newReportDirectory
```

可另传`--dsv-trades`和`--dsv-source-account`加入原生成交总量对照。CSV格式沿用[已有严格成交文件合同](trade-reconciliation-contract.md)，要求明确账户、模拟环境、柜台日、成交ID、合约、买卖、开平、数量及价格。此处跨账户比较不登记费用或导入自有成交。

## 结果如何看

`comparison.csv`的`MATCHED`只表示唯一时间候选。看`quantity_equal`、`theoretical_price_equal`、`time_difference_ms`及`price_difference`评估差异；不会把对上时间等同策略结果正确。DSV重复前缀时间中采用最后一个时间，并保留全部前缀；原日志只有秒精度，默认候选窗口2000ms且可调整。同一日志文件跨午夜时按已读时间顺序推断自然日递增并标注推断依据，不把文件日期当柜台日；其他时间倒退标记歧义，不自动配对。

DSV的`[平仓]`文本是触发记录。确认开平的总量/均价使用原生成交CSV补充。对比器保留歧义、缺失及原始事实，不能消除两个模拟账户的成交差异，也不会将不同资金/初始持仓导致的手数差异归为时钟故障。

## 本轮必要验证与当前限制

定向验证见[最终结果](../output/development/dev21-simnow-comparison-final3/result.json)及[检查日志](../output/development/dev21-simnow-comparison-final3/checks.log)。覆盖SimNow专属连接参数、身份隔离、原Ack后发送、原生成交开平与独立只读核对、时钟越界仍保留退出、双侧记录/CSV、跨午夜日志和受影响的原撤单/UNKNOWN/夜盘路径。报表只统计原账本已确认的成交，并按成交来源日分组；单独收到回报不提前算成交。未全量回归、构建wheel、安装更新或执行在线账户测试。

当前已备好接入和对照的开发入口；尚未验证真实SimNow登录、行情/交易回报及夜盘首轮结果。SimNow账户管理GUI和非关键统一诊断界面本轮省略，使用独立命令入口。原DSV程序、配置与数据、冻结V7、原SDK环境及历史在线证据均未修改。
