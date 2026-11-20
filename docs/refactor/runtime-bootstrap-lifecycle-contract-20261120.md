# Runtime Desktop Bootstrap 与生命周期合同

> 状态：PR-D 编码合同（2026-11-20）。本改动只移动桌面等待方式，不改变 Owner、账本、账号锁或默认 `DISARMED` 语义。

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
worker -> portable config: resolve account + pipe
worker -> runtime-view: read baseline
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

`E_INSTANCE_EXISTS` 只证明另一个 Owner/启动者赢得互斥；worker 等待它的 baseline 到主 deadline。进度框提供“取消等待”；UI 取消请求 worker interruption，禁止重复取消，worker 返回 `CANCELLED_UI`；不对 subprocess 调用 terminate/kill。关闭进度框和应用退出走同一 interruption/等待边界。

## 诊断合同

阶段变化包含 state、用户可读消息、账户别名和脱敏登录。失败保留错误码和 stderr/phase 路径；不记录凭据。超时明确说明 Owner 可能仍在继续，禁止用户用自动重试制造第二个 Owner。

## 回退入口与验收边界

若统一 Desktop bootstrap 无法使用，运维仍可显式运行 `runtime run` / `runtime schedule`，再用 `--runtime-pipe` 附着同一 Workbench。没有本次测试授权，因此窗口响应、并发双击、慢启动、取消和真实 pipe 场景均为 `NOT_RUN`；只执行源码编译检查。

