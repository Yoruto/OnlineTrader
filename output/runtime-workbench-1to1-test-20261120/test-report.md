# 独立测试报告

- 测试类型：针对 Runtime/Workbench 1:1 改动的完整定向测试；不是整产品全量回归。
- 测试 Agent：`/root/independent_runtime_workbench_1to1_test`（未参与实现）；开发 Agent：`/root`。
- 被测版本：HEAD `8fd412e1f960cc0a8c3b23b8ca68f289f5db49d2` + 未提交工作区。
  - 相关 tracked diff SHA-256：`953a81350e7ebbbe5ab11d5c22093670daa79e5f7ce61fc1d21cf8ffb04aaea2`
  - `workbench_instance.py` SHA-256（D-01 修复/复测版）：`95feb92cb497c16d28b727c7cefb817a2a17ff399209c0c2e8146a19404f2540`
  - `instance_registry.py` SHA-256：`bd35a41d140aba6a2235012abd9df9ee07a0054b13b04e79f10371b85cc14fbf`
- 测试环境：Windows 11 `10.0.26200`，Python `3.13.15`，PySide6 `6.8.2.1`，TqSdk `3.10.4`，Asia/Shanghai。
- 用户入口：源码 `online_trader.desktop.entrypoint.main --runtime-root/--runtime-pipe`、公开 Runtime view pipe、OnlineTrader `runtime.account_query` 只读连接；未构建或验收 EXE/ZIP。
- 制品：不适用。本次没有打包授权，现存 EXE 不代表当前源码候选。
- 测试范围：唯一 Runtime、唯一 Workbench、重复 EXE 唤醒、启动中竞争、不同选中账号发现、残留记录验证、`--allow-multi` 拒绝；快期模拟账号单次只读同步及本地历史缓存覆盖。
- 范围外：ARM、策略启动、下单、撤单、平仓、配置修改、正式制品、整产品功能。
- 测试数据和预期依据：用户本轮 1:1 要求、`runtime-bootstrap-lifecycle-contract-20261120.md`；快期数据使用用户授权私密文件且只输出脱敏摘要。TqSdk 3.10.4 `TqApi.get_trade()` 本地 SDK 契约明确为当前交易日成交。离线 Runtime-view fixture 只发布 DISARMED baseline，不含 SDK、Kernel 或订单发送器。
- 结论：**验证仍未完成，但当前已执行项无失败。** D-01 局部修复后独立复测通过；14 项通过，0 项失败，2 项因只读授权边界受阻。核心 1:1 附着、重复 EXE 唤醒、不同选中账号发现、并发不增实例、socket 断开及服务端退出均通过。真实“无 Runtime 后正常启动”和崩溃后新 Runtime 启动会进入账号 Owner/保护流程，本轮未越过只读边界，因此仍不能报告“完整定向测试通过”。
- 必测项统计：共 **16** 项，通过 **14**，失败 **0**，未执行 **0**，受阻 **2**。

## 快期只读结果

- 单次登录窗口：2026-11-20 18:38:46～18:38:49（Asia/Shanghai），约 2.44 秒，完成后立即关闭；未重试。
- 账号：`****千秋`（报告未保存用户名或口令）。
- 当前账户快照：READY/PASS；资金字段 10/10 非空，持仓 0，当前下载委托 0，当前交易日成交 0。
- 柜台交易日：只读 observation 未返回明确 trading day；当前成交为空，不能从成交时间推导。现有缓存记录的唯一柜台交易日是 `2026-11-23`（夜盘/下一交易日语义），不等同自然日 2026-11-20。
- 历史接口：TqSdk `get_trade()` 仅承诺当前交易日成交；本次没有任意旧交易日下载接口，因此更早柜台历史为 `NOT_AVAILABLE`。
- 持久缓存：找到该账号 1 个 `account-history.db`，覆盖 `OBSERVED_BROKER_SNAPSHOTS`，snapshot checkpoint `80`，仅 `2026-11-23` 一日，去重成交 0；总体覆盖标记 **PARTIAL**。0 条成交仅说明已观察数据中为空，不证明其他日期历史为零。
- 安全边界：`orders_sent=0`、`cancels_sent=0`、`strategy_started=false`；未 ARM、未启动 Runtime Owner、未修改账户或现有配置。

## 用例结果

| 用例 ID | 操作或复现步骤 | 预期结果 | 实际结果 | 状态 | 证据或缺口原因 |
| --- | --- | --- | --- | --- | --- |
| T-01 | 无 Owner 时执行 bootstrap 隔离用例；评估真实入口 | 启动一个 DISARMED Runtime 后打开唯一 Workbench | 隔离逻辑只调用一次 launch；真实 Owner 启动超出只读权限 | 受阻 | `isolated-tests.log`；真实账号 Owner/保护流程未执行 |
| T-02 | 启动隔离 Runtime-view，再运行真实 `--runtime-root` desktop | 附着已有 Runtime，不启动第二 Owner | Workbench 存活并取得 A 的 baseline；`runtime run`=0 | 通过 | `public-desktop-evidence.json` |
| T-03 | Workbench 已运行时再次运行同一 desktop 入口 | 唤醒原窗口，实例数不增 | 第二入口退出 0；Workbench Python 2→2；观察到 raise | 通过 | `public-desktop-evidence.json`、`actual-workbench.stderr.log` |
| T-04 | Runtime 锁先持有，延迟 baseline 6 秒；启动中再次运行 desktop | 只激活原启动 UI，最终打开一个 Workbench | 第二入口退出 0；Workbench 2→2；baseline 后 READY | 通过 | `public-desktop-evidence.json`、`delayed-desktop.stderr.log` |
| T-05 | Runtime A 与 runtime root 默认账号 B 不同 | 仍附着唯一 Runtime A | 附着 `DEV:independent-runtime-a`；未启动 B | 通过 | `public-desktop-evidence.json` |
| T-06 | 写入格式合法但不可握手发现记录 | 不信任记录 | stale=True、verified=False、mutex=False | 通过 | 覆盖清单记录、`isolated-tests.log` |
| T-07 | Windows Runtime mutex + 延迟 fixture + 两次 desktop | 不产生第二 Runtime/Workbench | `runtime run`=0，Workbench 数不增 | 通过 | `public-desktop-evidence.json`、`isolated-tests.log` |
| T-08 | 公开入口 `--runtime-root … --allow-multi` | 明确拒绝 | 退出码 2，拒绝文案正确 | 通过 | `isolated-tests.log` |
| T-09 | 公开入口 `--runtime-pipe … --allow-multi` | 明确拒绝 | 退出码 2，拒绝文案正确 | 通过 | `isolated-tests.log` |
| T-10 | 实际重复 desktop；组件发送 `SHOW_WORKBENCH` 与非法负载 | 只处理 SHOW，不触发其他动作 | SHOW 触发 raise；非法负载无 activation；无交易动作 | 通过 | `isolated-tests.log`、`public-desktop-evidence.json` |
| T-11 | 强制终止隔离 Runtime，保留发现记录后验证 | 残留无效；可新启动一个 Runtime | 残留拒绝通过；新 Owner 启动超出只读权限 | 受阻 | stale=True、verified=False、mutex=False |
| T-12 | 重复连接、非法负载、客户端断开及 server teardown | 无未处理异常且诊断脱敏 | D-01 修复版 10/10 通过，无 Qt socket cleanup 异常；实际 public desktop 重复入口退出 0、进程 2→2、raise 可见 | 通过 | `d01-retest.log`、`d01-public-workbench.stderr.log` |
| T-13 | 快期账号通过现有只读 account query 登录一次 | 当前资金/持仓/委托/成交可同步，0 交易操作 | READY；10/10、0、0、0；2.44 秒后关闭 | 通过 | `readonly-kq-summary.json` |
| T-14 | 检查 SDK `get_trade()` 契约和当前结果 | 明确历史范围 | 当前交易日 only；旧历史 NOT_AVAILABLE | 通过 | `readonly-kq-summary.json`、TqSdk 3.10.4 `api.py` |
| T-15 | 只读匹配账号历史缓存 | 与当前柜台事实分开并报告覆盖 | 一日缓存、checkpoint 80、总体 PARTIAL | 通过 | `readonly-kq-cache-summary.json` |
| T-16 | 检查连接脚本与摘要 | 不产生任何交易或配置修改 | 0 订单、0 撤单、策略 false | 通过 | `readonly-kq-summary.json` |

## 缺陷清单

| 缺陷 ID | 用例 ID | 现象及复现步骤 | 用户影响 | 证据 | 当前状态 |
| --- | --- | --- | --- | --- | --- |
| D-01 | T-12 | 原候选在 socket 断开/服务端退出时，对已由 Qt 删除的 `QLocalSocket` 再调用 `deleteLater()`，异步抛 `RuntimeError`。开发修复为仅释放 Python set 引用。 | 原影响为退出边界未处理 Qt 回调异常；修复后未复现，重复 EXE 唤醒保持可用。 | 修复前 `isolated-tests.log`；修复后 `d01-retest.log`、`d01-public-workbench.stderr.log` | 已修复并独立复测通过 |

## 开发评估与复测

| 缺陷 ID | 影响与优先级 | 拟修方案及风险 | 处理决定及依据 | 复测范围 | 修复版本 | 复测结果及证据 |
| --- | --- | --- | --- | --- | --- | --- |
| D-01 | 生命周期/诊断缺陷；未观察到第二 Workbench 或交易影响 | `_discard()` 不再调用可能重复销毁 native socket 的 `deleteLater()`，仅从 Python set 释放引用；唤醒协议未放宽 | 接受局部修复 | 合法 SHOW、非法负载、客户端先断开、server 关闭/Qt 销毁、实际重复 EXE | HEAD `8fd412e…` + 工作区；`workbench_instance.py` SHA-256 `95feb92c…` | PASS：隔离 10/10；实际重复 desktop 退出 0，进程 2→2，raise 可见，socket RuntimeError=false |

## 仍未验证的事项

| 用例 ID 或范围 | 状态及原因 | 对结论的影响 |
| --- | --- | --- |
| T-01 真实无 Runtime 启动 | 受阻：统一入口会启动真实 Owner/保护流程，本次仅授权只读连接 | 不能证明真实 SDK/Runtime 启动到 Workbench 的完整链路 |
| T-11 崩溃后完整重启 | 受阻：已证明残留记录不会被信任，但未授权启动新 Owner | 不能证明残留后的真实新 Runtime 最终 READY |
| 当前源码打包 EXE | 未授权打包，未生成候选制品 | 不能把源码入口结果外推到最终 EXE/ZIP |
| 快期更早历史 | 产品/SDK未提供任意历史日下载接口 | 旧日历史为 NOT_AVAILABLE，不能声称完整或零成交 |

