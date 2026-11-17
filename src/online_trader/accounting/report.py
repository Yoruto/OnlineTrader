"""Pure, exact reports from confirmed owned fills and explicit source facts.

The FIFO result is an original-fill-cost amount, not an exchange settlement or
an account balance. Missing fees, multipliers and valuations remain unknown.
"""

from collections import deque
import copy
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import re

from online_trader.contracts.codec import canonical_bytes, content_hash
from online_trader.domain.values import (MAX_QUANTITY, decimal_text, decimal_value,
                                         identifier, quantity, sequence)
from online_trader.errors import TraderError


MAX_REPORT_ITEMS = 100000
_UTC = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\Z')
_ACCOUNT_AMOUNTS = ('balance', 'pre_balance', 'available', 'margin', 'frozen_margin',
                    'close_profit', 'position_profit', 'float_profit', 'commission', 'net_cash_flow')
_FILL_KEYS = ('trade_key', 'position_cycle_id', 'contract_id', 'direction', 'effect',
              'quantity', 'price', 'multiplier', 'theoretical_entry_price', 'trading_day',
              'observed_at_utc', 'event_seq')


def _invalid(message):
    raise TraderError('E_MONEY_INPUT', message)


def _list(value, name):
    if type(value) not in (list, tuple) or len(value) > MAX_REPORT_ITEMS:
        _invalid(name + ' must be a bounded explicit fact list')
    return value


def _id(value):
    try:return identifier(value)
    except TraderError:_invalid('Invalid report evidence or scope identifier')


def _day(value):
    try:
        if type(value) is not str or date.fromisoformat(value).isoformat() != value:raise ValueError()
    except ValueError:_invalid('Expected an explicit YYYY-MM-DD trading day')
    return value


def _utc(value):
    if type(value) is not str or not _UTC.fullmatch(value):_invalid('Expected an explicit UTC timestamp')
    try:return datetime.fromisoformat(value[:-1] + '+00:00')
    except ValueError:_invalid('Invalid UTC timestamp')


def _number(value, *, positive=False, nullable=False):
    if value is None and nullable:return None
    try:return decimal_value(value, positive=positive)
    except TraderError:_invalid('Expected a bounded canonical decimal string')


def _seq(value):
    try:return sequence(value)
    except TraderError:_invalid('Expected a bounded event sequence string')


def _issue(issues, code, **scope):
    item={'code':code, **scope}
    if item not in issues:issues.append(item)


def _amount(value, issues, **scope):
    if value is None:return None
    with localcontext() as context:
        context.prec=80
        rounded=value.quantize(Decimal('0.000000000001'), rounding=ROUND_HALF_EVEN)
    text=decimal_text(rounded)
    try:decimal_value(text)
    except TraderError:
        _issue(issues, 'AMOUNT_DOMAIN_OVERFLOW', **scope)
        return None
    return text


def _count(value, issues, **scope):
    if value > MAX_QUANTITY:
        _issue(issues, 'QUANTITY_DOMAIN_OVERFLOW', **scope)
        return None
    return value


def add_close_point_checks(trades, cycles, issues):
    """Project the original DSV close diagnostics from owned, settled cycles."""
    reductions = {}
    for row in trades:
        if row['effect']=='REDUCE':
            reductions.setdefault(row['position_cycle_id'], []).append(row)
    for cid, fills in reductions.items():
        cycle = cycles.get(cid)
        if cycle is None:
            # A rollback archive supplies settled executions and nominal
            # totals. Exact execution-plan diagnostics remain on its source.
            continue
        exit_fact = cycle.get('nominal_exit') or {}
        reference = exit_fact.get('nominal_exit_price')
        if (cycle.get('lifecycle') != 'CLOSED' or not cycle.get('statistics_close_id')
                or cycle.get('statistics_review_required')
                or exit_fact.get('exit_reason') not in
                   {'STOP_LOSS','BREAK_EVEN','TAKE_PROFIT','TRAILING_STOP','PROTECTED_FLOOR','SESSION_CLOSE'}):
            continue
        row = max(fills, key=lambda item: (item['trading_day'], int(item['event_seq'])))
        sign = Decimal(1) if row['direction']=='LONG' else Decimal(-1)
        with localcontext() as context:
            context.prec = 80
            quantity = sum(fill['quantity'] for fill in fills)
            actual = sum(Decimal(fill['price']) * fill['quantity'] for fill in fills) / quantity
            state = cycle['protection']
            manual_reductions = (cycle.get('execution') or {}).get('manual_reduction_children')
            # Older cycles do not bind each reduction to its protection target.
            # A prior partial target cannot be compared to the final exit anchor.
            if (reference is not None and state.get('phase1_target') is None
                    and state.get('phase2_target') is None and not manual_reductions):
                row['close_slippage_points'] = _amount(sign * (Decimal(reference) - actual), issues,
                    trade_key=row['trade_key'], field='close_slippage_points')
            opening = cycle.get('initial_open_confirmation') or {}
            if opening.get('quantity'):
                entry = Decimal(opening['value']) / opening['quantity']
            else:
                entry = cycle.get('actual_average_entry_price')
            if entry is not None:
                row['real_close_points'] = _amount(sign * (actual - Decimal(entry)), issues,
                    trade_key=row['trade_key'], field='real_close_points')


def _fill(raw, account_key):
    if type(raw) is not dict or not set(_FILL_KEYS) <= raw.keys():_invalid('Incomplete confirmed fill')
    if raw.get('account_key', account_key) != account_key:raise TraderError('E_SCOPE', 'Fill belongs to another account')
    if raw.get('currency', 'CNY') != 'CNY':raise TraderError('E_CURRENCY', 'Report cannot mix currencies')
    item={name:copy.deepcopy(raw[name]) for name in _FILL_KEYS}
    for name in ('trade_key', 'position_cycle_id', 'contract_id'):_id(item[name])
    for name in ('fill_id', 'child_order_id', 'order_intent_id'):
        if raw.get(name) is not None:item[name]=_id(raw[name])
    if item['direction'] not in {'LONG','SHORT'} or item['effect'] not in {'OPEN','REDUCE'}:_invalid('Invalid owned fill direction or effect')
    try:quantity(item['quantity'], positive=True)
    except TraderError:_invalid('A confirmed fill needs positive explicit quantity')
    _number(item['price'], positive=True);_number(item['multiplier'], positive=True, nullable=True)
    _number(item['theoretical_entry_price'], positive=True)
    _day(item['trading_day']);_utc(item['observed_at_utc']);_seq(item['event_seq'])
    return item


def _fee_index(values, account_key):
    revisions={};index={}
    for raw in _list(values, 'Fees'):
        if type(raw) is not dict:_invalid('Expected an explicit fee fact')
        if raw.get('account_key', account_key) != account_key:raise TraderError('E_SCOPE', 'Fee belongs to another account')
        if raw.get('currency', 'CNY') != 'CNY':raise TraderError('E_CURRENCY', 'Report cannot mix currencies')
        key=_id(raw.get('trade_key'));evidence=_id(raw.get('evidence_id'))
        amount=_number(raw.get('amount'))
        if amount < 0:_invalid('Fee amounts must be nonnegative')
        quality=raw.get('quality');revision=raw.get('revision')
        if quality not in {'CONFIRMED','ESTIMATED'}:_invalid('Unknown fee quality')
        try:quantity(revision, positive=True)
        except TraderError:_invalid('Fee revisions must be positive bounded integers')
        row={'trade_key':key, 'amount':decimal_text(amount), 'quality':quality,
             'evidence_id':evidence, 'revision':revision}
        identity=(key, revision);old=revisions.get(identity)
        if old is not None and old != row:raise TraderError('E_DUPLICATE_CONFLICT', 'Fee revision changed content')
        revisions[identity]=row
        old=index.get(key)
        if (old is None or (quality=='CONFIRMED' and old['quality']!='CONFIRMED') or
                (quality==old['quality'] and revision>old['revision'])):index[key]=row
    return index


class _Bucket:
    def __init__(self):
        self.gross=Decimal(0);self.confirmed=Decimal(0);self.estimated=Decimal(0)
        self.unknown_fees=0;self.estimated_count=0;self.entries=0;self.exits=0;self.fills=0
        self.unrealized=Decimal(0)

    def add(self, fill, gross, fee):
        self.fills+=1
        if fill['effect']=='OPEN':self.entries+=fill['quantity']
        else:self.exits+=fill['quantity']
        if gross is None:self.gross=None
        elif self.gross is not None:self.gross+=gross
        if fee is None:self.unknown_fees+=1
        elif fee['quality']=='CONFIRMED':self.confirmed+=Decimal(fee['amount'])
        else:self.estimated+=Decimal(fee['amount']);self.estimated_count+=1

    def value(self, amount):
        if amount is None:self.unrealized=None
        elif self.unrealized is not None:self.unrealized+=amount

    def record(self, issues, **scope):
        fees=self.confirmed if self.unknown_fees==0 and self.estimated_count==0 else None
        covered=self.unknown_fees==0
        net=self.gross-fees if self.gross is not None and fees is not None else None
        estimated_net=self.gross-self.confirmed-self.estimated if covered and self.gross is not None else None
        values={'gross_realized_pnl':self.gross, 'confirmed_fees':self.confirmed,
                'estimated_fees':self.estimated, 'fees':fees, 'net_realized_pnl':net,
                'estimated_net_realized_pnl':estimated_net, 'unrealized_pnl':self.unrealized,
                'total_pnl':net+self.unrealized if net is not None and self.unrealized is not None else None}
        result={name:_amount(value, issues, field=name, **scope) for name,value in values.items()}
        result.update(entry_quantity=_count(self.entries,issues,**scope), exit_quantity=_count(self.exits,issues,**scope),
                      fill_count=_count(self.fills,issues,**scope), unknown_fee_count=self.unknown_fees,
                      estimated_fee_count=self.estimated_count)
        result['quality']='CONFIRMED_COST_BASIS' if result['gross_realized_pnl'] is not None and result['fees'] is not None else 'INCOMPLETE'
        return result


def _account_rows(values, account_key, checkpoint, issues):
    rows=[];identities={}
    for raw in _list(values, 'Account facts'):
        if type(raw) is not dict:_invalid('Expected an explicit account fact')
        if raw.get('account_key') != account_key:raise TraderError('E_SCOPE', 'Account snapshot belongs to another account')
        if raw.get('currency') != 'CNY':raise TraderError('E_CURRENCY', 'Report cannot mix currencies')
        seq=_seq(raw.get('event_seq'))
        if seq>checkpoint:continue
        item={name:raw.get(name) for name in _ACCOUNT_AMOUNTS}
        for value in item.values():_number(value, nullable=True)
        item.update(account_key=account_key, currency='CNY', trading_day=_day(raw.get('trading_day')),
            observed_at_utc=raw.get('observed_at_utc'), event_seq=raw['event_seq'],
            evidence_id=_id(raw.get('evidence_id')), source_epoch=raw.get('source_epoch'), revision=raw.get('revision'))
        complete=raw.get('complete',True)
        if type(complete) is not bool:_invalid('Account source completeness must be an explicit boolean')
        item.update(complete=complete,quality='KNOWN' if complete else 'INCOMPLETE')
        _utc(item['observed_at_utc']);_seq(item['source_epoch'])
        if type(item['revision']) is int:
            try:quantity(item['revision'],positive=True)
            except TraderError:_invalid('Invalid account source revision')
            item['revision']=str(item['revision'])
        else:_seq(item['revision'])
        identity=item['evidence_id'];old=identities.get(identity)
        if old is not None:
            if old != item:raise TraderError('E_DUPLICATE_CONFLICT', 'Account evidence changed content')
            continue
        identities[identity]=copy.deepcopy(item)
        balance=_number(item['balance'],nullable=True);pre=_number(item['pre_balance'],nullable=True)
        change=None;daily=None;basis='UNKNOWN'
        if complete and pre is not None and pre>0 and balance is not None:
            change=balance-pre;daily=change;basis='BALANCE_MINUS_PRE_BALANCE'
        elif complete and pre is not None and pre<=0 and all(item[name] is not None for name in ('close_profit','float_profit','commission')):
            daily=Decimal(item['close_profit'])+Decimal(item['float_profit'])-Decimal(item['commission'])
            basis='CLOSE_PLUS_FLOAT_MINUS_COMMISSION'
        flow=_number(item['net_cash_flow'],nullable=True)
        item.update(equity_change=_amount(change,issues,evidence_id=identity,field='equity_change'),
            daily_pnl=_amount(daily,issues,evidence_id=identity,field='daily_pnl'),daily_pnl_basis=basis,
            trading_pnl=_amount(change-flow if change is not None and flow is not None else None,
                               issues,evidence_id=identity,field='trading_pnl'),
            scope='ACCOUNT_SOURCE_FACTS', local_fill_reconciliation='NOT_ASSUMED')
        item['today_pnl']=item['daily_pnl']
        item['profit_ratio']=_amount(daily/pre if daily is not None and pre is not None and pre>0 else None,
                                    issues,evidence_id=identity,field='profit_ratio')
        item['today_pnl_ratio']=item['profit_ratio']
        rows.append(item)
    return sorted(rows,key=lambda item:(int(item['event_seq']),item['evidence_id']))


def build_report(*, account_key, checkpoint_event_seq, fills, fees=(), account_facts=(),
                 marks=None, nominal=(), as_of_utc=None, opening_inventory=(), inventory_reductions=()):
    """Build an immutable cost-basis report without creating or changing facts.

    ``marks.valid`` means the owner has verified market freshness. This pure
    function additionally rejects marks from after the explicit report instant.
    Fee amounts include entry and exit fees; net realized is gross realized minus
    all fees paid by the selected fills, including still-open entry fees.
    """
    account_key=_id(account_key);checkpoint=_seq(checkpoint_event_seq)
    if as_of_utc is None:as_of_utc=datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
    at=_utc(as_of_utc);issues=[];items=[];identities={}
    for raw in _list(fills,'Fills'):
        if type(raw) is not dict:_invalid('Expected an explicit confirmed fill')
        if _seq(raw.get('event_seq'))>checkpoint:continue
        item=_fill(raw,account_key);identity=item['trade_key'];old=identities.get(identity)
        if old is not None:
            if content_hash(old)!=content_hash(item):raise TraderError('E_DUPLICATE_CONFLICT','Trade identity changed content')
            continue
        identities[identity]=item;items.append(item)
    for row in _list(inventory_reductions,'Inventory reductions'):
        if _seq(row['event_seq'])<=checkpoint:
            items.append({'position_cycle_id':_id(row['position_cycle_id']),'quantity':row['quantity'],
                'event_seq':row['event_seq'],'trading_day':_day(row['trading_day']),'effect':'INVENTORY_REDUCTION'})
    items.sort(key=lambda row:int(row['event_seq']))
    fee_index=_fee_index(fees,account_key)
    if marks is None:marks={}
    if type(marks) is not dict:_invalid('Expected explicit cycle valuation facts')
    for cycle_id,mark in marks.items():
        _id(cycle_id)
        if type(mark) is not dict or type(mark.get('valid')) is not bool:_invalid('Invalid valuation quality')
        if mark.get('price') is not None:_number(mark['price'],positive=True)
        if mark.get('observed_at_utc') is not None:_utc(mark['observed_at_utc'])
        if mark.get('trading_day') is not None:_day(mark['trading_day'])
    overall=_Bucket();days={};contracts={};cycles={};trades=[]
    def day_bucket(day):return days.setdefault(day,_Bucket())
    for allocation in _list(opening_inventory,'Opening inventory'):
        if _seq(allocation['allocation_event_seq'])>checkpoint:continue
        cid=_id(allocation['position_cycle_id']);contract=_id(allocation['contract_id']);day=_day(allocation['trading_day'])
        qty=allocation['native_quantity'];direction=allocation['direction']
        if (allocation['account_key']!=account_key or cid in cycles or direction not in {'LONG','SHORT'}
                or type(qty) is not int or qty<=0):
            _invalid('Opening inventory has an invalid or repeated owner allocation')
        reference=allocation['reference_price'];price=_number(reference,positive=True)
        cycles[cid]={'position_cycle_id':cid,'contract_id':contract,'direction':direction,'trading_day':day,
            'last_fill_trading_day':day,'theoretical_entry_price':reference,'lots':deque([[qty,price,None]]),
            'bucket':_Bucket(),'invalid_quantity':False,'multiplier':None,'adopted_inventory':True}
        contracts.setdefault(contract,_Bucket())
        _issue(issues,'EXTERNAL_INVENTORY_COST_UNVERIFIED',position_cycle_id=cid)
    with localcontext() as context:
        context.prec=80
        for fill in items:
            cid=fill['position_cycle_id']
            if fill['effect']=='INVENTORY_REDUCTION':
                cycle=cycles.get(cid);qty=fill['quantity']
                if cycle is None or type(qty) is not int or not 0<qty<=sum(lot[0] for lot in cycle['lots']):
                    _invalid('Reviewed inventory reduction exceeds the original position')
                remaining=qty
                while remaining:
                    lot=cycle['lots'][0];used=min(remaining,lot[0]);remaining-=used;lot[0]-=used
                    if not lot[0]:cycle['lots'].popleft()
                for lot in cycle['lots']:lot[2]=None
                cycle['last_fill_trading_day']=fill['trading_day']
                for bucket in (overall,day_bucket(fill['trading_day']),contracts[cycle['contract_id']],cycle['bucket']):
                    bucket.gross=None
                _issue(issues,'EXTERNAL_REDUCTION_COST_UNVERIFIED',position_cycle_id=cid,trading_day=fill['trading_day'])
                continue
            contract=fill['contract_id'];direction=fill['direction']
            cycle=cycles.get(cid)
            if cycle is None:
                cycle={'position_cycle_id':cid,'contract_id':contract,'direction':direction,
                    'trading_day':fill['trading_day'],'last_fill_trading_day':fill['trading_day'],
                    'theoretical_entry_price':fill['theoretical_entry_price'],'lots':deque(),
                    'bucket':_Bucket(),'invalid_quantity':False,'multiplier':fill['multiplier']}
                cycles[cid]=cycle
            elif (cycle['contract_id'],cycle['direction'],cycle['theoretical_entry_price']) != (contract,direction,fill['theoretical_entry_price']):
                raise TraderError('E_SCOPE','A cycle changed its frozen instrument, direction or theoretical anchor')
            cycle['last_fill_trading_day']=fill['trading_day']
            if cycle['multiplier'] is not None and fill['multiplier'] is not None and cycle['multiplier']!=fill['multiplier']:
                _issue(issues,'CYCLE_MULTIPLIER_CONFLICT',position_cycle_id=cid,trade_key=fill['trade_key'])
                cycle['invalid_quantity']=True
            sign=Decimal(1) if direction=='LONG' else Decimal(-1)
            qty=fill['quantity'];price=Decimal(fill['price']);gross=Decimal(0)
            if fill['effect']=='OPEN':
                cycle['lots'].append([qty,price,None if fill['multiplier'] is None else Decimal(fill['multiplier'])])
            else:
                needed=qty
                while needed and cycle['lots']:
                    lot=cycle['lots'][0];matched=min(needed,lot[0]);needed-=matched;lot[0]-=matched
                    if lot[2] is None:
                        gross=None
                        if not cycle.get('adopted_inventory'):_issue(issues,'MULTIPLIER_UNKNOWN',trade_key=fill['trade_key'],position_cycle_id=cid)
                    elif gross is not None:gross+=sign*(price-lot[1])*matched*lot[2]
                    if not lot[0]:cycle['lots'].popleft()
                if needed:
                    gross=None;cycle['invalid_quantity']=True
                    _issue(issues,'UNMATCHED_CLOSE_QUANTITY',trade_key=fill['trade_key'],position_cycle_id=cid)
                if cycle['invalid_quantity']:gross=None
            fee=fee_index.get(fill['trade_key'])
            if fee is None:_issue(issues,'FEE_UNKNOWN',trade_key=fill['trade_key'])
            bucket=contracts.setdefault(contract,_Bucket())
            for aggregate in (overall,day_bucket(fill['trading_day']),bucket,cycle['bucket']):aggregate.add(fill,gross,fee)
            row=copy.deepcopy(fill)
            row.update(realized_pnl=_amount(gross,issues,trade_key=fill['trade_key'],field='realized_pnl'),
                fee=None if fee is None else fee['amount'],fee_quality='UNKNOWN' if fee is None else fee['quality'],
                fee_revision=None if fee is None else fee['revision'],
                fee_evidence_id=None if fee is None else fee['evidence_id'],
                directional_slippage_points=_amount(sign*(price-Decimal(fill['theoretical_entry_price'])),issues,
                    trade_key=fill['trade_key'],field='directional_slippage_points') if fill['effect']=='OPEN' else None)
            trades.append(row)
        cycle_rows=[]
        for cid,cycle in sorted(cycles.items()):
            qty=sum(lot[0] for lot in cycle['lots']);mark=marks.get(cid)
            valuation_day=cycle['last_fill_trading_day'];unrealized=Decimal(0)
            if qty or cycle['invalid_quantity']:
                if mark and mark.get('trading_day') is not None:valuation_day=mark['trading_day']
                else:_issue(issues,'VALUATION_TRADING_DAY_UNPROVEN',position_cycle_id=cid)
                usable=bool(mark and mark['valid'] and mark.get('price') is not None and mark.get('observed_at_utc') is not None)
                if usable and _utc(mark['observed_at_utc'])>at:
                    usable=False;_issue(issues,'FUTURE_VALUATION',position_cycle_id=cid)
                if cycle['invalid_quantity'] or not usable:
                    unrealized=None;_issue(issues,'VALUATION_UNKNOWN',position_cycle_id=cid)
                else:
                    sign=Decimal(1) if cycle['direction']=='LONG' else Decimal(-1)
                    for remaining,price,multiplier in cycle['lots']:
                        if multiplier is None:
                            unrealized=None
                            if not cycle.get('adopted_inventory'):_issue(issues,'MULTIPLIER_UNKNOWN',position_cycle_id=cid)
                            break
                        unrealized+=sign*(Decimal(mark['price'])-price)*remaining*multiplier
            for aggregate in (overall,day_bucket(valuation_day),contracts[cycle['contract_id']],cycle['bucket']):aggregate.value(unrealized)
            result=cycle['bucket'].record(issues,position_cycle_id=cid)
            result.update({name:cycle[name] for name in ('position_cycle_id','contract_id','direction','trading_day','last_fill_trading_day','theoretical_entry_price')})
            result.update(remaining_quantity=None if cycle['invalid_quantity'] else _count(qty,issues,position_cycle_id=cid),
                closed=bool(qty==0 and not cycle['invalid_quantity'] and (cycle['bucket'].entries or cycle.get('adopted_inventory'))),
                valuation_trading_day=valuation_day,
                valuation_observed_at_utc=None if not mark else mark.get('observed_at_utc'))
            cycle_rows.append(result)
        equity=_account_rows(account_facts,account_key,checkpoint,issues)
        summary=overall.record(issues,scope='SUMMARY')
        closed=[row for row in cycle_rows if row['closed']]
        known=[row for row in closed if row['gross_realized_pnl'] is not None]
        wins=sum(Decimal(row['gross_realized_pnl'])>0 for row in known)
        losses=sum(Decimal(row['gross_realized_pnl'])<0 for row in known)
        summary.update(closed_cycles=len(closed),win_count=wins,loss_count=losses,
            win_rate=_amount(Decimal(wins)/len(known),issues,field='win_rate') if known else None,
            win_rate_basis='GROSS_CLOSED_CYCLES',win_rate_known_cycles=len(known))
        by_day=[{'trading_day':day,**bucket.record(issues,trading_day=day)} for day,bucket in sorted(days.items())]
        by_contract=[{'contract_id':code,**bucket.record(issues,contract_id=code)} for code,bucket in sorted(contracts.items())]
    # Nominal state is already published by its own owner. Copy it independently;
    # it never enters any currency calculation or gets multiplied by lot size.
    nominal_rows=copy.deepcopy(list(_list(nominal,'Nominal statistics')))
    canonical_bytes(nominal_rows)
    result={'schema_version':'MONEY_REPORT/1','account_key':account_key,'currency':'CNY',
        'checkpoint_event_seq':checkpoint_event_seq,'as_of_utc':as_of_utc,
        'scope':'LOCAL_OWNED_CYCLES','accounting_basis':'CONFIRMED_FILL_FIFO',
        'rounding_policy':'DECIMAL_80_HALF_EVEN_MAX_12_FRACTIONAL_DIGITS',
        'summary':summary,'by_day':by_day,'by_contract':by_contract,'cycles':cycle_rows,
        'trades':trades,'equity':equity,'account':copy.deepcopy(equity[-1]) if equity else None,
        'nominal':nominal_rows,'issues':issues}
    canonical_bytes(result)
    return result
