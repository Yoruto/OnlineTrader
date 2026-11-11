# DSV与OnlineTrader同参数应用完成

本页记录 11 月 10 日夜盘应用时的状态。该夜盘包已结束。11 月 11 日上午回读时，DSV 定时启动已开启，五组偏移保持一致。快期只读试跑使用单独的日盘范围。[后续验证](kq-readonly-runtime-20261111.md)

2026-11-10，23:11保存，随后完成两边界面回读。版本仍为`0.1.0.dev22`，生产计算代码没有修改。

已按用户明确指令，将22:28真实日线/振幅、且经原DSV函数复算一致的结果应用到原DSV配置与新软件今晚启动包。当前完整业务参数hash统一为`fe42e982af218b60f2e46a92a5d83317c5ba857fb1b990f89b0b590582b708c9`。配置、启动解释值、原计算缓存恢复值完全相同，实际打开的两边参数界面也显示以下数值。

| 品种 | 合约 | 开仓偏移 | 止损偏移 | 止盈偏移 |
| --- | --- | --- | --- | --- |
| 棕榈油 | DCE.p2701 | 32 | 16 | 73 |
| 塑料 | DCE.l2701 | 55 | 20 | 106 |
| 沥青 | SHFE.bu2701 | 34 | 12 | 73 |
| 菜油 | CZCE.OI701 | 23 | 15 | 52 |
| 白银 | SHFE.ag2702 | 96 | 15 | 139 |

## 实际应用与回读

- DSV：更新`C:\Users\xyc\Desktop\trading\config.json`的11个偏移字段，原始字节备份留在同目录`config.json.before-OnlineTrader-20261110T151142Z.bak`，未复制到产品证据目录。用JSON叶节点定位替换，未改账户、密码、通知、UI和其他无关内容。未覆盖既有`config.json.bak`。
- 发现原DSV的`trade.auto_start_on_launch`和`schedule.enabled`均为true。为遵守“暂时不要下单”，在两边同时将这两个开关置false；其余策略、风控、自动手数、时段等设置完整保留。当前hash与上一轮计算hash不同只因为这两个启动开关，不是偏移重算变化。
- 打开原DSV程序，实际品种列表已显示五组新偏移，自动启动/定时启动未勾选，底部显示“未运行”，停止策略按钮禁用，行情/交易状态均显示未连接。没有点击启动、测试连接、报单或撤单按钮。
- 新软件：建立[已应用今晚包](../output/simnow/20261110-night-ready/applied-comparison-20261110T151142Z/application-result.json)，正式`load_inputs/load_preparation`读取的生效profile与DSV完整相等，运行比较绑定检查接受。实际打开产品参数中心并导入同包`business-settings.json`，界面五组偏移及两项关闭开关均核对一致。
- 新软件参数中心自身是预览界面，不能把“导入预览”单独算作运行参数应用。本次应用依据是**已保存且通过正式启动读取的scope/profile/cache**，界面用于额外回读。双方策略均未启动，因此没有声称已验证运行中策略线程内存或双侧真实成交。

新的自动来源范围绑定已应用profile及同一份日线、振幅；新计算收据保存这些启动控制变更后的完整来源。原始取数/复算收据及旧准备包均保留。范围的账户、柜台日、额度、风险上限、窗口和日历没有扩大，未创建交易账本或执行owner，权限保持`DISARMED`。

## 今晚对比准备

使用[当前统一准备入口](../output/simnow/20261110-night-ready/Start-Comparison.ps1)。默认只核对参数与缓存，不连接账户、不启动策略；本轮实际执行了这个默认入口，结果为`ORIGINAL_CALCULATION_SAVED`，来源/生效profile均为上述同一hash。

[当前包指针](../output/simnow/20261110-night-ready/current-comparison-packet.json)指向新包，不应再使用旧的`prepared-auto-dev22-final`入口；其来源参数尚为旧值，历史包未被改写。`Refresh-Preparation.ps1`也是更早的固定参数准备工具，不是本次已固定真实计算的入口。

当前包已挂接原DSV日志目录，并保存[比较基线](../output/simnow/20261110-night-ready/applied-comparison-20261110T151142Z/comparison-baseline.json)：已应用业务hash、原/新计算hash、原日志摘要与长度、合约及会话状态。真正比较采集的起止时刻仍由未来明确启动的运行manifest决定；本次没有提前生成交易比较事件或将旧日志计作今晚新交易。

23:11时钟独立复测约**66.027毫秒**，1000毫秒门禁通过，没有再次调整操作系统时钟。双方在同一台电脑，未来启动仍会重新检查参考时钟。

按原准备日历，23:00以后棕榈油、塑料、沥青、菜油已结束今夜夜盘，白银仍到次日02:30。本次没有延长时段、倒放开盘或绕过入场门禁。今晚剩余时段不能作为五品种同时运行的完整对比；当前范围也不自动延续到之后的日盘或下一夜盘。

## 证据与保留

[最终一致性审计](../output/simnow/20261110-night-ready/applied-comparison-20261110T151142Z/completion-audit.json)确认完整参数相同、实际界面数值相同、其他原配置保留、原始日线/振幅不变、额度和窗口不变、无今晚交易账本，dev22源码与历史在线账本保留。[界面观察记录](../output/simnow/20261110-night-ready/applied-comparison-20261110T151142Z/ui-observation.json)区分DSV实际表格观察、新软件预览表与正式启动绑定；[新软件界面文本](../output/simnow/20261110-night-ready/applied-comparison-20261110T151142Z/product-parameter-ui.txt)仅含业务参数。

[应用后原DSV函数复算](../output/simnow/20261110-night-ready/applied-comparison-20261110T151142Z/original-function-recheck/result.json)再次确认全部结果一致。SimNow SDK/V7环境未变，没有安装服务/任务、没有发布或运行全量测试。当前仅保留DSV未运行的主界面与新软件参数窗口，没有执行报撤单或启动交易采集。
