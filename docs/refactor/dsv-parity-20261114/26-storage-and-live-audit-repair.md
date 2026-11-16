# 存储读写隔离、SQLite 诊断与现场日志对照修复

基线 `6b781606823ae3b85f4a3c39e94817dfda7076da`。本批不重复现有 PR #11 的历史成交 ID 修复或 PR #12 的原版循环观察器、分钟对照和规则标签修复；这两个 PR 仍须分别审查、合并。本批不修改交易信号、账户锁、UNKNOWN 占量、配置哈希、冷切换条件或 CI/CD。

## 1. Journal 读写隔离

原 DELETE 模式中，观察器持有未结束的 SELECT 能使交易 COMMIT 在约两秒后失败。替代 Journal 在原 schema、账户和配置绑定验证成功后切换到 WAL，保持 synchronous=FULL、BEGIN IMMEDIATE 和每子单发送前的完整提交。磁盘库未成功进入 WAL 时拒绝启动，不静默回退。内存 QA 库允许 memory 模式。

只有原交易 owner 写入并执行默认 1000 页 PASSIVE 自动检查点；观察器必须 mode=ro，保留短批次读取，不引入后台写入者或第二个检查点线程。真实写入、磁盘及持久化失败仍回滚并沿原路径使执行器失败；未提交的目标绝不派发。不会重放事务函数、清除 _failed、无限等待或在内存中冒充提交成功。

在线 TQKQ/TQ_SIMNOW 库在任何打开/修改之前检查 SQLite 运行库：要求 3.51.3+，或官方修复回补 3.50.7 / 3.44.6。否则报 `E_DSV_STORE_SQLITE_VERSION`。这是 `sqlite3.sqlite_version`，不是 Python 版本。本地隔离实验使用 SQLite 3.46.1，只验证单 writer/只读观察器功能，不为旧库并发检查点安全背书。

部署必须停旧 owner、关闭观察器，在同一主机本地磁盘和原账户排他下启动新 owner。网络文件系统不在支持范围。WAL 与 SHM 属于数据库运行状态；运行中禁止只复制主 .db、手删 WAL 或原地换日志模式。使用 SQLite backup，或所有连接正常关闭后备份。迁移私有临时库关闭再发布的路径有本地回归。长读仍可能阻止 WAL 回收和造成文件增长，短读规范、磁盘容量监控和 Windows 长稳仍需要验收；不承诺任意外部锁/磁盘故障下继续交易。

参考：[SQLite WAL](https://www.sqlite.org/wal.html)，包括持久模式、同主机限制、备份及 WAL-reset bug 版本说明。在线升级环境与 Windows 磁盘/权限/崩溃恢复验收未执行，保持草稿，不直接部署。

## 2. 脱敏诊断

`exception_record` 与 `RuntimeDiagnostics` 保留真实 sqlite3.Error 的 `sqlite_errorcode`、与运行库常量匹配的 `sqlite_errorname`，以及事务阶段、配置的 busy timeout、日志模式和事务状态。字段为数字/枚举/布尔白名单；不输出 SQL、数据库路径、账户、参数或原始异常消息。嵌套异常链中的 SQLite 分类不会被 E_DSV_STORE 包装吞掉。回滚本身失败时保留第一次异常，不伪报成功。

参考：[Python sqlite3.Error](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.Error)。

## 3. 现场日志比较

`compare_live_logs` 不再以集合中“存在一个匹配”替代正确过程。保留有序候选收据与事件序号，报告同合约/分钟重复消费，校验 consume 与该 feed 的原生 candle_id 一致；没有行情键证据则不认定匹配，切换 feed 不沿用旧键。保留观察窗开始前、同一分钟的有效收据。

价格用 Decimal 比较原始文本数值，不把 9039.44 四舍五入成 9039.4 后宣告一致。先错后对、先对后错及重复同值均不能隐藏。原版文字日志有限频，因此不把每条未见于原版日志的候选决定一律判为多余交易。

报告明确输出 `verdict`：`MATCH_AT_LOG_PRECISION`、`MISMATCH`、`INSUFFICIENT_EVIDENCE`。命令默认相应退出 0、1、2；`--report-only` 仅允许生成报表成功退出零，报告结论不改变。原有混合正负例测试改为检查返回码 1，而非删除其负例。

这是日志显示数值和候选收据不变量核对，不是原版完整数值/顺序真值；`candidate_persisted_minute_state_verified=False` 明确未从每轮 checkpoint 验证状态。完整冻结原版分钟持久化对照属于 PR #12。零报撤成、相同报价抽样或报表生成成功不等于全策略验收。

## 4. 本次实际本地验证

Linux / CPython 3.13.5 / SQLite 3.46.1。以固定历史完整源码快照及后续精确源文件重建被测基线，关键更新文件校验 Git blob；正常导入，无缺失模块占位。只使用合成账户和临时库；DsvSession 的既有 OFFLINE 锁注入参数接真实 POSIX 文件锁（Windows 用原 AccountLock），交易发送端为 RecordingPort。没有 SDK 网络、真实账本或 CI。

- 新增 17 个存储/诊断测试与 16 个严格日志比较测试全部通过。
- 合并前五批、相邻 UNKNOWN/保护/历史显示/有序比较和原现场读取测试，共 217 个方法：216 PASS、0 FAIL、0 ERROR、1 SKIP（Windows AccountLock 公共会话用例）。重复执行不叠加覆盖数。
- 基线反例子集 7 个方法：2 FAIL、5 ERROR；包含长读提交失败、诊断分类缺失、重复/错误分钟漏报和价格舍入。错误项含旧输出缺少新字段，不能全说成不同业务 bug。
- STOP 后 5 手仍受止损保护：悬停长读时能够提交退出目标并生成一个记录端平仓请求；发送边界用另一只读连接确认 checkpoint/准备效果已经提交；相同轮次不重复发送。
- 存储失败仍没有发送，效果表和状态事务回滚；SQLite backup 保留 WAL 中已提交事实；独立子进程不关闭数据库直接退出后可读回已提交状态与事件；历史 DELETE 文件的读锁恢复测试保留实际锁场景。
- compileall 与 git diff --check 通过。完整最新工程的 Windows/Qt/IPC、真实 SDK/柜台、全冻结策略对跑、最终 EXE、断电/磁盘压力及长稳均 NOT_RUN。

手动复现（不新增工作流）：

```text
PYTHONPATH=src:. python -m unittest tests.test_dsv_storage_read_isolation tests.test_dsv_live_log_strict tests.test_dsv_live_log_comparison tests.test_dsv_workbench_history_compat tests.test_dsv_runtime_diagnostics tests.test_dsv_runtime_diagnostics_serve -v
```

以上命令在 Linux 使用现有 OFFLINE 锁夹具；Windows 设置 PYTHONPATH=src;. 后使用项目原解释器。测试不改变生产 SQLite 运行库，不将低版本伪装为已修复版本用于实际在线会话。
