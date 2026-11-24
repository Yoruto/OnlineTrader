# ADR：Standalone Runtime 业务连续性状态跨重启边界（草案）

2026-11-24 · `NEEDS_REVIEW / DESIGN_ONLY`

> 关联基线：[柜台权威、本机单实例与临时执行库](runtime-broker-authority-single-instance-20261120.md)、[DSV 原版与 OnlineTrader Standalone Runtime 做法差异及参考建议](dsv-vs-standalone-runtime-implementation-comparison-20261124.md) 第 9.5 节。

## 1. 背景与约束

Standalone Trading Runtime 目前严格遵循：
- 每次启动新建进程内临时执行库 `Journal`，退出后删除，不跨 boot 交接订单发送责任；
- 期货账户为持仓、委托、成交、资金与收益权威，新 boot 同步后默认 `DISARMED`；
- 本地仅持久化版本默认值、用户配置覆盖与可从柜台重建的 `account-history.db`。

这保证了订单与资金安全，但以下**策略业务状态**在同日盘中重启后尚缺定稿合同：
1. 当日停开（`day_stop`、`day_stop_reason`、`net_loss`）与月度停止（`month_stop`、`stop_days`）；
2. 已消费分钟去重（`last_signal_bar`）与每日开仓次数（`target_count`）；
3. 带仓重启时的保护阶段（`half_stage`）、浮盈峰值（`peak`）、保本状态及理论入场基线（`theory`）；
4. 故障/关机前用户已主动发出的 `STOP` / `PAUSE` 意图。

## 2. 待用户拍板的决策项（`NEEDS_REVIEW`）

| 状态项 | 候选方案 A（保守安全优先） | 候选方案 B（柜台可证重建） |
| --- | --- | --- |
| 日停 / 月停 / 当日开仓次数 | 重建缺口未核清前维持 `DISARMED`，需人工确认 | 从 `account-history.db` + 柜台当日成交/平仓盈亏按配置公式重建 |
| 带仓重启保护阶段与峰值 | 识别到存在仓位时保持 `DISARMED` 并提示人工接管或设置保护基线 | 从当日委托/成交反推均价并重置阶段，不能证明的历史峰值不虚构 |
| 同分钟信号去重 | 同步完成后首个自然分钟边界前限制新开，或由人工决定何时 `ARM` | 从柜台当日开仓成交时间戳推导最近开仓分钟并抑制同分钟重复开仓 |

## 3. 红线（无论选择哪种方案都不得突破）

1. **禁止恢复旧执行 `ledger.db` 作为订单发送或 `UNKNOWN` 恢复权威**。
2. **禁止静默清零日停或保护状态后自动 `ARM`**。
3. 本 ADR 定稿前，新 boot 保持同步后 `DISARMED`、需人工确认后再启用自动开仓。
