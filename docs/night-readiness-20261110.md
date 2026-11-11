# 2026-11-10 晚间运行准备与实际校时记录

后续更正：已核对DSV的零值启动纠正及计算器固定止损，白银会取15。dev22已接入启动自动计算；当前使用[新的自动计算准备包](auto-offsets-completion-20261110.md)，下文dev21保存0/待配置描述保留为当时实现状态，不作为DSV完整行为的结论。

本次用户授权：先同步时钟、完成今晚准备，暂不连接账户。当前版本仍为dev21；没有启动交易连接、交易owner或自动策略，没有创建交易账本。

## 时钟已实际同步

- 校准前两台NTP主机的4次有效采样显示Windows时钟慢约3.140秒，见[原始测量](../output/simnow/20261110-night-inputs/clock-before-sync-1245.json)。
- 已经Windows系统提权启动已有W32Time服务。原`time.windows.com`没有提供有效同步，随后切换到`ntp.aliyun.com`和`ntp1.aliyun.com`并重同步；没有安装服务或定时任务。
- Windows状态已报告Leap 0、Stratum 3、同步错误0，来源`ntp1.aliyun.com`。原始[服务操作记录](../output/simnow/20261110-night-inputs/windows-time-aliyun-result.json)保留了刚重启时仍未同步的中间状态，不能单看w32tm退出码判成功。
- [20:49独立复测](../output/simnow/20261110-night-inputs/clock-after-w32time-1249.json)估计偏差3.534ms；[20:56最终复测](../output/simnow/20261110-night-ready/clock-final.json)估计偏差12.009ms，4次采样均有效，满足产品1000ms门槛。W32Time现为Running，启动类型仍为Manual。

测量文件的`os_clock_changed:false`表示该次只读NTP测量自身不调钟；本轮整体已经由Windows Time完成了系统校时。运行时仍会刷新参考并核对偏差。

## 今晚文件已准备

主目录：[prepared-1254](../output/simnow/20261110-night-ready/prepared-1254/readiness.json)。

| 项目 | 状态 |
| --- | --- |
| 账户凭据 | 已从用户现有文件在内存解析，只写当前Windows用户凭据库，并读回核对。没有另存密码文本、输出密码或登录。运行目录只有无密码引用文件。 |
| 业务参数 | 已提取并核对DSV当前保存的5品种参数，保留自动手数和原风控比例，hash与上一轮输入包一致。 |
| 盘次日历 | 今晚21:00开始、柜台日11月11日；常规4品种23:00结束，白银到次日02:30；包含11日日盘用于同日完整性参考。 |
| 有限运行范围 | 已准备账户专属scope，20:55至次日02:31，禁止自动延续其他柜台日；准备不等于连接授权，权限保持DISARMED。 |
| 额度来源 | 单笔上限30取自DSV `max_volume`；整个5品种范围的开仓尝试上限1000取自每品种`max_daily_orders=200`。实际手数仍按DSV权益×0.002及原保证金/风险门禁计算。 |
| 内核名义风险上界 | 15900，按各品种保存止损距离×合约乘数×30手求和得到，见readiness分项；这是现有品种/手数上限对应的名义计算上界，不以猜测的SimNow余额代替业务手数算法。 |
| 启动入口 | 已生成Start-Night.ps1；默认只检查准备，须显式`-Connect`才连接，另需`-Arm`才申请首次新增。本轮仅执行了默认检查。 |
| 对比入口 | Report-Now.ps1只读取已产生的比较记录并写新报表；没有开始运行时会说明尚无记录，不连接账户。 |
| 依赖 | 已只读核对已有SDK版本3.10.4，使用原环境解释器加载当前源码；没有更新或安装SDK。 |

合约乘数参考[大商所资料中的棕榈油/LLDPE单位表](https://www.dce.com.cn/dalianshangpin/resource/cms/2016/11/%E7%8E%89%E7%B1%B3%E6%B7%80%E7%B2%89%E6%9C%9F%E8%B4%A7%E5%90%88%E7%BA%A6%E8%AE%BE%E8%AE%A1%E8%AF%B4%E6%98%8E.pdf)、[上期所沥青](https://www.shfe.com.cn/products/futures/energyandchemical/bu_f/)、[上期所交易数据单位说明](https://www.shfe.com.cn/reports/tradedata/dailyandweeklydata/)和[郑商所菜油细则](https://www.czce.com.cn/cn/uploadfile/2024/02/07/20240207144928864.pdf)；实际交易仍使用原生instrument事实。

## 保留的原设置与启动前检查

白银当前保存的止损偏移为0。本轮原样保留，没有擅填数值；它尚不具备本产品所需的有效入场止损参数。如果DSV启动计算后改变参数，先使用[Refresh-Preparation.ps1](../output/simnow/20261110-night-ready/Refresh-Preparation.ps1)生成新的本地准备包。此工具只准备，不连接；已有本次交易账本后拒绝新建范围，避免重置额度或遗漏原保护。

原DSV配置变化会被启动前的hash检查阻挡，不会悄悄以旧参数开始对比。现有trade_日志可由未来运行只读采集；本轮未启动或登录原DSV程序。

真实账户登录、账户资金/持仓/委托完整性及SimNow实际行情/成交回报仍按用户要求留到允许连接后核对；这些内容不能在不连接时预先证明。所有当前操作结果见[准备完成核对](../output/simnow/20261110-night-ready/preparation-completed.json)。
