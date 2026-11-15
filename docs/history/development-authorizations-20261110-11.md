# 2026-11-10 至 11-11 历史开发授权与验证记录

以下内容来自当时的项目开发指引，保留当次范围、结果及产物定位。窗口、额度和操作批准仅用于历史取证。当前范围见 [OnlineTrader 开发指引](../../AGENTS.md)。原文中的 `docs/`、`qa/` 和 `output/` 路径以 OnlineTrader 仓库根目录为基准。

One-off online simulation smoke was explicitly authorized on 2026-11-10 and completed through
the separate qa/online_kq_one_minute.py probe using TqKq, outside product runtime/package.
Its single one-lot opening allowance is consumed and closed. Evidence and limits are in
docs/online-simulation-smoke-20261110.md. This does not enable a production gateway, rearm the
offline kernel or authorize recurring openings. Preserve the original DSV process/activity.

The user's follow-up on 2026-11-10 authorizes continuing implementation and preparing the
13:30 afternoon simulation test. The new product simulation path is confined to an explicit
KQ_MANUAL_SIM/1 grant, TqKq only, a dedicated original product ledger, one opening attempt,
one long lot, a 60-second exit from the native fill time, fixed protection using saved DSV
distances, and owned-only cancellation/reduction. It does not enable DSV automatic entry or
advanced online modes. Preparation is read-only/disarmed; run requires the exact time window,
full synchronized account, fresh quotes and an unoccupied selected contract. Retain the
original commit/ack/send chain and final independent read-only login. Do not repeat openings
to compensate for a cancelled, unfilled or early-protected test. No recurring task/service.
The optional pinned SDK belongs only in the separate simulation environment. The default
core environment, default serve and packaged reference resources remain independent.

The afternoon product test is now completed and its grant is consumed: installed dev15,
DCE.l2701 one lot opened at 8339 and closed at 8341 after 61.384954178 native seconds.
Fresh read-only login confirmed the test contract flat and no active test-owned orders;
foreign positions were preserved. The sender and verification reader exited, authority is
DISARMED. Evidence: docs/product-online-simulation-20261110.md. This completed grant grants
no new opening or recurring activity. Full DSV automatic entry/advanced online protection
and complete live desktop acceptance remain unverified; retain the recorded closing-stage
read-only status timeout instead of claiming a successful holding-view snapshot.

The later 2026-11-10 continuation authorizes implementing the DSV simulation strategy,
advanced protection and G2 recovery, with offline checks only. The dev17 product path
uses a distinct KQ_DSV_SIM/1 scope and immutable 1.3 plans, reusing original owner,
writer, strategy, risk, scheduling and protection. It grants no account connection
or new order during development. The old KQ_MANUAL_SIM/1 scope stays one-shot/manual.
`dsv prepare` is fully offline; any future `dsv run` needs a new explicit scope.
QA-native facts and installed-wheel IPC results are offline evidence, never real
broker/GUI acceptance. Preserve the documented same-day close routing and missing
history/day/foreign/unknown gates; do not substitute a default or rearm on restart.

The dev18 continuation authorizes account/workbench/history/calendar/cross-day
implementation and offline validation only. Account selection/credential saving
never logs in. Windows user credentials stay outside catalogue history and argv.
The workbench uses the original IPC/command/receipt chain; timeouts query the
original command rather than resending. Calendar/history imports are reference
data, not live market facts or permission to modify an active plan. New-day
barriers retain original protection/UNKNOWN and original native evidence; mixed
today/historical routing is a reviewed preview until the remaining native/day
reconciliation and sending are implemented and separately verified. No account
connection, NTP request, live order, service/task install or release is authorized
by this development turn. Keep the SDK environment unchanged.

dev19 continues offline only. Native order/fill dates require exact timestamps
and the original hash-bound calendar; do not substitute the grant day for a
late fill. Day-change barriers may consume proved original facts but grant no
new send/recovery authority. Close targets include this cycle's pending closes;
native bucket routing is distinct from FIFO money cost and must not borrow
another cycle's position. Full historical native backfill and cross-day sending
remain incomplete; see docs/crossday-native-close-20261110.md.

The latest continuation prioritizes completing DSV-like online functionality
without account connections and with only necessary targeted checks. dev20 adds
explicit KQ_DSV_SIM/2 cabinet-day lists/rollover policy, original-owner native
terminal history, cross-day mixed bucket exits/recovery and contract entry
switches over the original control protocol. Preserve /1 scope semantics and all
historical evidence. Terminal history is never authority to release a missing
UNKNOWN. A contract enable does not arm the account. No full suite, wheel build,
installation, SDK environment update or release is required by this turn.

dev21 prioritizes SimNow integration, DSV comparison records and Windows/reference
clock alignment. SIMNOW_DSV_SIM/1 is a distinct provider/account/ledger scope;
reuse the original owner/CommitAck/send/UNKNOWN chain. SimNow uses only the pinned
TqAccount('simnow', ...) trade service and separate TQ market credentials. Local
profile/credential-reference/scope/prepare actions never connect. Runtime records
committed facts and reads DSV logs; comparison never adopts foreign trades or
writes fees. Only targeted necessary checks are requested, not a complete suite,
wheel or installation. This turn does not authorize an account connection. The
dated night input packet has no actual account scope. Windows clock sync is an
explicit separate tool; the current development run only queried its stopped
service and did not sample NTP or modify OS time. Preserve the SDK environment.

The subsequent user request explicitly authorizes Windows clock synchronization
and completing tonight's local preparation, while still forbidding account
connections. W32Time was started under Windows elevation and synchronized via
Aliyun NTP; independent measurement confirmed the alignment. Existing external
credentials were parsed in memory, saved in the current user's Windows vault
and read back without login or plaintext copies. A provider-specific night scope
and default-disarmed startup packet are prepared under output/simnow. Scope caps
are derived from saved DSV max_volume/max_daily_orders and the sum of maximum
nominal stop exposure, preserving DSV dynamic sizing. Prepared files grant no
current connection instruction; do not invoke -Connect/-Run/-Arm in this turn.
Retain zero-valued DSV parameters and recheck hashes after DSV calculations.

dev22 implements the user's requested startup offset calculation without account
connections. Static DSV evidence distinguishes the daily calculator/fixed stops
(silver fallback 15), explicit calculator application, and startup zero repair
(100/15/60). SIMNOW_DSV_AUTO/1 is a source calculation capability for one explicit
cabinet day and completed daily cutoff; Kernel cannot execute that source scope.
Only a reproducible derived fixed-profile scope reaches the original owner.
Use the same explicitly connected port for daily data, exclude unfinished/stale
daily labels and index gaps, commit the full calculation before entry. Restarts
restore original calculations/plans from the writer and never recalculate held
protection. Compare interpreted effective DSV parameters before arming comparison
entries; export the original eight-column symbol CSV, never rewrite DSV files.
The new night packet is prepared-auto-dev22-final; old fixed-parameter scopes
remain historical. This turn authorizes code and necessary targeted checks only,
not an account connection or -Connect/-Run/-Arm. Keep SDK/V7 unchanged.

The 2026-11-10 calculation request explicitly authorized a read-only SimNow
connection to fetch real daily data, calculate offsets and compare DSV parameters,
while prohibiting orders. This has completed at 22:28 Beijing: one SDK instance,
closed successfully, reader exited 0, no insert/cancel API calls or order packets,
no execution owner/ledger/arming. Real calculation is saved in the original
prepared-auto-dev22-final source cache; default prepare confirms it is restored.
All five interpreted stop distances match (silver 15), open/profit differ from
DSV saved/startup-interpreted values; all non-offset business settings match.
DSV was not running: do not claim its runtime memory or applied UI parameters
were verified. Do not modify/launch DSV, activate a strategy or send/cancel orders
under this read-only instruction. SDK/V7 and historical online evidence remain
unchanged. See docs/simnow-readonly-calculation-20261110.md and its real evidence.

The next user request authorized replaying DSV calculation logic on that exact
saved real daily/range dataset and correcting proven errors, with no orders.
qa/replay_dsv_offsets.py now extracts only pure calculator function code objects
and literal tables from the hash-verified executable; it does not import the DSV
application or evaluate module initialization. It uses a separate QA Python 3.14
environment (output/tools/dsv-calc-venv), pandas 3.0.5 and numpy 2.5.3 matching the
original bundle, with network/process guards. All five actual calculation triples
match the current product. Silver's raw None/None window metadata falls back to
3/30 inside original base_series, so compare actual windows. No production
formula/cache, original DSV settings or SDK/V7 changed; no account connection,
owner, arming, or order occurred. Saved-config differences remain distinct from
calculator equivalence and do not prove DSV runtime settings were applied.
See docs/dsv-same-input-calculation-20261110.md and final replay evidence there.

The 2026-11-10 application request authorized applying the verified calculation to DSV
and this product and preparing tonight's comparison, with no orders. Applied at
23:11 Beijing: only 11 offset fields and the two necessary startup controls
(trade.auto_start_on_launch and schedule.enabled, now false on BOTH sides) changed.
The exact private original backup stays beside DSV config, not in product output.
All other private/settings tokens are preserved. DSV GUI was launched safely with
automatic/scheduled startup disabled; its actual five-row table matches and it
shows stopped/disconnected. No strategy or order controls were used. Product's
actual parameter UI was imported for readback, but remains a preview: execution
application is the independently verified original startup scope/profile/cache.
Current full business hash is fe42e982af218b60f2e46a92a5d83317c5ba857fb1b990f89b0b590582b708c9.
Current packet is output/simnow/20261110-night-ready/applied-comparison-20261110T151142Z;
current-comparison-packet.json selects it and Start-Comparison.ps1 defaults to
prepare only. Old prepared-auto-dev22-final and Refresh-Preparation.ps1 are
historical, not the selected applied packet. Same real daily/range inputs are
preserved; original function replay still agrees; account/caps/day/window/calendar
unchanged. Clock recheck ~66ms. No product execution owner/ledger/arming or orders.
DSV and product parameter windows are open, both strategy runtimes stopped.
At 23:00 four contracts' night sessions ended; only silver remains until 02:30.
Do not extend the scope/window or treat later day/night sessions as authorized.
See docs/matched-parameters-applied-20261110.md and its actual UI/startup evidence.

2026-11-11 update. The night packet and morning KQ observation scope have ended.
The morning readback found DSV scheduled start enabled. Its five offset triples
still matched the prepared profile. The temporary KQ product run bound the then
current full DSV profile. The original DSV strategy remained stopped.

The dual-provider observation aligned 33,139 common native ticks. After fixing
mid-session startup, the actual KQ runtime recorded 387 geometry-block decisions
over five contracts, then an independent read-only login confirmed zero orders,
trades and positions. Both SDK sessions closed. See
docs/kq-readonly-runtime-20261111.md for the original results and limitations.
Natural entry, native order/cancel, fill and owned-position protection remain
separate acceptance scenarios. A new run needs the current session, explicit
profile/calendar/account binding and authority for the requested actions.
