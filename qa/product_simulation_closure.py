"""Read-only audit of a completed product simulation and fresh native login."""
from contextlib import closing
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from pathlib import Path
import argparse,hashlib,json,sqlite3

def audit(root):
    root=Path(root);run=root/'run';scope=json.loads((root/'scope.json').read_text(encoding='utf-8'))
    result=json.loads((run/'result.json').read_text(encoding='utf-8'));final=json.loads((run/'final-relogin.json').read_text(encoding='utf-8'))
    money=json.loads((run/'final-money-report.json').read_text(encoding='utf-8'))
    assert result['status']=='CLOSED' and final['status']=='PASS' and final['native_snapshot']['complete']
    assert len(result['cycles'])==1;cycle=result['cycles'][0];code=cycle['plan']['contract_id'];cid=cycle['position_cycle_id']
    assert cycle['plan']['execution_scope']=='ONLINE_SIMULATION' and cycle['plan']['approval_record_id']==scope['grant_id']
    assert cycle['plan']['initial_open_quantity']==1 and cycle['lifecycle']=='CLOSED' and cycle['protection']['owned_qty']==0
    assert len([o for o in cycle['orders'] if o['effect']=='OPEN'])==1
    assert sum(o['filled_qty'] for o in cycle['orders'] if o['effect']=='OPEN')==sum(o['filled_qty'] for o in cycle['orders'] if o['effect']=='REDUCE')==1
    assert all(o['terminal_confirmed'] and o['reported_fill_qty']==o['filled_qty'] and o['send_state']!='UNKNOWN' for o in cycle['orders'])
    owned=set(cycle['native_order_keys'].values());snapshot=final['native_snapshot']
    native={o['order_id']:o for o in snapshot['orders'] if o['order_id'] in owned}
    trades=[t for t in snapshot['trades'] if t['order_id'] in owned];by_trade={t['trade_id']:t for t in trades}
    assert len(trades)==2 and all(t['quantity']==1 and t['contract_id']==code for t in trades)
    assert all(o['status']=='FINISHED' for o in native.values())
    assert not any(p['contract_id']==code and (p['long'] or p['short']) for p in snapshot['positions'])
    opened=next(t for t in trades if t['offset']=='OPEN');closed=next(t for t in trades if t['offset']=='CLOSE')
    assert opened['direction']=='BUY' and closed['direction']=='SELL'
    interval=Decimal(int(closed['source_time_ns'])-int(opened['source_time_ns']))/Decimal(1_000_000_000)
    deadline=cycle['online_deadline'];assert deadline['first_fill_source_time_ns']==opened['source_time_ns']
    expected_due=Decimal(opened['source_time_ns'])/Decimal(1_000_000_000)+60
    elapsed=datetime.fromisoformat(deadline['due_reference_utc'].replace('Z','+00:00'))-datetime(1970,1,1,tzinfo=timezone.utc)
    actual_due=Decimal(elapsed.days*86400+elapsed.seconds)+Decimal(elapsed.microseconds)/1_000_000
    assert actual_due>=expected_due
    ledger=root/'ledger.db'
    with closing(sqlite3.connect(ledger.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
        conn.row_factory=sqlite3.Row
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        ledger_scope=dict(conn.execute('SELECT * FROM ledger_scope').fetchone());assert ledger_scope['environment']=='ONLINE_SIM' and ledger_scope['broker_identity']=='TQKQ'
        journal=[dict(r) for r in conn.execute('SELECT * FROM event_journal ORDER BY event_seq')]
        for seq,row in enumerate(journal,1):
            assert row['event_seq']==seq and hashlib.sha256(row['payload_json'].encode()).hexdigest()==row['payload_hash']
        fills=[dict(r) for r in conn.execute('SELECT * FROM fill_facts')];assert len(fills)==2
        for fill in fills:
            assert fill['trade_key'].startswith('kq-');native_fill=by_trade[fill['trade_key'][3:]]
            assert fill['quantity']==native_fill['quantity'] and fill['price']==native_fill['price'] and fill['gateway_identity']==snapshot['gateway_identity']
        allocations=conn.execute('SELECT COUNT(*) FROM fill_allocations WHERE position_cycle_id=?',(cid,)).fetchone()[0];assert allocations==2
        assert conn.execute('SELECT new_open_status FROM authority').fetchone()[0]=='DISARMED'
        child_ids={r[0] for r in conn.execute('SELECT child_order_id FROM child_orders')};assert child_ids==set(cycle['native_order_keys'])
        for attempt in conn.execute('SELECT child_order_id,prepared_event_seq FROM send_attempts'):
            prepared=next(r for r in journal if r['event_seq']==attempt['prepared_event_seq'])
            assert prepared['event_type'] in {'ENTRY_PREPARED','EXIT_PREPARED'}
        opening_fill=next(r for r in journal if r['event_type']=='FILL_CONFIRMED' and json.loads(r['payload_json'])['data']['fill']['effect']=='OPEN')
        assert json.loads(opening_fill['payload_json'])['data']['cycle']['online_deadline']==deadline
    multiplier=Decimal(cycle['instrument']['multiplier']);gross=(Decimal(closed['price'])-Decimal(opened['price']))*multiplier
    assert Decimal(money['summary']['gross_realized_pnl'])==gross
    baseline=cycle['online_entry_baseline'];old_orders={o['order_id']:o for o in baseline['orders']};new_orders={o['order_id']:o for o in snapshot['orders']}
    old_trades={t['trade_id']:t for t in baseline['trades']};new_trades={t['trade_id']:t for t in snapshot['trades']}
    changed_orders=[key for key,value in old_orders.items() if new_orders.get(key)!=value]
    changed_trades=[key for key,value in old_trades.items() if new_trades.get(key)!=value]
    def display(stamp):
        seconds,ns=divmod(int(stamp),1_000_000_000)
        return datetime.fromtimestamp(seconds,timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')+f'.{ns:09}'
    report={'status':'PASS','scope':'PRODUCT_KERNEL_BOUNDED_TQKQ_ONLINE_SIMULATION','product_acceptance_scope':'ONE_MANUAL_OPEN_AND_OWNED_EXIT_ONLY',
        'contract_id':code,'quantity':1,'entry_price':opened['price'],'exit_price':closed['price'],
        'entry_native_time_UTC8':display(opened['source_time_ns']),'exit_native_time_UTC8':display(closed['source_time_ns']),
        'native_fill_interval_seconds':str(interval),'one_minute_reached':interval>=60,'exit_reasons':cycle['reasons'],
        'gross_realized_pnl':str(gross),'net_realized_pnl':money['summary'].get('net_realized_pnl'),
        'owned_quantity':0,'active_owned_orders':0,'selected_contract_flat':True,'final_fresh_login':True,
        'foreign_final_positions':snapshot['positions'],'preexisting_order_records_changed':changed_orders,'preexisting_trade_records_changed':changed_trades,
        'foreign_new_order_ids':sorted(set(new_orders)-set(old_orders)-owned),
        'original_ledger_fill_count':len(fills),'original_ledger_fill_allocations':allocations,'journal_events':len(journal),
        'journal_payload_hashes':'PASS','sqlite_integrity':'PASS','durable_intent_before_send_result':'PASS','first_fill_and_deadline_atomic':'PASS',
        'final_authority':'DISARMED','ledger_sha256':hashlib.sha256(ledger.read_bytes()).hexdigest(),
        'final_relogin_sha256':hashlib.sha256((run/'final-relogin.json').read_bytes()).hexdigest()}
    with (root/'closure-audit.json').open('x',encoding='utf-8') as stream:json.dump(report,stream,ensure_ascii=False,indent=2)
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    print(json.dumps(audit(parser.parse_args().root),ensure_ascii=False))
