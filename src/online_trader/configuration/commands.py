"""Local parameter workflow. This module never creates a gateway connection."""

import json
from pathlib import Path

from online_trader.configuration.baseline import (
    check_reference_profile, legacy_baseline_available, load_parameter_reference, load_reference_approval,
)
from online_trader.configuration.files import export_profile, load_profile
from online_trader.errors import TraderError


def add_parameter_parser(commands):
    parser = commands.add_parser("parameters", help="查看、导入导出及记录DSV参数版本（离线）")
    actions = parser.add_subparsers(dest="parameter_action", required=True)
    for action in ("show", "diff", "export"):
        command = actions.add_parser(action)
        command.add_argument("--input", type=Path, required=action == "diff")
        command.add_argument("--dsv", action="store_true", help="只提取原DSV配置中的业务设置")
        if action == "export":command.add_argument("--output", type=Path, required=True)
    for action in ("register", "save", "history"):
        command = actions.add_parser(action)
        command.add_argument("--data-dir", type=Path, required=True)
        command.add_argument("--account-key", required=True)
        if action in {"register", "save"}:
            command.add_argument("--input", type=Path, required=action == "save")
            command.add_argument("--dsv", action="store_true")
        if action == "save":
            command.add_argument("--expected-revision", type=int, required=True)
    actions.add_parser("desktop", help="打开离线参数预览窗口")
    calculator = actions.add_parser("calculate", help="从本地日线计算DSV偏移（只预览）")
    calculator.add_argument("--input", type=Path)
    calculator.add_argument("--dsv", action="store_true")
    calculator.add_argument("--daily", type=Path, required=True)
    calculator.add_argument("--as-of", required=True, help="排除此交易日及之后的日线，YYYY-MM-DD")
    calculator.add_argument("--formula", choices=("minrange", "range3x09_contract", "range3x09_main", "atr20_main", "atr_mixw_main", "range3_main"))
    calculator.add_argument("--ceiling"); calculator.add_argument("--floor")
    calculator.add_argument("--ranges", type=Path, help="合约到当日振幅的JSON字符串映射")
    calculator.add_argument("--output", type=Path, help="导出新的计算依据JSON")
    calculator.add_argument("--apply-output", type=Path, help="将所选结果保存为新的业务参数JSON")
    calculator.add_argument("--contracts", nargs="+", help="明确选择要保存结果的合约")
    for action in ("symbols-import", "symbols-export", "template-list", "template-save", "template-apply", "template-delete"):
        command = actions.add_parser(action, help="离线品种CSV或交易参数模板")
        if action not in {"template-list", "template-delete"}:
            command.add_argument("--input", type=Path, help="业务参数JSON；省略时使用已确认基准")
            command.add_argument("--dsv", action="store_true")
        if action.startswith("template-"):
            command.add_argument("--store", type=Path, required=True, help="本地模板目录")
            if action != "template-list": command.add_argument("--name", required=True)
            if action in {"template-save", "template-delete"}:
                command.add_argument("--expected-revision", type=int, required=True)
            if action == "template-save": command.add_argument("--overwrite", action="store_true")
        if action == "symbols-import":
            command.add_argument("--csv", type=Path, required=True)
            command.add_argument("--mode", choices=("replace", "merge"), default="replace")
        if action in {"symbols-import", "symbols-export", "template-apply"}:
            command.add_argument("--output", type=Path, required=action == "symbols-export")


def _run_workspace(args):
    from online_trader.configuration.symbol_csv import load_symbols, export_symbols
    from online_trader.configuration.templates import TemplateStore
    action = args.parameter_action
    if action == "template-list": return TemplateStore(args.store).read().summary()
    if action == "template-delete":
        return TemplateStore(args.store).delete(args.name, expected_revision=args.expected_revision).summary()
    candidate = load_profile(args.input, dsv=args.dsv) if args.input is not None else load_parameter_reference()[0]
    if action == "template-save":
        return TemplateStore(args.store).save(args.name, candidate, expected_revision=args.expected_revision,
                                             overwrite=args.overwrite).summary()
    if action == "symbols-export":
        return {"exported": str(export_symbols(candidate, args.output)), "profile_hash": candidate.content_hash,
                "runtime_activation": False}
    if action == "symbols-import": candidate = load_symbols(args.csv, candidate, mode=args.mode)
    else:
        snapshot = TemplateStore(args.store).read()
        candidate = snapshot.apply(args.name, candidate)
    result = {"profile_hash": candidate.content_hash, "runtime_activation": False,
              "settings_record": candidate.to_record(), "diagnostics": candidate.diagnostics()}
    if action == "template-apply": result["template_revision"] = snapshot.revision
    if args.output is not None: result["exported"] = str(export_profile(candidate, args.output))
    return result


def parameter_diff(baseline, candidate):
    """Compare canonical business values, including disabled and zero values."""
    changes = []
    def visit(before, after, path):
        if type(before) is dict and type(after) is dict:
            for key in sorted(before.keys() | after.keys()):
                if key not in before:changes.append({"path": path + "." + key, "before": None, "after": after[key]})
                elif key not in after:changes.append({"path": path + "." + key, "before": before[key], "after": None})
                else:visit(before[key], after[key], path + "." + key)
        elif type(before) is list and type(after) is list:
            for index in range(max(len(before), len(after))):
                location = path + f"[{index}]"
                if index >= len(before):changes.append({"path": location, "before": None, "after": after[index]})
                elif index >= len(after):changes.append({"path": location, "before": before[index], "after": None})
                else:visit(before[index], after[index], location)
        elif type(before) is not type(after) or before != after:
            changes.append({"path": path, "before": before, "after": after})
    visit(baseline.to_record()["settings"], candidate.to_record()["settings"], "$")
    return changes


def run_parameters(args):
    if args.parameter_action == "desktop":
        from online_trader.desktop.settings import run
        return run()
    if args.parameter_action == "calculate":
        from online_trader.configuration.offsets import load_daily, calculate_offsets, export_calculation
        from online_trader.contracts.codec import strict_loads
        if bool(args.apply_output) != bool(args.contracts):
            raise TraderError("E_OFFSET", "Saving calculated parameters requires both --apply-output and --contracts")
        candidate = load_profile(args.input, dsv=args.dsv) if args.input is not None else load_parameter_reference()[0]
        ranges = None
        if args.ranges is not None:
            try:
                with args.ranges.open("rb") as stream: raw = stream.read(32_769)
            except OSError as error: raise TraderError("E_OFFSET", "Cannot read selected realized-range JSON") from error
            if len(raw) > 32_768: raise TraderError("E_OFFSET", "Realized-range JSON exceeds size limit")
            ranges = strict_loads(raw)
        calculation = calculate_offsets(candidate, load_daily(args.daily), as_of_date=args.as_of,
            realized_ranges=ranges, formula=args.formula, ceiling=args.ceiling, floor=args.floor)
        # Validate the selected application before publishing either output.
        applied = calculation.apply(candidate, args.contracts) if args.apply_output is not None else None
        if args.output is not None and args.apply_output is not None:
            raise TraderError("E_OFFSET", "Choose one output artifact per command")
        result = calculation.to_record() | {"calculation_hash": calculation.content_hash}
        if args.output is not None: result["exported"] = str(export_calculation(calculation, args.output))
        if applied is not None:
            result["exported"] = str(export_profile(applied, args.apply_output)); result["applied_profile_hash"] = applied.content_hash
    elif args.parameter_action in {"symbols-import", "symbols-export", "template-list", "template-save", "template-apply", "template-delete"}:
        result = _run_workspace(args)
    elif args.parameter_action == "history":
        from online_trader.kernel.configuration import read_catalog_file
        result = read_catalog_file(args.data_dir / "ledger.db", args.account_key)
    elif args.parameter_action in {"register", "save"}:
        # Parse explicit input before opening a writer or creating a boot.
        candidate = load_profile(args.input, dsv=args.dsv) if args.input is not None else None
        ledger_path = args.data_dir / "ledger.db"
        if args.parameter_action == "register":
            if candidate is not None:
                check_reference_profile(candidate, load_reference_approval())
            elif not legacy_baseline_available():
                from online_trader.kernel.configuration import read_catalog_file
                if not ledger_path.is_file() or not read_catalog_file(ledger_path, args.account_key):
                    raise TraderError("E_CONFIG_FILE", "Supply the confirmed original DSV profile with --input")
        from online_trader.kernel.engine import Kernel
        with Kernel(ledger_path, args.account_key) as kernel:
            kernel.wait_idle()
            if args.parameter_action == "register":
                future = kernel.configurations.register_confirmed_baseline(candidate)
            else:
                future = kernel.configurations.save(candidate, expected_revision=args.expected_revision)
            kernel.wait_idle()
            result = future.result()
    else:
        baseline, approval = load_parameter_reference()
        candidate = load_profile(args.input, dsv=args.dsv) if args.input is not None else baseline
        if args.parameter_action == "diff":
            result = {"baseline_hash": baseline.content_hash, "profile_hash": candidate.content_hash,
                      "matches_confirmed_baseline": candidate.content_hash == approval["profile_hash"],
                      "changes": parameter_diff(baseline, candidate), "diagnostics": candidate.diagnostics()}
        elif args.parameter_action == "export":
            path = export_profile(candidate, args.output)
            result = {"exported": str(path), "profile_hash": candidate.content_hash,
                      "matches_confirmed_baseline": candidate.content_hash == approval["profile_hash"]}
        else:
            result = {"rule_set_id": approval["rule_set_id"], "reference_approval_id": approval["record_id"],
                      "profile_hash": candidate.content_hash,
                      "matches_confirmed_baseline": candidate.content_hash == approval["profile_hash"],
                      "runtime_activation": False, "summary": candidate.summary(),
                      "diagnostics": candidate.diagnostics(), "settings_record": candidate.to_record()}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
