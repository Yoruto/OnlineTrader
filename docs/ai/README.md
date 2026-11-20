# AI 开发辅助入口

本目录维护 OnlineTrader 的开发侧路由、文档权威状态和静态定位基线，不属于产品运行时，也不授予账号连接、测试、报单、打包或发布权限。

开始源码任务时按以下顺序使用：

1. 先读仓库根目录 `AGENTS.md`，确定本次授权和默认执行路径。
2. 在 `architecture-status.yaml` 中按 `path + scope` 查找当前合同；同一文件可以在不同作用域有不同状态。
3. 用 `module-boundaries.yaml` 定位实际入口和状态所有者，再以当前源码核实。
4. Runtime、Workbench 或 DSV 任务按需加载 `.agents/skills/` 中的项目 Skill。
5. 文档与源码冲突时报告 `CONFLICT / NEEDS_REVIEW`，不能静默选择旧结论。

## 文件职责

- `architecture-status.yaml`：关键文档在具体作用域内的 `CURRENT / CANDIDATE / SUPERSEDED / HISTORICAL` 状态。它是导航索引，不替代被索引的合同正文。
- `module-boundaries.yaml`：人工确认的执行路径、入口、所有者和禁止依赖。
- `benchmark-tasks.yaml`：静态定位任务及人工核对的参考答案。
- `baseline-report-20261120.md`：本批静态核对结果和仍未执行的效率测量。
- `ai-development-efficiency-plan-20261120.md`：分阶段实施计划和停止条件。

## 状态和证据

`status` 只说明该文档在指定 `scope` 内的使用方式：

- `CURRENT`：当前任务可作为合同入口，但仍须核对 `verified_at_sha` 与工作区变化。
- `CANDIDATE`：设计或源码候选尚缺其声明的验证。
- `SUPERSEDED`：该作用域已被 `superseded_by` 替代。
- `HISTORICAL`：只保留历史、决策或旧候选证据。

每条记录另外区分 `design`、`source`、`tests` 和 `product_acceptance`。不得由设计成立推导源码已实现，也不得由静态核对推导功能测试或产品验收通过。

维护时先修改实际合同正文，再在同一变更中更新状态索引。状态 YAML 不能成为绕过设计评审的第二套权威。
