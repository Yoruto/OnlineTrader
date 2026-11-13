# 独立品种编辑窗口

此页记录已归档版本的交付与验收。当前便携版见[项目首页](../../../README.md)。

2026-11-12，OnlineTrader 0.1.0.dev28。

原主控的「添加品种」打开整个业务参数编辑器并插入空行。编辑器初始高度在缩放后的桌面上可能超出可用区域，底部确认和取消不易找到。本次改为独立单品种表单。

- 主控「添加品种」「编辑选中」和双击行进入独立表单。合约简称或完整代码可自动补全品种名称，名称仍可修改。
- 常用偏移和手数直接填写。自动/固定手数明确区分；半仓比例按百分数填写，与原配置中的 0～1 值转换。滞回点和候选换月合约在更多设置中。
- 确认和取消固定在滚动内容外。窗口依据可用屏幕定位；错误在窗口内提示并定位字段。取消或关闭窗口不产生空行或部分修改。
- 高级参数编辑器的新增、编辑复用该表单；子窗口确认仅改变外层草稿，外层取消仍放弃全部草稿。外层窗口同时适配可用屏幕，底部操作保持可见。
- 主控确认后显示反馈并选中新行或已编辑行；保存预览参数显示文件名。参数 JSON 继续使用另存为新文件的约定。

开发自检覆盖独立表单、取消、原子更新、原参数保留、界面布局、保存回读及参数/CSV/模板兼容，共 193 项通过；编译和 diff 检查通过。这些结果属于开发自检，独立桌面验收另外记录。

本轮证据根目录：[独立验收及制品证据](../../../output/portable-acceptance/20261112-symbol-editor)。生产变更的冻结哈希记录在 `source-candidate-hashes.json`，开发自检记录在 `developer-checks.json`。

独立测试 Agent 使用全局 `independent-testing` Skill，从实际 Windows 界面执行 S01–S15，源码阶段 15 项全部通过。包含普通及缩小窗口的固定底栏、有效新增、无效/重复输入、编辑与取消、文件保存与读回、外层草稿取消/应用、既有批量修改和 64 行上限；五个变更生产文件在验收前后哈希一致。[覆盖清单](../../../output/portable-acceptance/20261112-symbol-editor/qa/coverage-checklist.md) · [源码阶段报告](../../../output/portable-acceptance/20261112-symbol-editor/qa/source-test-report.md)。

本轮桌面验收使用空账号目录和本地参数文件，结论限于本次参数编辑改动和当前 Windows 环境。账户连接、策略启动和交易流程不属于本轮验收。

源码阶段通过后生成 SymbolEditor 便携 ZIP。独立测试 Agent 从该 ZIP 的实际解压目录启动 EXE，P01a–f 六项全部通过：启动身份、添加与错误恢复、选中编辑、双击及新增取消、保存取消及文件回读、高级入口与固定底栏。两个隔离测试实例均已关闭。[最终独立测试报告](../../../output/portable-acceptance/20261112-symbol-editor/qa/final-test-report.md) · [实包窗口截图](../../../output/portable-acceptance/20261112-symbol-editor/qa/package/01-add-clean-1.png)。

制品 SHA-256：`67f77698fe6cc2e70838624fc01140b9019cde6dfb562e89b020f8bf0465e6ea`。ZIP CRC、1152 个构建与解压文件的 SHA-256 比对通过；GUI 与 CLI 的 116 个产品模块均与当前源码一致，资源一致，未混入账户、账本或 QA 文件。[制品汇总](../../../output/portable-acceptance/20261112-symbol-editor/acceptance-result.json)。
