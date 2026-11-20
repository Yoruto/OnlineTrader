# AI 开发辅助第一批静态基线

> 日期：2026-11-20  
> 源码基线：`8fd412e1f960cc0a8c3b23b8ca68f289f5db49d2`  
> 范围：文档权威、模块边界和 B01/B04/B05/B08 参考答案  
> 产品测试、Qt、Named Pipe、回放、账号连接、报单、打包：`NOT_RUN`

## 结果

| 项目 | PASS | FAIL | SKIP | NOT_RUN |
| --- | ---: | ---: | ---: | ---: |
| 静态参考答案人工核对 | 4 | 0 | 0 | 0 |
| 受控效率对照测量 | 0 | 0 | 0 | 4 |

四项任务的参考文件、符号、合同和拒绝条件记录在 `benchmark-tasks.yaml`。本次通过 `rg`、Git 和直接源码阅读核对，没有调用 SDK、运行产品入口或执行测试。

## 已确认的问题

1. `trading-workbench-client-refactor-20261119.md` 仍保留注册表持久化 pending 和跨 boot 查询责任，当前 Standalone 合同已改为进程内关联。
2. `runtime-operations-and-storage-20261119.md` 正文仍保留长期原账本与换机 handoff 历史设计；其文件头已有新规则覆盖说明。
3. 单一文件状态不足以表达局部替代，因此 `architecture-status.yaml` 使用 `path + scope` 作为记录身份。

## 尚不能声称

- 没有执行相同任务的多轮冷/热缓存对照，因此不能声称定位耗时或 token 已下降。
- 没有安装或运行 Serena，不能声称其对本仓库有效。
- 项目 Skills 只完成结构和静态校验；真实任务触发准确率等待后续使用数据。

## Skill 规范源同步

- 源仓库与基线：`Yoruto/OnlineTrader`，HEAD `8fd412e1f960cc0a8c3b23b8ca68f289f5db49d2` 加本次未提交文档变更。
- 规范源：`E:\online-trader\docs\skills\online-trader.md`，SHA-256 `66432CF4571760E5B046F5616779631970C5894FE0B21E385F7375DD1BE2A430`。
- 已安装路由镜像：`C:\Users\xyc\.codex\skills\futures-trading-assistant\references\online-trader.md`，SHA-256 `9108640397BCBDCADB97BC46D7881CC2CDE2127C76A66F90C0FF027FDFB346EE`。
- 同步状态：`SYNCED_SCOPED_GUIDANCE`。项目 Skill 路径、规范源和状态清单导航已经同步；镜像是面向全局 Skill 的摘要，全文不要求与规范源逐字相同。

后续只有在评估具体导航工具或 Skill 版本时才记录耗时、读取文件数、token（可取得时）、路径误判和返工。四项试点出现稳定收益后，再决定是否扩展 B02/B03/B06/B07。
