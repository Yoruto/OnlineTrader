# Standalone Runtime 行情明细与策略解释空白问题记录

> 日期：2026-11-23  
> 路径：Standalone Trading Runtime / Target Owner / Runtime Workbench  
> 环境：SimNow 模拟账户 `****91`  
> 状态：源码已修复（含同类投影/日志缺口及 4 项定向单测）；全量回归、重新打包与最终只读 GUI 复测未执行  
> 初始修复快照（写入第 1–12 节时）：HEAD `ebf4c4a9274f371cac582496786af66641f74ba7`，worktree tree SHA-256 `a0c7c857d8695b3a33db2fdb95329221786209aca613d97d1b2ba778fdc78021`（第 13 节记录其后的补充修复与定向单测）

## 1. 摘要

最新 dev37 配置包能够连接 SimNow、同步资金和最新价，也能在 Owner 内部持续形成策略决策，但 Workbench 首页长期显示：

- “当前关注”为 `—`；
- 多头触发、空头触发和最近判断为 `—`；
- 1 分钟图显示“等待原生 K 线”；
- 四个品种显示“等待触发”，实际 Runtime 已经因 `GEOMETRY_BLOCK` 阻止开仓；
- “日志与诊断”显示“日志来源未绑定”，用户看不到真实阻挡原因。

问题不是 SimNow 没有行情，也不是 Qt 没有刷新，而是 **Target Owner 生成的 Workbench 只读投影缺少决策、触发线、分钟 OHLC 和运行目录身份**。基础行情字段可以穿过 IPC，策略解释字段却从未进入快照，因此界面只能忠实显示空值。

本次还暴露了一项操作判断错误：为了“更容易成交”把 `open_offset` 降到 2。当前几何规则为：

```text
geometry_limit = 2 × open_offset − stop_offset
当日振幅 = day_high − day_low
当 当日振幅 > geometry_limit 时，结果为 GEOMETRY_BLOCK
```

四个品种的止损为 12–20 点，`open_offset=2` 会使几何上限变成负数，因此任何正常的正振幅都会被阻止。偏移越小并不一定越容易成交；在这套规则中，盲目降低偏移反而会必然禁开。

## 2. 被测版本与证据范围

### 2.1 原始制品

- 文件：`OnlineTrader-0.1.0.dev37-windows-x64-Runtime-Configured.zip`
- ZIP SHA-256：`838E053E8C801631AC0496AD8D82F2447AAACE46E64A5B884267BC79A77C3B68`
- 制品源码 HEAD：`ebf4c4a9274f371cac582496786af66641f74ba7`
- 隔离测试目录：`output/testing/dev37-sim-20261123-1008/`
- 原始黑盒报告：`output/testing/dev37-sim-20261123-1008/evidence/black-box-ui-geometry-evidence-20261123-1101.md`

### 2.2 授权与安全边界

- 用户明确授权使用最新打包版本进行 SimNow 真实模拟测试，并允许修改参数促进成交。
- 未切换实盘，未测试白银或 `ag` 合约。
- 修复开发结束后没有重新打包。
- 主 Agent 只运行编译检查；没有运行单元、冒烟或全量源码回归。
- 独立测试 Agent 通过真实 Workbench 复测界面；测试结束时 Owner 为 `DISARMED`、持仓 0、活动委托 0。

## 3. 时间线

| 时间（北京时间） | 事件 | 结果 |
| --- | --- | --- |
| 09:31 | dev37 Runtime 配置包生成 | 后续测试使用该 ZIP 的全新隔离解压副本 |
| 10:30 | 计划开始盘中测试 | 未按时开始；参数导出、旧 Runtime 生命周期和重启处理耗时过长 |
| 10:55:55 | 新 Runtime ARM | 四个品种全部记录 `ORDER_BLOCKED / GEOMETRY_BLOCK`，无委托、无成交 |
| 11:00:39 | 用户截图并指出界面空白 | 最新价存在，但当前关注、触发线、最近判断和 K 线为空 |
| 11:01 | 独立黑盒证据固定 | 证明 Runtime 有阻挡事实，Workbench 未显示 |
| 11:14 后 | 第一轮源码修复复测 | 触发线、判断时间、K 线和阻挡原因恢复；日志来源仍未绑定 |
| 11:23 后 | 增加运行目录投影 | 因缺少 `run-identity.json`，日志路径仍被安全校验拒绝 |
| 11:31 后 | 最终候选启动 | `run-identity.json` 已生成，Runtime 为 `READY / DISARMED`；最终 GUI 日志绑定确认被实体 `Esc` 中断 |

10:30–10:55 的延误不能算作有效交易测试。参数导出失败和旧 Runtime 难以正常退出是实际缺陷，但不应掩盖“没有及时走到真实策略结果”的事实。

## 4. 现象与第一处差异

### 4.1 用户可见现象

Workbench 同时显示：

- 行情已更新；
- 交易已连接；
- 事实同步完整；
- 四个合约均有最新价；
- 当前关注、两向触发线、最近判断和 K 线为空；
- 状态错误地表现为“等待触发”。

这说明连接、基础快照和 Qt 刷新链路并未整体失效。第一处差异位于 **Target Owner 构造 Workbench 投影**：基础行情进入了 `markets`，策略解释和分钟序列没有进入。

### 4.2 逻辑层实际事实

只读 Runtime ledger 的 `ROUND` 事件证明每轮输入包含：

- 四个合约的新鲜报价；
- `native_candle_id` 与 `native_candle_ns`；
- 最新价、买一、卖一、日高、日低；
- 完整的 `GEOMETRY_BLOCK` 决策收据。

以现场一轮为例：

| 合约 | 日高 | 日低 | 日内振幅 | `open_offset` | 止损 | 几何上限 | 结果 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `DCE.p2701` | 8330 | 8161 | 169 | 2 | 16 | -12 | `GEOMETRY_BLOCK` |
| `DCE.l2701` | 7980 | 7811 | 169 | 2 | 20 | -16 | `GEOMETRY_BLOCK` |
| `SHFE.bu2701` | 4312 | 4202 | 110 | 2 | 12 | -8 | `GEOMETRY_BLOCK` |
| `CZCE.OI701` | 9956 | 9827 | 129 | 2 | 15 | -11 | `GEOMETRY_BLOCK` |

因此“不成交”的直接原因是几何规则明确阻挡，不是行情缺失，也不是触发价尚未达到。

## 5. 根因分析

### 5.1 根因一：分钟 serial 只取身份，完整 OHLC 未投影

`DsvSdkAdapter` 已为每个合约订阅 60 秒 K 线 serial，但原实现只读取末行 `datetime`，生成 `candle_id` 和 `native_candle_ns`。`open/high/low/close` 没有形成有界的显示副本，也没有进入 Workbench 快照。

结果：

- 策略拥有当前分钟身份，能够执行机会消费和同 K 线判断；
- Workbench 却没有任何可绘制的 OHLC 数据，只能显示“等待原生 K 线”。

### 5.2 根因二：决策收据已提交，但 Owner 未保存展示摘要

`DsvSession.apply_round()` 的收据含每个合约的决策原因和 `evaluated_at`，账本也完整保存 `ROUND`。原 `StandaloneDsvOwner.step()` 只递增投影序号，没有保留当前 boot 的“最近决策”展示摘要。

结果：执行事实存在，但同步快照无法回答“最近为什么没有下单”。

### 5.3 根因三：Standalone 投影缺少 UI 已支持的字段

原 `workbench_view()` 发布最新价、盘口、日高低等字段，但没有发布：

- `minute_bars`；
- `trigger_prices`；
- `decision_time_utc`；
- 当前决策原因对应的 `entry_reasons`。

Workbench 的渲染代码本来就支持这些字段。IPC 传输、ClientStateStore 和 Qt 刷新没有丢数据；它们收到的源快照本身不完整。

### 5.4 根因四：阻挡状态时强制隐藏“当前关注”

原界面只在状态等于“等待触发”时计算距离最新价最近的触发线。一旦状态正确显示为 `GEOMETRY_BLOCK`，即使两条触发线已经存在，“当前关注”仍会显示 `—`。

这是表现层条件过窄，不是业务逻辑缺失。

### 5.5 根因五：Target Owner 没有生成运行目录身份

Workbench 不会仅凭 IPC 中的任意路径读取日志。日志绑定要求：

1. Owner 投影 `run_directory`；
2. 目录中存在 `DSV_RUN_IDENTITY/1`；
3. 身份文件的 `account_key`、`kernel_boot_id` 和绝对目录与当前快照一致。

Target Owner 原来既不投影运行目录，也不生成 `run-identity.json`。第一轮只增加路径后，安全校验仍正确拒绝绑定。因此必须补齐身份文件，而不是放宽路径检查。

## 6. 不是根因的部分

以下链路有直接反证，因此不应继续在这些方向盲查：

- **不是 SimNow 无行情：**四个合约最新价持续变化，报价时间和 source epoch 正常。
- **不是 Runtime 未同步：**Owner 为 `synchronized=true`，资金、账户权益和报价均已发布。
- **不是 IPC 整体失效：**资金、连接状态、最新价和命令回执都能到达 Workbench。
- **不是 Qt 定时刷新停止：**“最后报价”持续推进。
- **不是订单发送故障：**策略在发送前就被 `GEOMETRY_BLOCK` 阻止，账本 `orders={}`。

## 7. 修复方案

```text
TqSdk quote + 60s serial
        │
        ▼
DsvSdkAdapter
  ├─ 交易输入：原逻辑不变
  └─ 展示副本：最多 48 根合法 OHLC
        │
        ▼
DsvSession ROUND receipt
        │
        ▼
StandaloneDsvOwner
  └─ 保存当前 boot 最近决策摘要（仅展示）
        │
        ▼
workbench_view
  ├─ trigger_prices
  ├─ decision_time_utc
  ├─ entry_reasons
  ├─ minute_bars
  └─ 经身份文件证明的 run_directory
        │
        ▼
runtime-view / Workbench
  └─ K线、当前关注、策略解释、重要事件、运行日志
```

### 7.1 分钟 K 线

在 `DsvSdkAdapter.minute_chart_bars()` 中：

- 每个合约最多复制最后 48 根；
- 只接受合法的原生分钟时间戳；
- OHLC 必须满足 `0 < low <= min(open, close) <= max(open, close) <= high`；
- 单行异常只丢弃该行，不影响 Owner；
- 使用 2 秒有界缓存，避免 750ms Workbench 发布节奏反复转换 DataFrame；
- 断线或 Owner 异常时清空缓存，避免跨 source epoch 显示旧图。

### 7.2 最近决策摘要

`StandaloneDsvOwner` 从已提交的 ROUND receipt 中按合约保存：

- 最近决策时间；
- 本轮有序决策列表；
- 只在当前 Runtime boot 内存中存活。

该摘要不进入订单、持仓或保护权威，不参与策略判断，也不跨 boot 恢复。

### 7.3 触发线与状态原因

投影层使用与策略相同的纯函数，根据已经验证的 `day_high/day_low/open_offset` 计算两向触发线。实际交易决策仍由 Owner 原执行路径完成，Workbench 不重评是否下单。

最新决策原因进入 `entry_reasons`，因此：

- 主表状态能显示“日内振幅超出开仓上限”；
- 策略解释卡能显示最近判断时间；
- 重要事件能记录真实阻挡原因；
- “当前关注”即使在阻挡状态下也显示最近触发线，但状态栏仍明确标记阻挡，不误导为可下单。

### 7.4 日志身份绑定

Target Owner 启动时原子创建 `run-identity.json`，内容绑定：

- schema `DSV_RUN_IDENTITY/1`；
- `account_key`；
- 当前 `kernel_boot_id`；
- `scope_hash`；
- 绝对 `run_directory`。

只有身份文件创建成功时才把 `run_directory` 放入 Workbench 快照。Workbench 原有的身份与路径检查保持不变。

## 8. 改动文件

| 文件 | 作用 |
| --- | --- |
| `src/online_trader/dsv_runtime/sdk_adapter.py` | 输出有界、校验后的原生分钟 OHLC，管理缓存生命周期 |
| `src/online_trader/dsv_runtime/standalone_owner.py` | 保存当前 boot 最近决策摘要及已证明运行目录 |
| `src/online_trader/dsv_runtime/standalone_projection.py` | 投影触发线、决策时间、原因、K线和运行目录 |
| `src/online_trader/desktop/workbench_layout.py` | 阻挡状态下仍显示最近触发线 |
| `src/online_trader/runtime/target_owner_startup.py` | 创建当前 boot 的 `run-identity.json` |

改动共 5 个源码文件，约 90 行新增、3 行修改/删除。没有修改交易决策公式、订单控制器、发送边界、保护责任或账本 schema。

## 9. 验证结果

### 9.1 编译检查

执行：

```powershell
.venv\Scripts\python.exe -X utf8 -m compileall -q src tests qa
```

结果：PASS。

### 9.2 独立 GUI 复测

中间候选 `c2876e770bfb15b3d4cfb036e4a3f413775459dea5774e978c46b6e4c069ae29` 已实际验证：

| 验收项 | 结果 | 证据摘要 |
| --- | --- | --- |
| 四合约最新价 | PASS | 4/4 行情新鲜 |
| 当前关注 | PASS | 四合约均显示最近触发线 |
| 多头/空头触发 | PASS | 策略卡均有数值 |
| 最近判断 | PASS | 显示当前轮判断时间 |
| 原生 1 分钟 K 线 | PASS | 四合约切换后均有实际蜡烛，不再显示等待 |
| `GEOMETRY_BLOCK` 主表状态 | PASS | 短暂 ARM 后四合约均显示“日内振幅超出开仓上限” |
| 重要事件中的阻挡原因 | PASS | 同一 boot 出现四条真实几何阻挡事件 |
| 停止后安全状态 | PASS | F6 回执成功，`DISARMED`、持仓 0、委托 0 |
| 日志来源绑定 | FAIL（该中间候选） | 缺少 `run-identity.json` |

最终候选 `a0c7c857d8695b3a33db2fdb95329221786209aca613d97d1b2ba778fdc78021` 的只读证据：

- Runtime boot：`runtime-5c7c2968a765445dab957616a23a3477`；
- `target-owner-status.json`：`READY / DISARMED`；
- `run-identity.json` 已存在，且 `account_key`、boot、scope 与绝对运行目录一致；
- 新临时 ledger：`online-trader-runtime-session-dkr2erfm/ledger.db`，未复用旧 boot。

最终候选的 GUI 日志绑定确认在窗口出现后被实体 `Esc` 中断，因此必须标为 **NOT_RUN**，不能沿用中间候选结果宣称通过。重新附着后只需做只读确认，不需要再次 ARM。

### 9.3 计数

中间候选 `c287…ae29` 的独立 GUI 场景：

- PASS：8
- FAIL：1（日志来源未绑定）
- SKIP：0
- NOT_RUN：0

最终候选 `a0c7…8021`：

- 只读证据 PASS：3（身份文件、Owner `READY / DISARMED`、新 boot 临时 ledger）
- GUI PASS/FAIL：未形成结论
- NOT_RUN：1（GUI 日志来源绑定；实体 `Esc` 中断）

中间候选的 8 个 PASS 没有改写成最终候选的新执行结果。

这不是完整产品回归。没有运行全量源码测试，也没有构建或验收新制品。

## 10. 伴随发现

### 10.1 参数保存覆盖失败

Workbench“保存参数”选择已有 `OnlineTrader-parameters.json` 后返回 `E_DUPLICATE_CONFLICT`。该功能实际采用“新文件独占导出”语义，不能覆盖当前配置，但界面流程让用户自然选择已有文件。此次通过隔离副本直接修改配置继续测试，原始 ZIP 未改变。

该问题与行情明细空白不是同一根因，建议单独修复参数应用/导出交互。

### 10.2 Runtime 缺少正常停止入口

关闭 Workbench 不会停止 Runtime；界面没有明确的 Runtime 停止入口。旧 Owner 忽略 Ctrl+C/Ctrl+Break，且推导的 DSV shutdown 端点并非 Standalone 实际协议，最终只能在确认 `DISARMED / flat / 0 委托` 后精确终止进程。

该问题延误了本次测试，也应按 `desktop-lifecycle-user-flow-20261120.md` 单独处理。

### 10.3 调参方法错误

本次把偏移统一降到 2 的方法没有先检查几何公式，导致四品种必然阻挡。后续为了促进自然成交，应先按当前日内振幅和止损计算必要下限：

```text
open_offset > (day_high − day_low + stop_offset) / 2
```

然后再核对触发线位置、跳空/追价规则、风险手数和交易时段。不能只根据“偏移更小、触发更近”的直觉修改。

## 11. 防复发建议

1. 将“最新价存在但决策字段为空”列为 Standalone Workbench 发布合同的必测项。
2. 对每个受控合约同时检查：报价、分钟身份、OHLC、触发线、最近决策和阻挡原因，不以绿色连接灯替代业务数据完整性。
3. Workbench 对缺失字段区分“源未就绪”和“投影未提供”，避免统一显示 `—`。
4. 参数调整前先计算规则派生值，特别是几何上限、触发价、激活价和风险仓位。
5. Runtime 启动即生成带 boot 身份的日志目录证明；日志路径不能依赖 GUI 猜测，也不能放宽身份校验。
6. 补充明确、可审计的“退出 OnlineTrader（含 Runtime）”入口，避免关闭窗口后 Owner 继续占锁。
7. 参数页面区分“导出新文件”“覆盖配置”“应用到下次 boot”，不要用一个“保存参数”动作混合三种语义。

## 12. 后续动作

按优先级：

1. 重新附着最终候选，完成一次只读 GUI 日志绑定确认；确认后保持 `DISARMED`。
2. 用户若要求继续真实成交测试，先按实时日内振幅反算合理 `open_offset`，再通过正式配置流程启动新 boot。
3. 单独处理 `E_DUPLICATE_CONFLICT` 参数交互和 Runtime 正常退出入口。
4. 用户明确授权后，再执行受影响测试或完整回归。
5. 用户明确要求打包后，才进行全局独立测试、构建以及最终制品独立验收。

## 13. 补充溯源、同类问题修复与定向单测（2026-11-23 12:14）

### 13.1 Git 提交溯源结论

问题并非单次提交改坏旧逻辑，而是“Target Owner 冷切换遗漏迁移”叠加“旧版 UI 窄条件限制”：

1. **直接缺口引入（`d19cf74`，2026-11-21 12:51:37 +0800，`feat(runtime): add target execution owner candidate`）**：新增 Target Owner 路径时，未把 Legacy Kernel（`kernel/workbench.py`、`gateway/tqkq_dsv.py`、`runtime/dsv_simulation.py`）原有的 `minute_bars`、最近决策摘要/`trigger_prices`/`entry_reasons`、扩展行情/保护/成交时间字段以及 `run-identity.json` 迁移到 `standalone_projection.py`、`standalone_owner.py`、`sdk_adapter.py` 和 `target_owner_startup.py`。
2. **默认路径生效（`d2cd048`，2026-11-21 13:45:05 +0800，`feat(runtime): cold switch standalone to target owner`）**：`desktop_bootstrap.py` 将 `_STANDALONE_EXECUTION_CORE` 从 `LEGACY_KERNEL` 切换为 `DSV_TARGET`，Standalone 默认入口与配置包开始全部受影响。后续 `3a3ce67`–`ebf4c4a` 仅补了止损/止盈价、外部持仓、权限与 `checkpoint_event_seq`，未补齐这组展示和身份字段。
3. **UI 窄条件叠加（`01ecdda`，2026-11-17 15:01:54 +0800，`Redesign the workbench and add live offset and tray workflows`）**：`workbench_layout.py` 将“当前关注”限制为 `status == '等待触发'`，导致阻挡态即使有触发线也会隐藏为 `—`。

### 13.2 同类遗漏排查与补充修复

基于 11-21 至 11-23 提交排查并补齐了以下同类投影/日志缺口：

| 文件 | 补充修复内容 |
| --- | --- |
| `src/online_trader/dsv_runtime/standalone_projection.py` | 修正 `markets[].enabled` 不再混入 `running` 与 `day_stop`（避免 `DISARMED`/停开时误显示“已停用”），新增 `day_stop_reason`、`margin`、`volume_multiple`、`upper_limit`、`lower_limit` 及盘口扩展/换月信息；补齐 `cycles[]` 保护/确认/半仓/退出等待字段、`orders[]` 的 `source_time_ns`/`native_message`、`configuration.schedule_display`/`clock_health`/`strategy_diagnostics` 及 `contract_catalog`；容量截断时移除 `minute_bars` |
| `src/online_trader/dsv_runtime/sdk_adapter.py` | `_order_facts()` 采集 `order.last_msg` 为 `native_message`；新增只读 `display_market_details()` 读取扩展盘口与 `rollover` |
| `src/online_trader/dsv_runtime/controller.py` | `_order_update()` 保留 `native_message`；`_position_fill()` 保留可选 `native_trade_id` 与 `source_time_ns` |
| `src/online_trader/runtime/target_owner_startup.py` | 绑定 `session.runtime_identity` 并写入 `runtime-identity.json`；为 `target-owner-status.json` 与 `target-owner-diagnostics.json` 补充 `observed_at_utc` |
| `src/online_trader/runtime/desktop_bootstrap.py` | 为 `phase-*-bootstrap.json` 与 `startup-failure.json` 补充 `observed_at_utc` |
| `src/online_trader/desktop/workbench_layout.py` | `log_paths()` / `executor_log()` 接入 `target-owner-status.json`、`target-owner-diagnostics.json` 与 `order-blocking-log.jsonl`；首页执行链提示识别 `DSV_TARGET_EXECUTION` |
| `src/online_trader/desktop/workbench.py` | 直连停机检测兼容 `target-owner-status.json` 的 `STOPPED` 状态 |

### 13.3 补充单测与静态检查（worktree，未运行全量/未打包）

- **新增单测（4 项，并同步注册到 `qa/test_manifest.json`）**：
  - `tests/test_target_owner_runtime.py::TargetOwnerWorkbenchProjectionTests`（3 项）：
    1. `test_workbench_view_projects_market_triggers_reasons_day_stop_and_minute_bars`
    2. `test_workbench_view_projects_cycle_protection_order_and_fill_timestamps`
    3. `test_standalone_owner_retains_latest_decisions_and_run_directory`
  - `tests/test_dsv_workbench_parity.py::DsvWorkbenchParityTests`（1 项）：
    4. `test_target_owner_workbench_displays_focus_under_geometry_block_and_reads_target_logs`
- **定向验证命令与结果（在当前 `WORKTREE` 上执行）**：
  - `.venv\Scripts\python.exe -X utf8 -m unittest -v tests.test_target_owner_runtime.TargetOwnerWorkbenchProjectionTests tests.test_dsv_workbench_parity.DsvWorkbenchParityTests.test_target_owner_workbench_displays_focus_under_geometry_block_and_reads_target_logs`（`PYTHONPATH=src`）：**PASS 4 / FAIL 0 / SKIP 0 / NOT_RUN 0（定向 4 项）**
  - `.venv\Scripts\python.exe -X utf8 qa\test_registry.py`：**PASS**（`manifest valid: 1939 cases in 133 modules`）
  - `.venv\Scripts\python.exe -X utf8 -m compileall -q src tests qa`：**PASS**
- **明确未执行范围**：按本次用户指令，**NOT_RUN**：`FullOffline` / 全量 `unittest discover` 回归、重新打包构建以及新候选的 GUI 实盘/只读复测。

## 14. 后续同日修正与验证收口（2026-11-23 14:43）

在工作树上继续完成以下修复与重构（版本由 `0.1.0.dev37` 依次推进至 `0.1.0.dev39`）：

1. **启动 `trusted_control_pid` 类型修复（`src/online_trader/runtime/target_owner_startup.py`）**：
   - 修正 `standalone.py` 传入 `process.trusted_pid` 绑定方法导致 `trusted_control_pid > 0` 抛 `TypeError` 的问题；新增 `_resolve_trusted_pid()` 用于构建 `runtime_identity.entry_point`，同时保留将可调用对象原样传给 `ControlServer`，维持对 `runtime-view` 重启后的动态 PID 校验。
2. **投影边界与离线适配器兼容（`standalone_owner.py`、`standalone_projection.py`）**：
   - `StandaloneDsvOwner.step()` 在 `adapter.contracts` 缺失时回退到 `self.profile` 合约集合，兼容 `OfflineAdapter`；`standalone_projection.py` 在已有 `EXTERNAL_POSITION_PRESENT` 时去除重复的 `UNOWNED_POSITION`。
3. **Target Owner 运行中热保存（`SAVE_DSV_PARAMETERS`）**：
   - 打通 `standalone_commands.py`、`runner.py`、`sdk_adapter.py`、`standalone_owner.py`、`standalone_projection.py`、`desktop_bootstrap.py`、`target_owner_startup.py`、`operator_api.py`：开放 `live_parameter_save=True` 与 `SAVE_DSV_PARAMETERS` 能力，在 `Journal` 事务内原子提交 `DSV_PARAMETERS_SAVED`、递增 `config_revision`，并通过 `configured_store.save_profile` 持久化至 `OnlineTrader-overrides.json`；保存前已持仓位继续冻结开仓时的保护规则（`_position_configs`）。
4. **`0.1.0.dev39` 离线回归与便携构建**：
   - 全量 `unittest discover -s tests -v`（非沙箱本机，`PYTHONPATH=src`）：**1941 PASS / 0 FAIL / 0 SKIP**；基于隔离测试配置完成 `ReleaseClean` 构建（`tree_sha256=bf739614d3a97dd073cb2ef02bedd0f09907c914e6555903765610c9dea7581a`，`ZIP SHA-256=b107a6443b49d82593219ed500f9c59c1721ff9198a33c8a5b2d36d407a000a2`）。
5. **退出面板后受控安全全进程退出（post-`dev39` 工作树）**：
   - 新增 `src/online_trader/runtime/shutdown.py`，并在 `desktop/workbench.py`、`desktop/tray.py`、`dsv_runtime/runner.py`、`dsv_runtime/standalone_projection.py` 接入：平时点窗口 `X` 仍收起到托盘；显式点托盘「退出 OnlineTrader」（或关闭托盘选项后点 `X`）时，空仓自动触发 `FLATTEN_THEN_STOP`，有持仓/未完结委托/故障时弹窗确认（默认取消保留保护），并经 Win32 内核句柄与 `runtime_owner_exists()` 确认 Owner 与 `runtime-view` 完全退出后才退出 Qt。
   - 最终代码改动后的静态校验与定向回归：`compileall -q src tests qa` 通过，`qa/test_registry.py` 校验 `1945` 条用例 / `133` 模块有效，`tests.test_workbench_tray` + `tests.test_workbench_processes` + `tests.test_target_owner_runtime` + `tests.test_dsv_workbench_parity` 共 **56 PASS / 0 FAIL / 0 SKIP**；在受控全进程退出改动之后，全量 `1945` 条回归与新便携包重打包均为 **NOT_RUN**。

