# Independent OnlineTrader

Work in `E:\online-trader`. Read `AGENTS.md` and `README.md` for current scope. For DSV parity, use `docs/refactor/dsv-parity-20261114/README.md` and its current replay notes. Keep the V7, WT9, OnlineTrader and reference DSV runtimes separate.

- For OnlineTrader source commits, confirm the fetch and push targets are `https://github.com/Yoruto/OnlineTrader.git`. Fetch the branch refs before deciding what needs merging. Include relevant source, tests, documentation and release scripts. Runtime logs, accounts, ledgers, credentials and generated packages stay in local evidence or their authorized delivery location.

## Inputs and authorization

- Use the product's pinned SDK environment for TqKq/SimNow connections. Keep credentials in the current user's vault and bind the provider and account identity before connecting. Use the original kernel owner for product execution and a fresh, finite scope for the authorized actions.
- Check the Beijing session, cabinet trading day, exact contract identities, profile, calendar and clock for a new observation. A prior readiness package or successful login is historical after its window ends. Apply the current request's connection and order permissions.
- A calculator preview, DSV saved parameters, DSV UI readback and the product's actual bound startup profile are separate facts. Compare the correct pair. Preserve the real daily rows, day cutoff and intraday range to reproduce an offset calculation. Re-read the saved configuration before comparing runs, including startup switches.

## Frozen DSV parity

Use the frozen archive and hashes in `qa/dsv_parity/fixtures/baseline-manifest.json`. Run the verified original strategy code and relevant SDK methods in isolated QA. Use the Python version required by the archive for bytecode and the project's environment for OnlineTrader. Supply the same business settings, ordered update batches, wall time, native positions and fills. Keep the original SDK network boundary local.

- Start with `docs/refactor/dsv-parity-20261114/15-strategy-sdk-schedule.md` for the commands. `compare_original_loop` covers strategy decisions and state. `sdk_round_replay` covers the order tasks. `scheduled_sdk_replay` connects both and traces `wait_update`, strategy targets, API calls and outbound packets. Locate the first differing round before changing the candidate. Per-round API invocation and packet emission can occur in different rounds.
- Confirm which native facts are proved. A current-session SDK local order can support cancellation by its bound identity after the original call returns. It does not establish acceptance, fill, terminal state or reconnect reconciliation. Preserve original quantity reservations and per-child commit-before-send evidence. Compare native flat and strategy close synchronization separately.
- Follow user-approved differences in `00-evidence-and-decisions.md`. The slow partial-fill regression uses `--verify-slow-partial` on the scheduled fixture. Round 7 keeps the five-lot target with three proved fills and only prepares the two-lot remainder. Round 9 retains five proved lots without an extra close. The guard checks the original divergence and exact round order. Keep unrelated mismatches visible.
- For a changed behavior, supply the version and actual entry point to the independent testing agent. Cover the failing input, its corrected behavior, adjacent execution and restart paths. Use the Qt interface when accepting a displayed result. An offline outbound queue proves local scheduling for those inputs. Network timing, live cabinet response and shipped binaries require their own evidence.

### Exit recovery and workbench acceptance

Use `docs/refactor/dsv-parity-20261114/16-exit-recovery-and-evidence-acceptance.md` for the fixtures, commands and expected observations.

- For a rejected reduction, first replay invalid then restored quotes through frozen DSV. In the candidate, inject a proved unsent reduction and separately an ambiguous SDK-call failure. Check that opening the same ledger and receiving a fresh, valid quote resumes the original exit revision with a new child after position and route checks. A repeated quote or a limit-band fallback alone cannot trigger recovery. An unresolved `UNKNOWN` keeps its original reservation and awaits facts.
- For history reads, measure the first snapshot and the first snapshot after reopen with a populated ledger. While the history loader still runs, act in the real Qt workbench and verify an owner command or round and its receipt can advance. Inspect the read-only worker boundary. Local results are observations, not fixed performance thresholds.
- For event display, drive actual Qt through the pipe. Check representative no-trade reasons and a cancellation-call exception. Verify visible time, contract/child identity, error code and sanitized exception type. Confirm the original order remains reserved, and reopen an older ledger to check the retained fills and missing historical fields.
- For comparison changes, mutate a valid trace to remove the final zero from `[0, 5, 0]`, and add, remove, duplicate or reorder rounds. Each must fail. Adjacent repeated zero targets remain valid. Run the comparator tests and all fixed scheduled samples, including the approved difference in slow partial-fill rounds 7 and 9.

## Startup and online evidence

- Historical replay restores observation state with entry disabled. The original tick can age while replay runs. After committing the path, wait briefly for continuous incremental ticks and register a fresh quote through the existing gate. Extend only from the accepted sequence in the same session and source epoch. Preserve the original observation start and leave unproved gaps blocked.
- For a no-order product test, keep entry disarmed, instrument and deny native order APIs, and collect actual startup, account and market decisions. Record attempt counts and the independent read-only final snapshot after the owner exits. A successful connection or comparison on a pure strategy replay covers less than the actual runtime.
- Align two providers by source tick identity and timestamp. Record the fields and decision outputs compared. Compare live DSV only when its original running strategy is observed under the user's authorization. Source-formula verification covers that formula. An observed geometry block covers the rejected path. Natural entry, fills, cancellation, and post-fill protection have their own evidence.

## Checks and documentation

Use targeted `unittest` files in the project environment. For a Windows full source run use the `-X utf8` invocation in `AGENTS.md`. `scripts/check.ps1` records source and validation-input hashes. For the mid-session startup path, use `test_online_dsv*.py` and `test_auto_offsets_runtime.py`.

- For workbench parity, estimate suggested lots from known inputs and keep it separate from execution sizing. Display intervals from the bound calendar for the accepted cabinet day. Report a proved terminal close shortfall against the original target and outstanding reservations without creating a new order.
- For explicit Windows portable delivery, use `scripts/build-portable.ps1` and verify the extracted directory as delivered. Keep the main and CLI executables with their shared `_internal` and adjacent scripts. For tray acceptance, verify close hides the window, a real tray double-click restores the same window and process, and the tray menu exits that process. Record the archive SHA-256 and distinguish extracted-build acceptance from later source checks. The watchdog is optional and only opens the workbench. Its activation, broker access and packaging follow the current request.

Update the current section of `docs/功能实现与待办清单_20261110.md` and route readers from the README and refactor index. Preserve dated evidence and expired scopes as historical records. Maintain this reference in the OnlineTrader repo at `docs/skills/online-trader.md`, the parent V7 skill snapshot and the installed skill under `references/online-trader.md`.
