"""Explicit SimNow login and parameter calculation ONLY; no execution owner.

Both public order APIs and outbound order packets are blocked before login.
Use the pinned SDK interpreter with -B -I. Never print credential values.
"""
import argparse
from collections import Counter
import csv
from datetime import datetime,timezone
from io import StringIO
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from online_trader.configuration.offsets import dsv_startup_profile
from online_trader.configuration.files import publish_new_bytes
from online_trader.configuration.symbol_csv import HEADERS,FIELDS
from online_trader.contracts.codec import canonical_bytes,strict_loads
from online_trader.gateway.simnow import SimNowDsvPort,credentials
from online_trader.gateway.daily_facts import NativeDailyFeed
from online_trader.online_scope import DsvSimulationGrant
from online_trader.runtime.auto_offsets import compile_preparation,cache_path
from online_trader.runtime.comparison import read_dsv_profile
from online_trader.runtime.dsv_simulation import load_inputs,validate_inputs
from online_trader.runtime.instance import AccountLock
from online_trader.scheduling.reference import measure_reference,clock_observation


def main():
    parser=argparse.ArgumentParser()
    for name in ('packet','credentials','dsv-config','output'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    result={'status':'STARTING','purpose':'SIMNOW_READ_ONLY_DAILY_CALCULATION_AND_PARAMETER_COMPARISON',
        'connection_authorized':True,'orders_authorized':False,'execution_owner_created':False,
        'entry_authority':'DISARMED','started_at_system_utc':datetime.now(timezone.utc).isoformat()}
    calls=Counter();packets=Counter();port=None;lease=None;apis=[]
    def save(name,value):return publish_new_bytes(canonical_bytes(value),args.output/name)
    def deny(name):
        def blocked(*a,**kw):
            calls[name]+=1
            raise RuntimeError('READ_ONLY_ORDER_API_BLOCKED')
        return blocked
    try:
        source,profile,calendar=load_inputs(args.packet/'scope.json',args.packet/'dsv-profile.json',args.packet/'calendar.json')
        if not source.automatic_offsets:raise RuntimeError('EXPECTED_AUTOMATIC_SOURCE_SCOPE')
        if Path(source.value['ledger_path']).exists():raise RuntimeError('EXISTING_EXECUTION_LEDGER_REQUIRES_ITS_ORIGINAL_OWNER')
        current=read_dsv_profile(args.dsv_config)
        if current.content_hash!=profile.content_hash or cache_path(source).exists():
            value=source.value;value.update(grant_id='readonly-'+uuid.uuid4().hex,
                profile_hash=current.content_hash,ledger_path=str((args.output/'unused-ledger.db').resolve()),
                authorization_reference='USER_READ_ONLY_CALCULATE_20261110_NO_ORDERS')
            source=DsvSimulationGrant.parse(canonical_bytes(value));profile=current
            validate_inputs(source,profile,calendar)
        lease=AccountLock(source.account_key)
        auth=credentials(args.credentials);secret_values=tuple(auth.values());del auth
        # SDK errors can contain login identities. Redact any console output;
        # persisted evidence below only uses whitelisted normalized facts.
        class Redactor:
            def __init__(self,stream):self.stream=stream
            def write(self,value):
                for secret in secret_values:
                    if secret:value=value.replace(secret,'[REDACTED]')
                return self.stream.write(value)
            def flush(self):return self.stream.flush()
            def __getattr__(self,name):return getattr(self.stream,name)
        sys.stdout=Redactor(sys.stdout);sys.stderr=Redactor(sys.stderr)
        from tqsdk import TqApi
        original_send=TqApi._send_pack;original_init=TqApi.__init__
        def init(api,*a,**kw):
            apis.append(api);return original_init(api,*a,**kw)
        def send(api,pack):
            aid=pack.get('aid','UNKNOWN');packets[aid]+=1
            if aid in {'insert_order','cancel_order','req_order_insert','req_order_action'}:
                calls['outbound_order_packet']+=1
                raise RuntimeError('READ_ONLY_ORDER_PACKET_BLOCKED')
            return original_send(api,pack)
        TqApi.__init__=init;TqApi._send_pack=send
        TqApi.insert_order=deny('insert_order');TqApi.cancel_order=deny('cancel_order')
        clock=measure_reference();observation=clock_observation(clock);save('clock-before.json',observation)
        if not observation['system_aligned']:raise RuntimeError('WINDOWS_REFERENCE_CLOCK_NOT_ALIGNED')
        result.update(source_scope_hash=source.content_hash,source_profile_hash=profile.content_hash,
            cabinet_day=source.value['trading_day'],account_key=source.account_key)
        save('read-only-source-scope.json',source.value)
        port=SimNowDsvPort.connect(args.credentials,source,clock,read_only=True);port.configure_calendar(calendar)
        result['broker_connected']=port.broker_connected
        save('native-before.json',port.snapshot())
        feed=NativeDailyFeed(port,profile)
        try:dataset,ranges,evidence=feed.wait(timeout_seconds=45)
        except Exception:
            diagnostics={}
            for identity,serial in feed.serials.items():
                rows=serial.to_dict('records')[-5:]
                diagnostics[identity]={'ready':bool(port.api.is_serial_ready(serial)),
                    'rows':[{'native_id':str(r.get('id')),'datetime_ns':str(r.get('datetime')),
                             'high':str(r.get('high')),'low':str(r.get('low')),'close':str(r.get('close'))} for r in rows]}
            save('daily-readiness-diagnostics.json',diagnostics)
            raise
        record,derived,effective=compile_preparation(source,profile,dataset,ranges,evidence)
        save('calculation-preparation.json',record)
        save('daily-evidence.json',evidence);save('daily-dataset.json',{'data_hash':dataset.content_hash,'rows':dataset.rows})
        save('effective-profile.json',effective.to_record())
        publish_new_bytes(effective.to_json().encode('utf-8'),args.output/'calculated-business-settings.json')
        csv_stream=StringIO(newline='');writer=csv.writer(csv_stream);writer.writerow(HEADERS[:8])
        for row in effective.to_record()['settings']['symbols']:writer.writerow([row.get(k,'') for k in FIELDS[:8]])
        publish_new_bytes(csv_stream.getvalue().encode('utf-8-sig'),args.output/'dsv-calculated-symbols.csv')
        # This is a calculation cache, not an execution ledger or entry command.
        publish_new_bytes(canonical_bytes(record),cache_path(source))
        dsv=read_dsv_profile(args.dsv_config);interpreted=dsv_startup_profile(dsv)
        fields=('open_offset','stop_offset','profit_offset');saved={r['code']:r for r in dsv.to_record()['settings']['symbols']}
        before={r['code']:r for r in interpreted.to_record()['settings']['symbols']};after={r['code']:r for r in effective.to_record()['settings']['symbols']}
        rows=[]
        for code in source.value['contracts']:
            a=before[code];b=after[code]
            rows.append({'contract_id':code,'name':b['name'],'dsv_saved':[saved[code][k] for k in fields],
                'dsv_startup_interpreted':[a[k] for k in fields],'online_trader_calculated':[b[k] for k in fields],
                'offsets_equal':all(a[k]==b[k] for k in fields)})
        comparison={'source':'DSV_SAVED_CONFIGURATION_WITH_VERIFIED_STARTUP_INTERPRETATION',
            'dsv_running_memory_verified':False,'dsv_saved_profile_hash':dsv.content_hash,
            'dsv_interpreted_profile_hash':interpreted.content_hash,'product_effective_profile_hash':effective.content_hash,
            'all_effective_parameters_equal':interpreted.content_hash==dsv_startup_profile(effective).content_hash,
            'rows':rows,'dsv_files_modified':False,'new_product_execution_activated':False}
        save('parameter-comparison.json',comparison)
        port.poll_updates(.2);save('native-after.json',port.snapshot())
        result.update(status='CALCULATED_AND_COMPARED_READ_ONLY',calculation_hash=record['preparation_hash'],
            effective_profile_hash=effective.content_hash,dataset_hash=dataset.content_hash,
            completed_cutoff=source.value['auto_offsets']['last_completed_day'],
            all_effective_parameters_equal=comparison['all_effective_parameters_equal'],rows=rows,
            cache_file=str(cache_path(source)),dsv_runtime_memory_verified=False)
    except BaseException as error:
        result.update(status='FAILED_READ_ONLY',error_code=getattr(error,'code',type(error).__name__))
    finally:
        closed=[]
        for api in reversed(apis):
            try:api.close();closed.append(True)
            except Exception:closed.append(False)
        if port is not None:port._closed=True
        if lease is not None:lease.close()
        result.update(order_api_calls=dict(calls),outbound_aid_counts=dict(packets),sdk_instances=len(apis),
            sdk_close_results=closed,orders_sent=packets.get('insert_order',0),cancellations_sent=packets.get('cancel_order',0),
            execution_ledger_created=False,finished_at_system_utc=datetime.now(timezone.utc).isoformat())
        save('result.json',result)
        print(__import__('json').dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['status']=='CALCULATED_AND_COMPARED_READ_ONLY' else 2


if __name__=='__main__':raise SystemExit(main())
