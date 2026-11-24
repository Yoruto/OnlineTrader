# OnlineTrader 测试与便携交付流水线 V2 实施规范

> 日期：2026-11-23  
> 审查基准：`main · a4c61df`（替代 [V1 方案](testing-and-portable-delivery-pipeline-optimization-20261120.md)）  
> 状态：**CANDIDATE / 第 1 批已在工作树实现（仅通过编译检查，测试与构建尚未授权运行）**  
> 适用范围：Standalone Trading Runtime 的离线测试门禁、Windows x64 便携程序冻结、Base Bundle 复用、配置组装与发行验收  
> 不改变：交易业务逻辑、Workbench/Runtime 权限边界、`onedir` 双 EXE 布局、Runtime 配置结构、SQLite 合同及 GitLab 更新协议

## 1. 核心原则

**可以复用构建结果（Base Bundle），但不能复用未经重新确认的可信结论。**

1. 程序主体未变时允许跳过重复的 PyInstaller 冻结；但是否满足最新测试门槛、配置快照是否有效、能否安全启动 Runtime、最终 ZIP 是否完整，必须各自持有独立且可核验的证据。
2. 正式出包仍必须执行 [AGENTS.md](../../AGENTS.md) 规定的全局测试与独立最终制品验收；当前 [qa/test_runner.py](../../qa/test_runner.py) 的 `Affected` 影响面映射尚未证明可靠，**现阶段正式出包一律 fail-closed 执行 `FullOffline`**，禁止因 Base Bundle 缓存命中而缩减必测范围。
3. 本轮 1–7 批严格收敛在构建脚本、预检工具、QA 身份一致性与对应测试中；不修改交易执行、Runtime 启动协议、自动更新器、`installer.iss` Bootstrap 结构或 Qt/TqSdk 依赖集合。

## 2. 架构约束与已确认修订（P0 / P1 / P2）

### 2.1 P0-1：三段式构建身份（Build Identity）

禁止仅按源码目录与少数包版本号计算 `binary_input_hash`。持久化 Base Bundle 的构建身份必须由以下三部分共同决定：

```text
Build Identity
├── Source Identity
│   ├── 参与冻结的源码（src/online_trader/）
│   ├── release/entry.py 与 release/OnlineTrader.spec
│   ├── pyproject.toml（版本写入 update-source.json）
│   ├── release/portable/* 与 release/runtime-portable/* 通用文件
│   └── 其他参与冻结的资源清单（遵循 .spec 排除规则）
│
├── Toolchain Identity
│   ├── Python 精确版本、patch 与构建标识及解释器二进制身份
│   ├── 专用不可变构建环境的已安装发行版完整清单（含 pyinstaller、hooks、PySide6、tqsdk 及全部传递依赖）
│   └── 目标平台架构（windows-x64）与构建配置
│
└── Build Contract
    ├── 构建与冻结脚本版本（freeze-portable.ps1 / portable_delivery.py）
    ├── 打包规则版本
    └── 输出目录布局版本
```

**Build Env 校验策略**：

- 当前仓库 `requirements-*.lock` 未覆盖 `pyinstaller` 与 `tqsdk` 及其传递依赖；因此不能仅依赖锁文件声明。
- 建立专用不可变 Build Env 时做一次全量环境检查并记录完整环境身份；日常构建只快速核验环境身份与关键输入摘要，避免每次全量哈希整个 `.venv` 造成高昂 I/O 开销；若环境身份异常则重建环境，不复用旧缓存。

### 2.2 P0-2：三套身份分离与发行资格记录

必须严格区分三类身份，禁止用“二进制未变”自动继承“测试通过”或“允许发布”：

| 身份类型 | 语义 | 能否复用 / 继承 |
| --- | --- | --- |
| **Binary Identity** | `Build Identity` 对应的程序主体是否相同 | 可跨账户配置复用 |
| **Test Evidence Identity** | 某固定候选树在某测试集下得到的证据 | 仅当 `candidate_tree_sha256`、`manifest_sha256`、`test_asset_sha256` 与测试环境完全匹配时有效；影响测试/执行/打包资产变更时 fail-closed 重跑 `FullOffline` |
| **Release Identity** | 特定候选 + Base + 具体账户配置快照 + 最终 ZIP 的发行批准 | 不得跨配置或跨候选自动继承 |

建立独立的 `release-qualification.json`（不写入 `base-build-manifest.json`），显式绑定：
- `candidate_tree_sha256`（由预检与 QA 共用同一规范算法）；
- `base_build_identity` 与 `base_bundle_tree_sha256`；
- 预构建 `FullOffline` 结果与摘要；
- 目标配置快照哈希；
- 最终制品 SHA-256、离线双探针及独立验收结论。

### 2.3 P0-3：Base Bundle 完整性、可信度与原子发布

`output/release-base/<build_identity>/` 必须满足以下工程约束：

1. **区分完整性与可信度**：Base Bundle 内的全文件 `{relpath: sha256}` 清单与 `base_bundle_tree_sha256` 负责**完整性验证**（检测意外损坏或文件变化）；构建来源与 `release-qualification.json` 负责**流程可信度与发行批准**。单机环境下普通 JSON 清单不作为防本地恶意篡改保证（未来如需本地防篡改需另行引入代码签名或受保护元数据）。
2. **目录外锁与原子发布**：构建锁放在目标目录之外（如 `output/release-base/<build_identity>.lock`）；先在同卷临时目录执行 `ReleaseClean` 构建 -> 运行离线双探针（`OnlineTraderCLI.exe --verify-runtime` 与 `OnlineTrader.exe --update-smoke`）-> 生成全文件 `{relpath: sha256}` 清单与只读 `base-build-manifest.json` -> 验证完整后原子发布到最终缓存目录。构建失败不得留下半成品。
3. **命中重验与强制重建**：缓存命中时必须重新扫描并全量重算 `base_bundle_tree_sha256` 校验完整性，支持强制重建开关，损坏时明确失败或安全重建。

### 2.4 P1-1：Base Manifest 与 Delivery Manifest 彻底解耦

- `base-build-manifest.json` 在 Base 封印后设为只读历史证据，**严禁再由组装或发布脚本就地修改**。
- 禁止直接把持久化 Base Bundle 目录传给会就地修改 manifest 或执行 `bundle.rename(output)` 的旧组装路径。
- Stage 2 必须将已验证的 Base Bundle **只读复制到独立短生命周期组装目录**，写入配置快照与独立的 `delivery-manifest.json`，再校验、打包、验收。

### 2.5 P1-2：不可变源码快照与 Editable 回流隔离

- 禁止在仍可能被工作进程或 Agent 修改的活跃工作树上分时执行“预检 -> 测试 -> PyInstaller”。
- 正式候选必须先冻结为不可变源码快照，对快照计算 `candidate_tree_sha256`，测试、预检与 PyInstaller 均以该同一快照为输入。
- **隔离关键点**：项目脚本使用 `python -I` 隔离模式（会忽略 `PYTHONPATH`，但**仍会读取 `.venv\Lib\site-packages\__editable__.*.pth`**）。因此**不能只靠设置 `PYTHONPATH=<snapshot>\src` 实现隔离**；必须在无工作树 editable `.pth` 的隔离构建/测试环境中运行（或在快照环境中安装目标源码），并在入口与 PyInstaller `Analysis` 阶段校验实际导入路径与收集路径均位于快照内、未回流主工作树。

### 2.6 P1-3：配置快照固定与真实产品解析

- 预检阶段即对配置目录完成完整产品级校验（`PortableAccountStore`、`DsvProfile.from_dsv_json`、`OnlineTrader-overrides.json` 合并、全部品种含白银的配置有效性、禁止/遗留文件排除与大小限制）。白银主连及上期所 `ag` 具体合约月不再作为阻断条件，按其他品种的相同规则校验。
- 组装前先将已确认配置固定到私有短生命周期快照并计算全量 `config_hashes`；后续组装、目录校验和 ZIP 成员校验全部仅读取该快照并交叉比对哈希，结束后通过 `try/finally` 清理含凭据的临时目录。

### 2.7 P1-4 与 P2：明确延后边界

- **P1-4（安装器双份 `_internal` 与 Bootstrap）**：[release/installer.iss](../../release/installer.iss) 当前依赖 `{app}\bootstrap\` 与 `{app}\versions\<AppVersion>\` 配合 [update_install.py](../../src/online_trader/update_install.py) 的 `managed_bootstrap`；最小化 Stable Bootstrap 作为第 8 批独立评估改造，不与本轮缓存优化耦合。
- **P2（QtPdf / TqSdk 瘦身 / ZIP 校验裁剪）**：保留 `PySide6.QtPdf` 与 `tqsdk` 完整依赖，保留 `build_archive` 的完整 CRC + 成员清单 + 关键 SHA-256 校验。

## 3. 目标两阶段流水线

```text
Stage 0: 候选与配置固定
  开发工作树 -> 冻结不可变源码快照 -> 固定配置快照
  -> release_preflight（统一 candidate_tree_sha256 + 真实 PortableAccountStore/DsvProfile 解析）
  -> 独立测试（FullOffline，绑定同一 candidate_tree_sha256）

Stage 1: 生成并密封不可变 Base Bundle（命中则跳过 PyInstaller）
  Build Identity -> 加目录外锁 -> 命中时重算全文件清单校验完整性
  -> 未命中则在同卷临时目录执行 ReleaseClean 冻结
  -> 运行双 EXE 离线探针（--verify-runtime + --update-smoke）
  -> 写入全文件清单与只读 base-build-manifest.json -> 原子发布至 output/release-base/<build_identity>/

Stage 2: 独立配置组装与交付
  从不可变 Base Bundle 复制到短生命周期组装目录
  -> 写入已固定配置快照与独立 delivery-manifest.json
  -> verify-bundle 交叉核对配置快照哈希与禁止项
  -> 生成 *-Configured.zip 并执行完整 ZIP 校验 -> 清理秘密临时目录
  -> 更新 release-qualification.json 并执行独立最终制品验收
```

## 4. 八批次实施路线

| 批次 | 内容 | 优先级 | 状态 |
| --- | --- | --- | --- |
| 1 | 统一 Preflight 与 QA 的 `candidate_tree_sha256` 规范算法；`release_preflight.py` 补齐真实 `PortableAccountStore` / `DsvProfile` 配置校验 | P0 | 已在工作树实现（待独立测试） |
| 2 | 建立不可变源码快照与无工作树 Editable 回流隔离（禁止仅靠 `PYTHONPATH`，验证实际导入与 PyInstaller 收集路径） | P0 | 待实施 |
| 3 | 拆分 `base-build-manifest.json`、`delivery-manifest.json` 与 `release-qualification.json`；解除 `portable_delivery.py` 对 Base 的原地修改与重命名 | P0 | 待实施 |
| 4 | 实现 `ReleaseClean` 基础包目录外锁、同卷临时构建、全文件清单完整性重算校验与原子发布 | P0 | 待实施 |
| 5 | 实现配置快照固定、多账户配置复用同一 Base 的隔离组装与 `finally` 凭据清理 | P1 | 待实施 |
| 6 | 补齐双 EXE 离线探针（`--verify-runtime` + `--update-smoke`）与“同版本号不同二进制内容”阻断门禁及对应验收用例 | P1 | 待实施 |
| 7 | 固化专用不可变 Build Env（首次全量指纹 + 增量启动快检 + 异常重建）与结构化性能报告 | P1 | 待实施 |
| 8 | 独立评估 Qt/TqSdk 精简可行性与最小化 Stable Bootstrap 设计 | P2 | 独立评估 |

## 5. 验收门槛矩阵（开放正式 Base 缓存前必须通过）

1. **同源码连续正式打包两次**：第二次跳过 PyInstaller 冻结，全量文件完整性重算通过后复用同一 Base。
2. **同源码换不同模拟账户配置**：PyInstaller 调用次数为 0；两份最终 ZIP 的 `delivery-manifest.json`、`config_hashes` 与账号配置严格隔离、无交叉污染。
3. **修改交易执行源码 / 构建 Hook / 间接依赖**：立即使旧 `Build Identity` 失效，触发干净重建。
4. **修改测试用例或门禁脚本但产品源码未变**：允许识别二进制身份未变，但必须阻断旧 `Test Evidence Identity`，按规则重新执行 `FullOffline`。
5. **同版本号不同二进制内容**：`Build Identity` 不同时禁止错误复用旧 Base，并阻断同版本不同内容静默混装。
6. **Base Bundle 文件被篡改或缺失**：缓存命中阶段的全量完整性重算立即拒绝复用。
7. **并发构建同一 `Build Identity`**：目录外锁阻塞竞争，不读取半成品、不相互覆盖。
8. **构建期原工作树发生修改**：不可变快照下的预检、测试与冻结结果不受污染。
9. **ZIP 校验失败**：不产出可交付状态，失败后秘密临时目录清理干净。
10. **新版本启动或更新失败**：现有安装与账户数据保留并可正常回退。
