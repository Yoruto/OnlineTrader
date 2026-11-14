"""Explicit one-round TqKq grant; no credentials or standing trading authority."""
from dataclasses import dataclass
from datetime import datetime,timezone
from pathlib import Path
import re
from online_trader.contracts.codec import canonical_bytes,content_hash,strict_loads
from online_trader.errors import TraderError


@dataclass(frozen=True,slots=True)
class SimulationGrant:
    _bytes:bytes

    @classmethod
    def parse(cls,raw):
        value=strict_loads(raw)
        required={"schema_version","grant_id","account_fingerprint","ledger_path","contracts","trading_day","not_before_utc","not_after_utc","quantity","opening_attempts","hold_seconds","authorization_reference"}
        if type(value) is not dict or value.keys()!=required or value["schema_version"]!="KQ_MANUAL_SIM/1":
            raise TraderError("E_ONLINE_SCOPE","Invalid explicit simulation grant")
        if (type(value['quantity']) is not int or value['quantity']!=1 or type(value['opening_attempts']) is not int or value['opening_attempts']!=1
                or type(value['hold_seconds']) is not int or value['hold_seconds']!=60):
            raise TraderError("E_ONLINE_SCOPE","This boundary supports one one-lot opening and a 60-second exit only")
        if not isinstance(value['account_fingerprint'],str) or not re.fullmatch('[0-9a-f]{16}',value['account_fingerprint']):
            raise TraderError('E_ONLINE_SCOPE','Invalid simulated account fingerprint')
        for key in ('grant_id','authorization_reference'):
            if type(value[key]) is not str or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._:-]{0,127}',value[key]):
                raise TraderError('E_ONLINE_SCOPE','Invalid grant identity')
        contracts=value['contracts']
        if (type(contracts) is not list or not 1<=len(contracts)<=8 or any(type(code) is not str or not re.fullmatch(r'DCE\.[a-z]{1,3}[0-9]{4}',code) for code in contracts)
                or len(set(contracts))!=len(contracts)):
            raise TraderError('E_ONLINE_SCOPE','Only explicit DCE future contracts are supported by this simulation stage')
        try:
            start,end=(datetime.fromisoformat(value[key].replace('Z','+00:00')) for key in ('not_before_utc','not_after_utc'))
            if any(not value[key].endswith('Z') for key in ('not_before_utc','not_after_utc')) or not 0<(end-start).total_seconds()<=600:
                raise ValueError()
            if start.astimezone(timezone.utc).date().isoformat()!=value['trading_day']:raise ValueError()
            path=Path(value['ledger_path'])
            if not path.is_absolute() or path.name!='ledger.db':raise ValueError()
        except (ValueError,TypeError,AttributeError):raise TraderError('E_ONLINE_SCOPE','Grant needs explicit UTC window, day and absolute ledger path') from None
        return cls(canonical_bytes(value))

    @classmethod
    def load(cls,path):
        try:
            with Path(path).open('rb') as stream:raw=stream.read(32769)
        except OSError as error:raise TraderError('E_ONLINE_SCOPE','Cannot read simulation grant') from error
        if len(raw)>32768:raise TraderError('E_ONLINE_SCOPE','Simulation grant exceeds size limit')
        return cls.parse(raw)

    @property
    def value(self):return strict_loads(self._bytes)
    @property
    def content_hash(self):return content_hash(self.value)
    @property
    def account_key(self):return 'SIM:KQ:'+self.value['account_fingerprint']
    def in_window(self,now):
        value=self.value
        return datetime.fromisoformat(value['not_before_utc'].replace('Z','+00:00'))<=now<=datetime.fromisoformat(value['not_after_utc'].replace('Z','+00:00'))


@dataclass(frozen=True,slots=True)
class DsvSimulationGrant:
    """A separate, finite strategy scope. Parsing never activates or connects it."""
    _bytes:bytes

    @classmethod
    def parse(cls,raw):
        from online_trader.domain.values import decimal_value
        value=strict_loads(raw)
        schema=value.get('schema_version') if type(value) is dict else None
        if type(schema) is not str:
            raise TraderError('E_ONLINE_SCOPE','Expected an independent finite DSV simulation grant')
        required={'schema_version','grant_id','account_fingerprint','ledger_path','contracts','trading_day',
                  'not_before_utc','not_after_utc','max_order_quantity','opening_attempts','risk_budget',
                  'profile_hash','calendar_hash','authorization_reference'}
        target_schemas = {'KQ_DSV_DESKTOP/1', 'SIMNOW_DSV_DESKTOP/1'}
        multi_day_schemas = target_schemas | {'KQ_DSV_SIM/2', 'SIMNOW_DSV_SIM/1', 'SIMNOW_DSV_AUTO/1',
                                             'KQ_WORKBENCH_SIM/1', 'SIMNOW_WORKBENCH_SIM/1'}
        if schema in target_schemas:
            required.remove('opening_attempts')
            required.add('daily_targets_per_contract')
        if schema in multi_day_schemas:
            required=required|{'trading_days','resume_entries_on_day_change'}
            if 'allow_external_allocation' in value:
                if type(value['allow_external_allocation']) is not bool:
                    raise TraderError('E_ONLINE_SCOPE','External position allocation needs an explicit boolean policy')
                required.add('allow_external_allocation')
        if schema=='SIMNOW_DSV_AUTO/1':required=required|{'auto_offsets'}
        if value.keys()!=required or schema not in multi_day_schemas|{'KQ_DSV_SIM/1'}:
            raise TraderError('E_ONLINE_SCOPE','Expected an independent finite DSV simulation grant')
        if value['schema_version'] in multi_day_schemas:
            from online_trader.scheduling.calendar import _day
            days=value['trading_days']
            if (type(days) is not list or not 1<=len(days)<=366 or any(type(day) is not str for day in days)
                    or days!=sorted(set(days)) or days[0]!=value['trading_day']
                    or type(value['resume_entries_on_day_change']) is not bool):
                raise TraderError('E_ONLINE_SCOPE','Multi-day scopes need explicit ordered cabinet days and rollover policy')
            for day in days:_day(day)
            if value['schema_version'] in {'KQ_WORKBENCH_SIM/1','SIMNOW_WORKBENCH_SIM/1'} and value['resume_entries_on_day_change']:
                raise TraderError('E_ONLINE_SCOPE','Manual workbench authority must be granted again after a cabinet-day change')
        if value['schema_version']=='SIMNOW_DSV_AUTO/1':
            policy=value['auto_offsets']
            if (type(policy) is not dict or policy.keys()!={'schema_version','as_of_date','last_completed_day'}
                    or policy['schema_version']!='DSV_START_OFFSETS/1' or len(value['trading_days'])!=1
                    or policy['as_of_date']!=value['trading_day'] or value['resume_entries_on_day_change']):
                raise TraderError('E_ONLINE_SCOPE','Startup calculation needs one exact cabinet day and an immutable calculation policy')
            _day(policy['last_completed_day'])
            if policy['last_completed_day']>=policy['as_of_date']:
                raise TraderError('E_ONLINE_SCOPE','The completed daily cutoff must precede the strategy cabinet day')
        # No default amount, retry allowance, profile, calendar or standing scope.
        quota_field = 'daily_targets_per_contract' if schema in target_schemas else 'opening_attempts'
        for field in ('max_order_quantity', quota_field):
            if type(value[field]) is not int or not 1<=value[field]<=2147483647:
                raise TraderError('E_ONLINE_SCOPE','Explicit positive strategy limits are required')
        decimal_value(value['risk_budget'],positive=True)
        for field in ('profile_hash','calendar_hash'):
            if type(value[field]) is not str or not re.fullmatch('[0-9a-f]{64}',value[field]):
                raise TraderError('E_ONLINE_SCOPE','Strategy settings and calendar must be hash-bound')
        if type(value['account_fingerprint']) is not str or not re.fullmatch('[0-9a-f]{16}',value['account_fingerprint']):
            raise TraderError('E_ONLINE_SCOPE','Invalid strategy account fingerprint')
        for field in ('grant_id','authorization_reference'):
            if type(value[field]) is not str or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._:-]{0,127}',value[field]):
                raise TraderError('E_ONLINE_SCOPE','Invalid strategy scope identity')
        contracts=value['contracts']
        if (type(contracts) is not list or not 1<=len(contracts)<=11
                or any(type(code) is not str or not re.fullmatch(r'(?:(?:DCE|SHFE|INE)\.[a-z]{1,3}[0-9]{4}|CZCE\.[A-Z]{1,3}[0-9]{3,4})',code) for code in contracts)
                or len(set(contracts))!=len(contracts)):
            raise TraderError('E_ONLINE_SCOPE','Explicit supported future contracts are required')
        try:
            start,end=(datetime.fromisoformat(value[key].replace('Z','+00:00')) for key in ('not_before_utc','not_after_utc'))
            limit=86400 if value['schema_version'] in {'KQ_DSV_SIM/1','SIMNOW_DSV_AUTO/1'} else 366*86400
            if not value['not_before_utc'].endswith('Z') or not value['not_after_utc'].endswith('Z') or not 0<(end-start).total_seconds()<=limit:
                raise ValueError()
            from datetime import date
            if date.fromisoformat(value['trading_day']).isoformat()!=value['trading_day']:raise ValueError()
            path=Path(value['ledger_path'])
            if not path.is_absolute() or path.name!='ledger.db':raise ValueError()
        except (ValueError,TypeError,AttributeError):
            raise TraderError('E_ONLINE_SCOPE','A finite UTC strategy window and exact cabinet day are required') from None
        return cls(canonical_bytes(value))

    @classmethod
    def load(cls,path):
        try:
            with Path(path).open('rb') as stream:raw=stream.read(32769)
        except OSError as error:raise TraderError('E_ONLINE_SCOPE','Cannot read strategy scope') from error
        if len(raw)>32768:raise TraderError('E_ONLINE_SCOPE','Strategy scope exceeds its size bound')
        return cls.parse(raw)

    @property
    def value(self):return strict_loads(self._bytes)
    @property
    def content_hash(self):return content_hash(self.value)
    @property
    def account_key(self):return ('SIM:SIMNOW:' if self.provider=='TQ_SIMNOW' else 'SIM:KQ:')+self.value['account_fingerprint']
    @property
    def provider(self):return 'TQ_SIMNOW' if self.value['schema_version'].startswith('SIMNOW_') else 'TQKQ'
    @property
    def daily_targets_per_contract(self):return self.value.get('daily_targets_per_contract')
    @property
    def opening_limit(self):
        if self.daily_targets_per_contract is not None:
            return {'daily_targets_per_contract': self.daily_targets_per_contract}
        return {'opening_attempts': self.value['opening_attempts']}
    @property
    def manual_workbench(self):return self.value['schema_version'] in {'KQ_WORKBENCH_SIM/1','SIMNOW_WORKBENCH_SIM/1'}
    @property
    def external_allocation_allowed(self):return self.value.get('allow_external_allocation',self.manual_workbench)
    @property
    def automatic_offsets(self):return self.value['schema_version']=='SIMNOW_DSV_AUTO/1'
    @property
    def trading_days(self):return self.value.get('trading_days',[self.value['trading_day']])
    @property
    def multi_day(self):return 'trading_days' in self.value
    def permits_day(self,day):return day in self.trading_days
    def in_window(self,now):
        value=self.value
        return datetime.fromisoformat(value['not_before_utc'])<=now<=datetime.fromisoformat(value['not_after_utc'])
