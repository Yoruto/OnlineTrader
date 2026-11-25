# 配置、账户与持久化

本文定义 OnlineTrader 各类数据的唯一权威来源、写入者、生命周期与重建规则。

---

## 1. 数据权威矩阵

| 数据/状态 | 唯一权威与写入者 | 生命周期 | 重建与隔离规则 |
| --- | --- | --- | --- |
| **持仓、今昨仓桶、活动委托、成交、资金** | 期货账户（柜台）；Runtime 单向同步 | 每次 boot 与重连全量刷新 | 覆盖账户内全部合约；其他电脑可独立使用同一账户，不使用账号级本地锁互斥 |
| **当前执行事实、子单、`UNKNOWN` 占量** | 当前 boot 临时执行库（`Journal`，单写者） | 仅当前 boot 存活，退出即删除 | 严禁跨 boot 交接；历史遗留 `ledger.db` 只读，绝不阻塞启动 |
| **历史成交与账户收益缓存** | `account-history.db`（`BrokerHistoryWriter`） | 本机跨 boot 持久 | 仅用于辅助展示与历史统计，可从柜台重建，**不是下单或持仓权威** |
| **配置默认值与用户覆盖** | `OnlineTrader-defaults.json`、`OnlineTrader-parameters.json`、`OnlineTrader-overrides.json`、`runtime.json` | 随交付与用户编辑持久保留 | 便携目录使用相对路径，不依赖 Windows 凭据库；更新时保留根目录既有配置 |
| **页面草稿与未决命令** | Workbench 进程内存（`CommandBus`） | 仅当前 UI 进程存活 | 不落盘 `QSettings`/注册表，重启后丢弃 |

---

## 2. 废弃的旧持久化与控制文件

以下历史文件在 Standalone Trading Runtime 中**一律忽略**，不得作为新交付或启动依赖：
- `runtime-control.json`
- `private/operators.json`
- 任何 watchdog、自动调度配置或跨机/跨 boot `handoff` 文件

---

## 3. 外部对账单核对与受限费用登记

- **对账单比较**：完全只读，不修改任何账本、成交价量或保护状态。
- **费用登记受限边界**：仅允许在既有的 `ONLINE_SIM` / 开发账本中，且同时满足**执行 Owner 已关闭、所有自有周期已平仓/结案、所有委托均为数量一致的终态事实**时，在单一原始写者事务内追加对账单费用与持久回执。
- 登记操作严禁改写成交价量或保护计划，且保持开仓权限为 `DISARMED`。单独的 CSV 导出或预览不构成登记授权。
