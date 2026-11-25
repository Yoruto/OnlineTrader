---
name: dsv-parity-review
description: Develop or review OnlineTrader DSV parity, Replacement Candidate, ExplicitOrderController, DsvSdkAdapter, Journal, or isolated frozen-DSV replay work. Excludes ordinary Standalone Runtime and Legacy maintenance.
---

# DSV Parity and Replacement Review

Start with the repository `AGENTS.md`, [strategy.md](../../../docs/strategy.md), and [dsv-confirmation-20261109.json](../../../docs/specs/dsv-confirmation-20261109.json). Read [testing.md](../../../docs/testing.md) when preparing isolated replay or parity verification.

## Current boundaries

- Replacement uses `DsvSession`, `ExplicitOrderController`, `Journal` and `DsvSdkAdapter`; it does not share the Legacy Kernel sender.
- Feed both implementations the same configuration, ticks, position snapshots and ordered native facts. Trace the first differing round through `wait_update`, strategy target, SDK API call and outbound packet.
- Keep `online_trader.dsv_core` free of SDK, UI, I/O, system clock and Legacy-owner dependencies.
- Preserve per-child commit-before-send evidence. An exception from inside native `insert_order` is not proof that nothing was sent.
- QA fixtures and injected facts remain isolated; they cannot become an online production input.

## Workflow

1. Confirm the task is Replacement or isolated DSV QA and record the applicable frozen fixture or external fact source.
2. Locate the first differing round before changing strategy or order behavior.
3. Separate strategy target, prepared child, SDK call, native order, fill and protection evidence.
4. Apply only user-approved differences. The slow partial-fill approval covers its recorded rounds and does not approve unrelated restart or month-stop behavior.
5. After routine changes, run only compilation. Replays, tests, SDK connections, orders and packaging require explicit authorization for the current task.

If a required DSV meaning is unresolved, use source or observed evidence and report `NEEDS_REVIEW`; do not infer it from similar Legacy or Standalone code.
