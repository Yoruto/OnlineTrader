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

QA_ROOT = Path(__file__).resolve().parent
if str(QA_ROOT) not in sys.path:
    sys.path.insert(0, str(QA_ROOT))

from test_registry import MANIFEST, ROOT, TEST_ROOT, RegistryError, discover_case_data, load_manifest, validate_manifest

SRC_ROOT = ROOT / "src"
for _path in (str(ROOT), str(SRC_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
_existing_pp = os.environ.get("PYTHONPATH", "")
_pp_parts = [p for p in _existing_pp.split(os.pathsep) if p]
for _path in (str(SRC_ROOT), str(ROOT)):
    if _path not in _pp_parts:
        _pp_parts.insert(0, _path)
os.environ["PYTHONPATH"] = os.pathsep.join(_pp_parts)

SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from candidate_identity import CANDIDATE_TREE_ALGORITHM, compute_candidate_identity
from evidence_schema import build_unified_evidence_bundle, write_evidence_bundle


PROFILES = (
    "Unit", "Quick", "Affected", "Trading", "Runtime", "FullOffline",
    "SmokeE2E", "TradingE2E", "TradingExtended", "DesktopE2E",
    "FaultE2E", "StatefulE2E", "SimAll", "ReleaseGate", "LivePreflight",
)
NON_PASS = {"FAIL", "ERROR", "SKIP", "BLOCKED", "NOT_RUN"}

E2E_ROOT = ROOT / "qa" / "scenarios"
E2E_PROFILES: dict[str, tuple[str, ...]] = {
    "SmokeE2E": (
        "smoke/entry_001_5lot_partial_stop_loss.py",
    ),
    "TradingE2E": (
        "smoke/entry_001_5lot_partial_stop_loss.py",
        "convergence/convergence_001_account_target.py",
        "unknown/unknown_001_ack_loss_and_reconcile.py",
        "unknown/unknown_002_negative_reconcile.py",
        "cancel/cancel_001_reprice_and_late_fill.py",
        "exit/exit_001_reject_recovery_and_duplicate_trade.py",
    ),
    "TradingExtended": (
        "smoke/entry_001_5lot_partial_stop_loss.py",
        "unknown/unknown_001_ack_loss_and_reconcile.py",
        "unknown/unknown_002_negative_reconcile.py",
        "cancel/cancel_001_reprice_and_late_fill.py",
        "exit/exit_001_reject_recovery_and_duplicate_trade.py",
        "market_clock/clock_001_discontinuity_disarm_and_block.py",
        "recovery/recovery_001_crash_kill_and_restart.py",
        "recovery/recovery_002_tick_error_and_crossday.py",
        "lifecycle/params_001_live_save_atomicity.py",
    ),
    "DesktopE2E": (
        "lifecycle/desktop_001_workbench_lifecycle_and_trade.py",
        "lifecycle/desktop_002_full_panel_consistency.py",
        "lifecycle/params_001_live_save_atomicity.py",
    ),
    "FaultE2E": (
        "faults/fault_001_multi_contract_stall_and_quiet.py",
        "faults/fault_002_sdk_pump_gap_and_diagnostics.py",
        "faults/fault_003_market_closed_and_projection_offload.py",
        "faults/fault_004_disconnect_reconnect_recovery.py",
        "lifecycle/command_001_idempotency_and_conflict.py",
    ),
    "StatefulE2E": (
        "stateful/stateful_001_seeded_fault_sequence.py",
        "stateful/stateful_002_oracle_negative_control.py",
    ),
}
E2E_PROFILES["SimAll"] = tuple(dict.fromkeys([
    *E2E_PROFILES["TradingE2E"],
    "market_clock/clock_001_discontinuity_disarm_and_block.py",
    "recovery/recovery_001_crash_kill_and_restart.py",
    "recovery/recovery_002_tick_error_and_crossday.py",
    *E2E_PROFILES["FaultE2E"],
    "timeline/timeline_001_accelerated_30min_session.py",
    *E2E_PROFILES["StatefulE2E"],
]))
RELEASE_GATE_E2E_SCENARIOS: tuple[str, ...] = tuple(dict.fromkeys([
    *E2E_PROFILES["SimAll"],
    *E2E_PROFILES["TradingExtended"],
    *E2E_PROFILES["DesktopE2E"],
]))
LIVE_PREFLIGHT_SCRIPTS: tuple[str, ...] = (
    "qa/online_kq_preflight.py",
    "qa/online_clock_reference.py",
    "qa/verify_broker_history_readonly.py",
    "qa/online_simnow_calculate.py",
)


def _git(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ).stdout


def _candidate_identity() -> tuple[str, str]:
    try:
        identity = compute_candidate_identity(ROOT, include_entries=False)
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        raise RegistryError(f"Cannot identify the candidate tree: {error}") from None
    return str(identity["head"]), str(identity["tree_sha256"])


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
    if profile == "Unit":
        selected = sorted(
            module for module, policy in modules.items()
            if policy.get("tier") == "T1"
        )
        reason: dict[str, object] = {"policy": "ALL_T1_UNIT_MODULES", "tier": "T1"}
    elif profile == "FullOffline":
        selected = sorted(modules)
        reason = {"policy": "ALL_REGISTERED_OFFLINE_CASES"}
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
        "scripts/candidate_identity.py": SCRIPTS_ROOT / "candidate_identity.py",
    }
    for module in sorted({str(row["module"]) for row in records}):
        path = TEST_ROOT.joinpath(*module.split(".")).with_suffix(".py")
        assets[path.relative_to(ROOT).as_posix()] = path
    return {name: _sha256(path) for name, path in sorted(assets.items())}


def _execute_e2e_scenarios(rel_paths: tuple[str, ...], e2e_work: Path) -> list[dict[str, object]]:
    from qa.e2e.runner import _load_scenario_module
    from qa.e2e import assert_offline_qa_environment

    assert_offline_qa_environment(block_tqsdk=True)
    results: list[dict[str, object]] = []
    for rel in rel_paths:
        path = E2E_ROOT / rel
        work_dir = e2e_work / path.stem
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            module = _load_scenario_module(path)
            report = module.run(work_dir)
            status = "PASS" if report.get("status") == "PASS" else "FAIL"
            detail = "" if status == "PASS" else json.dumps(report.get("failure"), ensure_ascii=False)
            results.append({
                "test_id": rel,
                "scenario": report.get("scenario"),
                "status": status,
                "rounds_recorded": report.get("rounds_recorded", 0),
                "report_path": str(work_dir / "evidence" / "report.json"),
                "detail": detail,
            })
        except Exception:
            results.append({
                "test_id": rel,
                "scenario": path.stem,
                "status": "ERROR",
                "rounds_recorded": 0,
                "report_path": str(work_dir / "evidence" / "report.json"),
                "detail": RecordingResult._safe_error(sys.exc_info()),
            })
    return results


def _execute_e2e_scenarios_isolated(rel_paths: tuple[str, ...], e2e_work: Path) -> list[dict[str, object]]:
    """Run offline E2E scenarios in a clean subprocess so prior unittest imports cannot taint sys.modules."""
    out_file = e2e_work / "_e2e_results.json"
    script = (
        "import json; from pathlib import Path; import qa.test_runner as tr; "
        f"res = tr._execute_e2e_scenarios({tuple(rel_paths)!r}, Path({str(e2e_work)!r})); "
        f"Path({str(out_file)!r}).write_text(json.dumps(res, ensure_ascii=False), encoding='utf-8')"
    )
    proc = subprocess.run([sys.executable, "-X", "utf8", "-c", script], cwd=str(ROOT))
    if proc.returncode != 0 or not out_file.is_file():
        return [{
            "test_id": "__E2E_SUBPROCESS__",
            "scenario": "e2e_subprocess",
            "status": "ERROR",
            "rounds_recorded": 0,
            "report_path": str(out_file),
            "detail": f"isolated E2E subprocess exited with code {proc.returncode}",
        }]
    return json.loads(out_file.read_text(encoding="utf-8"))


def _execute_unittest_records(records: list[dict[str, object]], blocked_cases: dict[str, str]) -> tuple[list[dict[str, object]], bool]:
    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=RecordingResult)
    try:
        runnable_records = [row for row in records if str(row["test_id"]) not in blocked_cases]
        suite = _load_suite(runnable_records)
        result = runner.run(suite)
    except Exception:
        result = RecordingResult(stream, True, 2)
        result.case_status["__HARNESS__"] = {"status": "ERROR", "detail": RecordingResult._safe_error(sys.exc_info())}

    results: list[dict[str, object]] = []
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
    return results, result.wasSuccessful()


def _emit_unified_bundle(
    *,
    output: Path,
    run_id: str,
    profile: str,
    evidence_source: str,
    candidate_commit: str,
    candidate_tree_sha256: str,
    final_commit: str,
    final_tree_sha256: str,
    candidate_changed: bool,
    environment: dict[str, object],
    runner_scope: dict[str, object],
    counts: dict[str, int],
    result_text: str,
    evidence_status: str,
    artifact_paths: dict[str, Path],
    notes: str | None = None,
) -> Path:
    bundle = build_unified_evidence_bundle(
        output_dir=output,
        run_id=run_id,
        profile=profile,
        evidence_source=evidence_source,
        candidate_commit=candidate_commit,
        candidate_tree_sha256=candidate_tree_sha256,
        candidate_identity_algorithm=CANDIDATE_TREE_ALGORITHM,
        final_candidate_commit=final_commit,
        final_candidate_tree_sha256=final_tree_sha256,
        candidate_changed_during_run=candidate_changed,
        environment=environment,
        runner_scope=runner_scope,
        counts=counts,
        result=result_text,
        evidence_status=evidence_status,
        artifact_paths=artifact_paths,
        notes=notes,
    )
    return write_evidence_bundle(output, bundle)


def _run_e2e_profile(args) -> int:
    rel_paths = E2E_PROFILES[args.profile]
    scenario_paths = [E2E_ROOT / rel for rel in rel_paths]
    missing = [p for p in scenario_paths if not p.is_file()]
    if missing:
        print(f"E2E scenarios missing: {', '.join(str(p) for p in missing)}", file=sys.stderr)
        return 2
    if args.list_only:
        listing = {
            "profile": args.profile,
            "selection_reason": {"policy": "V3_E2E_PROFILE", "profile": args.profile},
            "selected_scenarios": len(scenario_paths),
        }
        if args.show_ids:
            listing["scenarios"] = list(rel_paths)
        print(json.dumps(listing, ensure_ascii=False, indent=2))
        return 0

    try:
        candidate_commit, candidate_tree_sha256 = _candidate_identity()
    except RegistryError as error:
        print(f"test selection blocked: {error}", file=sys.stderr)
        return 2

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output = ROOT / "output" / "tests" / run_id
    e2e_work = output / "e2e"
    (output / "logs").mkdir(parents=True, exist_ok=False)
    e2e_work.mkdir(parents=True, exist_ok=True)

    results = _execute_e2e_scenarios(rel_paths, e2e_work)

    counts = Counter(row["status"] for row in results)
    failed = any(status in NON_PASS for status in counts)
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
                        "detail": "candidate commit or tree hash changed during E2E execution"})
        counts["FAIL"] += 1

    environment = _environment()
    environment.update({
        "candidate_commit": candidate_commit,
        "candidate_tree_sha256": candidate_tree_sha256,
        "candidate_identity_algorithm": CANDIDATE_TREE_ALGORITHM,
        "entry_point": "qa/test_runner.py",
    })
    env_path = output / "environment.json"
    env_path.write_text(json.dumps(environment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    runner_scope = {
        "broker_mode": "OFFLINE_QA_E2E",
        "runner_account_connection": False,
        "runner_real_order_submission": False,
        "packaging": False,
    }
    norm_counts = {status: counts.get(status, 0) for status in ("PASS", "FAIL", "ERROR", "SKIP", "BLOCKED", "NOT_RUN")}
    report_obj = {
        "schema_version": "ONLINE_TRADER_E2E_RESULTS/1",
        "run_id": run_id,
        "profile": args.profile,
        "candidate_commit": candidate_commit,
        "candidate_tree_sha256": candidate_tree_sha256,
        "final_candidate_commit": final_commit,
        "final_candidate_tree_sha256": final_tree_sha256,
        "candidate_changed_during_run": candidate_changed,
        "runner_scope": runner_scope,
        "counts": norm_counts,
        "result": "FAIL" if failed else "PASS",
        "scenarios": results,
    }
    results_path = output / "test-results.json"
    summary_path = output / "summary.json"
    results_path.write_text(json.dumps(report_obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps({
        "result": report_obj["result"],
        "profile": args.profile,
        "candidate_commit": candidate_commit,
        "candidate_tree_sha256": candidate_tree_sha256,
        "counts": norm_counts,
        "selected_scenarios": len(results),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _emit_unified_bundle(
        output=output,
        run_id=run_id,
        profile=args.profile,
        evidence_source="OFFLINE_FAKE_BROKER",
        candidate_commit=candidate_commit,
        candidate_tree_sha256=candidate_tree_sha256,
        final_commit=final_commit,
        final_tree_sha256=final_tree_sha256,
        candidate_changed=candidate_changed,
        environment=environment,
        runner_scope=runner_scope,
        counts=norm_counts,
        result_text=report_obj["result"],
        evidence_status="VERIFIED_LOCAL_OFFLINE",
        artifact_paths={"environment": env_path, "test_results": results_path, "summary": summary_path},
    )
    print(f"{report_obj['result']}: {len(results)} E2E scenarios; {dict(counts)}")
    print(results_path)
    return 1 if failed else 0


def _run_release_gate(args) -> int:
    try:
        discovered, static_policies = discover_case_data()
        manifest = load_manifest()
        problems = validate_manifest(manifest, discovered, static_policies)
        if problems:
            raise RegistryError("; ".join(problems))
        selected_modules, _ = _selected_modules("FullOffline", manifest)
        records = _records(manifest, selected_modules)
        blocked_cases = _preflight_blocked(records)
    except RegistryError as error:
        print(f"release gate blocked: {error}", file=sys.stderr)
        return 2

    missing_e2e = [E2E_ROOT / rel for rel in RELEASE_GATE_E2E_SCENARIOS if not (E2E_ROOT / rel).is_file()]
    if missing_e2e:
        print(f"ReleaseGate scenarios missing: {', '.join(str(p) for p in missing_e2e)}", file=sys.stderr)
        return 2

    if args.list_only:
        listing = {
            "profile": "ReleaseGate",
            "selection_reason": {"policy": "RELEASE_GATE_OFFLINE_ONLY", "packaging": False, "live_broker": False},
            "selected_unittest_cases": len(records),
            "selected_e2e_scenarios": len(RELEASE_GATE_E2E_SCENARIOS),
            "blocked_preflight_count": len(blocked_cases),
        }
        if args.show_ids:
            listing["unittest_ids"] = [row["test_id"] for row in records]
            listing["e2e_scenarios"] = list(RELEASE_GATE_E2E_SCENARIOS)
        print(json.dumps(listing, ensure_ascii=False, indent=2))
        return 0

    try:
        candidate_commit, candidate_tree_sha256 = _candidate_identity()
    except RegistryError as error:
        print(f"release gate blocked: {error}", file=sys.stderr)
        return 2

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output = ROOT / "output" / "tests" / run_id
    e2e_work = output / "e2e"
    (output / "logs").mkdir(parents=True, exist_ok=False)
    e2e_work.mkdir(parents=True, exist_ok=True)
    (output / "manifest.json").write_bytes(MANIFEST.read_bytes())
    test_asset_hashes = _test_asset_hashes(records)

    unittest_results, unittest_ok = _execute_unittest_records(records, blocked_cases)
    e2e_results = _execute_e2e_scenarios_isolated(RELEASE_GATE_E2E_SCENARIOS, e2e_work)

    combined = unittest_results + e2e_results
    counts = Counter(row["status"] for row in combined)
    failed = any(status in NON_PASS for status in counts) or not unittest_ok
    try:
        final_commit, final_tree_sha256 = _candidate_identity()
    except RegistryError as error:
        final_commit, final_tree_sha256 = "UNAVAILABLE", "UNAVAILABLE"
        failed = True
        combined.append({"test_id": "__CANDIDATE_IDENTITY__", "status": "ERROR", "detail": str(error)})
        counts["ERROR"] += 1
    candidate_changed = (final_commit != candidate_commit or final_tree_sha256 != candidate_tree_sha256)
    if candidate_changed:
        failed = True
        combined.append({"test_id": "__CANDIDATE_CHANGED_DURING_RUN__", "status": "FAIL",
                         "detail": "candidate commit or tree hash changed during ReleaseGate"})
        counts["FAIL"] += 1

    environment = _environment()
    environment.update({
        "candidate_commit": candidate_commit,
        "candidate_tree_sha256": candidate_tree_sha256,
        "candidate_identity_algorithm": CANDIDATE_TREE_ALGORITHM,
        "entry_point": "qa/test_runner.py",
    })
    env_path = output / "environment.json"
    env_path.write_text(json.dumps(environment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    runner_scope = {
        "broker_mode": "OFFLINE_RELEASE_GATE",
        "runner_account_connection": False,
        "runner_real_order_submission": False,
        "packaging": False,
    }
    norm_counts = {status: counts.get(status, 0) for status in ("PASS", "FAIL", "ERROR", "SKIP", "BLOCKED", "NOT_RUN")}
    result_text = "FAIL" if failed else "PASS"
    results_path = output / "test-results.json"
    summary_path = output / "summary.json"
    hashes_path = output / "evidence-hashes.json"
    results_path.write_text(json.dumps({
        "schema_version": "ONLINE_TRADER_RELEASE_GATE/1",
        "run_id": run_id,
        "profile": "ReleaseGate",
        "candidate_commit": candidate_commit,
        "candidate_tree_sha256": candidate_tree_sha256,
        "final_candidate_commit": final_commit,
        "final_candidate_tree_sha256": final_tree_sha256,
        "candidate_changed_during_run": candidate_changed,
        "manifest_sha256": _sha256(MANIFEST),
        "test_asset_sha256": test_asset_hashes,
        "runner_scope": runner_scope,
        "counts": norm_counts,
        "result": result_text,
        "unittest_cases": unittest_results,
        "e2e_scenarios": e2e_results,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    hashes_path.write_text(json.dumps({
        "candidate_commit": candidate_commit,
        "candidate_tree_sha256": candidate_tree_sha256,
        "candidate_identity_algorithm": CANDIDATE_TREE_ALGORITHM,
        "final_candidate_commit": final_commit,
        "final_candidate_tree_sha256": final_tree_sha256,
        "manifest_sha256": _sha256(MANIFEST),
        "test_asset_sha256": test_asset_hashes,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps({
        "result": result_text,
        "profile": "ReleaseGate",
        "candidate_commit": candidate_commit,
        "candidate_tree_sha256": candidate_tree_sha256,
        "counts": norm_counts,
        "selected_unittest_cases": len(records),
        "selected_e2e_scenarios": len(RELEASE_GATE_E2E_SCENARIOS),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    bundle_path = _emit_unified_bundle(
        output=output,
        run_id=run_id,
        profile="ReleaseGate",
        evidence_source="OFFLINE_FAKE_BROKER",
        candidate_commit=candidate_commit,
        candidate_tree_sha256=candidate_tree_sha256,
        final_commit=final_commit,
        final_tree_sha256=final_tree_sha256,
        candidate_changed=candidate_changed,
        environment=environment,
        runner_scope=runner_scope,
        counts=norm_counts,
        result_text=result_text,
        evidence_status="VERIFIED_LOCAL_OFFLINE",
        artifact_paths={
            "environment": env_path,
            "test_results": results_path,
            "summary": summary_path,
            "evidence_hashes": hashes_path,
        },
        notes="Offline release qualification only; live SimNow and packaged EXE acceptance require separate verification.",
    )
    print(f"{result_text}: ReleaseGate ({len(records)} unittest + {len(RELEASE_GATE_E2E_SCENARIOS)} E2E); {dict(counts)}")
    print(bundle_path)
    return 1 if failed else 0


def _run_live_preflight(args) -> int:
    if args.list_only:
        listing = {
            "profile": "LivePreflight",
            "selection_reason": {
                "policy": "LIVE_PREFLIGHT_AUTHORIZATION_GATED",
                "requires_explicit_authorize_live": True,
                "runner_account_connection": False,
                "runner_real_order_submission": False,
            },
            "preflight_scripts": list(LIVE_PREFLIGHT_SCRIPTS),
        }
        print(json.dumps(listing, ensure_ascii=False, indent=2))
        return 0

    try:
        candidate_commit, candidate_tree_sha256 = _candidate_identity()
    except RegistryError as error:
        print(f"live preflight blocked: {error}", file=sys.stderr)
        return 2

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output = ROOT / "output" / "tests" / run_id
    (output / "logs").mkdir(parents=True, exist_ok=False)
    environment = _environment()
    environment.update({
        "candidate_commit": candidate_commit,
        "candidate_tree_sha256": candidate_tree_sha256,
        "candidate_identity_algorithm": CANDIDATE_TREE_ALGORITHM,
        "entry_point": "qa/test_runner.py",
    })
    env_path = output / "environment.json"
    env_path.write_text(json.dumps(environment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    runner_scope = {
        "broker_mode": "LIVE_PREFLIGHT_GATE",
        "runner_account_connection": False,
        "runner_real_order_submission": False,
        "packaging": False,
        "authorize_live_flag": bool(args.authorize_live),
    }

    if not args.authorize_live:
        norm_counts = {"PASS": 0, "FAIL": 0, "ERROR": 0, "SKIP": 0, "BLOCKED": 0, "NOT_RUN": len(LIVE_PREFLIGHT_SCRIPTS)}
        bundle_path = _emit_unified_bundle(
            output=output,
            run_id=run_id,
            profile="LivePreflight",
            evidence_source="SIMNOW_BROKER",
            candidate_commit=candidate_commit,
            candidate_tree_sha256=candidate_tree_sha256,
            final_commit=candidate_commit,
            final_tree_sha256=candidate_tree_sha256,
            candidate_changed=False,
            environment=environment,
            runner_scope=runner_scope,
            counts=norm_counts,
            result_text="NOT_RUN",
            evidence_status="NOT_RUN_REQUIRES_AUTHORIZATION",
            artifact_paths={"environment": env_path},
            notes="Pass --authorize-live only with explicit user authorization for live preflight checks.",
        )
        print(f"NOT_RUN: LivePreflight requires --authorize-live ({bundle_path})")
        return 2

    # Even with --authorize-live, test_runner.py performs static/syntax readiness checks on live preflight scripts
    # without connecting or sending orders; actual SimNow sessions must be invoked explicitly via their dedicated CLI.
    checks = []
    for rel in LIVE_PREFLIGHT_SCRIPTS:
        path = ROOT / rel
        if not path.is_file():
            checks.append({"test_id": rel, "status": "FAIL", "detail": "missing preflight script"})
            continue
        try:
            import ast
            ast.parse(path.read_text(encoding="utf-8"))
            checks.append({"test_id": rel, "status": "PASS", "detail": "static preflight syntax ok"})
        except Exception as error:
            checks.append({"test_id": rel, "status": "ERROR", "detail": str(error)})

    counts = Counter(row["status"] for row in checks)
    failed = any(status in NON_PASS for status in counts)
    norm_counts = {status: counts.get(status, 0) for status in ("PASS", "FAIL", "ERROR", "SKIP", "BLOCKED", "NOT_RUN")}
    result_text = "FAIL" if failed else "PASS"
    results_path = output / "test-results.json"
    results_path.write_text(json.dumps({
        "schema_version": "ONLINE_TRADER_LIVE_PREFLIGHT/1",
        "run_id": run_id,
        "profile": "LivePreflight",
        "candidate_commit": candidate_commit,
        "candidate_tree_sha256": candidate_tree_sha256,
        "runner_scope": runner_scope,
        "counts": norm_counts,
        "result": result_text,
        "checks": checks,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    bundle_path = _emit_unified_bundle(
        output=output,
        run_id=run_id,
        profile="LivePreflight",
        evidence_source="SIMNOW_BROKER",
        candidate_commit=candidate_commit,
        candidate_tree_sha256=candidate_tree_sha256,
        final_commit=candidate_commit,
        final_tree_sha256=candidate_tree_sha256,
        candidate_changed=False,
        environment=environment,
        runner_scope=runner_scope,
        counts=norm_counts,
        result_text=result_text,
        evidence_status="VERIFIED_PREFLIGHT_ONLY",
        artifact_paths={"environment": env_path, "test_results": results_path},
        notes="Static live preflight check passed; live SimNow execution remains separate and requires explicit session grant.",
    )
    print(f"{result_text}: LivePreflight static check ({len(checks)} scripts); {bundle_path}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run explicitly selected offline unittest or E2E profiles.")
    parser.add_argument("--profile", required=True, choices=PROFILES)
    parser.add_argument("--list-only", action="store_true", help="select and print cases without running tests")
    parser.add_argument("--show-ids", action="store_true", help="include each selected test ID in list-only output")
    parser.add_argument("--base-ref", help="base commit for evidence; Affected still falls back to FullOffline")
    parser.add_argument("--authorize-live", action="store_true", help="required for LivePreflight execution")
    args = parser.parse_args(argv)
    if args.profile in E2E_PROFILES:
        return _run_e2e_profile(args)
    if args.profile == "ReleaseGate":
        return _run_release_gate(args)
    if args.profile == "LivePreflight":
        return _run_live_preflight(args)
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
                        "candidate_identity_algorithm": CANDIDATE_TREE_ALGORITHM,
                        "entry_point": "qa/test_runner.py"})
    env_path = output / "environment.json"
    env_path.write_text(json.dumps(environment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    results, unittest_ok = _execute_unittest_records(records, blocked_cases)
    with (output / "logs" / "run.log").open("w", encoding="utf-8") as log:
        for row in results:
            log.write(f"{row['status']}\t{row['test_id']}\n")
    counts = Counter(row["status"] for row in results)
    failed = any(status in NON_PASS for status in counts) or not unittest_ok
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
    runner_scope = {
        "broker_mode": "OFFLINE_MANIFEST_ONLY",
        "runner_account_connection": False,
        "runner_real_order_submission": False,
        "process_network_sandbox": False,
        "packaging": False,
    }
    norm_counts = {status: counts.get(status, 0) for status in ("PASS", "FAIL", "ERROR", "SKIP", "BLOCKED", "NOT_RUN")}
    report = {
        "schema_version": "ONLINE_TRADER_TEST_RESULTS/1",
        "run_id": run_id,
        "candidate_commit": candidate_commit,
        "candidate_tree_sha256": candidate_tree_sha256,
        "candidate_identity_algorithm": CANDIDATE_TREE_ALGORITHM,
        "final_candidate_commit": final_commit,
        "final_candidate_tree_sha256": final_tree_sha256,
        "candidate_changed_during_run": candidate_changed,
        "profile": args.profile,
        "selection_reason": selection_reason,
        "manifest_sha256": _sha256(MANIFEST),
        "test_asset_sha256": test_asset_hashes,
        "runner_scope": runner_scope,
        "environment": environment,
        "counts": norm_counts,
        "result": "FAIL" if failed else "PASS",
        "cases": results,
    }
    results_path = output / "test-results.json"
    hashes_path = output / "evidence-hashes.json"
    summary_path = output / "summary.json"
    results_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    hashes_path.write_text(json.dumps({
        "candidate_commit": candidate_commit,
        "candidate_tree_sha256": candidate_tree_sha256,
        "candidate_identity_algorithm": CANDIDATE_TREE_ALGORITHM,
        "final_candidate_commit": final_commit,
        "final_candidate_tree_sha256": final_tree_sha256,
        "manifest_sha256": _sha256(MANIFEST),
        "test_asset_sha256": test_asset_hashes,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps({
        "result": report["result"],
        "profile": args.profile,
        "candidate_commit": candidate_commit,
        "candidate_tree_sha256": candidate_tree_sha256,
        "candidate_identity_algorithm": CANDIDATE_TREE_ALGORITHM,
        "counts": norm_counts,
        "selected_cases": len(records),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _emit_unified_bundle(
        output=output,
        run_id=run_id,
        profile=args.profile,
        evidence_source="OFFLINE_UNITTEST",
        candidate_commit=candidate_commit,
        candidate_tree_sha256=candidate_tree_sha256,
        final_commit=final_commit,
        final_tree_sha256=final_tree_sha256,
        candidate_changed=candidate_changed,
        environment=environment,
        runner_scope=runner_scope,
        counts=norm_counts,
        result_text=report["result"],
        evidence_status="VERIFIED_LOCAL_OFFLINE",
        artifact_paths={
            "environment": env_path,
            "test_results": results_path,
            "summary": summary_path,
            "evidence_hashes": hashes_path,
        },
    )
    print(f"{report['result']}: {len(records)} selected cases; {dict(counts)}")
    print(results_path)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
