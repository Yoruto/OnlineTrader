# 冻结原版策略循环离线对跑

本篇记录首次原策略对跑的结果。后来完成了 [SDK 逐单对照与平仓同步修复](14-sdk-round-and-close-sync.md) 和 [策略到出站队列的串联验证](15-strategy-sdk-schedule.md)。下文的第 6、7 轮差异是修复前证据。

对照入口从基线清单指定的 ZIP 提取 `StrategyThread._run`，校验 ZIP、EXE、GUI 模块和循环字节码的 SHA-256。隔离的 CPython 3.14 进程直接执行原循环、原目标跟踪器 `_TPSpy`、原交易日历和策略辅助函数。入口用本地报价、K 线、账户快照、委托和成交替代 SDK 输入。目标任务记录原循环请求的绝对持仓目标，通知和日结落盘限制在临时目录或内存。对照进程禁止网络和外部进程。

OnlineTrader 在独立的临时 SQLite 中运行公开的 `DsvSession` 入口。两端读同一份 JSON，包含配置、轮次编号、行情、时间、账户持仓和合成原生回报。候选回报的子单身份映射到本轮实际生成的子单。每轮保留原始目标调用、候选委托计划和两侧状态，比较目标仓位变化、到期锁定、日停和名义净亏。

## 执行

```powershell
$env:PYTHONPATH = 'src'
$archive = Join-Path ([Environment]::GetFolderPath('Desktop')) 'DSV实盘1.0.zip'
$python314 = '.\output\tools\dsv-calc-venv\Scripts\python.exe'
$directory = 'output/dsv-parity/original-loop-20261115'
.\.venv\Scripts\python.exe -X utf8 -m qa.dsv_parity.original_loop_samples --output "$directory/inputs"
foreach ($fixture in Get-ChildItem "$directory/inputs" -Filter '*.json') {
    .\.venv\Scripts\python.exe -X utf8 -m qa.dsv_parity.compare_original_loop --archive $archive --python314 $python314 --input $fixture.FullName > "$directory/$($fixture.BaseName)-trace.json"
    if ($LASTEXITCODE -ne 0) { throw "对照失败 $($fixture.Name)" }
}
```

## 差异和修复

原版在当前品种首次走到到期检查，并获得有效到期日期后，锁存剩余天数。首次为 5 天时，后续行情改报 6 天仍然停开。首次为 6 天时，后续变成 5 天仍沿用首次记录。缺少到期字段时可以在后续行情补上。冻结循环只读取 `min_days_to_expiry`，没有以 `rollover_stop_enabled` 跳过阈值。候选替代执行链按这一行为保存首个有效读数，跨轮、跨柜台日及执行器重启沿用已提交的状态。旧事实库缺少该字段时，从下一次实际执行到期检查开始记录。工作台使用阻断决策保存的天数。

保护样本经 5 手开仓、成交 1061、80% 保护线激活、触发报价 1078、平仓成交 1076。两边名义统计都按理论保护价 1085 记入 25 点。原版重复申请目标 0，候选生成一次平仓委托。原版在平仓成交后的下一次同步轮将策略持仓归零，候选在收到一致的终态和成交明细时归零。这两处原始轨迹均保留。目标变化比较折叠了样本中同一轮重复的绝对目标，以及账户快照已经达成的重复目标。

该对照覆盖样本的策略目标和状态。原生 SDK 子单调度、撤改、部分成交的逐单对照、自动手数、其他盘段和完整 GUI 的原版对跑仍待扩展。冻结代码仅在 QA 解释器中调用，候选交易入口继续使用自身的 Python 3.13 运行环境。
