# 产品功能与桌面工作台交互

Trading Workbench（`WorkbenchWindow` / `WorkbenchLayout`）是 Standalone Trading Runtime 的唯一图形控制面，定位为**只读投影与本机控制客户端**。

---

## 1. 入口与单实例行为

- **统一启动**：`online-trader-desktop --runtime-root <目录>` 或便携版双击 `OnlineTrader.exe`。
- **单机单实例**：同一 Windows 主机最多运行一个 Runtime 与一个附着的 Workbench；重复启动只唤醒已有窗口，不得绕过锁另建 Owner 或控制面。
- **同用户通道安全**：Named Pipe 控制通道仅接受启动 Runtime 的同一 Windows 交互用户 SID（拒绝 `LocalSystem` 等其他 SID）。UI 不展示或读取操作员 ID/令牌。

---

## 2. 命令总线（CommandBus）与状态边界

```text
用户操作 ─► 页面校验快照与 Capability ─► CommandBus (进程内关联, 上限64条)
  ─► Named Pipe ─► Runtime 重新核对(账号/boot/revision/能力/风险) ─► Owner 执行/拒绝 ─► 按原 command_id 回执
```

- **进程内关联**：`CommandBus` 仅在当前 Workbench 进程内保留未决命令关联，不写入 `QSettings` 或注册表；Workbench 或 Owner 重启后重新拉取基线，不继承旧进程 pending 责任。
- **超时不重发**：超时或断线不等于失败，只允许查询原 `command_id`，严禁自动重发。
- **乐观并发（OCC）**：`MANUAL_CLOSE` 与 `MANUAL_CLOSE_ALL` 必须携带 `target_revision`（缺失报 `E_SCHEMA`，过期报 `E_REVISION_CONFLICT`）。
- **运行中热保存**：支持在运行中提交 `SAVE_DSV_PARAMETERS` 热更新策略参数（经 Runtime 校验并递增配置 revision）。

---

## 3. 投影状态、过期放行与柜台核对提示

- **全账户一致展示**：首页统一展示“账号实际持仓（Actual） vs 策略目标持仓（Desired/Target）”及待收敛动作（含非配置合约待清仓项），不将外部持仓标为阻挡故障。
- **投影过期与 `OWNER_RECHECK_OPERATIONS`**：
  - 当客户端处于 `STALE` / `SYNCHRONIZING`（界面未更新/正在重连）时，阻止开新仓、启动策略、修改参数等依赖最新完整快照的操作；
  - 若已有目标归属基线且未断连/存储故障（`E_STORAGE`），仍放行降风险操作（停策略、暂停开仓、撤单、人工平仓、全平并停止、重新核对），由 Owner 按最新实态裁决；
  - 客户端不得把界面投影过期误判为“Owner 行情滞后”；仅当 Owner 明确返回 quote-age 失败或 `DSV_QUOTE_AGE_EXCEEDED` 时显示行情滞后。
- **四档故障与核对引导**：明确区分自动同步（无需操作）、持久 Owner/时钟故障（显示具体原因与“重新核对”）、本机 `UNKNOWN` 委托（保留占量责任、引导查看委托而不诱导重发）及非对齐/外部持仓。
- **断线重连提示**：运行中失联时底部显示已等待时间与 30 秒重试倒计时，支持“立即重连”查询原执行器状态。

---

## 4. 托盘与退出生命周期

- **关闭窗口（`X`）**：默认最小化到托盘，Runtime Owner 继续在后台维持订单与保护责任。
- **显式退出**：从托盘选择「退出 OnlineTrader」（或关闭托盘驻留后点 `X`），触发受控安全停机核对：
  - 若无持仓与活动委托，自动安全停机；
  - 若存在持仓或在途委托，需用户确认「全部平仓并停止（`FLATTEN_THEN_STOP`）」；
  - Qt 进程必须等待目标 Owner 与 `runtime-view-process` 完全退出后才结束。若用户取消或停机失败，保持窗口与原保护不变。
- **子进程隔离**：Workbench 或 `runtime-view-process` 崩溃时，Owner 独立保持既有责任；`runtime-view` 按有界策略重启，绝不新建第二个 Owner。

---

## 5. 参数中心、模板与偏移计算（只读预览边界）

- **33 项模板与批量 CSV**：模板应用与整批合约 CSV 导入/导出仅生成界面预览值，不直接授予交易权、修改生效配置或覆盖保护计划；模板编辑要求显式预期 revision，并在账本外追加不可变局部历史。
- **DSV 偏移计算器**：基于显式离线日线序列标识与截止交易日计算，严禁推断实盘行情完整性或以今日未完结 K 线作回退；计算与参数应用均绑定数据哈希与预览哈希。
