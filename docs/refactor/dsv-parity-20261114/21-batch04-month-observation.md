# 批次04｜月停观察顺序与跨日语义

基线：`426ac0f9ca38dbff8b2dc54f3ed70a245ac8eb38`。本批只改变替代链，旧 Kernel 仍使用原 `business_statistics.reduce_stops`；不切换桌面，不修改授权、订单事实或配置哈希。

## 原版证据

本轮重新只读用户上传原包，核对 `trading.exe` SHA-256 为 `aabeafd83d265ab9b1cf5cc3fd0e9b0f21963ddcc95daa70feefc209997269a3`，GUI marshal 对象为 `07c82c9ada765284ed5a929227024cbf192f5f72192636900740d9a5764032ea`。

原包为 Python 3.14 编译对象。本次用惰性数据解析和固定3.14操作码表静态读取，不执行原应用、业务字节码或初始化。以下是编译对象行表位置，不是取得了 gui.py 明文，也不构成原版动态对跑：

- `_run` 6005–6015：月停功能关闭时跳过整块；使用品种保存的交易日；换月只清除月停原因对应的日停。
- 6019–6024：按原生浮点阈值判断，亏损日不存在时追加并截取最近12项；重复日期不重新截断。
- 6026–6027：同月已有标记时跳过再次施加月停，不等于该月永久禁止入场。
- 6028–6040：向前最多搜索40个自然日；最近N个交易日均在亏损历史中时设置月标记及日停。已记录日期不因当前净亏后来减少而删除。
- 6795–6811：随后才进行品种跨日初始化，重置日停/净亏等当天状态，不在此处清月标记。

`is_trading_day` 的原版节假日来源尚未在本批重做。候选延续当前明确交易日历输入契约；调用方必须提供完整相关区间。缺失区间不捏造交易日，也不新增入场门禁。这个边界不能宣传为所有日历输入均与原版一致。

## 修改

新增纯函数 `dsv_core.month_stop.observe_month_stop`。替代运行器每轮先对保存的业务日做月停观察，再跨日重置，避免上一轮最后一笔亏损在记录前被清除。品种后的日统计不再重复施加月停；独立亏损日停和已有日停原因仍保留。删除 `_evaluate_symbol` 中仅凭同月历史标记就拒绝入场的额外分支。

保留原生同步、用户停止、未终态数量、UNKNOWN、真实成交和退出目标。未重写历史事件、清库或追溯修正既有存量状态。上线前仍需当前状态核对或合法切换窗口。

## 本次验证

环境：Linux / CPython 3.13.5，定向源码子集；基线文件均核对Git blob SHA，不是完整 checkout。

- `test_dsv_month_stop.py`：20个方法 PASS，直接导入产品纯规则、日统计、values和errors。
- `test_dsv_month_stop_runtime.py`：16个方法，原基线9 FAIL/7 PASS；候选16 PASS。真实 `DsvSession` 方法、codec和SQLite Journal，覆盖交易提交、同日/跨日关闭重读、重复轮次和写入失败回滚。
- 运行器测试以 `__new__` 构造对象，未调用账户锁/会话正常构造；显式无订单控制器仅允许空事实和空效果。两项路由测试显式mock入场函数。本机8个未调用依赖使用调用即失败的导入占位，不能据此声称完整依赖或正常启动通过。
- 源码与测试 compileall PASS。叠加PR #6中runner止盈修复后，本批36个方法再次PASS；不等于#6全部配置/运行回归。

完整环境复现入口（本机不具备该环境，命令不是已运行声明）：

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p 'test_dsv_month_stop*.py' -v
.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_dsv_round_replay tests.test_dsv_refactor_repairs tests.test_dsv_schedule_comparator tests.test_dsv_stops -v
```

冻结原版完整轮次/SDK对跑（含批准第7/9轮）、完整依赖正常导入、Windows/Qt/IPC、账户锁、真实柜台和制品均NOT_RUN。保留草稿，不把36项方法通过写成F04全场景结案或正式软件已切换。
