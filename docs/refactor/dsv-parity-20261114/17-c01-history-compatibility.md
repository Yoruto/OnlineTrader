# C01｜旧事件的只读投影兼容

基线：`ddad472235b9fa0749646d7e3fded831040ca6c3`。本修复只处理工作台历史投影，不改变策略、订单状态、发送/撤单、持仓、账户授权或数据库 schema。

## 旧格式与证据边界

`aa8bd35` 的派发器写入 `EFFECT_RESULT` 时只保存 `id` 与 `result`。旧 `UNKNOWN` 没有 `child_order_id`，原后台读取器会在投影时失败，且不推进水位。旧记录不是必然损坏数据，也不能因为没有后加字段就被认为未发送。

新投影使用事件的完整 `id`，只读查询同库 `effects` 的已提交请求。请求 ID、表中的 child/kind 与请求中的身份必须一致；已有事件字段与请求冲突时，不覆盖或猜测。只有可证明的子单、动作类型、合约关联用于展示。**不解析 ID 后缀推断身份，不用当前 effects.result 改写旧事件结果，不用当前时间补造历史发生时间。**

## 降级与继续读取

| 输入 | 展示与游标行为 |
| --- | --- |
| 旧 UNKNOWN + 一致的原请求 | 仍展示 UNKNOWN；从原请求补全缺失身份；缺时间仍为空 |
| 旧 UNKNOWN + 找不到原请求 | 保留已知 UNKNOWN，未知身份留空；添加带原事件序号的降级诊断；继续后续记录 |
| 旧事件与请求身份冲突 | 不展示伪造关联；标记 `E_HISTORY_EFFECT_CONFLICT`；继续后续记录 |
| 不可解析的辅助 ROUND/EFFECT_RESULT | 标记 `E_HISTORY_EVENT_FORMAT`；不输出载荷或异常原文；继续后续记录 |
| 临时数据库锁或 SQLite 查询故障 | `UNAVAILABLE`；失败事件水位不推进，后续刷新重试 |
| 已有子单字段但缺时间/细分原因 | 不补造时间或原错误码，保留兼容展示 |

有未完整投影的事件时显示 `DEGRADED`，它不是交易权限。诊断单独有界保存20条，策略变化100条、报撤失败50条、逐笔100条的既有边界保留。ROUND 先验证并构造完整投影，再更新去重状态，避免坏记录的半条结果重复出现。单条降级只跳过展示，不删除源事件或修改 UNKNOWN 占量。

本修复不承诺读取损坏的 SQLite 文件、不绕过不支持的 schema，也不对真实 native_trades 自动修复。旧事实原样保留；需要修正交易事实时仍走单独的原生核对流程。

## 可复现回归

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p 'test_dsv_workbench_history*.py' -v
.\.venv\Scripts\python.exe -X utf8 -m compileall -q src tests qa
```

`test_dsv_workbench_history_compat.py` 使用实际 Journal、SQLite 和后台线程，不创建 SDK、账户锁或交易连接。覆盖：旧 SEND/CANCEL UNKNOWN、原请求缺失、身份冲突、坏辅助事件、原始表内容不变、后续追加/逐笔、重开、短暂锁、查询失败的可重试性、有界诊断和只读连接。

`test_dsv_workbench_history_qt.py` 在 Windows 环境检查缺身份不误称为发送、实际 Qt 窗口显示降级提示和事件序号。该测试用本地客户端替身，**不是命名管道端到端或柜台验收**。

## 本次执行记录

Linux / CPython 3.13.5；读取器及完整导入依赖从基线按 Git blob SHA 核验。候选通过正常 Python 导入运行，不以函数摘录或 SDK 替身替代读取器。当前工作区仅有定向测试所需的源码依赖，不是完整项目 checkout。

- 相同旧 UNKNOWN 用例在未修复基线失败：`UNAVAILABLE`、水位0；候选通过。
- 新增17个测试方法：**15 PASS，2 Windows Qt SKIP**；这些数字不包含基线反例。
- 所改 Python 文件及所取测试依赖语法编译通过；未跑完整仓库回归、Windows Qt/IPC、DSV 对跑或真实 SDK。
- 合并前仍需在完整 Windows 环境执行上述测试及相邻工作台回归，不把本页当作发布验收。
