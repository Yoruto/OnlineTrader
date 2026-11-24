"""Fast, side-effect-free release identity and configuration preflight."""

from __future__ import annotations

import argparse
import ast
import importlib.metadata
import json
from pathlib import Path
import platform
import re
import struct
import subprocess
import sys
import tomllib

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from candidate_identity import compute_candidate_identity, sha256_file


SCHEMA = "ONLINE_TRADER_RELEASE_PREFLIGHT/1"
REQUIRED_RUNTIME_FILES = (
    "runtime.json",
    "OnlineTrader-parameters.json",
    "private/OnlineTrader-defaults.json",
    "private/diagnostics-upload.json",
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
_CONFIG_PARSER_PROBE = """
import json
from pathlib import Path
import sys

src_root = Path(sys.argv[1]).resolve()
config_dir = Path(sys.argv[2]).resolve()
sys.modules["PySide6"] = None
sys.modules["tqsdk"] = None
sys.path.insert(0, str(src_root))

import online_trader
if not Path(online_trader.__file__).resolve().is_relative_to(src_root):
    raise SystemExit("online_trader imported outside candidate source root")

from online_trader.errors import TraderError
from online_trader.runtime.portable_config import PortableAccountStore

try:
    store = PortableAccountStore(config_dir)
    state = store.read()
    for row in state["accounts"]:
        store.credential_reference(row["account_id"]).resolve()
    profile = store.profile()
except (TraderError, OSError, ValueError, KeyError, TypeError) as error:
    print(str(error), file=sys.stderr)
    raise SystemExit(2)

print(json.dumps({
    "parser_verified": True,
    "account_count": len(state["accounts"]),
    "providers": sorted({row["provider"] for row in state["accounts"]}),
    "selected_account_id": state["selected_id"],
    "has_overrides": (config_dir / "OnlineTrader-overrides.json").is_file(),
    "profile_hash": profile.content_hash,
}, sort_keys=True))
""".strip()


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


def _validate_config_with_product_parser(directory: Path, source_root: Path) -> dict[str, object]:
    source_src = source_root / "src"
    if not (source_src / "online_trader").is_dir():
        raise ValueError(f"Candidate source is missing src/online_trader: {source_root}")
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", _CONFIG_PARSER_PROBE, str(source_src), str(directory)],
            cwd=source_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        raise ValueError("Runtime configuration parser probe timed out.") from None
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        message = detail[-1] if detail else f"exit code {completed.returncode}"
        raise ValueError(f"Runtime configuration failed product validation: {message}")
    try:
        summary = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise ValueError("Runtime configuration parser probe returned invalid JSON.") from None
    if not isinstance(summary, dict) or summary.get("parser_verified") is not True:
        raise ValueError("Runtime configuration parser probe returned an invalid summary.")
    return summary


def validate_runtime_config(directory: Path, source_root: Path) -> tuple[dict[str, str], dict[str, object]]:
    directory = directory.resolve()
    if not directory.is_dir():
        raise ValueError(f"Runtime config directory does not exist: {directory}")
    allowed = set(REQUIRED_RUNTIME_FILES) | set(OPTIONAL_RUNTIME_FILES)
    files: list[Path] = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or path.is_junction():
            raise ValueError(f"Runtime config contains a link: {path.relative_to(directory).as_posix()}")
        if path.is_file():
            files.append(path)
    relative = {path.relative_to(directory).as_posix() for path in files}
    missing = set(REQUIRED_RUNTIME_FILES) - relative
    unexpected = relative - allowed
    if missing:
        raise ValueError("Runtime config is missing: " + ", ".join(sorted(missing)))
    if unexpected:
        raise ValueError("Runtime config contains unexpected files: " + ", ".join(sorted(unexpected)))
    for name in sorted(relative):
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
    file_hashes = {name: sha256_file(directory / name) for name in sorted(relative)}
    summary = _validate_config_with_product_parser(directory, source_root)
    sys.path.insert(0, str((source_root / "src").resolve()))
    try:
        from online_trader.diagnostics.backend import load_upload_config
        upload = load_upload_config(directory)
    finally:
        sys.path.pop(0)
    if upload is None:
        raise ValueError("Runtime diagnostics upload configuration must be enabled.")
    summary["diagnostics_upload"] = {
        "host": upload.host,
        "project_id": upload.project_id,
        "project_path": upload.project_path,
        "package_name": upload.package_name,
    }
    return file_hashes, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-source", type=Path, default=Path.cwd())
    parser.add_argument("--delivery-mode", choices=("RuntimeConfigured", "UpdatePayload"), required=True)
    parser.add_argument("--config-directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.delivery_mode == "RuntimeConfigured" and args.config_directory is None:
        parser.error("--config-directory is required for RuntimeConfigured")
    if args.delivery_mode == "UpdatePayload" and args.config_directory is not None:
        parser.error("--config-directory must not be supplied for UpdatePayload")
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
            root / "scripts" / "candidate_identity.py",
            root / "scripts" / "freeze-portable.ps1",
            root / "scripts" / "portable_delivery.py",
        )
        absent = [path.relative_to(root).as_posix() for path in required_paths if not path.is_file()]
        if absent:
            raise ValueError("Missing release inputs: " + ", ".join(absent))
        if args.delivery_mode == "RuntimeConfigured":
            config_files, config_summary = validate_runtime_config(args.config_directory, root)
            config_block: dict[str, object] = {
                "mode": args.delivery_mode,
                "files": config_files,
                "validated_summary": config_summary,
            }
        else:
            config_block = {"mode": args.delivery_mode, "standalone": False, "files": {}}
        report.update(
            result="PASS",
            candidate=compute_candidate_identity(root),
            version=actual,
            platform={"python": platform.python_version(), "architecture": platform.machine()},
            toolchain=installed,
            release_inputs={path.relative_to(root).as_posix(): sha256_file(path) for path in required_paths},
            configuration=config_block,
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
