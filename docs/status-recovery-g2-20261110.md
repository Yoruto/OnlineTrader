# dev16 实时状态修复与 G2 离线回归

日期：2026-11-10，Asia/Shanghai。基线为用户交接的 dev15；本轮仅实现、离线测试和本地开发 wheel 安装核验，没有登录账户、使用凭据、联网下单或续用历史 scope。B01–B20 继续采用已确认 DSV 基准。

## 证据与复现

原 [下午状态失败](../output/online/afternoon-20261110T042355Z-76c58dabff84/holding-status-request-observation.json)仅记录 `E_TIMEOUT` 且请求与测试结束重叠，没有 owner 栈、取时耗时或请求阶段时间，不能据此判定唯一根因。原账本和新登录结果的摘要仍与原结案审计一致；dev15 的在线成交事实和界面验收限制保留。

本轮先归档 [dev15 源码与测试基线](../output/baselines/20261110T060609Z-dev15-handoff-ba7f5b38/manifest.json)，再用外部离线事实建立一手受管持仓，在原 `SimulationSession`、`ControlServer`、Windows 原生管道和另一个 CLI 进程中注入 6.2 秒只读取时延迟。[修改前复现](../output/status-recovery/dev15-reproduction/result.json)确认状态客户端在五秒期限内返回 `E_TIMEOUT`；后续暂停仍成功。此证据证明同步取时能阻塞 owner，不反推历史在线请求一定经历了相同故障。

历史 NTP 实现有四次 1.5 秒 UDP 等待，DNS 也在同一调用中；不能用简单提高客户端期限掩盖业务 owner 被阻塞。`status_client.py` 继续保持整个握手/快照请求共享的五秒预算、完整校验后发布及失败不发布半份结果。

## 实现

- `runtime/simulation.py` 把只读参考采样放入至多一个后台任务。采样线程只返回 clock/失败标记，不访问 SDK、内核或 SQLite。原 owner 检查结果仍有效，完成原 writer 提交及 CommitAck 接受后才替换参考。失败不延长旧参考寿命；过期仍禁开。关闭后不再接收迟到结果。
- `kernel/server.py` 在 owner tick 前后以有界批次处理已到达请求，避免每次只处理一帧导致积压。关闭时继续处理已协商的只读快照和已有命令回执，拒绝新控制命令；保留一秒总排空预算和正在发送字节的信用，不无限等待慢客户端。
- `gateway/tqkq.py` 验证回报的原订单价、方向、开平、数量、状态和成交合法值。详细成交多于原生累计量时拒绝；原生终态先到而明细未齐时等待，不释放数量。
- `kernel/simulation.py` 从原 journal 恢复原生成交身份/时间，并与原账本及当前完整快照逐笔核对。相同总量但订单身份、价格、成交 ID、方向、合约、纳秒时间不同，或快照有重复身份，均不能完成核对。冲突持久撤权并保留恢复屏障。
- 零成交终态后的迟到成交使本轮结案/入场核对缓存失效，仍走原 FILL、保护、期限与统一 remaining target；不得以旧 CLOSED 缓存忽略新持仓。已记名义结案的迟到成交保留统计修正屏障，不自动美化历史。
- 新适配器从原账本恢复原订单 ID；换 grant、账户、交易日或合约不能接管既有周期。原有外部单发生变化、同合约出现新外部活动或持仓不一致时保持阻挡。UNKNOWN 查无原单不等于未发送，不重放、不释放预占。
- `qa/check.py` 除产品哈希外增加测试、QA、入口脚本、构建版本和依赖锁哈希及运行前后一致性检查。

## 验证范围

`tests/test_simulation_status.py` 覆盖单任务、owner 提交顺序、提交失败、过期结果、关闭后迟到结果、关闭读请求和拒绝新命令。`tests/test_simulation_status_processes.py` 使用真实的独立 owner/CLI 进程及 Windows 管道，覆盖持仓时慢取时下的状态、持久暂停及与 owner 停止重叠的快照。

`tests/test_simulation_recovery.py` 覆盖零成交撤单、原生累计与明细乱序、结案后迟到成交、UNKNOWN 缺失/找回、重复和错身份快照、断线不完整/外部仓冲突、原 grant 重启、新适配器恢复原生身份及原期限。多手部分成交用原核心离线 owner 验证：先成两手，撤余单期间迟到一手，最终三手共用剩余目标，错误累计量不能释放。受限在线 grant 仍只允许一手，整数部分成交不能靠放大线上额度取证。

G2 开发准备已有代码和离线反例；零成交撤单、部分成交、未知发送、真实断线及真实带仓重启的在线结果仍未取得。完整 DSV 行情/自动入场和高级在线保护没有在 dev16 接通，不能把本轮回归计为 G3 在线完成。

新的在线验证仍需另行明确账户指纹、交易日、当前持仓/活动单、合约与时窗、开仓次数/数量和退出/取消范围。尤其未知发送只允许查询原身份、恢复只管理原退出、外部活动不认领。本文没有生成新 grant，也没有向用户请求扩额。

## 最终验证与制品

| 范围 | dev16 本轮结果 | 证据 |
| --- | --- | --- |
| 开发实现 | 状态采样/关闭修复、G2身份与迟到恢复已实现；单owner/单writer/对应Ack后发送保持 | 本文所列源码与测试；完整DSV在线链尚未接通 |
| 全量离线 | 1065项通过，0失败/错误/跳过；编译、边界及产品/验证输入稳定通过 | [最终检查](../output/checks/20261110T062025Z-a3a53650/result.json)、[逐项日志](../output/checks/20261110T062025Z-a3a53650/check.log) |
| 制品与安装 | 新wheel 78份Python、6份资源，与源码及干净无SDK安装逐字节一致；原有参数、报表等安装冒烟通过 | [最终wheel审计](../output/artifacts/20261110T062045Z-dev16-final-ebbdaf02/wheel-audit.json)、[安装冒烟](../output/artifacts/20261110T062045Z-dev16-final-ebbdaf02/smoke-result.json) |
| 已安装持仓IPC | 采样仍阻塞时，状态、持久暂停、再次状态均成功，保护持仓仍为1、权限DISARMED | [实际跨进程离线结果](../output/status-recovery/dev16-final-installed/result.json) |
| 已安装关闭IPC | 已协商快照与owner停止重叠时仍完整返回 | [关闭演练](../output/status-recovery/dev16-final-installed-closing/result.json) |
| 已安装开仓前IPC | 状态、暂停回执及暂停后不可自动重新武装通过；记录请求0、成交0 | [开仓前演练](../output/status-recovery/dev16-final-installed-preopen/result.json) |
| 在线交易 | 未连接账户、未读取凭据、未下单；旧额度不续用 | dev15历史在线结果单独保留 |
| 界面验收 | 离线桌面回归包含在完整检查；本轮未做新的可见GUI/在线持仓界面验收 | 原状态超时不能据离线修复追记为在线通过 |

已安装 dev16 持仓演练三次CLI请求各约0.59、0.63、0.59秒，均在6.2秒的取时注入结束前返回；dev15同场景首个状态请求约5.36秒后超时。这是本次故障注入的单次结果，不是性能或在线SLA承诺。源代码同样通过[持仓演练](../output/status-recovery/dev16-final-source/result.json)及[关闭演练](../output/status-recovery/dev16-final-source-closing/result.json)。

[最终开发wheel](../dist/20261110T062045Z-dev16-final-ebbdaf02/online_trader-0.1.0.dev16-py3-none-any.whl) SHA-256：`35bfaadfad49f9eb31583eaed81b207e15f703ff1028ace99d13b3e89a45f98c`。未发布、未安装服务、未修改独立SDK环境。中间1063项与早期dev16构建保留在原目录，不充当最终1065项对应源码的验证结果。

当前96项状态与后续缺口已更新至[功能清单](功能实现与待办清单_20261110.md)，G2仍为部分完成，G3–G6未被本轮离线结果提升为在线或发行完成。
