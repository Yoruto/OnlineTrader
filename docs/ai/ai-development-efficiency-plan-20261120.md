# OnlineTrader AI 辅助开发效率优化计划（V1.2）

> 日期：2026-11-20  
> 文档状态：**实施计划 / 第一批已落地，工具效果待测量**
> 仓库：[`Yoruto/OnlineTrader`](https://github.com/Yoruto/OnlineTrader)  
> V1.2 静态核对基线：`8fd412e1f960cc0a8c3b23b8ca68f289f5db49d2`；后续实施仍须重新确认 HEAD 与工作区变更。
> 目标：提高 Codex、OpenCode、Cline 等 AI 工具定位、理解、修改、审核 OnlineTrader 源码的效率与准确性。  
> 范围：**仅开发工具、规范、离线静态分析和可选审核交接**；不修改交易业务逻辑、账户连接、启动授权、数据库架构、打包流程或 CI/CD。

## 0. V1.2 修订摘要

| 评审发现 | V1.2 决定 |
| --- | --- |
| 旧文档可能把长期每账户 Ledger 当成当前 Standalone 的权威 | 按最新柜台权威设计区分 Standalone / Legacy / Replacement，增加文档状态与生效范围 |
| 一个文档可能只有部分作用域过期 | `architecture-status.yaml` 以 `path + scope` 标识记录，不用单一文件状态掩盖局部替代 |
| Serena、Aider Repo Map、自研 Router 职责重叠 | 先完成权威清单与 Skills，再在干净源码副本中验证 Serena；Context Router 继续暂缓 |
| Skills 和配置重复维护 | `docs/skills/online-trader.md` 保持规范源；首批只建 Runtime/Workbench 与 DSV 两个短入口 |
| 订单安全不能依赖 Skill 是否触发 | 强制安全不变量继续放在 `AGENTS.md`，不建立可选的 `order-safety-review` |
| 八个任务无法严谨支撑 95% 准确率 | 先核对 B01/B04/B05/B08 参考答案；用关键误判数和耗时中位数评价，稳定后再扩展 |
| Python AST 可能遗漏动态依赖 | 影响分析加入 `UNKNOWN / MANUAL_REVIEW_REQUIRED`，不自动证明交易安全 |
| 多 Agent、Repomix 引入过早 | 放到可选阶段，先证明交接瓶颈 |
| 忽略规则不是秘密隔离边界 | Serena 只能试点于不含运行数据、凭据和数据库的干净源码副本；不继承运行账号与交易权限 |

第一批已加入文档权威清单、模块边界、四项参考任务和两个项目 Skill。它们不表示软件已上线、功能测试已通过或 Serena 等新工具已安装。

## 1. 当前架构基线与事实优先级

### 1.1 必须首先识别执行路径

| 路径 | 当前约束 | AI 容易误判的旧结论 |
| --- | --- | --- |
| **Standalone Trading Runtime（默认）** | 同一台 Windows 主机最多一个 Standalone Runtime；不同电脑可以按柜台规则同时使用同一期货账户；柜台/SDK 是当前持仓、订单、成交、资金的业务权威 | 错误要求“每账户跨电脑单 Owner/账本交接” |
| Standalone 会话执行状态 | 本次 boot 使用进程拥有的临时 SQLite `ledger.db`；用于会话内事务和责任，不作为跨 boot 的历史权威 | 将旧每账户长期 Ledger 当成默认恢复来源 |
| Standalone 历史缓存 | `account-history.db` 仅承载可从柜台取得的历史快照/收益缓存；缺失日期不得伪造覆盖 | 将历史缓存当作 Sender 或订单归属证据 |
| Standalone 配置 | 版本默认 `OnlineTrader-parameters.json` + 用户显式覆盖 `OnlineTrader-overrides.json` | 继续套用旧的配置-账本联动交接约束 |
| Standalone Workbench | CommandBus 与 Runtime 权限校验负责本次会话；客户端 pending 为进程内关联；恢复后的业务事实以 Runtime/柜台同步为准 | 自动恢复已退役的跨 boot QSettings pending 设计 |
| Legacy（显式维护） | 保留旧执行/原账本恢复合同，必须单独评估 | 将 Standalone 的临时库规则反向应用于旧路径 |
| DSV Replacement Candidate | `DsvSession / ExplicitOrderController / Journal / DsvSdkAdapter` 的独立事实与恢复合同，按冻结 DSV 行为核对 | 因文件/类名相似而统一控制者或改写 Journal |
| Isolated QA | 回放与固定输入仅在隔离 QA 范围 | 将回放入口当成可在线发送的正式交易接口 |

当前优先阅读：

1. [本机单实例与柜台权威设计](../refactor/runtime-broker-authority-single-instance-20261120.md)
2. [当前功能实现与开放问题](../功能实现与待办清单_20261110.md)
3. [Runtime / Workbench 架构计划](../refactor/runtime-architecture-hardening-plan-20261120.md)（部分小节保留历史基线，需结合现行设计）
4. [DSV 对齐索引](../refactor/dsv-parity-20261114/README.md)（仅 DSV/Replacement 任务）
5. [项目全局 Agent 约束](../../AGENTS.md)

源码证据与已批准的最新合同优先于旧方案。文档冲突时输出 **CONFLICT / NEEDS_REVIEW**，不得悄悄选取最方便实现的版本。

### 1.2 文档状态机制

当前维护 `docs/ai/architecture-status.yaml`，记录关键文档在具体作用域内的：

- `path`、`scope`、`status`、`superseded_by`（可选）、`verified_at_sha`、`evidence`、`notes`；记录身份为 `path + scope`。
- 状态取 `CURRENT`（当前约束）、`CANDIDATE`（待验证实施）、`SUPERSEDED`（已替代）、`HISTORICAL`（历史证据）。
- **设计批准**、**源码已实现**、**测试已执行**、**产品已验收**为不同维度，不能用单一“完成”状态混淆。

`verified_at_sha` 说明核查时的源码版本，不是永久正确的保证。上下文工具读取当前 `HEAD`、工作区修改和文档状态，遇到冲突给出警告。

## 2. 目标、范围与衡量方式

### 2.1 可交付能力

- 任务开始时知道 **当前执行路径、相关源码入口、当前有效文档、不可破坏的合同**。
- 能按符号、定义和引用定位 Python 源码，减少整文件读取。
- 在明确范围内加载相关 Skills，而非所有历史规则。
- 能针对 Git Diff 生成**保守**的受影响模块、协议、测试建议。
- 工具失效时能退回 `rg` / Git 搜索 / 直接源码阅读。
- 所有开发辅助工具在生产软件与交付制品之外运行。

### 2.2 先测基线，再宣布效率收益

初步观察指标（不是承诺值）：首次定位耗时、读取文件数、可取得时的 token、关键执行路径误判和返工次数。四项小样本不使用百分比准确率门槛；先要求零关键路径误判，再观察多轮耗时中位数是否有稳定改善。

记录每个任务的：实际 Git SHA、模型/推理设置、Agent 客户端版本、任务顺序、是否冷缓存、定位耗时、读取文件数、tokens（能测则测；否则标 N/A）、正确路径率、误判与返工、实际工具调用、人工复核意见。相同任务做多轮对照，固定相似环境并交换执行顺序，以降低缓存与熟悉度影响。

## 3. 阶段 0：架构核对与 Benchmark 基线（所有 PR 的前置条件）

**第一批交付物（已加入工作区）：** `docs/ai/README.md`、`architecture-status.yaml`、`module-boundaries.yaml`、`benchmark-tasks.yaml` 与静态基线报告；不引入新开发服务。

首批 4 个只读/离线代表任务：

| ID | 场景 | 核查重点 |
| --- | --- | --- |
| B01 | 定位 Workbench 某交易按钮到 Runtime 命令提交的调用链 | 符号与跨模块定位 |
| B04 | 比较临时会话执行库与 `account-history.db` | 当前/历史状态所有权 |
| B05 | 定位 DSV 策略决策与 SDK 适配调用路径 | 路径与规则隔离 |
| B08 | 查找被标为 SUPERSEDED 的旧文档并识别新权威 | 知识新旧判定 |

参考答案已经人工静态核对，受控效率测量仍为 `NOT_RUN`。四项出现稳定收益后再增加 B02（pending/receipt）、B03（Bootstrap 生命周期）、B06（IPC/Capability）和 B07（验证授权）。

**阶段 0 Exit Gate：** 任务与正确答案（文件/符号/合同）经人工审核；基线具有可复现实验说明；记录不含账号、Token、订单流水等私有事实。仅以静态检索评价，不开展新的模拟柜台测试。

## 4. PR-02：Serena MCP（Skills 后按条件验证）

### 4.1 职责

Serena 是**可选的开发侧语义导航器**，承担符号索引、定义、引用、局部代码阅读；不用于运行时行情、委托、执行状态查询；不构成交易安全自动证明。

### 4.2 安装、初始化和版本

只能在不含运行配置、凭据、日志、数据库、WAL/SHM 或行情缓存的干净源码 clone/副本中，按 Serena 官方指引执行：

```powershell
uv tool install -p 3.13 serena-agent
serena init
cd <OnlineTrader源码目录>
serena project create
```

对已有源码，Serena 可自动识别语言；若无法识别或需多语言支持，查阅所安装版本的 `serena project create --help` 再指定 `--language`。为目标 Agent 按官方客户端说明单独配置 MCP 启动方式，不把客户端配置样例直接当作所有客户端通用配置。

本机成功验证后，将 **实际版本、安装源/哈希、MCP 启动方式、语言服务器版本、Python 环境** 写入 `docs/ai/toolchain-versions.md`；生产 `pyproject.toml` 不增加 Serena 依赖。升级须先在独立工作区验证兼容。

### 4.3 安全边界

- 默认先做**只读定位试点**；限制可用工具和代码工作区。禁用不必要的命令执行、文件编辑、远程监听/任意路径访问能力。
- 注意：MCP 工具层“只读”配置本身不是 OS 安全沙箱。对需要强隔离的情况使用独立权限较低的用户/容器/副本和操作系统文件权限；不要挂载运行账户、凭据目录或真实本地账本。
- `.gitignore`、Serena ignore 和项目排除规则只减少普通遍历，不视为秘密隔离边界。试点副本本身不得包含 `.venv/`、`output/`、`runtime/logs/`、`runtime/private/`、数据库/WAL/SHM、行情 Tick 缓存、含凭据的配置与构建制品。
- 默认使用本地 stdio，不开放可被其他主机访问的服务器。
- 仅当用户明确授权源码修改时，才允许编辑能力；即便可编辑也不得获得模拟盘连接或报单权限。

### 4.4 验收

- 在代表性 Python 模块上查到定义、类/方法和跨文件引用。
- 动态反射、Qt Signal/Slot、字符串路由等识别不全时显示限制并退回全文搜索。
- 切断 Serena 后主开发流程仍可继续。
- 相对阶段 0 真实数据评估是否节省定位时间/上下文，**没有收益则停止扩容**。

**回退：** 禁用本机 MCP/Serena 配置，不影响运行源码与发行包。

## 5. PR-01：Agent Skills 与轻量知识入口

### 5.1 单一规范源

保留 `AGENTS.md` 中所有全局安全不变量、默认执行路径与测试/发布授权。只增加简短导航，避免把长篇历史文档重复贴入全部任务上下文。

首批创建以下 2 个专项 Skill：

| Skill | 触发 | 要求 |
| --- | --- | --- |
| `runtime-workbench-development` | Runtime/Bootstrap/IPC/配置/历史缓存/Qt/CommandBus | 本机单实例、柜台权威、会话临时库、GUI 非 Sender、本会话 pending |
| `dsv-parity-review` | DSV 核心/Replacement/SDK 回放 | 冻结基准、原生事实、明确批准差异、对照执行轮次 |

订单安全、测试授权和打包约束继续由 `AGENTS.md` 强制提供，不依赖可选 Skill 是否被正确触发。`ledger-development`、`order-safety-review` 和 `release-review` 暂不独立新增；只有出现多次重复任务并能减少误判时再扩充。

### 5.2 目录与客户端差异

`docs/skills/online-trader.md` 继续作为 OnlineTrader 业务与交付规则的唯一规范源。`.agents/skills/<skill-name>/SKILL.md` 只维护短工作流、触发条件和指向当前合同的路由，不复制或改写完整交易规则。其他客户端的加载路径按当前安装版本实测；必要时生成适配副本，不能建立平行业务权威。

Skill 正文仅包含：触发条件、适用/禁止路径、需要加载的当前合同、常见入口、风险检查、受授权约束的验证建议及退回方法。不复制长篇 AGENTS 或设计文档。

**Exit Gate：** 两类任务能正确载入对应 Skill；默认 Standalone 不误用长期账本/handoff；交易安全约束仍由 `AGENTS.md` 覆盖；跨工具调用得到一致的业务边界。

**回退：** 禁用新增 Skills、保留原 `AGENTS.md`。

## 6. PR-03：最小 Context Router 与影响分析（按数据决定范围）

### 6.1 最小可用功能（MVP）

先维护 `docs/ai/module-boundaries.yaml` 中关键**人工确认**的入口/路径边界，再规划 `scripts/ai/context.py` 与 `scripts/ai/impact.py`；只有当 Serena 和 Skills 尚不能消除明显的重复劳动时再开发脚本。

Context 输出字段：

- `task`、`route`、`head_sha`、`dirty_worktree`、`changed_paths`；
- `applicable_contracts`、`superseded_warnings`、`source_entries`、`symbols`；
- `risk_level`、`unknown_dependencies`、`suggested_checks`、`authorization_state`。

全局安全约束与当前权威合同不能因为 tokens 预算而被裁掉。脚本不得改变代码、读取私有账户或发起 SDK 连接。

### 6.2 Repository Map：条件性建设

**不在首批强制自研完整 Repo Map。** Serena 解决符号级定位；若实测缺少稳定的全局结构概览，才考虑基于 Python AST + 导入信息生成简版模块地图。它只能声称“可静态观察到的符号/导入”，不能宣称包含完整运行时调用图，也不重复实现 Aider 的图排名系统。

地图可缓存，但缓存必须考虑 `HEAD`、未提交工作区变化和脚本版本；不可索引秘密与运行数据；可再生输出不提交 Git。

### 6.3 风险识别与失败语义

| 分类 | 典型范围 | 处理 |
| --- | --- | --- |
| R0 | 文档和无行为变化的展示内容 | 常规走读 |
| R1 | UI 展示、非交易工具 | 模块相关检查 |
| R2 | IPC、配置、生命周期、缓存及协议 | 合同/边界复核 |
| R3 | 下单、撤单、订单所有权、交易授权、恢复、执行事实 | 人工安全审核与明确验证范围 |
| **UNKNOWN** | 动态依赖、变更扩散不明、文档/源码冲突、静态工具识别失败 | **MANUAL_REVIEW_REQUIRED，不得降成 R0/R1** |

影响识别覆盖 Python、IPC JSON Schema、资源定义、PowerShell、安装脚本、SQL 和测试配置；`ast` 只是一部分，不可凭“没有引用”证明无影响。

### 6.4 检查执行权限

本地脚本可提供 `syntax`、`structure`、`impact` 的静态建议/检查。项目现有约束规定常规改动只做编译检查，**不得自动增加、修改、执行测试**。定向测试、全量回归、Qt/Named Pipe、SimNow、打包和发布都需要用户对此次任务的明确授权；没有授权时只输出 `SUGGESTED / NOT_RUN`。

**Exit Gate：** 已启用的 benchmark 任务中影响分析正确；UNKNOWN 不静默降级；可以退回基础工具；没有自动交易/测试副作用。

**回退：** 删除辅助脚本/缓存，不影响生产代码。

## 7. PR-04（可选）：Repomix 与多 Agent 审核

仅在前三阶段证实跨 Agent 交接有显著重复成本后启动。Repomix 用于**受限范围、脱敏代码交接**，不是第二套默认语义检索引擎。

- 使用明确 `--include` 范围、`.repomixignore` 与现有 `.gitignore`；保留默认敏感信息扫描。
- 打包前后都检查输出，不包含账户凭据、私有配置、运行日志、交易明细、执行库、WAL/SHM 和历史缓存。工具可能列出部分路径，路径本身也需审核。
- 不加载不可信远程仓库可执行配置；不得因为已扫描就认定无敏感数据。
- 主开发 Agent 是唯一负责修改/提交的人；代码分析与安全审查 Agent 默认只读；测试 Agent 在明确授权后才执行测试。
- 多 Agent 同时修改订单/交易状态机、同一 Ledger 或相同文件，必须使用独立工作区与明确合并责任，不作为默认模式。

**Exit Gate：** 受限审核包无敏感数据；审核结论能追踪到 SHA、任务和合同；相较人工直接审核存在可测收益。

**回退：** 停止 Repomix 交接与并行审核，无生产变更。

## 8. 推荐目录（按 PR 增量创建，非一次性生成）

```text
OnlineTrader/
├── AGENTS.md                        # 全局不变量，保留
├── .agents/skills/                  # PR-01：短工作流入口；业务规范源仍在 docs/skills
│   ├── runtime-workbench-development/SKILL.md
│   └── dsv-parity-review/SKILL.md
├── docs/ai/
│   ├── ai-development-efficiency-plan-20261120.md  # 本文
│   ├── README.md
│   ├── architecture-status.yaml
│   ├── module-boundaries.yaml
│   ├── benchmark-tasks.yaml
│   └── toolchain-versions.md
├── scripts/ai/
│   ├── doctor.py                    # 仅在确有需要时实现
│   ├── context.py                   # PR-03，有条件实施
│   └── impact.py                    # PR-03，有条件实施
└── .serena/project.yml              # PR-01：工具生成后按安全审查决定是否提交
```

不得因“目录计划”就引入不存在的代码文件、生产依赖或无意义的占位 Skill。

## 9. PR 顺序、依赖和范围

| 阶段 / PR | 交付 | 依赖 | 不包括 |
| --- | --- | --- | --- |
| **阶段 0（第一批）** | 当前架构/文档状态、模块边界、B01/B04/B05/B08 参考答案 | 无 | 修改交易代码 |
| **PR-01（第一批）** | AGENTS 导航 + 两个 Skills + 静态兼容验证 | 阶段 0 | 大规模目录搬迁 |
| **PR-02** | Serena 干净源码副本接入、安全配置与定位对照 | 阶段 0/PR-01 | 生产依赖、运行目录、柜台连接 |
| **PR-03** | 仅开发经测量确需的 Context / Impact MVP | PR-01/02 的评估结果 | 完整 Repo Map/RAG |
| **PR-04（可选）** | 受限 Repomix 与独立审核交接 | 前三阶段存在证实的需求 | 默认多 Agent 并发写入 |

每个 PR 保持独立可回滚。若 Serena + Skills 已满足目标，**不强制实施 PR-03/04**。本计划不创建、恢复或运行 CI/CD。

## 10. 验收与证据

所有阶段报告至少包含：

- 基线完整 SHA、评测环境、Agent/模型版本、任务/fixture、实际命令与耗时；
- `PASS / FAIL / SKIP / NOT_RUN` 及可独立复核的证据；
- 每条结论标注：设计成立、源码可用、静态验证、功能测试或产品验收；
- 有关账号连接、Qt 交互、SimNow 与最终制品的项目均不因文档或 Mock 证据而自动变成 PASS。

三条必须满足的门槛：

1. **正确性：** 当前 Standalone/Legacy/Replacement 不混淆；无已知文档权威冲突被静默忽略。
2. **效率：** 相比阶段 0，在多项任务中观察到可重复的定位/上下文/返工收益；新增维护成本可接受。
3. **安全：** 工具仅访问授权开发副本，既不产生交易权威，也不执行未授权测试/发布。

## 11. 风险、回滚与维护

| 风险 | 应对 |
| --- | --- |
| Serena 动态调用识别不完整 | 标明局限，回退文本搜索和人工调用链审查 |
| 过期文档误导 AI | 状态清单、适用执行路径、SHA、冲突显式提示 |
| Skill 重复 / 客户端加载不同内容 | 唯一规范源、版本化适配、自检实际加载路径 |
| AST 漏报 IPC/Qt/SQL 影响 | 非 Python 规则 + UNKNOWN + 人工审核 |
| MCP 能力过宽 | 隔离开发工作区、最小工具/OS 权限，不暴露运行目录 |
| 工具更新破坏可复现性 | 记录验证版本，升级前比较当时已启用的 benchmark 任务 |
| 增加工具反而变慢 | 用阶段 0 对照，收益不足时删减或停用 |
| 数据隐私或秘密进入上下文 | 排除规则、脱敏与人工复核，不以自动扫描代替审查 |

维护约定：新路径或新权威决定先修改实际合同正文，并在同一变更中更新 `architecture-status.yaml`；状态索引不能成为第二套权威。新增 Skill 必须证明确实减少重复开发；发布包的配置与交易行为仍服从项目现有授权规则。若源码事实和计划文本冲突，应评审并更新计划，而不是要求 AI 无条件遵循旧计划。

## 12. 实施建议

**先完成阶段 0 与 PR-01 Skills，再在干净源码副本中评估 PR-02 Serena；待验证有效后再决定 PR-03。** Repomix 与多 Agent 继续保持可选。

当前工作区已完成阶段 0 的静态参考答案和 PR-01 的两个 Skill 文件；受控效率测量、真实任务触发率、Serena、Context/Impact、Repomix 和多 Agent 均未执行。

## 13. 参考资料

**项目当前合同**

- [Standalone 本机单实例与柜台权威](../refactor/runtime-broker-authority-single-instance-20261120.md)
- [Runtime / Workbench 收敛计划](../refactor/runtime-architecture-hardening-plan-20261120.md)
- [DSV 替代链索引](../refactor/dsv-parity-20261114/README.md)
- [文档导航](../README.md)
- [AGENTS.md](../../AGENTS.md)

**外部官方资料**

- [Serena README / 安装](https://github.com/oraios/serena)
- [Serena 项目工作流](https://github.com/oraios/serena/blob/main/docs/02-usage/040_workflow.md)
- [Aider Repository Map](https://aider.chat/docs/repomap.html)
- [OpenCode Skills](https://opencode.ai/docs/skills)
- [Repomix Security](https://repomix.com/guide/security)
