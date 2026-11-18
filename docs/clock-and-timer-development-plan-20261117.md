# OnlineTrader 时钟、计时器与无行情订单维护开发方案

版本：V2.1 · 初版 2026-11-17，修订 2026-11-18 · 状态：开发规格和拟实施任务，不是代码完成或发布验收报告。

原 V2.0 审查基线：`01ecddaa43e6a1dde27ce9878de2f55e72fb3314`；原上游时钟改动：`7d6c646ba60410f7cfce8cca21440f2612b62ed8`。本次 V2.1 对照主线源码 `5359957935c0f53c32055966d85a2d35cfc4feb4`（2026-11-18）。历史缺陷描述仍按原固定基线解读，最新实现按实际文件核对；不得把已修复内容重复当作当前缺陷。本次仅更新文档，不改交易代码、账户、数据、Windows 系统时间或 CI/CD，不授权真实账户连接或下单。

文中“必须”是拟实现的验收要求；“建议默认值”不是已经验证的生产参数。新增模块、字段、测试和命令均为实施目标，不能据本文推定已经存在或已经通过。

## 0. 执行摘要

保留本次修复中“不再因报价略领先本机就静默丢弃”的方向，取消“最后行情时间覆盖当前时间”的实现。采用三个明确的时间用途，而不是建立三套授时系统：

| 用途 | 第一版实现依据 | 不允许的替代 |
| --- | --- | --- |
| 当前时刻、日历、授权窗口 | 应用内多源 NTP 校准的可信 UTC（`CalibratedClock`）+ 有界守时 | Windows 系统 UTC、所有合约报价的最大值、某合约最后报价 |
| 等了多久、超时、周期检查 | 可注入的单调计数 | Tick 次数、定时回调次数、墙钟直接相减 |
| 报价发生时间、原生分钟和源数据身份 | 选定行情提供者的原始时间戳、盘段及来源身份（TqSdk / 新增 CTP） | 本机接收时刻、被修改以“通过检查”的时间，或将行情时间当作全局 UTC |

继续采用同一个交易 owner（唯一负责交易状态、事实提交和派发的执行线程）。计时检查与行情更新独立触发，但不得新增第二个下单者。先核对本轮已取得的账户、订单与成交事实，再判断到期责任；无报价也维护订单，有合法执行条件才形成新的报单。

**V2.1 决策覆盖 V2.0 旧表述：**当前主线已有应用内 `CalibratedClock`（多源 NTP 锚点、单调推进、守时与恢复）。正式交易入口必须由它或经等价验证的可信 UTC 实现提供决策时间，禁止隐式退回 `SystemClock` / Windows 墙钟。保持 NTP 网络访问在后台，不在 Tick 热路径请求授时。新增 **CTP MdApi 行情只读接入与秒级目标**，不把 CTP 行情时间当作授时根，不引入 vn.py 整套运行时，不在本阶段切换交易 owner。保留账户排他、持久化提交屏障、订单身份、UNKNOWN 占用及成交核对；详见第 19 节。

实施顺序为：**先固定反例及时间合同 → 最小运行链修复 → 共用入口与恢复 → Windows/DSV 定向验收**。不得用零委托、零成交、正常行情抽样匹配替代交易闭环验收。

## 1. 目标、范围与证据等级

### 1.1 目标

消除慢钟导致的静默行情过滤；保证静默行情不会冻结等待、同步和收盘逻辑；保证迟到计时事件不会重放过时交易动作；在断线、重启、休眠或时间异常时保留已经承担的订单与持仓责任。

正常环境下，信号、分钟消费、数量、保护条件和等待边界继续按冻结 DSV 对齐。异常环境下的改进单独登记为工程差异，不宣称与原版逐指令相同。

### 1.2 非目标

不修改策略公式、价格追单规则、风控额度、订单归属、名义损益口径、授权范围或账户切换规则。不默认迁移桌面执行器及账本，不新增离线交易产品入口，不引入 vn.py、LEAN 或 NautilusTrader 作为新依赖。不重启 CI/CD；开发者使用手工命令与已有本地测试入口。

不承诺硬实时、毫秒级 Windows 系统授时、无网络环境的精确 UTC，或定时退出必然在柜台成交。纳秒字段表示存储单位，不是执行精度保证。用户提出的「有行情更新时 ≤1 秒」是待实测的端到端性能验收目标，不是 CTP 服务商的无条件承诺；静默行情不要求凭空生成每秒一条 Tick。

### 1.3 证据等级

- **源码事实**：固定基线中的函数和调用链，见附录 A 的 C01–C11。
- **DSV 行为证据**：仓库已有冻结发行版静态取证，参考 EXE SHA-256 为 `aabeafd83d265ab9b1cf5cc3fd0e9b0f21963ddcc95daa70feefc209997269a3`。本次文档没有重新执行原 EXE，静态定位不等于柜台验收。[C04]
- **外部设计参考**：开源源码、项目维护方和官方文档，见附录 B。仅支持基础设施取舍，不替代 DSV 业务真值。
- **新实现验收**：本文全部为 NOT_RUN。先前会话中的函数探针是提出反例的依据，不转记为本次整仓通过；上午日志也不归属于尚未发布的新实现。

## 2. 问题清单与修复追踪

| ID | 优先级 | 基线中的具体问题 | 影响与修复方向 |
| --- | --- | --- | --- |
| F01 | P1 | `sdk_adapter.step()` 取 `max(quote_times)`，`runner._evaluate()` 又用每合约 `native_time` 覆盖 `wall_time`。[C01][C02] | 报价静默时相对时间不增加。恢复独立当前时间，等待改用单调计数。 |
| F02 | P1 | 入场确认、被动等待、保护退出同步及价格上下文仍读取上述时间。[C02][C03] | 60 秒、3 秒、5 秒、90 秒等条件可能冻结。按实际起算事件逐一迁移，不只替换一个变量。 |
| F03 | P1 | `_market()` 按最后报价查询当前日历；收盘判断收到的 wall 与 native 可能等价。[C01][C02] | 错过尾盘窗口、休市仍显示盘中。当前时段与报价所属时段分开。 |
| F04 | P1 | 首次观察旧快照时静默年龄为零；单合约的全局报价差也为零。[C02] | 旧数据可能通过正数年龄阈值。源年龄、观察静默、时间质量独立保存。 |
| F05 | P1 | `controller.plan()` 在无 tick 时直接返回，runner 也可能只 settle。[C02][C03] | 即使计时已到期，必要撤单规划仍可能被价格门槛遮蔽。拆分维护与价格规划。 |
| F06 | P2 | `read_offset_inputs()` 要求 `0 <= instant-source <= 15`。[C05] | 实时偏移计算再次受零正向容差影响。复用时间解释，不静默等成泛化超时。 |
| F07 | P1/范围 | 桌面 `run_desktop_start()` 仍调用旧 `dsv_simulation.run()`；替代链是另一个入口。[C06] | 修一个入口不能宣称全产品已修。共享基础设施并分别验收，不隐式切换引擎。 |
| F08 | P2 | 校时脚本的交易进程识别未覆盖所有新入口；仅靠字符串检查存在维护竞态。[C07] | 补充识别并设计启动/维护互斥，不在有责任时强制拨钟。 |
| F09 | P1/验收 | 一条冻结对照路径直接 `apply_round(frame)`，绕过 SDK 适配器。[C08] | 核心对照通过不能证明原始行情输入正确。必须新增全输入链测试。 |
| F10 | P1/设计 | 若只新增独立 timer 而不定义身份、顺序、迟到和恢复，可能引入旧 timer 操作新目标等问题。 | 这是预防性设计风险，不声称已发生于现场；按第 6、7、10 节消除。 |

最小反例均应编码为确定性测试：报价固定 09:40:00 而实际经过 61 秒；14:59:00 后静默跨过收盘；首次收到两分钟前快照；本机慢 970 毫秒且行情持续更新。反例中的时间是测试输入，不是假称原始运行日志。

## 3. 外部项目比较与架构决策

| 参考 | 直接支持的结论 | 采用 | 不照搬 |
| --- | --- | --- | --- |
| vn.py EventEngine [R01] | 独立计时线程投递 EVENT_TIMER，由事件线程消费 | 时间到与 Tick 到是不同事件；业务集中处理 | 按收到几次 timer 推算几秒；增加第二个交易线程 |
| NautilusTrader clocks/timers [R02] | 可注入时钟、测试/实盘时钟、计划时间与创建时间区分 | FakeClock、到期检查记录、迟到量 | 纳秒单位等于纳秒保证；迁移整套运行时 |
| LEAN Scheduled Events [R03] | 实盘定时与行情到达有时序差异；定时可能读取旧数据 | 定时唤醒后复核事实、价格和事件顺序 | 将其多线程调度模式直接套进当前 owner |
| TqSdk [R04][R05] | 定时与 wait_update 同循环；deadline 为 Unix 时戳；部分内部时间以行情锚点加本地经过时间估计 | 持续驱动 SDK、严格区分 SDK deadline 与业务 deadline | 让最后报价冻结所有时间；把行情外推当全账户 UTC 真值 |
| Python [R06][R07] | 单调时钟用于间隔；回调可以提前，等时回调顺序不能依赖 | 到点重检比较符；自行定义业务顺序 | 回调被调用就等于条件满足 |
| Tokio MissedTickBehavior [R08] | 周期迟到需显式选补发/延后/跳过 | 合并过期检查，不补发历史交易动作 | 追赶所有旧周期并连续撤改单 |
| W32Time、QPC、chrony [R09–R11] | 系统授时与间隔计量是不同职责，质量不只有 offset | 一个系统调整者；应用只读监测 | 自建第一版 NTP 伺服、反复立即拨钟 |

### ADR-01（V2.1 修订）：统一可注入的可信 UTC，不以系统墙钟决策

保留 `utc_now()`、`monotonic_ns()` 和一致性采样接口；正式决策 UTC 必须来自已完成多源 NTP 校准的 `CalibratedClock` 或经等价测试的可信实现，不能来自 Windows `datetime.now()` / `time.time()`，也不能被行情最大值覆盖。现有校准器作为基础继续完善锚点冲突收敛、守时误差预算、恢复和状态公开；不另造第二套校准器，不主动调整 Windows 墙钟。SDK 的 Unix deadline 兼容调用必须隔离，不作为策略时钟。

### ADR-02：单 owner 与现有事实账本保持不变

计时检查只能提出重新评估请求。交易动作仍经过同一状态机、事务和已提交效果派发；诊断线程不持有可发送接口。

### ADR-03：先保留 DSV 正常行为，异常改进单独列账

起算事件、比较符、条件优先级、部分成交语义与源程序核对。不能因为“更合理”就悄悄替换追单、保护、报价年龄开关或交易日规则。

### ADR-04：先测试、后改造；正式入口迁移另案

既有核心回放继续保留；新增实际适配器输入测试。桌面与替代链分别接入相同接口，不同时经营同一账户，不因为共用时钟就自动迁移账本。

### ADR-05（V2.1 新增）：CTP 只作为行情提供者，发布采用逐级授权

`OnRtnDepthMarketData` 只做快速复制/入队；策略消费与交易 owner 在既有路径推进。第一阶段仅 **CTP_MD_SHADOW** 只读对照，不创建 CTP 下单 API、不开第二个发送者；与 TqSdk 对照延迟、源字段和 DSV 行为。只有通过事件身份、日历、信号、报单前有效报价和持仓保护验收，才另案审批 `CTP_MD_PRIMARY`。用户要求更新延迟 ≤1 秒，分源到策略/界面两条指标验收（第 19 节）；不以 CTP 常见约 500ms 推送机制冒充确定性的 ≤1 秒网络 SLA。

## 4. DSV 兼容合同

以下阈值来自已有取证或当前实现，生产使用配置值，不硬编码本文举例。每一项测试都应覆盖小于、等于、大于边界。[C02][C03][C04]

| 事项 | 必须保留的业务语义 | 时间迁移要求 |
| --- | --- | --- |
| 普通开盘等待 | 普通等待含终点，延续等待有不同严格边界及盘段范围 | 读取当前 UTC 和日历，不以新 Tick 到达为计时前提 |
| 被动等待 | 默认 3 秒；到期 `>=`；起点为相应 pending 建立事件 | 单调起点，不改成首次柜台回报或每轮重置 |
| 入场确认超时 | 默认 60 秒；严格 `>`；已有成交进入原确认/保护路径 | 单调经过；不可扩大为所有部分成交残单统一超时强平 |
| 保护退出同步 | 当前实现有 5 秒等待及原重复请求规则 | 仅在原来允许的业务事件刷新起点；不能每轮刷新，也不能擅自禁止原本存在的刷新 |
| 价格上下文 | passive/cap 90 秒、aggr1 60 秒、market 120 秒是冻结源取证；当前替代实现覆盖范围须逐项核对 | 建立映射表；未实现的源差异另立任务，不以迁移时钟名义宣称全部齐全 |
| 收盘阶梯 | 以首次收盘处理后的经过时间计算剩余阶梯；3 秒为目标刷新检查，不是所有撤单周期 | 用单调间隔保持原起点和公式，不改成另一套倒计时 |
| 收盘触发 | 冻结 DSV 原逻辑包含行情接近收盘 OR 运行时当前时刻接近收盘 | 保留两个不同输入；把原运行时当前时刻映射至可信 UTC，而不是 Windows 墙钟；异常报价不能越过执行校验 |
| 午休与最终清仓 | 10:15、11:30 盘段结束不等于统一强平终点 | 按日历中的盘段、禁开和清仓用途分别检查 |
| 普通报价年龄 | 0 关闭该策略年龄过滤；正数按源规则及数值精度判断 | 保留原字符串和兼容截秒行为，不能变成通用固定 5 秒门槛 |
| 重复报价 | 源用时间字符串变化刷新静默计时；重复读取不能刷新 | 原始字符串/精度保留；传输消息另记，不冒充报价变化 |
| 撤单和重报 | 源目标变化不等于已撤销；已有工程身份与终态屏障继续有效 | 超时不释放 UNKNOWN，不重复发送已准备效果 |
| 同分钟消费与统计 | 原信号、拒绝消费及统计口径不变 | 定时事件不生成虚假分钟、不增加信号次数、不捏造参考成交价 |

差异登记：E01 单调间隔抵抗跳钟；E02 无报价登记退出责任但不伪造执行价格；E03 迟到检查合并；E04 重启不以本地超时推定终态；E05 已确认时钟异常的重核对流程。它们须通过独立工程验收，不写成“原 DSV 已具备”。

## 5. 时间与行情数据合同

### 5.1 时间样本

以下为 V2.0 的抽象接口示意；V2.1 实施时须适配主线已有 `ClockSample` / `CalibratedClock`，不可重复创建冲突的类型：

```python
@dataclass(frozen=True)
class ClockSample:
    utc_now: datetime          # 必须 aware UTC
    mono_ns: int               # 非负；仅用于明确基准内的差值
    clock_epoch: str           # 每次 owner 启动生成；不得与 feed epoch 混用
    sample_span_ns: int        # 成对采样的最大夹持跨度

class Clock(Protocol):
    def sample(self) -> ClockSample: ...
    def utc_now(self) -> datetime: ...
    def monotonic_ns(self) -> int: ...
```

可采用 mono-before / 可信 UTC / mono-after 采样，保存跨度、参考源状态、估计误差和 clock epoch；不得假设两次调用原子发生。同一轮共享样本，发送前按既有责任边界重新读取，不能让 UI、CTP 回调或诊断线程修改样本。单调时钟不提供绝对日期，CTP 行情时间也不能自行续算“现在”。

`clock_epoch` 是本方案选择的进程内可比较域。Python 同机进程可能共享单调基准，但第一版不依赖跨重启复用；系统重启、换机、休眠另按第 10 节处理。[R06][R10]

内部单位统一 ns；序列化到 JSON 的大整数沿用精确字符串约定。现有 `monotonic_ms` 不得直接填 ns；兼容字段需要明确换算和新 schema。纳秒报价原值不得经浮点 UTC 往返而损失身份。

### 5.2 帧语义版本

建议新帧使用 `time_semantics_version = "OT_TIME/2"`，增加 `time_sample` 和明确的 `decision_utc`。`authorization_time` 仍为当前授权检查时刻，不受行情外推影响。不得在旧帧上无版本地改变 `wall_time` 的含义。

旧版缺时间版本的帧由显式 legacy 解码器按原含义重放；未知未来版本拒绝为交易输入，仍允许只读诊断。旧 ROUND、checkpoint 和订单事件不原地改写。`market_clock=True` 只能出现在 legacy 解码路径，不能继续作为新在线链隐含的时间策略。

源报价时间、观察时间、日历事件时间、日志写入时间必须使用不同字段。排序和幂等以 owner 顺序号、事件身份为准，不靠不同机器的时间戳推定全局先后。

### 5.3 每合约行情观测

| 字段 | 定义 |
| --- | --- |
| `source_time_raw` / `source_time_utc` | SDK 原字符串与规范化时刻，均不修改 |
| `native_candle_id` | SDK 原生分钟身份，不由 timer 推造 |
| `observed_at_utc` / `observed_mono_ns` | owner 在 wait_update 后观察到该数据的时刻，不声称是网卡接收时刻 |
| `source_epoch` | 行情/连接会话身份；重连与 clock epoch 分离 |
| `quote_identity` | 与源精度一致的报价身份；基础兼容规则保持原 datetime 字符串变化语义 |
| `last_distinct_quote_mono_ns` | 最近一次符合身份规则的更新观察点 |
| `source_age_raw_ns` | 当前 UTC 减源时间，可为负；不以零覆盖异常值 |
| `silence_ns` | 当前单调时间减最近有效更新观察点 |
| `age_quality` | VALID / CLOCK_UNVERIFIED / SOURCE_ANOMALY / UNKNOWN 等解释状态 |
| `quote_session` | 报价归属盘段，与当前活动盘段分离 |

连接活动、任意 SDK 数据更新、该合约报价更新分别计数。账户回报不能刷新全部合约新鲜度；轮询缓存不能刷新静默锚点；重连首份历史快照不能只凭“刚观察到”证明新鲜。

### 5.4 源年龄与小幅负年龄

保留 signed raw age，不再使用 `moment > local_now -> None`。数据先进入有界观测/拒绝解释路径，再按具体用途评估。相同源分钟、时段、价量与身份检查继续保留。

第一版不得把“所有负年龄”或固定 ±1 秒直接变成新的交易门禁。诊断偏差阈值和交易报价年龄阈值是不同参数。普通策略正数年龄限制仍按已核实 DSV 规则；0 只关闭该项年龄过滤，不取消价量、盘段和身份检查。

明显跨日、跨盘、时间倒退或与已确认参考冲突的报价只影响本合约，输出具体证据；不得用其时间最大值推动整个账户。无法确认源年龄时输出 UNKNOWN，不填写 0，不伪造参考时钟。

偏移计算器可继续有 15 秒的用途专属上界，但复用 signed age/quality 解释；轻微领先不再静默变成泛化超时，异常时显示原始差值和错误类别。跨用途不强行共用同一年龄上限。

## 6. 计时器、顺序与迟到策略

### 6.1 计时器是到期检查，不是交易指令

计时记录建议包含：`timer_id`、`scope_id`、`position_or_target_id`、`generation`、`kind`、`start_event_id`、`start_mono_ns`、`duration_ns`、`comparison`、`clock_epoch`、`phase`。起算事件由第 4 节映射表指定。

目标/阶段变化使旧 generation 失效；重连不自动使同进程未完成业务计时归零。相同 timer 到达多次只重检同一责任，不能生成重复目标或重复 effects。超过截止点不代表柜台状态变化。

开始阶段用 owner 内的小型映射和扫描即可，不引入专门调度线程或堆框架。只有测量证明扫描成为瓶颈后再优化。

### 6.2 每轮处理顺序

```text
1. 有界驱动 SDK，收集本轮已取得的 native 事实。
2. 采样时钟，标记连接/休眠/时间异常，验证输入完整性。
3. 在原有事实应用阶段接收订单、成交与账户变化。
4. 对仍然存在的业务阶段检查 timer；校验身份、版本和比较符。
5. 按既有 DSV 控制流维护持仓、时间窗口和信号优先级。
6. 先规划无需价格的维护，再规划需要价格的效果。
7. 事务提交状态、时间事件及已准备效果；失败则不得派发。
8. 同 owner 走原效果派发边界，随后继续驱动 SDK。
```

同轮部分成交优先进入已确认持仓逻辑，不再对旧“零成交 pending”执行超时撤回。这里只处理本轮已取得事实，不等待未来未知回报。不同计时器的业务优先级由源控制流和测试固定，不能简单按哈希或时间戳排序后改变策略。

主循环 sleep、数据库锁等待、诊断 I/O 都应单独测量。不得增加另一个线程直接操作 SDK 或 Journal。

### 6.3 deadline 与 SDK 等待

业务剩余时间来自单调截止点；等待切片建议不大于基线的 0.5 秒，并在有更近有效 deadline 时缩短。TqSdk 接收 Unix 时间戳，不接收单调绝对值。[R04]

```text
remaining = next_business_deadline_mono - current_mono
wait_s = min(max_poll_s, max(0, remaining))
api.wait_update(deadline=system_time_time() + wait_s)
# 返回 False 仍继续事实核对和计时维护。
```

这是伪代码。严格 `>` 到期点恰好相等时不能形成零等待忙循环：本轮判断未到期后，按最小可调度步长重查，但不得把业务比较符改为 `>=`。

SDK 内部在墙钟突变下的唤醒行为必须用锁定版本验证；每次传短 deadline 本身不构成硬实时保证。不得用另一个线程并发调用同一 API 来强行打断。发现 owner 卡死则先保留原进程/账户责任，不能启动第二发送者。

### 6.4 迟到处理表

| 类型 | 到期后的处理 | 禁止 |
| --- | --- | --- |
| 一次性确认超时 | 核对最新状态后处理一次 | 对已经成交/换阶段目标补做旧动作 |
| 周期诊断与状态刷新 | 合并为一次最新快照 | 补刷全部历史周期造成 I/O 风暴 |
| 周期改价检查 | 用最新状态、当前价重评一次 | 追赶每个旧周期连续撤改单 |
| 收盘时间事件 | 锁存退出责任，重新检查当前会话和报价 | 休市仍按过去盘段发新委托 |
| 失效 generation | 忽略并可聚合诊断 | 旧 timer 操作新目标 |
| 已提交未派发效果 | 按原恢复协议核对 | 把它当普通迟到 timer 删除或盲重发 |

记录 `scheduled_mono_ns`、`handled_mono_ns`、`lateness_ns` 和发生时业务版本。周期补发策略只影响检查机会，不删除既有订单责任。[R08]

## 7. 无报价订单维护与执行边界

将 `ExplicitOrderController.plan()` 的工作拆成逻辑上的 `plan_maintenance()` 与 `plan_priced_effects()`；可先保持同文件和同对象，不要求大型重构。[C03]

### 7.1 维护阶段

在 tick 缺失时也要：接收 native 事实；结算可证明完成的责任；处理已确认的目标缩减/超时意图；对身份明确、属于本账户、尚未终态且可取消的自有单规划撤单。

撤单仍需有效发送通道、原身份、去重及既有柜台限制。无连接时保存取消意图等待恢复，不声称已发出；没有可取消身份时进入核对，不猜订单号。价格缺失不成为必要取消的前置条件，但其他证明不能省略。

### 7.2 价格阶段

新开、新平和重报必须验证当前会话、账户/授权、归属、未决数量、合法价量及该用途允许的行情。沿用发送前校验，保证决策使用的报价身份与发送依据可追踪。

价格缺失时保留已形成的退出目标，显示 `EXIT_WAITING_FOR_PRICE`；休市时显示 `EXIT_WAITING_FOR_SESSION`。不得凭定时到期合成价格，不无条件用旧缓存绕过执行校验，不把 MARKET 模式名称当成已经发出市价单。

同目标内：取消请求返回后仍保留未决占用；只有满足既有原生终态/成交一致屏障后才允许规划剩余量。未知发送不能降格为“肯定未发送”。超时、查询暂未找到和诊断失败均不是终态证明。

### 7.3 目标登记与统计分离

无报价时登记的收盘/保护退出责任必须可持久恢复；但没有名义参考价时，不得捏造 DSV 点数或成交损益。退出请求、可执行价格、真实成交、名义统计各自留证，缺项显示未确定。正常有价路径的统计时点和公式保持不变；异常无价的补记规则作为 E02 单独验证。

## 8. 当前时段、收盘与柜台交易日

### 8.1 两种日历查询

分别查询 `calendar.query(contract, decision_utc)` 与报价所属盘段。当前是否交易由前者决定；源报价归属由后者决定。`market_session_active` 不能由最后报价永久维持。

收盘业务保留已确认的 DSV 双时间触发，但源时间辅助触发需要明确匹配的报价盘段及质量。当前已休市时不得因为旧报价仍在收盘窗口就产生新的可发送权限。

### 8.2 一次性日历责任

以账户作用域、合约、柜台日、盘段、退出类型、策略版本构造稳定身份。墙钟后退不能重复登记同一收盘责任；向前跳过窗口应记录 missed calendar event 并核对已有责任，不补开过时入场信号。

收盘窗口开始、收盘刷新经过时间、实际休市是三种条件。首次退出后用单调经过时间保持 DSV 阶梯公式，不擅自切换成另一种“距会话终点剩余时间”算法。

### 8.3 柜台日不是机器日期

取消跨合约最大报价驱动所有日切的隐式做法。日切要求现有明确授权、完整日历和 native 账户/订单事实能够相互核对；行情日历只是其中一份证据，不能凭本机午夜或单个异常合约自行推进。

当前 SDK/连接快照有些 trading_day 字段来自 grant，并不天然是独立柜台证明。[C01][C11] 第一批保持已授权的有限日范围；若拿不到真实独立日切证据，应明确限制自动日切覆盖范围，不声称已支持全天跨日无人值守。不得在无依据日切时清空待处理订单、冷却、分钟去重或保护状态。

## 9. 时钟健康、异常与维护

### 9.1 健康信息不直接等于交易权限

健康状态按当前 `CalibratedClock` 真实状态映射为：已校准、可信守时、参考冲突/重新收敛、绝对时间不可证明及待恢复；可兼容旧诊断状态名。记录参考来源、样本年龄、offset、往返时延、误差上界、守时预算、clock epoch，以及独立的 Windows 墙钟偏差（仅诊断）。CTP 连接健康、行情新鲜度与 UTC 健康必须分开显示。

NTP 单次请求失败或诊断文件写失败不等于已证明时间不可信，不能单凭一次故障改变交易授权；但若可信 UTC 的误差预算耗尽或明确冲突，则暂停需要可靠绝对时间的新开仓并登记恢复条件。已有退出责任与原生事实核对继续进行，不因无时钟证明虚称已经平仓。健康检查不可记录凭据、完整账号或未经脱敏的异常文本。[C10]

### 9.2 跳钟与不确定性

以可信 UTC 锚点、单调推进、参考一致性和误差上界判断时间是否可用于交易；`Windows wall_delta - mono_delta` 只能作为辅助诊断，不能反向校正决策时间。阈值显式配置并用实测确认，不共用为报价年龄阈值。小幅连续调速与已确认跳变分别记录，不宣称单一差值能诊断全部问题。

已确认的大幅跳变或唤醒进入恢复核对：停止基于未确认绝对时间增加新风险，继续接收 native 事实、保留保护/退出责任，并在证明充分时处理自有取消。它是 E05 的明确异常策略，不是“只要 NTP 暂时不可达就停开”；正常报价仅略领先本机不能触发此路径。

恢复要求：时间观测连续、柜台事实同步、当前日历/授权匹配、未完成责任核对完成；复用现有显式恢复/授权机制。不得仅靠经过一个 TTL 自动恢复，亦不得让全局时钟异常吞掉已有持仓保护。若某执行入口尚无这种恢复机制，实施 PR 必须单列状态转换及验收，不以诊断布尔值暗改授权。

绝对时间不可信时，不能同时保证准确收盘时点和任意正常交易。必须显示边界；不得悄悄把墙钟钳制成只增不减的“假 UTC”，也不得以行情最大值伪造恢复。

### 9.3 系统维护

Windows W32Time 仍可负责操作系统自身授时与运维；**交易决策不依赖 W32Time 是否调准系统墙钟**。应用以自身可信 UTC 为基准，不主动拨系统时间。维护脚本只能做独立诊断和已授权的系统管理，不能作为 CTP 上线或策略继续运行的必要条件。[R09]

补齐 `simnow replacement-run`、`desktop-start`、源码 Python、冻结程序等入口识别。进一步建立全产品启动/维护互斥协议：维护取得排他；owner 在有效生命周期持有互斥参与凭据。所有入口覆盖之前，进程正则只能辅助，不能宣称消除了检查后又启动的竞态。

主动大幅拨钟需要明确维护条件及管理员权限。停止新开仓不等于无持仓、无在途、无 UNKNOWN；不能强杀有责任进程后直接校时。原版 DSV 等未接入互斥的外部程序必须另外检查，信息不可读时不得声称安全空闲。

## 10. 重启、休眠、恢复与存储版本

### 10.1 持久化最小数据

保存时间语义版本、责任身份、generation、阶段、起算事件 ID、起算 UTC/质量、duration/比较符、进程 clock epoch 和已完成阶段。进程内单调起点可保存用于审计，但默认不跨重启直接使用。

计时改变了业务责任时，记录与状态更新同事务提交。每次扫描不必写一条巨大时间事件；保持有界诊断。增加字段或表前必须设计 schema 最低读取版本，保证旧程序不能静默忽略新语义并继续写入。

### 10.2 新版记录恢复

恢复先同步 native 事实，再恢复目标和计时。若起算/当前 UTC 可核对且没有未解释跳变，计算剩余时长并绑定新 clock epoch；已到期则核对后处理一次。已经成交的目标不执行零成交超时分支。

无法证明经过时间时，标记 `TIMER_RECOVERY_UNCERTAIN`：不重置为完整新周期，不伪造到期，不盲重报；保留核对和原明确退出意图。对未成交开仓残单可在已有停止/撤回责任与身份充分条件下继续取消，但不能把不确定计时单独解释为柜台终态。

### 10.3 旧版记录恢复

`01ecddaa` 的部分 `submitted_at/context_at/close_request_at` 可能是报价时间，不能直接重解释为实际创建 UTC。读取旧 schema 后尝试从可验证原事件恢复；证据不足就进入上述不确定路径，不回写伪造单调起点。

迁移先对冷备份和合成文件执行，只新增时间语义及迁移回执，不改订单 ID、成交价格/数量、归属和历史 ROUND。迁移失败不发布半成品；旧库可只读，新库兼容性需明确版本门槛。

### 10.4 休眠与长调度间隔

记录系统、Python 时钟实现和暂停行为。Windows QPC 与 Linux 常见 monotonic 的挂起语义不同，不能用 Linux 测试推定 Windows 已验证。[R06][R10]

唤醒/长间隔先重新核对连接、快照、当前会话和计时责任，再处理到期检查。诊断/改价检查合并；不以补执行的方式突发历史交易。生产长期运行建议禁止自动休眠，但禁止休眠不能替代恢复测试。

## 11. 入口、部署身份和配置

### 11.1 必须分别覆盖的入口

| 入口 | 当前链路 | 任务 |
| --- | --- | --- |
| `simnow replacement-run` | `dsv_runtime.sdk_startup` / DsvSession / DsvSdkAdapter | 保留替代链的独立时钟、原生事实和发送校验，单独验收 |
| 桌面 F5 / `desktop-start` | `runtime.desktop_bootstrap` → `runtime.dsv_simulation` → 旧 Kernel 链 | 第二批接入共用时间接口，独立回归 |
| 实时偏移取数 | `startup_metadata.read_offset_inputs`，只读行情 | 共享时间解释，保留用途上界与不下单边界 |
| 历史/冻结 QA | 已整理 frame、原循环和 SDK task 回放 | 保留既有测试，新增未整理输入链 |
| 维护校时 | PowerShell 脚本 | 入口识别、互斥、前后证据；绝不作为交易可信 UTC 来源 |
| CTP 行情只读探针 | 拟新增 `gateway/ctp_md.py`、`qa/ctp_md_probe.py` | 注入相同 `CalibratedClock`，不连接 TraderApi、不导出交易命令 |
| CTP 行情候选主源 | 拟新增 `market_feed` 适配层及现有桌面/替代链分别接入 | 必须独立验收并显式批准切换，不在只读 PR 隐式启用 |

不同入口不得因相同界面版本被当成相同执行引擎；一个入口通过不覆盖另一个。除独立策略服务迁移任务外，不改变桌面默认账本或 owner。

### 11.2 每次运行身份

至少保存：`commit_sha`、dirty/worktree 清单或 artifact 哈希、`entry_point`、`engine_kind`、Python/SQLite/TqSdk/CTP API及绑定版本、`time_semantics_version`、`market_provider`/`market_mode`、日历/配置/授权哈希、clock epoch、provider epoch、运行实例 ID。CTP 行情前置配置只记录脱敏身份或哈希，不写明文密码、账户或私有地址。

无法证明构建 SHA 时写 unknown 和制品哈希，不能读取仓库当前 HEAD 冒充运行进程版本。身份记录不包含凭据、真实账号或私有绝对路径。

### 11.3 配置原则

沿用既有策略参数。新增工程参数限于最大轮询切片、诊断采样频率、诊断阈值、恢复检测、日志预算、行情提供者只读模式与数据 SLA；不得混入策略偏移、下单手数或风控倍率。默认仍使用原 TqSdk 权威行情，CTP 仅 `SHADOW`；切换 `PRIMARY` 需要新的审批与实测凭据。

轮询建议以现有 0.5 秒上界起步并靠近 deadline 缩短；诊断阈值、CPU/存储预算和部署时钟容差作为发布参数清单，不在本文假定通用最优值。涉及授权或新增风险限制的变化必须单独审查。

## 12. 文件级任务与交付物

新增路径为建议名称，可在实施 PR 内统一命名，但必须维持职责边界。

| 任务 | 文件/模块 | 交付要求 |
| --- | --- | --- |
| T00 | 本文及验收夹具清单 | 固定基线、规则优先级、业务起算点、参数/预算和差异登记 |
| T01 | `scheduling/clock.py`、`scheduling/reference.py`（主线已存在） | 完善现有 `ClockSample`、`CalibratedClock`、可注入 FakeClock、误差预算及对 Windows 墙钟的禁用门禁；不重复造轮子 |
| T02 | `scheduling/timers.py`（新增或内聚于 owner） | generation、比较符、到期检查、失效/合并；不直接派发 |
| T03 | `dsv_runtime/sdk_adapter.py` | 当前时间与 source 时间拆分；原始观测、年龄解释；限制单合约异常传播 |
| T04 | `dsv_runtime/runner.py`、`dsv_core/transition.py` | 全部时间读取清单；间隔迁移；无报价维护；收盘退出锁存；源规则比较符不变 |
| T05 | `dsv_runtime/controller.py` | 维护与价格规划分离；原提交与 UNKNOWN 责任不变 |
| T06 | `dsv_runtime/sdk_startup.py`、`scheduling/reference.py` | 注入时钟、驱动切片、只读诊断、明确恢复流程 |
| T07 | `dsv_storage/journal.py`、迁移模块 | 时间 schema、最低读取版本、冷迁移与恢复回执、拒绝旧 writer |
| T08 | `runtime/desktop_bootstrap.py`、`runtime/dsv_simulation.py`、旧 scheduler/gateway | 旧入口共用基础设施与独立覆盖；不自动迁移引擎 |
| T09 | `runtime/startup_metadata.py`、工作台状态 | 修复偏移输入的零容差；显示每合约原因与实际引擎身份 |
| T10 | `scripts/sync-clock-once.ps1`、`scripts/clock.ps1`、实例锁模块 | 维护互斥和安全预检；只读状态与主动校时分开 |
| T11 | `qa/dsv_parity`、新增测试 | 全输入链、冻结 DSV 正常行为、异常工程合同、报告分项 |
| T12 | 拟新增 `gateway/ctp_md.py`（或独立 `market_feed/ctp.py`） | 只读 CTP MdApi 连接/登录/订阅/重连/释放；回调仅复制原始字段并入有界队列；无 TraderApi |
| T13 | 拟新增 `market_feed/events.py`、`market_feed/identity.py` | 统一 TqSdk/CTP 行情事件、原始来源身份、盘段/交易日解释、精度及质量标记，不能伪造 CTP 原生递增 ID |
| T14 | 拟新增 `market_feed/router.py`、`runtime/ctp_md_monitor.py` | `SHADOW` 与 `PRIMARY` 显式状态、source_epoch、积压/溢出/重连门禁、同源最新报价校验；不新增订单 owner |
| T15 | `runtime/market_monitor.py`、`kernel/workbench.py`、`transport/workbench_snapshot.py` | 可信 UTC 源年龄、接收→owner 延迟、每源状态分开，新增 1 秒轻量行情视图；完整业务快照仍有界 |
| T16 | `gateway/tqkq_dsv.py`、`gateway/dsv_facts.py`、`runtime/dsv_simulation.py` 及替代链接入点 | 先只读影子验证；后续切换需替代 TqSdk 特有原生 Tick 连续性与发单前报价一致性证明，逐入口复测 |
| T17 | `qa/ctp_md_probe.py`、`tests/test_ctp_market_feed*.py`、Windows 制品 | 离线回调重放、双源只读实测、打包兼容、秒级 P99、DSV 全输入链和收盘持仓闭环证据 |

源路径属于固定基线，但不是每个列出模块都必须大改。先查找所有 `time.time/datetime.now/monotonic/wall_time/context_at/submitted_at/last_stop/close_refresh` 使用，形成调用点清单；未检查到的时间路径不能宣告迁移完成。

## 13. 分批 PR 计划

### PR-A：反例、契约与可测试时钟

依赖 T00。新增三种时间独立推进的 FakeClock、SDK 形状替身和实际适配器集成夹具。基线问题测试应以明确标识的缺陷测试运行，保存 expected/observed 和退出码；不得删除断言、永久 expectedFailure 或把失败当通过。阶段性红测试须与常规套件分开说明，修复后移入常规通过套件。

同时固定完整场景中的规则优先级和起算点。无真实凭据、网络、交易账户或系统拨钟。

### PR-B：替代运行链最小修复

依赖 PR-A。完成 T01–T06 中主路径，以及必要的 T07 时间版本门槛。保留原 owner、提交、派发和恢复身份；修复 F01–F05；验证同轮成交/超时、失效 timer、静默期间取消和休市边界。

不混入 UI 改版、SDK 升级、CI/CD、策略公式或数据库性能重构。验收：所有对应红测试转绿，原相关回归不退化，正常 DSV 差异为零或有逐项明确批准。

### PR-C：恢复、共用入口与外围闭环

依赖 PR-B。完成 T07–T10，覆盖旧时间语义恢复、重连/休眠、桌面入口、实时偏移读取、维护互斥与运行身份。两个执行链分别标注测试范围。冷迁移前不允许旧 writer 继续交易；无迁移依据时拒绝自动解释旧 timer。

### PR-D：Windows 与冻结 DSV 验收证据

依赖 PR-C。运行第 14–16 节；输出固定 SHA、实际命令、各入口结果、完整环境、未执行项和原件哈希。只提交脱敏必要材料，不能把本机 output 路径存在当作远端已取得原件。

### PR-E（V2.1 新增）：CTP 行情只读接入与统一时钟观测

在 PR-A/B/C 的共享时间合同可用后实施 T12–T15；适配 SDK 版本、Windows/Python 3.13 制品和期货公司行情前置权限。`CTP_MD_SHADOW` 默认只采样不参与交易，原交易 owner、账本和授权原封不动；输出 TqSdk/CTP 同机对照报告、时间可信状态及 1 秒性能指标。不得将 CTP 回调线程用于直接策略报单或 SQLite 写入。

### PR-F（须另行审批）：CTP 主行情切换与交易闭环

依赖 PR-E 的 Windows 真实行情对照、策略同输入回放、缺号/断线/日切和报单前行情一致性验收；再实现 T16/T17 的 `CTP_MD_PRIMARY`。未获确认前不得更改桌面 F5、`simnow run` 或 replacement-run 的权威行情；更不得自动在 TqSdk 与 CTP 之间热切换。合并前做一次全账户单 owner、持仓、活动单、UNKNOWN 核对。

PR 完成不自动合并、不自动发布、不连接真实账户；合并与部署按现有确认流程执行。

## 14. 测试设计与验收矩阵

### 14.1 测试层次

L1：纯时钟/计时/年龄/日历单位测试。L2：实际 DsvSdkAdapter → DsvSession → ExplicitOrderController → 临时 SQLite → 无网络记录端，SDK 传输用替身，禁止替换关键业务函数。L3：冻结 DSV 及其实际 SDK 版本的受控对照。L4：Windows 源码/冻结制品、真实 IPC 和可控系统环境。L5：经单独授权的模拟柜台与长期运行。

L2 必须在适配器之前输入原始形状数据，不能只调用 `apply_round()`。Linux 对 Windows 锁/IPC 的替身单独标注；不能把它计为 Windows 原生通过。原 DSV 取证解释器与产品 Python 版本分开，所有离线数据均为测试，不增加产品离线业务。

### 14.2 必测用例

| 编号 | 层级 | 输入/故障 | 验收断言 |
| --- | --- | --- | --- |
| A01 | L1/L2 | 本机慢 1/30/970 毫秒，报价持续刷新 | 原始报价不被零容差静默吞掉，策略/发送依据可追踪 |
| A02 | L1/L2 | UTC 前跳/后跳 30 秒，单调时间正常 | 相对 timer 不提前/冻结；时间异常明确，旧日历动作不重复 |
| A03 | L2 | 报价固定，单调经过 2.999/3/3.001 秒 | 被动等待边界保持源 `>=` |
| A04 | L2 | 报价固定，经过 59.999/60/60.001 秒 | 未成交确认超时严格 `>`，不因报价静默冻结 |
| A05 | L2 | 无 tick，已有可取消自有开仓单 | 形成允许的取消请求并保持原订单责任；不得发新价单 |
| A06 | L2 | 无 tick 且发送状态 UNKNOWN/无可取消身份 | 只核对/保留意图，不猜身份，不释放占用 |
| A07 | L2 | 成交/部分成交与 timeout 同轮 | 先应用已得事实，不误走旧零成交超时分支 |
| A08 | L2 | 取消请求后迟到成交、成交先于终态 | 归属数量不丢，不提前重报，不出现负剩余量 |
| A09 | L1/L2 | 目标换 generation，旧 timer 触发 | 不操作新目标；旧 timer 可诊断但无效果 |
| A10 | L2 | 相同 ROUND/timer 重复 | 状态和收据幂等，无第二次报撤单 |
| A11 | L2 | owner 暂停 10 秒后恢复 | 周期检查合并；不突发补做历史改价动作 |
| A12 | L1/L2 | 提前唤醒或严格边界恰相等 | 重新检查比较符；无零等待忙循环 |
| A13 | L2 | 首次得到两分钟前快照，年龄阈值 2 秒 | 源年龄不为零，不通过该年龄检查 |
| A14 | L2/L3 | 相同快照、年龄阈值 0 | 不额外恢复已关闭的策略年龄门禁；其他执行约束仍在 |
| A15 | L2 | 账户回报不断、报价不变 | 连接活动增加，合约静默锚点不刷新 |
| A16 | L2 | SDK 同 datetime 原样重读 | 不算新报价；分钟消费不重复 |
| A17 | L2/L3 | 同时间字符串但价格字段变化 | 按冻结兼容规则处理，保留原值，不擅自创造报价时间 |
| A18 | L2 | 单合约异常未来/倒退时间 | 有具体原因；不推动其他合约或全账户交易日 |
| A19 | L2 | 最后报价 14:59，当前 14:59:40，提前 30 秒退出 | 登记收盘责任；是否报单取决于独立执行条件 |
| A20 | L2 | 同输入推进到 15:01 | 当前休市，不继续宣称盘中；退出责任不丢 |
| A21 | L2/L3 | 10:15/11:30 午休与最终收盘 | 不把所有盘段结束套为统一强平 |
| A22 | L2 | 日历事件后 UTC 倒退再跨过 | 相同责任不重复，授权不因报价或倒退被自动延长 |
| A23 | L2 | 断线/重连 feed epoch 改变，进程未重启 | 未完成业务 timer 不随行情缓存清空而重新赠送周期 |
| A24 | L2/L4 | 进程重启、系统重启、换机副本 | 不无证据比较单调值；先恢复订单事实 |
| A25 | L2 | 旧 submitted_at 实为报价时间 | 不冒充真实提交 UTC；按不确定恢复规则处理 |
| A26 | L2/L4 | 迁移失败、旧程序打开新库 | 原库不改坏；旧 writer 被明确阻止 |
| A27 | L2 | 提交失败/磁盘不可写 | 时间责任和效果事务不伪成功，不派发未提交效果 |
| A28 | L2 | timer 已提交后、派发前崩溃 | 按现有效果恢复核对，不重复原生发送 |
| A29 | L1/L2 | 实时偏移报价持续领先 30/970 毫秒 | 不仅因负年龄等到泛化超时；输入质量原因可见 |
| A30 | L2 | NTP 不可达/诊断写失败 | 不单独改变交易授权；健康诊断降级可见 |
| A31 | L4 | Windows 休眠跨收盘、Linux suspend 对照 | 唤醒先核对；平台语义差异被记录 |
| A32 | L2/L4 | F5、replacement-run、冻结入口 | 实际执行器身份正确，各自走到修改路径 |
| A33 | L4 | 执行器启动与维护校时同时申请 | 互斥有效；不出现双方都认为可执行 |
| A34 | L2/L3 | 普通/延续等待、冷却、价格上下文与收盘阶梯 | 源比较符、起点、优先级、正常输出保持 |
| A35 | L2/L5 | 授权日与日历/账户证据不一致 | 不凭本机日期/最大报价擅自日切；责任保留 |
| A36 | L2 | 全部无报价启动 | 能在证据完整时核对账户；不伪造首个行情或假盘段当前时间 |
| MD01 | L1/L2 | CTP 首次订阅仅收到缓存行情 | 不当新 Tick；以可信 UTC 计算源年龄，来源/盘段未知则不交易 |
| MD02 | L1/L2 | CTP `ActionDay` / `TradingDay` 夜盘跨日及 `UpdateMillisec` 边界 | 不用 Windows 日期拼接；无法消歧则标 `TIME_UNPROVED` |
| MD03 | L1/L2 | 同一报价时间多次回调、价格变化、字段不完整 | 按明确源身份去重；不臆造原生 Tick 递增 ID/连续性 |
| MD04 | L2 | CTP 回调高峰、owner 处理慢、队列满 | 记录背压/覆盖缺失，关闭新开仓，不静默丢掉策略需要的路径事件 |
| MD05 | L1/L2 | Windows 墙钟前后跳 60 秒且 CTP 流正常 | trusted UTC 及决策截止点不跟跳；SDK 兼容 deadline 不成为业务计时 |
| MD06 | L2/L4 | CTP 断线/重连，源 epoch 更新并带旧快照 | 先核对原生仓位/订单/成交；源年龄、路径恢复均需证明；绝不重放旧开仓 |
| MD07 | L2 | CTP 中断且 14:59:40 到收盘退出点 | 可信 UTC 登记退出责任；没有可用同源原生报价时不伪造发送 |
| MD08 | L2 | CTP 推出新行情、TqSdk 最后报价不同 | SHADOW 不改变交易；PRIMARY 不跨源绕过 `E_NATIVE_QUOTE_NOT_CURRENT` |
| MD09 | L4/L5 | 同机双源 4 合约活跃盘段对照 | 报告源→owner P50/P95/P99/max、覆盖率、迟到、断流与样本数量，不以最快样本代替通过 |
| MD10 | L4 | GUI 全量发布 2.5 秒，轻量行情流 1 秒 | 策略不等待 GUI；轻量视图不拖慢 owner，不伪称每秒有新 Tick |
| MD11 | L2/L4 | NTP 双源冲突、恢复、守时到期 | CTP 不能反向校时；降级按可信时间预算与既有持仓责任执行 |
| MD12 | L2/L5 | 切主行情前 DSV 固定 Tick/原生订单比较 | 默认未切换；只有签核并验证时间、信号、下单及退出闭环才能 PRIMARY |

测试出现失败时逐用例保存输入、首次差异、决策/订单变化和预期来源；不能仅给通过总数。合成反例的参数与现场样本严格区分。

### 14.3 拟新增测试模块与执行命令

以下模块尚待实施，命令是目标执行方式，不是本次已经执行的报告：

```sh
# Linux：仅合成输入；Windows 专属项要明确 SKIP，不能伪报 PASS。
PYTHONPATH=src:. python -m unittest \
  tests.test_clock_contract \
  tests.test_timer_lifecycle \
  tests.test_dsv_clock_input_chain \
  tests.test_dsv_no_quote_maintenance \
  tests.test_clock_recovery_contract \
  tests.test_offset_clock_quality -v
```

Windows 在实际产品 Python 环境设置 `PYTHONPATH=src;.` 后运行相同模块，再运行已有 Windows/Qt/IPC 回归。不为了测试便利把生产 SDK、SQLite 或账户版本策略伪装成已满足。

DSV 对照应分开控制 quote time、datetime.now/time.time 和 monotonic 输入。先固定源方法、代码对象哈希及规则预期，不能以候选程序输出生成黄金答案。

## 15. 性能、观测与日志合同

### 15.1 性能约束

记录 owner loop gap、SDK pump gap、native facts 处理耗时、timer lateness、事务提交耗时及派发前后耗时。分别给出 P50/P95/P99/max 和样本数，不把一次最快值当性能保证。

确定性测试要求在满足比较符后的首个可处理 owner 轮次完成判断。实机迟到预算由 T00 根据部署硬件和既有基线确认；必须分开调度迟到、存储耗时、网络往返和柜台确认。没有网络响应保证时，不设置“超时后推定成交/撤销”的捷径。

避免每轮全库历史扫描、每 Tick NTP、CTP 回调内做重计算、诊断同步阻塞与无限 timer 堆积。真实持久化错误仍必须阻止未提交效果，不能为性能跳过原提交屏障。行情采集线程与 owner 解耦只为避免阻塞，所有交易动作仍唯一 owner 发起；无界队列和静默覆盖事件不可接受。

### 15.2 必要诊断

| 事件/状态 | 最少证据 |
| --- | --- |
| 时间异常 | 前后 UTC/mono、采样跨度、差值、epoch、检测类别 |
| 报价解释或拒绝 | 合约别名、源时间、观察时间、signed age、silence、盘段、具体 reason |
| timer 到期/忽略 | timer/目标身份、generation、业务阶段、scheduled/handled、lateness、结果 |
| 无行情维护 | 退出/取消意图、缺少哪类证明、是否已有准备效果 |
| 恢复 | 新旧 schema、起算证据、计算方式或 UNKNOWN 原因、native 核对结果 |
| 运行身份 | 构建、入口、引擎、时间策略、行情来源/模式、依赖及配置/日历哈希 |
| CTP 时延与背压 | provider/epoch、原始事件时间、可信接收 UTC、回调/owner 单调时刻、队列深度、P50/P95/P99/max、覆盖与损失标记 |

诊断流有界轮转、重复聚合，首次发生、原因变化、恢复必须可见；聚合丢失范围如实说明。关键业务责任在原事实库，诊断文件不是授权或派发依据。不得记录凭据、完整账号、未经脱敏的原生报错或私有路径。

工作台分别显示时钟健康、连接活动、每合约报价新鲜度和退出等待原因。不能再由“READY/账户已同步”推断所有合约都有可执行报价。

### 15.3 报告结论分项

`clock_contract`、`input_quality`、`ctp_md_shadow`、`market_latency_1s`、`dsv_business_parity`、`order_lifecycle`、`recovery`、`windows_integration` 分别给 PASS/FAIL/ERROR/SKIP/NOT_RUN。日志精度匹配与 `--report-only` 成功退出不决定整机验收；现有比较器的有限范围仍须保留。[C09]

## 16. 部署与长期验收

执行次序：固定源码回归 → 实际 Windows 无网络测试 → **CTP + TqSdk 双源只读行情观测** → 经授权的模拟账户验证 → 长时间运行。影子行情接入不需要交易授权，但连接行情前置仍须具备合规的行情账号/接口权限；不得从已有 SimNow/TqSdk 账户推断 CTP 行情权限已具备。每阶段单独登记版本，不累加重复方法数制造覆盖。

建议先做一个完整日夜交易周期，稳定后再做 72 小时观测；这是测试计划，不是交付时间承诺。必须包含至少一次实际授权的开仓、成交后保护及退出证据。没有自然信号时填写 NOT_OBSERVED，不为得到通过结果擅自改生产策略或真实下单；缺失分支可用受控测试继续覆盖。

每阶段最少材料：候选 SHA/工作区状态、入口命令的脱敏形式、配置/日历身份、依赖、ClockSample 样本、ROUND 与 native 责任链、timer 统计、诊断轮转范围、人工操作记录、最终未完成责任清单。

现场账本通过受控只读副本或一致性备份审查。WAL 运行时不能仅复制主 db 或手删 WAL；遵循已有存储方案，不在本次时间修复中更换日志模式或在线清库。

## 17. 发布门槛与回滚

### 17.1 合并与发布分开

合并门槛：对应阶段测试具备实际输出，关键代码路径不被替身绕过，文档声明与执行结果一致，无未解释 P1。发布门槛还包括实际入口、恢复迁移、Windows 环境、部署时钟参数及原生交易责任闭环。

CI/CD 不启用。手工测试命令、结果及原件哈希必须保存；未执行就填 NOT_RUN，不以文档合并代替执行。

### 17.2 发布前

确认唯一 owner 与无并发旧 writer；取得一致性备份；核对 schema 读取版本和迁移预演；明确当前持仓、挂单、UNKNOWN 与退出责任。优先在 scope-flat 冷窗口部署，有未完成责任时必须使用已验收的恢复流程，不能靠重启清空。

新 owner 启动先核对，不自动增加开仓授权。功能开关不得在有责任时无审计地切换时间语义或主行情源。`CTP_MD_SHADOW` 可独立只读启动；`CTP_MD_PRIMARY` 必须经独立授权/灰度验收、所有未完成交易责任处理和受控冷切换。

### 17.3 回滚原则

回滚代码不等于把现场事实恢复到昨天。新版运行后若出现新订单/成交，禁止恢复旧备份并据此继续交易。保留新库和全部 native 事实，选择兼容读取的新修复版本或先完成受控核对与责任清理。

只有确认没有新增外部效果、无遗留责任，且旧二进制与数据格式兼容，才可进行冷回滚。旧二进制不支持新 schema 时拒绝启动，不移除版本标记强行降级。不能将已迁移新库交给会忽略时间字段的旧 writer。

## 18. 开发完成定义与未决事项

### 18.1 Definition of Done

时间用途无隐式混用且正式决策不依赖 Windows 墙钟；timer 具有明确起算事件、比较符、generation 和恢复语义；无报价维护独立；原始报价身份不改写；当前日历不由最后报价冻结；正常 DSV 对照及异常合同分开通过；两条入口和运行身份可核对；CTP SHADOW 的时延与质量独立达标；PRIMARY 另行获批并验收订单闭环；迁移/回滚有测试；手工验收证据完整；不修改 CI/CD。

### 18.2 实施前必须关闭的参数/证据项

| 项目 | 负责人角色 | 关闭方式 |
| --- | --- | --- |
| 各计时路径真实起算点/刷新条件 | 策略兼容开发者 | 与冻结 DSV 及现有调用链建立逐项映射 |
| 部署跳变阈值、诊断周期、迟到/磁盘预算 | 运行环境负责人 | 实测基线和版本化参数清单，不使用通用臆测值 |
| 产品锁定 TqSdk 的 deadline/跳钟行为 | 网关开发者 | 在实际 3.10.4 环境验证；公开 master/latest 只作参考 |
| 旧账本时间字段能否可靠恢复 | 存储负责人 | 冷副本验证；不能证明的明确 UNKNOWN |
| 桌面与替代入口发布范围 | 发布负责人 | 运行身份与各自测试结果，不默认两个都通过 |
| 独立柜台日来源与跨日覆盖 | 网关/策略负责人 | 真实字段及恢复对照；证据不足则明确限制日切范围 |
| CTP 行情前置/权限、绑定版本及 Windows Python 3.13 打包 | CTP 接入负责人 | 在授权行情环境做只读握手、订阅、重连与发布制品测试；不得虚报可用 |
| CTP `ActionDay`/`TradingDay` 与原生序列能力 | 行情/策略负责人 | 四交易所、夜盘跨日、重连、同时间多事件定向夹具；缺失序列只能标不完整 |
| ≤1 秒指标定义与环境基线 | 性能负责人 | 同机双源样本 P99/持续时长/时钟误差/失败样本数、GUI 独立周期和告警预算全部记录 |
| CTP 切换后的报单前报价来源一致性 | 交易执行负责人 | 从行情事件到发送校验全链相同来源身份；无法证明则拒发并保留退出责任 |

这些是具体开发任务和发布阻断项，不要求用户重新提供已知需求。本计划不能替代最终参数测量或现场交易证据。


## 19. V2.1 新增：CTP 秒级行情与可信时钟统一改造（2026-11-18）

> 用户需求：有新市场行情时，希望 OnlineTrader 从行情发生到策略可处理的延迟小于或等于 1 秒。CTP 作为新增候选行情数据源；这一条是**性能验收目标**，不是供应商或公网的延迟保证。CTP 不提供独立可信 UTC，不能用于校准交易时钟。原有策略逻辑、单交易 owner、账本和报撤单授权维持不变。

### 19.1 确定的时间来源与数据来源

| 来源 | 权限与用途 | 严禁行为 |
| --- | --- | --- |
| 应用 NTP 可信 UTC + 本机单调计时（现有 `CalibratedClock`） | 当前交易时刻、日历授权、源年龄、超时、心跳及守时状态 | 由 Windows 系统 UTC、TqSdk 最后行情或 CTP UpdateTime 回填当前时刻 |
| CTP MdApi 的 `OnRtnDepthMarketData` | 行情事件的原始时间、价量、来源和盘段证据 | 将 CTP 行情报文视为授时服务器；把接收时间伪装为交易所事件时间 |
| 原 TqSdk 行情流 | 当前基线权威行情、双源影子比对、历史兼容路径 | 静默地与 CTP 混合为同一个无来源的报价 |
| Windows 系统时间 / SDK Unix deadline | 界面/日志及第三方 SDK 所需兼容入参 | 参与开平仓、报价有效性、授权截止和业务计时 |

CTP 原生字段 `TradingDay`、`ActionDay`、`UpdateTime`、`UpdateMillisec` 不保证在所有交易所/柜台组合中含义相同或齐全。规范化时保留原值，结合明确的交易所规则和可信日历证明源时刻；夜盘跨日、字段缺失或歧义时标记 `TIME_UNPROVED`，不得从 Windows 日期自行补足。行情源时间、当前应用时间和原生柜台交易日是三个不同事实。

### 19.2 「≤1 秒」如何精确定义及验收

- **M1 主要 SLA（拟设发布门槛）**：活跃盘段中可明确识别的**新行情事件**，从原生事件 UTC 到唯一 owner 开始处理该事件的可信 UTC 时间差，目标 `P99 ≤ 1000 ms`；另记 P50/P95/max、样本数、可信时钟误差界与不合格数量。遇到无法证明源时间的样本，不按 0 ms 计入达标，而独立统计 `UNKNOWN` 并阻断以此证明发布成功。
- **M2 内部预算**：CTP 回调复制/入队 → owner 开始处理的本机单调间隔，初始目标 `P99 ≤ 100 ms`；另记录 SDK 泵间隔、策略耗时、账本提交、背压和 owner 最大调度间隙。这个目标不含交易所到行情前置的未知时间。
- **M3 界面体验**：增加 ≤1 秒轻量行情展示发布/刷新周期，且**不得要求市场每秒必须生成新 Tick**。现有 2.5 秒完整工作台快照维持业务/账户视图用途，轻量行情视图不得使用另一发送者或把旧行情标成新鲜。
- **M4 连续性**：活跃合约若连续超过建议诊断阈值（初始 3 秒）无不同身份行情，记录 `QUOTE_SILENCE`，综合市场是否有更新、连接/订阅状态和可比较的第二源区分自然静默与断流；不能直接断言 CTP 服务故障。断网、缓存首帧、源时刻未证明、未处理积压均单独计数，不能从 P99 样本中悄悄剔除。
- **M5 测量位置**：`trusted_receive_utc` 与 `owner_start_utc` 由同一应用可信时钟产生；原始 CTP 事件 UTC 单独解析且保留误差/盘段质量。单调时间只做同一时钟域的本地处理间隔；不同主机的 mono 值绝不相减。对 TqSdk 和 CTP 选择同一合约、同一事件身份可比样本，而不是比较「当时各自最新价」。

初始实测要求覆盖至少一个完整日盘与一个夜盘、开盘活跃段、尾盘、午休、断线和恢复；按每个合约分别给结果，记录真实环境和独立样本分母，不能只汇总四合约平均数。在证据不足时标 `NOT_RUN/NOT_PROVED`，不声称 M1 已被保证。

### 19.3 行情 Provider 接口和 CTP 回调边界

建议新增与 SDK 无关的只读 `MarketDataProvider` / `MarketEvent`：

- `provider_id`（CTP/TQSDK）、`provider_epoch`（每次源会话变化更新）、`instrument_id`、`exchange_id`、`contract_id`（经显式映射的统一合约身份）。
- `raw_action_day`、`raw_trading_day`、`raw_update_time`、`raw_update_millisec`、`source_time_utc`、`time_quality`、`quote_session`；原字段不可被后续修正覆盖。
- `last`、`bid1`、`ask1`、`bid_volume1`、`ask_volume1`、`day_high`、`day_low`、`volume`、`open_interest` 等，按类型/范围校验无效、零价和缺失值。
- `ingress_seq` 是**本机接入排序号**而非交易所原生连续号；CTP 没有等同现有 TqSdk Tick serial `id` 的普适连续性证明。对缺失完整 Tick 路径的信号依赖必须另有证据，否则标记 `PATH_UNKNOWN` 并禁止该信号。
- `trusted_receive_utc`、`receive_mono_ns`、`owner_start_mono_ns`、`latency_quality` 用于可核对的接收、排队和处理延迟；不得用原生报文的 `UpdateMillisec` 猜测网络接收时间。

`OnRtnDepthMarketData` 回调中只复制数据及采集成对时间戳，并向有界通道投递；禁止同步查 SQLite、执行 DSV 策略、请求 NTP、发撤单、执行 GUI 绘制。路径事件不能为提高吞吐而静默合并/丢弃：溢出应有来源、缺号/覆盖质量告警并冻结新入场；展示层可以只保留最新报价。单账户单 owner 不变，行情采集线程无任何交易发送权限。

连接流程包含行情前置握手 → 登录应答 → 逐合约订阅确认 → 首份快照质量检查 → LIVE；断线/重连改变 `provider_epoch`、重订阅、重新校验原生和策略恢复证据。CTP 原生行情通常只推送最新数据、没有可证明完整的历史 Tick 回补能力；不得将重连后的首条缓存行情当作完整恢复历史。

### 19.4 两阶段使用方式，禁止不受控跨源执行

**阶段一，CTP_MD_SHADOW（本计划默认）**：独立行情只读连接，现有 TqSdk 原交易链继续承担全部订单、持仓和风险管理。CTP 数据仅供性能测量、双源字段/时间对照、模拟策略只读回放；不对 owner 注入可能触发交易的 CTP 事件、不新增 TraderApi，不切换生产账本，不自动 fallback。若 CTP 前置权限或 Windows 二进制兼容未验证，阶段停留在本地夹具测试。

**阶段二，CTP_MD_PRIMARY（独立审批/PR）**：当且仅当 DSV 全输入链可用 CTP 事件重放并证明分钟消费/信号/保护等价，且订单/持仓原生事实与报单前行情来源匹配，才允许冷切换权威数据源。当前 `TqKqDsvPort.validate_send`、`DsvFactFeed` 等依赖 TqSdk 原生 Tick ID、Quote 与柜台同步的检查**不能因为切到 CTP 就删除或放宽**。须有等价且可核对的同源报价新鲜度、订单归属、盘段和保护前置校验；无法证明时拒发新单、保留平仓责任并告警。已持仓/活动单/UNKNOWN 未清理或未完成兼容恢复时不得热切换数据源。

### 19.5 开发与发布阻断条件

1. Windows Python 3.13 的 CTP MdApi 封装/二进制、许可和期货公司行情前置权限经真实只读连接验证；可参考 `vnpy_ctp` 的协议用法，但遵循仓库 `AGENTS.md` 禁止直接导入 vn.py 运行时。
2. 冻结四合约 `DCE.p2701`、`DCE.l2701`、`SHFE.bu2701`、`CZCE.OI701` 的合约映射、交易日和夜盘时间证据；不同交易所的字段差异按来源保留。
3. 使用同一可信 UTC 对比同机 TqSdk/CTP，证明 M1/M2/M3/M4；有缺失、无行情、质量 UNKNOWN 时明确注明，不能用降低采样率掩盖 P99。
4. 原 DSV 策略在「相同可比较市场事件 + 相同配置 + 相同账户事实」下行为一致；不直接把 CTP 快照转换成虚假的完整 TqSdk Tick 序列。
5. 真实 Windows SDK 生命周期、重订阅、网络切换、长期内存、队列溢出、进程退出释放及 1 秒界面更新全部完成定向测试；有持仓收盘须证明发单前报价检查、实际发单/成交及柜台终态，不能以时间责任触发成功替代平仓成功。
6. 新行情与旧执行链需要不同来源身份时，必须补原生身份与发单校验合同。未完成该项，即使 CTP 性能符合 1 秒目标，也只允许 SHADOW，不进入 PRIMARY。

参考：[CTP 客户端开发指南的订阅/推送说明](https://github.com/ctp-api/CTP-Quantitative-Trading/blob/main/CTP%20Client%20Development%20Guide.md)、[VeighNa CTP 网关与二进制约束](https://github.com/vnpy/vnpy_ctp)、[项目 dev34 收盘失效证据](refactor/dsv-parity-20261114/31-dev34-simnow-handoff-and-closeout.md)。这些只支持方案设计，不证明目标经纪商线路已实现 `≤1 秒`。

## 附录 A：固定代码与 DSV 证据

以下源码链接固定到本文基线；函数名是审查定位，后续代码变更应更新实施 PR 的映射。

[C01]: https://github.com/Yoruto/OnlineTrader/blob/01ecddaa43e6a1dde27ce9878de2f55e72fb3314/src/online_trader/dsv_runtime/sdk_adapter.py
[C02]: https://github.com/Yoruto/OnlineTrader/blob/01ecddaa43e6a1dde27ce9878de2f55e72fb3314/src/online_trader/dsv_runtime/runner.py
[C03]: https://github.com/Yoruto/OnlineTrader/blob/01ecddaa43e6a1dde27ce9878de2f55e72fb3314/src/online_trader/dsv_runtime/controller.py
[C04]: https://github.com/Yoruto/OnlineTrader/blob/01ecddaa43e6a1dde27ce9878de2f55e72fb3314/docs/strategy-dsv-evidence.md
[C05]: https://github.com/Yoruto/OnlineTrader/blob/01ecddaa43e6a1dde27ce9878de2f55e72fb3314/src/online_trader/runtime/startup_metadata.py
[C06]: https://github.com/Yoruto/OnlineTrader/blob/01ecddaa43e6a1dde27ce9878de2f55e72fb3314/src/online_trader/runtime/desktop_bootstrap.py
[C07]: https://github.com/Yoruto/OnlineTrader/blob/01ecddaa43e6a1dde27ce9878de2f55e72fb3314/scripts/sync-clock-once.ps1
[C08]: https://github.com/Yoruto/OnlineTrader/blob/01ecddaa43e6a1dde27ce9878de2f55e72fb3314/qa/dsv_parity/compare_original_loop.py
[C09]: https://github.com/Yoruto/OnlineTrader/blob/01ecddaa43e6a1dde27ce9878de2f55e72fb3314/qa/dsv_parity/compare_live_logs.py
[C10]: https://github.com/Yoruto/OnlineTrader/blob/01ecddaa43e6a1dde27ce9878de2f55e72fb3314/src/online_trader/dsv_runtime/runtime_diagnostics.py
[C11]: https://github.com/Yoruto/OnlineTrader/blob/01ecddaa43e6a1dde27ce9878de2f55e72fb3314/src/online_trader/gateway/tqkq.py

| 引用 | 内容 |
| --- | --- |
| [C01] | 原始报价、时间来源、帧和发送边界 |
| [C02] | 逐合约决策、等待、收盘、静默、同步 |
| [C03] | 订单责任、维护、价格选择、取消与派发 |
| [C04] | 冻结 DSV 时间/订单行为取证，非本次运行结果 |
| [C05] | 实时偏移输入的时间筛选 |
| [C06] | 桌面启动到旧执行链的真实路由 |
| [C07] | 系统维护校时脚本 |
| [C08] | 直接 frame 对照的覆盖边界 |
| [C09] | 现场日志比较器的有限结论 |
| [C10] | 脱敏诊断边界 |
| [C11] | SDK wait_update 驱动及快照字段来源 |

## 附录 B：外部技术参考与适用边界

资料核对日期：2026-11-17。外部 master/latest 页面会变化，实施者需为实际依赖记录版本/文件摘要；本文不把它们称为产品锁定版本源码。此次 TqSdk 文档页面显示 3.10.1，不能直接代替项目要求的 3.10.4 或冻结 DSV 所携 SDK。引用只作少量行为概述，不复制第三方实现。

[R01]: https://raw.githubusercontent.com/vnpy/vnpy/master/vnpy/event/engine.py
[R02]: https://nautilustrader.io/blog/clocks-and-timers/
[R03]: https://www.quantconnect.com/docs/v2/writing-algorithms/scheduled-events
[R04]: https://doc.shinnytech.com/tqsdk/latest/advanced/timer.html
[R05]: https://raw.githubusercontent.com/shinnytech/tqsdk-python/master/tqsdk/datetime.py
[R06]: https://docs.python.org/3.13/library/time.html
[R07]: https://docs.python.org/3.13/library/asyncio-eventloop.html
[R08]: https://docs.rs/tokio/latest/tokio/time/enum.MissedTickBehavior.html
[R09]: https://learn.microsoft.com/en-us/windows-server/networking/windows-time-service/windows-time-service-tools-and-settings
[R10]: https://learn.microsoft.com/en-us/windows/win32/sysinfo/acquiring-high-resolution-time-stamps
[R11]: https://chrony-project.org/doc/latest/chronyc.html
[R12]: https://doc.shinnytech.com/tqsdk/latest/reference/tqsdk.objs.html

| 引用 | 采用依据 | 不证明什么 |
| --- | --- | --- |
| [R01] vn.py 源码 | 计时事件与消费线程职责分离 | 事件次数等于精确经过时间 |
| [R02] NautilusTrader 维护方技术文章 | 注入时钟、TimeEvent 计划/创建时刻、测试接口 | OnlineTrader 已有纳秒实时保证 |
| [R03] LEAN 官方文档 | 调度与行情错位、陈旧价格与事件次序 | 应照搬其并行交易回调 |
| [R04] TqSdk 定时器文档 | 与 wait_update 同循环、Unix deadline、持续驱动 | 示例覆盖全部断流/恢复情形 |
| [R05] TqSdk datetime 源码 | 行情锚点加本地经过时间的估计思路 | 首个行情锚点一定新鲜或可作为全局 UTC |
| [R06] Python time | 单调间隔、时钟实现/分辨率、平台挂起差异 | 跨系统/重启计数天然可比 |
| [R07] Python asyncio | 到期回调可能提前、等时顺序不确定 | 本项目必须迁移为 asyncio |
| [R08] Tokio 文档 | 迟到周期策略需显式选择 | 必须引入 Rust/Tokio |
| [R09] W32Time 官方文档 | 系统同步、调整与监控职责 | resync 成功等于业务精度达标 |
| [R10] Windows QPC 官方文档 | 间隔计量不同于 UTC，包含挂起时间的行为 | Linux 的时钟具有完全相同语义 |
| [R11] chrony 手册 | 质量含偏差、频率和误差等维度 | Windows 应直接采用 Linux 部署方案 |
| [R12] TqSdk Quote 文档 | datetime 表示交易所发出的北京时间 | datetime 是本机接收时间 |

## 附录 C：验收报告最小模板

```json
{
  "schema_version": "OT_CLOCK_ACCEPTANCE/1",
  "candidate_commit": null,
  "artifact_hash": null,
  "entry_point": null,
  "engine_kind": null,
  "time_semantics_version": null,
  "environment": {},
  "commands_executed": [],
  "fixture_hashes": [],
  "substitutions": [],
  "clock_contract": "NOT_RUN",
  "input_quality": "NOT_RUN",
  "ctp_md_shadow": "NOT_RUN",
  "market_latency_1s": "NOT_RUN",
  "dsv_business_parity": "NOT_RUN",
  "order_lifecycle": "NOT_RUN",
  "recovery": "NOT_RUN",
  "windows_integration": "NOT_RUN",
  "counts": {"pass": null, "fail": null, "error": null, "skip": null},
  "cases": [],
  "artifacts": [],
  "known_differences": [],
  "open_issues": [],
  "evidence_status": "PLAN_ONLY"
}
```

本模板中的 null/NOT_RUN 是有意保留的未知，不得在未执行时改成 0/PASS。开发文档合并、命令退出码为零、哈希存在和某一段行情匹配，均不能替代完整验收。
