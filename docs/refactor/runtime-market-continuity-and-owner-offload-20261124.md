# Standalone Runtime 行情诊断、Owner 减负与快照连续性改进方案

2026-11-24 · 人工监督桌面自动交易终端基线（设计方案 / 未实施）

> 适用路径：Standalone Trading Runtime（`StandaloneDsvOwner`、`DsvSdkAdapter`、`DsvSession`、`ControlServer`、`WorkbenchPublication`、`ViewProcess`、`WorkbenchWindow`、`CommandBus`、`ClientStateStore`）  
> 基线提交：`b1be120`  
> 状态：`CANDIDATE / DESIGN_ONLY`（本文档仅作设计评审与排期依据，不授予账号连接、下单、测试或打包权限）

---

## 1. 目标定位与不可妥协原则

本方案面向**交易时段内人工监督下的高可靠桌面自动交易终端**，不追求 7×24 小时无人值守服务器集群架构。核心目标是在保留现有单一交易权威与 CS 隔离架构的前提下，解决以下三类已确认或待定位问题：

1. **可见性与取证缺失**：Target Owner 尚未接入行情延迟与主循环分阶段耗时诊断，发生全合约滞后时无法区分“行情源未推送 / SDK 重连丢订阅 / Owner 主循环阻塞 / 本地时钟偏差 / UI 快照过期”。
2. **Owner 主线程负载过重**：`ControlServer` 主循环、`DsvSdkAdapter.step()`、`DsvSession._commit()` 与 `prepare_workbench_publication()` 同步耦合，界面投影准备与高频率全量持久化可能反过来拖慢行情读取和下单发送。
3. **过期快照安全门禁过宽与状态误分类**：Standalone 工作台在快照过期时连降风险命令一起锁死；非行情原因在界面上容易被统一展示为“行情滞后”。

### 1.1 明确取消 / 暂缓的范围（避免过度重构）

- **不拆交易权威**：不将 `TqApi.wait_update()` 移至后台线程与策略线程并发读写 SDK 对象；不引入第二个执行器或跨进程自动接管。
- **不新建独立 `market_health` 大子系统**：先在 `dsv_runtime/` 内聚轻量健康判定模块，复用现有 `scheduling/clock.py` 与 `runtime/market_monitor.py`。
- **暂缓三流 IPC 协议重写**：优先优化现有 `ONLINE_TRADER_RUNTIME_VIEW/1`（其 `ViewHistory` 已具备增量 `DELTA`），不立即拆分为三条独立通道。
- **暂缓进程内 `TqApi` 重建（L4）与多行情源（CTP）自动切换**：依托 TqSdk 重连能力与明确告警人工介入；若后续 PR1 实测证明存在 SDK 无法自动恢复的断线死锁，再单独评估 L4。
- **取消 72 小时无人值守强制验收**：改为多合约故障注入 + 完整日盘/夜盘模拟盘验收。

### 1.2 必须守住的安全不变量

1. **单一写入与单一发单**：`StandaloneDsvOwner` / `DsvSession` / `Journal` / `DsvSdkAdapter` 的单一所有权与先记账后发单边界保持不变。
2. **不放宽行情新鲜度门槛**：任何合约超过 `max_quote_age_seconds` 或 `age_quality != 'VALID'` 时禁止新开仓；`QUIET` 仅用于抑制全盘断流误报与界面准确提示，**绝不放行开新仓**。
3. **保护与降风险责任不丢失**：UI 卡顿、快照过期或行情异常期间，已有未决订单、`UNKNOWN` 核对、持仓止盈止损与退出目标继续由 Owner 独立维持；工作台必须允许向当前 Owner 提交停策略、撤单、平仓和重新核对请求。
4. **人工停止不可被恢复覆盖**：故障期间用户一旦触发 `STOP` / `PAUSE_NEW_ENTRIES`，行情恢复后不得自动重新 `ARM`（继续严格校验 `authority_generation`）。

---

## 2. 现状源码核验结论（基于 `b1be120`）

| 问题项 | 当前源码位置与实码事实 | 本方案处置 |
| --- | --- | --- |
| 伪异步投影 | `StandaloneDsvOwner.prepare_workbench_publication()`（`src/online_trader/dsv_runtime/standalone_owner.py:104-112`）在 Owner 线程同步执行 `snapshot()`、`projection_batch()`、`workbench_view()`（含 `minute_chart_bars()`、`display_market_details()`、`canonical_bytes` 体积校验），后台 `_encoder` 仅做 `encode_frame()` | **PR2 改为真后台投影** |
| 冗余快照投影 | `StandaloneDsvOwner.step()` 每轮追加 `self._commit_views.append(self.snapshot())`（`standalone_owner.py:239`）；`ControlServer.serve()` 每轮调用两次 `_sync_streams()` 执行 `projection_batch()`（`src/online_trader/kernel/server.py:428, 441`） | **PR2 清理无订阅冗余构造** |
| 每轮全量 SQLite 同步提交 | `DsvSession.apply_round() -> _commit()`（`src/online_trader/dsv_runtime/runner.py:476-510`）每轮无论是否有事实变化，均做 `deepcopy(self._state)`、`canonical_bytes` 与 `PRAGMA synchronous=FULL` 提交（`src/online_trader/dsv_storage/journal.py:78`） | **PR2 基准评估后优化空转轮次** |
| 默认等待窗口 500ms | `DsvSdkAdapter.step()` 无计时器时等待 `0.5s`（`src/online_trader/dsv_runtime/sdk_adapter.py:431`），与 IPC/投影共用同一循环 | **PR2 在解决写入放大后对照 500/100/50ms** |
| 订单/成交全量扫描 | `_order_facts()` / `_trade_facts()` 每轮扫描账户全部委托与成交（`sdk_adapter.py:547-555`） | **PR2 增量缓存已终结对象** |
| Target Owner 缺行情诊断 | `QuoteTrace` / `MarketLatency` 仅接在 Legacy `runtime/dsv_simulation.py`；Target Owner 仅有 `order-blocking-log.jsonl` | **PR1 恢复轻量常驻诊断** |
| 过期快照四层锁死降风险命令 | Legacy 通过 `OWNER_RECHECK_OPERATIONS` 放行，但 Standalone 模式被 `_enable_commands()`、`_start_runtime_submit()`（`desktop/workbench.py:423-467, 648-652`）、`CommandBus.submit()`（`runtime_client/client.py:225-229`）和 `ClientStateStore.capability()`（`runtime_client/state.py:140-147`）四层阻挡 | **P0 热修（与 PR1 同批提前交付）** |
| 非行情异常被显示为行情滞后 | `desktop/workbench_layout.py:441-455, 1316-1317` 中，除了 `QUOTE_OUTSIDE_CURRENT_SESSION`，其余 `fresh=False` 且含 `source_time_utc` 的情况多落入 `'行情已滞后'` | **PR3 精确状态映射** |
| TqSdk 3.10.4 强耦合 | 约 9 处硬编码校验 `'3.10.4'`；`tq_wait.py:20-48` Hook `loop.call_later` 与 `_set_wait_timeout`；`sdk_adapter.py:1177-1255` 比对多个 TqSdk 内部函数 `__code__` | **PR1 升级前必须逐项验证** |
| Fake Broker 仅支持单合约 | `docs/ai/architecture-status.yaml:44` 记载 `qa/target_owner/fake_broker.py` 仅支持单合约与空仓起跑 | **PR4 前置扩展为多合约** |

---

## 3. 分阶段实施方案

### 阶段 0（P0 安全热修）：解除快照过期对降风险命令的四层阻挡

> **优先级**：P0（必须提前，不等待 PR3；可在 PR1 中作为独立前置提交完成）

1. **明确操作分级**
   - 复用并对齐既有 `OWNER_RECHECK_OPERATIONS` 集合（`STOP_DSV_STRATEGY`、`PAUSE_NEW_ENTRIES`、`CANCEL_SELECTED`、`CANCEL_OWNED`、`MANUAL_CLOSE`、`MANUAL_CLOSE_ALL`、`FLATTEN_THEN_STOP`、`REQUEST_RECONCILIATION`）及已提交命令回执查询。
   - **禁止放行**（在 `_stale` / `truncated` / 非 `READY` 时仍拦截）：`ARM_ENTRIES`、`SAVE_DSV_PARAMETERS`、`MANUAL_OPEN`（`buy`/`sell`）、`ADOPT_EXTERNAL_POSITION`、`SET_ENTRY_POLICY`。

2. **四层打通改造**
   - `src/online_trader/desktop/workbench.py`：
     - `_enable_commands()`：当 `self.view is not None and not self._closing` 且 `self._runtime_command_bus is not None` 时，不再用 `enabled` 一刀切禁用 `close / flatten / cancel / cancel_entries / reconcile / pause_button`；仅对开仓/改参类按钮强制要求 `enabled`（即未过期且未截断）。
     - `_start_runtime_submit()`：当 `self._stale or self.published_snapshot_expired() or self.view['truncated']` 时，对 `operation in OWNER_RECHECK_OPERATIONS` 放行提交（提示“正在直连当前 Owner 核对并提交控制请求”），其余操作继续拦截。
   - `src/online_trader/desktop/runtime_client/state.py`：
     - `ClientStateStore.capability(operation)`：当 `view is not None` 且 `self.connection_state in {ClientConnectionState.READY, ClientConnectionState.STALE, ClientConnectionState.SYNCHRONIZING}` 时，若 `operation in OWNER_RECHECK_OPERATIONS`，则回退检查 `view['capabilities']` 内的原始权限结果（仍尊重 `E_STORAGE` 等不可操作故障），仅在 `DISCONNECTED / INCOMPATIBLE / RESYNC_REQUIRED` 或 `view is None` 时拒绝。
   - `src/online_trader/desktop/runtime_client/client.py`：
     - `CommandBus.submit()`：与 `ClientStateStore.capability()` 保持一致，对 `OWNER_RECHECK_OPERATIONS` 允许在 `STALE / SYNCHRONIZING` 且已有 `view` 时入队发送，仍使用 `view` 绑定 `account_key / kernel_boot_id / config_revision` 并写入 `PendingCommandRegistry`。

3. **Owner 侧实态再校验（确认保留）**
   - 保持 `StandaloneDsvOwner.execute_command()` -> `translate_workbench_command()` 的现有校验不变：`kernel_boot_id`、`account_key`、`config_revision` 与 `MANUAL_CLOSE` 的 `expected_target_revision` 均以 Owner 当前实态为准；若背景状态已变化，Owner 返回明确拒绝码（如 `E_BOOT_MISMATCH`、`E_REVISION_CONFLICT`），工作台据此刷新，不会因旧快照误平或重发。

---

### PR1（P0）：Target Owner 轻量常驻诊断 + TqSdk 3.10.5 对照验证

> **执行原则**：先在 `tqsdk 3.10.4` 合入诊断并采集一轮基线，再切换 `tqsdk 3.10.5` 对照验证，确保可明确判断 SDK 升级是否真正解决全合约滞后。

#### 1.1 恢复 Target Owner 轻量常驻诊断

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
       "sdk_version": "3.10.x",
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

#### 1.2 TqSdk 3.10.5 升级与兼容性核验

1. **版本约束统一**：定义集中常量或更新以下位置（以实际搜索结果为准）：
   - `pyproject.toml`
   - `src/online_trader/runtime/startup_metadata.py`（`SDK_VERSION` 及日历标签说明）
   - `src/online_trader/gateway/tq_wait.py`
   - `src/online_trader/gateway/simnow.py`
   - `src/online_trader/dsv_runtime/sdk_startup.py`
   - `src/online_trader/dsv_runtime/standalone_owner.py`
   - `src/online_trader/runtime/launch.py`
   - `src/online_trader/runtime/startup_environment.py`
2. **内部 Hook 逐项回归（升级阻塞条件）**：
   - 验证 `tq_wait.py` 中 `loop.call_later` 拦截与 `api._set_wait_timeout` 在 3.10.5 下 `wait_mode` 仍返回 `'SDK_MONOTONIC_TIMEOUT'`，而不是退化为 `'SDK_TIMEOUT_NOT_OBSERVED'`；
   - 验证 `DsvSdkAdapter._sdk_send_refused_before_queue()` 与 `_sdk_cancel_refused_before_queue()` 对 `TqApi.insert_order`、`_get_insert_order_future_pack`、`_ensure_symbol`、`TqAuth._has_td_grants`、`TqRiskManager._could_insert_order` / `_could_cancel_order` 的调用栈帧比对在 3.10.5 下仍能准确识别队列前拒单（构造离线/受控测试验证不误退化为 `UNKNOWN`）；
   - 在 SimNow 环境对比 3.10.4 与 3.10.5 在正常运行与受控断网重连下的行情推送与订阅恢复。

---

### PR2（P0）：Owner 主循环减负、投影真后台化与轮询调优

#### 2.1 投影准备与编码彻底移出 Owner 线程

改造目标：`StandaloneDsvOwner.prepare_workbench_publication()` 在 Owner 线程内只采集**最小不可变/纯字典输入**，所有展示计算、格式化与体积校验移至 `WorkbenchPublication._encoder` 后台线程。

```text
Owner 线程（ControlServer.serve 主循环）:
  1. 提取 ProjectionInput（纯 dict / tuple / 基本类型，无任何 TqSdk 活引用）:
     - session state 浅/必要深拷贝（仅订单/持仓/symbol 核心字段）
     - latest_decisions / runtime_commands / account_snapshot
     - adapter._health 拷贝、dispatch_market 标量摘要、display_market_details 纯 dict
     - minute_bars 缓存（见下：仅在 K 线末尾变化时抽取原始数值列表）
  2. 将闭合函数/不可变对象交付给 WorkbenchPublication._encoder，立即返回 SDK 循环

后台发布线程（WorkbenchPublication._encoder）:
  1. 执行 projection_batch(state_copy)
  2. 执行 workbench_view(state_copy, profile=..., projection_inputs=..., configuration_revision=...)
  3. K 线 Decimal 校验与格式整理 + 触发线计算 + 950KB 截断检查
  4. encode_frame(...) 并更新最新可供读取的帧
```

- **K 线转换减负细节**：`DsvSdkAdapter.minute_chart_bars()`（`sdk_adapter.py:961-990`）保持 2 秒防抖；Owner 仅在 `serial` 长度或末行 `(datetime, close)` 变化时将最多 48 行原始数值提取为纯 Python 元组列表（不碰 `DataFrame` 跨线程传递），`Decimal` 校验与字典组装可放到后台或按需惰性执行。
- **解耦 `standalone_projection.workbench_view()` 对 `adapter` 活对象的依赖**：将 `workbench_view(...)` 签名改为支持传入预提取的 `projection_inputs`（纯数据结构体/字典），兼容现有离线单测传 `adapter` 的调用方式，同时确保生产后台发布路径零接触活 `adapter` / `TqApi`。

#### 2.2 消除 `ControlServer` 冗余投影与 `DsvSdkAdapter` 全量扫描

1. **清理 `_commit_views` 冗余工作**：
   - 当前在 Standalone 模式下，视图客户端主要通过 `WorkbenchPublication` -> `ViewProcess` 消费状态；若 `ControlServer._stream_peers` 为空（无直接 `StreamHub` 订阅者），`StandaloneDsvOwner.step()` 不需要每轮都执行 `copy.deepcopy` 级别的 `self.snapshot()` 堆叠，也不需要在 `_sync_streams()` 中每轮两次调用 `projection_batch()`。改为仅在有活跃 `stream_peers` 时生成流式增量，或仅记录轻量版本边界。
2. **`DsvSdkAdapter` 增量原生事实缓存**：
   - 在 `sdk_adapter.py` 中维护“已终结（`FINISHED` 且本地已吸收）`order_id`”与“已吸收 `trade_id`”的轻量索引，跳过已终结历史记录的重复 `native_day()`、字段解析和 `Decimal` 构造，避免日内单量增长导致 `native_facts_ns` 线性恶化。

#### 2.3 `wait_update` 窗口与 SQLite 提交频率协同调优

1. **严禁只缩 `wait_update` 不看 `DsvSession._commit()`**：
   - 在 `sdk_adapter.py:431` 将默认 `poll_slice` 上限从 `0.5s` 参数化，并结合 `DsvSession.apply_round()` 分析：当某轮 `wait_update` 返回 `False`（无任何原生数据到达）且无到期计时器、无 `clock_health` / `synchronized` 变更时，评估通过“跳过无变化空转轮次”或“仅在行情/订单/定时器/秒边界变化时推进 `apply_round`”来避免无效的 `deepcopy` + `PRAGMA synchronous=FULL` SQLite fsync。
   - **约束**：若修改空转提交条件，必须保持 `DsvSession` 序列号连续性、`timing` 计时精度、`clock_continuity` 15 秒卡顿检测和每轮对 `ControlServer` 管道命令的及时响应（管道有入站命令时 `ControlServer` 照常处理，不依赖 `apply_round` 推进）。
2. **三档基准对比**（在 PR1 诊断开启后于目标 Windows 机实测）：
   - `500ms`（基线）→ `100ms`（优先候选）→ `50ms`（对照）；
   - 比较 `sdk_pump_gap_ns`（P99）、`owner_loop_ns`（P99）、`transaction_ns`（P99）、CPU 占用与每秒磁盘写入量后固化默认值。

---

### PR3（P1）：轻量行情健康检查、UI 准确分级与安全恢复逻辑

#### 3.1 轻量行情健康模块（内聚在 `src/online_trader/dsv_runtime/market_health.py`）

不建立独立多文件子系统，仅增加一个纯逻辑小模块（1 个文件，约 150–250 行），由 `DsvSdkAdapter` 在每轮 `_observe_quote` 后更新并写入快照投影：

1. **全局行情源健康状态（`feed_health`）**：
   - `HEALTHY`：SDK 循环正常推进，且处于盘中的活跃合约存在近期有效行情更新；
   - `DEGRADED`：单轮延迟偏高或部分活跃合约超时未更新，尚未达到断流门槛；
   - `FEED_SUSPECTED_STALLED`：存在 2 个及以上处于开盘时段（`market_session_active=True`）的合约同时连续超过阈值（如 5 秒，需排除集合竞价/午休/休市）未见报价时间推进（或唯一活跃合约超过阈值且 SDK 无更新）；
   - `RECONNECTING` / `RESYNCHRONIZING`：Gateway `complete()` 为 `False`、网络断线或 `source_epoch` 刚切换等待首帧对齐；
   - `CLOCK_UNCERTAIN`：`clock_health.reason` 非空或 `_protection_clock_ready()` 为 `False`；
   - `OWNER_STALLED`：`sdk_pump_gap_ns` 或本机单调时钟间隔触发 Owner 阻塞告警。

2. **单合约行情状态（`symbol_health[code]`）**：
   - `FRESH`：当前处于交易盘段、`age_quality == 'VALID'` 且满足新鲜度要求；
   - `STALE`：在盘中且报价年龄超过策略允许阈值（如 `max_quote_age_seconds`）；
   - `QUIET`：报价字段合法、盘段正确且全局行情健康（其他活跃合约正常跳动），仅因该合约自身暂未成交导致无新报价；
   - `MARKET_CLOSED`：当前不在交易时段（`QUOTE_OUTSIDE_CURRENT_SESSION`）；
   - `WAITING_FIRST_QUOTE`：盘段刚开尚无本盘段报价；
   - `INVALID_QUOTE`：时间戳畸形、回退、字段无效或未通过盘口合法性检查；
   - `SESSION_MISMATCH`：报价时间戳属于其他盘段或交易日。

#### 3.2 简单的安全恢复与告警规则

- **异常阻断**：当 `feed_health in {'FEED_SUSPECTED_STALLED', 'RECONNECTING', 'RESYNCHRONIZING', 'CLOCK_UNCERTAIN'}` 时，Adapter / Session 通过非破坏性门控（如 `feed_entry_blocked=True`）阻止新开仓，**不改变 `state['running']` 授权标记，不撤销保护责任与退出目标**。
- **自动恢复条件**：当且仅当以下条件全部成立时解除 `feed_entry_blocked`：
  1. `connection.complete()` 为 `True` 且处于当前 `source_epoch`；
  2. 收到当前盘段、当前交易日的有效新鲜报价；
  3. 时钟校准正常；
  4. 柜台持仓、`UNKNOWN` 订单及活动委托完成本轮同步核对；
  5. 若故障期间用户提交过 `STOP` / `PAUSE_NEW_ENTRIES`（`state['running']` 已变为 `False` 且 `authority_generation` 已递增），恢复后维持 `DISARMED`，不得自动 `ARM`。
- **人工介入告警**：若 `FEED_SUSPECTED_STALLED` / `RECONNECTING` 持续超过告警阈值（如 15–30 秒），通过现有界面显著横幅、`WorkbenchSounds` 提示音和 `WorkbenchDingTalk` 发出一次性状态变更通知，提示用户介入检查网络/行情源。

#### 3.3 快照年龄分级与 UI 准确展示

1. **快照年龄显示分级**（在 `desktop/workbench.py` 与 `desktop/workbench_layout.py` 实现，保留 `MAX_AGE_NS` 协议兼容性或显式区分心跳与快照年龄）：
   - `0–2 秒`：正常实时；
   - `2–5 秒`：提示“界面刷新偏慢”，仍可正常显示与操作；
   - `5–15 秒`：显示上次确认快照并标记“工作台快照滞后 N 秒（后台策略由 Owner 独立维持）”，禁止新开仓/改参等依赖最新快照的命令，但**继续放行阶段 0 的降风险命令**；
   - `> 15 秒`：标记“无法确认后台最新状态（建议核查运行进程/日志）”，依然允许向 Owner 发送降风险探测请求。
2. **修正 `workbench_layout.py:441-455` 状态文案**：将 `MARKET_CLOSED`（休市）、`CLOCK_UNCERTAIN`（时钟异常）、`WAITING_FIRST_QUOTE`（等待开盘首报价）、`QUIET`（盘口静默，若超时明确提示“静默超限暂停新开”）与 `STALE`（报价滞后）一一对应展示，不再把非行情异常统一显示为“行情滞后”。

---

### PR4（发布门槛）：多合约故障注入与完整交易时段验收

1. **QA 替身扩展（仅在 `qa/` 下，不进入生产 wheel）**：
   - 将 `qa/target_owner/fake_broker.py` 从单合约扩展为支持多合约，支持按合约注入：单合约停更、全合约停更、`wait_update` 阻塞、重连后不推新报价、时间偏移、投影编码阻塞、`runtime-view` 崩溃重启等。
2. **必须通过的故障注入矩阵**：
   - 全部活跃合约停更 10 秒 → 识别 `FEED_SUSPECTED_STALLED`、阻断新开仓、保留持仓与退出目标、触发告警；
   - 单合约静默 10 秒而其他合约正常 → 不误判全局断流；超 `max_quote_age_seconds` 时该合约仍禁止开新仓；
   - `wait_update` / SDK 阻塞 6 秒 → 诊断日志记录 `sdk_pump_gap_ms` 异常并触发黑匣子落盘；
   - 投影编码线程人工阻塞 20 秒 → Owner 循环、策略决策、持仓保护不受影响；工作台降风险按钮仍可向 Owner 提交停止/撤单/平仓；
   - 休市时段 → 准确展示 `休市`，不误报行情滞后或断流；
   - 异常期间用户点击停止策略 → 行情恢复后保持 `DISARMED`，不自动开仓。
3. **模拟盘实测验收（经用户明确授权后执行）**：
   - 在 SimNow 环境完成至少一个**完整日盘 + 夜盘**运行，核对开盘、盘中休市恢复、日夜盘切换及一次受控断网重连全过程；
   - 验收 `target-market-summary.jsonl` 与 `target-market-anomaly.jsonl` 可完整定位异常时间线。

---

## 4. 量化验收指标

| 指标 | 目标值 | 说明 |
| --- | --- | --- |
| 活跃行情源年龄（正常推送合约） | `P99 ≤ 1 秒` | 以期货公司/行情源正常推送为前提，衡量本机观察到新报价后的端到端及时性 |
| SDK pump 启动间隔 | `P99 ≤ 250 ms` | 目标交易机上相邻两次 `wait_update` 启动的间隔 |
| Owner 单轮不含 `wait_update` 耗时 | `P99 ≤ 50 ms` | 原生事实提取 + 策略 + 账本提交 + 最小投影副本提取总和 |
| 陈旧行情引发的非法新开仓 | `0 次` | 含全合约断流、单合约静默与故障恢复瞬间 |
| 异常重复报单 | `0 次` | 断线、重连与 `UNKNOWN` 核对全程守住幂等与占量不变量 |
| 界面阻塞 / 快照过期期间控制可用性 | `100%` 可尝试发送降风险请求 | 停策略、撤单、人工平仓、重新核对可直达当前 Owner 核验 |

---

## 5. 开发与发布合规检查清单（依 `AGENTS.md`）

1. **测试边界**：`qa/` 与 `tests/` 不进入生产 wheel；未收到明确测试/打包指令前仅执行源码编译校验（`.venv\Scripts\python.exe -X utf8 -m compileall -q src tests qa`）。
2. **打包前必问**：每次正式打包前询问用户是否执行全量源码回归（`.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v`）。
3. **交付完整性与配置检查**：确认随包带入全部已确认配置文件（含 `runtime.json`、`OnlineTrader-defaults.json`、`OnlineTrader-parameters.json`）；允许包含经确认的白银主连及上期所 `ag` 具体合约月，并按其他品种的相同标准核验配置来源、参数有效性和最终归档内容；记录最终制品 SHA-256 与双击 `OnlineTrader.exe` 的独立验证结果。
