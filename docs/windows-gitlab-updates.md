# Windows 安装版与 GitLab 更新

安装者从 Windows 开始菜单启动 OnlineTrader。工作台后台检查预览渠道，「软件更新 → 检查更新」也可手动触发。下载可中断并续传，通过完整 ZIP 校验后在下次启动时尝试安装。运行中的安装版进程或账户账本里的未结事实会推迟切换。失败时可继续打开上一版，早期启动失败的版本会标记为失败以避免循环。自启动守护通过同一个固定启动入口。

安装根目录保存 `active.json`、`update-source.json` 和安装版共用的 `OnlineTrader.ini`。程序及配套工具位于 `versions/<版本>` 下。账户目录、账本和外部参数文件沿用原路径。旧便携版偏好仍在便携目录，可在首次安装后、运行前将旧 `OnlineTrader.ini` 复制到安装根目录。手工启动的外部 CLI、自定义账户目录或其他独立执行环境应先正常停止再启动安装版。

## 制品和发布

1. 分发项目为 [Yoruto/OnlineTrader-Releases](https://gitlab.com/Yoruto/onlinetrader-releases)，项目 ID 为 `89047383`，默认分支为 `main`。首发前确认 Package Registry 可匿名读取，并核对 Generic Package 重复文件设置。固定同一版本对应的构建目录、ZIP 和 SHA256。
2. 从同一个 `scripts/build-portable.ps1` 输出目录生成两种制品。首次安装使用 `release/installer.iss`，提供 `AppVersion` 和 `BundleDir`，安装器默认使用项目 `89047383`，可通过 `GitLabProjectId` 指定试验项目。更新使用 `OnlineTrader-<版本>-windows-x64.zip`，将输出目录内容放在 ZIP 根部。
3. 使用 `python scripts/publish_gitlab_update.py --version <版本> --archive <ZIP>` 检查本地制品。实际发布时增传 `--publish --project-id 89047383 --ref main`，发布端通过环境变量提供 `GITLAB_TOKEN`。脚本将 ZIP 上传到 `online-trader-windows-x64`，匿名回读文件大小、摘要及完整 ZIP 后，才创建 `onlinetrader-<版本>` Release。首个带更新器的安装版与用于验收的下一版应使用不同版本号。

上线验证使用干净 Windows 和两个实包，核对任务守护、Qt 可见窗口、实际执行文件路径、已有账户数据及回退。源码样本可以验证 GitLab 请求契约与切换控制，实包路径仍需在获准出包后验证。
