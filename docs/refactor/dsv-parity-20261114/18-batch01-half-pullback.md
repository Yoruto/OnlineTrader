# 批次 01｜半仓第二档回落保护

基线：`426ac0f9ca38dbff8b2dc54f3ed70a245ac8eb38`。只修改替代链 `dsv_core.transition.protect`，不切换桌面、不删除仍在用的旧 Kernel、不连接账户。

## 原版依据与修改

冻结 DSV `trading.exe` SHA-256：`aabeafd83d265ab9b1cf5cc3fd0e9b0f21963ddcc95daa70feefc209997269a3`。

上传审查证据中的 `gui.decide_half_close` 编译对象行表 1847–1879：第一档完成、剩余至少两手且第二档启用时，上涨达到第二档，或启用 `pullback_close` 后回落到第一档（含等号），均触发第二次半仓；阶段至少为 2 时不重复。`_run` 7621–7623 实际传入两个开关。这里的行号是原包编译对象调试信息，不是假称取得了 gui.py 明文。

本批仅补回落分支，保留 `PARTIAL_STAGE_2` 事件、原剩余目标、半偶舍入及既有保护顺序。缺少可选 `half_pullback_close` 仍按 False 解释；配置导入的一次性迁移不改动。

## 本次实际验证

Linux / CPython 3.13.5。使用从固定提交读取、Git blob SHA 校验一致的定向源码及依赖文件；不是完整 checkout。测试直接导入产品 `protect()`，不使用源码摘录执行或产品函数替身。

```sh
PYTHONPATH=src python -m unittest discover -s tests -p 'test_dsv_half_pullback_parity.py' -v
```

同一组 19 个方法：未修复基线 10 FAIL / 9 PASS，候选 19 PASS / 0 FAIL / 0 SKIP。

覆盖：固定四手反例、阈值等号、上下阈值间隙、第二档上涨、开关关闭/缺省、完成阶段、一手及奇数手、空头、在途减仓共用目标、重复观察、输入不变、保护优先级和同轮止损归零。

未运行：原版 EXE、冻结策略/SDK 串联完整回放（含第 7/9 轮）、整仓回归、Windows Qt/IPC、柜台及制品。不能把本批通过数量写成全量对齐或发布验收。合并前在完整 Windows 环境执行相邻 `test_dsv_round_replay`、`test_dsv_refactor_repairs`、调度回放和整仓测试。

## 剩余范围

普通止盈停开、配置兼容、月停跨日、持久异常诊断、统一入口和旧链退役分别处理。本批不扩大历史授权，也不改变 UNKNOWN、订单占量或逐笔成交核对。
