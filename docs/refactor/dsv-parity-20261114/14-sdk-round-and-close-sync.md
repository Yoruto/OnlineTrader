# 冻结 SDK 逐单对照与平仓同步

本篇的逐单测试直接注入目标用于核对 SDK 任务。需要核对目标从原策略发出到 SDK 出站的实际轮次时，使用 [串联调度回放](15-strategy-sdk-schedule.md)。

离线入口校验冻结 ZIP、SDK 版本和目标任务源文件，直接运行 TqSdk 3.10.2 的 `TargetPosTask` 目标处理、路由和两个原版子单任务。只在行情、账户快照、异步通道和 API 边界注入本地数据。原版测到 `insert_order`、`cancel_order` 调用，候选测到 `DsvSession` 持久化后记录端实际收到的发送、撤单指令。两边按轮次共享报价、原生状态和逐笔成交，并对齐各自生成的子单身份。

## 逐单结果

- 首单买开 5 手，限价 1060。成交 2 手且卖一涨到 1061 时，两边撤原单。
- 撤单终态报告已成 3 手，逐笔仍只有 2 手。两边保留未齐的事实并等待，不提前补单。补齐 1 手回报后，两边以 1061 买开余下 2 手。
- SDK 的原始价格监听在回报已经给出终态的同一轮仍可调用撤单。对照保留这条原始调用，在有效动作比较中排除该终态单撤单。候选对终态单不生成冗余撤单。

SDK 回放在同一阶段向两边加载相同目标和回报。原版策略经 `wait_update` 推进目标任务，候选在决策持久化后派发，跨两层的绝对调度时刻需另行对照。SDK 样本使用主动盘口价，原策略动态限价回调、不同交易所今昨仓组合和实际柜台响应未纳入本批次。

## 第 6、7 轮

冻结循环第 6236 行在最近一次调仓请求后等待至少 5 秒才读取策略对账用的账户仓位。保护退出的第 6 轮已经带来平仓终态、逐笔成交价 1076 和原生零仓。报价仍触发保护，原版走第 7723–7730 行的已记账分支，重申零目标并把策略对账时钟刷新到第 6 轮。第 7 轮到达检查窗口后，原版在第 6345–6399 行识别自发平仓、清理策略标记并结束该品种本轮处理。

替代链继续在第 6 轮保存成交和零仓事实，并保留策略关闭待同步状态。重复触发刷新已有策略时钟。策略同步等到 5 秒且原生仓位、终态和逐笔一致，在同步轮结束时才释放周期。边界样本验证从刷新时刻起 4.999 秒仍等待，5.000 秒完成同步。保留业务期间的策略状态不会增加名义点数、虚构持仓或产生第二笔平仓。

## 运行

```powershell
$env:PYTHONPATH = 'src'
$archive = Join-Path ([Environment]::GetFolderPath('Desktop')) 'DSV实盘1.0.zip'
$python314 = '.\output\tools\dsv-calc-venv\Scripts\python.exe'
$directory = 'output/dsv-parity/sdk-round-20261115'
.\.venv\Scripts\python.exe -X utf8 -m qa.dsv_parity.original_loop_samples --sdk --output $directory
foreach ($case in @('sdk-chase', 'sdk-protection', 'sdk-sync-boundary')) {
    .\.venv\Scripts\python.exe -X utf8 -m qa.dsv_parity.sdk_round_replay --compare --archive $archive --input "$directory/$case.json"
    if ($LASTEXITCODE -ne 0) { throw "SDK 对照失败 $case" }
}
foreach ($case in @('sdk-protection', 'sdk-sync-boundary')) {
    .\.venv\Scripts\python.exe -X utf8 -m qa.dsv_parity.compare_original_loop --archive $archive --python314 $python314 --input "$directory/$case.json"
    if ($LASTEXITCODE -ne 0) { throw "策略循环对照失败 $case" }
}
```
