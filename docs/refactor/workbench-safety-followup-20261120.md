# Workbench 命令与并行账本安全修复（2026-11-20）

## 范围

本分支基于 `b9726ae`，不改交易策略、订单路由、Runtime/Kernel 执行事实、SQLite schema、自动启动和柜台配置；不接入真实账户、不主动下单、不启用 CI/CD。修复两项已在生产入口存在的安全缺口：

1. 目前 Runtime Workbench 交易按钮尚未切换到新 `CommandBus`。旧 `_RuntimeOperatorRequest` 在 timeout/unknown 后可能再次发起相同意图并生成不同 `command_id`。
2. Portable Runtime 以前已生成 `ledger.db` 的目录，会绕过“标准桌面账本并行检测”。

## 行为与边界

- Workbench 在创建新的命令 ID 前读取未决 receipt：存在旧 ID 时不提交任何新 Runtime 命令，提示核对原命令，提供旧 ID；这是对未迁移旧控制入口的**保守安全封锁**，不是宣称已经完成 CommandBus 的正式迁移。
- 初次写入 ID 后立即执行 `QSettings.sync()`，检查错误后才发送命令。连接或内部错误保留原 ID；Runtime 在入队前明确返回的鉴权、忙碌、schema/scope 等拒绝，按现有 `CommandBus` 合同只清除同账号、同 ID 的 pending，避免把确定未提交误留成永久封锁。回执查询失败、断线和 boot 变化仍不作为原交易命令的终态。
- Runtime 命令结果必须同时匹配请求时记录的账号和 `command_id`；身份不一致时保留原 pending 并拒绝覆盖。明确终态回执仅可清理**同账号、同 ID** 的 pending，清理也必须通过 `QSettings.sync()`。
- 旧 Workbench 使用单一 `runtime/pending_receipt` 设置项；如果另一账号仍有 pending，本次宁愿拒绝新命令，也不能覆盖其 ID。后续 Client 迁移需升级成每账号持久化的多命令注册表，并对损坏/超过容量/异常断电失效执行 fail-closed。
- Portable Runtime 已有账本且无已验证 handoff 时，启动之前也需对标准 `LOCALAPPDATA/OnlineTrader/accounts/data` 账本进行碰撞检查；若有经检验的另一份原账本则拒绝启动。已验证 handoff 继续允许正常恢复。
- **尚未解决**：同机两个任意 Portable Runtime 目录间的账本分叉无法由固定标准路径发现；本 PR 不声称实现了全机器唯一的 ledger lineage/registry。既有分叉不得自动合并、覆盖或删除，必须冷停核对后 handoff。
- 真实 Workbench 的 one-shot 命令线程仍缺主动取消当前管道 I/O 的接口；退出时可能等待超时，不涉及执行 Owner 的继续运行。后续正式 CommandBus 接入需删除旧命令路径，而不是再叠加另一套 sender。

## 验证

新增离线回归：
- `tests/test_runtime_workbench.py`：unknown 阻止新提交、无关终态不可删除待核对 ID、相同 ID 的明确终态释放封锁。
- `tests/test_runtime_parallel_ledger_guard.py`：已存在 Portable ledger 也触发源检查、有效 handoff 的例外、与标准桌面账本碰撞拒绝。

本次审核修正还要求后续授权测试覆盖生产 `_runtime_request_result` / `_runtime_request_failed` 入口：明确未入队错误释放同一 pending，连接未知继续保留，以及账号或命令 ID 不一致的回执不能覆盖原 pending。当前仅完成源码修复，未把这些未执行场景写成已通过结论。

建议在具备产品 PySide6 的 Windows 环境执行：

```powershell
$env:PYTHONPATH = "src"
python -m unittest tests.test_runtime_workbench tests.test_runtime_parallel_ledger_guard tests.test_runtime_client_foundation -v
```

再做 Windows Named Pipe/Qt UI、断线、Owner 重启、真实账本冷交接、便携 ZIP 启动及隔离模拟账户的人工验收。以上验证在此提交中 **NOT_RUN**，提交代码不等于验证成功，**禁止直接把该 PR 当作可发布制品**。
