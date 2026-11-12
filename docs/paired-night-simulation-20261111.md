# 2026-11-11 夜盘 DSV 快期与 OnlineTrader SimNow 对照

两套 Windows 桌面程序使用同一份五合约业务参数，分别登录快期模拟盘和 SimNow。记录时间为北京时间。20:38 两边独立只读查询均确认柜台交易日为 11 月 12 日，五个测试合约均无委托、成交和持仓。SimNow 另有两条原有螺纹钢昨仓，各 1 手多仓。20:49 从桌面启动，四个合约运行至 23:00，白银运行至次日 02:30。自动手数使用各自模拟账户的实际资金。

| 合约 | 开仓偏移 | 止损偏移 | 止盈偏移 | 夜盘收盘 |
| --- | ---: | ---: | ---: | --- |
| 棕榈油 `DCE.p2701` | 32 | 16 | 73 | 23:00 |
| 塑料 `DCE.l2701` | 55 | 20 | 106 | 23:00 |
| 沥青 `SHFE.bu2701` | 34 | 12 | 73 | 23:00 |
| 菜油 `CZCE.OI701` | 23 | 15 | 52 | 23:00 |
| 白银 `SHFE.ag2702` | 96 | 15 | 139 | 02:30 |

参数哈希 `b2c5e0f55a6e12cbbfd10eb6637dde33898bac21033ae841d50b7b0c3e480e67`。原范围哈希 `9ce087830c81e9408015e6fe46e810e6fa28adfa82e58c15dd11f15dd2b444ab`。恢复前后的范围、参数、日历和账本相同。[快期开盘前](../output/dsv-online/paired-night-20261111/evidence/kq-before.json) · [SimNow 开盘前](../output/dsv-online/paired-night-20261111/evidence/simnow-before.json) · [开盘档案](../output/dsv-online/paired-night-20261111/evidence/mark-open.json)

## 柜台逐笔成交

收盘后，快期返回 **12 笔委托、12 笔成交**，6 笔开仓和 6 笔平仓各 76 手。12 笔成交均关联到同一合约、方向及开平标志的原委托，全部委托已终结且剩余量为零。以下价格与时间取自柜台逐笔成交。SimNow 同日 0 笔委托、0 笔成交。

| 合约 | DSV 快期实成交开仓 | DSV 快期实成交平仓 | OnlineTrader SimNow 对照 |
| --- | --- | --- | --- |
| 棕榈油 | 21:05:06 空开 12 手 @ 8862 | 21:19:36 止损买平 12 手 @ 8878 | 21:05:05.950 触及空开价，`ACCOUNT_VALUES_UNKNOWN`，缺项为合约持仓保证金 |
| 塑料 | 21:07:32 空开 20 手 @ 8405<br>21:45:35 多开 20 手 @ 8429 | 21:43:43 止损买平 20 手 @ 8426<br>21:56:08 止损卖平 20 手 @ 8407 | 两次达到触发价的行情用于补齐观察，后续行情得到 `CANDLE_OPPORTUNITY_CONSUMED` |
| 沥青 | 21:05:00 空开 16 手 @ 4301 | 22:59:40 收盘买平今 16 手 @ 4295 | 21:05:00 整秒仍为 `OPENING_WAIT`。此后迟到 Tick 重建消耗该分钟机会 |
| 菜油 | 0 | 0 | 0 委托、0 成交 |
| 白银 | 22:12:56 空开 4 手 @ 12623<br>22:30:29 空开 4 手 @ 12621 | 22:15:04 止损买平今 4 手 @ 12637<br>01:22:43 按 80% 保护买平今 4 手 @ 12553 | 首次触发进入观察回放，第二次触发时仍有恢复屏障。恢复后振幅持续超限 |

DSV 白银第二次开仓在 00:52:56 锁定 80% 保护，保护线 12552.5 并触发该品种当日停开。01:22:43 柜台成交 12553。塑料两次止损日志中的触发参考价分别为 8425、8408，实际成交价为 8426、8407。[逐笔关联与判定汇总](../output/dsv-online/paired-night-20261111/evidence/night-comparison.json) · [DSV 夜盘日志](../output/dsv-online/paired-night-20261111/evidence/dsv-night.log)

## 收盘档案

- 23:00。OnlineTrader 于 22:50 对四个先收盘合约记录收盘前 10 分钟禁开。DSV 22:59:40 平掉剩余沥青。盯盘界面曾中断，保留原监视记录 22:59:32、23:00:02 和 23:00:30 的样本。恢复操作后于 23:11 补存 SimNow 快照并查看 DSV 实时持仓页。四个收盘合约均空仓，DSV 只余白银 4 手空仓。快期逐笔成交在全场收盘后统一回查。[23:00 样本和 DSV 界面观察](../output/dsv-online/paired-night-20261111/evidence/close-2300-observations.json) · [23:11 补存快照](../output/dsv-online/paired-night-20261111/evidence/mark-close-2300.json)
- 午夜。OnlineTrader 已恢复授权，白银行情连续，账户快照仍属 11 月 12 日交易日。DSV 继续记录同一文件中的夜盘活动。[午夜档案](../output/dsv-online/paired-night-20261111/evidence/mark-midnight.json)
- 02:30。两边均于 02:20 记录白银尾盘禁开。OnlineTrader 按原时窗结束，02:30:30 产品独立只读重登通过。随后两边独立只读查询均返回 11 月 12 日交易日，五个测试合约全部空仓、无活动委托。SimNow 两笔范围外螺纹钢昨仓各 1 手多仓仍在。两次回查的 SDK 已退出，未发送报单或撤单。确认无仓无挂单后，从 DSV 桌面停止策略。[02:30 档案](../output/dsv-online/paired-night-20261111/evidence/mark-close-0230.json) · [快期收盘回查](../output/dsv-online/paired-night-20261111/evidence/kq-after.json) · [SimNow 收盘回查](../output/dsv-online/paired-night-20261111/evidence/simnow-after.json)

## OnlineTrader 差异与修复

- 22:19:59，原版 `E_FACT_BARRIER` 让策略转为禁开。之后行情及柜台同步恢复，核对屏障仍在。原事件只持久化错误码，具体触发点缺少证据。离线复现了报价在取资金和合约事实期间跨过时效阈值的分支。修复后此类过期报价只进入连续路径补齐，发送仍需新鲜报价。其他事实屏障继续拦截，新增错误详情随屏障记录。
- 修复迟到回放与批量观察提前占用同分钟候选的问题。下一笔新鲜行情需重新满足完整入场条件。柜台完整快照证明该合约空仓时，缺省的持仓保证金按零处理，有非零持仓且字段缺失时仍为未知。
- 23:22，在原执行器禁开、无委托、无成交、无自有仓和未决预占的情况下停止原执行器。第一次从恢复版桌面连接到 SimNow 后，账本里约 33 MB 的策略检查点事务超过提交时限。资金快照历史改用有序内容哈希与来源哈希绑定，原始行仍留在原账本。第二次恢复的检查点为 7,970 字节。
- 23:29 恢复版验证原账本的白银行情边界，通过重叠的原生行情窗口续接 639 笔观察，源 epoch 从 1 续接到 3，回放发送数为零。桌面按钮未得到入场回执，随后通过产品原控制接口提交一次 `ARM_ENTRIES`，持久回执确认于 23:31:50 成功。23:31 后白银仍因振幅大于 `2 × 96 − 15 = 177` 点未入场。恢复版通过 176 项定向离线验证与安装文件核对，真实在线验证覆盖空仓恢复、重新授权、尾盘禁开和结案。OnlineTrader 的自然成交及成交后保护仍待在线复测，缺失跨日原单与等量换仓归属也未出现样本。[恢复前空仓核对](../output/dsv-online/paired-night-20261111/evidence/pre-recovery-stop.json) · [首次恢复超时](../output/dsv-online/paired-night-20261111/runs/20261111T152242Z-75273ea5/startup-failure.json) · [恢复授权回执](../output/dsv-online/paired-night-20261111/evidence/recovery-resumed.json) · [恢复版安装核对](../output/windows-app/dsv-layout-20261111-recovery2/installation.json)

全夜两段产品日志合计记录 74,339 条市场判定。恢复后有一次 23:51:32 的观察接口查询超时，相邻样本权限均为开启，原账本未记录新的核对屏障。白银最后一条入场判定使用 02:29:52 的行情，记录尾盘禁开和振幅过滤。[本机连续观察](../output/dsv-online/paired-night-20261111/evidence/watch.jsonl) · [两套桌面结束状态](../output/dsv-online/paired-night-20261111/evidence/desktop-end-state.json)
