# 离线业务恢复与旧账本迁移演练

## 回放输入

- 夜盘参考来自同一柜台交易日内实际回放的夜盘批次。日盘合约快照可提供 `night_boundary`，值为已收到的夜盘末价原生 UTC 时间。时间须与持久化的夜盘末价一致。09:00 延续使用夜盘高低与末价，延续目标一经接受即登记当日机会，重启后继续读取该记录。
- 断流检测使用实际盘段、原生报价时间戳和可比的墙钟及单调耗时。重复原生时间不续时，阈值与开盘 60 秒宽限均为严格大于。触发后登记零剩余目标，等当前盘段中原生时间更新的报价后按已核仓位路由。选配日停在平仓和子单均确认后生效。
- 回放帧可带 `trading_days`，内容为包含当前柜台日的、已核对完整交易日日历区间。月停按连续交易日计算，同月跨日保留，进入新月解除。冻结版月停被日切复位削弱的路径见原版取证，持久保留属于闭环修复。
- 异周期迟到成交按原子单和原周期入库。旧平仓终态数量先更新而逐笔未到时，同合约发送保持等待。可分配的平仓成交只扣原周期已证数量。原周期缺数量时保留待核原生事实，后续同周期原始开仓回报能补足时重新核算。
- 工作台可选中明确列出的旧周期并登记减仓。命名管道命令为 `REDUCE_DETACHED`，字段含 `contract_id`、`position_id`、`quantity` 和命令 `id`。下一轮须核齐所有子单和四个今昨仓桶，才按原周期发送。普通 `REDUCE` 始终针对当前周期。

## 迁移命令

```powershell
python -m online_trader offline-dsv migrate --source legacy.sqlite --input manifest.json
python -m online_trader offline-dsv migrate --source legacy.sqlite --input manifest.json --target candidate.sqlite
python -m online_trader offline-dsv rollback-review --source legacy.sqlite --db candidate.sqlite
```

清单 `type` 为 `legacy-dsv-migration/1`，需要 `source_account_key`、`source_environment`、`source_broker_identity`、旧版本 SHA-256 `source_release`、按十进制字符串提供的 `source_event_seq`，以及 `session` 对象。该对象使用离线 `serve` 会话头，含独立 `DEV:` 账户、`OFFLINE` provider、柜台 `trading_day` 和原 `profile`。在 `native_flat` 数组逐合约填写 `contract_id` 和零值 `long_qty`、`short_qty`、`active_orders`、`unknown_results`，代表演练数据中的已核快照。

- 默认预检读取 SQLite 一致视图及已提交 WAL，检查旧账户级排他、库结构、事件水位、持仓、委托以及逐笔成交。异常或未映射的业务状态返回阻断原因。
- 显式提供 `--target` 才生成全新事实库。收据记录旧逻辑快照摘要、事件水位、目标业务摘要和归档边界。目标从已停、待同步状态启动。可用现有离线 `serve` 入口及相同会话头打开，再回放首轮并核对下一动作。
- 回滚资格检查同时持有新旧账户级排他。旧库变化、新库有订单指令、原生事实或已消费机会时，拒绝恢复旧状态。命令只给出该演练时点的只读判定。
- 旧成交、费用及旧授权保留在原账本。非零未映射字段、不能证实的历史消费及未结状态阻止导入。相同配置为本阶段必需，以免改变迁入字段的解释。
- 已证明来自同一账户和柜台日的人工工作台观察，若只有持久状态提案、无策略候选或意图、无分钟消费、触发线、夜盘参考等业务状态，可作为历史归档。迁移收据记录数量和事件水位。实际策略记录与停开统计仍按原规则核对，新控制器依赖当前原生同步后运行。

## 修复已在 SimNow 柜台平仓的旧账本

```powershell
python -m online_trader offline-dsv recover-flat --source legacy/ledger.db --calendar original-calendar.json --input reviewed-evidence.json --expected-hash SHA256 --target repaired
```

`SIMNOW_FLAT_RECOVERY/1` 输入来自审核过的同账户完整柜台快照，绑定原库事件水位、原柜台日、快照 UTC 时间和原始证据 SHA-256。提供账户资金字段、空的持仓列表和相互关联的两笔终态委托及逐笔。保留原始 ID、合约、方向、开平、手数和纳秒时间，以及委托限价和成交价。以规范 JSON 摘要锁定审核后的输入。

此入口处理原执行器已提交但尚未入账的一笔手动开仓，以及同日同合约由外部执行器完全平今的对应仓位。旧库已存的开仓观察和柜台逐笔须完全吻合。原开仓走原内核成交写入，外部平仓保留柜台实际价量、原始 ID 和时间，按外部仓位减少结案。佣金仍需单独的逐笔证据。其他未核责任阻止输出。

成功后输出 `repaired/ledger.db` 和 `recovery-receipt.json`。原账本保持只读，输出停开。以修复副本和新水位重新跑 `migrate`。柜台日、适用配置及策略观察路径仍须单独核对。

## 发布依赖

本阶段使用离线快照与原版静态取证。向运营交付前需完成新控制器真实 SDK 接入、同场对跑、授权模拟、Windows 制品及自动校时维护窗口验收。实际旧账本归档、真实柜台状态复核和生产控制者交接依照 M0–M9 执行。发生新业务后回旧版的反向转换和合法重置窗口须另行验证。

## 旧操作日志归档和逐轮对照

```powershell
python -m online_trader offline-dsv archive-log --source legacy.sqlite --runs-root legacy-runs --boot-id boot-... --input profile.json --target recovered-run
python -m qa.dsv_parity.compare_trace --input rounds.jsonl --expected expected.jsonl --candidate replay.jsonl --candidate-release COMMIT
```

归档从指定旧启动的身份文件、账本登记及可用恢复回执核对来源，在新的目录创建 `operation-log.jsonl` 和 `archive-receipt.json`。缺失 `finalized` 的旧回执按可恢复水位处理。输出只包含可从已提交事件重建的内容，收据标注不可恢复的辅助诊断。

对照器针对固定输入、预期和固定候选轨迹。预期首行 `type=DSV_TRACE_EXPECTATION/1` 含 `reference_release` 和 `input_sha256`。后续每行用 `type`、`input_id` 和 `evidence` 定位预期，提供有序 `decisions` 和 `effects`。末尾 `SNAPSHOT` 使用 `input_id=snapshot` 和 `state`。未知预期写 `pending` 和理由。对象按列出的字段对照，数组按顺序及数量对照，随机生成的身份以 `{"$id":"name"}` 在各轮一一绑定。输出给出首处分歧、对应字段路径和未定项。
