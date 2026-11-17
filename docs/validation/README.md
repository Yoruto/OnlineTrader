# 验收证据索引与当前范围

入口：[机器可读索引](evidence-index-20261115.json) · [当前功能与开放问题](../功能实现与待办清单_20261110.md) · [A01–B01复测方法](../refactor/dsv-parity-20261114/16-exit-recovery-and-evidence-acceptance.md)

## 2026-11-16 真实模拟盘定向记录

上午五合约同日迁移、10:55 同秒启动、首个差异与停开、激活位修复及独立复测见[现场与根因报告](../refactor/dsv-parity-20261114/23-simnow-migration-and-activation-parity.md)。[脱敏日志](20261116-simnow-dsv-cutover/events.txt)保留现场关键行、柜台终查摘要、测试输出及原件哈希。

[下午四合约真实模拟盘复跑](../refactor/dsv-parity-20261114/24-afternoon-four-contract-acceptance.md)补录了 29 条激活位匹配、柜台终查、停开后提交错误、读锁隔离复现和 QA 命令。

## 2026-11-17 四合约与授时复测

[运行记录和独立报告原件哈希](../refactor/dsv-parity-20261114/29-clock-stability-and-four-contract-retest.md)覆盖三轮 SimNow/TqKq 对照、SDK 行情时间复现和 30 分钟授时观察。

## 不把证据来源和执行结果混写

`reported_result` 记录原作者说测到了什么；`evidence_status` 记录本次审核是否实际取得相应原件。`REPORTED_PASS / NOT_INSPECTED` 不等于失败，也不等于本次独立通过。原件不可取得时，哈希、实际命令、完整环境及未知计数保留 `null`，禁止补造。

当前索引包含：6468b00 的89项与1,564项作者记录；独立修复候选 PR #2 的实际 C01 定向执行。两者来自不同版本和测试范围，不能相加成为产品通过总数。C01候选结果随本目录保存为脱敏文本，版本和脚本 blob 身份在索引中；它不是 Windows Qt、真实IPC或交易验收。

## 每项验收最少登记

| 字段 | 要求 |
| --- | --- |
| candidate_commit | 完整 Git SHA；文档修改不自动改变被测源码版本 |
| scope / entry_point | 规则/问题ID、具体函数或产品入口、替身边界 |
| fixtures / scripts | 来源版本、合成输入身份、原文件哈希；不能用候选输出当黄金预期 |
| environment / actual_command | 实际解释器、系统、依赖和调用；建议命令与已执行命令分开 |
| counts | PASS、FAIL、ERROR、SKIP、NOT_RUN分列；未获得的不填0 |
| artifacts | 原件哈希、脱敏方式及受控位置；私有路径可别名化 |
| evidence_status / open_issues | 作者记录与独立检查分开；已知缺口不随通过数量消失 |

报告原件和真实账本不必入 Git。原持有人可补脱敏摘要、哈希与经授权的交付位置；凭据、完整真实账号、原始成交文件及未授权的本地数据不得随索引上传。哈希只证明拿到相同材料，不证明结果正确。

## 旧账本必须说明精确格式

C01来源为 `aa8bd35` 精简事件 `{id,result}`，不是仅缺发生时间。至少分别检查：精简SEND/CANCEL UNKNOWN有/无原请求、身份冲突、PR #1后有child但无at、当前完整字段、坏辅助事件后的有效决定/逐笔、读锁恢复和重开。展示修复不能改写 UNKNOWN、成交价量或订单责任。

本次文档 PR 不关闭 C01。代码修复、合并状态及 Windows 复测见 [PR #2](https://github.com/Yoruto/OnlineTrader/pull/2)。部署时按实际 SHA重新核对，不能只见PR存在就标为已交付。
