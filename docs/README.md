# OnlineTrader 文档导航

本页只列当前开发入口和历史边界。日期化验收记录保留原版本语义，不自动成为当前候选的通过结论。

## 当前开发入口

- [当前功能状态与开放问题](功能实现与待办清单_20261110.md)
- [Standalone Trading Runtime 设计](refactor/standalone-trading-runtime-20261119.md)
- [Runtime / Workbench 架构收敛与安全改造计划](refactor/runtime-architecture-hardening-plan-20261120.md)
  - [执行路径与 Ledger Lineage 事实矩阵](refactor/runtime-execution-ledger-lineage-20261120.md)
  - [Workbench 本机命令安全合同](refactor/runtime-workbench-security-contract-20261120.md)
  - [CommandBus 持久化与失败合同](refactor/runtime-command-bus-contract-20261120.md)
  - [Desktop Bootstrap 与生命周期合同](refactor/runtime-bootstrap-lifecycle-contract-20261120.md)
  - [ConfigSnapshot 与逻辑通道合同](refactor/runtime-config-snapshot-contract-20261120.md)
- [Trading Workbench Client/View 重构计划](refactor/trading-workbench-client-refactor-20261119.md)
- [Runtime 运维、配置与账本](runtime-operations-and-storage-20261119.md)
- [Runtime 测试入口迁移](refactor/standalone-trading-runtime-test-migration-20261119.md)
- [账户数据同步与查询](account-data-sync-design-20261119.md)
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
