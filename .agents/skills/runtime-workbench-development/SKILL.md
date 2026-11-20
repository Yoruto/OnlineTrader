---
name: runtime-workbench-development
description: Develop or review OnlineTrader Standalone Runtime, desktop bootstrap, IPC, configuration, history cache, Runtime client, CommandBus, or Qt Workbench changes. Excludes explicit Legacy and DSV Replacement work.
---

# Runtime and Workbench Development

Use the Standalone Trading Runtime unless the user explicitly selects another execution path. Start with the repository `AGENTS.md`, then read the relevant current entries in [architecture status](../../../docs/ai/architecture-status.yaml) and the [canonical OnlineTrader guidance](../../../docs/skills/online-trader.md).

## Current boundaries

- One Windows host has at most one Runtime. Other computers may use the same futures account.
- Each boot owns a temporary execution ledger and synchronizes current positions, orders, trades and funds from the broker.
- `account-history.db` is a broker-rebuildable cache. It is not a sender, position authority or execution handoff.
- The Workbench is a read/control client. Its CommandBus keeps pending correlation only for the current process; Runtime revalidates every command.
- Qt and runtime-view failures must not create another SDK sender or transfer execution ownership.

Use [module boundaries](../../../docs/ai/module-boundaries.yaml) to locate the initial source set. For state, persistence, recovery, IPC or command work, read the current owning contracts named by `architecture-status.yaml`; do not implement from a `SUPERSEDED` scope.

## Workflow

1. Identify the actual entry point and first differing fact on the Standalone route.
2. Name the owner of each affected state and the process allowed to write it.
3. For cross-module behavior, review normal flow, failure flow, restart, compatibility, rollback and diagnostic evidence before editing.
4. Preserve correlated diagnostics for decisions, external calls, exact failed predicates and follow-on responsibility without exposing credentials.
5. After routine changes, run only the repository compilation check. Run tests, broker connections or packaging only when the current request explicitly authorizes them.

If current source conflicts with a current contract, report `CONFLICT / NEEDS_REVIEW` and resolve the owning design before expanding the change. Order and send safety remain mandatory through `AGENTS.md`; they are not optional Skill behavior.
