# Runtime 执行路径与 Ledger Lineage 事实矩阵

> 状态：PR-A 源码事实基线（2026-11-20）。本文只记录当前入口、Owner、锁、writer 与恢复归属；不授权迁移账本、切换 Owner、连接账户、测试或打包。

## 结论

当前代码不是“一套入口、两个可互换存储实现”。原执行路径和 Replacement Candidate 是两套有共同账号锁、但 Owner、事务模型和恢复合同不同的执行谱系：

| 路径 | 实际入口 | SDK / 执行 Owner | 账号互斥 | 持久化与 schema | 唯一 writer | handoff / 启停 | 恢复归属 | 边界 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Legacy | `simulation run`；`dsv run`；`simnow run`；旧 Workbench 附着入口 | 原 `Kernel`；DSV/SimNow 在线适配仍由原 simulation owner 驱动 | `AccountLock(account_key)`；若上层已持锁则把同一 lease 传给 Kernel | scope 指定或账户档案解析出的原 `ledger.db`；`Ledger` / `ledger_scope` / `event_journal` / `authority` | `Kernel` 内的原 `Ledger` writer | 原启动恢复合同；显式维护，不作为默认桌面路径 | 原账本、原命令/委托/保护事实；未知发送必须原地核对 | 仅本次明确维护时使用；不可与 Replacement 同账户双发 |
| Standalone Trading Runtime | `online-trader-desktop --runtime-root`；便携版 `OnlineTrader.exe`；`runtime run`；`runtime schedule` | Desktop bootstrap 只发现或启动 Owner；真正执行仍是原 `Kernel` + 原 simulation owner | bootstrap 先取得 `AccountLock`，再把 lease 传入原执行器/Kernel；附着进程不成为 sender | `runtime.json`、`runtime-control.json`、operator grant 是未来意图；已初始化账户继续使用账户档案绑定的原 `ledger.db` | 原 `Kernel` / `Ledger` writer；Qt、runtime-view、operator API 只读/控制 | 缺失时以 `DISARMED` 启动；运行目录和 pipe 只用于发现、附着与诊断，不替代账本 | 原账本负责成交、订单、保护、命令和配置权威恢复；缺失已初始化账本时不得新建替代 | 默认产品路径；桌面退出不转移订单和保护责任 |
| Replacement Candidate | `dsv replacement-run`；`simnow replacement-run` | `DsvSession` + `ExplicitOrderController` + SDK adapter | 同一个兼容 `AccountLock(account_key)`；因此和原 Owner 互斥，而非共享发送权 | 显式 `--db` 的 Replacement `Journal` schema | `DsvSession` 内 `Journal` / `ExplicitOrderController` 路径 | 只按候选启动合同运行；尚无批准的生产切换或热 handoff | 候选 Journal 自己恢复；不得用原账本缺失推断候选可接管 | 候选不是 Standalone Runtime 默认 Owner；不得与原 Owner 双跑 |
| Isolated QA | `offline-dsv`；`qa/dsv_parity/` 回放工具 | 离线 host / replay runner，不是在线 SDK Owner | 不取得在线账户锁所代表的发送权 | 固定样本、显式隔离数据库或输出目录 | QA-only writer | 无生产 handoff | 固定样本/隔离副本重建；不得写回生产账本 | QA 输入注入不能成为在线输入端口，也不能进入生产 wheel |

## Owning source

- CLI 入口和分派：[cli.py](../../src/online_trader/cli.py)
- Standalone Desktop 的锁、Owner 启动和附着：[desktop_bootstrap.py](../../src/online_trader/runtime/desktop_bootstrap.py)
- 原 DSV/SimNow 执行器：[dsv_simulation.py](../../src/online_trader/runtime/dsv_simulation.py)
- 原 Kernel 对账号锁和 Ledger 的所有权：[engine.py](../../src/online_trader/kernel/engine.py)
- 原账本实现：[store.py](../../src/online_trader/ledger/store.py)
- Replacement 启动和账号锁：[sdk_startup.py](../../src/online_trader/dsv_runtime/sdk_startup.py)
- Replacement Owner：[runner.py](../../src/online_trader/dsv_runtime/runner.py)
- Replacement Journal：[journal.py](../../src/online_trader/dsv_storage/journal.py)
- 便携配置与原账本绑定：[portable_config.py](../../src/online_trader/runtime/portable_config.py)

## 不变量与后续门禁

1. `AccountLock` 是“同账户不能同时存在两个执行 Owner”的进程互斥，不是把两个 writer 变成同一事务域。
2. portable configuration 和原执行账本是已初始化账户的两类共同必需状态；前者不能重建后者的成交、未知发送或保护责任。
3. 在 Replacement 获得正式切换决策、冷交接流程、schema 迁移、增量迁移和旧版恢复证据前，不合并 `Ledger` 与 `Journal`。
4. runtime-view、Workbench cache、展示日志和 `runtime-state.json` 都不是交易权威。
5. 任何未来存储归并必须逐项证明 commit-before-send、未知发送容量保留、取消不释放数量、单一剩余退出目标和跨重启保护责任没有变化。

