"""Risk-oriented offline unittest runner driven by the static manifest."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.metadata
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import unittest
import uuid
import traceback

from test_registry import MANIFEST, ROOT, TEST_ROOT, RegistryError, discover_case_data, load_manifest, validate_manifest


PROFILES = ("Quick", "Affected", "Trading", "Runtime", "FullOffline")
NON_PASS = {"FAIL", "ERROR", "SKIP", "BLOCKED", "NOT_RUN"}


def _git(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ).stdout


def _candidate_identity() -> tuple[str, str]:
    try:
        commit = _git("rev-parse", "HEAD").decode("ascii").strip()
        paths = set(_git("ls-files", "-z").decode("utf-8", "surrogateescape").split("\0"))
        paths.update(_git("ls-files", "--others", "--exclude-standard", "-z").decode("utf-8", "surrogateescape").split("\0"))
    except (OSError, subprocess.CalledProcessError) as error:
        raise RegistryError(f"Cannot identify the candidate tree: {error}") from None
    digest = hashlib.sha256()
    for relative in sorted(path for path in paths if path):
        path = ROOT / relative
        if not path.is_file():
            continue
        payload = path.read_bytes()
        digest.update(relative.replace("\\", "/").encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(hashlib.sha256(payload).digest())
    return commit, digest.hexdigest()


def _changed_paths(base_ref: str | None) -> list[str]:
    paths: set[str] = set()
    try:
        if base_ref:
            paths.update(_git("diff", "--name-only", "-z", f"{base_ref}...HEAD").decode("utf-8", "surrogateescape").split("\0"))
        paths.update(_git("diff", "--name-only", "-z", "HEAD").decode("utf-8", "surrogateescape").split("\0"))
        paths.update(_git("diff", "--cached", "--name-only", "-z").decode("utf-8", "surrogateescape").split("\0"))
        paths.update(_git("ls-files", "--others", "--exclude-standard", "-z").decode("utf-8", "surrogateescape").split("\0"))
    except (OSError, subprocess.CalledProcessError) as error:
        raise RegistryError(f"Cannot determine changed paths: {error}") from None
    return sorted(path.replace("\\", "/") for path in paths if path)


def _selected_modules(profile: str, manifest: dict[str, object]) -> tuple[list[str], dict[str, object]]:
    modules = manifest["modules"]
    assert isinstance(modules, dict)
    if profile == "FullOffline":
        selected = sorted(modules)
        reason: dict[str, object] = {"policy": "ALL_REGISTERED_OFFLINE_CASES"}
    elif profile == "Affected":
        # The checked-in path-to-suite map is not yet independently verified.
        # Fail closed to the full offline suite until that evidence exists.
        changed = _changed_paths(None)
        selected = sorted(modules)
        reason = {
            "policy": "AFFECTED_MAP_UNVERIFIED_FULL_OFFLINE",
            "changed_paths": changed,
            "base_ref": None,
        }
    else:
        selected = sorted(
            module for module, policy in modules.items()
            if profile in policy.get("profiles", [])
        )
        reason = {"policy": "REGISTERED_PROFILE_TAGS", "profile": profile}
    return selected, reason


def _records(manifest: dict[str, object], selected_modules: list[str]) -> list[dict[str, object]]:
    modules = manifest["modules"]
    assert isinstance(modules, dict)
    rows: list[dict[str, object]] = []
    for module in selected_modules:
        policy = modules[module]
        for case in policy["cases"]:
            override = policy.get("case_overrides", {}).get(case, {})
            rows.append({
                "test_id": f"{module}.{case}",
                "module": module,
                "case": case,
                "tier": policy["tier"],
                "domains": policy["domains"],
                "risk": policy["risk"],
                "profiles": policy["profiles"],
                "platform": override.get("platform", policy["platform"]),
                "broker_mode": policy["broker_mode"],
                "legacy": policy["legacy"],
                "requirements": override.get("requirements", policy["requirements"]),
                "timeout_seconds": policy["timeout_seconds"],
            })
    return sorted(rows, key=lambda row: str(row["test_id"]))


def _preflight_blocked(records: list[dict[str, object]]) -> dict[str, str]:
    blocked: dict[str, str] = {}
    cache: dict[str, bool] = {}
    current_platform = platform.system()
    for row in records:
        supported = row["platform"]
        if current_platform not in supported:
            blocked[str(row["test_id"])] = f"unsupported platform: {current_platform}; requires one of {', '.join(supported)}"
            continue
        missing = []
        for requirement in row["requirements"]:
            name = str(requirement)
            if name not in cache:
                try:
                    cache[name] = importlib.util.find_spec(name) is not None
                except (ImportError, ModuleNotFoundError, ValueError):
                    cache[name] = False
            if not cache[name]:
                missing.append(name)
        if missing:
            blocked[str(row["test_id"])] = "missing requirements: " + ", ".join(missing)
    return blocked


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.case_status: dict[str, dict[str, str]] = {}

    def _set(self, test: unittest.case.TestCase, status: str, detail: str = "") -> None:
        test_id = test.id()
        prior = self.case_status.get(test_id)
        if prior is None or status != "PASS":
            self.case_status[test_id] = {"status": status, "detail": detail}

    @staticmethod
    def _safe_error(err) -> str:
        frames = traceback.extract_tb(err[2])
        frame_text = ",".join(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in frames[-8:])
        message_hash = hashlib.sha256(str(err[1]).encode("utf-8", "replace")).hexdigest()
        return f"{err[0].__name__}; frames=[{frame_text}]; message_sha256={message_hash}"

    def addSuccess(self, test):
        super().addSuccess(test)
        self._set(test, "PASS")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._set(test, "FAIL", self._safe_error(err))

    def addError(self, test, err):
        super().addError(test, err)
        self._set(test, "ERROR", self._safe_error(err))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._set(test, "SKIP", str(reason))

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._set(test, "SKIP", "expected failure: " + self._safe_error(err))

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._set(test, "FAIL", "unexpected success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._set(test, "FAIL", self._safe_error(err))


def _load_suite(records: list[dict[str, object]]) -> unittest.TestSuite:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(TEST_ROOT))
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    cache: dict[str, object] = {}
    for row in records:
        module_name = str(row["module"])
        module = cache.get(module_name)
        if module is None:
            module = importlib.import_module(module_name)
            cache[module_name] = module
        case_name = str(row["case"])
        suite.addTests(loader.loadTestsFromName(case_name, module))
    return suite


def _environment() -> dict[str, object]:
    packages = {}
    for package in ("PySide6", "tqsdk", "jsonschema", "referencing"):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = None
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "os_name": os.name,
        "packages": packages,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _test_asset_hashes(records: list[dict[str, object]]) -> dict[str, str]:
    assets = {
        "qa/test_runner.py": ROOT / "qa" / "test_runner.py",
        "qa/test_registry.py": ROOT / "qa" / "test_registry.py",
        "qa/test_manifest.json": MANIFEST,
    }
    for module in sorted({str(row["module"]) for row in records}):
        path = TEST_ROOT.joinpath(*module.split(".")).with_suffix(".py")
        assets[path.relative_to(ROOT).as_posix()] = path
    return {name: _sha256(path) for name, path in sorted(assets.items())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run explicitly selected offline unittest profiles.")
    parser.add_argument("--profile", required=True, choices=PROFILES)
    parser.add_argument("--list-only", action="store_true", help="select and print cases without running tests")
    parser.add_argument("--show-ids", action="store_true", help="include each selected test ID in list-only output")
    parser.add_argument("--base-ref", help="base commit for evidence; Affected still falls back to FullOffline")
    args = parser.parse_args(argv)
    try:
        discovered, static_policies = discover_case_data()
        manifest = load_manifest()
        problems = validate_manifest(manifest, discovered, static_policies)
        if problems:
            raise RegistryError("; ".join(problems))
        selected_modules, selection_reason = _selected_modules(args.profile, manifest)
        records = _records(manifest, selected_modules)
        if not records:
            raise RegistryError(f"Profile {args.profile} selects no registered cases")
        if any(row["broker_mode"] != "OFFLINE" for row in records):
            raise RegistryError("PR1 runner refuses online broker modes")
        blocked_cases = _preflight_blocked(records)
        if args.profile == "Affected" and args.base_ref:
            selection_reason["base_ref"] = args.base_ref
            selection_reason["changed_paths"] = _changed_paths(args.base_ref)
    except RegistryError as error:
        print(f"test selection blocked: {error}", file=sys.stderr)
        return 2

    if args.list_only:
        listing = {"profile": args.profile, "selection_reason": selection_reason,
                   "selected_cases": len(records),
                   "selected_modules": len(selected_modules),
                   "blocked_preflight_count": len(blocked_cases)}
        if args.show_ids:
            listing["module_names"] = selected_modules
            listing["test_ids"] = [row["test_id"] for row in records]
        print(json.dumps(listing,
                         ensure_ascii=False, indent=2))
        return 0

    try:
        candidate_commit, candidate_tree_sha256 = _candidate_identity()
    except RegistryError as error:
        print(f"test selection blocked: {error}", file=sys.stderr)
        return 2
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output = ROOT / "output" / "tests" / run_id
    (output / "logs").mkdir(parents=True, exist_ok=False)
    (output / "manifest.json").write_bytes(MANIFEST.read_bytes())
    test_asset_hashes = _test_asset_hashes(records)
    (output / "selection.json").write_text(json.dumps({
        "profile": args.profile,
        "selection_reason": selection_reason,
        "selected_cases": len(records),
        "selected_test_ids": [row["test_id"] for row in records],
        "manifest_sha256": _sha256(MANIFEST),
        "test_asset_sha256": test_asset_hashes,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    environment = _environment()
    environment.update({"candidate_commit": candidate_commit,
                        "candidate_tree_sha256": candidate_tree_sha256,
                        "entry_point": "qa/test_runner.py"})
    (output / "environment.json").write_text(json.dumps(environment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=RecordingResult)
    try:
        runnable_records = [row for row in records if str(row["test_id"]) not in blocked_cases]
        suite = _load_suite(runnable_records)
        result = runner.run(suite)
    except Exception as error:
        result = RecordingResult(stream, True, 2)
        result.case_status["__HARNESS__"] = {"status": "ERROR", "detail": RecordingResult._safe_error(sys.exc_info())}

    results = []
    for row in records:
        test_id = str(row["test_id"])
        observed = result.case_status.get(test_id)
        if observed is None and test_id in blocked_cases:
            observed = {"status": "BLOCKED", "detail": blocked_cases[test_id]}
        if observed is None:
            observed = {"status": "NOT_RUN", "detail": "selected case produced no result"}
        results.append({**row, **observed})
    if "__HARNESS__" in result.case_status:
        results.append({"test_id": "__HARNESS__", **result.case_status["__HARNESS__"]})
    with (output / "logs" / "run.log").open("w", encoding="utf-8") as log:
        for row in results:
            log.write(f"{row['status']}\t{row['test_id']}\n")
    counts = Counter(row["status"] for row in results)
    failed = any(status in NON_PASS for status in counts) or not result.wasSuccessful()
    try:
        final_commit, final_tree_sha256 = _candidate_identity()
    except RegistryError as error:
        final_commit, final_tree_sha256 = "UNAVAILABLE", "UNAVAILABLE"
        failed = True
        results.append({"test_id": "__CANDIDATE_IDENTITY__", "status": "ERROR", "detail": str(error)})
        counts["ERROR"] += 1
    candidate_changed = (final_commit != candidate_commit or final_tree_sha256 != candidate_tree_sha256)
    if candidate_changed:
        failed = True
        results.append({"test_id": "__CANDIDATE_CHANGED_DURING_RUN__", "status": "FAIL",
                        "detail": "candidate commit or tracked/untracked tree hash changed during test execution"})
        counts["FAIL"] += 1
    report = {
        "schema_version": "ONLINE_TRADER_TEST_RESULTS/1",
        "run_id": run_id,
        "candidate_commit": candidate_commit,
        "candidate_tree_sha256": candidate_tree_sha256,
        "final_candidate_commit": final_commit,
        "final_candidate_tree_sha256": final_tree_sha256,
        "candidate_changed_during_run": candidate_changed,
        "profile": args.profile,
        "selection_reason": selection_reason,
        "manifest_sha256": _sha256(MANIFEST),
        "test_asset_sha256": test_asset_hashes,
        "runner_scope": {
            "broker_mode": "OFFLINE_MANIFEST_ONLY",
            "runner_account_connection": False,
            "runner_real_order_submission": False,
            "process_network_sandbox": False,
            "packaging": False,
        },
        "environment": environment,
        "counts": {status: counts.get(status, 0) for status in ("PASS", "FAIL", "ERROR", "SKIP", "BLOCKED", "NOT_RUN")},
        "result": "FAIL" if failed else "PASS",
        "cases": results,
    }
    (output / "test-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "evidence-hashes.json").write_text(json.dumps({
        "candidate_commit": candidate_commit,
        "candidate_tree_sha256": candidate_tree_sha256,
        "final_candidate_commit": final_commit,
        "final_candidate_tree_sha256": final_tree_sha256,
        "manifest_sha256": _sha256(MANIFEST),
        "test_asset_sha256": test_asset_hashes,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "summary.json").write_text(json.dumps({
        "result": report["result"],
        "profile": args.profile,
        "candidate_commit": candidate_commit,
        "candidate_tree_sha256": candidate_tree_sha256,
        "counts": report["counts"],
        "selected_cases": len(records),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{report['result']}: {len(records)} selected cases; {dict(counts)}")
    print(output / "test-results.json")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
