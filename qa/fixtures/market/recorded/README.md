# 实盘录制离线回放夹具 (`qa/fixtures/market/recorded/`)

本目录保存由桌面实盘录制原始 JSONL 数据转换而成的只读 SQLite 行情回放夹具，格式遵循 `MARKET_REPLAY_FIXTURE/1`（不含任何账户凭据、委托、成交或账本数据）。

## 数据集说明

| 交易日 | 目录 | 覆盖范围 (`coverage_scope`) | 合约 | Tick 数 | K 线数 | SQLite 大小 | `database_sha256` |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| `2026-11-25` | `2026-11-25/` | `FULL_TRADING_DAY` | `CZCE.OI701`, `DCE.l2701`, `DCE.p2701`, `SHFE.ag2612`, `SHFE.ag2702`, `SHFE.bu2701` | `292,276` | `6,216` | `65.07 MiB` (`68,235,264 B`) | `7ad218c3bd8eea12df7ded0fbb695c78ce1c8c451c2d1fd4436cb39cfa9a4914` |
| `2026-11-26` | `2026-11-26/` | `PARTIAL_LIVE_CAPTURE` | `CZCE.OI701`, `DCE.l2701`, `DCE.p2701`, `SHFE.ag2612`, `SHFE.ag2702`, `SHFE.bu2701` | `147,936` | `4,713` | `33.64 MiB` (`35,274,752 B`) | `53c29e04f298bd44a6ac6a6897ad8fc48c8d72d047ff28890e6ab24510bb5f0d` |

> [!NOTE]
> - 桌面 `data/2026-11-24/` 仅包含 `quotes.jsonl` 与 `ticks.jsonl`（共 `12,000` 条尾盘 Tick），缺少 `meta.jsonl` 元数据，且内容与根目录现有的 `2026-11-24` 午盘尾段夹具重叠，故未重复导入。
> - `2026-11-26` 数据录制至采集时刻为止，未覆盖全天收盘，因此标记为 `PARTIAL_LIVE_CAPTURE`。
> - `*.quotes.jsonl` 为行情快照流，不属于 `MARKET_REPLAY_FIXTURE/1` 表结构，未导入 SQLite（已在各日 `manifest.json` 中记录输入文件校验信息与 `known_gaps`）。

## 重建与校验命令

从原始录制目录重新转换生成：

```powershell
$env:PYTHONPATH="src"
.\.venv\Scripts\python.exe -X utf8 qa\market_data\import_recording.py --source-dir "$env:USERPROFILE\Desktop\data" --trading-day 2026-11-25 2026-11-26
```

校验只读数据库完整性、SHA-256、Tick 对齐与盘口覆盖率：

```powershell
.\.venv\Scripts\python.exe -X utf8 qa\market_data\validate.py qa\fixtures\market\recorded\2026-11-25\market_fixture.sqlite3 --manifest qa\fixtures\market\recorded\2026-11-25\manifest.json
.\.venv\Scripts\python.exe -X utf8 qa\market_data\validate.py qa\fixtures\market\recorded\2026-11-26\market_fixture.sqlite3 --manifest qa\fixtures\market\recorded\2026-11-26\manifest.json
```
