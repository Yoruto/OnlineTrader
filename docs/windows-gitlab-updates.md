# Windows 安装版、便携版与 GitLab 更新

安装版从 Windows 开始菜单启动。带更新配置的便携版直接双击解压目录根部的 `OnlineTrader.exe`。两种版本都会在工作台打开时后台检查预览渠道，也可以点「软件更新 → 检查更新」主动检查和下载。

下载完成并校验完整 ZIP 后，可从同一菜单选择「更新并重启」并确认，也可以继续使用当前版本，下次正常打开时再切换。更新要求相关进程退出，账户账本中的持仓、委托及未知结果已核对。条件不足时暂缓切换，继续使用当前版本。新版启动失败时回退上一版，并标记失败版本，避免启动时反复尝试。

安装根目录保存 `active.json`、`update-source.json` 和安装版共用的 `OnlineTrader.ini`。程序及配套工具位于 `versions/<版本>` 下。账户目录、账本和外部参数文件沿用原路径。旧便携版偏好仍在便携目录，可在首次安装后、运行前将旧 `OnlineTrader.ini` 复制到安装根目录。手工启动的外部 CLI、自定义账户目录或其他独立执行环境应先正常停止再启动安装版。

便携版需要解压到可写目录。根目录里的原 EXE 保持为启动入口，下载的版本解包到 `versions/<版本>`，根目录的偏好、参数及随包配置继续使用。便携版自启动守护也从根目录启动。原来缺少更新配置的便携版需要先换用一次带更新配置的完整包。

## 制品和发布

1. 分发项目为 [Yoruto/OnlineTrader-Releases](https://gitlab.com/Yoruto/onlinetrader-releases)，项目 ID 为 `89047383`，默认分支为 `main`。首发前确认 Package Registry 可匿名读取，并核对 Generic Package 重复文件设置。固定同一版本对应的构建目录、ZIP 和 SHA256。
2. 公开交付使用 `scripts/build-portable.ps1 -DeliveryMode Generic`。可用 `-Archive` 生成已校验的根目录布局 ZIP，文件名为 `OnlineTrader-<版本>-windows-x64.zip`。首次安装使用 `release/installer.iss` 并提供 `AppVersion` 和 `BundleDir`。安装器默认使用项目 `89047383`，可通过 `GitLabProjectId` 指定试验项目。
3. 使用 `python scripts/publish_gitlab_update.py --version <版本> --archive <ZIP>` 检查本地制品。实际发布时增传 `--publish --project-id 89047383 --ref main`，发布端通过环境变量提供 `GITLAB_TOKEN`。脚本将 ZIP 上传到 `online-trader-windows-x64`，匿名回读文件大小、摘要及完整 ZIP 后，才创建 `onlinetrader-<版本>` Release。首个带更新器的安装版与用于验收的下一版应使用不同版本号。

上线验证使用干净 Windows 和两个实包，核对任务守护、Qt 可见窗口、实际执行文件路径、已有账户数据及回退。源码样本可以验证 GitLab 请求契约与切换控制，实包路径仍需在获准出包后验证。

## 指定设备的配置版

使用 `-DeliveryMode Configured -DeliveryConfigDirectory <配置目录>`。打包要求目录中有 `OnlineTrader-defaults.json`，且包含 SimNow 和快期行情的账号口令。可同时提供 `OnlineTrader-parameters.json`。构建前检查输入，构建后核对随包文件，传入 `-Archive` 时同时校验 ZIP 的文件列表、CRC 与关键成员摘要。配置版 ZIP 和安装器名称都以 `-Configured` 标识。公开更新入口拒绝包含默认账号配置的 ZIP。

普通双击配置版工作台首次启动时登记默认账号。已有档案时只为匹配身份补齐缺失的凭据，保留已保存的口令和当前账号选择。配置版含可读取的凭据，应存放和交付在获准的范围内。
