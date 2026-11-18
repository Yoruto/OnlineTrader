"""Offline schema registry and semantic scalar checks; never retrieves remote URLs."""

from __future__ import annotations

from functools import lru_cache
from datetime import date, datetime
from importlib.resources import files
import re
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource

from online_trader.contracts.codec import canonical_bytes, strict_loads
from online_trader.errors import TraderError

_MAX_SEQ = 9_223_372_036_854_775_807
_SEQUENCE_FIELD = re.compile(r"(?:.*_seq|.*_epoch|.*_revision|.*_monotonic_ns|.*_monotonic_nanoseconds)$")


def _deny_remote(uri: str) -> Resource:
    raise NoSuchResource(ref=uri)


@lru_cache(maxsize=1)
def _validators() -> dict[str, Draft202012Validator]:
    resources = files("online_trader").joinpath("resources")
    schemas = {name: strict_loads(resources.joinpath(name).read_bytes()) for name in
               ("ipc.schema.json", "workbench-ipc.schema.json", "protection-plan.schema.json", "protection-plan-v1.1.schema.json", "protection-plan-v1.2.schema.json", "protection-plan-v1.3.schema.json")}
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)
    registry = Registry(retrieve=_deny_remote).with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values())
    return {name: Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
            for name, schema in schemas.items()}


def _sequences(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _SEQUENCE_FIELD.fullmatch(key) and type(item) is str and item.isascii() and item.isdigit():
                if len(item) > 19 or int(item) > _MAX_SEQ:
                    raise TraderError("E_SCHEMA", "Sequence exceeds signed 64-bit storage")
            if type(item) is str and (key.endswith("_utc") or key == "trading_day"):
                try:
                    if key == "trading_day":
                        date.fromisoformat(item)
                    else:
                        if not item.endswith("Z"):
                            raise ValueError("Expected UTC")
                        datetime.fromisoformat(item[:-1] + "+00:00")
                except ValueError as exc:
                    raise TraderError("E_SCHEMA", "Invalid calendar timestamp") from exc
            _sequences(item)
    elif isinstance(value, list):
        for item in value:
            _sequences(item)


def _validate(name: str, value: dict[str, Any]) -> None:
    # Also catches Python float/bool coercion that JSON Schema's integer semantics permit.
    canonical_bytes(value)
    if not _validators()[name].is_valid(value):
        raise TraderError("E_SCHEMA", "Message does not conform to the registered schema")
    _sequences(value)


def validate_message(message: dict[str, Any]) -> None:
    workbench = {'WorkbenchRequest', 'WorkbenchSnapshot', 'WorkbenchMonitorRequest', 'WorkbenchMonitorSnapshot',
                 'WorkbenchPositionsRequest', 'WorkbenchPositionsSnapshot'}
    name = 'workbench-ipc.schema.json' if message.get('type') in workbench else 'ipc.schema.json'
    _validate(name, message)


def validate_plan(plan: dict[str, Any]) -> None:
    version=plan.get("schema_version")
    name = f"protection-plan-v{version}.schema.json" if version in ("1.1", "1.2", "1.3") else "protection-plan.schema.json"
    _validate(name, plan)
