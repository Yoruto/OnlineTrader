# 全业务参数编辑与DSV离线偏移计算

日期2026-11-10，版本`0.1.0.dev12`。按原规划C01/C05补齐业务参数表单和偏移计算的离线链路。原DSV和冻结V7未运行或修改；没有柜台、SDK、账户连接、联网行情、撮合、服务安装或发布操作。

## 参数编辑合同

入口仍是`parameters desktop`的DSV参数中心。“编辑全部业务参数”打开独立工作副本，涵盖69项交易/风控参数、4项偏移计算设置、4项定时设置以及品种的9列（含可选换月候选）。布尔开关使用复选框，整数、精确小数及HH:MM分别验证；公式和收盘报价模式提供候选选择；假日列表与品种系数表使用明确的JSON数组/映射。支持参数名称筛选、品种增删、多行选择及批量值设置。原账户/口令、UI、通知密钥和运行权限不属于可编辑业务档案。

“校验并更新预览”成功后才替换参数中心的不可变DsvProfile。任何坏字段、重复合约、非法比例/日期/时间或超出64品种的批次都拒绝；输入继续保留，可修正或取消，不产生半份参数。批量设置先验证整个候选，再改变所选行；选中多行时不影响其他行。取消编辑放弃工作副本。编辑期间父预览变化则拒绝旧结果，要求重开。

交易比例使用0–1小数；偏移/滞回单位为价格点，金额沿原元值、时间沿原秒/分钟值。整数不接受小数、负数或隐式取整；精确小数最多18位整数及12位小数，不经过float，不接受负零、非有限数或科学计数法。非负数0、关闭开关及预填字段分别保留，不做取绝对值、自动启用或无界解释。偏移为0的品种继续保留待配置提示。

`configuration/editor.py`负责表单转换和整体验证；`desktop/editor.py`不持有内核/IPC引用。完整修改使用原“导出业务参数”保存新JSON，再用原显式NON_TRADING登记流程保存草稿；不会自动切换执行配置。DSV模板继续只保存原33项交易字段，完整档案不能以模板替代。系数键扩展为支持DSV使用的中文品种名，同时拒绝空白、控制字符及`p;execute`等非法标识；旧ASCII键和原确认基线保持兼容。

## 公式依据与数据来源

静态读取用户提供的`trading.exe`，SHA-256 `aabeafd83d265ab9b1cf5cc3fd0e9b0f21963ddcc95daa70feefc209997269a3`。其中`PYZ.pyz/offset_calc`的marshal数据15669字节，SHA-256 `26af1a7bd803b26c81a2274bbabf682d11af43e0ca41da21325101284887f412`。没有执行其代码对象。公式标签、全部品种系数、固定止损、函数位置及条件取证见[静态事实](references/dsv-offset-formulas-20261110.json)；脚本和反汇编位于`output/tools/offset-*-20261110.*`及`calculator-evidence-20261110.*`。gui模块指纹与此前参数模板取证相同。

独立公式版本为`DSV_OFFSETS/1`，实现位于`configuration/offsets.py`，不依赖pandas或交易SDK。R表示日线high-low，TR表示max(high-low, abs(high-上一收盘), abs(low-上一收盘))；首根TR取自身振幅。

| 公式 | 基准B | 数据序列与最少已完成根数 |
| --- | --- | --- |
| minrange | min(SMA3(R), SMA30(R))；沥青为SMA20(R) | 其他品种使用具体合约；可用3根短窗时按原pandas跳过NaN行为计算并提示长窗不足；沥青使用主连且需20根 |
| range3x09_contract | SMA3(R)，对应系数表 | 具体合约，3根 |
| range3x09_main | SMA3(R)，对应系数表 | 主连，3根 |
| atr20_main | SMA20(TR) | 主连，20根 |
| atr_mixw_main | (EWM5(TR)+EWM10(TR)+EWM20(TR))/3 | 主连，首根种子、alpha=1/n、adjust=False；原实现1根即可有值，不伪称20根限制 |
| range3_main | SMA3(R)，对应系数表 | 主连，3根 |

两种range3x09原式的0.45等系数已含“三日振幅和/6×0.9”的换算，不再额外乘一遍0.9。

| 品种名（严格按DSV名称） | minrange | range3x09_contract | range3x09_main | atr20_main | atr_mixw_main | range3_main | 固定止损 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 塑料 | 0.45 | 0.45 | 0.45 | 0.36 | 0.355 | 0.38 | 20 |
| 沥青 | 0.38 | 0.4 | 0.31 | 0.37 | 0.35 | 0.3 | 12 |
| 菜油 | 0.45 | 0.45 | 0.45 | 0.375 | 0.39 | 0.445 | 15 |
| 棕榈油 | 0.45 | 0.45 | 0.45 | 0.39 | 0.38 | 0.38 | 16 |
| PTA | 0.45 | 0.45 | 0.45 | 0.33 | 0.335 | 0.45 | 18 |

其他名称按DSV回退系数0.35和固定止损15；不会根据相似名称猜测品种。沥青例外和系数覆盖都按保存品种名匹配。`auto_offset.coef`的正数覆盖对应默认系数；保存0按DSV数值真假规则回退默认，原保存0不被改写。计算器“编辑品种系数”只修改其工作副本，重新计算并应用所选结果后才更新父预览；取消计算器时一并放弃。

## 取整、振幅与完成日线

设c为有效品种系数，C为振幅上限比例（原默认0.5），F为开仓下限比例（原默认0.4），A为用户明确输入的当日已实现振幅：

```text
O = max(round_ties_even(B × c), 1)
当A > 0且C > 0：open = max(int(max(min(O, A × C), min(O × F, O))), 1)
否则：open = O
profit = max(round_ties_even(B × c × 0.9), 1)
stop = DSV固定品种止损表，未收录名称为15
```

`int`对最终正数截断，不先截断振幅或下限；止盈使用未取整基准独立计算，不由已封顶开仓偏移倒推。全部计算使用有理数，按DSV的Python round语义五成双，避免float边界漂移；报告同时保留精确分子/分母及12位显示小数。C/F是非负比例，按原代码保留大于1时的min/max夹限，不新增隐式绝对值。缺失/0振幅、0上限按DSV跳过振幅约束，并在界面明确提示。

截止交易日由用户显式给出，全部date>=截止交易日的日线排除，不按本机18点或周末猜交易日。原`completed_only`在没有更早数据时回退整份输入的行为会使用未完成日线，本实现明确拒绝该回退。没有对应具体合约/主连数据时不混用另一来源，也不自动把旧合约、换月候选或同名品种日线移接过来。上述修正是输入完整性边界，不修改已确认保存基线。

## 离线数据与操作

日线CSV为UTF-8（可带BOM），列名`series_id,date,high,low,close`，顺序可调整但不得缺列/重列/增加私有列。具体合约示例`DCE.p2701`；主连示例`KQ.m@SHFE.bu`。同一序列日期必须严格递增，不重复；high>=close>=low>0，允许真实零振幅。最大2MiB、16384行、128个序列、每序列512根。空文件、坏行、错序、非法价格/日期/身份均整批拒绝并给行号。DSV原始日线缓存应先显式标明series_id，不能靠文件名自动证明数据来源。

“加载日线CSV”校验后形成不可变内存缓存，之后修改原文件不会偷偷改变结果；重新加载有效文件才替换缓存并让旧结果失效。每次计算绑定数据哈希、表单profile哈希、截止交易日和公式版本，显示实际序列、已完成根数、最后日期、当前与计算开/损/盈、是否施加振幅约束。日线缺失的品种明确不可计算；其余成功行可逐项勾选或全选。

公式、日期、C/F、品种系数或当日振幅改变时清除旧结果并禁用应用；要重新计算。单击“应用所选到预览”只更新选中成功合约的3个偏移及明确的计算设置，保留其他品种、手数、候选合约、滞回、交易开关和持仓计划。重复/未知/失败合约选择及过期profile均拒绝。全局参数0与待配置白银原值在未选择前保持原样。当前值对比使用完整合约身份，修正原DSV按同名品种覆盖多行的风险。

复制文本和导出计算依据不应用结果；计算依据JSON按排他新文件发布，不能覆盖既有文件。CLI每次只发布一个输出，先验证所选应用完整性，避免写出部分结果后再发现选错合约。

```powershell
Set-Location E:\online-trader
.venv\Scripts\python.exe -m online_trader parameters desktop
# 以下日线样例完全由开发工具人工生成，不是市场数据
.venv\Scripts\python.exe -m online_trader parameters calculate --daily docs\examples\offset-daily-sample.csv --as-of 2026-11-10 --output output\offset-evidence.json
.venv\Scripts\python.exe -m online_trader parameters calculate --daily docs\examples\offset-daily-sample.csv --as-of 2026-11-10 --apply-output output\offset-preview.json --contracts DCE.p2701 SHFE.bu2701
```

可选`--input`业务JSON、`--dsv`显式剥离原DSV私有区、`--formula`、`--ceiling`、`--floor`和`--ranges`。ranges是合约到精确小数字符串的JSON映射，例如`{"DCE.p2701":"100"}`。省略输出时只打印报告；`--apply-output`必须同时给`--contracts`，仍只是新的业务参数文件。所有结果标记`runtime_activation:false`。

## 验证与保留范围

最终独立检查 **962项通过，0失败/错误/跳过**，编译、产品导入边界、6份冻结资源检查通过，检查期间源码稳定。见[确切报告](../output/checks/20261110T021134Z-92e067aa/result.json)及[逐项日志](../output/checks/20261110T021134Z-92e067aa/check.log)。报告SHA-256 `ccc021142b78b588f146053779d3cf6e2ab7cb85122c5232debdca548c82f6cf`。开发环境`pip check`无冲突。

新增84项包含表单域19、计算域32、CLI8、Qt25；原878项同时通过。覆盖全部69项交易字段的独立变更、77项表单完整往返、关闭字段预填值、中文系数/0回退、日期/精度/范围/批次失败、候选保留、多行编辑、6种公式与静态系数表一致、短窗不足、ATR跳空/首根种子、精确半整数取整、振幅上下界、未完成日线排除、错误来源拒绝、缓存不可变、过期计算禁用、逐合约应用及导出排他失败。扩展的持仓隔离检查在明确注入3手成交的开发周期中编辑二阶段开关并应用计算偏移，确认已提交计划、保护数量、authority、账本序号和记录端口请求列表逐项不变。QA端口只记录请求，不连接柜台或撮合成交。

开发制品为[dev12 wheel](../dist/20261110T021226Z-186e52be74b5/online_trader-0.1.0.dev12-py3-none-any.whl)，SHA-256 `bf233f67955b9474b9d1f2681b156858040b5899df9cd0cfd30a987dad8a99e2`。[构建/安装审计](../output/artifacts/20261110T021226Z-186e52be74b5/wheel-audit.json)核对73份Python源码+6份冻结资源与最终检查、wheel和全新安装逐字节一致；无测试/QA、原软件代码、凭据或柜台SDK。全新核心venv使用本地wheelhouse离线安装，以`-I`运行，确认无Qt、QA和柜台SDK；既有策略/保护/调度/报表/对照/模板冒烟与本轮完整表单、中文系数JSON、数据哈希、振幅约束和已安装CLI冒烟通过。制品没有发布或部署。

实际Qt离屏渲染及人工查看见[全参数筛选](../output/previews/20261110T020842Z-8182c8c1a4-parameter-tools/all-parameters.png)、[多行编辑](../output/previews/20261110T020842Z-8182c8c1a4-parameter-tools/batch-symbols.png)、[偏移计算器](../output/previews/20261110T020842Z-8182c8c1a4-parameter-tools/offset-calculator.png)、[应用后预览](../output/previews/20261110T020842Z-8182c8c1a4-parameter-tools/calculated-preview.png)、[旧结果失效](../output/previews/20261110T020842Z-8182c8c1a4-parameter-tools/stale-result-disabled.png)。数据完全为人工生成的离线样例，Windows字体只在QA进程中加载。截图对应的全部79份产品文件与最终完整检查及当前源码一致，见[哈希核对](../output/previews/20261110T020842Z-8182c8c1a4-parameter-tools/final-source-match.json)和[视觉记录](../output/previews/20261110T020842Z-8182c8c1a4-parameter-tools/visual-review.json)。

本轮不宣称整个G4完成：联网参考数据、已安装桌面交互、断电/长稳和真实合约目录仍不在本次离线验证范围。原G0/计划1.0/1.1/1.2/DSV确认6份资源保持原哈希，V7保持冻结。

dev11原75份产品文件已按878项检查源码哈希逐字节归档于`output/baselines/20261110T014738Z-c555402f/dev11-product.zip`，SHA-256 `ab4f8a7b2ad0921e9b0f7e68621f5977474122ba0192f2be71206e65df3a676f`。旧制品、检查和Qt证据保留，不覆盖旧结论。
