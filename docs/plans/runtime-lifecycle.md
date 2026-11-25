# 计划：Runtime 生命周期解耦与维护停机

> **状态**：`PLAN / DESIGN_ONLY`（尚未实施；合并自原架构 V1.1、实施规格 V1.0 与业务连续性 ADR，待实施完成并合入正式文档后删除本文件）。

---

## 1. 目标

在不拆出多进程或第二套状态机的前提下，允许用户在**监控状态（无活跃交易责任）**下直接在 Workbench 内切换期货账户或执行软件更新，而无需手动关闭整个 Runtime 进程。

---

## 2. 核心设计约束（不可违背）

1. **自动冷切换为长期默认**：切换账户时保持 Workbench 打开，后台安全退出旧 Runtime 后再拉起新账户 Runtime，不实现同进程 SDK 热切换。
2. **单一衍生模式，不并行维护第二套状态机**：
   - 保留 `strategy_intent`（`STOPPED` / `RUNNING`）、唯一 `maintenance` 请求与既有 `authority_generation`。
   - 由 Owner 单线程采集的统一只读责任摘要 `ResponsibilitySummary`（`NONE` / `ACTIVE` / `DRAINING` / `UNKNOWN`）派生运行模式：`TRADING` / `DRAINING` / `MONITORING`。
   - `NONE` 必须已证明无本机执行责任（无非终态 child、无 `UNKNOWN`、无已激活保护仓位、无未终态收敛撤单），严禁将“暂无数据”或 `DISARMED` 下的 Plan B 预览仓位误判为真实保护责任。
3. **不违背现行安全合同**：
   - `MONITORING` 模式下严禁因维护需要触发任何自动平仓或撤单；
   - `STOP` 后不再生成新开仓或全账户收敛任务，但已提交子单、非策略撤单与 `UNKNOWN` 仍须核对至终态；
   - 纯行情过期但无实际责任时，不应永久阻断维护或更新。
4. **不新增过度抽象**：不引入 ORM、消息中间件、独立 `MaintenanceManager` / `CapabilityManager` 双系统或跨设备同步。

---

## 3. 待实施批次

| 批次 | 内容 | 说明 |
| --- | --- | --- |
| **PR1** | 运行模式与维护门禁 | `ResponsibilitySummary` 纯函数派生 `mode` 与 `maintenance_blockers`，通过 `workbench/1` 发布给 `ClientStateStore` |
| **PR2A** | 无平仓安全停机 | 在 `shutdown.py` 新增无平仓维护停机入口（复用等待 helper，绝不触发 `FLATTEN_THEN_STOP`） |
| **PR2B / PR2C** | 账户冷切换与更新协调 | 共享同一停机门禁：等待旧 Owner 安全退出后切换目标 `account_key` 或交由稳定启动器更新 |
| **PR3 / PR4** | Configuration 与 Persistence 收敛 | `PortableAccountStore` 职责分离、`BrokerHistoryWriter` 初始化故障隔离；若需跨 boot 保留 `day_stop`/`half_stage`/`peak` 等不可重建状态，须另行审批后实施 |
| **PR5 / PR6** | 内部整理与独立验收 | 清理历史兼容入口并完成离线/实包验收 |
