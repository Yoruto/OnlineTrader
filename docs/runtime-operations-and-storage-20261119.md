# Trading Runtime 运维、配置与账本约定

> **2026-11-20 规则变更：** 本文关于“原执行账本长期权威、换机交接、handoff、跨重启 pending”的内容已被[本机单实例与柜台权威设计](refactor/runtime-broker-authority-single-instance-20261120.md)替代。当前 Standalone Runtime 每次启动使用临时执行库；同一台电脑只有一个 Runtime，不限制其他电脑使用同一期货账户。长期保留仅包括版本默认配置、用户覆盖配置和可由柜台重建的历史/收益缓存。下文未改写部分仅作为旧候选的历史设计记录，不得用于阻止当前启动。

2026-11-19 · 独立 Trading Runtime 运维说明 · 依赖实现候选 PR #17

本文说明独立 Trading Runtime 的运行目录、自动启动、进程/线程职责、SQLite 长期账本、WAL/SHM、接口进程故障和验收方法。它补充[独立运行时设计](refactor/standalone-trading-runtime-20261119.md)与[账号数据同步设计](account-data-sync-design-20261119.md)，不把候选源码、模拟柜台或最终制品描述成已经发布验收。

## 默认产品入口

后续未明确指定路径的 OnlineTrader 启动、重启、验收候选和打包均默认指 Standalone Trading Runtime。普通用户从 `online-trader-desktop --runtime-root <目录>` 进入；Runtime 配置便携版直接双击 `OnlineTrader.exe`。每台 Windows 电脑只有一个 Runtime 和一个附着它的 Workbench：统一入口先唤醒已有 Workbench；若只有 Runtime，则验证并附着它；若 Runtime 也不存在，才启动 `DISARMED` Owner，取得权威投影后打开与 dev 共用的 `WorkbenchWindow` / `WorkbenchLayout`。`runtime run`、受 `runtime-control.json` 约束的 `runtime schedule` 与 `--runtime-pipe` 保留为运维、无人值守及高级附着入口，但高级附着也不能创建第二个 Workbench。普通 Legacy 工作台不作为默认启动或交付入口，只在用户明确要求旧路径维护时使用。

该默认只决定目标架构，不扩大当次权限：账号连接、测试、订单操作和打包仍分别需要用户授权。打包时交付并验收 Runtime 完整目录/ZIP，配置、操作员授权与已初始化账户原账本均按接收方核对；禁止用能够打开但缺少 Runtime 配置或账本的普通工作台包替代。

## 1. 运行目录由配置和原执行账本共同组成

对已经启动过的账户，**配置和原每账户 SQLite 执行账本缺一不可**。配置描述“下一次怎样运行”，账本保存“已经发生并需要恢复、核对或审计的事实”。不新增第二个 Runtime 数据库。

典型目录：

```text
runtime/
├─ runtime.json
├─ runtime-control.json
├─ OnlineTrader-parameters.json
├─ private/
│  ├─ OnlineTrader-defaults.json
│  └─ operators.json
├─ data/
│  └─ .../<account>/ledger.db
└─ logs/
   ├─ runtime-view.log
   └─ runtime-view.log.1
```

| 数据 | 权威来源 | 用途 |
| --- | --- | --- |
| 账户选择、别名、provider、线路 | `runtime.json` | 选择规范账户并派生原账本位置。 |
| 是否允许无人值守定时启动 | `runtime-control.json` | 运行级自动化意图；可热读取，不是交易历史。 |
| 日/夜盘启动时间、`auto_start_on_launch`、策略参数 | `OnlineTrader-parameters.json` | 新启动配置、预览和明确选择的新参数来源。 |
| 券商/行情登录资料 | `private/OnlineTrader-defaults.json` | 私有登录输入；不能写入公开日志或证据。 |
| CLI、定时任务和受托工具的令牌摘要、账户和操作授权 | `private/operators.json` | 非界面控制接口认证；Workbench 不读取令牌，使用受 Windows 本机身份边界约束的独立通道。该边界不证明真实按钮点击。 |
| 委托、成交、持仓责任、未知发送、保护、已提交命令、账户观察、已生效配置和策略事实 | 原每账户 `ledger.db` | 恢复、审计、历史查询和发送前持久化。 |
| UI/接口进程诊断 | `logs/` | 可删除/轮转的诊断，不是交易事实。 |

首次从未启动的账户可以只有配置，由运行时取得账户锁后创建原账本。已经存在 `initialized.json`、handoff 清单或既有账本身份时，缺失 `ledger.db` 必须报错，不能把旧账户误判为新账户。

换机交接携带配置和原账本的一致性副本。不要只复制配置，也不要只复制数据库后猜测账户和参数。

## 2. 定时启动器

无人值守入口：

```powershell
OnlineTraderCLI.exe runtime schedule --root .\runtime --operator <operator>
```

运行级总开关：

```json
{
  "schema_version": "ONLINE_TRADER_RUNTIME_CONTROL/1",
  "scheduled_start": {
    "enabled": false
  }
}
```

职责分层：

- `runtime-control.json` 的 `scheduled_start.enabled` 决定**是否允许新的自动启动**。
- `OnlineTrader-parameters.json` 继续决定日盘/夜盘触发时间和 `auto_start_on_launch`。
- 调度进程等待期间每秒重新读取两份配置；命中触发点后，在创建交易请求前再读取一次。
- 配置缺失、格式错误或触发条件在最后一次核对时已改变，均按 fail-closed 处理，不启动新的策略 owner。
- 把 `scheduled_start.enabled` 从 `true` 改为 `false` 只阻止之后的新启动，**不会停止已经运行的 Trading Runtime，也不会删除已有持仓保护责任**。
- 同一账户、同一柜台日和同一触发身份继续使用确定性的 request ID 去重。

新交付建议把 `scheduled_start.enabled` 初始设置为 `false`；确认目标电脑、账户、参数、操作员授权和开机启动方式后再显式开启。

换机交接导出时，如果源目录尚无 `runtime-control.json`，导出入口会在源目录原子创建上述 `enabled=false` 的默认配置并随交接清单复制；已有文件仍按原内容严格校验，格式非法时停止交接，不覆盖或猜测用户配置。

## 3. 交易 Owner 与辅助线程/进程

### 3.1 必须保留在交易 Owner 的工作

Trading Runtime 的 Owner 主线程仍是唯一的交易决策和 SDK 驱动线程。以下工作不得因为“减负”被随意迁移成并发 sender：

- TqSdk 更新推进和柜台连接状态；
- 时钟、柜台日、行情新鲜度和 source epoch 判断；
- DSV 策略计算和最终开仓门禁；
- OMS、报单、撤单、替换、部分成交和结果核对；
- 止损、止盈、收盘退出及已有持仓保护；
- 最终操作员命令授权；
- 关键 SQLite 提交 ACK 与 commit-before-send 顺序。

一个账户始终只有一个交易 owner 和一个原账本 writer。Qt、runtime-view、历史查询和编码线程都不能取得 SDK 或发送权。

### 3.2 可以异步或降频的读侧工作

候选实现把展示相关工作进一步限制在不影响交易语义的范围：

- 策略最新展示决定使用**已提交后的内存缓存**，避免每次 Workbench 发布都扫描 `strategy_proposals`；重启时仍从原账本恢复。
- Workbench/行情投影目标周期约 750 ms。
- 完整账户截面（资金、持仓、全部原生委托和成交）的采集目标周期约 2.5 s。
- 大帧编码在后台线程完成；已有编码任务积压时不在 Owner 线程不断重复构造新帧。
- runtime-view-process 负责对外状态、历史、账户读取和操作员接口，可退出并重建。

Trading Workbench 通过 runtime-view-process 的本机控制通道提交命令，不在 Qt 中输入操作员 ID 或令牌。命名管道核验连接进程的 Windows 身份；当前服务端边界包含 Owner SID 与 `LocalSystem`，但这种检查不能证明命令来自真实用户的按钮点击，也不能把 Capability 当成授权票据。接口和原 Runtime Owner 仍逐次核对显式 Workbench allowlist、账号、能力、owner boot、配置版本及交易前置条件。命令日志保留脱敏账号、actor、boot 与 peer PID/SID。超时或断线保留原 `command_id`，只能查询原回执，不能由界面自动生成新 ID 重发；同机唯一 Workbench 在当前进程内保留待核 ID，Workbench 退出后必须重新从 Runtime 与柜台事实同步。CLI、定时任务和受托工具仍走操作员协议。Qt 和 runtime-view-process 仍只是控制/读侧，不因此成为 SDK sender 或账本 writer。详细威胁模型与迁移门禁见[架构收敛与安全改造计划](refactor/runtime-architecture-hardening-plan-20261120.md)。

750 ms 和 2.5 s 是当前实现的发布节奏，不是交易 SLA，也不改变报单、撤单、策略或保护轮次。性能评审应观察 `OWNER_LOOP_SLOW`、SDK 泵间隔以及 Tick → 决策 → 发送的 P95/P99。

## 4. SQLite、WAL、SHM 和冷账本

OnlineTrader 的长期交易事实仍保存在原 `ledger.db`。运行 writer 使用 SQLite WAL 模式和 `synchronous=FULL`。

### 4.1 三个文件的含义

```text
ledger.db       主数据库
ledger.db-wal   已提交但可能尚未 checkpoint 回主数据库的 WAL 页面
ledger.db-shm   WAL index / 多连接协调文件
```

`ledger.db-wal` 不是可以随手删除的临时垃圾。它可能包含已经 COMMIT 的真实委托、成交、命令或保护事实。运行中、异常退出后或证据复制时，**禁止手工删除 WAL**。

`ledger.db-shm` 通常可以由 SQLite 重建，但运行中也不要手工删除；让 SQLite 自己管理它。

### 4.2 热账本

存在执行 owner 或数据库仍有 WAL/SHM 协调状态时，使用普通 SQLite 只读 WAL 连接取得一致性快照。读侧不能用 `immutable=1` 绕过 WAL，因为这样可能看不到已经提交但尚未 checkpoint 的事实。

### 4.3 冷账本

本文所称“冷账本”是没有执行 owner、没有 writer 的停机账本。

- **无 owner、无 WAL/SHM**：在持有账户启动锁时可用 `mode=ro&immutable=1`，避免只读查询生成新的协调文件。
- **无 owner、但仍有 WAL/SHM**：继续用标准 `mode=ro`，让 SQLite 合并主库和 WAL 的已提交视图。
- 冷历史 reader 之间先经过独立 reader gate 串行化，再判断执行 owner，避免“另一个历史 reader”被误认为“交易 owner”。
- 历史 reader 的排队时间不计入 SQL 自身的 5 秒查询预算。

这套约定的目标是同时满足两件事：**不能漏掉 WAL 中已经提交的事实，也不能让纯冷查询无必要地污染交接目录**。

## 5. 备份、交接和恢复

- 活跃 owner 的原始文件复制不是可直接启动的恢复镜像。需要备份时使用 SQLite 一致性 backup 或现有 handoff 流程。
- `runtime export-handoff` 要求源 owner 已完成相应停机/结清核对，并导出配置、原账本副本和清单摘要。
- 目标机先校验账户/provider/账本身份和文件摘要，再由原执行入口重新核对当前柜台事实。
- 查询副本没有交易权；源 owner 未停止时，不能因为有一份数据库副本就启动第二个 sender。
- 未观察日期、柜台完整历史或外部成交缺口不能由本地账本猜测补造。

## 6. runtime-view-process 故障边界

runtime-view-process 是可重建的本机接口进程，不是交易 owner。它退出时 Trading Runtime 仍继续处理行情、订单和保护。

候选实现把其 stdout/stderr、启动和退出记录到：

```text
runtime/logs/runtime-view.log
runtime/logs/runtime-view.log.1
```

日志记录 START、EXIT、退出码、短时运行时长和启动失败类型。连续快速失败采用有上限的退避，避免 0.5 秒无限重启风暴。

排障顺序：

1. 先确认 Trading Runtime owner 是否仍在、柜台是否同步、持仓保护是否继续。
2. 再查看 `runtime-view.log` 是否有 traceback、Named Pipe、ACL/SID 或初始化错误。
3. 接口恢复后重新取得基线，不把旧 Qt 画面当成当前交易事实。
4. 不通过杀掉 Trading Runtime 来“修复界面”。

## 7. 变更和验收检查

### 配置与定时启动

- `runtime-control.json` 缺失或非法时不自动启动。
- `enabled=true` 时命中当前参数里的合法日/夜盘触发。
- scheduler 运行期间把 `enabled` 改为 `false`，无需重启即可阻止新启动。
- scheduler 运行期间修改策略触发时间，最后一次触发前核对使用新配置。
- 已经运行的 owner 不因关闭定时总开关被停止。

### Owner 主线程负载

- 对比变更前后的 `OWNER_LOOP_SLOW` 数量和持续时间。
- 记录 SDK update 间隔、Tick 到策略判断、策略判断到订单发送的 P50/P95/P99。
- 大量委托/成交历史和慢 Workbench 客户端不能形成不断增长的发布积压。
- 读侧故障不能改变订单、UNKNOWN 占量或保护状态。

### SQLite 和历史查询

- 单 reader 冷账本查询前后文件集合及原文件 SHA 不变。
- 两个及以上 reader 并发读取冷账本时，后续 reader 等待而不是错误降级成“活跃 owner”。
- 带已提交 WAL 的冷账本能够读到 WAL 内事件。
- 活跃 owner 追加时，旧游标保持原水位，新查询可见新水位。
- 任何验证都不得通过删除 `-wal`、清库或重建账本来获得通过。

### 接口进程

- 强制结束 runtime-view-process 后，Trading Runtime 继续交易责任，接口按退避规则重建。
- 日志能看到退出码/异常，但日志写失败本身不阻断交易 owner。
- Qt 重连取得新基线，不能用旧窗口状态推断已经平仓或已撤单。

### Runtime 测试入口

- 后续用户级产品测试从统一 `online-trader-desktop --runtime-root`/配置版 `OnlineTrader.exe` 入口执行；Runtime CLI/API、`RuntimeViewClient`、`RuntimeCommandClient` 和 `--runtime-pipe` 用于对应的运维或接口范围。
- 旧工作台或 QA 直接持有 Kernel/ControlServer 的回放只保留为 Legacy 或组件不变量测试，不能作为 Runtime 产品验收。
- 纯离线事实只进入 `qa/` 中不随 wheel 发布的宿主，生产 `runtime run` 不增加 QA 注入开关。
- 固定边界、退役入口和离线证据范围见[测试入口迁移](refactor/standalone-trading-runtime-test-migration-20261119.md)。

## 8. 发布边界

本说明描述源码合同，不等于最终制品验收。正式放行仍分别需要：

- Windows x64 最终候选源码检查；
- 实际 Named Pipe / runtime-view 重启场景；
- 获准 SimNow 的无人值守启动和交易闭环；
- Owner 主线程延迟对比；
- 冷热 WAL 与并发历史读取；
- 最终便携目录/ZIP 的配置、数据库和摘要检查；
- 干净目标电脑按用户实际入口启动。

源码层快速 Runtime 定向测试不能替代上述联网、可见 Qt、故障恢复或最终制品验收。每次报告都要分别列出已经执行和 `NOT_RUN` 的范围。

验收记录应注明完整候选 SHA、配置摘要（不含秘密）、环境、实际命令、PASS/FAIL/SKIP/NOT_RUN 和制品 SHA-256。
