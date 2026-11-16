# 2026-11-16 下午四合约实际模拟盘对照

时间均为北京时间。原版 DSV 使用 TqKq 模拟，OnlineTrader `e49bcc522fe7c606e879ab4a3da83bc5d9769701` 使用 SimNow 替代链。执行环境为 Windows 11 和 OnlineTrader CPython 3.13.15、TqSdk 3.10.4。策略范围为棕榈油 `DCE.p2701`、塑料 `DCE.l2701`、沥青 `SHFE.bu2701`、菜油 `CZCE.OI701`。两端沿用已核对的原自动手数和风控。

13:30:05.000 发出 OnlineTrader START，13:30:05.007 发出原版 F5。OnlineTrader 首次激活位判定在 13:30:05.154，DSV 建立连接后于 13:30:19 写出首条策略日志。棕榈油多头、塑料空头、沥青多头都在同一根分钟线上得到一致的拒绝方向、触发价和激活价。

## 重叠运行对照

| 观察窗 | OnlineTrader 轮次 | DSV 激活位提示匹配 |
| --- | ---: | ---: |
| 13:30:05–13:40:05 | 4,404 | 3/3 |
| 13:40:05–13:50:05 | 4,665 | 3/3 |
| 13:50:05–14:00:05 | 4,902 | 4/4 |
| 14:00:05–14:10:05 | 5,189 | 4/4 |
| 14:10:05–14:20:05 | 4,587 | 4/4 |
| 14:20:05–14:30:05 | 5,337 | 4/4 |
| 14:30:05–14:40:05 | 4,472 | 4/4 |
| 14:40:05–14:45:30.755 | 2,699 | 3/3 |
| 合计 | 36,255 | 29/29 |

四标均为几何阻断。DSV 记录的 301 个行情点以合约与秒为单位，其中 294 个存在 OnlineTrader 同秒报价。可比点的日高、日低全部相同，273 个点的现价也相同，其余 21 个点的最近报价均差一个最小跳价。DSV 的跳过提示限频，按合约和原生分钟对齐方向及两价。OnlineTrader 各合约同一个原生分钟只消费一次。

两边本次没有新增报单、撤单或成交。OnlineTrader 14:45:30.755 发出 STOP，14:45:31.516 公开快照确认已停开、同步、零仓零单。14:50:00 DSV 记录四标的收盘前禁开提示，用户随后按 F6，确认按钮变灰。两边柜台终查都为空仓、无活动或待核委托，全部历史委托和逐笔与开跑前一致。TqKq 终查时间是 14:52:34。

独立对照覆盖 7 个整十分钟窗口和末段。本轮实际验证了几何拒绝及已达激活位的不开仓路径。两边共同运行未覆盖收盘前禁开、实际开仓、撤单、成交保护和平仓。

## 停开后的 SQLite 提交错误

14:46:27.385，OnlineTrader 的 `SDK_ROUND` 在 SQLite `COMMIT` 抛出 `OperationalError`，再由外层记录停机异常。执行器停在 `NEEDS_REVIEW / DISARMED`，错码为 `E_DSV_STORE`，发生于派发之前。候选账本关闭后的 SQLite `quick_check` 返回 `ok`。

该库使用 DELETE 日志模式。末段长游标读取与提交失败的时序吻合。在独立隔离库中，长只读游标持锁使写入端在 2.255 秒后报 `database is locked`。现场只记录了异常类型和位置，具体 SQLite 子码及持锁者未保留。改为固定事件水位、每批最多 32 行并关闭只读游标后再解析，同一隔离条件下写入提交成功，耗时 0.002 秒。此改动位于 QA 对照工具，生产执行代码及原始账本均保留。

## 只读复现

`--run-dir` 接受含 `candidate.db`、`original-dsv/logs/trade_日期.log` 的实际运行目录。运行目录中的状态和诊断文件存在时会一起汇总。报表按 `[开始, 结束)` 取轮次，时间参数须带显式时区并处在同一北京时间自然日。历史 DSV 文字日志精度为秒。行情按同秒、同合约计数，激活位匹配要求同一合约和日期的有效原生分钟键，包含观察窗开始前在同一分钟已经写入的收据。脚本读取账本，不发起任何柜台连接。 [QA 对照工具](../../../qa/dsv_parity/compare_live_logs.py) · [读锁回归](../../../tests/test_dsv_live_log_comparison.py)

```powershell
.\.venv\Scripts\python.exe -X utf8 -m qa.dsv_parity.compare_live_logs `
    '2026-11-16T13:30:05+08:00' '2026-11-16T14:45:30.755+08:00' `
    --run-dir 'output/validation/20261116-dsv-afternoon-four'
.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_dsv_live_log_comparison -v
```

本机原始证据的 SHA-256 供复核时比对。

| 原件 | SHA-256 |
| --- | --- |
| DSV `trade_2026-11-16.log` | `9ce24e2e2596d13c0ff118867660e3a574c4f0e0a2adf970d222bf1d53a96033` |
| `log-crosscheck.json` | `a1e87e840d4ee1b3cc590c53454752620dd8ff70135484414690cf227d7511fd` |
| `acceptance-close-summary.json` | `dfd437958abcb73ece0659c3fcec3d9b914595431f464db6a55453647f4bcbab` |
| `runtime-diagnostics.jsonl` | `380e9c0a7235a1c2de88548442890ab8d99ce97e6da7d06e58b5aca4bf4a5c95` |
| `observer-lock-reproduction.json` | `21c18786dd25cd639cd491d20b1f0d31ba68b0e1f7a0082e0e455c83a5f2384d` |
| 下午独立复核报告 | `34edd62f25316d03c044594a3e738aaf0392b3b3b34967e0390591841c032c79` |
| `TqKq-post-stop.json` | `5269af09f0b19c10ff9c9406ce680bb5d3db304207319b2bbb0926ec5038554e` |
| `SimNow-post-stop.json` | `306a72d55fc25702e44a19c009534fa7394fa502c7d17ef74c7688af4ad11736` |
