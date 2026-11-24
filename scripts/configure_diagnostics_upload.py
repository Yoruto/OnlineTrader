"""Write the authorized JiHuLab diagnostic upload configuration into one Runtime root."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def _token(path: Path, entry: int) -> str:
    rows = [row.strip() for row in path.read_text(encoding="utf-8-sig").splitlines() if row.strip()]
    if not 1 <= entry <= len(rows):
        raise ValueError("token entry does not exist")
    value = rows[entry - 1]
    for separator in ("：", ":"):
        if separator in value:
            value = value.split(separator, 1)[1].strip()
            break
    if (not value or len(value) > 512
            or any(not char.isascii() or char.isspace() or ord(char) < 33 for char in value)):
        raise ValueError("selected token entry is invalid")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--token-entry", type=int, default=3)
    args = parser.parse_args(argv)
    try:
        root = args.runtime_root.resolve()
        if not (root / "runtime.json").is_file():
            raise ValueError("runtime root is missing runtime.json")
        document = {
            "schema_version": "ONLINE_TRADER_DIAGNOSTIC_UPLOAD/1",
            "enabled": True,
            "backend": "JIHULAB_GENERIC_PACKAGE",
            "host": "jihulab.com",
            "project_id": 390001,
            "project_path": "Yoruto/online-trader-diagnostics",
            "package_name": "online-trader-diagnostics",
            "private_token": _token(args.token_file, args.token_entry),
            "connect_timeout_seconds": 5,
            "upload_timeout_seconds": 20,
        }
        path = root / "private" / "diagnostics-upload.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)
        print(path)
        return 0
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
