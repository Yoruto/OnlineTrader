# OnlineTrader 桌面操作习惯与应用生命周期改造方案

> 2026-11-20 · 设计基线：`9817f11a4b03203753df30134d5a3d807ab24f07`（main）  
> 交付范围：**只修改用户操作流程及后台生命周期；现有 WorkbenchWindow、WorkbenchLayout 的视觉、导航、页面结构、尺寸、控件布局与风格全部保持不变。**  
> 关联：[Runtime/Workbench 架构收敛](runtime-architecture-hardening-plan-20261120.md) · [Trading Runtime](standalone-trading-runtime-20261119.md) · [客户端重构](trading-workbench-client-refactor-20261119.md) · [操作与持久化](../runtime-operations-and-storage-20261119.md)。  
> 状态：**待实现设计**；本次仅文档 PR。不连接账户、不测试、不打包、不终止运行中的交易进程。任何实施 PR 需要对应代码审查、单独测试授权与产品发布验收。

## 0. 用户最终预期

用户只认识 **OnlineTrader 一款桌面软件**，不需要学习 Runtime、Owner、Named Pipe、CommandBus。

| 用户操作 | 目标交互 | 系统内部语义 |
| --- | --- | --- |
| 双击 `OnlineTrader.exe` | 直接打开原有工作台 | 已有 Runtime 先附着；没有则启动 DISARMED Runtime；完成权威 baseline 才展示可用状态 |
| 再次双击 EXE | 恢复/前置现有窗口 | GUI 单实例唤醒；不能创建第二 Owner 或重复启动策略 |
| 点击现有“启动策略/开始”按钮（包括既有快捷键） | 进行当前策略启动/授权，不让用户理解 ARM 状态 | 按原有业务前置条件检查，Runtime 授权成功后才提示策略可新增；参数/账户/保护门禁仍为权威 |
| 点击现有“停止策略” | 停止自动新增，已有仓位及必要保护继续 | 保持原 Kernel 的 `STOP_DSV_STRATEGY` 与保护合同；不是 Runtime 进程退出 |
| 点击窗口 X | 默认隐藏到托盘，任务继续；不弹退出确认 | 不向 Runtime 下 stop；不终止 SDK/保护；第一次可用托盘系统通知说明 |
| 托盘“打开/显示窗口”或双击托盘图标 | 恢复原工作台 | 保留原控件/页面/账号/排序；状态必须从 Runtime 更新 |
| 托盘“退出 OnlineTrader” | 用户意图是**最终退出本软件所有相关进程** | 进入安全停机检查与明确决策，成功后 Owner、View、Qt 均终止；不能假装 Qt 退出就算全部退出 |
| Windows 注销/关机 | 尽可能协作停机，无法完成时保证可恢复证据 | 由 OS 超时边界约束，不能声称一定安全平仓/完整退出 |

**禁止**重新设计主题、按钮位置、主页面、额外“新工作台”、弹出替代交易页面或把“应用启动”与“策略开仓授权”混为一谈。

## 1. 核对过的代码事实（本设计起点）

1. `desktop/entrypoint.py`：冻结配置包可识别相邻 `runtime/`，`ensure_runtime()` 已实现“附着 → 无实例则 DISARMED 启动”。保留该路径，不增加第二个 UI。
2. `desktop/tray.py`：已有 `minimize_to_tray=True` 默认偏好、窗口关闭时隐藏托盘、双击恢复；目前托盘菜单是“退出工作台”，`close_window()` 只关闭 Qt。
3. `desktop/workbench.py`：现有 `start_strategy()`、`STOP_DSV_STRATEGY` 和快捷键，保留原按钮；`closeEvent()` 先尝试托盘隐藏，后续仅清理 Qt worker/订阅，不负责 Runtime 停机。
4. `desktop/workbench.py` 的 `run()` 退出 Qt event loop 并不等于已结束 Runtime。窗口退出/重启更新路径已有异步 worker/托盘生命周期要求。
5. 当前运行态 `entry_authority` 为 `ARMED/DISARMED`，禁止为 UX 改名引入 schema `PAUSED`；连接、恢复、存储、时钟等正交事实不压成单一枚举。
6. Runtime 必须维持原一账户一 Owner、一 writer、commit-before-send、UNKNOWN 恢复、原有保护责任；任何普通窗口关闭事件都不能停止交易。

## 2. 用户流程与可见文案

### 2.1 双击与再次双击

```text
双击 OnlineTrader
  ├─ 工作台已有实例 → 激活窗口/从托盘恢复；不重复发起策略命令
  └─ 不存在工作台
       ├─ Runtime 可用 → 附着同一账号/同一 boot
       └─ Runtime 不可用 → 调用已有统一 bootstrap，DISARMED 启动
              ├─ baseline 合法 → 打开原 Workbench
              └─ 失败 → 显示可操作中文原因及诊断路径，不伪装已连接
```

现有 `AccountLock('DESKTOP:ONLINE-TRADER')` 拦截二次启动时，需实现**进程间唤醒请求**（本机用户边界 + 无交易能力），不能只显示已运行错误，也不能自动绕过 GUI 单实例锁。唤醒协议只允许 `SHOW_WORKBENCH`、`PING`，不提供交易命令；所有异常只能降级为准确提示，不能重新创建交易 owner。

### 2.2 开始与停止（保留原按钮与 F5）

- “开始”不是重新启动 OS 进程：已有 Runtime 下按原策略命令和前置条件进行启动/ARM；不得用 GUI 自己将状态改为 ARMED 或提前显示成功。
- 已有持仓保护、未知发送或未完成 Reconciliation 时，入口遵循 Kernel 裁决；失败在**现有反馈区**解释，不弹新交易界面。
- 停止策略要解释实际效果：“停止新增开仓/继续保护已有仓位”或根据当次明确指令的其他原有 stop 语义；不能误导为 Owner 已退出。
- 如果命令结果未知，只保留原 command_id 查询回执，不自动生成第二个 ID 重试；尊重旧生产 pending 保护与未来 CommandBus 改造门禁。
- UI 按钮是否可操作须与当前 Runtime 能力兼容；现有 Capability 投影不覆盖所有命令，不能直接切换未验收的 CommandBus。

### 2.3 X / 托盘

- 窗口右上角 X **默认**隐藏；程序仍运行，Runtime 不受影响。现有可配置偏好不能使有交易责任的运行态在未经“退出流程”时静默关闭 GUI 并让用户以为退出了整个软件。
- 托盘显示/双击只负责恢复窗口，不触发策略启动。
- 菜单文案只调整必要措辞：“退出工作台”→“退出 OnlineTrader”，**但只能在真实全进程退出协议落地且可验证之后**改名；在此之前必须保持真实语义，不制造虚假的“已全部退出”承诺。
- 首次 X 时可显示非阻塞系统通知“已最小化到系统托盘，交易服务继续运行”；通知不可包含账户秘密。当前启动或异常状态下也可恢复诊断页面。
- 已支持的 Windows 用户设置可保留，但需求优先级为“普通 X 不代表 Runtime 退出”，不能因为隐藏到托盘不可用就假装已安全停机。

## 3. 托盘全退出：必须先建设受控的 Shutdown Contract

**不能仅通过** `process.terminate()`、`taskkill /T`、枚举同名进程或 Qt `app.quit()` 实现用户所说的“退出全部进程”。现有 Runtime 可能由 scheduler、CLI、另一个工作台或本机其他 session 启动；启动者 PID 与 Windows 当前用户 SID 都不是可安全停机的充分授权。

### 3.1 确认对象

最小 `ShutdownReview`（由 Runtime 的权威快照和完整账本/柜台状态查询得到）：

- `account_key`、`kernel_boot_id`、账本身份、进程所有权/实例标识和来源（GUI 启动、CLI、定时任务）
- 策略运行/当前新增开仓权限
- 自有仓位、柜台原生持仓（含外部持仓）、在途订单/撤单、UNKNOWN/SEND_POSSIBLE、保护责任
- 柜台同步覆盖率、账本可读/可写、时钟/连接/恢复故障
- 已附着的客户端数或生命周期租约；`ShutdownReview` revision/过期时间
- 是否可安全退出、可执行的停机步骤、阻断原因及需要用户明确选择的风险

任何数据缺失、快照过期、Owner 不符、账本失败或柜台无法核对，不能默认为安全退出。

### 3.2 用户可见分支

| 状态 | 托盘退出操作 |
| --- | --- |
| 无自有/原生持仓、无活动/未知委托、无保护义务、状态完整可信 | 提交 `REQUEST_SAFE_SHUTDOWN`，等待 Runtime 正式进入 quiesced/stopped 后再退出 Qt |
| 策略运行但仓位与订单均清空 | 先阻止新开仓、完成必要同步和事实写入，收到明确 shutdown ACK 后退出 |
| 有持仓 / 活动订单 / 退出保护责任 | 默认取消退出/返回托盘；可明确选择“先平仓/撤单并核对后退出”（必须走原 Kernel 对应操作及真实成交确认），**不能以操作已发出当作清仓完成** |
| 有未知报单、未完成平仓、账本/柜台不可用 | 禁止宣称安全退出；明确告知未知风险、允许返回托盘继续处理 |
| 用户坚持停止服务并接受剩余风险 | **另设明确的高风险停机确认和受审计的授权策略**；仅在有可执行的受控协议后提供，不得偷偷 kill。关闭服务意味着软件自动保护停止，不能声称仓位会自动平掉 |
| Owner 属于 scheduler/CLI 或其他客户端共享 | 不能通过当前 GUI 无条件终止；必须明确所有权/授权与是否影响其他控制者 |

不允许用普通“退出”隐式选择“全部平仓”，也不允许在持仓风险尚未声明时隐式杀 Runtime。用户取消退出后窗口继续在托盘、原自动保护正常。

### 3.3 Runtime 端停机协议（新能力，需独立实施）

建议引入 `ShutdownCoordinator`，**不要把它写进 `WorkbenchTray` 或 Qt worker**：

```text
Tray EXIT
  → Runtime SHUTDOWN_REVIEW (只读、权威)
  → 用户选择（无风险时可省略二次确认）
  → REQUEST_SAFE_SHUTDOWN(account, boot, review_revision, request_id)
  → Runtime 取消新增进入请求、核对在途/UNKNOWN/持仓保护/交易日状态
  → 持久化 shutdown intent / event 与必要回执
  → 阻止新控制命令、停止 Owner SDK pump 并优雅释放账本 writer 和账户锁
  → View Process 按 Owner 退出而关闭，发布受控终态/本机协调 ACK
  → Desktop 验证已附着 Owner 和 View 全部真正退出
  → Desktop 关闭 Qt 和托盘图标
```

- 唯一合法停止目标为**已核实的同一 account/boot/实例**；不得按进程名称搜索并杀死无关进程。
- 不能仅依赖在结束前即将失效的 View 通道 ACK；需建立进程/服务端停机完成可核对结果与超时/失败恢复处理。
- 设计幂等 request ID、重复点击、Qt 随时关闭、Owner 在停机中崩溃、CLI/scheduler 并发启动、无法刷新状态等竞态测试。
- 若 Runtime 无法停止，UI 必须保持/恢复错误提示，明确“交易服务仍在运行”；不能让用户误解已经完整退出。
- `EXIT` 与 `RESTART_FOR_UPDATE`、`MINIMIZE`、`CLOSE_UI_ONLY` 是**不同生命周期意图**。自动更新路径不得自动触发强制交易退出；有交易责任时升级需要单独安全门禁。
- 当用户最终确认高风险停机时，确认操作、风险摘要与服务端最终结果应保留为审计事件（不保存秘密）。

## 4. 实施分批与可验收退出条件

### UX-A：桌面交互梳理与 GUI 单实例唤醒（不改布局）
- 将`SHOW_WORKBENCH`本机唤醒加入桌面单实例路由，覆盖再次双击/隐藏托盘/已有 Owner/账号不一致。
- 现有“开始/停止”按钮和 F5 保留，梳理当前文案与反馈，不重建 UI；明确 STOP 与 EXIT 的差别。
- 现有 X 默认托盘行为回归；保留用户偏好但禁止“关闭窗口误当全退出”的路径。
- 不触碰订单执行、账本 schema、SDK、配置格式。
- Exit Gate：相同 Windows 用户重复双击只出现一套工作台，Runtime owner 无重复；X 只隐藏，策略/保护继续；所有原业务按钮位置保持不变。

### UX-B：Runtime 安全停机协议与可验证进程退出（高风险，独立 PR）
- 引入只读 ShutdownReview，定义可安全退出/阻断的准则与过期语义。
- 引入 owner 受理的 `REQUEST_SAFE_SHUTDOWN`，与账本事实和原操作授权对齐；非交易确认的本地 GUI 通道不自动拥有超出原授权的新能力。
- 增加“退出后所有本软件归属进程结束”的进程所有权与退出完成证明；区分用户启动和无人值守 Owner。
- 缺少任何退出证明都拒绝 UI 假成功；保留 `runtime run`、`runtime schedule` 运维与恢复路径。
- Exit Gate：无风险退出仅终止目标 Owner/View/Qt，其他账户无影响；故障与风险分支不越权、不丢 UNKNOWN、不删除 WAL。

### UX-C：托盘退出接入与 Windows 用户端验收（独立 PR）
- `WorkbenchTray.close_window` 调整为 `request_application_exit`，等待 UX-B 安全协议；真正完成后再显示“退出 OnlineTrader”。
- 使用现有 QMessageBox/反馈区域提供必要确认，不改页面布局；绝不把 UI 关闭当作 Runtime 停止。
- 启动时、退出时、订单在途、重复点击、最小化、Qt 强杀、Runtime/View 独立重启、自动更新与 OS 关机分别验收。
- Exit Gate：托盘退出完整关闭当前软件受控进程；无法验证时精确提示哪些进程仍运行。

### UX-D：可选的“工作台退出但服务继续”高级操作（未来）
普通用户只看到“X 到托盘”和“托盘退出整个软件”。若 7×24 无人值守服务需要脱离 GUI 存活，可在运维入口提供**明确独立命名**的“仅关闭工作台/保持 Runtime”操作；不可将其伪装为“退出 OnlineTrader”。

## 5. 必须分开的门禁与授权

- **源码合并门禁**：按本次获准范围做静态审查；Unit/Integration/Windows Qt 的实际执行均需要当次授权，并逐项记录 `PASS/FAIL/SKIP/NOT_RUN`；未测不能记通过。
- **发布/制品门禁**：Windows 便携目录/ZIP 双击、SimNow（如果需要）与真实交易故障演练分别获得明确许可；不能因源码 PR 合并而默认可做。
- 建议维护 UX 操作矩阵：`UI 事件 → 本地状态 → Runtime API → Capability/Allowlist → 是否写账本 → 失败反馈 → 测试`。
- 此方案和既有 WP0/WP1 的安全、Capability 覆盖与 CommandBus 未接管事实一致；不把非生产 CommandBus 当可直接使用的安全底座。

## 6. 关键保护原则（实施期间不可妥协）

1. **操作习惯改变，不改变 DSV 策略决策与持仓保护逻辑。**
2. **开始策略与启动 Runtime 分离；停止策略与退出全部进程分离。**
3. **关闭窗口不终止交易服务；托盘全退出须以 Runtime 真正退出为结果，并保留风险确认与失败可见性。**
4. **UNKNOWN、订单占量、外部持仓、恢复义务与 WAL 绝不能被 GUI 的“退出成功”覆盖。**
5. **不改任何 UI 样式、页面布局、布局文件或主题。** 必要的确认弹窗/托盘菜单文字属于交互层，不是界面改版。

## 7. 当前已知限制与验收状态

2026-11-23 已落地实现“托盘「退出 OnlineTrader」（或取消托盘驻留后关闭窗口）→ 受控安全停机并等待 Owner/View 退出”：无持仓/活动委托时自动提交 `FLATTEN_THEN_STOP` 并等待 PID 与视图进程真实退出后再退出 Qt；有持仓/在途委托/未同步故障时弹窗确认「全部平仓并退出」或取消返回托盘；停机超时/被拒或管道失联而 PID 仍存活时拒绝退出并保持界面打开。当前新增退出路径需经授权实包/实盘演练后方记为完整产品验收。

