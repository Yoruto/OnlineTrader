# QA Market Replay Fixture (`MARKET_REPLAY_FIXTURE/1`)

本目录保存用于离线回放的真实市场行情 SQLite 只读夹具及对应外部校验清单（对应 GitHub Issue #27）。所有数据均在隔离环境中采集，不含任何账户凭据、Token、用户委托、成交或账本数据。

## 文件说明

- `market_fixture.sqlite3`：只读 SQLite 行情数据集（含 `dataset_metadata`、`instruments`、`trading_sessions`、`market_ticks`、`market_klines` 五张表）。
- `manifest.json`：外部校验清单，记录 `dataset_id`、合约范围、`coverage_scope`、数据库 SHA-256、每合约每侧每档（L1–L5）盘口覆盖率统计、各交易时段行数与边界时间戳。
- `dce_p2701_entry_stop_loss.jsonl`：原有人工构造短烟雾场景夹具（保留兼容）。
- `recorded/`：由实盘录制 JSONL 转换的多日 6 合约回放夹具目录（含 `2026-11-25` 全天与 `2026-11-26` 部分录制，详见 `recorded/README.md`）。

## 当前制品范围与诚实边界

- **数据源**：TqSdk `3.10.5` 普通账号（`tq_dl = False`），通过 `get_tick_serial` + `get_kline_serial` + `get_quote` 采集，未连接任何交易账户，未调用报撤单接口。
- **当前版本 (`2026-11-24`)**：`coverage_scope = PARTIAL_DAY_AFTERNOON_TAIL`。因普通账号无历史下载接口权限，`get_tick_serial` 单合约上限为最近 `10,000` 条 Tick，当前涵盖 `2026-11-24` 午盘尾段（约 `13:30–15:00 CST`，四合约共 `40,000` 条 Tick）及 `2,000` 根 1 分钟 K 线 / `30` 根日线。
- **盘口深度**：`SHFE.bu2701` 为真实五档（`FIVE_LEVEL`）；`DCE.p2701`、`DCE.l2701`、`CZCE.OI701` 为真实一档（`ONE_LEVEL_ONLY`），买二至买五/卖二至卖五已按规范置为 `NULL`。

## 校验与重建命令

校验只读数据库完整性、SHA-256 与盘口覆盖率：

```powershell
.\.venv\Scripts\python.exe -X utf8 qa\market_data\validate.py qa\fixtures\market\market_fixture.sqlite3 --manifest qa\fixtures\market\manifest.json
```

使用普通账号重新拉取最近序列：

```powershell
.\.venv\Scripts\python.exe -X utf8 qa\market_data\pull_tqsdk.py --trading-day 2026-11-24
```

使用 QA-only 实时录制工具在完整交易日（夜盘开盘前至日盘收盘后）录制全天 Tick：

```powershell
.\.venv\Scripts\python.exe -X utf8 qa\market_data\record_live.py --trading-day 2026-11-25 --night-date 2026-11-24
```
