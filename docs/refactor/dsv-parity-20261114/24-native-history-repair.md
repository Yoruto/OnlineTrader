# 24｜历史成交身份与迁移完整性修复

基线：`e49bcc522fe7c606e879ab4a3da83bc5d9769701`。本批不改账户授权、冷切换条件、SDK 历史一致性检查或真实账本；不启用 CI/CD。

## 修复

旧迁移器只读取 `native_trade_id`，但合法普通成交仅保存在 `sn-<原ID>` / `kq-<原ID>` 内。迁移后的历史委托有成交量而逐笔映射为空，SDK 返回相同成交时误报 `E_DSV_SDK_FACT`。

新只读 `legacy_history` 使用源库账户/提供方选择身份编码器，复用 `OnlineSimulationPort.native_trade_id` 并验证回编码。普通旧键、特殊字符/长ID编码键及显式原始字段均须绑定一致。确认委托必须有原生身份；重复逐笔、冲突原生委托、缺失/超额数量和错误提供方不再静默跳过或覆盖。每张委托的逐笔总量必须等于确认成交量。已审定外部平今历史继续保留。错误在发布新库前以 `E_DSV_MIGRATION_HISTORY` 拒绝。

不自动改写已生成的迁移库或收据；存量错误映射需从保留的源库按原冷切换预检重新生成，不能手改收据哈希。测试数据均为合成，不含现场账号和委托。

## 本次实际本地验证

Linux / CPython 3.13.5，已校验本批相关基线 Git blob 的源码与正常导入；未替换产品模块。不是完整最新工程验收。

- `test_migration_native_history`：26 方法通过。真实 SQLite 表、真实身份生成/迁移提取/SDK 历史核对函数，柜台对象为显式合成。相同测试在基线为 7 通过、16 失败、3 异常。
- `test_legacy_recovery_evidence`：21 方法通过。真实证据解析、日历及恢复前置拒绝；不执行 Windows 账户锁或整条恢复交易。
- 与另一修复批组合：新增 86 方法中 84 通过、2 个 Qt 用例因缺少 PySide6 跳过；此前01–05批 138 方法通过。组合数字不重复当作新增覆盖。
- 修改源码/测试/QA `compileall` 通过。

```sh
PYTHONPATH=src:. python -m unittest tests.test_migration_native_history tests.test_legacy_recovery_evidence -v
```

完整 Windows `recover-flat → migrate → SDK启动`、真实账户历史、全套回归、EXE 和带仓迁移均 NOT_RUN。保持草稿，不把边界函数测试等同真实恢复成功。
