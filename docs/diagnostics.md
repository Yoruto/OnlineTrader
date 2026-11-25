# 诊断日志、错误码与故障排查

OnlineTrader 内置 **Runtime & Workbench Diagnostics** 旁路观测系统，用于故障现场还原与契约核对。

---

## 1. 权威边界与核心原则

- **非权威旁路**：诊断记录写入 `<runtime-root>/diagnostics/`，**不是**交易权威、恢复账本或测试通过证明。磁盘满或诊断写入失败绝不传播阻塞交易主循环。
- **每进程单写入器**：Runtime Owner、runtime-view、Workbench 各自最多一个 `Evidence Sink` 异步有界写入小型不可变记录（事件模型 `ONLINE_TRADER_DIAGNOSTIC_EVENT/1`），统一脱敏并保留 `runtime_boot_id`、`account_pseudonym`、`configuration_revision` 与 `event_seq`。
- **交易链与历史链分离**：分别使用 `execution_journal_event_seq` 与 `history_snapshot_seq` 关联，不混用模糊的提交 ID。

---

## 2. 盘后切片、退出上报与覆盖语义

- **触发时机**：
  1. 当前 boot 内每 10 秒检查一次：当绑定日历中所有 scope 合约均有 `force_close_at_end` 时，取最晚收盘时间为截止点，当前 UTC 时间不早于截止点即触发一次盘后切片（盘后启动且日历仍为当日时补报一次）；
  2. Runtime 正常退出时由短生命周期独立 `Detached Reporter` 打包并尽力上报（若 `private/diagnostics-upload.json` 已启用，优先使用项目级 Deploy Token `write_package_registry`）。
- **诚实的覆盖语义**：Reporter 读取活跃文件时截取已观测完整 JSON 行；若存在任何必需活跃流或缺失预期 Workbench 事件，包状态标为 `PARTIAL` 而非完整。Analyzer 结论严格区分 `OBSERVED_OK`、`VIOLATION`、`DEGRADED`、`INCONCLUSIVE`、`NOT_EXERCISED`、`NOT_APPLICABLE`。

---

## 3. 关键排障链路

1. **行情拒绝与停开**：日志须能还原实际失败的报价年龄、交易时段（`market_phase`）、盘口条件及输入依据；区分界面投影过期（`STALE`）与 Owner 行情滞后（`DSV_QUOTE_AGE_EXCEEDED`）。
2. **订单全链路**：事件须能区分 UI 触发、发送前中止、IPC 入队/拒绝、Owner 接收、SDK 调用前、SDK 结果未知（`SEND_POSSIBLE`/`UNKNOWN`）、柜台回报、成交与保护推进。
3. **源码验证启动日志**：使用桌面开发启动器时，启动器自身诊断写入 `%LOCALAPPDATA%\OnlineTrader\dev-launcher`，Runtime 诊断仍写入所选 `<runtime-root>/diagnostics/`。
