"""Replay confirmed journal facts into a read-only financial view.

The journal remains the only writer. This book is a disposable projection, not
an account balance, a matching engine or a source of execution authority.
"""

import copy
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext

from online_trader.contracts.codec import canonical_bytes, content_hash
from online_trader.domain.values import decimal_value, decimal_text, identifier
from online_trader.errors import TraderError
from online_trader.scheduling.clock import ClockSample


ACCOUNT_FIELDS=('balance','pre_balance','available','margin','frozen_margin','close_profit',
                'position_profit','float_profit','commission','net_cash_flow')


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')


def trading_day(value):
    try:
        if type(value) is not str or date.fromisoformat(value).isoformat()!=value:raise ValueError()
    except ValueError as error:raise TraderError('E_MONEY_INPUT','Expected an explicit trading-day label') from error
    return value


def validate_account(fact,account_key):
    expected={'account_key','trading_day','observed_at_utc','source_epoch','revision','evidence_id',
              'currency','complete',*ACCOUNT_FIELDS}
    if type(fact) is not dict or not expected<=set(fact) or not set(fact)<=expected|{'source_issues'}:
        raise TraderError('E_MONEY_INPUT','Expected the complete explicit account money structure')
    canonical_bytes(fact)
    if fact['account_key']!=account_key:raise TraderError('E_SCOPE','Financial account scope differs')
    if fact['currency']!='CNY':raise TraderError('E_MONEY_INPUT','Only explicitly denominated CNY facts are supported')
    trading_day(fact['trading_day']);ClockSample(fact['observed_at_utc'],'0');identifier(fact['evidence_id'])
    for field in ('source_epoch','revision'):
        value=fact[field]
        if type(value) is not int or not 1<=value<=2147483647:raise TraderError('E_MONEY_INPUT','Invalid financial source revision')
    if type(fact['complete']) is not bool:raise TraderError('E_MONEY_INPUT','Account money completeness must be explicit')
    issues=fact.get('source_issues',[])
    if type(issues) is not list or len(issues)>len(ACCOUNT_FIELDS):
        raise TraderError('E_MONEY_INPUT','Invalid financial source field issues')
    for issue in issues:
        if (type(issue) is not dict or set(issue)!={'code','field','error_code'} or
                issue['code']!='ACCOUNT_MONEY_FIELD_INVALID' or issue['field'] not in ACCOUNT_FIELDS):
            raise TraderError('E_MONEY_INPUT','Invalid financial source field issue')
        identifier(issue['error_code'])
    for field in ACCOUNT_FIELDS:
        value=fact[field]
        if value is not None:
            number=decimal_value(value)
            if field in {'margin','frozen_margin','commission'} and number<0:
                raise TraderError('E_MONEY_INPUT','Margin and fees cannot be negative')
    return copy.deepcopy(fact)


def validate_fee(fact):
    if type(fact) is not dict or set(fact)!={'trade_key','amount','quality','evidence_id','revision'}:
        raise TraderError('E_MONEY_INPUT','Expected an explicit per-fill fee fact')
    canonical_bytes(fact);identifier(fact['trade_key']);identifier(fact['evidence_id'])
    if decimal_value(fact['amount'])<0 or fact['quality'] not in {'CONFIRMED','ESTIMATED'}:
        raise TraderError('E_MONEY_INPUT','Invalid fee amount or quality')
    if type(fact['revision']) is not int or not 1<=fact['revision']<=2147483647:
        raise TraderError('E_MONEY_INPUT','Invalid fee revision')
    return copy.deepcopy(fact)


class MoneyBook:
    def __init__(self,account_key):
        self.account_key=identifier(account_key)
        self.cycles={};self.fills={};self.fees={};self.fee_evidence={};self.account_facts=[]
        self.account_evidence={};self.nominal={};self.issues=[]
        self.invalid_marks=set()
        self.daily_equity={}
        self.fee_imports={}

    def check_fee(self,fact):
        fact=validate_fee(fact);key=fact['trade_key'];evidence=fact['evidence_id']
        if key not in self.fills:raise TraderError('E_OWNERSHIP','Fee needs a committed owned fill')
        previous=self.fee_evidence.get(evidence)
        if previous is not None:
            if previous!=fact:raise TraderError('E_DUPLICATE_CONFLICT','Fee evidence identity changed content')
            return False
        old=self.fees.get(key)
        if old:
            if fact['revision']<=old['revision']:raise TraderError('E_REVISION_CONFLICT','Fee revisions must advance')
            if old['quality']=='CONFIRMED' and fact['quality']!='CONFIRMED':
                raise TraderError('E_REVISION_CONFLICT','Confirmed fees cannot revert to estimates')
        return True

    def check_account(self,fact):
        fact=validate_account(fact,self.account_key)
        previous=self.account_evidence.get(fact['evidence_id'])
        if previous is not None:
            if previous!=fact:raise TraderError('E_DUPLICATE_CONFLICT','Account evidence identity changed content')
            return False
        if self.account_facts:
            old=self.account_facts[-1]
            if (fact['source_epoch'],fact['revision'])<=(old['source_epoch'],old['revision']):
                raise TraderError('E_REVISION_CONFLICT','Account money source cannot move backwards or reuse a revision')
            if fact['trading_day']<old['trading_day'] or datetime.fromisoformat(fact['observed_at_utc'])<datetime.fromisoformat(old['observed_at_utc']):
                raise TraderError('E_REVISION_CONFLICT','Account money facts cannot move backwards')
        return True

    @staticmethod
    def instrument(cycle):
        if cycle.get('financial_instrument') is not None:return copy.deepcopy(cycle['financial_instrument'])
        original=cycle.get('instrument') or {}
        multiplier=original.get('multiplier');basis='ORIGINAL_INSTRUMENT'
        if multiplier is None:
            # Legacy owned development cycles froze risk/lot = original S*M.
            # Recover only from those immutable facts, never a new boot's M.
            try:
                with localcontext() as context:
                    context.prec=80
                    multiplier=decimal_text(Decimal(cycle['risk_per_lot'])/Decimal(cycle['plan']['fixed_stop_loss']['distance_points']))
                decimal_value(multiplier,positive=True);basis='LEGACY_FROZEN_RISK_DIVIDED_BY_STOP'
            except (KeyError,ArithmeticError,TraderError):multiplier=None;basis='UNKNOWN'
        return {'currency':'CNY','multiplier':multiplier,'basis':basis,'max_quote_age_ms':30000}

    def accept(self,data,event_seq,observed_at_utc):
        event_seq=str(event_seq);ClockSample(observed_at_utc,'0')
        if not isinstance(data,dict):raise TraderError('E_MONEY_INPUT','Invalid journal financial data')
        if any(type(reason) is str and reason.startswith(('CLOCK_','E_CLOCK')) for reason in data.get('reasons',[])):
            for cid,cycle in self.cycles.items():
                quote=cycle.get('last_quote')
                if quote:self.invalid_marks.add((cid,quote.get('received_ns')))
        for cycle in ([data['cycle']] if data.get('cycle') else [])+data.get('cycles',[]):
            if cycle['plan']['account_key']!=self.account_key:raise TraderError('E_SCOPE','Cycle account differs')
            self.cycles[cycle['position_cycle_id']]=copy.deepcopy(cycle)
        raw=data.get('fill')
        if raw is not None:
            cycle=data.get('cycle')
            if cycle is None or not any(o['child_order_id']==raw['child_order_id'] for o in cycle['orders']):
                raise TraderError('E_OWNERSHIP','Journal fill has no owned cycle')
            plan=cycle['plan'];instrument=self.instrument(cycle)
            fact={key:raw[key] for key in ('trade_key','quantity','price','effect','fill_id','child_order_id')}
            fact.update(position_cycle_id=cycle['position_cycle_id'],contract_id=plan['contract_id'],
                direction=plan['direction'],multiplier=instrument['multiplier'],currency=instrument['currency'],
                theoretical_entry_price=plan['theoretical_entry_price'],
                trading_day=raw.get('trading_day',plan['trading_day']),
                observed_at_utc=raw.get('observed_at_utc',observed_at_utc),event_seq=event_seq,
                time_basis='EXPLICIT_OBSERVATION' if raw.get('observed_at_utc') else 'JOURNAL_OBSERVATION')
            fact['original_fee']=copy.deepcopy(raw.get('money_fee'))
            old=self.fills.get(fact['trade_key'])
            if old is not None and old!=fact:raise TraderError('E_DUPLICATE_CONFLICT','Journal fill changed its financial identity')
            self.fills[fact['trade_key']]=fact
            for issue in raw.get('money_issues',[]):
                canonical_bytes(issue)
                if issue not in self.issues:self.issues.append(copy.deepcopy(issue))
        fee=data.get('money_fee') or (raw or {}).get('money_fee')
        fees=([fee] if fee is not None else [])+data.get('money_fees',[])
        for fee in fees:
            if self.check_fee(fee):
                self.fees[fee['trade_key']]=copy.deepcopy(fee);self.fee_evidence[fee['evidence_id']]=copy.deepcopy(fee)
        receipt=data.get('fee_import')
        if receipt is not None:
            if receipt.get('account_key')!=self.account_key:
                raise TraderError('E_SCOPE','Statement fee receipt account differs')
            key=identifier(receipt['import_id']);previous=self.fee_imports.get(key)
            saved=copy.deepcopy(receipt)|{'event_seq':event_seq}
            if previous is not None and previous!=saved:
                raise TraderError('E_DUPLICATE_CONFLICT','Statement fee import identity changed content')
            self.fee_imports[key]=saved
        account=data.get('money_account')
        if account is not None and self.check_account(account):
            self.account_evidence[account['evidence_id']]=copy.deepcopy(account)
            self.account_facts.append(copy.deepcopy(account)|{'event_seq':event_seq})
            if account['complete'] and account.get('balance') is not None and Decimal(account['balance'])>0:
                day=account['trading_day']
                previous=self.daily_equity.get(day)
                if previous is None or datetime.fromisoformat(account['observed_at_utc'])>=datetime.fromisoformat(previous['observed_at_utc']):
                    self.daily_equity[day]={key:account.get(key) for key in
                        ('trading_day','observed_at_utc','balance','pre_balance')}
            for source_issue in account.get('source_issues',[]):
                issue=copy.deepcopy(source_issue)|{'evidence_id':account['evidence_id']}
                if issue not in self.issues:self.issues.append(issue)
        nominal=data.get('nominal_statistics')
        if nominal is not None:
            state=nominal['state'];self.nominal[(state['contract_id'],state['trading_day'])]=copy.deepcopy(state)

    def report(self,*,checkpoint_event_seq,as_of_utc=None,valuation_boot_id=None):
        from online_trader.accounting.report import build_report
        at=as_of_utc or utc_now();ClockSample(at,'0');now=datetime.fromisoformat(at)
        marks={}
        for cid,cycle in self.cycles.items():
            mark=cycle.get('money_quote');quote=cycle.get('last_quote');instrument=self.instrument(cycle)
            if mark is None or quote is None:continue
            elapsed=(now-datetime.fromisoformat(mark['observed_at_utc'])).total_seconds()*1000
            valid=(cycle['protection'].get('quality')=='CONTIGUOUS' and
                   (cid,quote.get('received_ns')) not in self.invalid_marks and
                   (valuation_boot_id is None or quote.get('kernel_boot_id')==valuation_boot_id) and
                   mark.get('received_ns')==quote.get('received_ns') and 0<=elapsed<=instrument.get('max_quote_age_ms',30000))
            marks[cid]={'price':mark['price'],'valid':valid,'observed_at_utc':mark['observed_at_utc'],
                        'trading_day':mark['trading_day']}
        nominal=[{key:row.get(key) for key in ('contract_id','trading_day','daily_pnl_points','net_loss_points',
                   'stop_count','profit_count','trade_count','total_volume','stop_streak','day_stopped')}
                 for _,row in sorted(self.nominal.items())]
        result=build_report(account_key=self.account_key,checkpoint_event_seq=str(checkpoint_event_seq),
            fills=list(self.fills.values()),fees=list(self.fees.values()),
            account_facts=[copy.deepcopy(fact)|{'source_epoch':str(fact['source_epoch'])} for fact in self.account_facts],
            marks=marks,nominal=nominal,as_of_utc=at,
            opening_inventory=[cycle['external_adoption']|{'position_cycle_id':cid}
                for cid,cycle in self.cycles.items() if cycle.get('external_adoption')],
            inventory_reductions=[row|{'position_cycle_id':cid} for cid,cycle in self.cycles.items()
                for row in cycle.get('external_reductions',[])])
        result['issues']+=copy.deepcopy(self.issues)
        result['fee_imports']=[copy.deepcopy(row) for _,row in sorted(self.fee_imports.items())]
        for cid,cycle in self.cycles.items():
            if cycle.get('statistics_review_required'):
                result['issues'].append({'code':'CYCLE_REVIEW_REQUIRED','position_cycle_id':cid})
        return result
