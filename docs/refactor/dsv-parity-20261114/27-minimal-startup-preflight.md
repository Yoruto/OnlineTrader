# 最小启动预检修复与分钟消费状态回归

基线 `52a82960152f75585b0682cfa30db4f09d90e866`。沿用用户确认的取舍：只修实际启动缺口，不为合成日志反例再增加交易门禁；不重写已合入的 WAL，不改策略或发送责任，不启用 CI/CD。

## 修改范围

`Journal` 中原有的 SQLite 运行库条件抽为 `validate_sqlite_runtime(provider)`。允许版本、提供方范围、拒绝错误码及 OFFLINE 行为不变；Journal 在打开库前仍自行复查。

`replacement-prepare` 在账户锁、源库核对及输出发布前调用同一检查；不兼容环境不会生成新的预检文件或覆盖旧文件。成功输出沿用 `DSV_REPLACEMENT_PREPARE/1`，仅追加 `sqlite_runtime`（实际 SQLite 版本和原 WAL 兼容条件的布尔结果），仍是 `DISARMED / broker_connected=False`。这些字段不代表柜台连接、设备或 Windows 平台的全部条件已验收。

`replacement-run` 在读取事实库、占用运行输出目录和连接 SDK 前重新执行检查，不把旧预检文件当成授权或永久环境证明。账户、配置、旧库、交易日与现有恢复条件不变。检查是一次性启动检查，不进入行情循环。

## 为什么不新增分钟门禁

补充真实 `DsvSession` / 显式控制器 / Journal / SQLite 的四个离线回归：先由追价上限拒绝消费分钟，随后同分钟出现激活位条件时不得再次消费；关闭重开后仍保留；换分钟可消费一次；重复输入只复用回执，不产生第二条 ROUND 或报单。合成行情与 RecordingPort 无网络，POSIX 用既有 OFFLINE 参数接文件锁夹具，Windows 用原 AccountLock。

四项均在未修改交易决策的实现上通过，证明这几个正常执行场景已有保护；不据此声称所有可能的跨原因/重启路径都已验证。现场比较器跨原因检查不在本 PR 扩展；其输出仍不是整段运行健康验收。

## 与现有 PR 的关系

历史逐笔身份与迁移完整性仍由 #11 处理；原版循环关键状态、有序拒绝、D02 标签及桌面入口回归仍由 #12 处理。本 PR 不复制、覆盖、合并或解决它们的分支冲突，不声称这些修复已经进入主分支。

## 本轮实际验证

Linux / CPython 3.13.5 / SQLite 3.46.1。测试目录由保存的历史源码快照及已取得的当前相关文件重建；两个产品修改文件、runner/transition/controller、历史投影和诊断关键文件已核对远端 Git blob，不是完整最新工程的重新检出。

- 新增预检/版本策略组件测试 12 项通过。源库核对、账户锁和连接边界使用明确替身；成功预检实际写临时文件。版本分支测试仅替换版本元组，没有把低版本运行库伪装成升级成功并连接账户或打开在线库。
- 新增分钟状态回归 4 项通过，实际构造离线会话和临时 SQLite，不替换业务函数。
- 组合上述 16 项与存储、诊断、控制接口和现场日志相邻回归，共 105 方法：105 PASS / 0 FAIL / 0 ERROR / 0 SKIP。重复执行不累加覆盖。
- 修复前相同预检测试已复现错误；旧实现缺少新辅助函数/元数据也会报错，原输出单列，不将失败子用例数解释为独立缺陷数。
- 定向 compileall 和补丁空白/应用检查通过。

手动复现：

```sh
PYTHONPATH=src:. python -m unittest tests.test_dsv_sqlite_preflight tests.test_dsv_minute_consumption_state tests.test_dsv_storage_read_isolation tests.test_dsv_runtime_diagnostics tests.test_dsv_runtime_diagnostics_serve tests.test_dsv_control_endpoint tests.test_dsv_control_endpoint_owner tests.test_dsv_live_log_strict tests.test_dsv_live_log_comparison -v
```

Windows 将 PYTHONPATH 设为 `src;.`，使用实际产品解释器。Windows 锁/Qt/IPC、完整源库恢复迁移、真实 SDK/柜台、实际部署运行库、冻结 DSV 全量对跑和整仓回归仍未执行。本 PR 不证明这些范围通过，也没有修改现场账本、安装运行库、发布制品或改动 main。
