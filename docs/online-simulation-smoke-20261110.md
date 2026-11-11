# 快期在线模拟：一手开仓与一分钟后平仓

日期2026-11-10。用户明确授权使用所提供的SimNow或快期模拟账号，在任意合约下单并于一分钟后平仓。本次选择快期在线模拟服务TqKq，仅一次买入开仓、1手上限；没有使用实盘资金账户。SDK为3.10.4，仅安装在`output/online/sdk-venv`，不进入OnlineTrader的环境、runtime或wheel。

**结果：完成。** 独立重新登录后确认账户无持仓、无活动委托；原有两条沥青委托及两条成交保持不变。本轮新增两条委托、两条成交，均属于本次一手开平。

| 动作 | 合约 | 方向/手数 | 成交价 | 服务端成交时间（北京时间） |
| --- | --- | --- | --- | --- |
| 开仓 | DCE.m2701（豆粕2701） | 买入开仓1手 | 3075 | 2026-11-10 10:40:24.704 |
| 平仓 | DCE.m2701 | 卖出平仓1手 | 3073 | 2026-11-10 10:41:24.904 |

服务端成交间隔`60.200029987`秒；本地按实际成交确认启动的单调计时为`60.1877649`秒。平仓意图在原60秒截止后约0.143秒持久化，之后调用SDK发送。开平各一次，无撤单或重报价。模拟余额从997688.656变为997665.656，余额差−23.000；此处不推断其手续费分项。

## 证据

本轮完整证据位于[成功测试目录](../output/online/20261110T024003Z-97f0ec18-kq-one-minute-r2)。

- [开平结果](../output/online/20261110T024003Z-97f0ec18-kq-one-minute-r2/result.json)保留原委托ID、成交ID、价量、服务端纳秒时间戳和本地计时；SHA-256 `8986e28bc666f948845b6ba20856b1b1ec650e179d30b42723021ea0440b9135`。
- [独立重连快照](../output/online/20261110T024003Z-97f0ec18-kq-one-minute-r2/final-reconnect/preflight.json)在平仓后新建连接，完成初始交易状态同步，重新获取资金、持仓、当日全部委托和成交；最终共4条委托/4条成交，无持仓、无活动委托。这是快期同步流的完整新登录快照，不冒充CTP四种原生查询的请求回执。
- [结案审计](../output/online/20261110T024003Z-97f0ec18-kq-one-minute-r2/closure-audit.json)逐项连接初始/最终委托与成交身份，验证仅新增本次两条、原记录逐字段不变、测试数量为1、订单均终态且无剩量、平仓意图晚于截止。
- [探针SQLite账本](../output/online/20261110T024003Z-97f0ec18-kq-one-minute-r2/probe-ledger.db)独立于产品账本，单写入者、WAL/FULL，意图commit后才发送。审计验证SQLite integrity_check及完整事件哈希链，OPEN_INTENT/CLOSE_INTENT各1个。
- [17项保护测试](../output/online/20261110T024003Z-97f0ec18-kq-one-minute-r2/policy-tests.log)覆盖一手额度、成交计时、提前平仓拒绝、未知发送占量、撤单终态/成交一致后才释放、重复回报、持仓归属冲突、报价时效、涨跌停/最小变动价位和NTP参考过期。该测试日志先于实际开仓生成。
- [源码身份](../output/online/20261110T024003Z-97f0ec18-kq-one-minute-r2/source-manifest.json)、源码副本、[SDK锁定清单](../output/online/20261110T024003Z-97f0ec18-kq-one-minute-r2/sdk-requirements.txt)及[产品未改变核对](../output/online/20261110T024003Z-97f0ec18-kq-one-minute-r2/source-identity.json)保留复核入口。原dev12的73份Python+6份资源仍与962项离线检查逐字节一致，没有用此次在线结果重新标记旧制品。

所有模拟凭据直接在进程中读取用户指定文件；未复制到产品配置、源码、命令行或本报告。SDK通知关闭，证据仅写白名单交易字段及账号指纹。原DSV进程28620保留，本轮探针和独立复查进程均已退出，没有安装后台服务或自动重复任务。

## 前置诊断与时间口径

首次只读登录成功，账户原无持仓/活动委托；当时行情停在10:14:59。之后恢复的新行情仍被未来时间门禁拒绝，没有OPEN_INTENT。定位时在持有探针账本SQLite写事务并确认开仓意图为0的条件下终止该只读等待进程；[原记录](../output/online/20261110T022517Z-a956b5cc-kq-one-minute/preflight-abort.json)保留，没有将它改写为成功交易。

只读NTP观测发现本机时钟慢约2.6秒。第一组6次观测中2次有效、4次超时，未将超时算作有效样本。成功轮重新测量两个配置NTP服务器，至少3个有效样本、往返不超过0.5秒、样本离散不超过0.25秒、偏差不超过10秒才建立参考；成功轮偏差为`+2.614698350429535`秒，见[NTP参考](../output/online/20261110T024003Z-97f0ec18-kq-one-minute-r2/clock-reference.json)。参考用单调时钟推进并有300秒有效期；原报价时效条件保持未来不超过1秒、过期不超过5秒，仍需连续两次不同的新鲜报价及有效买卖盘/涨跌停。

没有修改Windows系统时间、时间服务、原DSV或产品时钟状态。原始本地`at_utc`字段沿系统时钟记录，行情时效判断使用已观测NTP参考；表中时间使用服务端成交时间戳，60秒持有使用单调时钟。这些时间口径分别保存，不相互冒充。

## 验证范围

执行器是`qa/online_kq_one_minute.py`独立模拟探针，账户强制为TqKq；直接使用明确客户端订单ID，发送前提交意图、未知发送不重发、仅本次有据成交可退出。没有通过OnlineTrader现有的DEVELOPMENT端口发送联网订单，也没有把模拟结果注入原产品账本。

因此本次证明快期账号/行情/委托/成交链路和这一轮一分钟开平完成；主程序在线适配器、DSV策略自然入场、半仓/二阶段、跨进程保护恢复及整个G2仍待实现/验收。本轮一次开仓额度已用完并结案，没有留下后续自动开仓任务。

官方接口依据：[TqKq在线模拟账户](https://doc.shinnytech.com/tqsdk/latest/usage/shinny_account.html)、[TqApi订单与状态接口](https://doc.shinnytech.com/tqsdk/latest/reference/tqsdk.api.html)、[NTP四时间戳算法](https://datatracker.ietf.org/doc/html/rfc5905)、[阿里云公共NTP域名](https://www.alibabacloud.com/help/en/ecs/user-guide/alibaba-cloud-ntp-server/)。
