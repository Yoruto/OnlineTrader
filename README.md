# OnlineTrader

OnlineTrader 是一款独立的 Windows 期货交易桌面软件。默认以 **Standalone Trading Runtime** 作为运行与交付路径：交易执行器（Target Owner）与界面（Trading Workbench）进程解耦，统一由期货账户同步资金、持仓、委托与成交事实。

---

## 快速启动

### 1. 便携版 / 正式交付版

配置完整的便携版直接双击根目录的 `OnlineTrader.exe`。程序会：
1. 检查本机是否已有运行中的 Runtime（同一台 Windows 电脑最多运行一个 Runtime）；
2. 若无，则以 `DISARMED`（未启动策略）拉起唯一 Owner 并从柜台同步最新事实；
3. 打开或唤醒唯一的 Trading Workbench 窗口。

### 2. 源码开发入口

本机日常源码验证可直接运行桌面启动器（委托 [start-dev-runtime-workbench.ps1](file:///e:/online-trader/scripts/start-dev-runtime-workbench.ps1)）：

```powershell
# 自动选取桌面上版本号最高的 *-Runtime-Configured/runtime 作为配置与缓存目录
.\scripts\start-dev-runtime-workbench.ps1

# 或显式指定 runtime 目录
.\.venv\Scripts\python.exe -m online_trader.desktop.entrypoint --runtime-root <目录>
```

> [!NOTE]
> - 缺少有效 Runtime 配置时，默认入口将明确报错并退出，不会静默回落到 Legacy 路径。
> - `runtime run` 与 `--runtime-pipe` 仅保留为运维及高级附着入口；CLI 与公开 API 不提供交易下单或无人值守调度。

---

## 文档导航

| 文档 | 核心职责 |
| --- | --- |
| [AGENTS.md](file:///e:/online-trader/AGENTS.md) | AI 开发约束、安全红线、授权与交付规范 |
| [architecture.md](file:///e:/online-trader/docs/architecture.md) | 系统整体架构、执行路径与模块边界 |
| [runtime.md](file:///e:/online-trader/docs/runtime.md) | 交易运行时、全账户收敛、市场权限与执行合同 |
| [strategy.md](file:///e:/online-trader/docs/strategy.md) | DSV 策略业务规范、参数基准与取证原则 |
| [workbench.md](file:///e:/online-trader/docs/workbench.md) | 桌面工作台交互、命令总线与生命周期 |
| [storage.md](file:///e:/online-trader/docs/storage.md) | 配置、临时执行库、历史缓存与数据权威 |
| [testing.md](file:///e:/online-trader/docs/testing.md) | 编译检查、离线仿真 Profile 与独立验收方法 |
| [delivery.md](file:///e:/online-trader/docs/delivery.md) | 便携打包、随包配置规范与 GitLab/极狐受管更新 |
| [diagnostics.md](file:///e:/online-trader/docs/diagnostics.md) | 诊断事件、盘后/退出上报与故障排查 |
| [plans/runtime-lifecycle.md](file:///e:/online-trader/docs/plans/runtime-lifecycle.md) | 进行中：Runtime 生命周期解耦与维护停机方案 |

原始规格、DSV 用户确认记录与取证材料位于 `docs/specs/`、`docs/references/` 与 `docs/examples/`。
