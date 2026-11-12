# 柜台历史查询与等量换仓批次核对

2026-11-11，OnlineTrader `0.1.0.dev25` 源码。

## 接口核对

| 来源 | 可用数据 | 能否直接证明缺失原单 |
| --- | --- | --- |
| 快期模拟 TqKq 与当前 SimNow TqAccount | 固定 TqSdk 3.10.4 的 `get_order`、`get_trade` 读当前交易日同步集合。指定 ID 仍读取本地对象路径。 | 缺失时不能证明历史终态或零成交。 [TqSdk 当日委托成交导出](https://doc.shinnytech.com/tqsdk/latest/demo/base.html) |
| 快期专业版历史绩效 API | 本机 8815 的 `/trades/query` 可按账户和交易日查已同步的历史成交。快期模拟日快照是文档列出的来源之一。 | 返回行缺委托号和成交时间，适合只读对照。快期专业版的持仓明细说明列出暂不支持快期模拟。 [查询 API](https://publish2.shinnytech.com/doc/qpro/latest/ai_editor/qpro_performance_api.html) · [字段](https://publish2.shinnytech.com/doc/qpro/latest/ai_editor/qpro_performance_schema.html) · [持仓明细](https://publish2.shinnytech.com/doc/qpro/latest/blocks/position-detail-list.html) |
| SimNow 公开 CTP 接口 | 文档列出委托、成交、持仓明细查询。委托和成交请求按合约、交易所、单号或时间过滤，持仓明细带开仓日期、成交编号和数量，是否可用依交易后台。标准 CTP 开发指南另列按日期查询结算单。 | 未找到可按过去交易日返回完整原委托终态和逐笔成交的 SimNow 公共接口。历史结算单与持仓明细的实际覆盖未在线验证。 [SimNow 公布的 CTP II Mini 手册 §3.1.24、§3.1.25、§3.1.37、§3.2.26](https://www.simnow.com.cn/DocumentDown/api_3/5_2_4/CTPIIMini_API_Ver1.2.pdf) · [上期技术 CTP 开发指南](https://www.eastmoneyfutures.com/software/9809db08-c8cb-4cc4-b631-8a16f0c2dfb9/0f757a06-10a5-48c2-817e-362b42202b51/a7a19350-141d-48c0-b586-253cb3ec5b18/CTPcdg_ch_2016-10-27.pdf) |

`qpro-history --company <期货公司名称> --client-id <客户号> --start-day YYYY-MM-DD --end-day YYYY-MM-DD --output <新文件>` 查询本机 QPro 已有历史成交，支持显式 `--port`。输出标记为 `COMPARISON_ONLY`，独立于交易账本。命令不登录或同步上游数据，也不会释放原委托占量。

快期与 SimNow 的交易工作台都可用“查询原单留档”。它从原账本查询已持久化的柜台终态观察，验证原单号、原提交日、详细成交和当前同步快照后，沿原回报链继续恢复。只有当前集合确实缺失且仍未决的原单进入该流程。找不到完整终态时保留原占量，可用已有的“回补历史原单”导入审核过的柜台证据。

## 等量换仓

在“柜台持仓差异”中选择待核对合约，使用“核对等量换仓”。通过 `workbench-status` 查看该行的 `original_lots`，依据可追溯的柜台存续批次明细整理 `POSITION_LOT_REVIEW/1` 文件。若柜台报表使用交易所成交号，须从原回报或柜台资料先核对到当前通道的原成交身份。下面的占位符用于说明字段。

```json
{
  "schema_version": "POSITION_LOT_REVIEW/1",
  "account_key": "<当前账户键>",
  "gateway_identity": "<当前通道身份>",
  "calendar_hash": "<绑定日历哈希>",
  "trading_day": "2026-11-11",
  "contract_id": "SHFE.bu2701",
  "statement_reference": "<柜台持仓明细流水标识>",
  "original_lots": [
    {"lot_key": "<原批次键1>", "remaining_quantity": 1},
    {"lot_key": "<原批次键2>", "remaining_quantity": 0}
  ],
  "replacement_lots": [
    {"trade_id": "<替换开仓的原生成交号>", "remaining_quantity": 1}
  ]
}
```

文件必须列出所选合约全部原有存续批次及其核对后数量。内核按明确的批次键移除已退出数量，并用当前柜台明细中的外部开仓逐笔建立替换周期。方向、仓桶、完整平仓明细、原单终态和当前柜台仓位须互相吻合。已消耗的批次或成交身份不能复用。保留存续原批次的保护，替换批次的历史成本和手续费仍按未知处理。

当前可核对有原生身份、当前交易日能取得完整换仓成交的同桶等量换仓。缺少原批次身份、跨历史日缺逐笔明细、组合平仓无法证明今昨归属等情形保持阻挡。

## 离线验证

16 组定向回归共 246 项通过。最终复跑相关的 12 项全部通过，覆盖 QPro 分页和账户边界、留档终态恢复、按指定原批次归属、缺少逐笔或使用错误成交时拒绝，以及重启保留批次。源码编译、Git 差异检查及 CLI 帮助检查通过。QPro 使用模拟响应，快期和 SimNow 使用离线柜台事实。本轮账户连接和线上报撤单均为零。
