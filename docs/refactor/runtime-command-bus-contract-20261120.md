# Runtime CommandBus 进程内关联与失败合同

> 状态：2026-11-20 按期货账户权威与 Standalone Runtime 进程临时执行账本合同更新。当前源码候选已接入 Workbench，行为验收仍为 `NOT_RUN`。

## 权威与状态边界

- 期货账户是持仓、委托、成交、资金和收益的权威。Runtime 每次启动都从柜台同步当前事实。
- CommandBus 只在当前 Workbench 进程内保留未决命令关联，上限 64 条。它不写入 `QSettings`、注册表或本地耐久文件。
- 旧 `runtime/pending_receipt` 和 `runtime/pending_receipts_v2` 不作为恢复输入，也不会触发自动重发。新启动以当前 Runtime/柜台基线为准。
- 工作台或 Owner 进程结束后，不存在本地 pending 责任交接。新进程不得从旧本地状态推断上次命令结果。

## 提交时序

```text
UI -> CommandBus: validate current view + Capability + duplicate intent
CommandBus -> process-local registry: reserve command ID
CommandBus -> bounded worker queue: enqueue original request
worker -> Runtime endpoint: submit
Runtime endpoint -> Owner: command with account/boot/config binding
Owner --> worker: receipt
worker --> CommandBus: request_id + receipt
CommandBus -> process-local registry: update/remove matching ID
CommandBus --> UI: state + feedback
```

如果 bounded queue 在同步 `enqueue` 时拒绝，请求确定没有到达 worker：内存状态先标为 `LOCAL_NOT_ENQUEUED`，再删除同一 command ID 的进程内记录。不存在本地持久化清理或交接步骤。

## 回执身份合同

回调必须先找到 client `request_id`，再同时校验：

1. 返回 `command_id` 与原请求一致；
2. 返回 `operation` 与原请求一致；
3. 返回 `account_key` 与请求时账号一致；
4. 状态属于已知 Runtime receipt 状态。

当前窗口选择不得替代请求时账号。未知 request、身份不匹配或非法状态不创建新 pending，也不清理原 pending；当前进程中的原 ID 降级为 `OUTCOME_UNKNOWN / E_RESPONSE_IDENTITY`。

## 失败分类

| 场景 | 本地结论 | 当前进程的 pending |
| --- | --- | --- |
| Capability 在入队前拒绝 | 未发送 | 不创建 |
| worker queue 同步拒绝 | 确定未送达 worker | 标记 `LOCAL_NOT_ENQUEUED` 后清理该 ID |
| endpoint 明确返回 `E_AUTH/E_SCHEMA/E_SCOPE/E_BOOT_MISMATCH/E_CONFIG_EDIT/E_REVISION_CONFLICT/E_CONTEXT_EXPIRED/E_BUSY/E_CAPABILITY/E_NOT_READY` | 服务端明确拒绝 | `REJECTED` 后清理匹配 ID |
| `E_TIMEOUT/E_PIPE_CLOSED/E_RUNTIME_CONNECTION/E_INTERNAL/E_STORAGE/E_OWNERSHIP` 或未分类传输/服务失败 | 可能已经送达 | `SUBMISSION_UNKNOWN`，本进程保留原 ID |
| receipt 查询失败，包括 `E_OWNERSHIP` | 没有得到终态证据 | `OUTCOME_UNKNOWN`，本进程保留原 ID |
| receipt 为 `NEEDS_RECONCILIATION` 或其他已知非终态 | 责任未闭合 | 本进程保留原 ID |
| receipt 为 `SUCCEEDED/FAILED/REJECTED/SUPERSEDED` 且身份匹配 | 服务端可靠终态 | 清理对应 ID |

Owner boot 在同一 Workbench 进程内变化时，不生成新 ID，也不自动重发；当前进程中的旧 ID 降级为 `OUTCOME_UNKNOWN / OWNER_RESTARTED`。Workbench 本身重启后该内存集合消失，新会话从新 Runtime 基线和柜台事实开始。

## 关闭合同

- Coordinator 先把三个 worker 标记为 interruption，再关闭活动 pipe，最后等待线程退出。
- interruption 后不得开始队列中的下一条业务请求；Qt 关闭不向 Owner 发送 stop，也不 kill Owner。
- 活动 submit 被取消时，只能在当前进程存活期间按“可能送达”处理。进程结束后，重新同步柜台事实，不从本地交接未决状态。

## 回退和验收边界

Workbench 只保留一个 Runtime CommandBus sender。回退使用显式 `runtime run` + `--runtime-pipe` 诊断入口或回退整个候选版本，不在运行中恢复旧 sender。本次修改后的独立行为测试仍需专门授权；未执行时必须报告 `NOT_RUN`。
