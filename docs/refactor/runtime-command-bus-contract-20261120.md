# Runtime CommandBus 持久化与失败合同

> 状态：PR-B 编码合同及 PR-C 生产接管后的持久化依据（2026-11-20）。当前源码候选已接入 Workbench，行为验收仍为 `NOT_RUN`。

## 状态与数据合同

本机注册表键为 `runtime/pending_receipts_v2`，值是一个完整 canonical JSON envelope：

```text
ONLINE_TRADER_PENDING_RECEIPTS/2
  records[]
    schema_version = ONLINE_TRADER_PENDING_RECEIPT/2
    account_key
    command_id
    operation
    scope_hash | null
    source = WORKBENCH
    local_status
    reason | null
```

- 注册表最多保留 64 条未决责任；达到容量后拒绝产生新 command ID，不截断旧记录。
- 每次整体写入后执行 `QSettings.sync()`，检查 `status()`，并回读比较完整 payload。
- `--allow-multi` 的多个 Workbench 使用同一 `QLockFile`。每次变更在锁内 `sync`、重读当前 envelope、只增删目标 ID、合并其他进程记录、整体写回并核验；锁不可用时禁止新提交。
- 解析损坏、未知 schema、字段形状错误、重复 command ID 或容量超限时，保留原设置值并锁定新提交；不自动覆盖证据。
- 旧的无 envelope 原型列表是已知迁移输入：仅在全部记录可严格验证且不超容量时转成 v2；任何异常 fail-closed。
- 进程重启后只能推断“原 ID 仍需查询”，一律恢复成 `OUTCOME_UNKNOWN`，不从本地状态推断服务端已接收或已终态。

## 提交时序

```text
UI -> CommandBus: validate current view + Capability + duplicate intent
CommandBus -> QSettings: add ID, sync, verify
QSettings --> CommandBus: durable
CommandBus -> bounded worker queue: enqueue original request
worker -> Runtime endpoint: submit
Runtime endpoint -> Owner: command with account/boot/config binding
Owner --> worker: receipt
worker --> CommandBus: request_id + receipt
CommandBus -> QSettings: update/remove only matching ID, sync, verify
CommandBus --> UI: state + feedback
```

如果 bounded queue 在同步 `enqueue` 时拒绝，该请求确定没有到达 worker：内存状态标为 `LOCAL_NOT_ENQUEUED`，并只删除同一个 command ID 的 durable 记录。若清理持久化失败，保留该 ID、锁定新提交并报告存储故障。

## 回执身份合同

回调必须先找到 client `request_id`，再校验：

1. 返回 `command_id` 与原请求一致；
2. 返回 `operation` 与原请求一致；
3. 状态属于已知 Runtime receipt 状态；
4. pending 的 `account_key` 与请求时账号一致。

当前窗口选择不得替代请求时账号。未知 request、身份不匹配或非法状态不创建新 pending，也不清理原 pending；原 ID 降级为 `OUTCOME_UNKNOWN / E_RESPONSE_IDENTITY`。

## 失败分类

| 场景 | 本地结论 | pending |
| --- | --- | --- |
| Capability 在入队前拒绝 | 未发送 | 不创建 |
| durable 写入失败 | 未发送 | 保留原证据并锁定新提交 |
| worker queue 同步拒绝 | 确定未送达 worker | 仅在 durable 清理成功后删除该 ID |
| endpoint 明确返回 `E_AUTH/E_SCHEMA/E_SCOPE/E_BOOT_MISMATCH/E_CONFIG_EDIT/E_REVISION_CONFLICT/E_CONTEXT_EXPIRED/E_BUSY` | 服务端明确拒绝 | `REJECTED` 后清理匹配 ID |
| `E_TIMEOUT/E_PIPE_CLOSED/E_RUNTIME_CONNECTION/E_INTERNAL/E_STORAGE/E_OWNERSHIP` 或未分类传输/服务失败 | 可能已经送达 | `SUBMISSION_UNKNOWN`，保留原 ID |
| receipt 查询失败，包括 `E_OWNERSHIP` | 没有得到终态证据 | `OUTCOME_UNKNOWN`，保留原 ID |
| receipt 为 `NEEDS_RECONCILIATION` 或其他已知非终态 | 责任未闭合 | 保留原 ID |
| receipt 为 `SUCCEEDED/FAILED/REJECTED/SUPERSEDED` 且身份匹配 | 服务端可靠终态 | 只清理对应 ID |

Owner boot 改变不会生成新 ID，也不会自动重发；所有旧 ID 降级为 `OUTCOME_UNKNOWN / OWNER_RESTARTED`，待新基线取得后查询原回执。

## 关闭合同

- Coordinator 先把三个 worker 标记为 interruption，再关闭活动 pipe，最后等待线程退出。
- interruption 后不得开始队列中的下一条业务请求；Qt 关闭不向 Owner 发送 stop，也不 kill Owner。
- 活动 submit 被取消时，其 command ID 仍按“可能送达”处理；进程持久化记录是下次启动的查询依据。

## 回退和验收边界

PR-C 已在源码中单向接入生产 `WorkbenchWindow`，旧 `_RuntimeWorkbenchRequest` 和单槽运行链已退役，不保留第二个 sender。回退使用显式 `runtime run` + `--runtime-pipe` 诊断入口或回退整个候选版本，不在运行中恢复旧 sender。测试、真实管道、账号连接和订单行为没有本次授权，均为 `NOT_RUN`，只能执行源码编译检查。

