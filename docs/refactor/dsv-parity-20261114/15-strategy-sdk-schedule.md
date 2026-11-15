# 原策略与 SDK 调度串联离线对照

冻结策略循环在 CPython 3.14 中直接调用冻结 TqSdk 3.10.2 的目标任务和逐单任务，使用原版策略价格回调。每轮执行原版 SDK 的 `wait_update`、`_wait_update` 和 `_send_pack`；本地回放替代事件循环的网络发送、收包和数据合并端口。候选通过真实 `DsvSdkAdapter.step()` 和 `DsvSession`，执行钉住的 TqSdk 3.10.4 对应方法与相同输入批次。报撤单证据取自本地出站队列，调用链没有连接真实 SDK 账户和柜台。

## 逐轮观察

| 轮次 | 迟确认追价回放 |
| --- | --- |
| 2 | 两侧策略产生 5 手目标。候选在持久化后调用 SDK，原版先把目标交给目标任务。两侧本轮出站队列均未发该单。 |
| 3 | 下一次 `wait_update` 内两侧均出站首单 5 手，限价 1060。收到更差盘口，柜台尚未确认首单。候选用刚返回的 SDK 本地身份排队撤单。 |
| 4 | 原版价格任务调用撤单，两侧在同一次 `wait_update` 内出站撤单。随后收到已成 2 手及原单确认。 |
| 5 | 终态报告累计成交 3 手，逐笔仍只证明 2 手。两边无补单出站。 |
| 6 | 补齐迟到的 1 手逐笔回报。候选把余下 2 手委托放入 SDK 队列。 |
| 7 | 两侧出站余下 2 手的新委托，限价 1061。 |

空批次用例的 `wait_update` 返回 `False`，已经排队的首单依然在该次推进中出站。保护用例的目标在第 5 轮生成，平仓包在第 6 轮出站。重复零目标保留原始调用次数。源 SDK 价格监听对上一批已经终态的订单仍产生空撤，轨迹保留原始调用并在比较有效报撤时排除。

## 本地回执与柜台确认

真实 SDK 的插单调用先产生本进程持有的订单对象，此时柜台受理时间可以为空。候选以前把这个对象当成无有效时间戳的柜台订单，导致第 3 轮中止。现在只在同一连接上确认 SDK 调用成功返回、订单身份和原委托参数匹配、未见柜台确认且无成交时接受本地证据。它允许按该原始身份撤单，状态仍为 `CALL_RETURNED`，不会证明受理、成交、结清或重连核对完成。连接中断后丢弃本地撤单证据，保留已提交的原订单责任和待核对数量。

把首单部分成交阶段拉长到原版的五秒持仓检查窗口后，第 7 轮原版把 3 手自发成交误判成手动减仓，目标从 5 手改成 3 手。SDK 原先的补单任务随后买入余下 2 手，第 9 轮又卖出 2 手。按用户确认，OnlineTrader 依据已核对的订单和逐笔成交保留 5 手目标。固定回归检查第 7 轮已成交 3 手且只准备补开剩余 2 手，第 9 轮已成交 5 手且没有新增报撤单。原版这两轮的目标和报单也纳入断言，其余七轮仍要求一致。

## 复现

调度比较器先核对轮次 ID、顺序与唯一性，再检查每轮有序目标和有效报撤。只有连续相同的原版目标可以折叠。多余、缺失、重复和乱序轮次，或将 `[0, 5, 0]` 缩成 `[0, 5]`，均由 `tests.test_dsv_schedule_comparator` 覆盖。

```powershell
$env:PYTHONPATH = 'src'
$archive = Join-Path ([Environment]::GetFolderPath('Desktop')) 'DSV实盘1.0.zip'
$python314 = '.\output\tools\dsv-calc-venv\Scripts\python.exe'
$candidateApi = '.\output\online\sdk-venv\Lib\site-packages\tqsdk\api.py'
$directory = 'output/dsv-parity/scheduled-sdk-20261115'
.\.venv\Scripts\python.exe -X utf8 -m qa.dsv_parity.original_loop_samples --scheduled --output $directory
foreach ($case in @('pipeline-chase', 'pipeline-protect', 'pipeline-timeout')) {
    .\.venv\Scripts\python.exe -X utf8 -m qa.dsv_parity.scheduled_sdk_replay --archive $archive --python314 $python314 --candidate-api $candidateApi --input "$directory/$case.json"
    if ($LASTEXITCODE -ne 0) { throw "调度对照失败 $case" }
}
.\.venv\Scripts\python.exe -X utf8 -m qa.dsv_parity.scheduled_sdk_replay --verify-slow-partial --archive $archive --python314 $python314 --candidate-api $candidateApi --input "$directory/late-partial-ownership.json"
if ($LASTEXITCODE -ne 0) { throw '慢部分成交回归失败' }
```
