"""Minimal C01-C08 analyzer and cross-process timeline builder for diagnostic bundles."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from zipfile import BadZipFile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_market import evaluate_market
from validate_bundle import load_validated_bundle


_SEVERITY_ORDER = {
    "VIOLATION": 5,
    "DEGRADED": 4,
    "INCONCLUSIVE": 3,
    "OBSERVED_OK": 2,
    "NOT_EXERCISED": 1,
    "NOT_APPLICABLE": 0,
}


def _utc_instant(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _int_seq(row):
    try:
        return int(row.get("event_seq") or 0)
    except (TypeError, ValueError):
        return 0


def _cite(row):
    return {
        "stream_id": row.get("stream_id"),
        "event_seq": row.get("event_seq"),
        "process_role": row.get("process_role"),
        "event_type": row.get("event_type"),
        "reason_code": row.get("reason_code"),
        "error_code": row.get("error_code"),
        "observed_at_utc": row.get("observed_at_utc"),
    }


def _complete_capture(validation, manifest):
    return (
        validation.get("result") == "VALID"
        and manifest.get("capture_status") == "COMPLETE_WITHIN_SCOPE"
        and not validation.get("sequence_gaps")
        and int(manifest.get("dropped_events") or 0) == 0
    )


def _check_c01_lifecycle(validation, manifest, events, bootstrap_rows):
    lifecycle = [row for row in events if row.get("component") == "LIFECYCLE"]
    failures = [
        row for row in lifecycle
        if row.get("event_type") in {"RUNTIME_FAILED", "OWNER_FAILED"} or row.get("error_code")
    ]
    boot_failures = [row for row in bootstrap_rows if row.get("source") == "startup-failure"]
    if failures or boot_failures:
        citations = [_cite(row) for row in failures[:5]]
        return {
            "check_id": "C01",
            "name": "生命周期",
            "stage": "BOOTSTRAP" if boot_failures and not lifecycle else "LIFECYCLE",
            "result": "VIOLATION",
            "reason": (failures[0].get("error_code") if failures else boot_failures[0].get("error_code")) or "LIFECYCLE_FAILURE",
            "citations": citations,
            "evidence_gaps": [],
        }
    started = any(row.get("event_type") == "RUNTIME_STARTED" for row in lifecycle)
    stopped = any(row.get("event_type") in {"RUNTIME_STOPPED", "OWNER_STOPPED"} for row in lifecycle)
    if started and stopped and _complete_capture(validation, manifest):
        return {
            "check_id": "C01",
            "name": "生命周期",
            "stage": "LIFECYCLE",
            "result": "OBSERVED_OK",
            "reason": "CLEAN_LIFECYCLE_OBSERVED",
            "citations": [_cite(lifecycle[0]), _cite(lifecycle[-1])],
            "evidence_gaps": [],
        }
    if not manifest.get("clean_shutdown") and started:
        return {
            "check_id": "C01",
            "name": "生命周期",
            "stage": "LIFECYCLE",
            "result": "DEGRADED" if manifest.get("report_reason") != "MARKET_CLOSE" else "INCONCLUSIVE",
            "reason": "OPEN_OR_UNCLEAN_LIFECYCLE",
            "citations": [_cite(lifecycle[0])],
            "evidence_gaps": list(manifest.get("missing_sources") or ()),
        }
    return {
        "check_id": "C01",
        "name": "生命周期",
        "stage": "LIFECYCLE",
        "result": "INCONCLUSIVE",
        "reason": "INCOMPLETE_LIFECYCLE_EVIDENCE",
        "citations": [_cite(row) for row in lifecycle[:4]],
        "evidence_gaps": list(manifest.get("missing_sources") or ()),
    }


def _check_c03_strategy(validation, manifest, events):
    decisions = [row for row in events if row.get("event_type") == "STRATEGY_DECISION_CHANGED"]
    if not decisions:
        return {
            "check_id": "C03",
            "name": "策略判断",
            "stage": "STRATEGY",
            "result": "NOT_EXERCISED" if _complete_capture(validation, manifest) else "INCONCLUSIVE",
            "reason": "NO_STRATEGY_DECISION_EVENTS",
            "citations": [],
            "evidence_gaps": [] if _complete_capture(validation, manifest) else ["NO_STRATEGY_DECISIONS"],
        }
    violations = []
    by_contract = {}
    for row in decisions:
        p = row.get("payload") or {}
        cid = p.get("contract_id") or (row.get("correlation") or {}).get("contract_id") or "UNKNOWN"
        reason = row.get("reason_code") or p.get("reason")
        by_contract.setdefault(cid, []).append(reason)
        if reason == "NORMAL_ENTRY":
            try:
                high, low = float(p["day_high"]), float(p["day_low"])
                opening, stop = float(p["open_offset"]), float(p["stop_offset"])
                if high - low > 2 * opening - stop:
                    violations.append(row)
            except (KeyError, TypeError, ValueError):
                pass
    summary_reasons = {cid: list(dict.fromkeys(r for r in rs if r))[-5:] for cid, rs in sorted(by_contract.items())}
    if violations:
        return {
            "check_id": "C03",
            "name": "策略判断",
            "stage": "STRATEGY",
            "result": "VIOLATION",
            "reason": "GEOMETRY_LIMIT_EXCEEDED_ON_NORMAL_ENTRY",
            "contract_reasons": summary_reasons,
            "citations": [_cite(row) for row in violations[:5]],
            "evidence_gaps": [],
        }
    return {
        "check_id": "C03",
        "name": "策略判断",
        "stage": "STRATEGY",
        "result": "OBSERVED_OK" if _complete_capture(validation, manifest) else "INCONCLUSIVE",
        "reason": "STRATEGY_INVARIANTS_HELD" if _complete_capture(validation, manifest) else "PARTIAL_CAPTURE",
        "contract_reasons": summary_reasons,
        "citations": [_cite(decisions[0]), _cite(decisions[-1])] if len(decisions) > 1 else [_cite(decisions[0])],
        "evidence_gaps": [] if _complete_capture(validation, manifest) else list(manifest.get("missing_sources") or ()),
    }


def _check_orders(validation, manifest, events):
    prepared = [row for row in events if row.get("event_type") == "ORDER_PREPARED"]
    cancels = [row for row in events if row.get("event_type") == "ORDER_CANCEL_REQUESTED"]
    dispatched = [row for row in events if row.get("event_type") == "SDK_SEND_DISPATCHED"]
    updates = [row for row in events if row.get("event_type") == "ORDER_NATIVE_UPDATE"]
    fills = [row for row in events if row.get("event_type") == "TRADE_FILL"]
    refusals = [
        row for row in events
        if row.get("event_type") in {
            "PRE_SEND_VALIDATION_REFUSED", "SDK_SEND_REFUSED_BEFORE_QUEUE",
            "EFFECT_DISPATCH_DEFINITELY_NOT_SENT",
        }
    ]
    unknowns = [
        row for row in events
        if row.get("event_type") in {"SDK_SEND_UNCERTAIN_EXCEPTION", "EFFECT_DISPATCH_UNKNOWN"}
    ]
    exit_failures = [
        row for row in events
        if row.get("event_type") == "EXIT_FAILURE_CLASSIFIED"
        or row.get("reason_code") == "EXIT_MANUAL_TAKEOVER_REQUIRED"
    ]
    blocks = [row for row in events if row.get("event_type") == "ORDER_BLOCKED"]
    complete = _complete_capture(validation, manifest)

    # C04 Entry execution
    open_prepared = [row for row in prepared if (row.get("payload") or {}).get("effect") == "OPEN"]
    open_fills = [row for row in fills if (row.get("payload") or {}).get("effect") == "OPEN"]
    if unknowns:
        c04 = {
            "check_id": "C04", "name": "开仓执行", "stage": "EXECUTION",
            "result": "DEGRADED", "reason": unknowns[0].get("reason_code") or unknowns[0].get("event_type"),
            "citations": [_cite(unknowns[0])], "evidence_gaps": [],
        }
    elif not open_prepared:
        c04 = {
            "check_id": "C04", "name": "开仓执行", "stage": "STRATEGY" if blocks else "EXECUTION",
            "result": "NOT_EXERCISED" if complete else "INCONCLUSIVE",
            "reason": blocks[-1].get("reason_code") if blocks else "NO_OPEN_ORDERS_PREPARED",
            "citations": [_cite(row) for row in blocks[-3:]],
            "evidence_gaps": [] if complete else ["NO_OPEN_ORDER_EVIDENCE"],
        }
    else:
        open_refusals = [row for row in refusals if (row.get("payload") or {}).get("native_offset") == "OPEN"]
        c04 = {
            "check_id": "C04", "name": "开仓执行", "stage": "EXECUTION",
            "result": "DEGRADED" if open_refusals else ("OBSERVED_OK" if complete else "INCONCLUSIVE"),
            "reason": open_refusals[0].get("reason_code") if open_refusals else (
                "OPEN_EXECUTION_TRACED" if complete else "PARTIAL_CAPTURE"
            ),
            "open_prepared_count": len(open_prepared),
            "open_fill_count": len(open_fills),
            "citations": [_cite(row) for row in (open_refusals or open_prepared)[:5]],
            "evidence_gaps": [] if complete else list(manifest.get("missing_sources") or ()),
        }

    # C05 Exit execution
    reduce_prepared = [row for row in prepared if (row.get("payload") or {}).get("effect") == "REDUCE"]
    conv_events = [row for row in events if row.get("event_type") in {"CONVERGENCE_ACTION", "CONVERGENCE_DEFERRED"}]
    if exit_failures:
        takeover = any(row.get("reason_code") == "EXIT_MANUAL_TAKEOVER_REQUIRED" for row in exit_failures)
        c05 = {
            "check_id": "C05", "name": "平仓执行", "stage": "EXIT",
            "result": "VIOLATION" if takeover else "DEGRADED",
            "reason": exit_failures[0].get("reason_code") or "EXIT_FAILURE_CLASSIFIED",
            "citations": [_cite(row) for row in exit_failures[:5]], "evidence_gaps": [],
        }
    elif not reduce_prepared and not conv_events:
        c05 = {
            "check_id": "C05", "name": "平仓执行", "stage": "EXIT",
            "result": "NOT_EXERCISED" if complete else "INCONCLUSIVE",
            "reason": "NO_EXIT_ORDERS_REQUIRED",
            "citations": [], "evidence_gaps": [] if complete else ["NO_EXIT_EVIDENCE"],
        }
    else:
        c05 = {
            "check_id": "C05", "name": "平仓执行", "stage": "EXIT",
            "result": "OBSERVED_OK" if complete else "INCONCLUSIVE",
            "reason": "EXIT_EXECUTION_TRACED" if complete else "PARTIAL_CAPTURE",
            "reduce_prepared_count": len(reduce_prepared),
            "cancel_requested_count": len(cancels),
            "citations": [_cite(row) for row in (reduce_prepared or conv_events)[:5]],
            "evidence_gaps": [] if complete else list(manifest.get("missing_sources") or ()),
        }

    # C06 Duplicate / overlapping active child orders
    active_by_slot: dict[tuple[str, str], dict[str, dict]] = {}
    duplicates = []
    for row in events:
        etype = row.get("event_type")
        corr = row.get("correlation") or {}
        pay = row.get("payload") or {}
        cid = pay.get("contract_id") or corr.get("contract_id")
        child = corr.get("child_order_id")
        if etype == "ORDER_PREPARED" and cid and child:
            effect = pay.get("effect") or "OPEN"
            slot = active_by_slot.setdefault((cid, effect), {})
            if slot:
                duplicates.append((row, list(slot.keys())))
            slot[child] = row
        elif child and etype in {
            "PRE_SEND_VALIDATION_REFUSED", "SDK_SEND_REFUSED_BEFORE_QUEUE",
            "EFFECT_DISPATCH_DEFINITELY_NOT_SENT", "UNKNOWN_NEGATIVE_RECONCILED",
        }:
            for slot in active_by_slot.values():
                slot.pop(child, None)
        elif child and etype == "ORDER_NATIVE_UPDATE" and pay.get("terminal_confirmed"):
            for slot in active_by_slot.values():
                slot.pop(child, None)

    if duplicates:
        c06 = {
            "check_id": "C06", "name": "重复委托", "stage": "ORDER",
            "result": "VIOLATION", "reason": "OVERLAPPING_NON_TERMINAL_CHILD_ORDERS",
            "citations": [_cite(row) for row, _ in duplicates[:5]], "evidence_gaps": [],
        }
    elif not prepared:
        c06 = {
            "check_id": "C06", "name": "重复委托", "stage": "ORDER",
            "result": "NOT_EXERCISED" if complete else "INCONCLUSIVE",
            "reason": "NO_CHILD_ORDERS_PREPARED",
            "citations": [], "evidence_gaps": [] if complete else ["NO_ORDER_EVIDENCE"],
        }
    else:
        c06 = {
            "check_id": "C06", "name": "重复委托", "stage": "ORDER",
            "result": "OBSERVED_OK" if complete else "INCONCLUSIVE",
            "reason": "NO_OVERLAPPING_CHILD_ORDERS" if complete else "PARTIAL_CAPTURE",
            "dispatched_count": len(dispatched),
            "native_update_count": len(updates),
            "citations": [_cite(prepared[0])],
            "evidence_gaps": [] if complete else list(manifest.get("missing_sources") or ()),
        }
    return c04, c05, c06


def _check_c07_commands_and_ui(validation, manifest, events):
    cmd_events = [row for row in events if row.get("component") == "COMMAND"]
    proj_events = [row for row in events if row.get("component") == "PROJECTION"]
    expired = [row for row in proj_events if row.get("event_type") == "WORKBENCH_SNAPSHOT_EXPIRED"]
    submit_failed = [
        row for row in cmd_events
        if row.get("event_type") in {"WORKBENCH_COMMAND_SUBMIT_FAILED", "WORKBENCH_COMMAND_BLOCKED"}
    ]
    complete = _complete_capture(validation, manifest)
    if submit_failed or expired:
        first = (submit_failed or expired)[0]
        return {
            "check_id": "C07", "name": "命令与界面一致性",
            "stage": "COMMAND" if submit_failed else "PROJECTION",
            "result": "DEGRADED",
            "reason": first.get("error_code") or first.get("reason_code") or first.get("event_type"),
            "citations": [_cite(row) for row in (submit_failed + expired)[:5]],
            "evidence_gaps": [],
        }
    if not cmd_events and not proj_events:
        wb_expected = bool((manifest.get("expected_sources") or {}).get("workbench"))
        return {
            "check_id": "C07", "name": "命令与界面一致性", "stage": "PROJECTION",
            "result": "NOT_EXERCISED" if (complete and not wb_expected) else "INCONCLUSIVE",
            "reason": "NO_WORKBENCH_OR_COMMAND_EVENTS",
            "citations": [], "evidence_gaps": [] if (complete and not wb_expected) else ["MISSING_WORKBENCH_EVIDENCE"],
        }
    return {
        "check_id": "C07", "name": "命令与界面一致性", "stage": "PROJECTION",
        "result": "OBSERVED_OK" if complete else "INCONCLUSIVE",
        "reason": "COMMAND_AND_PROJECTION_CONSISTENT" if complete else "PARTIAL_CAPTURE",
        "command_event_count": len(cmd_events),
        "projection_event_count": len(proj_events),
        "citations": [_cite(row) for row in (cmd_events or proj_events)[:4]],
        "evidence_gaps": [] if complete else list(manifest.get("missing_sources") or ()),
    }


def _check_c08_sync_and_storage(validation, manifest, events):
    storage_err = [
        row for row in events
        if row.get("component") == "STORAGE" and (row.get("severity") in {"ERROR", "CRITICAL"} or row.get("error_code"))
    ]
    history_warn = [
        row for row in events
        if row.get("component") == "HISTORY" and (row.get("severity") in {"WARNING", "ERROR", "CRITICAL"} or row.get("error_code"))
    ]
    cutoff = [row for row in events if row.get("event_type") == "EXECUTION_JOURNAL_CUTOFF"]
    complete = _complete_capture(validation, manifest)
    if storage_err:
        return {
            "check_id": "C08", "name": "数据同步与存储", "stage": "STORAGE",
            "result": "VIOLATION",
            "reason": storage_err[0].get("error_code") or storage_err[0].get("event_type"),
            "citations": [_cite(row) for row in storage_err[:5]], "evidence_gaps": [],
        }
    if history_warn:
        return {
            "check_id": "C08", "name": "数据同步与存储", "stage": "HISTORY",
            "result": "DEGRADED",
            "reason": history_warn[0].get("error_code") or history_warn[0].get("event_type"),
            "citations": [_cite(row) for row in history_warn[:5]], "evidence_gaps": [],
        }
    if not cutoff and not complete:
        return {
            "check_id": "C08", "name": "数据同步与存储", "stage": "STORAGE",
            "result": "INCONCLUSIVE", "reason": "NO_JOURNAL_CUTOFF_OR_PARTIAL_CAPTURE",
            "citations": [], "evidence_gaps": list(manifest.get("missing_sources") or ()),
        }
    return {
        "check_id": "C08", "name": "数据同步与存储", "stage": "STORAGE",
        "result": "OBSERVED_OK" if complete else "INCONCLUSIVE",
        "reason": "SYNC_AND_STORAGE_CLEAN" if complete else "PARTIAL_CAPTURE",
        "citations": [_cite(cutoff[0])] if cutoff else [],
        "evidence_gaps": [] if complete else list(manifest.get("missing_sources") or ()),
    }


def format_markdown(report: dict) -> str:
    lines = [
        f"# OnlineTrader 诊断分析报告 ({report.get('report_id') or 'UNKNOWN'})",
        "",
        f"- 总体结论: `{report['result']}`",
        f"- 首个异常阶段: `{report.get('first_fault_stage') or 'NONE'}`",
        f"- 校验状态: `{report['validation']['result']}` / 覆盖: `{report['validation']['coverage']}`",
        f"- 采集状态: `{report.get('capture_status')}` · 事件数: `{report.get('event_count')}` · 错误数: `{report.get('error_count')}`",
        "",
        "## C01–C08 行为核对",
        "",
        "| 检查 | 名称 | 阶段 | 结论 | 原因 | 证据缺口 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for chk in report.get("checks", ()):
        gaps = ", ".join(chk.get("evidence_gaps") or ()) or "—"
        lines.append(
            f"| {chk['check_id']} | {chk['name']} | {chk['stage']} | `{chk['result']}` | `{chk['reason']}` | {gaps} |"
        )
    return "\n".join(lines) + "\n"


def analyze(path):
    try:
        validation, manifest, events, view_logs, bootstrap_rows = load_validated_bundle(path)
    except (OSError, ValueError, BadZipFile, KeyError, json.JSONDecodeError) as error:
        return {
            "schema_version": "ONLINE_TRADER_DIAGNOSTIC_ANALYSIS/2",
            "result": "INCONCLUSIVE",
            "first_fault_stage": "BUNDLE",
            "validation": {
                "schema_version": "ONLINE_TRADER_DIAGNOSTIC_VALIDATION/1",
                "result": "INVALID",
                "coverage": "FAILED",
                "errors": [type(error).__name__],
            },
            "checks": [],
            "timeline": [],
        }

    if validation["result"] != "VALID":
        return {
            "schema_version": "ONLINE_TRADER_DIAGNOSTIC_ANALYSIS/2",
            "result": "INCONCLUSIVE",
            "first_fault_stage": "BUNDLE",
            "validation": validation,
            "checks": [],
            "timeline": [],
        }

    ordered = sorted(
        events,
        key=lambda row: (_utc_instant(row.get("observed_at_utc")), str(row.get("stream_id") or ""), _int_seq(row)),
    )
    timeline = [
        {
            "stream_id": row.get("stream_id"),
            "event_seq": row.get("event_seq"),
            "observed_at_utc": row.get("observed_at_utc"),
            "monotonic_ns": row.get("monotonic_ns"),
            "process_role": row.get("process_role"),
            "runtime_boot_id": row.get("runtime_boot_id"),
            "configuration_revision": row.get("configuration_revision"),
            "component": row.get("component"),
            "event_type": row.get("event_type"),
            "reason_code": row.get("reason_code"),
            "error_code": row.get("error_code"),
            "correlation": row.get("correlation") or {},
            "payload": row.get("payload") or {},
        }
        for row in ordered
    ]

    c01 = _check_c01_lifecycle(validation, manifest, ordered, bootstrap_rows)
    market_res = evaluate_market(validation, manifest, ordered)
    c02 = {
        "check_id": "C02",
        "name": "行情连续性",
        "stage": "MARKET",
        "result": market_res["result"],
        "reason": market_res["reason"],
        "citations": market_res.get("citations", []),
        "evidence_gaps": market_res.get("evidence_gaps", []),
    }
    c03 = _check_c03_strategy(validation, manifest, ordered)
    c04, c05, c06 = _check_orders(validation, manifest, ordered)
    c07 = _check_c07_commands_and_ui(validation, manifest, ordered)
    c08 = _check_c08_sync_and_storage(validation, manifest, ordered)
    checks = [c01, c02, c03, c04, c05, c06, c07, c08]

    faults = [c for c in checks if c["result"] in {"VIOLATION", "DEGRADED"}]
    first_fault_stage = faults[0]["stage"] if faults else None
    worst = max(checks, key=lambda c: _SEVERITY_ORDER.get(c["result"], 0))
    overall = worst["result"]
    if overall == "OBSERVED_OK" and not _complete_capture(validation, manifest):
        overall = "INCONCLUSIVE"
    if overall == "NOT_EXERCISED" and not _complete_capture(validation, manifest):
        overall = "INCONCLUSIVE"

    errors = [row for row in timeline if row["error_code"]]
    return {
        "schema_version": "ONLINE_TRADER_DIAGNOSTIC_ANALYSIS/2",
        "report_id": manifest.get("report_id"),
        "capture_status": manifest.get("capture_status"),
        "result": overall,
        "first_fault_stage": first_fault_stage,
        "validation": validation,
        "event_count": len(timeline),
        "runtime_view_record_count": len(view_logs),
        "bootstrap_context_count": len(bootstrap_rows),
        "error_count": len(errors),
        "checks": checks,
        "timeline": timeline,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args(argv)
    result = analyze(args.bundle)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print(text, end="")
    else:
        args.output.write_text(text, encoding="utf-8")
    if args.markdown is not None:
        args.markdown.write_text(format_markdown(result), encoding="utf-8")
    return 0 if result["validation"]["result"] == "VALID" else 2


if __name__ == "__main__":
    raise SystemExit(main())
