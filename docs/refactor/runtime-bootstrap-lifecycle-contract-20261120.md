# Runtime Desktop Bootstrap 与生命周期合同

> 状态：PR-D 编码合同（2026-11-20）。每台 Windows 电脑只有一个 Standalone Runtime，并且只有一个 Workbench 附着它。本改动不改变 Owner、账本或默认 `DISARMED` 语义。

## 1:1 桌面合同

- `OnlineTrader.exe` 先取得本机唯一 Workbench 锁；第二个 EXE 只能通过同一 Windows 用户的 `SHOW_WORKBENCH` 通道恢复/前置现有窗口。该通道不接受交易命令。
- Runtime 持有原本的全机互斥锁，并发布脱敏发现记录。发现记录只是提示，Desktop 必须用其中的 pipe 与 account key 取得可归属 baseline 后才能附着；残留文件、PID 或账号显示都不是所有权证明。
- 已有 Runtime、没有 Workbench：打开唯一 Workbench 并附着该 Runtime，不再启动 Owner。
- 没有 Runtime：按原正常流程启动一个 `DISARMED` Owner，baseline 成功后打开唯一 Workbench。
- `--runtime-pipe` 仍是高级附着入口，但同样遵守 Workbench 单实例；Standalone 路径不接受 `--allow-multi`。

## 状态机

```text
DISCOVERING
  -> ATTACHING
       -> READY
       -> LAUNCHING
            -> WAITING_BASELINE
                 -> READY
                 -> FAILED
                 -> CANCELLED_UI
```

- `DISCOVERING`：在后台读取 portable 配置、所选账户、原账本绑定和 control 配置。
- `ATTACHING`：探测既有 Runtime baseline；成功时直接附着，不启动进程。
- `LAUNCHING`：仅在既有 Runtime 不可附着时启动一个 `runtime run` 子进程；新 Owner 仍由原账号锁裁决并保持 `DISARMED`。
- `WAITING_BASELINE`：观察结构化 phase、启动失败和原 Owner baseline。并发启动者若得到 `E_INSTANCE_EXISTS`，继续等待赢得账号锁的原 Owner，不启动第三个进程。
- `READY`：baseline 的 account key 与所选账户一致，才交给同一个 `WorkbenchWindow`。
- `FAILED`：配置、账本、进程、结构化启动失败或等待超时；不自动重启、不 kill 可能仍在启动的 Owner。
- `CANCELLED_UI`：用户/应用退出只中断本地探测和等待；已经启动的 Owner 独立存活，订单与保护责任不转移。

## 线程与进程所有权

Qt 主线程只创建/更新进度窗口、打开 Workbench 和显示最终错误。配置读取、pipe 探测、进程启动、phase 文件读取和 200ms 轮询全部在 `BootstrapWorker` 中执行；不再调用 `QApplication.processEvents()` 或在主线程 `sleep()`。

`BootstrapController` 拥有 worker 的 Qt 生命周期，但不拥有 Runtime Owner 的业务生命周期。worker 退出、进度窗关闭或 Workbench 关闭都不得发送 stop 或终止已启动 Owner。

## 正常与故障时序

### 附着既有 Owner

```text
Desktop -> worker: start
worker -> Runtime discovery: read bounded same-user hint
worker -> runtime-view: verify hinted pipe + account baseline
runtime-view --> worker: matching account baseline
worker --> Desktop: READY(pipe)
Desktop -> WorkbenchWindow: open existing Runtime
```

### 缺失 Owner 后启动

```text
worker -> runtime-view: no endpoint
worker -> Runtime process: launch runtime run (DISARMED)
worker -> phase files/runtime-view: bounded wait
Runtime Owner -> account lock + original ledger: acquire/recover
runtime-view --> worker: matching baseline
worker --> Desktop: READY(pipe)
```

### 启动竞争和 UI 取消

`E_INSTANCE_EXISTS` 只证明另一个 Owner/启动者赢得互斥；worker 反复读取发现记录并验证 baseline 到主 deadline。进度框提供“取消等待”；UI 取消请求 worker interruption，禁止重复取消，worker 返回 `CANCELLED_UI`；不对 subprocess 调用 terminate/kill。关闭进度框和应用退出走同一 interruption/等待边界。

## 诊断合同

阶段变化包含 state、用户可读消息、账户别名和脱敏登录。Runtime 发现记录只含实例 ID、PID、pipe、account key、别名、脱敏登录与 Runtime 根路径，不含凭据；退出时按实例 ID 条件删除，崩溃残留由下一次握手判为无效。失败保留错误码和 stderr/phase 路径。超时明确说明 Owner 可能仍在继续，禁止用户用自动重试制造第二个 Owner 或第二个 Workbench。

## 回退入口与验收边界

若统一 Desktop bootstrap 无法使用，运维仍可显式运行 `runtime run` / `runtime schedule`，再用 `--runtime-pipe` 附着同一 Workbench。没有本次测试授权，因此窗口响应、并发双击、慢启动、取消和真实 pipe 场景均为 `NOT_RUN`；只执行源码编译检查。

