"""Static, import-free inventory for the unittest test manifest.

This module reads test source with ``ast`` only. It never imports a test module,
so checking registry completeness cannot connect an SDK or start product code.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "tests"
MANIFEST = ROOT / "qa" / "test_manifest.json"
SCHEMA = "ONLINE_TRADER_TEST_MANIFEST/1"


class RegistryError(ValueError):
    pass


def _dotted(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(TEST_ROOT).with_suffix("").parts)


def _imports(tree: ast.Module) -> tuple[set[str], set[str]]:
    unittest_modules: set[str] = set()
    testcase_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "unittest":
                    unittest_modules.add(alias.asname or "unittest")
        elif isinstance(node, ast.ImportFrom) and node.module == "unittest":
            for alias in node.names:
                if alias.name == "TestCase":
                    testcase_names.add(alias.asname or alias.name)
    return unittest_modules, testcase_names


def _is_testcase_base(base: ast.expr, modules: set[str], names: set[str]) -> bool:
    if isinstance(base, ast.Name):
        return base.id in names
    if isinstance(base, ast.Attribute) and base.attr == "TestCase":
        owner = _dotted(base.value)
        return owner in modules or owner in {f"{module}.case" for module in modules}
    return False


def _case_methods(tree: ast.Module, modules: set[str], names: set[str]) -> dict[str, list[str]]:
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    class_by_name = {node.name: node for node in classes}
    testcase_classes: set[str] = set()
    methods: dict[str, set[str]] = {
        node.name: {
            child.name for child in node.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and child.name.startswith("test")
        }
        for node in classes
    }

    changed = True
    while changed:
        changed = False
        for node in classes:
            if node.name in testcase_classes:
                continue
            if any(
                _is_testcase_base(base, modules, names)
                or (isinstance(base, ast.Name) and base.id in testcase_classes)
                for base in node.bases
            ):
                testcase_classes.add(node.name)
                changed = True

    # unittest discovers inherited ``test*`` methods too. Resolve local test
    # base classes to preserve those IDs without importing their modules.
    def inherited(class_name: str, seen: set[str] | None = None) -> set[str]:
        seen = set() if seen is None else seen
        if class_name in seen or class_name not in class_by_name:
            return set()
        seen.add(class_name)
        node = class_by_name[class_name]
        result = set(methods[class_name])
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id in class_by_name:
                result.update(inherited(base.id, seen))
        return result

    output: dict[str, list[str]] = {}
    for class_name in sorted(testcase_classes):
        names_for_class = inherited(class_name)
        if names_for_class:
            output[class_name] = sorted(names_for_class)
    return output


def _case_overrides(tree: ast.Module, classes: dict[str, list[str]]) -> dict[str, dict[str, object]]:
    class_nodes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    overrides: dict[str, dict[str, object]] = {}

    def platform_when_true(condition: ast.AST) -> list[str] | None:
        if not isinstance(condition, ast.Compare) or len(condition.ops) != 1 or len(condition.comparators) != 1:
            return None
        left, right = condition.left, condition.comparators[0]
        left_name, right_name = _dotted(left), _dotted(right)
        if isinstance(left, ast.Constant) and isinstance(left.value, str):
            left_name = left.value
        if isinstance(right, ast.Constant) and isinstance(right.value, str):
            right_name = right.value
        if {left_name, right_name} == {"sys.platform", "win32"}:
            true_platform = ["Windows"]
        elif {left_name, right_name} == {"os.name", "nt"}:
            true_platform = ["Windows"]
        else:
            return None
        if isinstance(condition.ops[0], ast.Eq):
            return true_platform
        if isinstance(condition.ops[0], ast.NotEq):
            return ["Linux"]
        return None

    for class_name, methods in classes.items():
        node = class_nodes[class_name]
        class_decorators = node.decorator_list
        for method in methods:
            method_node = next((child for child in node.body
                                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                                and child.name == method), None)
            method_decorators = [] if method_node is None else method_node.decorator_list
            decorators = class_decorators + method_decorators
            text = " ".join(_dotted(item) for item in decorators)
            row: dict[str, object] = {}
            allowed_platforms: list[str] | None = None
            for decorator in decorators:
                if not isinstance(decorator, ast.Call) or not decorator.args:
                    continue
                decorator_name = _dotted(decorator.func).split(".")[-1]
                if decorator_name not in {"skipUnless", "skipIf"}:
                    continue
                true_platforms = platform_when_true(decorator.args[0])
                if true_platforms is None:
                    continue
                if decorator_name == "skipUnless":
                    decorator_platforms = true_platforms
                else:
                    decorator_platforms = [name for name in ("Windows", "Linux") if name not in true_platforms]
                allowed_platforms = (decorator_platforms if allowed_platforms is None else
                                     [name for name in allowed_platforms if name in decorator_platforms])
            if allowed_platforms is not None:
                row["platform"] = allowed_platforms
            if "find_spec" in text and "PySide6" in text:
                row["requirements"] = ["PySide6"]
            if row:
                overrides[f"{class_name}.{method}"] = row
    return overrides


def _module_policy(module: str, path: Path, tree: ast.Module, classes: dict[str, list[str]]) -> dict[str, object]:
    name = module.removeprefix("test_").lower()
    domains: list[str] = []
    if module == "test_target_owner_runtime" or any(part in name for part in ("dsv", "online", "simulation", "strategy", "protection", "kernel")):
        domains.append("trading")
    if any(part in name for part in ("runtime", "desktop", "workbench", "transport", "server", "process", "stream")):
        domains.append("runtime")
    if any(part in name for part in ("account", "broker", "statement", "reconcil", "money", "report")):
        domains.append("account")
    if any(part in name for part in ("clock", "calendar", "schedule")):
        domains.append("clock")
    if any(part in name for part in ("fault", "recovery", "ledger", "storage", "migration")):
        domains.append("recovery")
    if not domains:
        domains.append(name.replace("_", "-"))

    profiles = ["FullOffline"]
    if "trading" in domains or "recovery" in domains or "clock" in domains:
        profiles.append("Trading")
    runtime_modules = {
        "test_account_providers", "test_accounts", "test_broker_history_and_lots",
        "test_money_report", "test_money_runtime", "test_reconcile_commands",
        "test_reconciliation", "test_reconciliation_desktop", "test_reconciliation_export",
        "test_report_commands", "test_reporting", "test_session_launch",
        "test_startup_environment", "test_startup_metadata", "test_statement_workflow",
    }
    if "runtime" in domains or module in runtime_modules:
        profiles.append("Runtime")
    if module in {
        "test_values", "test_contracts", "test_configuration_catalog",
        "test_trading_calendar", "test_dsv_risk",
    }:
        profiles.append("Quick")

    requirements: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            requirements.update(alias.name.split(".")[0] for alias in node.names
                                if alias.name.split(".")[0] in {"PySide6", "tqsdk"})
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root in {"PySide6", "tqsdk"}:
                requirements.add(root)
    high_cost = any(part in name for part in ("process", "desktop", "workbench", "qt", "visible")) or "PySide6" in requirements
    tier = "T2" if high_cost or "runtime" in domains or module in runtime_modules else "T1"
    legacy_modules = {
        "test_kernel", "test_ledger", "test_control", "test_online_dsv",
        "test_online_dsv_multi", "test_online_dsv_processes", "test_online_simulation",
        "test_online_simulation_boundary", "test_simulation_recovery",
        "test_simulation_status", "test_simulation_status_processes",
        "test_manual_workbench", "test_workbench_processes", "test_visible_console",
    }
    if module in legacy_modules:
        legacy: bool | None = True
    elif module == "test_target_owner_runtime":
        legacy = False
    else:
        legacy = None
    return {
        "tier": tier,
        "domains": sorted(set(domains)),
        "risk": "UNTRIAGED",
        "profiles": sorted(set(profiles)),
        "platform": ["Windows", "Linux"],
        "broker_mode": "OFFLINE",
        "legacy": legacy,
        "requirements": sorted(requirements),
        "timeout_seconds": 120 if high_cost else 30,
        "cases": sorted(f"{class_name}.{method}" for class_name, methods in classes.items() for method in methods),
        "case_overrides": _case_overrides(tree, classes),
    }


def discover_case_data(test_root: Path = TEST_ROOT) -> tuple[dict[str, list[str]], dict[str, dict[str, object]]]:
    """Return module -> fully qualified unittest IDs without importing tests."""
    result: dict[str, list[str]] = {}
    policies: dict[str, dict[str, object]] = {}
    files = sorted(test_root.rglob("test*.py"))
    for path in files:
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError) as error:
            raise RegistryError(f"Cannot statically inspect {path}: {error}") from None
        if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "load_tests"
               for node in tree.body):
            raise RegistryError(f"Dynamic unittest load_tests hook is not supported: {path}")
        modules, names = _imports(tree)
        module = _module_name(path)
        classes = _case_methods(tree, modules, names)
        if not classes:
            continue
        policies[module] = _module_policy(module, path, tree, classes)
        for class_name, methods in classes.items():
            ids = [f"{module}.{class_name}.{method}" for method in methods]
            if ids:
                result.setdefault(module, []).extend(ids)
    return {module: sorted(ids) for module, ids in sorted(result.items())}, policies


def discover_cases(test_root: Path = TEST_ROOT) -> dict[str, list[str]]:
    return discover_case_data(test_root)[0]


def load_manifest(path: Path = MANIFEST) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RegistryError(f"Cannot read test manifest {path}: {error}") from None
    if value.get("schema_version") != SCHEMA or not isinstance(value.get("modules"), dict):
        raise RegistryError(f"Unsupported or malformed test manifest: {path}")
    return value


def validate_manifest(manifest: dict[str, object], discovered: dict[str, list[str]],
                      static_policies: dict[str, dict[str, object]] | None = None) -> list[str]:
    problems: list[str] = []
    modules = manifest["modules"]
    assert isinstance(modules, dict)
    discovered_ids = {test_id for ids in discovered.values() for test_id in ids}
    registered_ids: set[str] = set()
    for module, rows in modules.items():
        if not isinstance(rows, dict):
            problems.append(f"{module}: module metadata must be an object")
            continue
        cases = rows.get("cases")
        if not isinstance(cases, list) or any(not isinstance(case, str) for case in cases):
            problems.append(f"{module}: cases must be a string array")
            continue
        if len(cases) != len(set(cases)):
            problems.append(f"{module}: duplicate case IDs")
        for case in cases:
            if not case or len(case.split(".")) != 2:
                problems.append(f"{module}: case must be Class.method: {case}")
        for case in cases:
            test_id = f"{module}.{case}"
            registered_ids.add(test_id)
        if module not in discovered:
            problems.append(f"{module}: stale module entry")
    for module, rows in discovered.items():
        if module not in modules:
            problems.append(f"{module}: missing module entry")
            continue
        registered = {f"{module}.{case}" for case in modules[module].get("cases", [])}
        actual = set(rows)
        for test_id in sorted(actual - registered):
            problems.append(f"{module}: unregistered case {test_id}")
        for test_id in sorted(registered - actual):
            problems.append(f"{module}: stale case {test_id}")
        policy = modules[module]
        required_fields = ("tier", "domains", "risk", "profiles", "platform", "broker_mode", "legacy", "requirements", "timeout_seconds")
        for field in required_fields:
            if field not in policy:
                problems.append(f"{module}: missing metadata field {field}")
        if not isinstance(policy.get("tier"), str) or policy.get("tier") not in {"T1", "T2", "T3", "T4", "T5", "T6"}:
            problems.append(f"{module}: invalid tier")
        if not isinstance(policy.get("domains"), list) or not policy.get("domains") or any(not isinstance(value, str) for value in policy.get("domains", [])):
            problems.append(f"{module}: domains must be a non-empty string array")
        if not isinstance(policy.get("risk"), str) or policy.get("risk") not in {"P0", "P1", "P2", "P3", "UNTRIAGED"}:
            problems.append(f"{module}: invalid risk tag")
        allowed_profiles = {"Quick", "Affected", "Trading", "Runtime", "FullOffline"}
        if not isinstance(policy.get("profiles"), list) or any(not isinstance(value, str) or value not in allowed_profiles for value in policy.get("profiles", [])):
            problems.append(f"{module}: contains unsupported PR1 profile")
        if not isinstance(policy.get("platform"), list) or any(not isinstance(value, str) or value not in {"Windows", "Linux"} for value in policy.get("platform", [])):
            problems.append(f"{module}: invalid platform metadata")
        if not (policy.get("legacy") is True or policy.get("legacy") is False or policy.get("legacy") is None):
            problems.append(f"{module}: legacy must be true, false or null when unreviewed")
        for field in ("requirements",):
            if not isinstance(policy.get(field), list) or any(not isinstance(value, str) for value in policy.get(field, [])):
                problems.append(f"{module}: {field} must be a string array")
        timeout = policy.get("timeout_seconds")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            problems.append(f"{module}: timeout_seconds must be a positive integer")
        if policy.get("risk") == "UNTRIAGED" and "FullOffline" not in policy.get("profiles", []):
            problems.append(f"{module}: untriaged cases must remain in FullOffline")
        if policy.get("broker_mode") != "OFFLINE":
            problems.append(f"{module}: PR1 profiles only accept OFFLINE cases")
        case_overrides = policy.get("case_overrides", {})
        registered_cases = set(policy.get("cases", []))
        if not isinstance(case_overrides, dict) or set(case_overrides) - registered_cases:
            problems.append(f"{module}: case overrides must refer to registered cases")
        elif any(not isinstance(value, dict) or set(value) - {"platform", "requirements"} for value in case_overrides.values()):
            problems.append(f"{module}: case override contains unsupported metadata")
        if static_policies is not None and module in static_policies:
            expected_overrides = static_policies[module].get("case_overrides", {})
            if case_overrides != expected_overrides:
                problems.append(f"{module}: per-case platform/requirement metadata differs from static source decorators")
    for test_id in sorted(registered_ids - discovered_ids):
        problems.append(f"manifest: stale test ID {test_id}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the offline unittest registry without importing test modules.")
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--list", action="store_true", help="print static inventory counts")
    parser.add_argument("--show-ids", action="store_true", help="print statically discovered test IDs")
    parser.add_argument("--write-initial-manifest", action="store_true",
                        help="write a baseline inventory; all risks remain UNTRIAGED")
    args = parser.parse_args(argv)
    try:
        discovered, policies = discover_case_data()
        if args.show_ids:
            for module, ids in discovered.items():
                for test_id in ids:
                    print(test_id)
            print(f"{sum(map(len, discovered.values()))} statically discovered cases in {len(discovered)} modules")
            return 0
        if args.list:
            print(f"{sum(map(len, discovered.values()))} statically discovered cases in {len(discovered)} modules")
            return 0
        if args.write_initial_manifest:
            if args.manifest.exists():
                raise RegistryError(f"Refusing to overwrite existing manifest: {args.manifest}")
            document = {
                "schema_version": SCHEMA,
                "inventory_method": "AST unittest TestCase class/method discovery; test modules are not imported",
                "baseline_commit": "483ee21",
                "selection_policy": {
                    "FullOffline": "all registered offline test cases",
                    "Quick": "curated static contract set; runtime target must be measured before relying on its time budget",
                    "Trading": "registered cases tagged with trading/recovery/clock domains",
                    "Runtime": "registered cases tagged with runtime domain; includes legacy/component coverage where tagged",
                    "Affected": "FullOffline until the path-to-suite dependency map has verified coverage",
                },
                "modules": policies,
            }
            args.manifest.parent.mkdir(parents=True, exist_ok=True)
            args.manifest.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"wrote {sum(map(len, discovered.values()))} case IDs in {len(discovered)} modules to {args.manifest}")
            return 0
        manifest = load_manifest(args.manifest)
        problems = validate_manifest(manifest, discovered, policies)
    except RegistryError as error:
        print(f"registry error: {error}", file=sys.stderr)
        return 2
    if problems:
        for problem in problems:
            print(f"registry error: {problem}", file=sys.stderr)
        return 2
    print(f"manifest valid: {sum(map(len, discovered.values()))} cases in {len(discovered)} modules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
