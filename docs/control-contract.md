# G1 操作者控制协议与验收契约

运行中工作台失联后，立即重连和 30 秒到期重试通过原执行器发起只读状态查询。回执未到达时保留已发送命令的身份，继续用原命令身份查询回执。直连结果生效后，早于该次回复的已发布快照不再覆盖它。 [工作台重连验收](refactor/dsv-parity-20261114/30-dev32-simnow-and-workbench-reconnect.md#工作台重连)

dev20扩展原`PAUSE_NEW_ENTRIES`/`ARM_ENTRIES`的CONTRACT范围，用于工作台逐品种新增门禁；不增加发送者、不改原协议资源。停用可撤所选品种自有开仓余单，保留原保护；启用只解除品种限制，不武装全账户。原ACCOUNT语义及下方G1历史记录保留，当前流程见[dev20运行功能](dsv-runtime-completion-20261110.md)。

本契约对应 G0 的 13 种 `OperatorCommand`。实现位于
`src/online_trader/kernel/control.py`，使用唯一内核所有者和 SQLite writer。
G1 不连接柜台；dev2运行种子仍为 `PROPOSED`。后续用户已[确认B01–B20全部以DSV为准](b01-b20-recommendation-confirmation.md)，文档批准尚未映射到运行审批。下文正向执行仅用于显式构造的
`Kernel(development=True, port=RecordingPort(), ...)` 和内部登记的 DEVELOPMENT
政策、事实，不授予正式或仿真柜台业务权限。

## 统一入口、幂等与事务

`Kernel.execute_command(message, peer_id)` 返回首份持久回执的 Future。
结构校验、账户、canonical payload hash、context 绑定 peer/purpose/operation/scope、
boot、epoch 和四项 revision 均先校验。schema/hash/account 等线缆错误直接报错；
已有命令身份先查幂等记录，随后才对新命令检查时效，因而断线、旧 context 或重启
不会使已提交命令重复执行。同 ID 同 hash 合并在途 Future 或返回已记回执，异 hash
返回 `E_DUPLICATE_CONFLICT`。按 ID/hash 查询使用 `control.get_status(...)`。

首个业务事务同时插入 `operator_commands`、命令元数据和原业务写入。
`bind_submission` 将命令 INSERT 放在业务写入之前，接管、维护等外键引用在同一事务
成立。只有身份、序号和 revision 均相符的 CommitAck 才完成回执 Future；提交失败
回滚全部写入并返回 `E_STORAGE`，不会把失败伪装为持久 `REJECTED`。

请求发送、撤单调用返回与最终业务完成分别记录。后置条件推进通过单独事务 CAS
增加 `receipt_revision` 并更新 `event_seq/durable_seq/tx_id`。重启从 SQL 回执和 journal
元数据恢复进度；不会重发原开仓或有歧义的撤单。

## 13 项命令

| 操作 | 接受条件与持久动作 | `SUCCEEDED` 的业务含义 |
| --- | --- | --- |
| `PAUSE_NEW_ENTRIES` | ACCOUNT；立即收紧内存权限，事务撤权；可选自身开仓残单批量准备撤单 | 撤权已持久；要求撤残单时还必须数量一致地终态 |
| `ARM_ENTRIES` | ACCOUNT；当前规则/config、内部 AUTHORITY 政策包含 MANUAL；无维护/接管/未结清仓限制 | 当前授权已持久；后来的撤权可使在途回执 SUPERSEDED |
| `FLATTEN_THEN_STOP` | ACCOUNT；EXECUTION 政策；先撤权，全部周期合并到同一剩余目标 0 | 持仓 0、相关订单数量一致终态、无 UNKNOWN，且当前完整账户四类事实覆盖；空周期也不能跳过核对 |
| `BEGIN_TAKEOVER` | ACCOUNT/CYCLE；唯一 takeover_id；登记不可变残余风险清单，撤权并置 TAKEOVER_PENDING | 接管准备已建立，可信原保护在确认前继续；不表示交接完成 |
| `CONFIRM_TAKEOVER` | 与 pending 同范围；当前完整 run、当前且与准备清单相同的 hash；逐一接受所有活动子单和 UNKNOWN/SEND_POSSIBLE attempt | 记录与周期同事务转 TAKEN_OVER；原目标不再自动发减仓请求，晚到事实仍记账 |
| `ENTER_MAINTENANCE` | ACCOUNT；唯一有效维护锁；建立 QUIESCING 并禁开；不会附带平仓 | 空仓路径需当前完整账户事实、无活动/UNKNOWN单、未决预占、未解差异及待提交事务；转 SAFE_TO_STOP。显式完整账户交接路径转 STOP_COMMITTED，并保留残余风险 |
| `MANUAL_OPEN` | CONTRACT 精确匹配；EXECUTION 政策、当前入场授权及最终风险检查；数量/方向/合约与冻结保护计划一致 | 开仓请求已得到数量一致的终态；调用返回仅为 EXECUTING，UNKNOWN 为 NEEDS_RECONCILIATION |
| `MANUAL_CLOSE` | CYCLE 精确匹配；正数量不超自身仓；EXECUTION 政策；G1 不认领外部仓 | 共同剩余目标已达到，相关潜在量与 UNKNOWN 已清；与保护出口共享目标，旧目标只收紧 |
| `CANCEL_SELECTED` | 精确 SELECTED_ORDERS，全部为已证明自身子单，ID 不重复 | 所选单全部数量一致终态；所有撤单准备与命令首次提交同事务 |
| `CANCEL_OWNED` | 仅枚举自身范围，`include_exits` 显式决定是否撤保护单 | 所选单全部数量一致终态；撤单请求不会释放潜在量 |
| `CANCEL_ACCOUNT_CONFIRMED` | ACCOUNT；完整当前账户 run、confirmation_id 与 manifest hash 均匹配 | 已证明自身的相关订单终态；G1 无外部账户订单执行适配器 |
| `REQUEST_RECONCILIATION` | FULL_ACCOUNT 或精确 ORIGINAL_CYCLE；登记 run 和四类 REQUESTED 子范围 | 独立来源明确完成四类事实，且范围和当前 revision 仍适用；请求本身绝不生成 COMPLETE 事实 |
| `RESUME_ORIGINAL_EXITS` | 精确原周期集合、当前完整 run、RESUME 政策；无 UNKNOWN，管理权未转出 | 原周期退出管理恢复；保留冻结保护状态与单一目标，不授予新仓权限 |

`SUCCEEDED` 的清仓、接管、维护回执不声明进程已经 STOPPED；G1 未实现停止服务或
柜台切换。默认运行或缺少相应 DEVELOPMENT 政策时，新增风险返回持久
`REJECTED/E_RULE_UNAPPROVED`。

## 事实屏障和维护窗口失效

证据绑定 account、boot、范围、相关周期 revision、四类事实覆盖与风险清单 hash。
ACCOUNT 事实可覆盖其内的周期操作；周期事实即使恰好覆盖当前所有周期，也不能
充当完整账户事实。任何相关 staged/committed 差异、晚到成交或风险清单变化均使旧证据
不适用。接管确认必须重新准备当前风险清单；发出撤单并不构成交接。

SAFE_TO_STOP 后若新事实使原证明失效，receipt_revision 前进到
NEEDS_RECONCILIATION/E_FACT_BARRIER，维护锁同事务回 QUIESCING，禁开保留。
重启后的旧 boot 证据同样失效并主动排队持久更新。只有 CommitAck 后发布维护投影。
SAFE_TO_STOP 是本机所有者控制域内的安全判断，不提供外部客户端的交易排他权。

## 内部离线 QA 接口

以下方法不暴露给 IPC，也不在默认生产模式制造事实：

```python
control.register_policy("qa-authority", "AUTHORITY", {
    "execution_scope": "DEVELOPMENT", "allowed_origins": ["MANUAL"]})
control.register_policy("qa-execution", "EXECUTION", {
    "execution_scope": "DEVELOPMENT"})
control.register_policy("qa-resume", "RESUME", {
    "execution_scope": "DEVELOPMENT"})

scope = {"scope_type": "ACCOUNT", "scope_ids": []}
control.register_evidence("qa-run", scope=scope,
                          complete=True, confirmation_id="qa-confirmation")
digest = control.manifest_hash(scope)
context_scope_id = control.context_scope(operation, scope, parameters)
```

政策 ID 不可变，绑定当前 boot；重启需新的政策 ID。证据仅在所有者接受边界登记，
持久保存四类范围；`complete=False` 可注入不完整核对，不能满足完成条件。
RecordingPort 只记录请求，不自动匹配、成交、核销 UNKNOWN 或产生柜台查询结果。

专项验收：`.venv\Scripts\python.exe -m unittest discover -s tests -p test_control.py -v`。
27 例覆盖全部 13 操作的可达正向或后置条件，并覆盖幂等、过期 context、缺政策、
原子回滚、批量撤单、未知撤单、风险清单变化、窄范围核对、维护晚到成交及重启失效。
