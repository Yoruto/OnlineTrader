# 独立测试覆盖清单

## 范围与环境

- 测试类型：针对本次改动的完整定向测试（不是整产品全量回归）。
- 用户需求及验收依据：每台 Windows 电脑最多一个 Standalone Runtime，并且最多一个 Workbench 附着；统一 EXE 在 Runtime 存在时附着、缺失时正常启动，Workbench 已存在时只唤醒；本机不同选中账号、残留发现记录和并发启动不得突破该约束。
- 新增只读授权：使用桌面私密文件中的快期模拟账号，只读同步资金、持仓、委托、成交和可用历史，不 ARM、不启动策略、不发送任何交易操作。
- 范围：Standalone desktop/bootstrap、Runtime 和 Workbench 单实例、重复启动唤醒、Runtime 发现与验证、并发边界、`--runtime-pipe`/`--allow-multi`；快期模拟账号当前交易日数据、缓存与历史覆盖能力。
- 范围外：策略与订单行为、ARM、下单、撤单、平仓、账户/配置修改、打包、整产品功能。
- 被测版本、环境与入口：执行时记录完整 HEAD、工作区差异哈希、Windows/Python/PySide6；优先 `online-trader-desktop --runtime-root`、`OnlineTrader.exe`（若已有未重建制品只可作为旧制品记录，不代表当前源码）、Runtime CLI/API 与实际 Qt；外部账号仅通过项目固定 SDK 环境和只读入口。

## 覆盖盘点

- [x] 本次需求的正常、边界、异常、恢复和并发场景均已列入。
- [x] 预期来自用户指令和当前公开生命周期合同，不以实现分支或当前输出反推。
- [x] 外部系统结果区分当前交易日、持久缓存、历史接口覆盖；空列表不证明历史为零。
- [x] 关键故障要求核对用户反馈及脱敏诊断。
- [x] 所有当前可执行的必测场景已执行并记录；真实“无 Runtime 后正常启动”及崩溃后重新启动因只读授权边界受阻。
- [ ] 制品验收不适用：本次未授权打包，现有 EXE 不代表当前源码候选。

| 用例 ID | 需求、功能或流程 | 入口、角色、环境及前置数据 | 用户操作 | 预期结果及独立依据 | 必测 | 状态 | 证据、缺陷 ID 或未测原因 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T-01 | 无 Runtime/Workbench 的统一启动 | 源码 desktop 入口；隔离 Runtime 配置；无本机 Owner | 启动 `online-trader-desktop --runtime-root` | 仅启动一个 DISARMED Runtime，取得可归属 baseline 后打开一个 Workbench | 是 | 受阻 | Bootstrap 隔离用例证明只调用一次 launch；真实入口会连接账号并进入 Owner/保护流程，超出本次只读授权，未执行。 |
| T-02 | 已有 Runtime、没有 Workbench | 隔离 Runtime-view fixture 已运行，Qt 未运行 | 运行统一 desktop `--runtime-root` | 不启动第二 Runtime，打开唯一 Workbench并附着原 boot/account | 是 | 通过 | 实际 public desktop 保持运行；Runtime baseline=`boot-independent-runtime-a`；进程中 `runtime run` 数为 0。 |
| T-03 | Workbench 已存在时重复 EXE | 一个隔离 Runtime + 一个实际 Workbench | 再运行统一入口 | 原窗口恢复/前置；Runtime 和 Workbench 进程数不增加 | 是 | 通过 | 第二入口退出码 0；Workbench Python 进程数 2→2（venv launcher+解释器）；首进程观察到 raise 请求。 |
| T-04 | Workbench 启动中重复 EXE | Runtime 锁已持有、baseline 延迟 6 秒，启动进度窗口存在 | 并发再次启动统一入口 | 只激活现有启动 UI，不创建第二等待窗口、Runtime 或 Workbench | 是 | 通过 | 延迟 fixture 下重复入口退出码 0，Workbench 进程数 2→2；baseline 后附着完成。 |
| T-05 | 本机不同选中账号发现 | fixture Runtime A=`DEV:independent-runtime-a`；Runtime root 默认账号 B=`SIM:KQ:2fe6…` | 运行统一入口 | 验证并附着本机唯一 Runtime A，不因 B 再启动或超时 | 是 | 通过 | 实际 public desktop 附着 A，`runtime run` 进程数 0。 |
| T-06 | 残留/伪造发现记录 | 无真实 Runtime；格式合法但无可握手 endpoint 的记录 | 执行发现 | 记录不被当作 Owner/baseline；只有真实互斥与握手可决定附着/启动 | 是 | 通过 | `stale_record_present=True`、`verified_runtime=False`、`runtime_mutex_owned=False`。 |
| T-07 | Runtime 并发启动 | 隔离 Owner 先持 Runtime 锁，baseline 延迟；两个 desktop 入口近同时启动 | 并发启动 | 最多一个 Runtime 成为 Owner，最终最多一个 Workbench | 是 | 通过 | 真实 Windows mutex + 延迟 public desktop；无第二 `runtime run`，Workbench 进程数不增加。 |
| T-08 | Standalone `--allow-multi` | Runtime root 入口 | 携带 `--allow-multi` 启动 | 明确拒绝，不创建 Runtime/Workbench | 是 | 通过 | 公开 entrypoint 退出码 2，stderr 含明确拒绝。 |
| T-09 | `--runtime-pipe --allow-multi` | 任意隔离 pipe 值 | 携带两参数启动 | 明确拒绝，不绕过 Workbench 单实例 | 是 | 通过 | 公开 entrypoint 退出码 2，stderr 含明确拒绝。 |
| T-10 | Workbench 唤醒通道能力边界 | 已有实际 Workbench；同一 Windows 用户 | 重复 EXE/发送非允许负载 | `SHOW_WORKBENCH` 可恢复窗口；非允许负载不触发交易或其他动作 | 是 | 通过 | 实际 Workbench 收到 raise；组件检查非法负载无 activation；0 个交易动作。 |
| T-11 | Runtime 崩溃残留发现记录 | 已强制终止隔离 Runtime，发现记录残留 | 读取并验证记录 | 残留记录经握手失败后失效，允许一个新 DISARMED Runtime 正常启动 | 是 | 受阻 | 残留拒绝已通过；真正重启会连接账号/进入 Owner 流程，因只读授权未执行后半段。 |
| T-12 | 重复启动/失败诊断脱敏 | 启动竞争、无效发现记录、客户端断开、server 关闭/Qt 销毁 | 查看 UI 与日志 | 能定位阶段与后续状态，不包含账号口令；无未处理异常 | 是 | 通过 | D-01 修复版复测：10/10 通过，合法 SHOW、非法负载、客户端断开及 server 销毁均无 `QLocalSocket already deleted`；实际 public desktop 重复启动仍可 raise 且进程数不增。证据 `d01-retest.log`、`d01-public-workbench.stderr.log`。 |
| T-13 | 快期模拟账号只读当前事实 | 用户授权账号；收盘阶段；TqSdk 3.10.4 | 使用 OnlineTrader `account_query` 只读连接一次 | 不产生交易操作；记录时间和资金/持仓/委托/成交类别数量 | 是 | 通过 | 18:38:49+08:00 完成：资金 10/10、持仓 0、当前下载委托 0、当前交易日成交 0；会话已关闭。 |
| T-14 | 快期历史接口真实覆盖 | 同一只读连接与本地 SDK 源码契约 | 核对 `TqApi.get_trade()` 与产品只读观察 | 明确当前交易日与更早历史覆盖；空列表不得标 COMPLETE | 是 | 通过 | 当前交易日范围；更早历史 `NOT_AVAILABLE`，未将 0 条解释为无历史。 |
| T-15 | 持久历史缓存语义 | workspace/LOCALAPPDATA 中该账号匹配缓存 | 只读 `account-history.db` 摘要 | 缓存与柜台当前事实分开；覆盖明确 | 是 | 通过 | 1 个缓存，`OBSERVED_BROKER_SNAPSHOTS`，仅 2026-11-23，checkpoint 80，去重成交 0；总体 `PARTIAL`。 |
| T-16 | 收盘状态下安全边界 | 同一账号与只读入口 | 核对脚本与结果 | 无策略启动、订单、撤单、平仓或配置修改 | 是 | 通过 | 单次只读登录；`orders_sent=0`、`cancels_sent=0`、`strategy_started=false`。 |

