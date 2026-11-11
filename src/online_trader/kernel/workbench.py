"""Bounded read-only workbench view, assembled by the existing owner.

No SDK methods that wait for network data and no second execution state exist.
Business rows come exclusively from accepted commits at the stated watermark.
"""
import copy

from online_trader.contracts.codec import canonical_bytes, strict_loads
from online_trader.domain.orders import OrderState
from online_trader.kernel.projection import projection_batch
from online_trader.errors import TraderError


def workbench_view(kernel):
    kernel._check_owner()
    snapshot=kernel._make_snapshot();port=kernel.port
    records=projection_batch(snapshot)['records'];scheduler=getattr(kernel,'_scheduled_runtime',None)
    runtime=getattr(kernel,'_dsv_runtime',None)
    markets=[];decisions={}
    for row in kernel.ledger.query('SELECT payload_json FROM strategy_proposals WHERE recorded_event_seq<=? ORDER BY recorded_event_seq DESC LIMIT 512',(kernel._durable_seq,)):
        value=strict_loads(row['payload_json']);state=value.get('state',{})
        decisions.setdefault((state.get('contract_id'),state.get('trading_day')),value.get('report') or {})
    for code,row in sorted((runtime._latest if runtime else {}).items(),key=lambda item:str(item[0])):
        event=row['event'];name=event['contract_id']
        profile=runtime.profile.to_record()['settings']['symbols']
        symbol=next((s for s in profile if s['code']==name),{})
        received=row.get('received_ns');fresh=received is not None and kernel._quote_age_ns(received)<=kernel.max_quote_age_ms*1000000
        markets.append({key:event.get(key) for key in ('contract_id','trading_day','session_id','last','bid1','ask1','day_high','day_low','source_time_utc','path_complete')}
            |{'name':symbol.get('name',name),'fresh':fresh,'enabled':kernel.control.symbol_entry_enabled(name,committed=True),
              'entry_switch_revision':kernel.control.symbol_entries.get(name,{}).get('revision',0),
              'entry_reasons':copy.deepcopy(decisions.get(code,{}).get('reasons',[]))})
    cycles=[];orders=[]
    for cycle in snapshot['cycles']:
        p=cycle['plan'];s=cycle['protection'];cid=cycle['position_cycle_id']
        route_review={'status':'NO_OWNED_QUANTITY'}
        if s['owned_qty']:
            from online_trader.execution.close_routes import preview_owned_routes
            try:route_review=preview_owned_routes(kernel,cid,s['owned_qty'])
            except TraderError as error:route_review={'status':'BLOCKED','error_code':error.code,'execution_eligible':False}
        cycles.append({'position_cycle_id':cid,'contract_id':p['contract_id'],'direction':p['direction'],
            'trading_day':p['trading_day'],'lifecycle':cycle['lifecycle'],'remaining_quantity':s['owned_qty'],
            'target_remaining_quantity':s.get('target_remaining_qty'),'mode':s['mode'],'quality':s['quality'],
            'phase1_status':s.get('phase1_status'),'phase2_status':s.get('phase2_status'),
            'break_even_status':s.get('break_even_status'),'max_favorable_move':s.get('max_favorable_move'),
            'management_state':cycle.get('management_state'),'plan_id':p['plan_id'],'plan_revision':p['plan_revision'],'close_route_review':route_review})
        for order in cycle['orders']:
            orders.append({key:order.get(key) for key in ('child_order_id','intent_id','attempt_id','effect','original_qty','filled_qty',
                'limit_price','native_offset','send_state','broker_state','cancel_status','terminal_confirmed')}
                |{'position_cycle_id':cid,'contract_id':p['contract_id'],'potential_remaining':OrderState.from_dict(order).potential_remaining})
    trades=list(kernel.money.book.fills.values())
    commands=[copy.deepcopy(s['receipt']) for s in kernel.control.states.values() if s.get('receipt')]
    policies=[]
    for row in kernel.ledger.query('SELECT policy_id,policy_kind,policy_json FROM policy_revisions'):
        try:kernel.control._policy(row['policy_id'],row['policy_kind'])
        except Exception:continue
        policies.append({'policy_id':row['policy_id'],'policy_kind':row['policy_kind']})
    result={'view_schema':'workbench/1','account_key':kernel.account_key,'kernel_boot_id':kernel.boot_id,
        'checkpoint_event_seq':snapshot['durable_seq'],'entry_authority':'ARMED' if snapshot['armed'] else 'DISARMED',
        'execution_scope':snapshot['execution_scope'],'configuration':copy.deepcopy(kernel._configuration)|{'config_revision':str(kernel._config_revision)},
        'connection':{'broker_connected':bool(getattr(port,'broker_connected',False)),
            'synchronized':bool(getattr(port,'complete',lambda:False)()),'fault':bool(getattr(port,'fault',False)),
            'recovery_required':snapshot['recovery_required'],'storage_failed':snapshot['storage_failed'],
            'clock_fault':copy.deepcopy(scheduler._fault) if scheduler else None,'pending_transactions':len(kernel._pending)},
        'markets':markets,'cycles':cycles,'orders':orders,'trades':copy.deepcopy(trades),
        'blocks':[r for r in records if r['record_type']=='BLOCK']+[{'reason_code':e,'active':True} for e in snapshot['errors'][-64:]],
        'commands':commands,'policies':policies,'truncated':False}
    result['symbol_entry_switches']=copy.deepcopy(kernel.control.symbol_entries)
    # Every table has an explicit cap. Keep the last records; state that the view
    # is incomplete and disable bulk actions at the desktop when it is truncated.
    for key in ('markets','cycles','orders','trades','blocks','commands','policies'):
        if len(result[key])>512:result['truncated']=True;result[key]=result[key][-512:]
    while len(canonical_bytes(result))>800000:
        key=max(('trades','commands','orders','cycles','blocks','markets'),key=lambda k:len(canonical_bytes(result[k])))
        result[key]=result[key][len(result[key])//2+1:];result['truncated']=True
    return result
