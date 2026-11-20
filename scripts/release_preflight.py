"""Fast, side-effect-free release identity and configuration preflight."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import re
import struct
import subprocess
import sys
import tomllib


SCHEMA = "ONLINE_TRADER_RELEASE_PREFLIGHT/1"
REQUIRED_RUNTIME_FILES = (
    "runtime.json",
    "OnlineTrader-parameters.json",
    "private/OnlineTrader-defaults.json",
)
OPTIONAL_RUNTIME_FILES = ("OnlineTrader-overrides.json",)
RETIRED_NAMES = {
    "runtime-control.json",
    "operators.json",
    "initialized.json",
}
RETIRED_PARTS = {"watchdog", "handoff"}
DATABASE_SUFFIXES = (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3")
SILVER = re.compile(r"(?<![A-Za-z0-9])SHFE\.ag[0-9]*(?![A-Za-z0-9])", re.IGNORECASE)
REQUIRED_TOOLCHAIN = {
    "pyinstaller": "6.22.3",
    "PySide6": "6.8.2.1",
    "tqsdk": "3.10.4",
    "jsonschema": "4.26.0",
    "referencing": "0.37.0",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(root: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", *arguments], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ).stdout


def candidate_identity(root: Path) -> dict[str, object]:
    head = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
    tracked = _git(root, "ls-files", "-z").split(b"\0")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
    paths = sorted({item.decode("utf-8", "surrogateescape") for item in (*tracked, *untracked) if item})
    tree = hashlib.sha256()
    entries = []
    for relative in paths:
        path = root / relative
        normalized = relative.replace("\\", "/")
        if path.is_file():
            file_hash = _sha256(path)
            marker = "file"
        elif path.is_symlink():
            file_hash = hashlib.sha256(path.readlink().as_posix().encode("utf-8")).hexdigest()
            marker = "symlink"
        else:
            file_hash = "-"
            marker = "missing"
        record = f"{marker}\0{normalized}\0{file_hash}\n".encode("utf-8", "surrogateescape")
        tree.update(record)
        entries.append({"path": normalized, "kind": marker, "sha256": file_hash})
    return {
        "head": head,
        "tree_sha256": tree.hexdigest(),
        "entry_count": len(entries),
        "entries": entries,
    }


def source_version(root: Path) -> str:
    module = root / "src" / "online_trader" / "__init__.py"
    parsed = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    for statement in parsed.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in statement.targets
        ):
            if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                return statement.value.value
    raise ValueError("src/online_trader/__init__.py has no literal __version__ assignment")


def validate_runtime_config(directory: Path) -> dict[str, str]:
    directory = directory.resolve()
    if not directory.is_dir():
        raise ValueError(f"Runtime config directory does not exist: {directory}")
    allowed = set(REQUIRED_RUNTIME_FILES) | set(OPTIONAL_RUNTIME_FILES)
    files = sorted(path for path in directory.rglob("*") if path.is_file())
    relative = {path.relative_to(directory).as_posix() for path in files}
    missing = set(REQUIRED_RUNTIME_FILES) - relative
    unexpected = relative - allowed
    if missing:
        raise ValueError("Runtime config is missing: " + ", ".join(sorted(missing)))
    if unexpected:
        raise ValueError("Runtime config contains unexpected files: " + ", ".join(sorted(unexpected)))
    for name in relative:
        lowered = name.casefold()
        parts = {part.casefold() for part in Path(name).parts}
        if Path(name).name.casefold() in RETIRED_NAMES or parts & RETIRED_PARTS or lowered.endswith(DATABASE_SUFFIXES):
            raise ValueError(f"Runtime config contains retired or runtime-owned state: {name}")
        raw = (directory / name).read_bytes()
        if len(raw) > 262_144:
            raise ValueError(f"Runtime config is too large: {name}")
        document = json.loads(raw.decode("utf-8-sig"))
        serialized = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        if "白银" in serialized or SILVER.search(serialized):
            raise ValueError(f"Runtime config contains a silver contract: {name}")
    return {name: _sha256(directory / name) for name in sorted(relative)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-source", type=Path, default=Path.cwd())
    parser.add_argument("--delivery-mode", choices=("RuntimeConfigured",), required=True)
    parser.add_argument("--config-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.candidate_source.resolve()
    report: dict[str, object] = {"schema_version": SCHEMA, "result": "FAIL", "errors": []}
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        declared = project["project"]["version"]
        actual = source_version(root)
        if declared != actual:
            raise ValueError(f"Version mismatch: pyproject={declared}, source={actual}")
        if sys.platform != "win32" or sys.version_info[:2] != (3, 13) or struct.calcsize("P") != 8:
            raise ValueError("Release tools require Windows x64 and Python 3.13.")
        installed = {name: importlib.metadata.version(name) for name in REQUIRED_TOOLCHAIN}
        mismatched = {
            name: {"expected": expected, "actual": installed[name]}
            for name, expected in REQUIRED_TOOLCHAIN.items()
            if installed[name] != expected
        }
        if mismatched:
            raise ValueError("Release toolchain mismatch: " + json.dumps(mismatched, sort_keys=True))
        required_paths = (
            root / "release" / "OnlineTrader.spec",
            root / "release" / "entry.py",
            root / "scripts" / "build-portable.ps1",
            root / "scripts" / "portable_delivery.py",
        )
        absent = [path.relative_to(root).as_posix() for path in required_paths if not path.is_file()]
        if absent:
            raise ValueError("Missing release inputs: " + ", ".join(absent))
        report.update(
            result="PASS",
            candidate=candidate_identity(root),
            version=actual,
            platform={"python": platform.python_version(), "architecture": platform.machine()},
            toolchain=installed,
            release_inputs={path.relative_to(root).as_posix(): _sha256(path) for path in required_paths},
            configuration={"mode": args.delivery_mode, "files": validate_runtime_config(args.config_directory)},
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        report["errors"] = [str(error)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report["result"] != "PASS":
        print(report["errors"][0], file=sys.stderr)
        return 2
    print(report["candidate"]["tree_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
