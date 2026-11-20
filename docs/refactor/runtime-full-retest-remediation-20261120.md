# Standalone Runtime 独立全量复测缺陷修正记录

> 日期：2026-11-20
> 状态：源码修正并完成编译检查；独立功能复测、柜台验收和打包均为 `NOT_RUN`。

## 1. 输入证据和结论边界

本次修正依据本机最新独立报告 `output/independent-full-runtime-ui-retest-20261120/test-report.md`。该报告的被测基线为 `9817f11a4b03203753df30134d5a3d807ab24f07`，结论为 `PASS 22 / FAIL 4 / BLOCKED 6 / NOT_RUN 6`；完整回归为 1910 项中 1878 PASS / 10 FAIL / 22 ERROR。

报告已证明临时会话账本、单机唯一 Owner、旧 ledger 只读、历史缓存、配置分层和 Qt 关闭后 Owner 存续等主合同，但不能由此声称当前修正候选已验收。报告原件位于 Git 忽略的 `output/`，本记录仅固化可执行的根因、修正范围和后续复测条件。

## 2. 第一失败与根因

| 报告项 | 第一失败 | 根因 | 本次修正 |
| --- | --- | --- | --- |
| F-01 完整源码回归 | `WorkbenchSnapshot` schema 拒绝 Owner 已输出的顶层字段 | 读侧生产者已增加 `config_snapshot`、`capabilities`、`diagnostics`、`contract_catalog`，但 `workbench-ipc.schema.json` 仍使用旧的封闭字段集 | 为新投影和脱敏 `account_display` 补齐有界 schema，重算并更新资源 SHA-256 清单 |
| F-01 历史测试合同 | 多个测试引用已退役的私有 Qt API、跨重启 pending 和 ledger handoff | 柜台权威/进程临时账本改造后，旧测试没有迁移到当前公开 CommandBus 与 session ledger 合同 | 迁移测试定义，保留同进程未知结果、严格回执身份、全新会话账本与公开 CommandBus 委托合同 |
| F-02 Runtime QA replay | history 查询将执行 `ledger.db` 当作 `account-history.db` | `ViewProcess` 允许缺省 history 路径并回退到执行库，QA 入口正好触发该分支 | history 路径改为必填；QA 传入独立缓存路径；缓存查询失败记录为可见降级，不中止 Owner 回放 |
| F-03 Qt 账号显示 | Runtime 顶部与账户页显示“未提供/请先添加模拟账号” | `ClientStateStore` 将投影冻结为 `MappingProxyType`，UI 只接受 `dict`；Runtime 模式的账号页仍走本地账号库 | 显示层接受 `Mapping`；Runtime 模式仅显示已脱敏账号投影，禁用本地账号编辑/切换入口 |
| F-01 诊断文案 | 旧断言仍期待“几何条件不满足/已跑过激活位” | 产品文案已改为更可诊断的具体失败条件 | 测试预期与“日内振幅超出开仓上限/已越过不追价线”对齐，不回退产品诊断精度 |

## 3. 状态、数据与回退

- CommandBus pending 只在当前 Workbench 进程内存活；Owner boot 在同进程变化时保留原 ID 查回执，Workbench 重启后从当前 Runtime/柜台基线开始。
- Runtime 每个 boot 创建新的进程临时执行库；旧执行账本不交接、不阻塞启动、不成为跨设备交易权威。
- `account-history.db` 是柜台可重建历史/收益缓存，与执行库分离。缓存缺失或损坏不能导出空仓、命令终态或发单权威。
- 本次不引入第二套 UI、第二个 sender 或在线 QA 输入口。若需回退，回退整个候选版本，不在运行中恢复旧 pending/handoff 路径。

## 4. 本次已执行检查

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -X utf8 -m compileall -q src tests qa
```

结果：`PASS`，exit code 0。同时已解析 JSON schema/资源清单，并确认两个资源摘要与 `docs/spec-extension-workbench.json` 匹配：

- `ipc.schema.json`: `85e5b5dff56702a60674b90005e0f9d26e2211fb5e1637248f9192ecff93d054`
- `workbench-ipc.schema.json`: `1370379a9967f25ebd3655d0203b3b01d3fe2072fc774d7d0db400e3744a326a`

根据当前授权，未运行单元、GUI、回放、柜台连接、真实委托或打包检查。因此不能将源码修正和编译通过表述为缺陷已关闭。

## 5. 下一次独立复测

后续取得明确测试授权后，由未参与本次修正的独立测试 Agent 使用 Standalone Runtime 实际入口执行：

1. 先运行本次受影响模块，确认 Workbench schema、CommandBus、session ledger、Runtime Qt 账号显示和 QA replay。
2. 再按用户要求决定是否执行完整源码回归；不把 Legacy/Kernel 直连回放当作 Runtime 产品验收。
3. 在可见 Qt 中确认顶部和账号页都显示同一脱敏 Runtime 账号，且不出现本地账号编辑权限。
4. 在历史缓存缺失和损坏两种代表故障下，核对 Owner 继续、回放结果、明确错误码与诊断日志。
5. 将原报告中不稳定的自动偏移连接失败作为独立观察项；当前没有稳定复现和第一失败证据，本次未做猜测性修改。
6. 报告必须记录完整候选 SHA、入口、环境、用例和 `PASS/FAIL/SKIP/NOT_RUN` 计数；柜台休市、缺少第二台 Windows 和未打包均继续如实报告。
