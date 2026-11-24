"""Fresh, bounded TqSdk account observations with a read-only wire allowlist."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from importlib.metadata import version
import json
import math
from pathlib import Path
import sys
import time
from unittest.mock import patch
from contextlib import ExitStack

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from online_trader.accounts import account_identity
from online_trader.gateway.simnow import account_fingerprint, credentials
from online_trader.runtime.instance import AccountLock
from online_trader.runtime.startup_metadata import SDK_VERSION


BEIJING=timezone(timedelta(hours=8))
READ_ONLY_PACKETS={'req_login','peek_message','subscribe_quote','subscribe_trading_status','ins_query'}
POSITION_FIELDS=('exchange_id','instrument_id','pos_long','pos_short','pos_long_today','pos_long_his',
                 'pos_short_today','pos_short_his','open_price_long','open_price_short')
DETAIL_KEYS={'open_date','open_datetime','open_trade_id','trade_id','exchange_trade_id','position_detail','position_details'}


def stamp():
    return datetime.now(BEIJING).isoformat(timespec='seconds')


def cabinet_date_hint(value):
    if type(value) is int and value>0:
        return datetime.fromtimestamp(value//1_000_000_000,BEIJING).date().isoformat()
    return None


def identity(value):
    return sha256(value.encode()).hexdigest()[:16]


def presence(api,account):
    orders=api.get_order(account=account);trades=api.get_trade(account=account)
    positions=api.get_position(account=account)
    # A typed placeholder for an unknown ID has empty instrument and zero size.
    order_rows={oid:row for oid,row in orders.items() if row.instrument_id and row.volume_orign>0}
    trade_rows={tid:row for tid,row in trades.items() if row.instrument_id and row.volume>0}
    def position_fields(row):
        result={}
        for name in POSITION_FIELDS:
            value=getattr(row,name,None)
            result[name]=None if type(value) is float and not math.isfinite(value) else value
        return result
    nonzero=[position_fields(row) for row in positions.values() if row.pos_long or row.pos_short]
    native_node=api._data.get('trade',{}).get(api._account._get_account_key(account),{})
    public_account=api.get_account(account=account)
    account_day={key:value for key,value in dict(native_node).items() if key in {'trading_day','trading_date'}
        and isinstance(value,(str,int))}
    for key in ('trading_day','trading_date'):
        value=getattr(public_account,key,None)
        if value and isinstance(value,(str,int)):account_day[f'account.{key}']=value
    return {
        'synchronized':not api._account._get_trade_more_data(api._data),
        'account_balance_present':type(public_account.balance) in {int,float} and math.isfinite(public_account.balance),
        'collection_counts':{'orders':len(order_rows),'trades':len(trade_rows),'nonzero_positions':len(nonzero)},
        'activity_digest':sha256(json.dumps({
            'orders':sorted((oid,row.status,row.volume_left) for oid,row in order_rows.items()),
            'trades':sorted((tid,row.order_id,row.volume,row.trade_date_time) for tid,row in trade_rows.items()),
            'positions':nonzero},sort_keys=True).encode()).hexdigest(),
        'trade_node_fields':sorted(k for k in native_node if not k.startswith('_')),
        'broker_day_fields':account_day,
        'native_order_local_dates':dict(Counter(cabinet_date_hint(row.insert_date_time) or 'UNKNOWN' for row in order_rows.values())),
        'native_trade_local_dates':dict(Counter(cabinet_date_hint(row.trade_date_time) or 'UNKNOWN' for row in trade_rows.values())),
        'native_trade_fields':sorted(set().union(*(set(row.keys()) for row in trade_rows.values()))),
        'native_order_fields':sorted(set().union(*(set(row.keys()) for row in order_rows.values()))),
        'nonzero_position_rows':nonzero,
        'opening_batch_identity_fields_on_nonzero_positions':sorted(set().union(*(
            set(row.keys())&DETAIL_KEYS for row in positions.values() if row.pos_long or row.pos_short))),
        'raw_trade_lookup_keys':len(trades),'raw_order_lookup_keys':len(orders),
    }


def observe(provider, auth, prior, result):
    from tqsdk import TqAccount, TqApi, TqAuth, TqKq
    from tqsdk.connect import TqConnect
    from tqsdk.objs import Position
    from tqsdk.tradeable.mixin import FutureMixin

    kq=provider=='TqKq'
    account_key=account_identity(auth['kq_username'])[0] if kq else 'SIM:SIMNOW:'+account_fingerprint(auth['simnow_username'])
    result.update(started_at=stamp(),account_fingerprint=account_key.rsplit(':',1)[1],sdk=version('tqsdk'),
        historical_source='LOCAL_ARCHIVE_ID_CHECK' if prior and prior['account_key']==account_key else 'NO_BOUND_ORIGINAL_SAMPLE',
        available_trade_methods={name:hasattr(TqApi,name) or hasattr(FutureMixin,name) for name in
            ('get_order','get_trade','get_position','get_position_detail','get_investor_position_detail',
             'query_investor_position_detail','get_settlement_info','get_history_orders','get_history_trades')})
    result['sdk_position_schema_fields']=sorted(k for k in Position(None).keys() if not k.startswith('_'))
    api_calls=Counter();frames=Counter();blocked=Counter();suppressed=Counter();closed=[]
    original_send=TqConnect._send_handler
    original_close=TqApi.close
    original_init=TqApi.__init__
    instances=[]

    def track_init(api,*args,**kwargs):
        instances.append(api)
        return original_init(api,*args,**kwargs)

    def track_close(api,*args,**kwargs):
        value=original_close(api,*args,**kwargs)
        closed.append(id(api))
        return value

    def reject(name):
        def denied(*args,**kwargs):
            api_calls[name]+=1
            raise RuntimeError('READ_ONLY_API_BLOCK')
        return denied

    async def guarded_send(connection, channel, client):
        async def safe_frames():
            async for packet in channel:
                action=packet.get('aid','UNKNOWN')
                if action=='confirm_settlement':
                    suppressed[action]+=1
                    continue
                if action not in READ_ONLY_PACKETS:
                    blocked[action]+=1
                    raise RuntimeError('READ_ONLY_FRAME_BLOCK')
                frames[action]+=1
                yield packet
        return await original_send(connection,safe_frames(),client)

    # Hide identifiers in SDK console logging while retaining exception type.
    secrets=tuple(auth.values())
    class Redactor:
        def __init__(self, stream):self.stream=stream
        def write(self,value):
            for secret in secrets:
                if secret:value=value.replace(secret,'[REDACTED]')
            return self.stream.write(value)
        def __getattr__(self,name):return getattr(self.stream,name)

    with AccountLock(account_key), ExitStack() as guard:
        guard.enter_context(patch.object(sys,'stdout',Redactor(sys.stdout)))
        guard.enter_context(patch.object(sys,'stderr',Redactor(sys.stderr)))
        guard.enter_context(patch.object(TqConnect,'_send_handler',guarded_send))
        guard.enter_context(patch.object(TqApi,'__init__',track_init))
        guard.enter_context(patch.object(TqApi,'close',track_close))
        for name in ('insert_order','cancel_order','set_risk_management_rule'):
            guard.enter_context(patch.object(TqApi,name,reject(name)))
        api=None
        try:
            account=TqKq() if kq else TqAccount('simnow',auth['simnow_username'],auth['simnow_password'])
            api=TqApi(account=account,auth=TqAuth(auth['kq_username'],auth['kq_password']),
                      debug=False,web_gui=False,disable_print=True)
            before=presence(api,account)
            result['first_snapshot']=before
            if not before['synchronized'] or not before['account_balance_present']:
                raise RuntimeError('ACCOUNT_SYNC_INCOMPLETE')
            if prior and prior['account_key']==account_key:
                result['original_sample_day']=prior['trading_day']
                order_ids={r['order_id'] for r in prior['orders'] if r['contract_id']=='DCE.l2701'}
                trade_ids={r['trade_id'] for r in prior['trades'] if r['order_id'] in order_ids}
                if len(order_ids)!=2 or len(trade_ids)!=2:
                    raise RuntimeError('ORIGINAL_SAMPLE_NOT_BOUND')
                result['known_id_checks']=[]
                for kind,ids,getter,attribute in (('order',order_ids,api.get_order,'volume_orign'),
                                                   ('trade',trade_ids,api.get_trade,'volume')):
                    for native_id in sorted(ids):
                        seen=native_id in getter(account=account)
                        row=getter(native_id,account=account)
                        result['known_id_checks'].append({'kind':kind,'id_digest':identity(native_id),'present_before_lookup':seen,
                            'present_after_lookup':native_id in getter(account=account),
                            'initial_native_quantity':getattr(row,attribute),'initial_contract_present':bool(row.instrument_id)})
                deadline=time.monotonic()+3
                while time.monotonic()<deadline:api.wait_update(deadline=time.time()+.35)
                for kind,ids,getter,attribute in (('order',order_ids,api.get_order,'volume_orign'),
                                                   ('trade',trade_ids,api.get_trade,'volume')):
                    updates=[entry for entry in result['known_id_checks'] if entry['kind']==kind]
                    for native_id,entry in zip(sorted(ids),updates):
                        row=getter(native_id,account=account)
                        entry.update(final_native_quantity=getattr(row,attribute),final_contract_present=bool(row.instrument_id))
            else:
                api.wait_update(deadline=time.time()+1)
            result['last_snapshot']=presence(api,account)
            result['status']='COMPLETE' if result['last_snapshot']['synchronized'] else 'INCOMPLETE'
        except Exception as error:
            result.update(status='INCOMPLETE',error_type=type(error).__name__,error_code=getattr(error,'code',None))
        finally:
            for candidate in instances:
                if id(candidate) not in closed:
                    try:candidate.close()
                    except Exception:pass
            result.update(finished_at=stamp(),sdk_instances=len(instances),sdk_closed=len(set(closed)),
                wire_actions=dict(frames),blocked_wire_actions=dict(blocked),suppressed_wire_actions=dict(suppressed),
                denied_mutating_api_calls=dict(api_calls),execution_owner_created=False)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('provider',choices=('TqKq','SimNow'))
    parser.add_argument('--credentials',type=Path,required=True)
    parser.add_argument('--prior-kq-evidence',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if version('tqsdk')!=SDK_VERSION:raise RuntimeError('PINNED_SDK_REQUIRED')
    auth=credentials(args.credentials)
    prior=json.loads(args.prior_kq_evidence.read_bytes())['native_snapshot'] if args.provider=='TqKq' and args.prior_kq_evidence else None
    result={'provider':args.provider}
    observe(args.provider,auth,prior,result)
    with args.output.open('x',encoding='utf-8') as stream:json.dump(result,stream,ensure_ascii=False,indent=2,allow_nan=False)
    print(json.dumps({'provider':args.provider,'status':result['status'],'output':str(args.output)},ensure_ascii=False))
    return 0 if result['status']=='COMPLETE' and result['sdk_closed']==result['sdk_instances'] and not result['blocked_wire_actions'] and not result['denied_mutating_api_calls'] else 2


if __name__=='__main__':
    raise SystemExit(main())
