# 13:30开盘准备：主程序受限在线模拟

目标是将2026-11-10下午的一次模拟测试接入OnlineTrader原内核，验证提交、发送、原生回报、保护退出、原账本和资金报表。上午的独立QA探针已经结案；本次使用新的显式范围和产品账本，不复用上午额度。

## 接入与边界

- `simulation prepare`只读登录快期TqKq，完整同步资金、持仓、委托、成交并保持禁开；`simulation run`通过独立`KQ_MANUAL_SIM/1`文件绑定账户指纹、绝对账本路径、合约、交易日、开仓窗口、1手/1次/60秒。
- 默认`serve`仍无交易通道。只有`gateway/tqkq.py:connect`显式延迟导入可选`tqsdk==3.10.4`；核心环境不安装SDK。禁止TqSim本地撮合或实盘账户替代。
- `Kernel.admit_entry`沿原风控与入口上下文建立G0 1.0 `ONLINE_SIMULATION`计划；原writer提交子单、接收对应ack之后才调用SDK。未知发送占量且禁开，不重放。回报入口要求在线适配器持有的身份令牌。
- 固定止损/止盈采用已确认DSV保存的距离；候选为棕榈油`DCE.p2701`（16/50点）和塑料`DCE.l2701`（20/72点）。只有当前无持仓、无活动委托且连续两条不同新鲜报价的合约可开。买入开仓1手，不自动追开、不重开。
- 首次原生成交与原FILL事务同时保存成交纳秒时间和60秒截止；同进程使用单调时钟，重启使用原截止参考时间，晚发现成交不重新延时。止损/止盈可提前退出。到期经原`manual_close`及统一remaining target卖出平仓。撤单15秒后发起，必须终态、累计成交及明细一致才可释放；退出最多3个子单。
- 断线持久化暂停，完整同步并核对订单、成交和真实持仓后仅恢复原退出；暂停开仓保留已有保护，控制通道中断时执行owner继续处理原退出。原DSV与其他合约订单/成交不归入本次持仓。
- 资金快照、成交、闭合状态及最终重新登录均进入原writer；资金缺项保留未知。原生委托/成交白名单证据可与原账本连接复核，结果文件不能替代真实回报。

## 启动与停止

使用安装dev15 wheel的独立SDK环境执行。dev14额外绑定启动时的权限版本，控制协议的暂停或其他权限变更会终止尚未开仓的测试，不能被等待开盘逻辑重新武装：

```powershell
python -I -m online_trader simulation prepare --scope <绝对scope.json> --credentials <用户原凭据文件> --output <独立预检目录>
python -I -m online_trader simulation run --scope <同一scope.json> --credentials <用户原凭据文件> --output <独立运行目录> --pipe <独立管道名>
```

运行可以提前等待显式窗口；13:30前不创建开仓意图。窗口结束仍无适用条件就以无开仓结束。开仓前可在运行输出目录创建`stop-before-open`，或通过原控制端口暂停。额度按已提交计划消耗；无成交、拒单或保护提前退出均不重新开仓。

完成后先关闭发送连接，再用新的只读登录核对本次委托全部终态、明细成交数量一致、所选合约持仓为零，输出`final-relogin.json`及`final-money-report.json`。凭据仅在进程内读取；输出保留指纹和白名单字段。

dev15在关闭控制端前最多等待1秒，完成已接收命令的提交、回执入队与原生管道发送。等待包含正在发送的字节；不可把队列取出视为客户端已收到。慢客户端不会无限阻塞退出。

## 12:42准备核对

- 最终源码：[1035项完整离线检查](../output/checks/20261110T043850Z-d664a0be/result.json)，0失败、0错误、0跳过，编译/边界/源码稳定通过。此结果不冒充在线成交。
- 最终制品：[dev15 wheel审计](../output/artifacts/20261110T043841Z-930a454873fe/wheel-audit.json)，78份Python、6份原始资源逐字节一致，独立无SDK环境安装检查通过。wheel SHA-256 `824788003de2819b779c1398ec4c4f0292c989a713fd230d35ad63dff1668fa2`。
- [已安装版本与84份文件身份](../output/online/afternoon-20261110T042355Z-76c58dabff84/installed-identity-dev15.json)；SDK仅在独立simulation环境，版本3.10.4。
- [已安装wheel的Windows管道演练](../output/online/afternoon-20261110T042355Z-76c58dabff84/installed-ipc-rehearsal-dev15/result.json)：跨进程查询、暂停、持久回执、禁开保持通过；使用外部离线测试事实，未联网、未下单。早期演练暴露并修复的等待开盘覆盖暂停及关闭前回执未发送问题保留原失败目录，不改写旧结果。
- [dev15只读预检](../output/online/afternoon-20261110T042355Z-76c58dabff84/preparation-dev15/session-start.json)完整同步成功，保持DISARMED。
- [等待状态核对](../output/online/afternoon-20261110T042355Z-76c58dabff84/waiting-audit.json)：12:42已启动主程序并可从真实控制管道读取状态，原产品账本委托0、成交0、权限DISARMED。窗口为13:30–13:35，尚未证明实际开平。

预检账户已有其他软件的`SHFE.bu2701`空仓16手；该持仓不归本次管理。界面账户资金反映整个账户，本次收益仅由本次归属成交计算，不拿账户余额变化替代本次净盈亏。

## 13:30实际测试结案

**本轮受限人工开平通过。** 实际执行为已安装的dev15 wheel，使用TqKq在线模拟、原Kernel/OMS/writer/保护和资金报表；没有另行调用上午QA下单探针。

| 动作 | 合约 | 数量 | 成交价 | 服务端北京时间 |
| --- | --- | --- | --- | --- |
| 买入开仓 | DCE.l2701（塑料2701） | 1手 | 8339 | 13:30:02.430989555 |
| 卖出平仓 | DCE.l2701 | 1手 | 8341 | 13:31:03.815943733 |

原生成交间隔`61.384954178`秒。退出原因为到期调用原`manual_close`形成统一目标，原原因码为`MANUAL_EXIT`；未触发提前止损/止盈。共1个开仓子单和1个平仓子单，均终态，没有撤单、重报价或额外开仓。真实合约乘数为5，成交毛盈亏`(8341−8339)×5=10`元；两笔手续费仍未知，因此净盈亏和含费总盈亏保持未知，没有把`confirmed_fees=0`解释为免费交易。

发送连接关闭后新建只读登录，重新完整同步：本次塑料持仓为0、本次活动委托为0，订单和成交明细数量一致。账户另有非本次测试的菜油`CZCE.OI701`多仓13手和沥青`SHFE.bu2701`空仓16手；本次没有操作它们。原有委托/成交逐字段未变，同时账户出现1条其他客户端的新委托；因此不以整个账户的持仓或余额变化替代本次结案。

- [结案审计](../output/online/afternoon-20261110T042355Z-76c58dabff84/closure-audit.json)：原账本2条成交/2条归属分配、757条连续事件的payload哈希、SQLite完整性、原FILL事务中的截止、终态和原生新登录核对通过。
- [主程序运行结果](../output/online/afternoon-20261110T042355Z-76c58dabff84/run/result.json)、[独立重新登录](../output/online/afternoon-20261110T042355Z-76c58dabff84/run/final-relogin.json)、[最终资金报表](../output/online/afternoon-20261110T042355Z-76c58dabff84/run/final-money-report.json)。
- [闭合原账本报表复核](../output/online/afternoon-20261110T042355Z-76c58dabff84/closed-report-audit.json)：无SDK连接地重建同一757事件前缀，CLI报表与执行owner汇总完全一致，[CSV](../output/online/afternoon-20261110T042355Z-76c58dabff84/run/closed-ledger-trades.csv)仅2条本次归属成交，读取/导出没有改变原账本。
- 结案账本SHA-256 `f177568c515f279da4a9109ba62d2cee5669a340ebeadff240cadeba06427b35`；新登录结果SHA-256 `6f56a4182f01dcbf1be345011f7acb01706fbbce6924c458c0a954681e77b382`。

原开仓额度已消耗，权限最终为DISARMED。执行器及重新登录连接均已退出；原DSV进程28620仍运行。没有留下重复开仓、后台服务或新的定时任务。

平仓阶段尝试读取一次实时持仓状态时，控制请求返回`E_TIMEOUT`，未取得持仓中的IPC快照；该请求未发送交易指令，也未用于证明成交。本轮真实控制证据限于开盘前状态读取，暂停/回执关闭流程另由已安装wheel的离线跨进程演练验证；不将其算作完整在线界面验收。

## 尚未验收

本阶段是人工一次模拟开平，不代表DSV自动策略自然入场、半仓/二阶段在线行为、CTP柜台、全G2或长期稳定性通过。本轮真实开平、离线检查、制品身份和界面验证限制分别记录，不将预检或发送返回标记为成交。

官方接口依据：[TqApi](https://doc.shinnytech.com/tqsdk/latest/reference/tqsdk.api.html)、[TqKq](https://doc.shinnytech.com/tqsdk/latest/reference/tqsdk.tqkq.html)。

## 后续 dev16 离线修复

dev16已复现同步参考取时阻塞owner导致状态查询超时的机制，并修复后台采样、owner接收及关闭阶段的只读请求处理；G2原生身份与迟到/UNKNOWN恢复也已补离线回归。[本轮实现、1065项检查、已安装新wheel的实际跨进程结果](status-recovery-g2-20261110.md)与以上dev15在线结案分别保留。没有再次连接账户或复用额度，历史在线超时的唯一原因仍未证实，在线持仓界面验收仍未完成。

后续dev17已新增独立DSV模拟范围、1.3计划及原生事实→自动策略/风控→原OMS→高级保护链，完成[1121项最终离线检查与新制品演练](dsv-online-strategy-20261110.md)。旧人工scope仍不能启用高级策略；dev17未联网，本文dev15成交记录不作为dev17真实策略验收证据。
