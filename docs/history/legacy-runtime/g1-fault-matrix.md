# G1 故障验证与 G0 SM24 对照

日期：2026-11-09。适用范围：本工程隔离的 DEV 事件、SQLite、Windows 本地 IPC 和只读 Qt 状态台；没有连接柜台、安装服务或发出真实/模拟柜台委托。测试执行时B01–B20运行种子为PROPOSED；后续[DSV业务基准确认](../../b01-b20-recommendation-confirmation.md)未改变这些原始测试结果或运行审批。

本报告区分真实进程强杀、故障注入和可见窗口验证。`TerminateProcess` 证明指定进程被终止后的本机恢复行为；它不证明物理断电、操作系统崩溃、存储控制器缓存丢失或整机断网行为。模拟 `SQLITE_FULL` 证明事务错误处理，没有实际填满磁盘。

## 已执行的 11 个新增用例

`tests/test_faults.py` 的 9 例在 2026-11-09 05:37:19–05:37:23 UTC 的独立重复运行全部通过；`tests/test_visible_console.py` 的 2 例在 05:37:08–05:37:10 UTC 全部通过。此处记录这些已保留的原始结果；随后合入控制协议和增量流的完整工程检查结果，以 `qa/check.py` 的独立运行报告为准。

| 编号 | 测试方法（省略 `test_` 前缀） | 执行方式与断点 | 已断言的恢复/界面结果 |
| --- | --- | --- | --- |
| F01 | `durable_intent_before_owner_ack_becomes_unknown_without_replay` | 真实子进程；TX_INTENT 已提交，owner 尚未接收 Ack，发送次数 0 后强杀 | 保留 plan/child 身份；订单 UNKNOWN；原 60 额度 UNKNOWN 预占；新 boot DISARMED、要求核对；没有重发 |
| F02 | `actual_send_call_without_saved_return_remains_unknown` | 真实子进程；SEND_POSSIBLE 已提交，进入 RecordingPort.send，尚未保存返回时强杀 | 原尝试仅 1 个，保持 UNKNOWN，无伪造成交；重开没有重发。RecordingPort 只记录调用，不撮合、不连接柜台 |
| F03 | `uncommitted_protection_latch_is_not_claimed_durable_after_kill` | 真实子进程；保护触及的 SQLite 事务已执行 mutation、尚未 COMMIT 时强杀 | 未提交锁存回滚为 NORMAL；已提交持仓 1 保留；重启要求核对；没有把丢失的观察尾部声称为 durable 保护 |
| F04 | `committed_latch_survives_kill_before_owner_accepts_ack` | 真实子进程；保护 COMMIT 已成功、owner 尚未接收 Ack 时强杀 | PROTECTED 和高水位 80 保留；原身份保留；新 boot 无发送 |
| F05 | `switch_before_commit_rolls_back_both_strategy_updates` | 真实子进程；新旧策略切换处于未提交事务时强杀 | 重开前查询原 ACTIVE、新 SHADOW；两个更新一同回滚；新 boot 撤销所有 ACTIVE 授权；原保护保留 |
| F06 | `switch_commit_survives_kill_before_owner_epoch_changes` | 真实子进程；切换已提交，owner 内存 epoch 仍旧时强杀 | 重开前仅新 epoch ACTIVE，更新账本指向新 epoch；重开后无旧授权复活；原保护保留 |
| F07 | `injected_disk_full_rolls_back_intent_and_blocks_all_sends` | 进程内故障注入；intent mutation 后抛出 `sqlite3.OperationalError("database or disk is full")` | intent 事务整体回滚；durable 不前进；发送次数 0；没有实际占满磁盘 |
| F08 | `old_boot_ack_cannot_authorize_current_owner_send` | 进程内故障注入；将 pending Ack 的 boot 替换为旧 boot | owner 进入存储协议失败关闭；发送次数 0 |
| F09 | `later_ack_cannot_advance_past_missing_earlier_commit` | 进程内协议错误注入；将后一个事务的 Ack 放入前一个事务的 Future | 拒绝身份/序号错配；durable 连续边界不前进；发送次数 0。这不是正常异步 Future 的乱序完成测试 |
| F10 | `visible_window_refresh_and_close_keep_kernel_protection` | 真正可见的 Windows Qt 窗口；实际 native IPC 刷新后关闭窗口 | `IsWindowVisible` 为真、截图存在、资金显示未知；窗口关闭后内核仍在，PROTECTED/持仓 1 保留 |
| F11 | `visible_disconnected_window_retains_rows_and_marks_them_stale` | 真正可见的 Windows Qt 窗口；刷新后终止该用例自己的 DEV 服务进程，再刷新 | 连接失败被观察到；原持仓行保留并标记过期；截图存在；没有伪造连接成功 |

强杀只针对由本用例创建、报告过断点的 runtime PID。Windows venv 启动器可能不是 Python runtime PID；测试先核对其与已知 launcher 的父子关系，再取得该 PID 的进程句柄并终止。没有按进程名或系统范围查杀。每次等待均小于 10 秒，断点子进程还具有自己的退出期限。

## 原始证据

所有用例的原始材料保存在唯一 `output/faults/<UTC>-<case>-<uuid>/`；成功和失败均不删除。每份 `result.json` 包含执行范围、结果和断言。真实强杀额外保留 `stage.json`、`child.log`、原 SQLite 库、实际 runtime/launcher PID、退出码及重开结果；可见台额外保留 native 服务日志、GUI 日志、ready/report JSON 和截图。测试产物不进入生产 wheel。

完整检查 [188项全部通过](../../../output/checks/20261109T055210Z-a024681d/result.json)，源文件在检查期间未变。下表11份对应结果均为 `passed`，已核对所需文件存在：

| 编号 | `output/faults/` 下的证据目录 |
| --- | --- |
| F01 | `20261109T055214Z-test_durable_intent_before_owner_ack_becomes_unknown_without_replay-0db762d6` |
| F02 | `20261109T055213Z-test_actual_send_call_without_saved_return_remains_unknown-5c7abc8e` |
| F03 | `20261109T055216Z-test_uncommitted_protection_latch_is_not_claimed_durable_after_kill-6b3dab8f` |
| F04 | `20261109T055214Z-test_committed_latch_survives_kill_before_owner_accepts_ack-7334ea92` |
| F05 | `20261109T055215Z-test_switch_before_commit_rolls_back_both_strategy_updates-ecf9d48a` |
| F06 | `20261109T055216Z-test_switch_commit_survives_kill_before_owner_epoch_changes-ca366cd8` |
| F07 | `20261109T055217Z-test_injected_disk_full_rolls_back_intent_and_blocks_all_sends-b1e42f4c` |
| F08 | `20261109T055217Z-test_old_boot_ack_cannot_authorize_current_owner_send-0a3b58f5` |
| F09 | `20261109T055217Z-test_later_ack_cannot_advance_past_missing_earlier_commit-a5876b47` |
| F10 | `20261109T055229Z-test_visible_window_refresh_and_close_keep_kernel_protection-0e42a00a` |
| F11 | `20261109T055227Z-test_visible_disconnected_window_retains_rows_and_marks_them_stale-dc8e5a4d` |

可见截图：[正常刷新并关闭前](../../../output/faults/20261109T055229Z-test_visible_window_refresh_and_close_keep_kernel_protection-0e42a00a/console.png)、[断连后旧行标记过期](../../../output/faults/20261109T055227Z-test_visible_disconnected_window_retains_rows_and_marks_them_stale-dc8e5a4d/console-stale.png)。这是自动化可见窗口证据，不是人工操作验收，也没有验证跨 Windows 服务身份/会话的访问控制。

## G0 状态机 24 例的覆盖边界

对照来源：`docs/spec-source.json` 指向的 `OT-G0-SM-1/state-machines.md` 第 11 节。本表用于追踪，不能把一个部分覆盖项计作整行验收通过。内核/reducer/策略、控制与状态流测试均已纳入上述完整检查；结果只说明所列开发路径。

| G0 用例 | G1 已有证据或关联测试 | 尚未由本报告证明的部分 |
| --- | --- | --- |
| SM-001 新 boot 不恢复旧 ARMED/context | F01–F06 重开均 DISARMED；`test_new_boot_revokes_old_epoch_and_epoch_numbers_do_not_repeat`；默认入口不能 arm 未批准规则 | 柜台四类事实核对及 READY 门禁的实际来源在 G2 |
| SM-002 首笔 pending 预占阻止重复额度 | `test_pending_reservations_prevent_double_budget`、`test_no_sender_before_owner_accepts_commit_ack` | 真实账户预算与跨品种柜台额度在 G2 |
| SM-003 TX_INTENT 提交后强杀 | F01、F02 直接覆盖两种发送边界；验证原身份/UNKNOWN/无重发 | 真实 SDK 调用返回、柜台查询恢复在 G2；物理断电未验 |
| SM-004 临发 PauseNew 与过期授权 | `test_pause_while_storage_pending_prevents_send`、`test_context_expiring_after_admission_prevents_dispatch`；F08 | 精确重复旧 Ack 与控制队列完整序列不由 F08 代替；参见控制协议测试结果 |
| SM-005 第 2 Ack 先到、旧 boot Ack | F08/F09拒绝旧boot/错配；另有 `test_later_valid_ack_waits_for_earlier_future_before_any_send`：两个真实事务已提交、合法Ack逆序完成，第2个不得越过第1个发送，补齐后按序且只发送一次 | 本地有序提交/逆序Ack交付已测；真实SDK/柜台身份适配在G2 |
| SM-006 开仓部分成交/撤单/晚到成交 | `test_partial_fill_protected_late_fill_and_single_exit_target`、`test_cancel_request_and_quantity_gap_do_not_release` | 已有例数值为 1 再 2；G0 的 10→3→2 精确轨迹和柜台回报归并仍须 G2 |
| SM-007 平仓部分成交/撤改剩余目标 | 同一退出 target 的内核例；`test_pending_and_unknown_exit_quantity_cannot_be_reused` | G0 指定的平 10→成 3→再成 2→余量终态全序列及真实撤改在 G2 |
| SM-008 撤单 ACK/终态/成交晚到 | `test_cancel_request_and_quantity_gap_do_not_release`、`test_cancellation_and_terminal_without_details_do_not_release` | 柜台 ACK、累计与明细乱序归并在 G2 |
| SM-009 重复及冲突成交 | `test_fill_duplicate_is_idempotent_and_conflict_is_visible` | 柜台 trade_key 适配及跨交易日身份在 G2 |
| SM-010 锁存不依赖策略/GUI | F04、F10；`test_cli_client_exit_does_not_delete_protection_or_stop_kernel`；`test_registered_shadow_can_be_killed_without_stopping_kernel` | 已有策略子进程真实强杀、GUI 真实关闭；未证明人工验收或全部生产策略版本 |
| SM-011 未提交锁存/观察尾部丢失 | F03、F04 区分未提交与已提交；重开要求核对且不假称 NORMAL 为 durable PROTECTED | 没有物理断电，也没有存储设备缓存掉电测试；细粒度观察覆盖报告仍须后续来源接入 |
| SM-012 同事件多个退出原因 | protection 的 12 组 G0 候选序列 reducer、`test_protection_suppresses_waiting_but_preserves_inflight_stage` | 候选业务规则未批准；真实 OMS 多来源并发退出在 G2 |
| SM-013 0 仓但原开仓活动/UNKNOWN | `test_pending_and_unknown_exit_quantity_cannot_be_reused`、部分成交晚到及 quantity-gap 内核例 | 完整 FLAT_PENDING_CONFIRM、UNKNOWN 晚到与核心更新禁入组合需专项验收 |
| SM-014 CLOSED 后发现漏成交 | `test_late_fill_invalidates_q0_evidence_without_recalculating_it` 只覆盖 reducer 证据失效 | CLOSED 封存后完整账本追加纠正、重新纳管/接管告警流程未由本报告证明 |
| SM-015 优先平仓与较早成交屏障 | 控制协议的事实屏障/命令队列测试关联项 | 由新控制测试和完整检查报告确认；本 11 例未单独执行 |
| SM-016 快照 B 后增量不可被覆盖 | `test_snapshot_boundary_preserves_later_facts_even_when_delta_interleaves`、`test_pending_observation_heartbeat_cannot_advance_committed_view` | 由新流测试和完整检查报告确认；F10/F11 只证明 native 读取/断连显示 |
| SM-017 策略切换提交前后故障 | F05、F06 真实强杀；`test_switch_rejects_old_epoch_and_never_has_two_active_rows`、`test_handoff_preserves_protection_opportunities_and_orders` | F05/F06 不代表同时新旧提案的全轨迹；真实策略计算和物理断电未验 |
| SM-018 接管中新增事实使清单失效 | 控制协议的接管证据/版本测试关联项 | 本 11 例未执行；需完整检查报告确认本地语义，真实外部渠道在 G2/G5 |
| SM-019 接管完成后不补原目标 | 控制协议的接管/重启测试关联项 | 本 11 例未执行；外部平仓及人工确认闭环不在本轮 |
| SM-020 维护检查后新事实/旧提交竞态 | 控制协议的维护安全屏障测试关联项 | 本 11 例未执行；由完整检查报告确认，本轮不安装/停止实际服务 |
| SM-021 持久维护阻止守护重启 | 本地维护状态可由控制协议开发测试验证 | 没有安装、启动或验收守护/Windows 服务；G5 执行 |
| SM-022 休市/墙钟跳变/跨柜台交易日 | protection 仅有 session disabled/due 的纯规则测试 | 真实交易日历、行情时效、墙钟跳变与跨日未知保留未整体验收，后续 G2/G3/G5 |
| SM-023 权限收紧不删既有保护 | F05/F06、新 boot 撤销授权保留保护；策略上下文和不可变计划测试 | 许可/白名单/模板/策略租约的各类实际来源与组合仍须后续阶段 |
| SM-024 全机断网反复检查但不复制平仓 | F02 的 UNKNOWN/不重发、F11 的界面断连仅覆盖局部；流测试覆盖有界保留/显式 Gap | 没有整机断网、柜台 5 秒检查或外部观察点验收；不得据此声称已平仓，G2/G5 执行 |

## 与新内核提交接口的兼容复核

本轮只读复核 `engine.py` 的 `_Pending.tx_id`、committed authority、提交视图历史和多 cycle stage：六个子进程断点仍使用公开内核/ledger 行为，未引入生产 fixture 开关；Pending 的 `future` 可被 QA 进程内错误注入替换；新 Ack 的 tx_id/outcome 匹配会进一步拒绝 F08/F09。提交历史只接收 owner 已确认的状态，未提交阶段截图不用于证明 durable。合入后已执行上述188项完整检查，原故障与可见窗口11例全部通过。

复跑命令（不连接柜台）：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_faults.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_visible_console.py -v
```
