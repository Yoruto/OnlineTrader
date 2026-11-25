# OnlineTrader 开发规范

OnlineTrader 是独立 Windows 产品。G0 原始规格索引见 [spec-source.json](file:///e:/online-trader/docs/spec-source.json)。
开发时必须保留既有工作成果；严禁直接导入 V7、vnpy、DSV 原版程序及其凭据、数据库或运行环境。

---

## 1. 默认运行与开发路径

未明确指定执行路径时，启动、重启、用户验收、测试候选和打包一律默认采用 **Standalone Trading Runtime**：
- 普通用户入口：`online-trader-desktop --runtime-root <目录>`，或配置便携版直接双击 `OnlineTrader.exe`。
- 统一入口先附着已有 Runtime，缺失时以 `DISARMED` 启动 Owner，再打开同一个 `WorkbenchWindow` / `WorkbenchLayout`。
- `runtime run` 和 `--runtime-pipe` 仅保留为运维及高级附着入口；CLI 不提供交易命令或无人值守调度，禁止另建第二套界面。
- 仅当用户明确要求 **Legacy 维护**、**Replacement candidate** 或 **Isolated QA** 时，才切换对应路径并在报告中标明（各路径入口与边界见 [architecture.md](file:///e:/online-trader/docs/architecture.md)）。

---

## 2. 按任务定位文档与源码

| 任务类型 | 必读文档 | 说明 |
| --- | --- | --- |
| 架构与模块定位 | [architecture.md](file:///e:/online-trader/docs/architecture.md) | 确认执行路径、模块入口与禁止越权边界 |
| 执行、订单、持仓收敛、权限、时钟 | [runtime.md](file:///e:/online-trader/docs/runtime.md) | 单 Owner/Sender、临时 Journal、收敛与市场门禁 |
| DSV 策略规则与参数 | [strategy.md](file:///e:/online-trader/docs/strategy.md) | B01–B20 基准、慢部分成交差异、纯逻辑边界 |
| Qt 界面与工作台交互 | [workbench.md](file:///e:/online-trader/docs/workbench.md) | CommandBus、过期快照放行、柜台核对提示、托盘退出 |
| 配置、缓存与持久化 | [storage.md](file:///e:/online-trader/docs/storage.md) | 柜台权威、临时执行库、可重建历史缓存、对账单边界 |
| 测试与验收 | [testing.md](file:///e:/online-trader/docs/testing.md) | 编译命令、离线 Profile、独立验收记录要求 |
| 打包与更新 | [delivery.md](file:///e:/online-trader/docs/delivery.md) | 随包配置清单、白银规则、更新载荷与交付核对 |
| 诊断与排障 | [diagnostics.md](file:///e:/online-trader/docs/diagnostics.md) | 诊断旁路、事件模型与脱敏边界 |

> [!IMPORTANT]
> 若发现文档合同与当前源码冲突，立即报告 `CONFLICT / NEEDS_REVIEW`，不得静默采用旧结论或通过改文档掩盖源码缺陷。

---

## 3. 交易业务与安全红线

1. **单 Owner / 单 Sender / 单 SQLite 写者**：同一台 Windows 主机最多一个 Runtime；不同主机不互斥（允许同账户多机按柜台规则运行）。每次启动创建进程所有的临时执行库，退出即删除，不跨 boot 交接。严禁把 SDK/下单/撤单/保护逻辑移出当前 Owner。
2. **期货账户权威与 `DISARMED` 安全**：柜台为持仓、委托、成交、资金的最终权威。`DISARMED` 期间只同步事实、推算目标并预览，严禁自动发单或撤单。
3. **`commit-before-send` 与 `UNKNOWN` 占量**：每次发单/撤单前必须先写入临时 Journal；调用结果未知（`SEND_POSSIBLE` / `UNKNOWN`）时必须保留最大剩余占量并等待核对。所有退出路径共享同一剩余目标；撤单请求未终态前不释放占量，GUI/策略生命周期不得删除保护。
4. **控制通道隔离**：交易控制仅通过同一 Windows 交互用户的 Workbench 通道提交；CLI、公开 API、委托工具仅只读。旧 `runtime-control.json`、`private/operators.json`、watchdog 与 handoff 文件一律忽略。
5. **参数计算与模板预览边界**：33 项模板、批量 CSV 导入导出、偏移计算器（`offset_calc`）仅返回预览结果，不得直接写入交易权限或覆盖生效配置/保护计划；计算与生效绑定数据哈希与预览哈希，不得以今日未完结 K 线作回退。
6. **外部对账单只读边界**：对账单比较只读；仅在 `ONLINE_SIM`/开发账本且 Owner 关闭、周期全部平仓、委托终态一致时允许原子登记费用与回执，不改写成交价量或保护，且保持 `DISARMED`。
7. **路径隔离原则**：不得因 Legacy 路径存在某些检查而将其强行引入 Replacement/Standalone，也不得在维护 Legacy 时擅自移除其原有检查；QA 注入端口严禁接入在线生产模式。

---

## 4. 用户授权范围

默认选择 Standalone 路径**不授予**以下操作权限，以下动作均须本次会话用户的明确授权：
- 连接模拟或实盘期货账户；
- 执行真实或模拟报撤单；
- 新增、调整或运行测试套件；
- 安装系统服务、执行打包构建、发布 Release 或切换生产版本。

---

## 5. 测试与交付约束

### 5.1 测试执行规范
- **日常源码改动后只运行编译检查**：
  ```powershell
  .\.venv\Scripts\python.exe -X utf8 -m compileall -q src tests qa
  ```
- 未经明确要求测试或打包，**不得擅自新增、修改或运行测试**。
- **每次获准打包前**，必须先向用户确认是否运行该包对应的全量测试：
  - 若用户批准全量测试：构建前独立运行全局产品测试（`.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v`，`PYTHONPATH=src`），构建后对最终制品做独立验收；
  - 若用户明确拒绝测试：可跳过构建前测试与构建后功能验收，但必须在交付说明中**明确标明未测试**、列出省略项，不得宣称测试或验收通过；非测试类的制品完整性、配置内容核对与 SHA-256 校验仍须执行。
- 每次打包/测试选择仅对当次有效，不得沿用过往授权。验收声明必须写明完整候选 SHA、场景/fixture、运行环境及 `PASS / FAIL / SKIP / NOT_RUN` 计数。

### 5.2 随包配置与完整制品交付
- **本机私有账号与令牌优先从 `.local/` 查找**：极狐发布令牌、自动更新只读令牌、模拟期货账号等本地私有凭据统一放在仓库根目录被 `.gitignore` 忽略的 `.local/` 下（如 `.local/jihulab-release.json`、`.local/jihulab-release.token`、`.local/jihulab-read.token`、`.local/jihulab-owner.token`、`.local/sim-futures-accounts.txt`）；需要相关凭据时优先从 `.local/` 读取，严禁提交到 Git、严禁打印明文、严禁将 `.local/` 整体打入制品。
- **完整交付制品必须随包带齐配置**（首次/全量安装器、便携包 `RuntimeConfigured`、客户包、测试包均适用）。禁止交付缺配置、空配置或占位配置包；含账户口令的制品只通过获准私有渠道交付。
- **受管程序更新载荷（`ONLINE_TRADER_UPDATE_PAYLOAD/1`）例外**：仅含程序本体与 `update-payload.json`（`standalone: false, requires_managed_root: true`），严禁包含 `runtime/`、`OnlineTrader-defaults.json`、`OnlineTrader-parameters.json`、`OnlineTrader-overrides.json`、`update-source.json`、`active.json`、`OnlineTrader.ini`，以免覆盖用户既有配置；单独解压直接启动须 fail-closed 拒绝运行（详见 [delivery.md](file:///e:/online-trader/docs/delivery.md)）。
- 独立 Trading Runtime 新交付必须显式带入 `runtime.json` 及已确认的 `OnlineTrader-defaults.json`、`OnlineTrader-parameters.json`（不得依赖已废弃的 `runtime-control.json` / `private/operators.json`）。
- **白银合约规则**：正式交付物允许随包配置白银（含白银主连及上期所 `ag` 具体合约月），适用与其它品种相同的配置校验要求；不得仅因存在白银配置而停止交付，也不得未经确认擅自增删或替换白银合约。
- **交付前检查最终制品本身**：对 ZIP 检查归档内容，对安装器在隔离目录安装验证，使用用户实际启动入口验证配置可读取，记录版本、制品 SHA-256 与配置核对结果（省略口令等秘密）。任一检查失败则停止交付。

---

## 6. 文档更新要求

仅当代码修改**改变了其他开发者需要遵守的架构合同、业务规则，或改变了用户需要了解的使用/测试/交付方式**时，才同步更新对应的 `docs/*.md` 文档。普通的 Bug 修复、内部辅助函数重构、性能优化、单测补充不要求修改正式文档。
