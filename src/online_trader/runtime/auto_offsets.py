"""Compile a source-bound automatic setup into one immutable execution scope.

Only offsets change. A cache belongs to one original source scope/cabinet day;
restart reproduces it instead of recalculating or changing existing plans.
"""
from io import StringIO
from pathlib import Path
import csv
import sqlite3

from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.offsets import DailyDataset,calculate_offsets,formula_source,STOPS
from online_trader.configuration.files import publish_new_bytes
from online_trader.contracts.codec import canonical_bytes,content_hash,strict_loads
from online_trader.errors import TraderError
from online_trader.online_scope import DsvSimulationGrant


def cache_path(source):
    return Path(source.value['ledger_path']).parent/'auto-offsets'/source.content_hash/'preparation.json'


def fixed_stops(profile,contracts):
    return {r['code']:str(STOPS.get(r['name'],15)) for r in profile.to_record()['settings']['symbols'] if r['code'] in contracts}


def compile_preparation(source,profile,dataset,ranges,evidence):
    if (not source.automatic_offsets or source.value['profile_hash']!=profile.content_hash
            or not isinstance(dataset,DailyDataset)):
        raise TraderError('E_AUTO_OFFSETS','Automatic calculation needs its exact source scope, profile and daily dataset')
    policy=source.value['auto_offsets'];codes=source.value['contracts'];settings=profile.to_record()['settings']
    if type(ranges) is not dict or set(ranges)!=set(codes):
        raise TraderError('E_AUTO_OFFSETS','Each scoped contract needs its explicit native current-day range')
    for symbol in settings['symbols']:
        if symbol['code'] not in codes:continue
        identity=formula_source(settings['auto_offset']['formula'],symbol['name'],symbol['code'])[1]
        completed=[r for r in dataset.rows if r['series_id']==identity and r['date']<policy['as_of_date']]
        if not completed or completed[-1]['date']!=policy['last_completed_day']:
            raise TraderError('E_AUTO_OFFSETS','The exact completed daily cutoff is absent; no stale fallback is permitted')
    calculation=calculate_offsets(profile,dataset,as_of_date=policy['as_of_date'],realized_ranges=ranges)
    rows={r['contract_id']:r for r in calculation.to_record()['rows']}
    if any(rows.get(code,{}).get('status')!='CALCULATED' for code in codes):
        raise TraderError('E_AUTO_OFFSETS','Every scoped contract needs a successful source-bound calculation')
    effective=calculation.apply(profile,codes)
    digest=content_hash({'source_scope_hash':source.content_hash,'calculation':calculation.to_record()})
    value=source.value;value.pop('auto_offsets');value.update(schema_version='SIMNOW_DSV_SIM/1',
        grant_id='auto-'+digest[:48],profile_hash=effective.content_hash,authorization_reference='AUTO:'+source.content_hash)
    derived=DsvSimulationGrant.parse(canonical_bytes(value))
    result={'schema_version':'DSV_AUTO_PREPARATION/1','source_scope_hash':source.content_hash,
        'source_scope':source.value,'source_profile':profile.to_record(),'effective_scope':derived.value,
        'effective_profile':effective.to_record(),'daily_rows':dataset.rows,'realized_ranges':ranges,
        'calculation':calculation.to_record(),'source_evidence':evidence,
        'offset_changes':[{'contract_id':code,'before':rows[code]['current'],
                           'after':[rows[code][k] for k in ('open','stop','profit')]} for code in codes],
        'scope_limits_changed':False,'existing_plans_changed':False}
    result['preparation_hash']=content_hash(result)
    return result,derived,effective


def dataset_from_rows(rows):
    stream=StringIO();writer=csv.DictWriter(stream,('series_id','date','high','low','close'));writer.writeheader();writer.writerows(rows)
    return DailyDataset.from_csv(stream.getvalue())


def verify_preparation(source,profile,record):
    if (type(record) is not dict or record.get('schema_version')!='DSV_AUTO_PREPARATION/1'
            or record.get('source_scope_hash')!=source.content_hash or record.get('source_scope')!=source.value
            or record.get('source_profile')!=profile.to_record()):
        raise TraderError('E_AUTO_OFFSETS','Saved automatic preparation belongs to a different original scope or profile')
    expected,derived,effective=compile_preparation(source,profile,dataset_from_rows(record['daily_rows']),
        record['realized_ranges'],record['source_evidence'])
    if record!=expected:raise TraderError('E_AUTO_OFFSETS','Saved automatic calculation cannot be reproduced exactly')
    return record,derived,effective


def load_preparation(source,profile):
    path=cache_path(source);ledger=Path(source.value['ledger_path'])
    conn=None;record=None
    if ledger.exists():
        try:
            conn=sqlite3.connect(ledger.resolve().as_uri()+'?mode=ro',uri=True)
            app_id=conn.execute('PRAGMA application_id').fetchone()[0]
            has_tables=bool(conn.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone())
            if app_id==0x44535631 or not has_tables:
                conn.close();conn=None
            else:
                identity=conn.execute('SELECT account_key,broker_identity FROM ledger_scope WHERE singleton=1').fetchone()
                if identity!=(source.account_key,'TQ_SIMNOW'):raise TraderError('E_AUTO_OFFSETS','Original automatic ledger identity differs')
                saved=conn.execute("SELECT payload_json FROM event_journal WHERE event_type='ONLINE_AUTO_OFFSETS_APPLIED' ORDER BY event_seq DESC LIMIT 1").fetchone()
                if saved:
                    material=strict_loads(saved[0]);record=material.get('data',material).get('preparation')
        except sqlite3.Error:
            if conn is not None:conn.close()
            raise TraderError('E_AUTO_OFFSETS','Cannot verify the original automatic ledger; no new calculation is permitted') from None
        except BaseException:
            if conn is not None:conn.close()
            raise
    try:
        if record is None:
            if not path.exists():
                if conn is not None:raise TraderError('E_AUTO_OFFSETS','An existing ledger has no original automatic preparation; do not recalculate it')
                return None
            with path.open('rb') as stream:raw=stream.read(4*1024*1024+1)
            if len(raw)>4*1024*1024:raise TraderError('E_AUTO_OFFSETS','Saved automatic preparation exceeds its bound')
            record=strict_loads(raw)
        result=verify_preparation(source,profile,record)
        if conn is not None:
            grant=result[1];rows=conn.execute('SELECT manifest_hash FROM approval_manifests WHERE approval_record_id=?',(grant.value['grant_id'],)).fetchall()
            if rows and rows!=[(grant.content_hash,)]:raise TraderError('E_AUTO_OFFSETS','Saved automatic scope and original approval differ')
            if not rows and conn.execute('SELECT COUNT(*) FROM protection_plans').fetchone()[0]:
                raise TraderError('E_AUTO_OFFSETS','An occupied ledger cannot adopt a new automatic scope')
        return result
    finally:
        if conn is not None:conn.close()


def prepare_connected(source,profile,port):
    """Called by explicit run only, using its existing connected port."""
    from online_trader.gateway.daily_facts import NativeDailyFeed
    dataset,ranges,evidence=NativeDailyFeed(port,profile).wait()
    record,derived,effective=compile_preparation(source,profile,dataset,ranges,evidence)
    if port._issued or getattr(port,'kernel',None) is not None:
        raise TraderError('E_AUTO_OFFSETS','Offset compilation must precede the original execution owner and every native send')
    publish_new_bytes(canonical_bytes(record),cache_path(source))
    # Same account/calendar/limits, no active owner, order binding or send yet.
    port.grant=derived
    return record,derived,effective
