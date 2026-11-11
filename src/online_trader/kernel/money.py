"""Owner-accepted financial facts. No adapter, trading grant or second writer."""

import copy
from concurrent.futures import Future
from datetime import datetime, timezone

from online_trader.accounting.book import MoneyBook, ACCOUNT_FIELDS, validate_account, validate_fee, utc_now
from online_trader.contracts.codec import content_hash, strict_loads
from online_trader.domain.values import decimal_value
from online_trader.errors import TraderError
from online_trader.gateway.ports import UnavailablePort


class MoneyOwner:
    def __init__(self,kernel):
        self.kernel=kernel;self.book=MoneyBook(kernel.account_key);self.error=None;self._account_boots={}
        self._expired_accounts=set();self._active_account=None;self._expiry_queued=False
        for row in kernel.ledger.query('SELECT event_seq,payload_json,observed_at_utc,kernel_boot_id FROM event_journal ORDER BY event_seq'):
            data=strict_loads(row['payload_json']);self.accept(data.get('data',data),row['event_seq'],row['observed_at_utc'],kernel_boot_id=row['kernel_boot_id'])

    def accept(self,data,sequence,observed_at_utc=None,*,kernel_boot_id=None):
        if self.error:return
        try:
            self.book.accept(data,sequence,observed_at_utc or utc_now())
            account=data.get('money_account')
            if account is not None and account['evidence_id'] in self.book.account_evidence:
                self._account_boots.setdefault(account['evidence_id'],kernel_boot_id or self.kernel.boot_id)
                age=(datetime.now(timezone.utc)-datetime.fromisoformat(account['observed_at_utc'])).total_seconds()*1000
                if (self._account_boots[account['evidence_id']]==self.kernel.boot_id and account['complete'] and
                        0<=age<=self.kernel.max_quote_age_ms and account['evidence_id'] not in self._expired_accounts):
                    self._active_account=account['evidence_id']
            expired=data.get('money_account_expired')
            if expired:
                self._expired_accounts.add(expired);self._expiry_queued=False
                if self._active_account==expired:self._active_account=None
        except (TraderError,KeyError,TypeError,ValueError) as error:
            # A disposable report must never stop protection or lose executions.
            self.error=getattr(error,'code','E_MONEY_REPLAY')

    def report(self,*,as_of_utc=None):
        self.kernel._check_owner()
        if self.error:raise TraderError('E_MONEY_REPLAY','Financial view needs review; execution facts remain durable')
        scheduler=getattr(self.kernel,'_scheduled_runtime',None)
        result=self.book.report(checkpoint_event_seq=self.kernel._durable_seq,as_of_utc=as_of_utc,
                                valuation_boot_id='clock-fault' if scheduler and scheduler._fault else self.kernel.boot_id)
        result['boundary']='OWNER_ACCEPTED';return result

    def account_projection(self):
        if self.error or not self.book.account_facts:return None
        fact=self.book.account_facts[-1]
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(fact['observed_at_utc'])).total_seconds()*1000
        known=(fact['complete'] and self._account_boots.get(fact['evidence_id'])==self.kernel.boot_id and
               fact['evidence_id'] not in self._expired_accounts and
               0<=age<=self.kernel.max_quote_age_ms)
        return {'equity':fact['balance'],'available':fact['available'],'margin':fact['margin'],'fees':fact['commission'],
            'funds_quality':'KNOWN' if known else 'UNKNOWN','source_epoch':str(fact['source_epoch']),
            'fact_barrier_id':fact['evidence_id']}

    def business_state(self):
        return {'fills':copy.deepcopy(list(self.book.fills.values())),
            'fees':copy.deepcopy(list(self.book.fees.values())),
            'fee_imports':copy.deepcopy(list(self.book.fee_imports.values())),
            'account_facts':copy.deepcopy(self.book.account_facts),
            'account_source_boots':copy.deepcopy(self._account_boots),
            'expired_account_sources':sorted(self._expired_accounts),'error':self.error}

    def poll(self):
        k=self.kernel
        if k._failed or self.error or k._pending or not self._active_account or self._expiry_queued:return
        fact=self.book.account_facts[-1]
        if fact['evidence_id']!=self._active_account:return
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(fact['observed_at_utc'])).total_seconds()*1000
        scheduler=getattr(k,'_scheduled_runtime',None)
        if not 0<=age<=k.max_quote_age_ms or (scheduler and scheduler._fault):
            self._expiry_queued=True
            k._submit('TX_OBSERVATION','MONEY_ACCOUNT_SOURCE_EXPIRED',
                {'money_account_expired':fact['evidence_id']},lambda conn,seq:None)

    def _development(self):
        k=self.kernel;k._writable()
        if not k.development or isinstance(k.port,UnavailablePort):
            raise TraderError('E_UNSUPPORTED','Financial source facts require an isolated development port')
        if k._pending:raise TraderError('E_FACT_BARRIER','Accept preceding facts before adding financial evidence')
        if self.error:raise TraderError('E_MONEY_REPLAY','Financial view needs review')

    def record_fee(self,trade_key,amount,*,evidence_id,quality='CONFIRMED',revision=1):
        self._development()
        fact=validate_fee({'trade_key':trade_key,'amount':amount,'evidence_id':evidence_id,'quality':quality,'revision':revision})
        if not self.book.check_fee(fact):
            ready=Future();ready.set_result({'status':'DUPLICATE_FEE'});return ready
        return self.kernel._submit('TX_COUNTER','MONEY_FEE_CONFIRMED',{'money_fee':fact},lambda conn,seq:None)

    def record_account(self,fact):
        self._development();fact=validate_account(fact,self.kernel.account_key)
        if not self.book.check_account(fact):
            ready=Future();ready.set_result({'status':'DUPLICATE_ACCOUNT_MONEY'});return ready
        return self.kernel._submit('TX_OBSERVATION','MONEY_ACCOUNT_OBSERVED',{'money_account':fact},lambda conn,seq:None)

    def account_from_risk(self,event,account):
        """Capture only declared money fields, never the complete account record."""
        fact={key:account.get(key) for key in ACCOUNT_FIELDS}
        fact['balance']=account.get('equity')
        issues=[]
        for field in ACCOUNT_FIELDS:
            if fact[field] is None:continue
            try:
                number=decimal_value(fact[field])
                if field in {'margin','frozen_margin','commission'} and number<0:
                    raise TraderError('E_MONEY_INPUT','Margin and fees cannot be negative')
            except TraderError as error:
                fact[field]=None
                issues.append({'code':'ACCOUNT_MONEY_FIELD_INVALID','field':field,'error_code':error.code})
        fact.update(account_key=self.kernel.account_key,trading_day=event['trading_day'],
            observed_at_utc=event['source_time_utc'],source_epoch=event['source_epoch'],revision=account['revision'],
            currency='CNY',complete=account['complete'] and not issues,evidence_id='risk-money-'+content_hash({
                'account_key':self.kernel.account_key,'source_epoch':event['source_epoch'],'revision':account['revision']}))
        if issues:fact['source_issues']=issues
        fact=validate_account(fact,self.kernel.account_key)
        # Repeated market updates can share a complete account source revision.
        previous=self.book.account_facts[-1] if self.book.account_facts else None
        if previous and (fact['source_epoch'],fact['revision'])<=(previous['source_epoch'],previous['revision']):
            return None
        return fact
