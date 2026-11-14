"""Independently exercise selected fixed TqSdk source functions on offline facts."""
import argparse
import ast
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import types
import zipfile


MEMBER = "trading/_internal/tqsdk/lib/target_pos_task.py"
VERSION_MEMBER = "trading/_internal/tqsdk/__version__.py"
DEFAULT_MANIFEST = Path(__file__).with_name("fixtures") / "baseline-manifest.json"


class NoNetwork:
    @staticmethod
    def audit(event, args):
        if event.startswith(("socket.", "subprocess.", "os.spawn", "os.exec")) or event == "os.system":
            raise RuntimeError(f"Offline probe rejected {event}")


class InjectedCrash(RuntimeError):
    pass


class Order(dict):
    __getattr__ = dict.__getitem__


class Probe:
    def __init__(self, *, order, updates=(), crash=False, price_updates=()):
        self.order = order
        self.updates = iter(updates)
        self.price_updates = iter(price_updates)
        self.crash = crash
        self.trace = []

    def make_id(self, prefix):
        identity = f"{prefix}_fixture"
        self.trace.append(["child_identity_generated", identity])
        return identity

    def insert_order(self, symbol, direction, offset, volume, price, *, order_id, account):
        self.trace.append(["insert_called", order_id, symbol, direction, offset, volume, price])
        if self.crash:
            raise InjectedCrash("adapter boundary")
        return self.order

    def get_order(self, order_id, *, account):
        return self.order

    def cancel_order(self, order_id, *, account):
        self.trace.append(["cancel_called", order_id])

    def register_update_notify(self, *, chan=None):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def send(self, event):
        if isinstance(event, dict):
            event = {k: v for k, v in event.items() if k != "trade_records"}
        self.trace.append(["channel_observation", event])

    def send_nowait(self, volume):
        self.trace.append(["target_queued", volume])

    async def recv(self):
        update = next(self.updates)
        self.trace.append(["update_applied", update[0]])
        self.order.update(update[1])

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            label, price, exchange_order_id, status = next(self.price_updates)
        except StopIteration:
            raise StopAsyncIteration from None
        self.price = price
        self.order.update(exchange_order_id=exchange_order_id, status=status)
        self.trace.append(["quote_update", label])
        return label


def method(tree, class_name, method_name, namespace=None):
    class_def = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    node = next(item for item in class_def.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == method_name)
    if namespace is None:
        return node
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<fixed-sdk-source>", "exec"), namespace)
    return namespace[method_name]


async def exercise(source, version, early_cancel):
    tree = ast.parse(source)
    target_signature = method(tree, "TargetPosTask", "__init__").args
    signature = [arg.arg for arg in target_signature.posonlyargs + target_signature.args + target_signature.kwonlyargs]
    assert "trade_objs_chan" in signature
    assert "order_id" not in signature
    queue = Probe(order=None)
    post_target = method(tree, "TargetPosTask", "set_target_volume", {})
    post_target(types.SimpleNamespace(_task=types.SimpleNamespace(done=lambda: False), _pos_chan=queue), -3)
    assert queue.trace == [["target_queued", -3]]

    traces = {}
    for crash in (False, True):
        initial = Order(status="ALIVE", order_id="PYSDK_target_fixture", volume_left=3,
                        volume_orign=3, trade_records={})
        facts = [("FINISHED_without_fills", {"status": "FINISHED", "volume_left": 1, "trade_records": {}}),
                 ("late_fills", {"trade_records": {"trade-1": Order(volume=2)}})]
        adapter = Probe(order=initial, updates=facts, crash=crash)
        child = method(tree, "InsertOrderTask", "_run", {"utils": types.SimpleNamespace(_generate_uuid=adapter.make_id)})
        task = types.SimpleNamespace(_api=adapter, _symbol="SHFE.fixture", _direction="BUY", _offset="OPEN",
                                     _volume=3, _limit_price=100, _account="synthetic", _order_chan=adapter,
                                     _trade_chan=None, _trade_objs_chan=None)
        try:
            await child(task)
        except InjectedCrash:
            assert crash
        else:
            assert not crash
        assert [event[0] for event in adapter.trace[:2]] == ["child_identity_generated", "insert_called"]
        if crash:
            assert len(adapter.trace) == 2
        else:
            assert [event[1] for event in adapter.trace if event[0] == "update_applied"] == [
                "FINISHED_without_fills", "late_fills"]
            assert adapter.order.trade_records["trade-1"].volume == 2
        traces["crash_at_insert" if crash else "late_fill"] = adapter.trace

    for direction, better, worse in (("BUY", 99, 101), ("SELL", 101, 99)):
        prices = [("equal", 100, "", "ALIVE"), ("improved", better, "", "ALIVE"),
                  ("worsened_no_exchange_identity", worse, "", "ALIVE"),
                  ("worsened_with_exchange_identity", worse, "EX-fixture", "ALIVE")]
        adapter = Probe(order=Order(status="ALIVE", exchange_order_id=""), price_updates=prices)
        check = method(tree, "InsertOrderUntilAllTradedTask", "_check_price", {})
        task = types.SimpleNamespace(_api=adapter, _account="synthetic", _direction=direction,
                                     _get_price=lambda side: adapter.price)
        await check(task, adapter, 100, "PYSDK_target_fixture")
        expected = "worsened_no_exchange_identity" if early_cancel else "worsened_with_exchange_identity"
        assert adapter.trace[-2:] == [["quote_update", expected], ["cancel_called", "PYSDK_target_fixture"]], adapter.trace
        traces[f"reprice_{direction.lower()}"] = adapter.trace

    return {"version": version, "source_sha256": hashlib.sha256(source).hexdigest(),
            "target_constructor_arguments": signature, "source_only_synthetic_traces": traces}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--sdk-source", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    reference = manifest["dsv_reference"]
    locked = manifest["online_trader"]

    def verify(label, data, expected):
        if hashlib.sha256(data).hexdigest() != expected:
            parser.exit(1, f"Reference verification failed: {label} SHA-256 mismatch\n")

    with args.archive.open("rb") as archive:
        if hashlib.file_digest(archive, "sha256").hexdigest() != reference["archive_sha256"]:
            parser.exit(1, "Reference verification failed: archive SHA-256 mismatch\n")
        archive.seek(0)
        with zipfile.ZipFile(archive) as bundle:
            source = bundle.read(MEMBER)
            version_file = bundle.read(VERSION_MEMBER)
    verify("reference SDK version", version_file, reference["sdk_version_file_sha256"])
    verify("reference SDK source", source, reference["sdk_target_task_sha256"])
    current_sdk = args.sdk_source.read_bytes()
    verify("current SDK source", current_sdk, locked["sdk_target_task_sha256"])
    # Initialize the local asyncio loop before blocking socket access. Python's
    # Windows event loop uses a private local wake-up pair, never a broker socket.
    loop = asyncio.new_event_loop()
    sys.addaudithook(NoNetwork.audit)
    try:
        reference_results = loop.run_until_complete(exercise(source, reference["sdk_version"], early_cancel=True))
        locked_results = loop.run_until_complete(exercise(current_sdk, locked["sdk_version"], early_cancel=False))
    finally:
        loop.close()
    print(json.dumps({"status": "passed", "scope": "AST-selected methods only, synthetic adapter, no SDK imports",
                      "reference": reference_results, "online_trader_locked": locked_results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
