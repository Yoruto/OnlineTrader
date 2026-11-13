# DSV参数登记、修订与只读历史

本页保留dev3参数阶段结果。dev12的全参数编辑、偏移计算及最新检查见[编辑与计算合同](parameter-editing-offset-contract.md)，dev11模板和品种CSV见[参数工作流合同](parameter-workspace-contract.md)，dev4策略准入见[策略合同](strategy-contract.md)；参数草稿本身仍不会切换执行配置。

日期：2026-11-09。实现：`kernel/configuration.py`。本阶段把用户已确认的DSV参考基准登记到独立产品账本，并保存参数草稿；不连接柜台，不给参数登记附带开仓权。

版本：`0.1.0.dev3`。最终独立检查 **233项通过，0失败/错误/跳过**，见[确切报告](../output/checks/20261109T083852Z-bcbc3935/result.json)及[逐项日志](../output/checks/20261109T083852Z-bcbc3935/check.log)。编译、G0资源哈希及产品导入边界检查通过，检查期间源码未变化；开发环境pip check无依赖冲突。

新增45项包含配置域18、账本12、Qt参数中心7、CLI/文件工作流8；原188项同时通过。参数域和CLI覆盖非法输入、凭据过滤、零值/关闭值保留、大数精度、导出失败无半文件；新窗口通过Qt专项并留有原生Windows截图。本轮不宣称完整产品桌面、在线交易或长稳验收。

开发制品为 `online_trader-0.1.0.dev3-py3-none-any.whl`，SHA-256 `c81767b2d55348ad51fce24b326daf00b04917ffe27060d1dd63bdec2470a876`。[构建审计报告](../output/artifacts/20261109T083947Z-f3be4dcbf492/wheel-audit.json)核对43份Python源码及4份资源与最终检查、wheel、全新安装内容逐字节一致；本地wheelhouse离线安装及无Qt/柜台SDK的核心基准查看、参考登记、只读历史通过。登记后authority仍为DISARMED/config1。旧候选与旧检查证据保留；本段链接才是本轮最终制品。

## 业务确认与执行配置分开

`configuration.baseline.load_confirmed_baseline()`提供经过资源完整性检查的参数与参考批准manifest。首次登记在一个`TX_CONFIG`中写入：

- 独立DSV参考`rule_sets`记录。
- B01–B20共20项`APPROVED`决定，保留“以DSV为准”的确认文字、来源证据hash和未取证语义说明。
- `approval_manifests`及20个对应item。
- 一份`config_revisions`，`effective_boundary=NON_TRADING`、`runtime_activation=false`。

原G1工程种子和历史批准不覆盖、不改写。DSV参考批准只确认来源/保存配置；不把未核实算法写成已验证，不推定其可用于在线执行。

`authority.config_revision`仍指向原执行配置，已有周期、保护计划和权限均不被参数登记替换。保存后默认内核仍禁开；自动启动、保底一手、零值、未启用阈值和白银待配置行都按配置域原样保存。

## Owner接口

`Kernel.configurations`由唯一业务所有者持有；Catalog不另开SQLite writer。

| 接口 | 合同 |
| --- | --- |
| `register_confirmed_baseline()` | 返回Future。首次原子登记参考和配置；在途重复调用合并同Future，已登记后返回原基线的持久回执，不覆盖后来草稿 |
| `save(profile, expected_revision)` | 要求已登记参考、无未接受事务，expected_revision精确匹配当前catalog版本；保存新NON_TRADING修订，返回Future |
| `get(revision=None)` | 返回指定/最新owner已接受版本的安全深拷贝；没有任何catalog版本时默认返回None，指定未知版本拒绝 |
| `list_revisions()` | 返回owner已接受的修订概要深拷贝 |
| `current_revision` | 无catalog时为0；实际config_revision与全账本共享，首次通常为2，不能假设从1开始 |
| `poll()` | 由Kernel提交处理末尾调用；存储或Ack失败时完成在途失败Future，不发布成功 |

`get()`包含config_revision、rule_set_id、profile_hash、confirmation_status、matches_confirmed_baseline、reference_approval_id、saved_event_seq、settings_record、summary、diagnostics及runtime_activation。`settings_record`使用OT-CJ-1兼容Decimal字符串；显示/DSV数值导出由配置域负责。

改值不会继承参考批准：与已确认基线hash不同则为`UNCONFIRMED_DRAFT`。恢复原样基线可显示`CONFIRMED_DSV_REFERENCE`，也仍是NON_TRADING。与当前修订完全相同的保存幂等返回原回执；expected_revision检查先于该幂等判断，不吞掉过期修改。

写入只有正确boot、tx_id、event/durable序号、state_revision与COMMITTED outcome均匹配的owner Ack后，才发布内存版本和COMMITTED回执。事务失败时20项决定、manifest及配置整体回滚；错误Ack不完成成功Future。COMMITTED仅表示该参数/参考记录已提交，不表示交易授权、成交或DSV行为已验证。

重启从账本恢复版本、原参考和最新草稿，检查保存内容hash、profile hash、reference范围与全部20项批准引用；不因重启重新登记或替换基线。

## 只读历史

`read_catalog_file(path, account_key)`用SQLite URI `mode=ro`和一致读事务读取已提交catalog，返回与get相同的安全entry列表。它不构造Kernel、不获得执行权、不创建数据库、不产生TX_BOOT、不撤权、不改变authority；错误账户为E_SCOPE，不存在或不兼容的文件明确拒绝。

只读历史读取SQLite已COMMIT事实，不代表当前在线所有者已接受某Ack，也不是账户交易活动投影。没有catalog记录时返回空列表；工程种子不冒充DSV参数修订。

## 本地验收

`tests/test_configuration_catalog.py`的12个用例覆盖同事务登记、Ack前不可见、重复幂等、零值/关闭值原样、草稿批准边界、revision冲突无写、相同值幂等、重启恢复、整笔回滚、错Ack不成功、只读历史不增boot/不改authority、错账户/不存在文件及owner限制。测试使用独立临时账本，不连接柜台。

配置域严格解析、凭据过滤、Decimal编码及Qt/CLI展示由相应模块测试分别验收。当前尚未实现将这些参考参数切换为真实执行配置，也没有新增IPC消息。

## 参数域与桌面入口

`configuration.dsv.DsvProfile`只接收trade/auto_offset/schedule/symbols四个业务节。显式导入原DSV文件时过滤账户、通知、使用协议身份和UI等节；没有默认读取DSV目录或凭据的后台动作。重复键、NaN/Infinity、布尔冒充数值、负业务参数和缺失/不支持字段明确拒绝。DSV原文件中被过滤的UI负坐标不影响导入。

内部记录使用规范Decimal字符串，hash基于OT-CJ-1；`to_json()`使用精确数字JSON导出。CLI show输出settings_record，Qt表格和差异也使用精确记录，避免大数或多位小数经float显示而舍入。`to_settings()`是原生数字兼容边界，不用于账本或精度敏感导出。

DSV基准随产品打包为数据资源，不含旧程序、PDF、凭据、QA或个人目录；资源校验绑定原配置hash、程序hash和用户确认evidence_hash。默认基线保留保底一手=true、触发更新=false、实盘保护=false、5品种和白银0/0/0。0值不推断为关闭/自动/无限，未启用2手/5000元不显示为已生效硬限。

独立参数窗口：

```powershell
E:\online-trader\.venv\Scripts\python.exe -m online_trader parameters desktop
```

状态台也有“DSV参数”按钮。参数窗口支持基准查看、原DSV或安全业务JSON导入预览、逐字段差异、只含业务值的新文件导出及恢复基准预览。失败导入保留原预览；导出已有文件时拒绝覆盖，以免覆盖原DSV的账户配置。原生窗口截图见[参数中心](../output/ui/20261109T083255Z-parameters-native-7597ab21.png)。当前未提供全字段表单编辑或模板管理；参数修改可先在独立安全JSON中完成再预览/保存草稿。

## 命令行工作流

查看基准、预览导入与导出新文件不创建Kernel：

```powershell
E:\online-trader\.venv\Scripts\python.exe -m online_trader parameters show
E:\online-trader\.venv\Scripts\python.exe -m online_trader parameters diff --input C:\Users\xyc\Desktop\trading\config.json --dsv
E:\online-trader\.venv\Scripts\python.exe -m online_trader parameters export --output E:\online-trader\output\dsv-business-parameters.json
```

`--dsv`明确表示从原DSV文件提取白名单业务节；不带它时输入必须是安全业务JSON。导出只写新文件，已有目标需另选文件名。`show/diff/history`输出可在终端核对，不包含原DSV账户或通知节。

登记基准会在指定**独立开发数据目录**创建或打开内核账本，并受本机单所有者排他约束。它不连接柜台；已有内核运行时不会竞争打开第二个写入者：

```powershell
E:\online-trader\.venv\Scripts\python.exe -m online_trader parameters register --data-dir E:\online-trader\output\parameters-demo --account-key DEV:parameters-demo
E:\online-trader\.venv\Scripts\python.exe -m online_trader parameters history --data-dir E:\online-trader\output\parameters-demo --account-key DEV:parameters-demo
```

改值草稿使用`parameters save --input <安全JSON> --data-dir <同目录> --account-key <同标识> --expected-revision <当前config_revision>`，可加`--dsv`显式读取原配置。请使用register/history实际返回的修订号，不能假定从1开始。校验输入失败发生在打开writer之前；版本冲突拒绝覆盖旧修订。保存同值幂等返回原版本，改值只产生UNCONFIRMED_DRAFT。

## 本轮覆盖与后续

本轮推进W02批准/参数映射、W07配置基础及W10参数预览，覆盖C02/C07的离线子范围；不把这些子范围计为G3/G4整阶段完成。DSV几何/K线/首分钟/滑点分母等执行语义继续取证，现有记录不会替它们填猜测值。正式策略入场、参数应用到活动执行计划、账户连接、完整操作与报表仍是后续工作。
