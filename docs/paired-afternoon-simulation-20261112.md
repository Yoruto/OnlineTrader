# 2026-11-12 下午双平台模拟对照

DSV v1.0.0（内核 DSV1.9.2）用快期模拟盘，OnlineTrader 用已安装的 dev28 和 SimNow。两边绑定相同的五个合约和参数，参数摘要为 `b2c5e0f55a6e12cbbfd10eb6637dde33898bac21033ae841d50b7b0c3e480e67`。两次柜台快照均返回交易日 20261112。OnlineTrader 在 13:31:22.662 启用开仓，DSV 日志记录 13:31:53 启动，共同运行到 15:00。[安装核对](../output/windows-app/dev28-20261112/installation.json) · [启动回执](../output/dsv-online/paired-afternoon-20261112/dev28-arm-final-receipt.json) · [DSV 午盘原始日志](../output/dsv-online/paired-afternoon-20261112/dsv-afternoon.log)

## 开仓、平仓、成交与收盘

| 合约 | 开仓委托 DSV / OnlineTrader | 平仓委托 DSV / OnlineTrader | 成交 DSV / OnlineTrader | 收盘持仓、挂单 |
|---|---:|---:|---:|---|
| 棕榈油 `DCE.p2701` | 0 / 0 | 0 / 0 | 0 / 0 | 两边均为 0 |
| 塑料 `DCE.l2701` | 0 / 0 | 0 / 0 | 0 / 0 | 两边均为 0 |
| 沥青 `SHFE.bu2701` | 0 / 0 | 0 / 0 | 0 / 0 | 两边均为 0 |
| 菜油 `CZCE.OI701` | 0 / 0 | 0 / 0 | 0 / 0 | 两边均为 0 |
| 白银 `SHFE.ag2702` | 0 / 0 | 0 / 0 | 0 / 0 | 两边均为 0 |

快期盘前和盘后均返回 12 笔旧委托和 12 笔旧成交，逐笔标识一致。SimNow 盘前和盘后均返回零委托、零成交，两个范围外螺纹钢昨仓仍在。OnlineTrader 账本无开平仓意图或成交。下午盘检验了行情恢复、开仓过滤和空仓收盘，实际开平仓及成交回报链路仍待自然触发验证。[快期回查](../output/dsv-online/paired-afternoon-20261112/kq-after.json) · [SimNow 回查](../output/dsv-online/paired-afternoon-20261112/simnow-after.json) · [逐合约柜台和账本比较](../output/dsv-online/paired-afternoon-20261112/afternoon-comparison.json)

## 关键日志

- **午休恢复。** dev28 在 13:30:05 前后按首批原生行情重建了五个合约的下午观察窗口，均为 `CONTIGUOUS`，历史信号执行数为零。14:59:40 有收盘窗口快照，15:00:00.024 账本提交 `PAUSE_NEW`，15:00:30.958 复登确认范围内空仓、无活动委托。15:00 的 IPC 采样遇到执行器结束，收盘证据以持久回执和独立柜台回查为准。[行情恢复](../output/dsv-online/paired-afternoon-20261112/dev28-afternoon-path.json) · [收盘窗口](../output/dsv-online/paired-afternoon-20261112/close-window-145940.json) · [复登凭证](../output/dsv-online/paired-day-20261112/runs/20261112T045116Z-3e97e364/final-relogin.json)
- **相同几何输入。** 首批对比中，棕榈油振幅 131 点大于上限 48 点，多空触发价 8872 / 8939。沥青 104 点大于 56 点，触发价 4266 / 4302。菜油 139 点大于 31 点，触发价 9949 / 10042。OnlineTrader 的塑料和白银也命中几何限制，分别为 159 大于 90、433 大于 177。DSV 塑料因本交易日净亏 40 点达到 40 点阈值而停开，白银保留夜盘浮盈曾达止盈 80% 的停开状态。SimNow 同日没有对应成交及停开记录。[开盘风控状态](../output/dsv-online/paired-afternoon-20261112/day-risk-before.json) · [DSV 午盘日志](../output/dsv-online/paired-afternoon-20261112/dsv-afternoon.log)
- **激活位日志遗漏。** 棕榈油、沥青和菜油的 DSV 日志各有 8 条「当日行情已跑过多头激活位」。OnlineTrader 在命中 `GEOMETRY_BLOCK` 时就返回，未记录后续的 `NO_CHASE_AFTER_ACT` 原因。三合约这段运行的交易结果均为零开仓。DSV 会节流这些提示，OnlineTrader 的详细记录按行情决策保存，因此条数也不同。[源码](../src/online_trader/strategy/entry.py#L369) · [逐项日志比较](../output/dsv-online/paired-afternoon-20261112/afternoon-comparison.json)
- **工作台暂时清空报价。** 13:31:53 至 14:36:10 的 148 次采样中，9 次五个合约同时显示「等待行情」。这些采样在柜台已同步且策略运行时发生，均可对应到五合约刚提交的行情重建。重建完成后会移除工作台上一条行情，界面暂用等待状态。含真实报价时间的同一组采样中，原生路径完整。资金快照过期另有 110 条账本记录，尚无成交可核验对执行的影响。[状态采样](../output/dsv-online/paired-afternoon-20261112/interim-view-freshness.json) · [行情重建提交](../src/online_trader/kernel/dsv_strategy.py#L508) · [工作台投影](../src/online_trader/kernel/workbench.py#L71)
- **收盘时间和日志。** DSV 在 14:50:00 写了四个合约的收盘前 10 分钟禁开，塑料继续输出净亏阈值。OnlineTrader 五个合约均记录 `CLOSE_SESSION_NO_OPEN`，首批对应行情时间为 14:50:00.418 或 14:50:07.000。DSV 在 15:00 后记录了两轮慢循环，耗时 13.9 秒和 14.3 秒，主要耗在主力诊断。午盘原始日志没有单独的「收盘清点」行，挂单页和持仓页显示均为零，随后用 F6 停止并通过快期柜台回查。临时停用的 DSV 钉钉通知已恢复，策略参数摘要与盘前一致。[原始日志](../output/dsv-online/paired-afternoon-20261112/dsv-afternoon.log) · [收盘和设置恢复记录](../output/dsv-online/paired-afternoon-20261112/dsv-close-and-restore.json)
