"""Reader for the disarmed, append-only same-day rollback handoff."""
from hashlib import sha256

from online_trader.contracts.codec import content_hash, strict_loads
from online_trader.errors import TraderError


EVENT = 'DSV_SAME_DAY_ROLLBACK'


def read_handoffs(ledger, account):
    sql = """SELECT e.payload_json,e.payload_hash,e.kernel_boot_id,b.executable_hash,b.account_key,b.stopped_at_utc
             FROM event_journal e JOIN kernel_boots b USING(kernel_boot_id)
             WHERE e.event_type=? ORDER BY e.event_seq"""
    for row in ledger.query(sql, (EVENT,)):
        raw = row['payload_json'].encode('utf-8')
        material = strict_loads(raw)['data']['dsv_rollback']
        digest = content_hash(material)
        if (sha256(raw).hexdigest() != row['payload_hash'] or material['account_key'] != account
                or row['account_key'] != account or row['kernel_boot_id'] != 'reverse-' + digest[:48]
                or row['executable_hash'] != digest or not row['stopped_at_utc']):
            raise TraderError('E_DSV_ROLLBACK', 'Legacy rollback handoff identity does not match the archive')
        yield material
