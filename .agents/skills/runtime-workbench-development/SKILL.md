---
name: runtime-workbench-development
description: Develop, document, debug, or review OnlineTrader Standalone Runtime and Workbench flows, including desktop bootstrap, lifecycle, IPC, configuration, history cache, Runtime client, CommandBus, and Qt integration. Excludes explicit Legacy and DSV Replacement work.
---

# Runtime and Workbench Development

Use the Standalone Trading Runtime unless the user explicitly selects another execution path. Start with the repository `AGENTS.md`, then read the relevant current entries in [architecture status](../../../docs/ai/architecture-status.yaml), the [current Workbench contract](../../../docs/refactor/runtime-workbench-current-20261120.md), and the [canonical OnlineTrader guidance](../../../docs/skills/online-trader.md).

## Current boundaries

- One Windows host has at most one Runtime. Other computers may use the same futures account.
- One host has at most one attached Workbench. Re-running the desktop entry wakes the existing Workbench.
- Each boot owns a temporary execution ledger and synchronizes current positions, orders, trades and funds from the broker.
- `account-history.db` is a broker-rebuildable cache. It is not a sender, position authority or execution handoff.
- The Workbench is a read/control client. Its CommandBus keeps pending correlation only for the current process; Runtime revalidates every command.
- Qt and runtime-view failures must not create another SDK sender or transfer execution ownership.
- Trading controls are manual-only through the same interactive Windows user's Workbench channel. CLI/API/delegated tools are read-only; legacy schedule, operator grant, watchdog and handoff files are ignored.

Use [module boundaries](../../../docs/ai/module-boundaries.yaml) to locate the initial source set. For state, persistence, recovery, IPC or command work, read the current owning contracts named by `architecture-status.yaml`; do not implement from a `SUPERSEDED` scope.

## Workflow

1. Identify the user entry point and the first differing fact on the Standalone route. Do not fall back to Legacy when Runtime configuration is missing.
2. Name the authority, writer, lifetime and rebuild source for every affected state. Distinguish broker facts, the current boot's temporary execution store, rebuildable history cache, configuration and process-local UI state.
3. For cross-module behavior, review normal flow, duplicate launch, disconnect/unknown result, Workbench or runtime-view loss, Owner restart, another host, compatibility, rollback and diagnostic evidence before editing.
4. Keep commands bound to account, Runtime boot, configuration revision and the original `command_id`. A timeout is not permission to resend; a new process does not inherit old pending responsibility.
5. Preserve correlated diagnostics for decisions, external calls, exact failed predicates and follow-on responsibility without exposing credentials or account plaintext.
6. Update the current contract owner and navigation when guidance changes. Keep dated evidence as historical and never relabel old results as current validation.
7. After routine changes, run only the repository compilation check. Run tests, broker connections or packaging only when the current request explicitly authorizes them.

If current source conflicts with a current contract, report `CONFLICT / NEEDS_REVIEW` and resolve the owning design before expanding the change. Order and send safety remain mandatory through `AGENTS.md`; they are not optional Skill behavior.
