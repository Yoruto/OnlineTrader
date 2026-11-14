"""Verify frozen DSV sources without extracting files or executing their code."""

import argparse
import hashlib
from io import BytesIO
import json
import marshal
from pathlib import Path
import struct
import sys
from types import CodeType
from zipfile import ZipFile
import zlib


MANIFEST = Path(__file__).with_name("fixtures") / "baseline-manifest.json"
COOKIE_MAGIC = b"MEI\014\013\012\013\016"
COOKIE = struct.Struct("!8sIIII64s")
TOC_ENTRY = struct.Struct("!IIIIBc")


def verify(label, data, expected):
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"{label}: SHA-256 mismatch ({actual})")
    return actual


def archive_objects(executable, expected_version):
    cookie_start = executable.rfind(COOKIE_MAGIC)
    _, package_size, toc_offset, toc_size, version, _ = COOKIE.unpack_from(executable, cookie_start)
    if version != expected_version:
        raise ValueError(f"Unexpected PyInstaller Python version: {version}")
    package_start = cookie_start + COOKIE.size - package_size
    toc_start = package_start + toc_offset
    toc = executable[toc_start:toc_start + toc_size]
    pos = 0
    objects = {}
    while pos < len(toc):
        entry_size, offset, length, raw_length, compressed, _ = TOC_ENTRY.unpack_from(toc, pos)
        name = toc[pos + TOC_ENTRY.size:pos + entry_size].split(b"\0", 1)[0]
        pos += entry_size
        if name in (b"gui", b"PYZ.pyz"):
            start = package_start + offset
            raw = executable[start:start + length]
            if compressed:
                raw = zlib.decompress(raw)
            if len(raw) != raw_length:
                raise ValueError(f"Unexpected data length for {name!r}")
            objects[name] = raw
    return objects[b"gui"], objects[b"PYZ.pyz"]


def code_objects(root):
    yield root
    for value in root.co_consts:
        if isinstance(value, CodeType):
            yield from code_objects(value)


def inspect_reference(archive, sdk_source=None):
    baseline = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected = baseline["dsv_reference"]
    source = archive.read_bytes()
    observed = {"archive_sha256": verify("archive", source, expected["archive_sha256"])}
    with ZipFile(BytesIO(source)) as package:
        executable = package.read("trading/trading.exe")
        for field, member in (
            ("sdk_version_file_sha256", "__version__.py"),
            ("sdk_target_task_sha256", "lib/target_pos_task.py"),
        ):
            raw = package.read(f"trading/_internal/tqsdk/{member}")
            observed[field] = verify(member, raw, expected[field])
    observed["exe_sha256"] = verify("exe", executable, expected["exe_sha256"])
    gui, pyz = archive_objects(executable, expected["python_archive_version"])
    observed["gui_marshal_sha256"] = verify("gui", gui, expected["gui_marshal_sha256"])
    toc = dict(marshal.loads(pyz[struct.unpack_from("!i", pyz, 8)[0]:]))
    _, start, length = toc["tqsdk.lib.target_pos_task"]
    embedded_sdk = zlib.decompress(pyz[start:start + length])
    observed["sdk_target_task_marshal_sha256"] = verify(
        "embedded target task", embedded_sdk, expected["sdk_target_task_marshal_sha256"]
    )
    functions = {code.co_qualname: code for code in code_objects(marshal.loads(gui))}
    observed["gui_functions"] = {}
    for name, reference in expected["gui_functions"].items():
        code = functions[name]
        if code.co_firstlineno != reference["line"]:
            raise ValueError(f"{name}: source line changed")
        observed["gui_functions"][name] = {
            "line": code.co_firstlineno,
            "bytecode_sha256": verify(name, code.co_code, reference["bytecode_sha256"]),
        }
    if sdk_source is not None:
        observed["online_trader_sdk_target_task_sha256"] = verify(
            "OnlineTrader SDK target task", sdk_source.read_bytes(),
            baseline["online_trader"]["sdk_target_task_sha256"],
        )
    return observed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--sdk-source", type=Path)
    args = parser.parse_args(argv)
    if sys.version_info[:2] != (3, 14):
        parser.error("Use the independent Python 3.14 environment to inspect the archive")
    try:
        result = inspect_reference(args.archive, args.sdk_source)
    except (OSError, ValueError, KeyError, struct.error) as error:
        parser.exit(1, f"Reference verification failed: {error}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
