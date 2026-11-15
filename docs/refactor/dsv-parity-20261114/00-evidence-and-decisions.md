# 00｜证据、术语与决策登记

返回 [计划总览](README.md)。本文件将来源事实与拟议设计分开；除明确标为来源事实外，架构和任务均为方案，不是DSV已实现行为。

## 用户批准的行为差异

2026-11-15 确认保留 OnlineTrader 的逐笔成交核对。在固定慢部分成交样本中，第 7 轮锁定原 5 手目标，按已证实的 3 手补开剩余 2 手。第 9 轮保留 5 手实际仓位，无额外反向平仓。原版同样本误判手工减仓并产生的额外交易作为对照证据。验收须核对这两轮的目标、逐笔数量和报撤单，另外七轮仍要求按样本一致。[原版轨迹及固定回归](15-strategy-sdk-schedule.md)

## 1. 基准与本次实际核查范围

- OnlineTrader固定提交：`af8b53b16592bfbd5b09185e1eee48120b3ead34`；其父提交为`7a2dd6952742989d85a0a2c193808d5914a20717`。
- 本轮重新读取仓库引用、根AGENTS.md、既有DSV确认记录、行为基准相关段落；依据上下文已取得的同提交关键函数，规划调用方与状态退役。
- 本轮重新读取用户上传的`DSV实盘1.0.zip`，校验EXE、SDK版本文件和`target_pos_task.py`字节；没有执行EXE、导入原应用初始化、连接账户、请求NTP或操作订单。
- 原应用业务元数据来自先前生成并本轮重新读取的《DSV_跳钟与撤单原版核对证据.txt》。这不是完整业务源码，也不构成新的在线实测。
- 本轮是规划，不重跑历史“1532项”等检查；本目录没有任何新的产品测试通过声明。

## 2. 可复核指纹

| 材料 | SHA-256 | 解释 |
| --- | --- | --- |
| 原包`trading/trading.exe` | `aabeafd83d265ab9b1cf5cc3fd0e9b0f21963ddcc95daa70feefc209997269a3` | 本轮从上传ZIP重新计算；26,300,596字节 |
| 原包`tqsdk/lib/target_pos_task.py` | `d88ed96d447dac01542f84acda4f1222f4a514a68863c9939deac3a325078beb` | 本轮重新计算；46,128字节 |
| 原包`tqsdk/__version__.py` | `846a5b3a39664a374c814ebca5cd2a49c6199e88e0241d10034c0d29117932e5` | 内容标注`3.10.2` |
| 先前提取的原应用gui对象 | `07c82c9ada765284ed5a929227024cbf192f5f72192636900740d9a5764032ea` | 来自现有取证记录；本轮没有重新提取对象 |

禁止将本机私有路径、账户配置或整个原包提交为重构依赖。需要原包的QA任务在用户授权的隔离位置读取，并在结果中记录上述指纹。不得仅凭同名文件认定版本一致。

## 3. 来源索引

以下代码链接均固定到本轮基线。历史工程报告是线索，不因写了“已实现”就自动成为原版行为真值。

| ID | 来源 | 支持的结论与限制 |
| --- | --- | --- |
| E01 | 上传原包`trading/_internal/tqsdk/lib/target_pos_task.py`，22–104、639–668、690–698、744–756行 | 单账户合约任务约束、目标设置后需继续wait_update、默认净持仓范围、不能混用直接报单、追价撤单、终态与成交细节等待。仅证明随包SDK控制流 |
| E02 | 上传原包`tqsdk/__version__.py` | 原包标注3.10.2，不证明当前可安装环境兼容性 |
| E03 | 本轮重读的`DSV_跳钟与撤单原版核对证据.txt` | 原函数元数据将NTP/环境自检描述为告警，不参与交易判断；不能据此排除其他时间风险 |
| E04 | [现有DSV确认记录](https://github.com/Yoruto/OnlineTrader/blob/af8b53b16592bfbd5b09185e1eee48120b3ead34/docs/decisions/dsv-confirmation-20261109.json) | 业务按DSV，先前非DSV推荐撤回；保存配置并不等于所有执行语义已验证；不授予连接和交易权限 |
| E05 | [现有行为基准](https://github.com/Yoruto/OnlineTrader/blob/af8b53b16592bfbd5b09185e1eee48120b3ead34/docs/dsv-behavior-baseline-20261113.md) | A/M/S等分类、取证定位和历史未验收项；不是新版合格证明 |
| E06 | [execution.py](https://github.com/Yoruto/OnlineTrader/blob/af8b53b16592bfbd5b09185e1eee48120b3ead34/src/online_trader/kernel/execution.py)，progress/tick | 自定义撤单超时升级、执行上下文和price_only关联 |
| E07 | [scheduler.py](https://github.com/Yoruto/OnlineTrader/blob/af8b53b16592bfbd5b09185e1eee48120b3ead34/src/online_trader/kernel/scheduler.py) | 时钟故障、特殊保护、调度和重置后行情条件相互关联 |
| E08 | [dsv_strategy.py](https://github.com/Yoruto/OnlineTrader/blob/af8b53b16592bfbd5b09185e1eee48120b3ead34/src/online_trader/kernel/dsv_strategy.py)，_blocked/_final_gate/on_market | 策略、权限、风控复核、观察回补与状态消费混合，需要按职责拆分 |
| E09 | [engine.py](https://github.com/Yoruto/OnlineTrader/blob/af8b53b16592bfbd5b09185e1eee48120b3ead34/src/online_trader/kernel/engine.py)，admit_entry/_dispatch | 现有提交确认后派发、上下文和预算约束；不能仅删门禁保留残余状态 |
| E10 | [online_dsv.py](https://github.com/Yoruto/OnlineTrader/blob/af8b53b16592bfbd5b09185e1eee48120b3ead34/src/online_trader/kernel/online_dsv.py) | 新旧额度、账户事实、原周期恢复、目标归属与保护校验 |
| E11 | [dsv_simulation.py](https://github.com/Yoruto/OnlineTrader/blob/af8b53b16592bfbd5b09185e1eee48120b3ead34/src/online_trader/runtime/dsv_simulation.py) | 当前启动、回补、比较、日志注册、跳钟保护均进入运行循环 |
| E12 | [strategy_log.py](https://github.com/Yoruto/OnlineTrader/blob/af8b53b16592bfbd5b09185e1eee48120b3ead34/src/online_trader/runtime/strategy_log.py) | 操作日志索引、备用回执、重建及旧启动发现体系 |
| E13 | [pyproject.toml](https://github.com/Yoruto/OnlineTrader/blob/af8b53b16592bfbd5b09185e1eee48120b3ead34/pyproject.toml) | 当前Python>=3.13,<3.14；可选SDK固定3.10.4；PySide6固定6.8.2.1。不是新方案最终版本决定 |
| E14 | [AGENTS.md](https://github.com/Yoruto/OnlineTrader/blob/af8b53b16592bfbd5b09185e1eee48120b3ead34/AGENTS.md) | 单owner/单writer、必要事实提交后发送、历史授权边界及环境隔离；替换前继续适用 |
| E15 | [官方TargetPosTask文档](https://doc.shinnytech.com/tqsdk/latest/reference/tqsdk.lib.html) | 2026-11-14读取，页面显示3.10.1；支持接口层约束，不替代3.10.2原包行为 |
| E16 | [官方程序结构文档](https://doc.shinnytech.com/tqsdk/latest/usage/framework.html) | wait_update驱动及跨交易日重启建议；重启时点仍需结合原版与未结订单取证 |
| E17 | [新版跳钟测试](https://github.com/Yoruto/OnlineTrader/blob/af8b53b16592bfbd5b09185e1eee48120b3ead34/tests/test_online_dsv.py)、[日志测试](https://github.com/Yoruto/OnlineTrader/blob/af8b53b16592bfbd5b09185e1eee48120b3ead34/tests/test_simnow_comparison.py) | 可复用的旧测试素材；不把其自定义断言直接当作DSV真值 |

## 4. 来源优先级与冲突处理

顺序：当前用户明确要求 → 固定版DSV在同输入下的可复核实际行为 → 相同版本业务源码/字节码控制流 → 随包SDK实现 → 同版说明 → 既有工程取证报告 → 外部当前文档。不同层有冲突时记录输入、版本和分支；不能把后面的“推荐做法”替代前面的实际行为。

运行现象也必须可重复且输入匹配。不同报价、配置或时间源下的两条日志不是可靠反例。原版日志整秒精度不能证明毫秒先后，代码注释不能替代异常实际控制流。

规则分类：

- **D：DSV业务规则**。需要明确原版出处和用例；没有完整证据的标为D-PENDING。
- **U：用户明确附加能力**。例如自动系统校时、可部署长期运行、完整诊断。与DSV差异单列。
- **I：最小事实与操作正确性**。单执行者、去重、原订单占用、用户命令顺序、账户隔离。它们不是DSV策略公式，不能伪装成额外风险参数。
- **X：工程自发增强**。没有D/U来源，默认删除候选或降为纯观察。不能仅凭名称含risk/safety就自动保留。

任何新增交易阻挡必须写明：影响哪个账户/合约/订单，如何解除，是否阻止减仓，和DSV的差异。不能用一个全局recovery布尔值概括所有故障。

## 5. 初始规则登记（不是已完成取证清单）

| 规则ID | 内容 | 初始分类/状态 | 处理 |
| --- | --- | --- | --- |
| D01 | 普通触发公式、先多后空、同轮顺序 | D-PENDING，E05定位 | 独立验证后移植 |
| D02 | 几何过滤、等号、提示频率与日停区别 | D-PENDING | 不把提示转换为全天停开 |
| D03 | 滞回、零值、换线及止损后冷却 | D-PENDING | 不以统一默认替代分支 |
| D04 | 原生分钟机会及拒绝分支消费 | D-PENDING | 普通实时、回补、重启分别取证 |
| D05 | 固定/自动手数、保证金、保底一手 | D-PENDING | 保留数值运算顺序 |
| D06 | 开盘等待、尾盘禁开与收盘目标 | D-PENDING | 保留原时间来源及边界 |
| D07 | 夜盘参考和早盘延续 | D-PENDING | 普通/延续年龄规则不得强行统一 |
| D08 | 止损、止盈、80%保护及优先级 | D-PENDING | 真实均价与理论价不混用 |
| D09 | 半仓两档、保本、移动止盈 | D-PENDING | 奇数手和部分成交必须独立验 |
| D10 | 日/月停止、净点数和次数 | D-PENDING | 不能用账面金额替代策略统计 |
| D11 | 价格回调和追价 | SDK部分已证，应用回调待证 | 不直接使用SDK默认ACTIVE替代DSV回调 |
| D12 | 普通撤单等待终态及成交核齐 | SDK已证E01 | 保留等待；无证据不加全局30秒停开 |
| D13 | 任务退出清理30秒超时 | SDK已证E01 | 与正常撤单计时分开 |
| D14 | 启动/停止/关闭/重启状态 | D-PENDING | 不把F6或关窗等同于平仓/杀进程 |
| D15 | 跨日、午休、换月及外部仓 | D-PENDING | 未确认行为不得发布为全量兼容 |
| U01 | 自动校准系统时钟 | 用户需求，实施待审批窗口 | 独立运维模块，不改变策略公式 |
| U02 | 中文可解释日志与诊断 | 用户需求 | 增加信息，不增加交易拒绝 |
| U03 | 长期部署与界面独立 | 用户需求 | UI退出不能终止已接管订单管理 |
| I01 | 单账户单执行控制者 | 保留 | 含SDK内部任务、CLI、GUI和旧引擎 |
| I02 | 原生订单/成交身份、去重与数量 | 保留 | 未核齐不能当成终结 |
| I03 | 模拟/实盘及凭据隔离 | 保留 | 删除有限测试额度不等于扩权 |
| X01 | 普通撤单30秒升级全局恢复屏障 | 删除候选E06 | 可保留告警，不自动释放或重发 |
| X02 | 跳钟锁存及price_only例外系统 | 整簇替换候选E07 | 正常调度接管后再删除 |
| X03 | 重复额度/附加预算/跨层重复否决 | 逐条溯源E08–E10 | 保留D规则、合并事实校验、删无证增强 |
| X04 | 完整逐Tick路径作为永久准入证明 | 待溯源 | 只要求原版决策实际需要的数据 |
| X05 | 通用多策略热切身份体系 | 当前单策略目标不需要 | 保留会话身份，退役热切平台职责 |
| X06 | 展示日志恢复状态参与控制 | 退役 | 记录/迁移职责移出交易环 |

## 6. 架构决策记录（ADR）

| ADR | 拟议决定 | 通过条件/证据 | 未通过时 |
| --- | --- | --- | --- |
| ADR01 | 保留外围、建立独立精简核心 | 业务功能目录、模块依赖清单完成 | 不开始批量删代码 |
| ADR02 | 原版SDK作为第一执行候选 | G1验证目标范围、生命周期、版本和回调 | 转为薄的显式订单适配器，不保留两套sender |
| ADR03 | 同合约唯一执行控制者 | 手动与自动路由、暂停任务、外部仓边界全测 | 不开放混用入口 |
| ADR04 | 时钟检测不改变策略权限 | DSV时间分支证据、异常同输入验证 | 保留旧引擎运行，不在生产关门禁试错 |
| ADR05 | 自定义撤单超时不升级交易状态 | D12/D13确认、迟到回报测试 | 旧行为只在隔离旧路径保留 |
| ADR06 | 日志展示可降级，恢复事实不可悄丢 | 存储分层及故障测试 | 禁止将所有异常一律吞掉 |
| ADR07 | 冷切换，旧数据库只读迁移 | 无仓无活动单核清、业务日状态一致 | 不迁移，原控制者继续负责 |
| ADR08 | 确定SDK内部子单与提交前记录的边界 | 见01的G1-B；明确是否改变既有每子单CommitAck保证 | 不得宣称仅持久化目标就满足原保证 |
| ADR09 | 常驻壳与受控交易进程轮换 | 无未结状态切换窗口及跨日用例 | 不按定时器硬杀交易进程 |
| ADR10 | 独立命名空间和存储版本 | 无旧Kernel隐式依赖、独立制品检查 | 不发布长期双栈 |

本次用户授权拟定并提交方案，不等于已经批准ADR08涉及的保证变化、开启实盘、混合外部仓或带仓热迁移。实施选择应依据证据记录，不反复要求用户重新确认已明确的DSV业务规则。

## 7. 必须解答但不能凭经验填空的问题

Q01 原DSV创建、停止和复用TargetPosTask的精确位置；Q02 手工下单与自动任务的协调方式；Q03 持仓归属是账户合约整体还是策略子集；Q04 重启时真实的自动启动、计数、分钟消费和仓位恢复；Q05 时间跳变对原业务各计时的实际影响；Q06 所用SDK与Python、数据依赖组合；Q07 每个保护分支的价格和数量基准；Q08 原日历及夜盘交易日认定；Q09 原应用整段异常捕获后的状态；Q10 是否能获得有权使用且可构建的业务源码。

这些问题通过离线源核对、受控同输入观察或明确授权的原版测试回答。待核不是自动否决用户策略，也不是产品运行时再增加一个门禁；它是开发与发布阶段的未完成事项。
