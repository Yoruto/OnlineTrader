# 打包、发布与受管更新

## 1. 交付物类型与随包配置规范

OnlineTrader 以包含 `EXE`、`_internal`、必需脚本与配置的**完整交付单元**出包（停用缺少配置的通用空包）：

1. **首次/全量私有交付（`RuntimeConfigured`）**
   - 构建入口：`scripts/build-portable.ps1 -DeliveryMode RuntimeConfigured -DeliveryConfigDirectory <confirmed-config>`，产出 `*-Runtime-Configured.zip`。
   - 必须随包带齐已确认的 `runtime.json`、`OnlineTrader-defaults.json`、`OnlineTrader-parameters.json`，以及受控更新配置（可通过 `-UpdateHost` / `-UpdateProjectId` / `-UpdateChannel` / `-UpdateReadTokenFile` 注入）。
   - 含账户口令的制品只通过获准的私有渠道交付。
   - **测试用临时实包**：尚未获准正式出包但需验证真实 EXE 时，使用 `scripts/build-test-portable.ps1` 生成显式标记 `TEST-ONLY` 的目录（不生成发布 ZIP）。
2. **不可独立启动的程序更新载荷（`ONLINE_TRADER_UPDATE_PAYLOAD/1`）**
   - 构建入口：`scripts/build-update-payload.ps1`（或 `scripts/portable_delivery.py --update-payload`），产出 `OnlineTrader-<版本>-windows-x64.zip`。
   - 根内仅含程序本体与 `update-payload.json`（`standalone: false, requires_managed_root: true`），**严禁包含** `runtime/`、`OnlineTrader-defaults.json`、`OnlineTrader-parameters.json`、`OnlineTrader-overrides.json`、`update-source.json`、`active.json`、`OnlineTrader.ini`。
   - 除安装期 `_offline_check`（`--verify-runtime` / `--update-smoke`）外，单独解压直接启动一律 fail-closed 拒绝运行。

### 白银合约配置规则
正式交付的安装器、便携包和更新 ZIP 允许随包配置白银（包括白银主连及上期所 `ag` 具体合约月），适用与其它品种相同的配置来源确认、参数有效性、设备适配及最终制品核对要求；不得仅因存在白银配置而停止交付，也不得未经确认擅自增删或替换白银合约。

---

## 2. 发布与受管更新流程

- **发布源**：支持 `gitlab.com` 与中国大陆受控极狐 `jihulab.com`。使用 [publish_gitlab_update.py](file:///e:/online-trader/scripts/publish_gitlab_update.py) 校验载荷、上传资产、按客户端同款逻辑回读大小与 SHA-256 后创建 Release。`update-source.json` 中仅允许可选只读 `read_token`，严禁携带写权限令牌或跨域名跳转透传。
- **安装根目录布局**：安装根维护 `active.json`、`update-source.json`、`OnlineTrader.ini` 与稳定的 `runtime/`；程序本体解压在 `versions/<版本>/`。
- **更新状态与安全切换**：
  - Workbench 后台线程可检查并下载更新包到 `updates/`，但**绝不**为了更新自动停止策略、杀 Owner、撤单或平仓。
  - 用户确认「安全更新并重启」后，由稳定启动器在安装独占锁、Runtime 单实例锁与账号锁内确认相关进程退出，执行离线检查并原子切换 `active.json`；若新版在就绪期限内失败，自动回退 `previous` 版本。
  - 更新器**绝不**扫描、解释或迁移旧执行账本，亦不改动根目录既有 `runtime/` 配置。

---

## 3. 交付前强制核对清单

1. **测试确认**：每次获准打包前先询问用户是否运行该包全量测试；用户批准则跑全量测试并独立验收最终制品，用户拒绝则明确标注“未测试”并列出省略项。
2. **制品实检**：对 ZIP 检查实际归档列表与成员摘要，对安装器在隔离目录安装验证；核对配置文件路径、有效内容及与已确认输入的一致性；使用用户实际启动入口（双击 `OnlineTrader.exe`）验证配置可读，并另核 `runtime run` 与 `--runtime-pipe` 运维入口。
3. **交付记录**：写明版本号、制品 `SHA-256` 与配置验证结果（省略口令等秘密）。任一检查失败则停止交付。
