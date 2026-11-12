# Independent OnlineTrader

Work in `E:\online-trader`. Its `README.md` points to the current 96-item list and dated evidence. Use `AGENTS.md` and the relevant product contract for constraints. The parent V7 documentation keeps the historical design and skill source. Keep the V7, WT9, OnlineTrader and original DSV runtimes separate.

- For OnlineTrader source commits, confirm the fetch and push targets are `https://github.com/Yoruto/OnlineTrader.git`. Fetch the branch refs before deciding what needs merging. Include relevant source, tests, documentation and release scripts. Runtime logs, accounts, ledgers, credentials and generated packages stay in local evidence or their authorized delivery location.

## Inputs and authorization

- Use the product's pinned SDK environment for TqKq/SimNow connections. Keep credentials in the current user's vault and bind the provider and account identity before connecting. Use the original kernel owner for product execution and a fresh, finite scope for the authorized actions.
- Check the Beijing session, cabinet trading day, exact contract identities, profile, calendar and clock for a new observation. A prior readiness package or successful login is historical after its window ends. Apply the current request's connection and order permissions.
- A calculator preview, DSV saved parameters, DSV UI readback and the product's actual bound startup profile are separate facts. Compare the correct pair. Preserve the real daily rows, day cutoff and intraday range to reproduce an offset calculation. Re-read the saved configuration before comparing runs, including startup switches.

## Startup and online evidence

- Historical replay restores observation state with entry disabled. The original tick can age while replay runs. After committing the path, wait briefly for continuous incremental ticks and register a fresh quote through the existing gate. Extend only from the accepted sequence in the same session and source epoch. Preserve the original observation start and leave unproved gaps blocked.
- For a no-order product test, keep entry disarmed, instrument and deny native order APIs, and collect actual startup, account and market decisions. Record attempt counts and the independent read-only final snapshot after the owner exits. A successful connection or comparison on a pure strategy replay covers less than the actual runtime.
- Align two providers by source tick identity and timestamp. Record the fields and decision outputs compared. Compare live DSV only when its original running strategy is observed under the user's authorization. Source-formula verification covers that formula. An observed geometry block covers the rejected path. Natural entry, fills, cancellation, and post-fill protection have their own evidence.

## Checks and documentation

Use targeted `unittest` files in the independent core environment and `scripts/check.ps1` for full source-only validation. Its result records source and validation-input hashes, which can establish whether an earlier run still applies to a documentation-only change. Keep SDK-backed runs in their separate environment. For the mid-session startup path, use `test_online_dsv*.py` and `test_auto_offsets_runtime.py`.

- For DSV parity work, verify the original behavior in the installed reference archive or existing evidence and trace the OnlineTrader feature from its normal entry point. Replay the same market and account facts when available. Check entry, risk, order/cancel, post-fill protection and recovery separately. Check whether a DSV control has a functional handler before treating the control as a missing feature.
- For workbench parity, estimate suggested lots from known inputs and keep it separate from execution sizing. Display intervals from the bound calendar for the accepted cabinet day. Report a proved terminal close shortfall against the original target and outstanding reservations without creating a new order.
- For explicit Windows portable delivery, use `scripts/build-portable.ps1` and verify the extracted directory as delivered. Keep the main and CLI executables with their shared `_internal` and adjacent scripts. For tray acceptance, verify close hides the window, a real tray double-click restores the same window and process, and the tray menu exits that process. Record the archive SHA-256 and distinguish extracted-build acceptance from later source checks. The watchdog is optional and only opens the workbench. Its activation, broker access and packaging follow the current request.

Update current state in `docs/功能实现与待办清单_20261110.md` and link the dated run from `README.md`. Distinguish offline injected facts, live observation, real order/fill evidence and installed-build acceptance. Retain older reports and expired scopes as evidence. When changing this reference, sync the installed skill and the OnlineTrader repository copy at `docs/skills/online-trader.md`.
