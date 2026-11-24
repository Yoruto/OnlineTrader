# OnlineTrader 文档导航

本页只列当前开发入口和历史边界。日期化验收记录保留原版本语义，不自动成为当前候选的通过结论。

## 当前开发入口

- [AI 开发辅助入口与文档状态](ai/README.md)
  - [效率优化实施计划 V1.2](ai/ai-development-efficiency-plan-20261120.md)（第一批静态基线与两个项目 Skill 已加入；工具效果待测）
  - [第一批静态基线报告](ai/baseline-report-20261120.md)

- [当前功能状态与开放问题](功能实现与待办清单_20261110.md)
- [Standalone Runtime Workbench 当前合同总览](refactor/runtime-workbench-current-20261120.md)
- [Standalone Trading Runtime 设计](refactor/standalone-trading-runtime-20261119.md)
- [Runtime 本机单实例与柜台权威设计](refactor/runtime-broker-authority-single-instance-20261120.md)
- [Configuration、Persistence 与 Runtime 基础架构与生命周期解耦设计 V1.1](refactor/configuration-persistence-runtime-architecture-20261124.md)（运行模式、账户切换、更新协调及数据连续性；待评审，未实施）
- [2026-11-20 独立全量复测缺陷修正记录](refactor/runtime-full-retest-remediation-20261120.md)
- [Runtime / Workbench 架构收敛与安全改造计划](refactor/runtime-architecture-hardening-plan-20261120.md)
  - [执行路径与 Ledger Lineage 事实矩阵](refactor/runtime-execution-ledger-lineage-20261120.md)
  - [Workbench 本机命令安全合同](refactor/runtime-workbench-security-contract-20261120.md)
  - [CommandBus 进程内关联与失败合同](refactor/runtime-command-bus-contract-20261120.md)
  - [Desktop Bootstrap 与生命周期合同](refactor/runtime-bootstrap-lifecycle-contract-20261120.md)
  - [ConfigSnapshot 与逻辑通道合同](refactor/runtime-config-snapshot-contract-20261120.md)
- [Trading Workbench Client/View 重构计划](refactor/trading-workbench-client-refactor-20261119.md)
- [桌面操作习惯与安全退出改造方案（保留当前界面）](refactor/desktop-lifecycle-user-flow-20261120.md)
- [Runtime 运维、配置与账本](runtime-operations-and-storage-20261119.md)
- [Runtime 测试入口迁移](refactor/standalone-trading-runtime-test-migration-20261119.md)
- [账户数据同步与查询](account-data-sync-design-20261119.md)
- [运行架构与账户历史同步旧草案（已废止）](refactor/runtime-config-ledger-account-sync-design-20261120.md)
- [Windows 安装与更新](windows-gitlab-updates.md)

## DSV 对齐与替代候选

- [DSV 行为基准](dsv-behavior-baseline-20261113.md)
- [当前重构索引](refactor/dsv-parity-20261114/README.md)
- [验收证据索引](validation/README.md)
- [B01–B20 用户确认](b01-b20-recommendation-confirmation.md)

## 时钟、行情与长期运行

- [时钟与计时器开发规格](clock-and-timer-development-plan-20261117.md)
- [时钟与行情稳定性实施计划](clock-market-data-stability-implementation-plan-20261118.md)
- [时钟实现与检查入口](clock-timer-implementation-20261117.md)
- [长期运行计划](always-on-operation-development-plan-20261117.md)

## 历史资料

- [早期设计与 G0 规格](history/early-design/README.md)
- [旧 G1 与修复前审计](history/legacy-runtime/README.md)
- [桌面版本历史](history/releases/README.md)
- [2026-11-10 至 11 授权记录](history/development-authorizations-20261110-11.md)
- [ddad472 功能清单原文](功能实现与待办清单_20261110_归档_ddad472.md)

历史文档用于解释当时的代码、授权和证据。开发新功能时从本页“当前开发入口”开始；只有明确维护旧路径或复核旧证据时才进入历史目录。
