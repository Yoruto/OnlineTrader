# 快期和 SimNow 历史查询只读在线核验

2026-11-11，北京时间 12:56:23–12:56:36。使用项目固定 TqSdk 3.10.4 和现有 Windows 凭据引用，各新建一次独立登录。快期账户与 11 月 10 日实测开平的归档账户一致。SimNow 使用项目当前 `TqAccount("simnow", ...)` 通道。两次柜台同步均完成，服务端交易日均为 2026-11-11，两次 SDK 连接均已关闭。

## 柜台实际返回

| 通道 | 当前同步的委托、成交、持仓 | 历史原单和原批次 |
| --- | --- | --- |
| 快期 TqKq | 0 笔真实委托、0 笔真实成交、0 条非零持仓。 | 对归档中 2026-11-10 已确认的塑料开平，分别按 2 个原委托号和 2 个原成交号查询。查询前均不在集合中。查询后 SDK 在本地集合加入空合约、零数量占位对象，继续接收更新后仍为空。没有取得历史终态或逐笔成交。 |
| SimNow | 0 笔委托、0 笔成交。现有 `SHFE.rb2612` 和 `SHFE.rb2701` 各有 1 手昨多，今仓均为 0。 | 返回按合约汇总的方向、今昨手数和开仓均价，持仓行没有开仓成交号、开仓日期或批次明细。当前账户没有可用于逐号反查的已知历史委托或成交样本。 |

快期空对象的数量为零属于 SDK 默认值，不能据此认定旧委托零成交或解除未知占量。SimNow 的昨仓证实当前通道能返回分桶汇总；该结果不足以识别等量换仓后哪些原批次存续。前后账户活动摘要一致。

## 只读边界及补充路径

- 在 SDK 公共交易方法和 WebSocket 发送边界设防。快期和 SimNow 实际各发送 1 个登录包和 10 个读更新包。SimNow SDK 尝试生成 1 个自动结算确认包，已在发包前丢弃。两边下单、撤单、风险设置调用和线上交易指令均为 0。未启动策略或执行内核，未向账本写入回补事实。
- 本机未发现快期专业版默认 8811、8815 监听，未取得 QPro 的实际历史返回。其公开历史成交契约缺委托号和成交时间，项目继续将其标记为只读对照来源。[QPro 历史绩效 API](https://publish2.shinnytech.com/doc/qpro/latest/ai_editor/qpro_performance_api.html)
- SimNow 原生 CTP `ReqQryInvestorPositionDetail` 的公开契约带开仓日期和成交号，是否可用取决于交易后台。本轮固定 SDK 环境未配置可直接调用该接口的独立 CTP 前置与交易绑定，在线结果只覆盖项目当前 TqSdk 路径。没有把该路径的汇总结果外推为原生 CTP 柜台不提供明细。[SimNow 公布的 CTP II Mini 手册](https://www.simnow.com.cn/DocumentDown/api_3/5_2_4/CTPIIMini_API_Ver1.2.pdf)

缺失原单仍可使用原账本留档或经审核的柜台原证据回补。等量换仓仍需要能追溯到原成交身份的存续批次及完整换仓明细。本轮没有满足这两项恢复条件。

[只读查询脚本](../qa/verify_broker_history_readonly.py) · [快期原始结果](../output/online/broker-history-readonly-20261111T045621Z/tqkq.json) · [SimNow 原始结果](../output/online/broker-history-readonly-20261111T045621Z/simnow.json) · [核验摘要与文件哈希](../output/online/broker-history-readonly-20261111T045621Z/verification.json) · [11 月 10 日原开平归档](product-online-simulation-20261110.md)
