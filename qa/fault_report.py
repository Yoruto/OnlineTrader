"""Append-only local fault evidence; no product imports or trading permissions."""

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import uuid


ROOT = Path(__file__).resolve().parents[1]


class FaultReport:
    def __init__(self, case: str) -> None:
        slug = re.sub(r"[^a-zA-Z0-9_-]", "-", case)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = ROOT / "output" / "faults" / f"{stamp}-{slug}-{uuid.uuid4().hex[:8]}"
        self.path.mkdir(parents=True)
        self.data = {"case": case, "status": "running", "scope": "isolated development events; no broker or physical power-loss test", "events": []}
        self.write()

    def write(self) -> None:
        (self.path / "result.json").write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def event(self, name: str, **values) -> None:
        self.data["events"].append({"event": name, **values})
        self.write()

    def passed(self, **assertions) -> None:
        self.data["assertions"] = assertions
        self.data["assertions_passed"] = True
        self.write()

    def finish(self) -> None:
        self.data["status"] = "passed" if self.data.get("assertions_passed") else "failed"
        self.write()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.flush()
        import os
        os.fsync(output.fileno())
    temporary.replace(path)
