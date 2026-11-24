# Standalone Runtime 行情诊断、Owner 减负与快照连续性改进方案

2026-11-24 · 人工监督桌面自动交易终端基线（修订后设计方案 / 未实施）

> 适用路径：Standalone Trading Runtime（`StandaloneDsvOwner`、`DsvSdkAdapter`、`DsvSession`、`ControlServer`、`WorkbenchPublication`、`ViewProcess`、`WorkbenchWindow`、`CommandBus`、`ClientStateStore`）  
> 基线提交：`12b4e6e`  
> 对照依据：[DSV 原版与 OnlineTrader Standalone Runtime 做法差异及参考建议](dsv-vs-standalone-runtime-implementation-comparison-20261124.md)  
> 状态：`CANDIDATE / DESIGN_ONLY`（本文档仅作设计评审与排期依据，不授予账号连接、下单、测试或打包权限）

---

## 1. 目标定位与不可妥协原则

本方案面向**交易时段内人工监督下的高可靠桌面自动交易终端**，不追求 7×24 小时无人值守服务器集群架构。核心目标是在保留现有单一交易权威与 CS 隔离架构的前提下，解决以下三类已确认或待定位问题：

1. **可见性与取证缺失**：Target Owner 尚未接入行情延迟与主循环分阶段耗时诊断，发生全合约滞后时无法区分“行情源未推送 / SDK 重连丢订阅 / Owner 主循环阻塞 / 本地时钟偏差 / UI 快照过期”。
2. **Owner 主线程负载过重**：`ControlServer` 主循环、`DsvSdkAdapter.step()`、`DsvSession._commit()` 与 `prepare_workbench_publication()` 同步耦合，界面投影准备与高频率全量持久化可能反过来拖慢行情读取和下单发送。
3. **过期快照安全门禁过宽与状态误分类**：Standalone 工作台在快照过期时连降风险命令一起锁死；非行情原因在界面上容易被统一展示为“行情滞后”。

### 1.1 明确取消 / 暂缓的范围（避免过度重构）

- **不拆交易权威**：不将 `TqApi.wait_update()` 移至后台线程与策略线程并发读写 SDK 对象；不引入第二个执行器或跨进程自动接管；不恢复 DSV 式“手工线程直调 SDK”第二发送旁路。
- **不新建独立 `market_health` 大子系统**：先在 `dsv_runtime/` 内聚轻量健康判定模块，复用现有 `scheduling/clock.py` 与 `runtime/market_monitor.py`。
- **暂缓三流 IPC 协议重写**：优先优化现有 `ONLINE_TRADER_RUNTIME_VIEW/1`（其 `ViewHistory` 已具备增量 `DELTA`），不立即拆分为三条独立通道。
- **暂缓进程内 `TqApi` 重建（L4）与多行情源（CTP）自动切换**：依托 TqSdk 重连能力与明确告警人工介入；若后续诊断实测证明存在 SDK 无法自动恢复的断线死锁，再单独评估 L4。
- **取消 72 小时无人值守强制验收**：改为多合约故障注入 + 完整日盘/夜盘模拟盘验收。

### 1.2 必须守住的安全不变量

1. **单一写入与单一发单**：`StandaloneDsvOwner` / `DsvSession` / `Journal` / `DsvSdkAdapter` 的单一所有权与先记账后发单边界保持不变。
2. **不放宽行情新鲜度门槛**：任何合约超过 `max_quote_age_seconds` 或 `age_quality != 'VALID'` 时禁止新开仓；`QUIET` 仅用于抑制全盘断流误报与界面准确提示，**绝不放行开新仓**；当 `max_quote_age_seconds == 0` 时，全局断流/重连/时钟异常门控依然必须阻断新开仓，并在 UI/配置提示中显式告警。
3. **保护与降风险责任不丢失**：UI 卡顿、快照过期或行情异常期间，已有未决订单、`UNKNOWN` 核对、持仓止盈止损与退出目标继续由 Owner 独立维持；工作台必须允许向当前 Owner 提交停策略、撤单、平仓和重新核对请求。
4. **人工停止不可被恢复覆盖**：故障期间用户一旦触发 `STOP` / `PAUSE_NEW_ENTRIES`，行情恢复后不得自动重新 `ARM`（继续严格校验 `authority_generation`）。
5. **不因空转优化跳过责任推进**：即使跳过无有效事实变化的 `Journal` 持久化，也绝不能跳过 `wait_update` 超时时的 SDK 任务推进、计时器到期检查、撤单/`UNKNOWN` 核对、保护计算与 IPC 命令响应。

---

## 2. 现状源码核验结论（基于 `12b4e6e`）

| 问题项 | 当前源码位置与实码事实 | 本方案处置 |
| --- | --- | --- |
| 合同冲突（`CONFLICT / NEEDS_REVIEW`） | `docs/refactor/runtime-workbench-current-20261120.md:49`（`CURRENT`）仍写“停用新的 UI 控制请求”，`runtime-command-bus-contract-20261120.md:15` 写 `validate current view + Capability` | **阶段 0 先行修订两份合同，再改代码** |
| 伪异步投影 | `StandaloneDsvOwner.prepare_workbench_publication()`（`src/online_trader/dsv_runtime/standalone_owner.py:104-112`）在 Owner 线程同步执行 `snapshot()`、`projection_batch()`、`workbench_view()`（含 `minute_chart_bars()`、`display_market_details()`、`canonical_bytes` 体积校验），后台 `_encoder` 仅做 `encode_frame()` | **PR2 改为真后台投影** |
| 冗余快照投影 | `StandaloneDsvOwner.step()` 每轮追加 `self._commit_views.append(self.snapshot())`（`standalone_owner.py:239`）；`ControlServer.serve()` 每轮调用两次 `_sync_streams()` 执行 `projection_batch()`（`src/online_trader/kernel/server.py:428, 441`） | **PR2 第一步清理无订阅冗余构造** |
| 每轮全量 SQLite 同步提交 | `DsvSession.apply_round() -> _commit()`（`src/online_trader/dsv_runtime/runner.py:476-510`）每轮无论是否有事实变化，均做 `deepcopy(self._state)`、`canonical_bytes` 与 `PRAGMA synchronous=FULL` 提交（`src/online_trader/dsv_storage/journal.py:78`） | **PR2 先测量，再优化空转持久化** |
| 默认等待窗口 500ms | `DsvSdkAdapter.step()` 无计时器时等待 `0.5s`（`src/online_trader/dsv_runtime/sdk_adapter.py:431`），与 IPC/投影共用同一循环 | **PR2 最后一步对照 500/100/50ms** |
| 订单/成交全量扫描 | `_order_facts()` / `_trade_facts()` 每轮扫描账户全部委托与成交（`sdk_adapter.py:547-555`） | **PR2 增量索引已终结对象** |
| Target Owner 缺行情诊断 | `QuoteTrace` / `MarketLatency` 仅接在 Legacy `runtime/dsv_simulation.py`；Target Owner 仅有 `order-blocking-log.jsonl` | **PR1 在 3.10.4 上先接入并采基线** |
| 过期快照四层锁死降风险命令 | Legacy 通过 `OWNER_RECHECK_OPERATIONS` 放行，但 Standalone 模式被 `_enable_commands()`、`_start_runtime_submit()`（`desktop/workbench.py:423-467, 648-652`）、`CommandBus.submit()`（`runtime_client/client.py:225-229`）和 `ClientStateStore.capability()`（`runtime_client/state.py:140-147`）四层阻挡 | **阶段 0（P0）立即放行** |
| 非行情异常被显示为行情滞后 | `desktop/workbench_layout.py:441-455, 1316-1317` 中，除了 `QUOTE_OUTSIDE_CURRENT_SESSION`，其余 `fresh=False` 且含 `source_time_utc` 的情况多落入 `'行情已滞后'` | **PR3-B 精确状态映射** |
| `max_quote_age_seconds = 0` 静默关停过滤 | `runner.py:1109` 与 `sdk_adapter.py:1115` 仅在 `limit > 0` 时检查开仓报价年龄，0 值会静默关闭开仓新鲜度检查 | **PR3-B 保留全局断流门控并显式告警** |
| TqSdk 3.10.4 强耦合 | 约 9 处硬编码校验 `'3.10.4'`；`tq_wait.py:20-48` Hook `loop.call_later` 与 `_set_wait_timeout`；`sdk_adapter.py:1177-1255` 比对多个 TqSdk 内部函数 `__code__` | **PR3-A 独立对照升级与回归验证** |
| 同日业务状态跨重启连续性未定稿 | 新 boot 强制重建临时账本并 `DISARMED` 同步柜台，但日停、月停、已消费分钟、保护阶段/峰值/保本线与故障期 `STOP` 如何跨 boot 衔接尚未定稿 | **独立 `NEEDS_REVIEW` ADR（不阻塞主线）** |
| Fake Broker 仅支持单合约 | `docs/ai/architecture-status.yaml:44` 记载 `qa/target_owner/fake_broker.py` 仅支持单合约与空仓起跑 | **PR4 前置扩展为多合约** |

---

## 3. 分阶段实施方案（按对照评审校准后的次序）

> **核心调整说明**：将原方案中混在 PR1 的“诊断接入”与“TqSdk 3.10.5 升级”拆开——先在当前 `3.10.4` 完成 **阶段 0 + PR1（轻量诊断）+ PR2（Owner 减负）** 并采集实机基线，再以独立可归因的提交完成 **PR3-A（3.10.5 对照升级）** 与 **PR3-B（健康分级与恢复闭环）**。

### 阶段 0（P0 安全热修）：先修合同冲突，再打通四层降风险命令通道

> **优先级**：P0（最高优先级，立即交付）

1. **先行修订两份 `CURRENT` 合同（解决 `CONFLICT / NEEDS_REVIEW`）**
   - 修订 [runtime-workbench-current-20261120.md](runtime-workbench-current-20261120.md) 第 49 行：将“投影过期停用新的 UI 控制请求”改为**按操作安全属性分级**：过期或截断时继续阻止新开仓、修改参数、启动策略和外部仓认领，但允许向当前 Owner 尝试提交降风险请求。
   - 同步修订 [runtime-command-bus-contract-20261120.md](runtime-command-bus-contract-20261120.md) 提交时序说明，明确旧快照只用于路由和绑定原 `account_key / kernel_boot_id / config_revision`，实态裁决完全由 Owner 完成。

2. **明确操作分级**
   - **过期仍可尝试提交**（复用 `OWNER_RECHECK_OPERATIONS`）：`STOP_DSV_STRATEGY`、`PAUSE_NEW_ENTRIES`、`CANCEL_SELECTED`、`CANCEL_OWNED`、`MANUAL_CLOSE`、`MANUAL_CLOSE_ALL`、`FLATTEN_THEN_STOP`、`REQUEST_RECONCILIATION`，以及已提交命令的回执查询。
   - **过期/截断继续禁止**：`ARM_ENTRIES`、`SAVE_DSV_PARAMETERS`、`MANUAL_OPEN`（`buy`/`sell`）、`ADOPT_EXTERNAL_POSITION`、`SET_ENTRY_POLICY`。

3. **四层打通改造**
   - `src/online_trader/desktop/workbench.py`：
     - `_enable_commands()`：当 `self.view is not None and not self._closing` 且 `self._runtime_command_bus is not None` 时，不再用 `enabled` 一刀切禁用 `close / flatten / cancel / cancel_entries / reconcile / pause_button`；仅对开仓/改参类按钮强制要求 `enabled`（即未过期且未截断）。
     - `_start_runtime_submit()`：当 `self._stale or self.published_snapshot_expired() or self.view['truncated']` 时，对 `operation in OWNER_RECHECK_OPERATIONS` 放行提交（界面提示“正在直连当前 Owner 核对并提交控制请求”），其余操作继续拦截。
   - `src/online_trader/desktop/runtime_client/state.py`：
     - `ClientStateStore.capability(operation)`：当 `view is not None` 且 `self.connection_state in {ClientConnectionState.READY, ClientConnectionState.STALE, ClientConnectionState.SYNCHRONIZING}` 时，若 `operation in OWNER_RECHECK_OPERATIONS`，则回退检查 `view['capabilities']` 内的原始权限结果（仍尊重 `E_STORAGE` 等硬阻挡），仅在 `DISCONNECTED / INCOMPATIBLE / RESYNC_REQUIRED` 或 `view is None` 时拒绝。
   - `src/online_trader/desktop/runtime_client/client.py`：
     - `CommandBus.submit()`：与 `ClientStateStore.capability()` 保持一致，对 `OWNER_RECHECK_OPERATIONS` 允许在 `STALE / SYNCHRONIZING` 且已有 `view` 时入队发送，仍使用 `view` 绑定 `account_key / kernel_boot_id / config_revision` 并写入 `PendingCommandRegistry`。

4. **Owner 侧实态再校验（确认保留）**
   - 保持 `StandaloneDsvOwner.execute_command()` -> `translate_workbench_command()` 的现有校验不变：`kernel_boot_id`、`account_key`、`config_revision` 与 `MANUAL_CLOSE` 的 `expected_target_revision` 均以 Owner 当前实态为准；若背景状态已变化，Owner 返回明确拒绝码（如 `E_BOOT_MISMATCH`、`E_REVISION_CONFLICT`），工作台据此刷新，不从旧快照推断平仓数量或重复发送。

---

### PR1（P0）：在现有 TqSdk 3.10.4 上恢复 Target Owner 轻量常驻诊断

> **关键原则**：本 PR 不改动 TqSdk 版本，专门在当前 `3.10.4` 上补全可见性与分阶段计时，为 PR2 性能优化和 PR3-A SDK 升级提供可比对基线。

1. **五段计时点位**（全部与 `kernel_boot_id`、`source_epoch` 关联）：
   - `T0 -> T1`（源报价年龄）：`source_age_raw_ns`（由已校准 UTC 与报价原始时间戳计算，附带 `clock_health.reference_status` 与 `utc_error_ns`）；
   - `T1 -> T2`（Owner 采集与策略处理）：`sdk_pump_ns`、`sdk_pump_gap_ns`、`native_facts_ns`、`round_transaction_ns`、`dispatch_ns`；
   - `T2 -> T3`（投影准备与发布）：`projection_prepare_ns`、`projection_encode_ns`（在后台线程记录）；
   - `T3 -> T4`（界面接收与渲染）：保留客户端已有延迟观察点（`desktop/workbench.py`）。

2. **默认开启的两级有界文件输出**（复用 `runtime/market_monitor.py:append_latency` 的 4MB 自动轮转与有界异步队列，写盘失败或队列满只累计丢弃计数，**严禁阻塞 Owner 线程**）：
   - **常规摘要（`target-market-summary.jsonl`，位于运行目录 `run/`）**：每 1 秒或周期性聚合输出 1 条统计摘要：
     ```json
     {
       "schema_version": "DSV_TARGET_MARKET_SUMMARY/1",
       "runtime_boot_id": "...",
       "observed_at_utc": "...",
       "source_epoch": "1",
       "sdk_version": "3.10.4",
       "sdk_polls": 10,
       "sdk_updates": 6,
       "sdk_timeouts": 4,
       "sdk_wait_max_ms": 102,
       "sdk_pump_gap_max_ms": 180,
       "owner_loop_max_ms": 135,
       "native_facts_max_ms": 8,
       "transaction_max_ms": 14,
       "projection_prepare_max_ms": 6,
       "publication_age_ms": 520,
       "active_symbols": 4,
       "fresh_symbols": 4,
       "max_source_age_ms": 480,
       "clock_state": "CALIBRATED",
       "queue_drops": 0,
       "write_failures": 0
     }
     ```
   - **异常触发黑匣子（`target-market-anomaly.jsonl`）**：平时在 Owner 内存环形缓冲区（例如 `deque(maxlen=256)`，覆盖约 60 秒窗口）保留最近轮次明细与每合约最新报价时间；一旦触发以下任一条件，将异常前后快照切片异步落盘（同类事件带 10 秒抑制冷却防刷盘）：
     - 多个活跃开盘合约同时超过 3 秒无新报价；
     - `sdk_pump_gap_ns > 1_000_000_000`（1 秒）；
     - 单轮 `owner_loop_ns > 500_000_000`（500 毫秒）；
     - `clock_health.reason` 非空或 `reference_status` 异常切换；
     - `source_epoch` 变化 / `disconnect` / `complete()` 翻转为 `False`；
     - `WorkbenchPublication` 状态报 `PUBLICATION_FAILED` 或 `PUBLICATION_OVERSIZE`。

---

### PR2（P0）：Owner 主循环减负与投影真后台化（严格按 6 步推进）

严格遵循以下 6 个顺序步骤，解决局部展示或存储负载扩大为全局延迟的问题：

1. **步骤 1 — 停止无订阅端时的 `_commit_views` 全量快照堆叠**：
   - 在 Standalone 模式下，视图客户端主要通过 `WorkbenchPublication` -> `ViewProcess` 消费状态；当 `ControlServer._stream_peers` 为空（无直接 `StreamHub` 订阅者）时，`StandaloneDsvOwner.step()` 不再每轮执行 `copy.deepcopy` 级别的 `self.snapshot()` 堆叠，也不在 `_sync_streams()` 中每轮两次调用 `projection_batch()`，仅记录轻量版本边界。

2. **步骤 2 — Owner 仅复制最小纯数据，后台线程构造 `projection_batch()` 与 `workbench_view()`**：
   ```text
   Owner 线程（ControlServer.serve 主循环）:
     1. 提取 ProjectionInput（纯 dict / tuple / 基本类型，无任何 TqSdk 活引用）:
        - session state 必要副本（仅订单/持仓/symbol 核心字段）
        - latest_decisions / runtime_commands / account_snapshot
        - adapter._health 拷贝、dispatch_market 标量摘要、display_market_details 纯 dict
        - minute_bars 缓存（仅在 K 线末尾变化时抽取原始数值元组列表）
     2. 将闭合函数/不可变对象交付给 WorkbenchPublication._encoder，立即返回 SDK 循环

   后台发布线程（WorkbenchPublication._encoder）:
     1. 执行 projection_batch(state_copy)
     2. 执行 workbench_view(state_copy, profile=..., projection_inputs=..., configuration_revision=...)
     3. K 线 Decimal 校验与格式整理 + 触发线计算 + 950KB 截断检查
     4. encode_frame(...) 并更新最新可供读取的帧
   ```

3. **步骤 3 — 切断 `workbench_view()` 对活 `adapter` 与 `DataFrame` 的读取**：
   - 调整 `standalone_projection.workbench_view()` 签名以优先接收预提取的 `projection_inputs`（保留传入 `adapter` 的测试兼容分支）；`DsvSdkAdapter.minute_chart_bars()`（`sdk_adapter.py:961-990`）保持 2 秒防抖，Owner 仅在 `serial` 长度或末行 `(datetime, close)` 变化时提取原始元组列表，严禁在后台线程触碰 `TqApi`、`DataFrame`、当前 `DsvSession` 可变状态或 `Journal` writer。

4. **步骤 4 — 对已吸收的终态订单与成交建立增量索引**：
   - 在 `sdk_adapter.py` 中维护“已终结（`FINISHED` 且本地已吸收）`order_id`”与“已吸收 `trade_id`”的增量索引，跳过已终结历史记录的重复 `native_day()`、字段解析和 `Decimal` 构造，避免长交易日下单量增长导致 `native_facts_ns` 线性恶化。

5. **步骤 5 — 测量无变化空轮成本后，再决定是否跳过持久化提交**：
   - 依托 PR1 采集的目标机 `transaction_ns` 数据，评估当 `wait_update` 返回 `False`（无原生数据到达）且无到期计时器、无 `clock_health` / `synchronized` / 业务状态变更时，跳过重复 `deepcopy(self._state)`、`canonical_bytes` 与 `PRAGMA synchronous=FULL` 提交的可行性；
   - 必须守住安全边界：**绝不能因为跳过持久化而跳过 `wait_update=False` 时的计时器检查、撤单/`UNKNOWN` 推进、保护计算与 IPC 命令响应**。

6. **步骤 6 — 最后调优 `poll_slice` / `wait_update` 上限**：
   - 完成上述 1–5 步减负后，再将 `sdk_adapter.py:431` 的默认 `0.5s` 上限参数化，在目标 Windows 机实测对比 `500ms`（基线）→ `100ms`（优先候选）→ `50ms`（对照）的 `sdk_pump_gap_ns`（P99）、`owner_loop_ns`（P99）、`transaction_ns`（P99）、CPU 与磁盘 IOPS，最后固化默认值。

---

### PR3（P1）：TqSdk 3.10.5 对照升级 + 轻量行情健康与恢复闭环

为保证每个变化都能独立归因，PR3 分两个可独立评审的交付项（`PR3-A` 与 `PR3-B`）：

#### PR3-A：TqSdk 3.10.5 对照升级与 Hook 回归

1. **版本约束统一**：集中常量或更新以下位置（以全仓搜索结果为准）：
   - `pyproject.toml`
   - `src/online_trader/runtime/startup_metadata.py`（`SDK_VERSION` 及日历标签说明）
   - `src/online_trader/gateway/tq_wait.py`
   - `src/online_trader/gateway/simnow.py`
   - `src/online_trader/dsv_runtime/sdk_startup.py`
   - `src/online_trader/dsv_runtime/standalone_owner.py`
   - `src/online_trader/runtime/launch.py`
   - `src/online_trader/runtime/startup_environment.py`
2. **内部 Hook 逐项回归（升级阻塞条件）**：
   - 验证 `tq_wait.py` 中 `loop.call_later` 拦截与 `api._set_wait_timeout` 在 3.10.5 下 `wait_mode` 仍返回 `'SDK_MONOTONIC_TIMEOUT'`，不退化为 `'SDK_TIMEOUT_NOT_OBSERVED'`；
   - 验证 `DsvSdkAdapter._sdk_send_refused_before_queue()` 与 `_sdk_cancel_refused_before_queue()` 对 `TqApi.insert_order`、`_get_insert_order_future_pack`、`_ensure_symbol`、`TqAuth._has_td_grants`、`TqRiskManager._could_insert_order` / `_could_cancel_order` 的字节码帧比对在 3.10.5 下仍能准确识别队列前拒单，不退化为 `UNKNOWN`；
   - 在 PR1+PR2 干净基线上对比 3.10.4 与 3.10.5 在 SimNow 下的重连、正常关闭与订阅恢复行为。

#### PR3-B：轻量行情健康检查、防呆与 UI 分级展示

1. **轻量行情健康模块（内聚在 `src/online_trader/dsv_runtime/market_health.py`）**：
   - **全局状态 `feed_health`**（负责解释共同源状态、告警及全局断流兜底门控）：`HEALTHY` / `DEGRADED` / `FEED_SUSPECTED_STALLED` / `RECONNECTING` / `RESYNCHRONIZING` / `CLOCK_UNCERTAIN` / `OWNER_STALLED`。仅在 2 个及以上活跃盘中合约同时超阈值无新报价（或唯一活跃合约超阈值且 SDK 无更新）时标为 `FEED_SUSPECTED_STALLED`。
   - **单合约状态 `symbol_health[code]`**（负责该合约开仓门槛与状态解释）：`FRESH` / `STALE` / `QUIET` / `MARKET_CLOSED` / `WAITING_FIRST_QUOTE` / `INVALID_QUOTE` / `SESSION_MISMATCH`。
   - **防呆规则**：`QUIET` 仅表示“全局健康、该合约暂未成交”，**不能豁免 `max_quote_age_seconds` 新鲜度检查**；针对 `runner.py:1109` 与 `sdk_adapter.py:1115` 中 `max_quote_age_seconds == 0` 跳过年龄检查的现有行为，当 `feed_health` 处于 `FEED_SUSPECTED_STALLED / RECONNECTING / RESYNCHRONIZING / CLOCK_UNCERTAIN` 时依然强制禁止新开仓，并在参数配置与界面显式标出“开仓报价年龄检查已关闭”风险提示。

2. **非破坏性安全恢复与人工告警**：
   - 全局行情异常期间通过 `feed_entry_blocked` 阻止新开仓，**保留 `state['running']`、已有挂单、持仓和退出/保护目标**；
   - 恢复须同时满足：`connection.complete() == True` 且在当前 `source_epoch`、收到当前盘段/柜台日有效新鲜报价、时钟正常、柜台持仓/`UNKNOWN`/活动委托核对完成，且 `authority_generation` 未被故障期人工 `STOP` / `PAUSE` 推进；
   - 持续异常超阈值（如 15–30 秒）时通过界面横幅、`WorkbenchSounds` 和 `WorkbenchDingTalk` 发出脱敏告警，由人工介入，不重启 Runtime。

3. **快照年龄分级与 UI 状态去误导**：
   - 在现有一套 `ONLINE_TRADER_RUNTIME_VIEW/1` 内展示 4 档年龄：`0–2s`（正常）、`2–5s`（偏慢）、`5–15s`（显示上次确认快照、阻断开仓/改参并放行降风险命令）、`>15s`（无法确认后台最新状态、仍可尝试向 Owner 发送降风险探测）；
   - 修正 `desktop/workbench_layout.py:441-455`，准确区分 `休市`、`时钟异常`、`等待开盘首报价`、`盘口静默` 与 `报价滞后`。

---

### 独立并行项（P1 / `NEEDS_REVIEW`）：业务状态重启连续性 ADR

依据对照文档第 4.7 与 9.5 节，单独起草一篇 ADR（不阻塞阶段 0 及 PR1–PR3 落地），明确以下**非订单发送责任**的跨 boot 业务连续性口径：
- 当日停开（`day_stop` / `day_stop_reason`）、月度停止（`month_stop`）及日内亏点/次数，能否从 `account-history.db` 与柜台当日成交历史可靠重建；
- 带仓重启时，`half_stage`、保护 `peak`、保本线与触发参考价的合法重建来源；
- 已消费分钟（`last_signal_bar`）与故障期间人工 `STOP` 意图如何防止重启后重复开仓；
- 当恢复证据不完整时，如何通过保持 `DISARMED` + 明确提示人工接管替代“静默清零后自动开仓”，同时严格遵守“不恢复旧执行 ledger 发送责任”的既定合同。

---

### PR4（P2 / 发布门槛）：多合约故障注入与完整交易时段验收

1. **QA 替身扩展（仅在 `qa/` 下，不进入生产 wheel）**：
   - 将 `qa/target_owner/fake_broker.py` 从单合约扩展为支持多合约，支持注入单合约静默、全合约停更、`wait_update` 阻塞、重连后不推新报价、时间偏移、投影编码阻塞、`runtime-view` 崩溃重启等。
2. **必须通过的故障注入矩阵**：
   - 全部活跃合约停更 10 秒 → 识别 `FEED_SUSPECTED_STALLED`、阻断新开仓、保留持仓与退出目标、触发告警；
   - 单合约静默 10 秒而其他合约正常 → 不误判全局断流；超 `max_quote_age_seconds` 时该合约仍禁止开新仓；
   - `wait_update` / SDK 阻塞 6 秒 → `target-market-summary.jsonl` 与 `target-market-anomaly.jsonl` 准确记录 `sdk_pump_gap_ms`；
   - 投影编码线程人工阻塞 20 秒 → Owner 循环、策略决策、持仓保护不受影响；工作台降风险按钮仍可直达 Owner 核验；
   - 休市时段 → 准确展示 `休市`，不误报行情滞后或断流；
   - 异常期间用户点击停止策略 → 行情恢复后保持 `DISARMED`，不自动开仓。
3. **模拟盘实测验收（经用户明确授权后执行）**：
   - 在 SimNow 环境完成至少一个**完整日盘 + 夜盘**运行，核对开盘、盘中休市恢复、日夜盘切换及一次受控断网重连全过程；
   - 验收 `target-market-summary.jsonl` 与 `target-market-anomaly.jsonl` 可完整定位异常时间线。

---

## 4. 推荐落地优先级总表

| 阶段 | 工作内容 | 优先级 | 独立验证目标 |
| --- | --- | --- | --- |
| **阶段 0** | 修订 Workbench/CommandBus 合同 + 打通四层过期快照降风险命令放行 | **P0** | `_stale`/`truncated` 时可提交 `OWNER_RECHECK_OPERATIONS`，开仓/改参仍禁止 |
| **PR1** | 基于 `3.10.4` 接入 Target Owner 轻量摘要与 60 秒异常黑匣子 | **P0** | 产出现场 `T0-T4` 与 Owner 各阶段耗时基线，写盘失败不阻塞交易 |
| **PR2** | Owner 减负 6 步走（清理 `_commit_views`、真后台投影、增量索引、空轮测量、调优 `poll_slice`） | **P0** | 后台阻塞 20s 不影响 Owner；无变化空轮不放大 fsync；pump 间隔达标 |
| **PR3-A** | `tqsdk 3.10.5` 对照升级与私有字节码 Hook 回归 | **P1** | `tq_wait.py` 与 `_sdk_*_refused_before_queue` 全部命中，重连订阅可对照验证 |
| **PR3-B** | `market_health.py`、0 值防呆、UI 4 档年龄与状态文案修正 | **P1** | 单合约静默不误报全盘断流；`QUIET` 不放行陈旧开仓；休市/时钟不误标滞后 |
| **独立 ADR** | 业务状态重启连续性决策定稿（`NEEDS_REVIEW`） | **P1** | 明确日停/保护/计数重启口径，不与订单安全边界冲突 |
| **PR4** | 多合约 `fake_broker` 故障注入 + 完整日夜盘模拟盘验收 | **P2 / 发布门槛** | 全部异常场景可证、可追溯、可恢复，无重复单与非法开仓 |

---

## 5. 量化验收指标

| 指标 | 目标值 | 说明 |
| --- | --- | --- |
| 活跃行情源年龄（正常推送合约） | `P99 ≤ 1 秒` | 以期货公司/行情源正常推送为前提，衡量本机观察到新报价后的端到端及时性 |
| SDK pump 启动间隔 | `P99 ≤ 250 ms` | 目标交易机上相邻两次 `wait_update` 启动的间隔 |
| Owner 单轮不含 `wait_update` 耗时 | `P99 ≤ 50 ms` | 原生事实提取 + 策略 + 账本提交 + 最小投影副本提取总和 |
| 陈旧行情引发的非法新开仓 | `0 次` | 含全合约断流、单合约静默、0 值配置与故障恢复瞬间 |
| 异常重复报单 | `0 次` | 断线、重连与 `UNKNOWN` 核对全程守住幂等与占量不变量 |
| 界面阻塞 / 快照过期期间控制可用性 | `100%` 可尝试发送降风险请求 | 停策略、撤单、人工平仓、重新核对可直达当前 Owner 核验 |

---

## 6. 开发与发布合规检查清单（依 `AGENTS.md`）

1. **测试边界**：`qa/` 与 `tests/` 不进入生产 wheel；未收到明确测试/打包指令前仅执行源码编译校验（`.venv\Scripts\python.exe -X utf8 -m compileall -q src tests qa`）。
2. **打包前必问**：每次正式打包前询问用户是否执行全量源码回归（`.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v`）。
3. **交付完整性与配置检查**：确认随包带入全部已确认配置文件（含 `runtime.json`、`OnlineTrader-defaults.json`、`OnlineTrader-parameters.json`）；允许包含经确认的白银主连及上期所 `ag` 具体合约月，并按其他品种的相同标准核验配置来源、参数有效性和最终归档内容；记录最终制品 SHA-256 与双击 `OnlineTrader.exe` 的独立验证结果。
