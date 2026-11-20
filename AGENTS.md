# OnlineTrader development

Independent Windows product. G0 source specifications are recorded in docs/spec-source.json.
Preserve existing work; do not import V7, vnpy, DSV, their credentials, databases or environments.

## 默认启动与交付路径

- 未明确指定执行路径时，OnlineTrader 的启动、重启、用户验收、测试候选和打包默认采用 **Standalone Trading Runtime**。普通用户从 `online-trader-desktop --runtime-root <目录>`（配置便携版直接双击 `OnlineTrader.exe`）进入；统一桌面入口先附着已有 Runtime，缺失时以 `DISARMED` 启动 Owner，再打开同一个 `WorkbenchWindow` / `WorkbenchLayout`。`runtime run`、`runtime schedule` 和 `--runtime-pipe` 保留为运维、无人值守及高级附着入口；不要另建或交付第二套界面。
- 默认 Runtime 路径不授予账号连接、订单操作、测试或打包权限；这些动作仍按本次用户授权执行。用户明确要求 Legacy 维护、replacement candidate 或 isolated QA 时，才切换相应路径，并在报告中标明。
- 默认打包目标是可实际运行的 Runtime 完整交付单元，不是仅含普通工作台的 Legacy 包。除通用便携依赖外，按接收方带齐 Runtime 配置和操作员授权；执行账本不属于交付状态，历史缓存可由期货账户重新下载。从最终 ZIP/安装器按双击 `OnlineTrader.exe` 的统一 Runtime 入口独立验收，并另外保留 `runtime run`/`runtime schedule` 与 `--runtime-pipe` 运维入口核对。

## 所有软件制品必须随包交付配置

- OnlineTrader 每次出包都必须带齐本次交付所需的配置。安装器、便携包、更新 ZIP、客户专用包和测试包均适用。打包前确认目标设备或接收方使用的配置来源和文件清单。需要预配账户和交易参数时，必须随包带入已确认的 `OnlineTrader-defaults.json` 和 `OnlineTrader-parameters.json`，以及该包所需的启动和更新配置。独立 Trading Runtime 的新交付还必须显式带入 `runtime.json`、`runtime-control.json` 和相应的 `private/operators.json`；不要用缺少自动启动总开关的目录猜测默认行为。
- 禁止将缺少交付配置的通用包、空配置包或占位配置包作为制品交付。客户包使用为该客户或设备确认的配置。测试包使用能完成指定场景的隔离测试配置。配置缺失、归属不明，或分发渠道无法承载经授权的配置时，停止出包或发布并说明原因。含账户口令的制品只通过获准的私有渠道交付。
- 正式交付的安装器、便携包和更新 ZIP 禁止随包配置白银。检查品种列表、交易参数、默认策略和自动加载的预设，排除白银主连及上期所全部 `ag` 合约月。发现任何白银配置项时停止交付。
- 交付前检查最终制品本身。对 ZIP 检查实际归档内容，对安装器在隔离目录安装验证，核对配置文件的路径、有效内容及其与已确认输入的一致性。使用用户实际启动入口验证配置可读取。交付记录写明版本、制品 SHA-256 和配置验证结果，省略口令等秘密。任一检查缺失或失败，该制品不能交付。

## 完整制品与关键流程评审

- 便携版以包含 EXE、`_internal`、必需脚本和配置的完整 ZIP 为交付单元。交付链接指向已验收的 ZIP 或安装器。承诺单文件交付时，独立测试只复制该文件到干净的目标环境，按用户实际方式验证。构建和验收记录均关联最终制品 SHA-256。发现缺依赖或配置时停止交付，替换件须复跑原接收场景。
- 改动执行入口、订单与账本、时钟与行情判定、换机恢复或安装更新等跨模块行为时，先定位实际入口的第一处差异。实现前在现有设计中评审流程图、时序图、状态归属、兼容与回退及异常验收条件。
- 开发时加入关联得到的诊断证据。关键行情拒绝须能追出实际失败的年龄、时段或盘口条件及其输入依据。订单链路须能区分触发、发送前中止、实际发送、回执、成交与保护。独立验收用代表性故障验证日志能还原原因。

For DSV parity, start with the [current refactor index](docs/refactor/dsv-parity-20261114/README.md)
and [the connected strategy and SDK replay](docs/refactor/dsv-parity-20261114/15-strategy-sdk-schedule.md).
Verify the frozen archive and run the original code in isolated QA when it can be reused.
Feed both implementations the same configuration, ticks, position snapshots and ordered facts.
Trace the first differing round through `wait_update`, strategy target, SDK API call and outbound packet.
Keep `online_trader.dsv_core` free of the legacy owner, SDK, UI, I/O and system clock.
Keep the explicit controller's per-child pre-send durability and the existing cutover gates.

## Select the execution path before changing it

| Path | Entry points and owner | Applicable contract |
| --- | --- | --- |
| Legacy, not yet retired; explicit maintenance only | `simulation run`, `dsv run`, `simnow run`, and the ordinary workbench's existing launch route | Preserve the original Kernel owner and that route's recovery contract until approved cutover. Do not select this as the default launch or package path. |
| Standalone Trading Runtime; default | `online-trader-desktop --runtime-root`, packaged `OnlineTrader.exe`, `runtime run`, `runtime schedule`, `runtime command`, attached status/account/history and runtime Workbench | One Windows host has at most one Runtime; other hosts are not mutually excluded, including when they use the same futures account. Each boot uses a process-owned temporary Kernel/SQLite execution store and synchronizes current facts from the broker. Qt and runtime-view are read/control-side processes. Follow [the current authority design](docs/refactor/runtime-broker-authority-single-instance-20261120.md). |
| Replacement candidate | `dsv replacement-run`, `simnow replacement-run` | `DsvSession` + `ExplicitOrderController` + `Journal` + `DsvSdkAdapter`; share the compatible account lock, not the legacy Kernel sender. See [candidate startup](docs/refactor/dsv-parity-20261114/10-sdk-startup-and-same-day-rollback.md). |
| Isolated QA | `offline-dsv` and `qa/dsv_parity/` replay tools | DEV/OFFLINE fixtures or locally bounded SDK replay; no real account connection or order authority. |

A shared helper or lock does not authorize two senders. Do not import legacy full-Tick-path gates,
epoch-switching or clock-fault exceptions into the replacement merely because the legacy route
uses them. Conversely, do not remove legacy checks while maintaining that route. Every changed
trading condition needs DSV evidence, a scoped user-approved difference, or a stated minimum
fact/ownership requirement. QA input injection must never become an online input port.

For slow partial fills, honor the approved behavior in
[the decision register](docs/refactor/dsv-parity-20261114/00-evidence-and-decisions.md#用户批准的行为差异).
In the fixed sample, keep the 5-lot opening target when 3 lots are proved, then hold the
5 proved lots after the remaining 2 fill. When testing an affected change within an authorized scope, include the fixed round 7 and 9 replay. This approval does not approve unrelated month-stop or restart deviations.

Current implementation and evidence are indexed in [README](README.md) and the
[96-item checklist](docs/功能实现与待办清单_20261110.md). The product has separate
TqKq and SimNow simulation paths and a pinned optional SDK environment. Use the
current user's authorization and current session scope for account connections,
order operations, service installation, CI or release work. B01-B20 follow the
user-confirmed DSV reference in docs/decisions/dsv-confirmation-20261109.json.
A saved reference or draft is NON_TRADING. Confirm unresolved DSV semantics from
source or observation evidence.

One owner controls state and one writer controls SQLite. Commit required facts before send;
unknown sends reserve capacity and require reconciliation. GUI/strategy lifetime cannot delete
protection. All exits share one remaining target; cancellation requests do not release quantity.

For the standalone Trading Runtime, the futures account is authoritative for positions, orders,
trades, funds and returns. Enforce one Runtime per Windows host with a machine-local lock, but do
not use local files or locks to exclude another computer using the same account. Each boot creates
a fresh process-owned execution ledger and synchronizes broker facts; it is deleted when that boot
ends and is never handed off. Persist only version defaults plus user configuration overrides and a
rebuildable broker-history/return cache. Existing old execution ledgers remain read-only historical
artifacts and never block startup. Do not move SDK/order/protection work out of the single owner.

External statement comparison is read only. Reviewed fee registration is confined to the existing
ONLINE_SIM/development ledger while the execution owner is closed, all owned cycles are flat/closed
and orders are quantity-consistent terminal facts. It appends statement fees and a durable receipt
in one original-writer transaction, never imports or rewrites trade prices, quantities or protection,
and leaves entry authority DISARMED. A CSV/export/preview alone is not a registration instruction.

Tests and QA are outside the production wheel. After routine changes, run compilation only.
Do not add, adjust, or run tests without an explicit request for testing or packaging.
Before authorized packaging, independently run global product tests, then independently
verify the final artifact after building. When full source regression is in scope, run
`.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v` with `PYTHONPATH=src`.
Use `.venv\Scripts\python.exe -X utf8 -m compileall -q src tests qa` for source compilation.
Follow the global independent-testing instructions for delivery acceptance and use the actual
entry point with isolated fixtures. Report offline, connected and packaged evidence by scope.
An acceptance statement must name the full candidate SHA, scenario/fixture, environment and
PASS/FAIL/SKIP/NOT_RUN counts. Unread local reports remain reported results, not independently
verified evidence. A documentation-only commit does not fix an open source defect.

Offline parameter work also includes the DSV 33-field trade-form templates and whole-batch symbol
CSV preview/import/export. Template application and CSV import return preview values only; they
never write trading authority, active configuration or existing protection plans. Template edits
require an explicit expected revision and append immutable local history outside the ledger.
All business-parameter editing and the versioned DSV offset calculator remain preview-only.
Use explicit offline daily-series identities and an explicit cutoff trading date; never infer
live market completeness, use today's unfinished bars as fallback, or activate trading from a
calculated/exported result. Calculations and applications bind the data and preview hashes.

Historical execution grants and evidence are archived in [the November 10–11 development record](docs/history/development-authorizations-20261110-11.md).

When modifying the legacy startup replay, commit historical path replay separately from the quote
used for execution. Preserve the durable observation start and validate native
sequence, session and source epoch before extending the path. Delayed incremental
updates use the bounded startup catch-up and the original freshness limit. These are legacy
maintenance instructions, not a new replacement entry gate. For replacement startup, follow its
bound SDK facts and documented synchronization contract. Reuse existing evidence when the task
only maintains documentation, and do not relabel old results as newly executed.
