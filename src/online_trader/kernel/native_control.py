"""Broker-account control using only the current owner's synchronized native facts."""
from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.errors import TraderError, exception_record


def _native_order_identity(order):
    return tuple(order[key] for key in ('contract_id','direction','offset','quantity','limit_price'))


def cancel_review(owner, *, snapshot=None, allow_external=True):
    snapshot=owner.port.snapshot() if snapshot is None else snapshot
    orders=sorted((o for o in snapshot['orders'] if o['status']=='ALIVE'),key=lambda o:o['order_id'])
    outside=sorted({o['contract_id'] for o in orders}-set(owner.grant.value['contracts']))
    evidence={'account_key':owner.kernel.account_key,'kernel_boot_id':owner.kernel.boot_id,
        'trading_day':snapshot['trading_day'],'source_epoch':snapshot['source_epoch'],'orders':orders}
    eligible=(allow_external and owner.grant.manual_workbench and snapshot['complete'] and not owner.port.fault and not outside
        and owner.grant.in_window(owner.port.clock.now()) and 0<len(orders)<=512)
    by_contract={}
    for order in orders:
        code=order['contract_id'];by_contract[code]=by_contract.get(code,0)+1
    owned={owner.port.order_id(child['child_order_id']) for cycle in owner.kernel._staged.values() for child in cycle['orders']}
    return snapshot,orders,{'evidence_hash':content_hash(evidence),'eligible':eligible,
        'active_count':len(orders),'external_count':sum(order['order_id'] not in owned for order in orders),
        'outside_scope':outside[:32],'by_contract':by_contract if eligible else {}}


def prepare_account_cancel(control,binding):
    k=control.kernel;owner=k.simulation;p=binding['body']['parameters']
    snapshot,orders,review=cancel_review(owner)
    if not review['eligible'] or review['evidence_hash']!=p['evidence_hash'] or k._pending:
        raise TraderError('E_FACT_BARRIER','A current reviewed set of all active native orders is required')
    owner._validate_snapshot(snapshot)
    ids={o['order_id'] for o in orders}
    for state in control.states.values():
        prior={o['order_id'] for o in state['metadata'].get('native_cancel_orders',[])}
        if prior & ids and state.get('receipt',{}).get('status') not in {'FAILED','REJECTED','SUCCEEDED','SUPERSEDED'}:
            raise TraderError('E_UNKNOWN_SEND','An earlier cancellation of these native orders still needs reconciliation')
    owned={owner.port.order_id(o['child_order_id']):o['child_order_id'] for c in k._staged.values() for o in c['orders']}
    if any(o.get('cancel_status') for c in k._staged.values() for o in c['orders'] if owner.port.order_id(o['child_order_id']) in ids):
        raise TraderError('E_UNKNOWN_SEND','An owned order still has a pending cancellation')
    binding.update(initial_status='EXECUTING',reason='Reviewed native cancellation committed; awaiting terminal facts')
    k._armed=False;k.contexts.revoke_all();k._authority_revision+=1

    def recheck():
        current,current_orders,current_review=cancel_review(owner)
        return current,current_orders if current_review['evidence_hash']==review['evidence_hash'] and current_review['eligible'] else []

    _commit_native_cancel(control,binding,snapshot,orders,owned,recheck,'NATIVE_ACCOUNT_CANCEL_PREPARED')


def prepare_strategy_stop(control,binding):
    k=control.kernel;owner=k.simulation;runtime=getattr(k,'_dsv_runtime',None)
    if not getattr(owner,'strategy_enabled',False) or runtime is None:
        raise TraderError('E_SCOPE','Current owner has no active DSV profile')

    if binding['body']['parameters'].get('recover_flat'):
        from online_trader.runtime.dsv_simulation import verify_closed_snapshot

        snapshot = owner.port.snapshot()
        if (getattr(owner.grant, 'manual_workbench', False)
                or k._armed or owner.port.fault or not snapshot['complete'] or snapshot['positions']
                or any(order['status'] == 'ALIVE' or (order['quantity'] > 0 and order['status'] != 'FINISHED')
                       for order in snapshot['orders'])
                or k._pending or control.stop_cancel_pending()
                or any(cycle.get('statistics_review_required') for cycle in k._staged.values())):
            raise TraderError('E_FACT_BARRIER', '恢复要求原执行器已停开、柜台空仓无单且账本已核对。')
        owner._validate_snapshot(snapshot)
        verify_closed_snapshot(k, snapshot)
        control.entry_stop_generation += 1
        k.contexts.revoke_all(reason='WORKBENCH_RECOVERY_STOP')
        k._authority_revision += 1
        binding['metadata']['strategy_stop_grant_id'] = owner.grant.value['grant_id']
        binding.update(initial_status='SUCCEEDED', reason='空仓恢复退出已受理，等待原执行器退出回执。')
        control._stage([], 'TX_AUTH', 'DSV_RECOVERY_STOP_REQUESTED')
        return

    k._armed=False;k.contexts.revoke_all();k._authority_revision+=1
    binding['metadata']['strategy_stop_grant_id'] = owner.grant.value['grant_id']
    cancel=runtime.profile.to_settings()['trade']['cancel_on_stop']
    held=sum(c['protection']['owned_qty'] for c in k._staged.values() if c.get('management_state')!='TAKEN_OVER')
    message='策略已停止新增'
    if held:
        message+=f'，仍有自有持仓 {held} 手，原持仓保护继续'
    if not cancel:
        control.preserve_open_orders_revision = k._authority_revision
        binding['metadata']['preserve_open_orders_revision'] = k._authority_revision
        binding.update(initial_status='SUCCEEDED',reason=message+'。已关闭停止时撤单。')
        control._stage([], 'TX_AUTH','DSV_STRATEGY_STOPPED')
        return

    try:
        snapshot,active,review=_strategy_stop_cancel_review(owner)
        owner._validate_snapshot(snapshot)
        if owner.port.fault:
            raise TraderError('E_FACT_BARRIER','Native channel is not synchronized')
    except (TraderError, AttributeError, KeyError, TypeError, ValueError):
        binding.update(initial_status='NEEDS_RECONCILIATION',error_code='E_FACT_BARRIER',
                       reason=message+'。柜台状态待核对，撤单尚未发送。')
        control._stage([], 'TX_AUTH','DSV_STRATEGY_STOPPED')
        return

    by_native_id={owner.port.order_id(o['child_order_id']):(c,o) for c in k._staged.values() for o in c['orders']}
    confirmed_external=review['eligible'] and review['evidence_hash']==binding['body']['parameters'].get('evidence_hash')
    contracts=set(owner.grant.value['contracts'])
    previously_reviewed={row['order_id'] for state in control.states.values()
        if state.get('receipt',{}).get('status') not in {'FAILED','REJECTED','SUCCEEDED','SUPERSEDED'}
        for row in state['metadata'].get('native_cancel_orders',[])}
    orders=[];owned={};unverified=0
    for order in active:
        oid=order['order_id']
        if oid in previously_reviewed or order['contract_id'] not in contracts:
            continue
        saved=by_native_id.get(oid)
        if saved is None:
            if confirmed_external:orders.append(order)
            continue
        cycle,child=saved
        if cycle.get('management_state') in {'TAKEN_OVER','TAKEOVER_PENDING'}:
            continue
        plan=cycle['plan']
        identity=(plan['contract_id'],
                  'BUY' if (plan['direction']=='LONG')==(child['effect']=='OPEN') else 'SELL',
                  child.get('native_offset') or ('OPEN' if child['effect']=='OPEN' else 'CLOSE'),
                  child['original_qty'],child['limit_price'])
        if child['terminal_confirmed'] or _native_order_identity(order)!=identity:
            unverified+=1
            continue
        orders.append(order)
        owned[oid]=child['child_order_id']
    skipped=len(active)-len(orders)
    if skipped:message+=f'，{skipped} 笔未获撤单授权、已在核对或归属待核对的挂单未发撤单'
    if not orders:
        binding.update(initial_status='NEEDS_RECONCILIATION' if unverified else 'SUCCEEDED',
                       error_code='E_FACT_BARRIER' if unverified else 'NONE',reason=message+'。')
        control._stage([], 'TX_AUTH','DSV_STRATEGY_STOPPED')
        return
    binding.update(initial_status='EXECUTING',reason=message+f'。请求撤销 {len(orders)} 笔挂单，等待柜台确认。')
    binding['metadata']['stop_message']=message

    def recheck():
        current,current_orders,current_review=_strategy_stop_cancel_review(owner)
        owner._validate_snapshot(current)
        if (owner.port.fault or current['trading_day']!=snapshot['trading_day']
                or current['source_epoch']!=snapshot['source_epoch']):
            return current,[]
        approved={row['order_id']:row for row in orders}
        external_matches=confirmed_external and current_review['eligible'] and current_review['evidence_hash']==review['evidence_hash']
        return current,[row for row in current_orders if row['order_id'] in approved
            and (row['order_id'] in owned or external_matches)
            and _native_order_identity(row)==_native_order_identity(approved[row['order_id']])]

    _commit_native_cancel(control,binding,snapshot,orders,owned,recheck,'DSV_STRATEGY_STOPPED')


def _strategy_stop_cancel_review(owner):
    scheduler = getattr(owner.kernel, '_scheduled_runtime', None)
    if scheduler is not None and scheduler._fault:
        sample = scheduler.clock_protection_sample()
        snapshot = scheduler.clock_protection_snapshot(sample)
        if snapshot is None:
            raise TraderError('E_FACT_BARRIER', 'The current native cancel session cannot be proved')
        return cancel_review(owner, snapshot=snapshot, allow_external=False)
    return cancel_review(owner)


def _commit_native_cancel(control,binding,snapshot,orders,owned,recheck,event_type):
    k=control.kernel;owner=k.simulation
    binding['metadata'].update(native_cancel_orders=orders,native_cancel_day=snapshot['trading_day'],native_cancel_boot=k.boot_id)

    def send():
        attempts={}
        exceptions=[]
        epoch=snapshot['source_epoch']
        try:
            current_snapshot,current=recheck()
            epoch=current_snapshot['source_epoch']
        except (TraderError, AttributeError, KeyError, TypeError, ValueError):
            current=[]
        for order in current:
            oid=order['order_id']
            try:
                if oid in owned:
                    k.request_cancel(owned[oid],retain_exit_target=True)
                    attempts[oid]='OWNED_CANCEL_PREPARED'
                else:
                    owner.port.cancel_external(order,snapshot['trading_day'])
                    attempts[oid]='CALL_RETURNED'
            except Exception as error:
                attempts[oid]='UNKNOWN'
                exceptions.append(exception_record(error) | {'native_order_id': oid})
        data={'command_id':binding['body']['command_id'],'attempts':attempts,
              'source_epoch':epoch,'exceptions':exceptions}
        k._submit('TX_CANCEL','NATIVE_ACCOUNT_CANCEL_RESULTS',data,lambda conn,seq:None,
            after=lambda:control.native_cancel_results.__setitem__(data['command_id'],attempts))

    control._stage([], 'TX_CANCEL',event_type,after=send)


def account_cancel_result(owner,metadata,command_id,*,strategy_stop=False):
    if owner is None or not (strategy_stop or owner.grant.manual_workbench):return None
    kernel=owner.kernel
    unresolved=('NEEDS_RECONCILIATION','E_UNKNOWN_SEND','Original native cancellation is awaiting terminal evidence',[])
    owned={owner.port.order_id(o['child_order_id']):o for c in kernel._committed.values() for o in c['orders']}
    if strategy_stop and all(order['order_id'] in owned and owned[order['order_id']]['terminal_confirmed']
            and owned[order['order_id']]['reported_fill_qty']==owned[order['order_id']]['filled_qty']
            for order in metadata['native_cancel_orders']):
        return ('SUCCEEDED','NONE','All original owned orders have quantity-consistent terminal evidence',[])
    try:
        if strategy_stop:
            snapshot, _, _ = _strategy_stop_cancel_review(owner)
        else:
            snapshot=owner.port.snapshot()
    except TraderError:return unresolved
    if not snapshot['complete']:return None
    if snapshot['trading_day']!=metadata['native_cancel_day']:
        return ('NEEDS_RECONCILIATION','E_FACT_BARRIER','Original native terminal evidence is still required',[])
    native={row['order_id']:row for row in snapshot['orders']}
    attempts=kernel.control.native_cancel_results.get(command_id)
    for order in metadata['native_cancel_orders']:
        oid=order['order_id'];current=native.get(oid);local=owned.get(oid)
        if (current is None or current['status']!='FINISHED'
                or _native_order_identity(current)!=_native_order_identity(order)):
            if local and local.get('cancel_status')=='UNKNOWN':return unresolved
            if attempts is not None and attempts.get(oid) not in {'OWNED_CANCEL_PREPARED','CALL_RETURNED'}:return unresolved
            if kernel.boot_id!=metadata.get('native_cancel_boot'):return unresolved
            return None
        trades=[t for t in snapshot['trades'] if t['order_id']==oid]
        if (any(any(t[key]!=order[key] for key in ('contract_id','direction','offset')) for t in trades)
                or sum(t['quantity'] for t in trades)!=current['quantity']-current['remaining']):
            return None
        if strategy_stop and local and (not local['terminal_confirmed'] or local['filled_qty']!=current['quantity']-current['remaining']):
            return None
    return ('SUCCEEDED','NONE','Every reviewed order has native terminal and detailed trade evidence',[])


def record_reconciliation(control,binding,run_id):
    k=control.kernel;owner=k.simulation;scope=binding['body']['scope']
    snapshot=owner.port.snapshot()
    owner._validate_snapshot(snapshot)
    scheduler = getattr(k, '_scheduled_runtime', None)
    clock_recovery = scope['scope_type'] == 'ACCOUNT' and scheduler is not None and scheduler._fault
    if clock_recovery:
        from online_trader.scheduling.clock import ClockSample
        clock_sample = scheduler.clock.sample()
        ClockSample.from_record(clock_sample)
        command = k._command_binding
        k._command_binding = None
        try:
            observations = owner.observe_native_orders()
            k.wait_idle()
            # A wall-clock jump can leave same-session price protection live.
            # Reset still requires the existing full original-cycle review.
            owned = [c for c in k._staged.values() if c['lifecycle'] != 'CLOSED' and not owner.archived_cycle(c)]
            pending = set(k._recovery_pending)
            owner.recover_working(observations, reconcile_cycles=False)
            k.wait_idle()
            for code in {cycle['plan']['contract_id'] for cycle in owned}:
                current = owner.port.calendar.query(code, clock_sample['now_utc'])
                if (not current['complete'] or (current['is_trading'] and
                        current['trading_day'] != snapshot['trading_day'])):
                    raise TraderError('E_FACT_BARRIER', '原持仓所在的柜台交易日待核对。')
            for cycle in owned:
                cid = cycle['position_cycle_id']
                orders = k._staged[cid]['orders']
                if ('CANCEL_TERMINAL_TIMEOUT' in cycle.get('execution', {}).get('last_reasons', [])
                        and any(o.get('cancel_status') in {'SEND_POSSIBLE', 'CALL_RETURNED', 'UNKNOWN'}
                                and not o['terminal_confirmed'] for o in orders)):
                    raise TraderError('E_FACT_BARRIER', '仍在等待原撤单的柜台终态。')
                if cid in pending:
                    owner.reconcile(cid, allow_working=True, clear_gateway_fault=False)
                else:
                    owner.verify_clock_protection(k._staged[cid], snapshot)
        finally:
            k._command_binding = command
        if k._has_unknown() or k.recovery_required or k._recovery_pending or owner.port.fault:
            raise TraderError('E_FACT_BARRIER', '原委托及持仓仍待核对，时钟保护状态未解除。')
        snapshot = owner.port.snapshot()
        owner._validate_snapshot(snapshot)
    recover_flat=False
    if (getattr(owner,'strategy_enabled',False) and not owner.grant.manual_workbench
            and k.recovery_required and not k._armed and not k._staged
            and not k._recovery_pending and not k._has_unknown()
            and not owner.port.fault and not getattr(getattr(k,'_scheduled_runtime',None),'_fault',None)
            and owner.days.ready(snapshot['trading_day'])
            and owner.grant.permits_day(snapshot['trading_day'])):
        contracts=set(owner.grant.value['contracts'])
        if not any(row['contract_id'] in contracts
                   for part in ('orders','trades','positions') for row in snapshot[part]) and not k.ledger.query(
                       "SELECT 1 FROM reservations WHERE status IN ('HELD','UNKNOWN') LIMIT 1"):
            last=k.ledger.query("""SELECT event_type,payload_json FROM event_journal WHERE kernel_boot_id=?
                AND event_type IN ('ONLINE_DSV_FACT_BARRIER','CONTROL_NATIVE_FLAT_RECOVERY')
                ORDER BY event_seq DESC LIMIT 1""",(k.boot_id,))
            recover_flat=bool(last and last[0]['event_type']=='ONLINE_DSV_FACT_BARRIER' and
                strict_loads(last[0]['payload_json'])['data']['conflict']['error_code']=='E_FACT_BARRIER')
    if clock_recovery:
        reason = '已核对柜台事实，时钟恢复后等待新行情及 F5 启动。'
    else:
        reason = ('Same-day flat facts verified; entry authority remains disabled'
                  if recover_flat else 'Four synchronized native ranges saved; existing ownership and recovery gates retained')
    binding.update(initial_status='SUCCEEDED',error_code='NONE',
        reason=reason,evidence_ids=[run_id])

    def save(conn,seq):
        conn.execute('INSERT INTO reconciliation_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (run_id,snapshot['gateway_identity'],snapshot['source_epoch'],snapshot['trading_day'],'COMPLETE',
            canonical_bytes(scope).decode(),0,seq,0,seq,seq))
        for part in ('FUNDS','POSITIONS','ORDERS','TRADES'):
            conn.execute('INSERT INTO reconciliation_parts VALUES(?,?,?,?,?,?,?)',
                (run_id,part,'COMPLETE','{}',canonical_bytes({'snapshot_id':snapshot['snapshot_id'],
                    'source_epoch':snapshot['source_epoch'],'kind':'NATIVE_SYNCHRONIZED_QUERY'}).decode(),
                 0,canonical_bytes(snapshot[part.lower()]).decode()))

    def accepted():
        if recover_flat:
            k.recovery_required = False
        if clock_recovery:
            scheduler.reset_clock(sample=clock_sample)

    control._stage([], 'TX_RECONCILE',
        'CONTROL_NATIVE_FLAT_RECOVERY' if recover_flat else 'CONTROL_NATIVE_ACCOUNT_QUERY_COMPLETED',
        extra=save,after=accepted)
