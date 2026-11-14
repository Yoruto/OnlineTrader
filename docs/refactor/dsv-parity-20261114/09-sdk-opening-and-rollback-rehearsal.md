# SDK 适配、首分钟规则与成交后回滚演练

## SDK 适配

`DsvSdkAdapter` 接受已经构造、已绑定账户的天勤 API 对象，当前由新 `DsvSession` 的离线入口用替身驱动。适配器本身没有创建 API、登录、存储凭据或附着旧执行内核的入口。产品验证依赖锁定 3.10.4，冻结原版包为 3.10.2。两版可见接口的静态对照使用公开 `wait_update`、行情、分钟 K 线、资金、持仓、委托、逐笔和发撤单接口。 [SDK API](https://doc.shinnytech.com/tqsdk/latest/reference/tqsdk.api.html) · [业务对象](https://doc.shinnytech.com/tqsdk/latest/reference/tqsdk.objs.html)

- `step(session, trading_day=..., complete=...)` 单轮先调用 SDK 的 `wait_update`，读取最终可见快照。返回 false 的无新增轮仍处理等待与保护。原生 quote 文本身份和 60 秒 K 线末行独立记录。盘间旧报价不进入新盘决策，午休端点不套尾盘禁开。
- `complete` 由调用者提供明确完整性事实，或提供本轮更新后读取完整状态的回调。重启、断线、缺原单、原生时间或归属冲突会等待或拒绝。日期结合明确柜台日和交易日历映射，今昨四桶与总量互证。
- `command(session, command)` 用同一所有者线程接收启停、减仓或撤单。出站前再次核对事实库原请求，SDK 用绑定的账户和确定性子单 ID，重启以相同 ID 核对迟到逐笔。SDK 调用异常保留未知，既有事实可按原身份收敛。关闭仍由拥有 SDK 生命周期的调用者负责。
- 替身复跑可以覆盖公开接口形状、请求顺序、落盘及恢复。真实行情和柜台的字段完整性、通知、时间及授权入口仍须经后续现场验收。

## 首分钟与滞回

冻结 `gui` 的 `_run` 在 8070 行写局部 `_secs_open`，8080 行读全局 `_secs_o`，冻结代码中未见写入；所在异常分支跳过首分钟/滞回并继续普通入场。默认逐轮执行和 SDK 适配保留此路径。 [冻结版证据](../../strategy-dsv-evidence.md#首分钟滞回调用顺序与静态缺陷)

离线帧可选填 `market.wall_reads.opening_observation`，用明确 UTC 秒数执行该块的源码规则。这条工程对照路径使用当前交易日和盘段起点，先走普通开盘等待、分钟消费和跑过激活位判断，再检查双向跳空及共享滞回。范围含 0、60 秒，贴线为 1.5 tick，偏移零回落止损偏移，探测方向按双线中点选取，等于中点走空头探测。未达滞回消费本分钟，已武装状态在原盘段换线后继续有效。跳空置日停后按原字节码继续评估同轮目标，下一轮才受该日停约束。显式路径的决定和原版默认结果可各用一份预期轨迹比较。

## 已成交后的安全回滚演练

```powershell
python -m online_trader offline-dsv rollback-rehearse --source legacy.sqlite --db candidate.sqlite --input trading-calendar.json
python -m online_trader offline-dsv rollback-rehearse --source legacy.sqlite --db candidate.sqlite --input trading-calendar.json --target rehearsal-directory
```

使用本阶段迁移器写出的绑定收据、完整 `TRADING_CALENDAR/1` 及两个停止的隔离账户。演练走文档允许的原策略合法日切路径。两个一致读快照在各自账户锁内复查。要求新版成交、仓位和子单全部核齐，原周期分配与逐笔一致，下一已证柜台日首盘内有完整空仓快照，且该日还未发送、成交或消费新业务。已经核过的旧日重复回报不算新增业务。比对跨日保留的冷却、需更新触发的原方向/锚点、同月停止及止损日历史。跨日状态差异、过了首盘窗口或旧库发生变化时，工具给出阻断原因。

显式指定目标时，演练写入两个新的 SQLite 一致副本及包含摘要、水位、配置绑定和交易日的判定收据。保持旧库未覆盖、两边均未启动，候选日内成交留档。该路径以确认日切为前提。同日回滚、跨日业务状态无法等价时，需要验证过的反向迁移方案。真实账户重新核对、旧控制者启动及生产交接依照 M0–M9 的现场流程。
