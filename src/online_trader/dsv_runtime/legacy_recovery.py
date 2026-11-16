"""Recover a proved external flatten in an isolated SimNow legacy ledger."""

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.domain.values import decimal_value, quantity
from online_trader.dsv_runtime.migration import RECOVERY_EVENT, _COLD_CHECKS, _readonly, _source_digest, _source_schema
from online_trader.dsv_runtime.runner import _day, _utc
from online_trader.errors import TraderError
from online_trader.gateway.native_days import native_day
from online_trader.gateway.ports import OnlineSimulationPort
from online_trader.gateway.tqkq import TqKqPort
from online_trader.kernel import Kernel
from online_trader.kernel.external_positions import apply_external_reduction
from online_trader.online_scope import DsvSimulationGrant
from online_trader.runtime.instance import AccountLock
from online_trader.scheduling.calendar import TradingCalendar


class ArchivedSimNowPort(OnlineSimulationPort):
    """A historical snapshot with no dispatch, quotes, or connection."""

    trade_prefix = 'sn-'
    order_id = staticmethod(TqKqPort.order_id)
    fault = True

    def __init__(self, evidence, calendar, grant):
        account = evidence['account_key']
        self.gateway_identity = 'TQ_SIMNOW:' + account.rsplit(':', 1)[1]
        self.calendar = calendar
        self.grant = grant
        self.clock = SimpleNamespace(now=lambda: _utc(evidence['observed_at_utc']))
        self.quotes = {}
        self.order_observation = None
        self.evidence = evidence

    def snapshot(self):
        snapshot = {key: self.evidence[key] for key in (
            'account_key', 'trading_day', 'observed_at_utc', 'complete', 'orders', 'trades', 'positions', 'funds')}
        return deepcopy(snapshot) | {
            'gateway_identity': self.gateway_identity, 'source_epoch': self.source_epoch,
            'snapshot_id': 'reviewed-flat-' + content_hash(self.evidence)}

    def observations(self):
        return [deepcopy(self.order_observation)]


def _check_pair(evidence, calendar):
    fields = {'schema_version', 'account_key', 'trading_day', 'source_event_seq', 'observed_at_utc',
              'proof_sha256', 'complete', 'orders', 'trades', 'positions', 'funds'}
    if (type(evidence) is not dict or evidence.keys() != fields
            or evidence['schema_version'] != 'SIMNOW_FLAT_RECOVERY/1'
            or type(evidence['account_key']) is not str
            or not re.fullmatch(r'SIM:SIMNOW:[0-9a-f]{16}', evidence['account_key'])
            or evidence['complete'] is not True or evidence['positions'] != []
            or type(evidence['funds']) is not dict or not evidence['funds']
            or type(evidence['proof_sha256']) is not list or not evidence['proof_sha256']
            or any(type(item) is not str or not re.fullmatch(r'[0-9a-f]{64}', item)
                   for item in evidence['proof_sha256'])):
        raise TraderError('E_DSV_RECOVERY', 'Expected reviewed complete SimNow account, flat positions and evidence hashes')
    day = _day(evidence['trading_day'])
    observed = _utc(evidence['observed_at_utc'])
    order_fields = {'order_id', 'contract_id', 'direction', 'offset', 'quantity', 'remaining',
                    'status', 'limit_price', 'source_time_ns'}
    trade_fields = {'trade_id', 'order_id', 'contract_id', 'direction', 'offset', 'quantity', 'price', 'source_time_ns'}
    orders, trades = evidence['orders'], evidence['trades']
    if (type(orders) is not list or len(orders) != 2 or type(trades) is not list or len(trades) != 2
            or any(type(row) is not dict or row.keys() != order_fields for row in orders)
            or any(type(row) is not dict or row.keys() != trade_fields for row in trades)):
        raise TraderError('E_DSV_RECOVERY', 'Exactly one original opening and one external closing order with their fills are required')
    for row in orders + trades:
        identifiers = (row['order_id'], row['trade_id']) if 'trade_id' in row else (row['order_id'],)
        if any(type(raw_id) is not str or not raw_id.strip() or len(raw_id) > 512 or not raw_id.isprintable()
               for raw_id in identifiers):
            raise TraderError('E_DSV_RECOVERY', 'Native identities must be retained as received')
    by_order = {row['order_id']: row for row in trades}
    if (len(by_order) != 2 or len({row['trade_id'] for row in trades}) != 2
            or {row['order_id'] for row in orders} != set(by_order)):
        raise TraderError('E_DSV_RECOVERY', 'Each terminal order must have a distinct identified trade')
    observed_ns = int(observed.timestamp()) * 1_000_000_000 + observed.microsecond * 1000
    for order in orders:
        trade = by_order[order['order_id']]
        for field in ('order_id', 'contract_id', 'direction', 'offset', 'quantity'):
            if trade[field] != order[field]:
                raise TraderError('E_DSV_RECOVERY', 'Trade conflicts with its terminal order')
        if (order['status'] != 'FINISHED' or quantity(order['remaining']) != 0
                or quantity(order['quantity'], positive=True) != quantity(trade['quantity'], positive=True)):
            raise TraderError('E_DSV_RECOVERY', 'Both native orders must be terminal with complete detailed fills')
        limit = decimal_value(order['limit_price'], positive=True)
        price = decimal_value(trade['price'], positive=True)
        if ((order['direction'] == 'BUY' and price > limit) or (order['direction'] == 'SELL' and price < limit)
                or order['direction'] not in ('BUY', 'SELL')):
            raise TraderError('E_DSV_RECOVERY', 'Fill price conflicts with the native limit order')
        for item in (order, trade):
            if native_day(calendar, item['contract_id'], item['source_time_ns'])['trading_day'] != day:
                raise TraderError('E_DSV_RECOVERY', 'Native execution does not belong to the original cabinet day')
        if not int(order['source_time_ns']) <= int(trade['source_time_ns']) <= observed_ns:
            raise TraderError('E_DSV_RECOVERY', 'Native order, fill and snapshot chronology conflicts')
    opening = next((i for i, row in enumerate(orders) if row['offset'] == 'OPEN'), None)
    if opening is None:
        raise TraderError('E_DSV_RECOVERY', 'The snapshot has no original opening')
    closing = 1 - opening
    entry, exit_order = orders[opening], orders[closing]
    entry_trade, exit_trade = by_order[entry['order_id']], by_order[exit_order['order_id']]
    if (exit_order['offset'] != 'CLOSETODAY' or exit_order['direction'] == entry['direction']
            or entry['contract_id'] != exit_order['contract_id'] or entry['quantity'] != exit_order['quantity']
            or int(entry_trade['source_time_ns']) >= int(exit_order['source_time_ns'])):
        raise TraderError('E_DSV_RECOVERY', 'The external order must close exactly the proved original today lot')
    return entry, entry_trade, exit_order, exit_trade


def _recover_cycle(kernel, port, pair):
    opening, fill, closing_order, closing_trade = pair
    owner = kernel.simulation
    open_cycles = [cycle for cycle in kernel._staged.values() if cycle['lifecycle'] != 'CLOSED']
    if len(open_cycles) != 1 or len(open_cycles[0]['orders']) != 1:
        raise TraderError('E_DSV_RECOVERY', 'Other unresolved cycles or children require separate original evidence')
    cycle = deepcopy(open_cycles[0])
    child = cycle['orders'][0]
    plan = cycle['plan']
    day = plan['trading_day']
    if (cycle.get('entry_origin') != 'MANUAL' or child['effect'] != 'OPEN'
            or child['send_state'] != 'UNKNOWN' or child['filled_qty'] or cycle['protection']['owned_qty']
            or day != port.evidence['trading_day'] or opening['order_id'] != port.order_id(child['child_order_id'])
            or opening['contract_id'] != plan['contract_id'] or child['original_qty'] != opening['quantity']
            or child['limit_price'] != opening['limit_price']
            or opening['direction'] != ('BUY' if plan['direction'] == 'LONG' else 'SELL')
            or any(port.order_id(order['child_order_id']) == closing_order['order_id']
                   for original in kernel._staged.values() for order in original['orders'])):
        raise TraderError('E_DSV_RECOVERY', 'Reviewed execution must match one original unbooked manual opening and a separate external close')
    observation = owner._observed_orders.get(child['child_order_id'])
    order_proof = native_day(port.calendar, opening['contract_id'], opening['source_time_ns'])
    fill_proof = native_day(port.calendar, fill['contract_id'], fill['source_time_ns'])
    expected_fill = port.trade_identity(fill['trade_id']) | {
        'quantity': fill['quantity'], 'price': fill['price'], 'source_time_ns': fill['source_time_ns'],
        'trading_day': day, 'native_day_evidence': fill_proof}
    if (not observation or observation['order_id'] != opening['order_id']
            or observation['contract_id'] != opening['contract_id'] or observation['status'] != 'FINISHED'
            or observation['remaining'] != 0 or observation['reported_fill_qty'] != fill['quantity']
            or observation['order_day_evidence'] != order_proof or observation['fills'] != [expected_fill]):
        raise TraderError('E_DSV_RECOVERY', 'Native opening and the original durable observation must agree exactly')
    port.order_observation = observation
    owner.observe_native_orders()
    cid = cycle['position_cycle_id']
    cycle = deepcopy(kernel._committed[cid])
    snapshot = port.snapshot()
    owner.verify_cycle_orders(cycle, cycle['orders'], snapshot)
    if cycle['protection']['owned_qty'] != fill['quantity']:
        raise TraderError('E_DSV_RECOVERY', 'Original fill ownership was not recovered')
    digest = content_hash(port.evidence)
    cycle['reviewed_foreign_baseline'] = {'trading_day': day, 'orders': snapshot['orders']}
    cycle['external_review'] = {'evidence_hash': digest, 'command_id': 'archive-flat-' + digest,
        'snapshot_id': snapshot['snapshot_id'], 'source_epoch': snapshot['source_epoch'],
        'native_trades_hash': content_hash(snapshot['trades'])}
    apply_external_reduction(owner, cycle, {'bucket': 'today', 'quantity': closing_trade['quantity']},
                             cycle['external_review']['command_id'], day)
    kernel.stage_control_cycles([cycle], 'TX_TAKEOVER', RECOVERY_EVENT, allocation_review={
        'evidence_hash': digest, 'proof_sha256': port.evidence['proof_sha256'],
        'external_order': closing_order, 'external_trade': closing_trade})
    kernel.wait_idle()
    owner.reconcile(cid, _snapshot=snapshot, clear_gateway_fault=False)
    owner.reconcile(cid, close=True, _snapshot=snapshot, clear_gateway_fault=False)


def recover_flat(source, evidence, calendar_path, target, *, expected_hash):
    """Publish a new ledger directory after replay and all cold checks succeed."""
    if content_hash(evidence) != expected_hash:
        raise TraderError('E_DSV_RECOVERY', 'The supplied evidence differs from the reviewed hash')
    source = source.resolve(strict=True)
    target = target.absolute()
    if target.exists() or target.is_symlink():
        raise TraderError('E_DUPLICATE_CONFLICT', 'Recovery requires a new output directory')
    calendar = TradingCalendar.from_record(strict_loads(calendar_path.read_bytes()))
    pair = _check_pair(evidence, calendar)
    account = evidence['account_key']
    with AccountLock(account) as lease, _readonly(source) as original:
        _source_schema(original)
        scope = original.execute('SELECT account_key,environment,broker_identity FROM ledger_scope').fetchone()
        rows = original.execute("""SELECT manifest_json,manifest_hash FROM approval_manifests
                                WHERE json_extract(manifest_json,'$.schema_version')='SIMNOW_WORKBENCH_SIM/1'""").fetchall()
        watermark = str(original.execute('SELECT MAX(event_seq) FROM event_journal').fetchone()[0])
        if (scope is None or tuple(scope) != (account, 'ONLINE_SIM', 'TQ_SIMNOW') or len(rows) != 1
                or watermark != evidence['source_event_seq']):
            raise TraderError('E_DSV_RECOVERY', 'Source account, original grant or event watermark differs')
        grant = DsvSimulationGrant.parse(rows[0]['manifest_json'])
        if (grant.content_hash != rows[0]['manifest_hash'] or not grant.manual_workbench
                or calendar.content_hash != grant.value['calendar_hash']):
            raise TraderError('E_DSV_RECOVERY', 'The original approved manual scope and its exact calendar are required')
        source_digest = _source_digest(original)
        target.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=target.parent, prefix='.simnow-flat-recovery-') as temp:
            stage = Path(temp) / 'recovered'
            stage.mkdir()
            copy = stage / 'ledger.db'
            with closing(sqlite3.connect(copy)) as writer:
                original.backup(writer)
            port = ArchivedSimNowPort(evidence, calendar, grant)
            kernel = Kernel(copy, account, port=port, simulation_grant=grant,
                            risk_budget=grant.value['risk_budget'], _execution_lock=lease, _archive_source=source)
            try:
                _recover_cycle(kernel, port, pair)
            finally:
                kernel.close()
            with _readonly(copy) as repaired:
                _source_schema(repaired)
                blockers = [code for code, sql in _COLD_CHECKS.items() if repaired.execute(sql).fetchone()]
                if blockers:
                    raise TraderError('E_DSV_RECOVERY', 'Unresolved ledger facts: ' + ', '.join(sorted(blockers)))
                result = {'type': 'SIMNOW_FLAT_RECOVERY_RECEIPT/1', 'source_digest': source_digest,
                    'source_event_seq': watermark, 'repaired_digest': _source_digest(repaired),
                    'evidence_hash': content_hash(evidence), 'trading_day': evidence['trading_day'],
                    'contract': pair[0]['contract_id'], 'original_fill_quantity': pair[1]['quantity'],
                    'external_close_quantity': pair[3]['quantity'], 'execution_authority': 'DISARMED'}
            (stage / 'recovery-receipt.json').write_bytes(canonical_bytes(result))
            stage.rename(target)
    return result
