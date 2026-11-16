# 25｜拒绝决定、分钟消费对照与桌面入口回归

基线：`e49bcc522fe7c606e879ab4a3da83bc5d9769701`。本批独立于历史成交迁移修复，不改入场规则、订单发送权限、配置或数据库格式；不启用 CI/CD。

## 对照器修复

新增 `DSV_ENTRY_TRACE/1`，本版只覆盖 `GEOMETRY_BLOCK` 与 `NO_CHASE_AFTER_ACT` 两类拒绝及 `last_signal_bar`，不宣称覆盖全部拒绝。

冻结原循环原有哈希校验保持。QA观察器只观察实际执行的原代码行：8027（原几何计算之后）和8062（原禁止追价分支写入分钟之后）；输出真实分支顺序和源局部变量。分钟来自原状态，不用候选生成参考。`_trig/_lvl` 只在600秒日志分支更新，故不作为逐轮证据；方向、触发价及激活距离取实际源变量并保持原float计算顺序。异常观察不能被原版宽泛Exception捕获后伪装成通过。

比较器核对覆盖范围内的有序拒绝、方向、触发价、激活价、消费分钟与持久分钟。价格只统一数值表示，不设置误差带。旧参考缺字段返回 `entry_evidence_missing`，必须重新生成，不能从候选补参考。原目标、到期、名义净亏、日停及平仓同步比较保留。

新增合成场景生成器：上午棕榈油四轮含同分钟换方向、下一分钟以及文字限频内的新分钟；关闭开关、两向等值边界。行情和参数来自已公开脱敏记录，其余输入为合成，不冒充10868轮现场完整录制。

```sh
PYTHONPATH=src:. python -m qa.dsv_parity.activation_samples --output <new-directory>
# 在具备冻结原包、Python3.14和Windows产品环境的隔离QA中手动运行compare_original_loop。
# 旧样本（含已批准第7/9轮）也须重新生成源证据；禁止用旧无字段结果判通过。
```

## 产品与冒烟回归

`runner` 同轮补记的几何事件规则ID从错误D05改为D02，订单/目标算法不变。上午已经修复的桌面公开常量导入不重复改写；新增依赖导入检查和实际Qt窗口测试，防止再次遗漏桌面调用方。

## 实际本地验证与边界

Linux / CPython3.13.5，正常导入，未启动网络SDK或CI：

- 比较器及源观察器机制31方法通过；前18个比较器测试在旧实现中14失败、4通过。观察器机制用显式源局部变量夹具，不等同运行原版字节码。
- 真实DsvSession/显式控制器/SQLite 5方法通过；仅DEV锁用测试POSIX flock夹具，发送端RecordingPort。覆盖四轮状态持久化、开关、等值和D02；D02反例在旧runner失败。
- 桌面依赖检查1方法通过；2个真实Qt测试因本机未装PySide6明确SKIP。
- 合并两批修改后新增86方法：84通过/0失败/0异常/2跳过；01–05批138方法通过；compileall通过。无需启用工作流。

```sh
PYTHONPATH=src:. python -m unittest tests.test_dsv_entry_evidence tests.test_dsv_activation_evidence_runtime tests.test_dsv_workbench_import_smoke -v
```

本轮仅数据方式检查冻结3.14代码对象的行表/局部变量，未执行其字节码。真实Python3.14冻结循环串联回放（含第7/9轮）、Windows Qt/IPC、真实SDK和柜台、完整最新工程与制品均NOT_RUN。观察站点虽有静态证据与哈希约束，仍需真实原循环复跑后才能把此PR视为完成对跑验收。保持草稿。
