# 批次05｜在线控制接口与离线回放解耦

基于PR #7分支 `fix/dsv-batch03-runtime-diagnostics-20261115` 的 `ecb5575217c1e2f3bae71e0d6b14669187400f00`。这是叠加PR：先审查/合并#7，再重新核对本分支基线；不重复撤销#7诊断改动。

## 范围

新增中立 `dsv_runtime.control_endpoint`，在线 `sdk_startup` 不再导入 `offline_endpoint`。抽出原快照投影、命令/回执处理和串行请求交换，删除离线模块中的重复实现。仅做这段依赖拆分，不称旧Kernel、QA打包或桌面入口已全部退役。

生产传输验证器只接受snapshot/command/receipt/shutdown及返回方向的reply/error；服务器派发再次排除reply/error，直接控制处理器也拒绝round/disconnect。离线模块保留DEV回放和内部旧op/body辅助调用；网络包仍须完整校验，绑定SDK的旧辅助调用仍拒绝注入。

原在线处理器此前已在sdk存在时拒绝round/disconnect，不能将本批描述为修复了已证实的在线任意报单漏洞。本次把边界从离线模块中抽离，并提前到传输验证及独立派发层。

暂保留 `DSV_OFFLINE/1` 通信标签，兼容现有客户端；标签不授予回放权限。没有配置项允许在线切换到回放操作集。正常快照仍委托原控制器的terminal规则，不重写UNKNOWN或数量语义。

命令顺序保持scope核对→旧回执→新命令的交易日核对→单一owner执行。shutdown仍由SDK所有者检查同步、停止、空仓及无未决责任，通用模块不关闭SDK。普通KeyError/TypeError只回通用协议消息，不泄漏异常键文本。账户授权和冷迁移要求不改。

## 验证

Linux / CPython 3.13.5，定向源码，非完整checkout：

- 22个控制接口方法PASS：真实校验器和codec、传输/SDK/session显式替身；含绕过传输校验后的二次拒绝、旧wire兼容、幂等顺序、超大返回、异常、独立导入防护。
- 9个包装/owner-loop方法PASS：真实offline包装、实际 `_serve` 和临时状态文件，SDK/session/管道为显式替身。本机13个未调用启动依赖使用调用即失败的导入占位；没有实际连接、会话构造或Windows管道。
- 在叠加#7后的候选上重跑其17个真实文件诊断测试与6个循环组件测试：23 PASS，异常持久化/恢复和日志降级仍保留。
- 本批及组合源码compileall PASS。另将#6的runner改动和批次04组合后做定向检查；不等于前三批全部回归或整仓验证。

完整Windows环境复现入口：

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p 'test_dsv_control_endpoint*.py' -v
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p 'test_dsv_runtime_diagnostics*.py' -v
```

正常完整依赖导入、Qt/IPC端到端、真实SDK、柜台、冷迁移与最终制品均NOT_RUN。合并前需要现有离线工作台/在线启动相邻回归及整仓检查。保留草稿；不合并main、不登录账户、不报撤单、不改变系统时间。
