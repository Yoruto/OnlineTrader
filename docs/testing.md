# 测试与验收方法

## 1. 日常开发检查（默认唯一允许）

日常修改源码后，**仅运行全仓字节码编译检查**（除非当前任务已获明确测试授权）：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m compileall -q src tests qa
```

---

## 2. 离线仿真与回归 Profile

在获得明确测试授权后，可通过 [scripts/test.ps1](file:///e:/online-trader/scripts/test.ps1) 或 [qa/test_runner.py](file:///e:/online-trader/qa/test_runner.py) 按风险选择离线验证集（每次运行在 `output/tests/<run_id>/evidence-index.json` 生成 `ONLINE_TRADER_EVIDENCE_BUNDLE/1` 证据索引）：

| Profile | 覆盖范围 | 说明 |
| --- | --- | --- |
| `Unit` | 全部 `T1` 纯逻辑与合同单测 | 无工作台、无子进程、零 PySide6 依赖 |
| `SimAll` | 全部无头 Fake 行情/持仓仿真 | 含 `TradingE2E`、`FaultE2E`、`timeline_001`、`recovery`、`StatefulE2E` |
| `SmokeE2E` / `TradingE2E` / `TradingExtended` | 独立 QA Owner 子进程 + `FakeBroker` + `FakeMarket` + `VirtualClock` + `ReferenceOracle` | 离线端到端交易链路仿真 |
| `DesktopE2E` | 真实 Qt 工作台面板 + Runtime/`FakeBroker` | 校验 UI 操作与后端状态一致性 |
| `FaultE2E` | 断流（`QUIET`/`FEED_SUSPECTED_STALLED`）、重连同步、SDK 泵阻塞、命令幂等/冲突 | 故障注入与诊断 JSONL 核验 |
| `StatefulE2E` | 确定性种子状态机探索、轨迹回放与 Oracle 负向控制 | 标准库实现，无三方依赖 |
| `ReleaseGate` | `FullOffline` 单测 + `SimAll` + `DesktopE2E` | 离线全量门禁，不打包、不连真实账户 |
| `LivePreflight` | 默认 fail-closed 返回 `NOT_RUN` | 仅显式传 `--authorize-live` 时做静态就绪核对 |

完整源码回归命令（需授权）：

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
```

---

## 3. 测试注册表与静态架构守卫

- 新增、重命名或删除 `unittest` 用例时，必须同步更新 [qa/test_manifest.json](file:///e:/online-trader/qa/test_manifest.json) 并运行 `.\.venv\Scripts\python.exe -X utf8 qa\test_registry.py` 校验一致性。
- [qa/check.py](file:///e:/online-trader/qa/check.py) 负责检查模块依赖边界（如 `dsv_core` 纯净性）以及 [spec-source.json](file:///e:/online-trader/docs/spec-source.json) / `docs/spec-extension-*.json` 与 `src/online_trader/resources/` 之间的 SHA-256 一致性。

---

## 4. 独立验收与报告规范

- **范围隔离**：离线仿真、连接模拟盘（TqKq/SimNow）、Windows 实包验收必须分别报告，**严禁将离线通过混写为真实柜台或最终制品通过**。
- **验收声明要素**：每项验收结论必须绑定完整候选 Git `SHA`、实际运行入口与场景/fixture、运行环境，以及分列的 `PASS / FAIL / SKIP / NOT_RUN` 计数；未取得原件的本地报告只能记为 `REPORTED`，不可当作本次独立核验结论。
