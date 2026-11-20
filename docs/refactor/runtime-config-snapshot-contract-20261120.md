# Runtime ConfigSnapshot 与逻辑通道合同

> 状态：PR-E 编码合同（2026-11-20）。ConfigSnapshot 是原 Ledger 配置权威的只读规范投影，不是新事件源或平行配置表。

## ConfigSnapshot

每个 Workbench baseline/delta 发布一个 `ONLINE_TRADER_CONFIG_SNAPSHOT/1`：

| 字段 | 来源 |
| --- | --- |
| `account_key` / `kernel_boot_id` | 当前唯一 Owner |
| `config_revision` | `authority.config_revision` / Kernel 已提交 revision |
| `payload_hash` / `rule_set_id` | `config_revisions` 与 Kernel 当前配置身份 |
| `profile_hash` | Ledger payload；在线 DSV Runtime 还必须与当前内存 Profile 一致 |
| `effective_boundary` | `config_revisions.effective_boundary`，当前在线保存为 `NEXT_CYCLE` |
| `created_event_seq` | 配置事务提交水位 |
| `effective_at_utc` | payload 中已提交的 `parameters_saved_at_utc`，没有则为 null |
| `decision_inputs` | 日历 hash、行情 source epoch、gateway identity、时间语义/clock epoch、合约元数据 hash |
| `integrity` / `errors` | payload hash、revision、rule set 和当前 Profile 的只读交叉核验 |

投影不写 Ledger。`integrity != VERIFIED` 时增加 `CONFIG_SNAPSHOT_INVALID` block，不把 portable 文件或 UI 草稿提升为有效配置。

## 生效与恢复边界

```text
strict Profile validation
  -> expected config revision check
  -> insert config_revisions + update authority in one Ledger transaction
  -> commit
  -> replace in-memory Profile/Strategy
  -> publish ConfigSnapshot
```

新参数只影响新周期；已有周期继续通过 plan 的 `config_revision + config_hash` 回读原 Profile 管理保护。重启由 authority 指向的 Ledger payload 恢复，并复核 payload/Profile hash。portable 配置描述未来启动意图，不能覆盖已初始化账户的执行事实。

## 正交状态

- Service 摘要只从连接事实派生，不写数据库万能枚举。
- Broker/Reconciliation 继续发布 `broker_connected`、`synchronized`、`fault`、`storage_failed`、`recovery_required`、`clock_fault` 和 `pending_transactions`。
- Entry Authority 只使用 `ARMED/DISARMED`；品种/来源暂停继续使用 Entry Policy。
- Client preference 只保存选择、布局、筛选、草稿和待查询 command ID，不能保存交易恢复结论。

## 逻辑通道和背压

| 通道 | 合同 | 当前实现 |
| --- | --- | --- |
| Control | command ID、提交、回执可靠有序；未知结果可按原 ID 查询 | 独立 bounded command worker；队列满在发送前拒绝 |
| State | baseline + contiguous delta + generation/sequence/digest；gap 重建 baseline | Runtime View + `ViewReplica`；128 delta / 8 MiB 历史上限 |
| Query | 历史、账户和 Runtime 日志不阻塞 Control/State | 独立 bounded query worker |
| Market display | View 内按业务主键更新；只影响展示，不替代 Owner 行情门禁 | delta row upsert；Owner 原行情路径不丢必要事实 |
| Diagnostics | 有界、脱敏、失败不成为交易权威 | Runtime log/query 与现有 run diagnostics |

现有边界已能隔离控制、状态和慢查询，因此本阶段不新增 Redis/Kafka、网络服务或第二执行 Owner。是否进一步拆 Market/Diagnostics 只能由帧大小、队列水位和 UI 延迟证据触发。

