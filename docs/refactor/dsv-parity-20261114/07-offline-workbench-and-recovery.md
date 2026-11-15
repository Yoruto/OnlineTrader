# 离线工作台、重启恢复和今昨仓

本阶段接入 T22、T24 和 T25 的离线范围。独立 Windows 命名管道采用现有同用户 ACL、进程身份检查和帧长度限制。离线工作台只读投影和发命令，独立执行器拥有账户锁与唯一 SQLite writer。关工作台后，同一执行器仍可接收注入事实并维护已接受目标。

## 使用入口

以产品 `.venv` 在仓库根目录运行。两条命令分别在两个终端运行。会话头格式与前一阶段回放相同，`account_key` 以 `DEV:` 开头，`provider` 为 `OFFLINE`，`trading_day` 为账本起始柜台日，`profile` 为规范化的 DSV 业务参数。重启执行器使用同一会话头及离线事实库。

```powershell
.\.venv\Scripts\python.exe -m online_trader offline-dsv serve --input .\session.json --db .\facts.db --pipe OnlineTrader.OfflineDSV
.\.venv\Scripts\python.exe -m online_trader workbench --offline-pipe OnlineTrader.OfflineDSV
```

F5/F6 登记启动和停止意图。F6 只停止新增，仓位及原保护继续由离线执行器管理；是否撤活动开仓单取自配置。选中自有持仓后输入手数可登记手工减仓，选中委托可撤单。减仓命令与原有保护共用剩余目标。手工撤单在终态明细齐备前保留占用；同一目标保持撤销，后续手工减仓或更紧的保护目标可继续处理剩余量。工作台显示上一条命令回执，以账户、配置和操作时的柜台日绑定新命令。响应丢失时查原 ID，只有查到无回执后才可明确重试同一内容。

本地注入命令使用 `python -m online_trader offline-dsv request --pipe OnlineTrader.OfflineDSV --input request.json`。文件是 `{"op":"snapshot","body":{}}`、`{"op":"receipt","body":{"id":"..."}}`、`{"op":"disconnect","body":{}}`、`{"op":"command","body":{...}}` 或 `{"op":"round","body":{...}}`。`round` 复用上阶段回放格式，去掉外层 `type`。`snapshot` 可传 `detail:true` 查看包含已终态子单和成交依据的完整投影。记录端口的发送与撤单都只落入离线库，原生回报需显式注入。

「策略事件」显示本地拒发原因与错误码、发送结果未知、到期阻断的剩余天数和阈值，以及保护线理论价和名义点数。「逐笔成交」显示已记录的成交价和数量，结清后仍可查。旧账本缺少细分拒发理由时显示「发送前校验失败」，仍保留错误码。[初版 Qt 独立验收](../../../output/validation/20261115-offline-evidence-independent/test-report.md)

A02–A03 补验版本中，历史在后台线程用独立只读 SQLite 连接分批读取与解码。执行者仅返回已完成的有界快照，界面在首查期间显示读取中，遇到暂时读锁等故障可在后续刷新重试。策略事件展示各品种的决定变化，包括追价超限、资金不足、行情过期、等待同步、止损及正常入场。新增报撤失败记录带 UTC 时间和错误码，撤单异常显示已脱敏的异常类型；较早的发送记录缺少时间时仍显示 `—`。

## 同步与换日

执行器启动或收到断线后，清理旧报价缓存并等待同步。收原单事实和记录启停意图仍可进行。恢复时传 `synchronized:true`，每个当前未核齐子单需在该轮有 `ORDER` 事实且列入 `sync_orders`，已由本轮逐笔事实证明核齐的子单也可对账通过。还需匹配现有自有仓。缺证据时保持待核对，可以随后补报。恢复不会重放准备动作或撤销先前的 F6。底层单机回放可以继续使用原 `DsvSession` API；要模拟断线须显式调用 `disconnect()`，工作台执行器每次启动自动执行此步骤。

跨日要求 `round.trading_day` 明确前进且 `synchronized:true`，全局序列连续。上一柜台日的未结子单继续占量，相关品种禁止新发送，直到原单终态与逐笔已成交核清。原生事实中的 `trading_day` 始终标原子单柜台日；跨日迟到成交额外标 `fill_day`。跨日有仓时提供四个柜台仓位桶 `long_today`、`long_historical`、`short_today`、`short_historical`，与各方向总量及回放自有成交一致。SHFE/INE 先派发 `CLOSETODAY`，该子单核齐后再按剩余目标派发历史仓 `CLOSE`；其他交易所用 `CLOSE`，自有桶记账先扣今天。合并交易所可将同开平标志的已证数量合为一个子单。双向仓、外部仓、外部活动单或桶不符时不借仓派发。

原生成交用归属账户、提供者、原订单柜台日、原订单 ID 和原成交 ID 去重，保留原始 ID。新事实库为 schema 2。前阶段同类离线库的 schema 1 在同一事务内将原生事实归入其已绑定柜台日，保留命令、目标与待核子单；现有生产账本的格式会被拒绝。已报终态但后续成交仍落在可证明的原意图及自有量范围时，保留成交并重新核对终态与占量。若另一个策略周期已持仓，迟到开仓成交继续归到原周期并显示归属待核对，避免以当前周期仓位抵消。跨周期分配工具、已结案平仓的冲突修复、真实 SDK 日历和生产迁移仍需后续实现或验证。

## 验收证据

独立 Agent 的 8 项 Windows Qt 用例、17 项命名管道与恢复用例、30 项前阶段回放回归，以及 1534 项标准源码检查全部通过。覆盖启停、关窗再附着、响应丢失与同 ID 重试、原终态修正、旧离线库升级、跨日同原生 ID、今昨桶错配及外部仓拒绝。 [覆盖清单](../../../output/dsv-parity/offline-workbench-acceptance/coverage-checklist.md) · [独立报告](../../../output/dsv-parity/offline-workbench-acceptance/test-report.md) · [标准检查](../../../output/checks/20261114T114835Z-e4a086c5/result.json)
