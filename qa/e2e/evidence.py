"""Structured JSONL trace recorder and human-readable trade trajectory report."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path


class TraceRecorder:
    """Writes per-round JSONL trace and produces trajectory / failure reports."""

    def __init__(self, output_dir: str | Path, *, scenario_name: str, run_id: str) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.scenario_name = scenario_name
        self.run_id = run_id
        self.trace_path = self.output_dir / "scenario-trace.jsonl"
        self.report_json_path = self.output_dir / "report.json"
        self.report_text_path = self.output_dir / "report.txt"
        self._entries: list[dict[str, object]] = []
        self._seq = 0

    def record(self, kind: str, **fields: object) -> dict[str, object]:
        self._seq += 1
        entry = {
            "trace_seq": self._seq,
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "scenario": self.scenario_name,
            "run_id": self.run_id,
            "kind": kind,
            **fields,
        }
        self._entries.append(entry)
        with self.trace_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
        return entry

    def finalize(
        self,
        *,
        passed: bool,
        failure: dict[str, object] | None = None,
        final_broker: dict[str, object] | None = None,
        final_workbench: dict[str, object] | None = None,
        evidence_files: dict[str, str] | None = None,
    ) -> dict[str, object]:
        report = {
            "schema_version": "ONLINE_TRADER_E2E_REPORT/1",
            "scenario": self.scenario_name,
            "run_id": self.run_id,
            "status": "PASS" if passed else "FAIL",
            "rounds_recorded": sum(1 for item in self._entries if item["kind"] == "ROUND"),
            "failure": failure,
            "final_broker_positions": (final_broker or {}).get("positions"),
            "final_workbench_responsibility": (final_workbench or {}).get("current_responsibility"),
            "evidence_files": evidence_files or {},
            "trajectory": self._entries,
        }
        self.report_json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        lines = [
            f"Scenario: {self.scenario_name}",
            f"Run ID:   {self.run_id}",
            f"Status:   {report['status']}",
            "",
            "--- Trajectory ---",
        ]
        for item in self._entries:
            kind = item["kind"]
            if kind == "ROUND":
                lines.append(
                    f"[{item.get('round_seq'):>2}] {item.get('wall_time')} | "
                    f"last={item.get('last')} | auth={item.get('entry_authority')} | "
                    f"owned={item.get('workbench_owned')} | outbound={len(item.get('outbound', []))} | "
                    f"reasons={item.get('entry_reasons')}"
                )
            elif kind == "COMMAND":
                lines.append(
                    f"[CMD] {item.get('operation')} -> status={item.get('receipt_status')}"
                )
            elif kind == "BROKER_ACTION":
                lines.append(
                    f"[BRK] {item.get('action')} child={item.get('child_order_id')} "
                    f"detail={item.get('detail')}"
                )
            elif kind == "FAILURE":
                lines.append(
                    f"[ERR] invariant={item.get('invariant_id')} round={item.get('round_seq')} "
                    f"msg={item.get('message')}"
                )
        if failure:
            lines.extend([
                "",
                "--- Failure Summary ---",
                json.dumps(failure, ensure_ascii=False, indent=2, default=str),
            ])
        if evidence_files:
            lines.extend(["", "--- Evidence Files ---"])
            for label, path in evidence_files.items():
                lines.append(f"  {label}: {path}")
        self.report_text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return report
