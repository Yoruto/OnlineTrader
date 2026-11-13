# DSV交易参数模板与品种CSV

日期2026-11-10，独立开发版本 `0.1.0.dev11`。对应原实施计划C03/C04的离线工作流。模板、CSV和JSON操作只产生预览或业务文件；保存参考版本仍走原NON_TRADING配置目录，不变更authority或已有持仓保护。

本页保留dev11结果；dev12已补齐业务参数表单编辑及离线偏移计算，最新证据见[编辑与计算合同](parameter-editing-offset-contract.md)。下文未完成清单属于dev11阶段记录。

## DSV依据与边界

只静态读取用户提供的 `C:\Users\xyc\Desktop\trading\trading.exe`，没有启动应用、导入原应用代码或SDK。EXE SHA-256 `aabeafd83d265ab9b1cf5cc3fd0e9b0f21963ddcc95daa70feefc209997269a3`；gui代码对象995361字节，SHA-256 `07c82c9ada765284ed5a929227024cbf192f5f72192636900740d9a5764032ea`。函数位置、33个字段、144个别名及原8个CSV标题记录于[独立静态事实](references/dsv-parameter-workspace-20261110.json)。取证仅使用marshal/dis读取常量和指令，不执行代码对象；提取脚本与指令输出位于 `output/tools/parameters-*-20261110.*`。

DSV `on_save_template`（15119–15169）以名称保存33项交易表单参数；`on_apply_template`（15171–15267）填回界面；`on_delete_template`（15269–15282）删除所选名称。独立程序按这33项保存，详见事实JSON，不扩大为全局/品种/定时配置快照。应用仅覆盖这些字段，原品种清单、偏移计算、定时与未收录的保护参数保留；不会复制原应用“缺失字段回退成false”的行为。完整业务配置备份继续使用原JSON导出。

DSV `on_import_symbols`（14980–15057）兼容中文和英文标题，但会跳过部分坏行、整数截断偏移并对部分半仓值回退0.5；本实现按原规划的整批验证要求修正：不跳行，不截断小数，不把非法值改成默认值。半仓比例明确使用0–1小数；`半仓%`是兼容列名，填写50不会被解释成0.5。已保存0、关闭开关及其预填值原样保留，包括白银0/0/0。

别名表来自 `SYMBOL_ALIAS`（3060–3130），70个品种、144个别名。离线解析要求明确月份，如`白银2612`、`ag2612`、`SHFE.AG2612`统一为`SHFE.ag2612`；`TA701`保留三位月份，不猜年份；`沪深3002612`支持带数字的品种别名。拒绝裸品种、主连、错误交易所及无效月份。新产品仅接受明确交易所和月份的完整代码。这些是词法解析，不证明合约存在、到期状态、可交易性或当前主力；运行前的合约核验继续由原准入链负责。

## 桌面操作

`E:\online-trader\.venv\Scripts\python.exe -m online_trader parameters desktop` 打开已有参数中心，不需要启动内核。

1. 品种清单选择“替换品种预览”或“合并品种预览”，再导入CSV。替换改变当前预览列表；合并按解析后的合约更新原行并追加新合约，保留原顺序。查看“与基准的差异”后，可导出业务JSON或品种CSV。
2. “选择模板目录”只读载入，不创建目录。输入名称、点击“另存模板”才写入本地历史。选择已有模板后，“应用到预览”填入33项交易参数；要制作不同参数的模板，可先用原业务JSON导入预览。
3. “覆盖所选”“删除所选”显示明确对象并要求点击确认，旧版本保留。其他窗口写入后，当前窗口拒绝应用、覆盖或删除过期选择，点击“重新载入”后再核对。
4. 关闭窗口不会更改持仓保护。恢复基准只恢复预览，不清空模板历史。CSV/模板写入失败时，当前预览和已发布版本保留，错误不回显原始单元格。

## CSV合同

UTF-8，可带BOM；输出为BOM+CRLF。整份最大262144字节，最多64个品种。标题、列数、每行、重复解析合约、合并后数量全部校验，任一失败整批不返回候选。错误给出物理行号、受影响字段及固定原因；空白行不静默跳过，只有标题/空列表也拒绝。未知列和别名映射后的重复列拒绝，账户、口令等不进入白名单。

| 字段 | 中文/兼容列名 | 规则 |
| --- | --- | --- |
| name | 品种名 | 必填，保留Unicode；控制字符及超长文本拒绝 |
| code | 合约代码 | 必填，明确合约或别名加月份；解析后不得重复 |
| open_offset / stop_offset / profit_offset | 开仓偏移 / 止损偏移 / 止盈偏移 | 必填，非负价格点，最多18位整数、12位小数，0原样保留 |
| order_volume | 下单手数 | 非负整数或`自动`；缺列默认0（保存的自动手数值），空单元格拒绝 |
| half_close_ratio | 半仓比例 / 半仓% | 0–1小数，缺列默认0.5，空单元格拒绝 |
| hysteresis_points | 滞回点 / hyst_pts | 非负价格点，缺列默认0，空单元格拒绝 |
| code_new | 候选换月合约 | 新增可选第9列；非空使用相同代码解析，显式空格清除预览候选；旧8列不提供此字段时保留同合约已有候选 |

所有字段兼容英文内部名称。数字不接受负数、负零、NaN/Infinity、科学计数法、布尔值或千分位，不经过float。候选换月不会替换保存合约或自动迁移仓位。导出包含第9列，防止白银候选合约丢失。品种名称以`= + - @ '`开头或带首尾空格时使用可逆单引号转义，避免电子表格把名称当公式；本程序导回时解除该转义。

## 模板存储与故障行为

实现位于 `configuration/templates.py`。调用`read()`获得不可变快照；`save(name,profile,expected_revision,overwrite=False)`、`delete(name,expected_revision)`按明确全局版本写入；`snapshot.apply(name,profile)`返回新DsvProfile，不持有内核/IPC/数据库引用。模板名1–64字符、NFC、首尾无空格，无控制字符及路径分隔符，名称只作键，文件名由版本号生成。上限64个当前模板、1024次修订，达到上限明确拒绝；不自动清理历史。

每次操作新建`00000001.json`等规范JSON事件，字段为schema_version/revision/previous_hash/action/name/trade；动作CREATE/OVERWRITE/DELETE。文件最多32768字节。读取顺序校验连续版本、前序哈希、字段、动作转移与全部参数；格式破损、缺号、非法字段不能被当作空目录。历史是本地恢复证据，不是抵御外部篡改或删除的认证备份。

先在同目录写临时文件、flush/fsync，再以排他硬链接发布新版本；无可变head、无就地覆盖。两个写入者竞争同一版本时只有一个发布成功，另一个收到`E_TEMPLATE_CONFLICT`并要求重读。发布前中断只留下未发布临时文件，读取忽略；清理当前操作的临时文件，不清除别人的历史。原JSON和CSV导出复用排他发布，已存在目的文件绝不覆盖。未支持硬链接的目标文件系统明确失败，没有降级为覆盖写。此验证不宣称跨机器网络盘、断电或长期耐久验收。

## 命令行

以下相对路径以`E:\online-trader`为工作目录；应用/CSV导入省略`--output`时只打印预览，提供时写入新的业务JSON。

```powershell
.venv\Scripts\python.exe -m online_trader parameters symbols-export --output output\my-symbols.csv
.venv\Scripts\python.exe -m online_trader parameters symbols-import --csv output\my-symbols.csv --mode merge --output output\symbol-preview.json
.venv\Scripts\python.exe -m online_trader parameters template-list --store output\my-templates
.venv\Scripts\python.exe -m online_trader parameters template-save --store output\my-templates --name DSV默认 --expected-revision 0
.venv\Scripts\python.exe -m online_trader parameters template-apply --store output\my-templates --name DSV默认 --input output\symbol-preview.json --output output\template-preview.json
.venv\Scripts\python.exe -m online_trader parameters template-save --store output\my-templates --name DSV默认 --input output\template-preview.json --expected-revision 1 --overwrite
.venv\Scripts\python.exe -m online_trader parameters template-delete --store output\my-templates --name DSV默认 --expected-revision 2
```

`--input`读取业务JSON；原DSV配置需另加`--dsv`，先过滤账户/通知/UI。模板列表显示版本，保存/删除必须传所见版本。输出`runtime_activation:false`，不会隐式调用`parameters save`、启动内核或获取账户锁。

## 验证与后续范围

最终独立检查 **878项通过，0失败/错误/跳过**，编译、导入边界及冻结规格检查通过，检查期间源码稳定。确切报告见[最终检查JSON](../output/checks/20261110T014057Z-811eb0e0/result.json)及[逐项日志](../output/checks/20261110T014057Z-811eb0e0/check.log)；报告SHA-256 `86a6e6276a356395841dab903d0d7409f817e0cd37e8c48b8e3b1295ea54154f`。开发环境`pip check`通过。

新增68项包含CSV26项、模板23项、Qt/CLI工作流19项，既有810项同时通过。重点包括144个别名和原33字段与独立静态取证一致；精确小数/0值/候选保留；整批拒绝、重复、范围、编码与文件容量；两写入者争抢同一版本只成功一个；发布失败不破坏旧版本；坏历史/缺号不当作空目录；过期界面必须重载；取消确认不修改；凭据剥离及公式名称转义。实际已入场且有3手自有成交的开发周期中，执行CSV/模板/恢复预览后，已提交计划、保护剩量、authority、账本序号及记录端口发送列表逐项保持一致。成交由QA明确注入，端口只记录请求，没有撮合或柜台订单。

开发wheel见dev11制品，SHA-256 `0ac5f1bd8b383d9ed42826b19c9810784fe5e19dfc59acf5afb43ef5ddfe596c`。[构建审计](../output/artifacts/20261110T014138Z-b6ff584cdeaf/wheel-audit.json)核对69份Python源码、6份冻结资源与最终检查、wheel、全新核心安装逐字节相同；QA/测试、原软件代码、凭据和柜台SDK均不在wheel内。全新venv从本地wheelhouse离线安装，以`-I`运行，确认无Qt/QA/柜台SDK；既有策略、保护、调度、报表与费用登记冒烟，以及新模板保存/覆盖/应用/删除、CSV往返、别名及CLI冒烟通过。未发布或部署该开发制品。

实际Qt离屏渲染见[基准界面](../output/previews/20261110T014048Z-652ecf3acf-parameters/baseline.png)、[品种与模板预览](../output/previews/20261110T014048Z-652ecf3acf-parameters/symbols-and-template.png)、[坏行整批拒绝](../output/previews/20261110T014048Z-652ecf3acf-parameters/rejected-batch.png)、[差异页](../output/previews/20261110T014048Z-652ecf3acf-parameters/differences.png)。截图、样例CSV与本地模板历史使用匿名QA数据；现有Windows字体只在QA进程中加载。全部75份产品文件与最终检查及当前源码一致，见[哈希核对](../output/previews/20261110T014048Z-652ecf3acf-parameters/final-source-match.json)和[视觉检查](../output/previews/20261110T014048Z-652ecf3acf-parameters/visual-review.json)。这是实际Qt布局证据，不代替已安装交互式桌面、在线或耐久验收。

dev10原73份产品文件已按810项检查的源码哈希逐字节归档于 `output/baselines/20261110T012519Z-27157e22/dev10-product.zip`，SHA-256 `6b253450e1ebc09bb8b864bd59b5177d69c600b58d9407df143ba20329a89990`，旧检查/制品保留。

本轮未完成全参数表单编辑、Excel工作簿导入、偏移计算器、帮助/通知、真实合约目录和在线适配器，因此不宣称整个G4或全功能覆盖完成。V7 rc.41、原G0/计划1.0/1.1/1.2和DSV确认资源均保持原哈希；不连接柜台。
