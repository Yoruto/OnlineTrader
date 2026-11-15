# OnlineTrader development

Independent Windows product. G0 source specifications are recorded in docs/spec-source.json.
Preserve existing work; do not import V7, vnpy, DSV, their credentials, databases or environments.

For DSV parity, start with the [current refactor index](docs/refactor/dsv-parity-20261114/README.md)
and [the connected strategy and SDK replay](docs/refactor/dsv-parity-20261114/15-strategy-sdk-schedule.md).
Verify the frozen archive and run the original code in isolated QA when it can be reused.
Feed both implementations the same configuration, ticks, position snapshots and ordered facts.
Trace the first differing round through `wait_update`, strategy target, SDK API call and outbound packet.
Keep `online_trader.dsv_core` free of the legacy owner, SDK, UI, I/O and system clock.
Keep the explicit controller's per-child pre-send durability and the existing cutover gates.

For slow partial fills, honor the approved behavior in
[the decision register](docs/refactor/dsv-parity-20261114/00-evidence-and-decisions.md#用户批准的行为差异).
In the fixed sample, keep the 5-lot opening target when 3 lots are proved, then hold the
5 proved lots after the remaining 2 fill. Include the fixed round 7 and 9 replay when changing
affected behavior.

Current implementation and evidence are indexed in [README](README.md) and the
[96-item checklist](docs/功能实现与待办清单_20261110.md). The product has separate
TqKq and SimNow simulation paths and a pinned optional SDK environment. Use the
current user's authorization and current session scope for account connections,
order operations, service installation, CI or release work. B01-B20 follow the
user-confirmed DSV reference in docs/decisions/dsv-confirmation-20261109.json.
A saved reference or draft is NON_TRADING. Confirm unresolved DSV semantics from
source or observation evidence.

One owner controls state and one writer controls SQLite. Commit required facts before send;
unknown sends reserve capacity and require reconciliation. GUI/strategy lifetime cannot delete
protection. All exits share one remaining target; cancellation requests do not release quantity.

External statement comparison is read only. Reviewed fee registration is confined to the existing
ONLINE_SIM/development ledger while the execution owner is closed, all owned cycles are flat/closed
and orders are quantity-consistent terminal facts. It appends statement fees and a durable receipt
in one original-writer transaction, never imports or rewrites trade prices, quantities or protection,
and leaves entry authority DISARMED. A CSV/export/preview alone is not a registration instruction.

Tests and QA are outside the production wheel. For a full source regression on Windows, run
`.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v` with `PYTHONPATH=src`.
Use `.venv\Scripts\python.exe -X utf8 -m compileall -q src tests qa` for source compilation.
Follow the global independent-testing instructions for delivery acceptance and use the actual
entry point with isolated fixtures. Report offline, connected and packaged evidence by scope.

Offline parameter work also includes the DSV 33-field trade-form templates and whole-batch symbol
CSV preview/import/export. Template application and CSV import return preview values only; they
never write trading authority, active configuration or existing protection plans. Template edits
require an explicit expected revision and append immutable local history outside the ledger.
All business-parameter editing and the versioned DSV offset calculator remain preview-only.
Use explicit offline daily-series identities and an explicit cutoff trading date; never infer
live market completeness, use today's unfinished bars as fallback, or activate trading from a
calculated/exported result. Calculations and applications bind the data and preview hashes.

Historical execution grants and evidence are archived in [the November 10–11 development record](docs/history/development-authorizations-20261110-11.md).

When modifying startup, commit historical path replay separately from the quote
used for execution. Preserve the durable observation start and validate native
sequence, session and source epoch before extending the path. Delayed incremental
updates use the bounded startup catch-up and the original freshness limit. Reuse
the existing evidence when the task only maintains documentation.
