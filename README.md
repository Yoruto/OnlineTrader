# OnlineTrader

[DSV 行为基准](docs/dsv-behavior-baseline-20261113.md) · [重构计划与阶段记录](docs/refactor/dsv-parity-20261114/README.md) · [当前功能状态及开放问题](docs/功能实现与待办清单_20261110.md) · [验收证据索引](docs/validation/README.md)

## DSV 替代链开发与验收

2026-11-16 的[上午五合约迁移与模拟盘对照](docs/refactor/dsv-parity-20261114/23-simnow-migration-and-activation-parity.md)覆盖原账本副本恢复、同柜台日迁移和激活位修复。[下午四合约真实模拟盘复跑](docs/refactor/dsv-parity-20261114/24-afternoon-four-contract-acceptance.md)完成了几何拒绝、激活位与柜台空仓对照，记录了停开后的 SQLite 提交错误及读库修正。实际发单、成交保护和平仓仍待验证。

截至本次文档核对，源码基线为 `ddad472235b9fa0749646d7e3fded831040ca6c3`；该提交仅更新文档，运行代码沿用 `6468b003394315ee2fd1fa919ea6b1117db4cac9`。**A01、A02、A03、B01 所列场景的记录，不代表整个替代链、全部旧账本格式、真实柜台或正式发布已经验收。**

`6468b00` 实现明确未发送减仓的同目标恢复、后台只读历史投影、策略决定与撤单异常展示，以及有序目标/轮次比较。第16篇记载作者的 Windows/Qt/IPC 复测及源码自检数量；原始报告存于本机 `output/`，本次远端文档核对未取得原件，不能把引用数字当作新增独立验证。[适用场景与复测方法](docs/refactor/dsv-parity-20261114/16-exit-recovery-and-evidence-acceptance.md)

此前调度对照在冻结 DSV 策略循环与 TqSdk 3.10.2 目标任务之间注入同一批本地输入，比较策略目标、API 调用和 `wait_update` 出站轮次。慢部分成交仅第7、9轮采用已登记的差异；其他差异不因此获得批准。[调度回放](docs/refactor/dsv-parity-20261114/15-strategy-sdk-schedule.md) · [批准范围](docs/refactor/dsv-parity-20261114/00-evidence-and-decisions.md#用户批准的行为差异)

### 当前必须保留的边界

| 项目 | 状态与依据 |
| --- | --- |
| C01 旧精简 UNKNOWN | 在 `ddad472` 基线仍为 OPEN：只含 `id/result` 的事件会阻断历史视图。修复见 [PR #2](https://github.com/Yoruto/OnlineTrader/pull/2)，候选 `018ce03` 的定向测试为15通过、2项 Windows Qt跳过。合并与 Windows 复测状态需另核，不能因本文更新而关闭。 |
| F04 月停跨日加强 | 仍需冻结原版语义或对应批准记录；慢部分成交批准不能替代此项。 |
| Qt/IPC、柜台、制品、切换 | 分别验收。离线代码、客户端替身或本地出站包不证明真实柜台交易或已交付制品通过。 |
| 交易事实 | 展示兼容不改 UNKNOWN、订单占量、成交价量、账户权限；禁止清库绕过迁移。 |

## 执行入口与开发指引

旧 `dsv run` / `simnow run` 与新 `replacement-run` 尚属不同路径。替代链使用 `DsvSession`、显式订单控制器、独立 Journal 和 SDK 适配层；共享账户排他锁不等于共享两个发送控制者。[候选启动与回滚](docs/refactor/dsv-parity-20261114/10-sdk-startup-and-same-day-rollback.md) · [AGENTS](AGENTS.md) · [skill 规范源](docs/skills/online-trader.md)

连接、报撤单、服务安装、发布和切换均按当前明确授权。旧成功登录、历史有限额度或本文的复测命令不是新的交易授权。

## 检查与证据

在完整 Windows 项目环境运行原有定向命令；这些是复现入口，不是本次文档 PR 已执行声明：

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_dsv_schedule_comparator tests.test_dsv_refactor_repairs tests.test_dsv_round_replay -v
```

历史本机报告仍由原持有人保管。可提交脱敏的候选 SHA、fixture/脚本哈希、实际命令、环境、PASS/FAIL/SKIP/NOT_RUN 和报告哈希；不要提交凭据、真实账号或未经授权的原始账本。当前可独立取得和仅作者记录的证据已分列在[机器可读索引](docs/validation/evidence-index-20261115.json)。

## 历史验收与桌面交付

[早期设计与 G0 规格归档](docs/history/early-design/README.md)收录 2026-11-09 的三份设计稿和 OT-G0-1 规格。

`ddad472` 原 README 已**逐字节保留**为 [README.legacy-ddad472.md](README.legacy-ddad472.md)，Git blob 为 `26beae2901169b8da5e27d86cb7e11e9566c79fa`。归档仍在根目录，原相对链接保持原定位；其内容、历史测试数量与版本说明未被重新认定为当前替代链的完成状态。

原96项编号、全部逐项备注、历史实施及验收记录同样保留在[功能清单原文归档](docs/功能实现与待办清单_20261110_归档_ddad472.md)。当前入口不重算那96项的完成率。

## Windows 便携发行目录

既有 dev28 发行、目录布局、构建命令及托盘验收见[原发行说明](README.legacy-ddad472.md#windows-便携发行目录)。这些历史制品不能替代新替代链的制品验收；本次 PR 不构建或发布安装包。

## Windows 校时

原脚本和使用说明见[历史校时说明](README.legacy-ddad472.md#windows-校时)。其中原内核的跳钟核对/F5流程仅适用于所记旧路径，不应套成替代入口的额外门禁。自动校时需求、运行中系统时间修改及新链路的完整验证仍单列，不由这次文档修订授予执行权限。

## 打开账户管理与交易工作台

旧入口、参数编辑、报表、默认启动及维护命令完整保留在[原工作台说明](README.legacy-ddad472.md#打开账户管理与交易工作台)。使用前核对入口、所部署版本及当前授权，不能把旧制品操作记录当作替代链已切换的证明。

## 打开DSV参数中心

[原参数中心、模板与偏移计算说明](README.legacy-ddad472.md#打开dsv参数中心)保留在原文归档。预览、保存与运行中应用不是同一动作；不得由计算结果自动取得交易权限。

## 运行最小状态台

[原G1状态台与旧Kernel入口](README.legacy-ddad472.md#运行最小状态台)仅按其既有范围使用，不作为新替代控制器的入口。

## 独立检查

[原完整检查与历史范围](README.legacy-ddad472.md#独立检查)保留不变；当前验收需绑定候选完整SHA、实际入口及[证据状态](docs/validation/README.md)，源码、Qt/IPC、柜台与制品分别记录。
