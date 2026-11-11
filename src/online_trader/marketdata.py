"""Immutable local reference history and source-labelled calendar review.

Imports never become execution ticks, live completeness or a changed active
calendar. The running owner's original calendar hash and plans stay frozen.
"""
from datetime import date,datetime,timedelta
from pathlib import Path
import hashlib
import re

from online_trader.configuration.files import publish_new_bytes
from online_trader.configuration.offsets import DailyDataset,load_daily
from online_trader.contracts.codec import canonical_bytes,content_hash,strict_loads
from online_trader.errors import TraderError
from online_trader.scheduling.calendar import TradingCalendar,_utc,_day


def _error(message):raise TraderError('E_REFERENCE_DATA',message)


def bounded_json(path):
    try:
        with Path(path).open('rb') as stream:raw=stream.read(8*1024*1024+1)
        if len(raw)>8*1024*1024:_error('Reference input exceeds its size limit')
        return strict_loads(raw)
    except OSError:_error('Cannot read the explicitly selected reference file')


def review_calendar(package,*,as_of_utc):
    """Cross-check explicit UTC sessions against explicit dated market flags.

    Daily flags do not prove a contract's night session. Every session still has
    to be supplied explicitly, including holiday-eve breaks and forced closes.
    """
    keys={'schema_version','source','observed_at_utc','valid_until_utc','date_flags','calendar'}
    if type(package) is not dict or set(package)!=keys or package['schema_version']!='CALENDAR_PACKAGE/1':
        _error('Expected an explicit calendar package and provenance')
    source=package['source']
    if (type(source) is not dict or set(source)!={'kind','reference','sha256'}
            or source['kind'] not in {'EXCHANGE_FILE','TQSDK_EXPORT','USER_REFERENCE'}
            or type(source['reference']) is not str or not 1<=len(source['reference'])<=1024
            or type(source['sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',source['sha256'])):
        _error('Calendar provenance is missing or invalid')
    observed,expiry,now=map(_utc,(package['observed_at_utc'],package['valid_until_utc'],as_of_utc))
    if not observed<expiry:_error('Calendar validity must be explicit and forward')
    flags=package['date_flags']
    if type(flags) is not list or not 1<=len(flags)<=3660:_error('Expected bounded explicit daily trading flags')
    by_day={};previous=None
    for row in flags:
        if type(row) is not dict or set(row)!={'date','trading'} or type(row['trading']) is not bool:_error('Invalid trading-date flag')
        day=date.fromisoformat(_day(row['date']))
        if previous is not None and day!=previous+timedelta(days=1):_error('Daily source has a gap, duplicate or disorder')
        by_day[row['date']]=row['trading'];previous=day
    calendar=TradingCalendar.from_record(package['calendar']);record=calendar.to_record();reasons=[]
    for contract in record['contracts']:
        for session in contract['sessions']:
            if by_day.get(session['trading_day']) is not True:_error('Session trading day is absent or marked closed in the source')
    if not record['complete']:reasons.append('CALENDAR_INCOMPLETE')
    if now<observed:reasons.append('SOURCE_FROM_FUTURE')
    if now>=expiry:reasons.append('SOURCE_EXPIRED')
    if not _utc(record['coverage_from_utc'])<=now<_utc(record['coverage_to_utc']):reasons.append('COVERAGE_EXPIRED_OR_NOT_STARTED')
    if source['kind']=='USER_REFERENCE':reasons.append('USER_REFERENCE_NOT_AUTHORITATIVE')
    return {'status':'REVIEWED_REFERENCE','calendar_hash':calendar.content_hash,'package_hash':content_hash(package),
        'source':source,'source_verified_online':False,'reasons':reasons,'current_reference':not reasons,
        'runtime_activation':False,'broker_connected':False}


class ReferenceStore:
    def __init__(self,directory):self.directory=Path(directory)

    def import_daily(self,path):
        try:
            with Path(path).open('rb') as stream:raw=stream.read(2*1024*1024+1)
        except OSError:_error('Cannot read the explicitly selected daily CSV')
        dataset=DailyDataset.from_csv(raw)
        record={'schema_version':'REFERENCE_HISTORY/1','kind':'DAILY','source_sha256':hashlib.sha256(raw).hexdigest(),
            'dataset_hash':dataset.content_hash,'rows':dataset.rows,'execution_eligible':False}
        return self._put('daily',record)|dataset.summary()

    def import_calendar(self,path,*,as_of_utc):
        package=bounded_json(path);review=review_calendar(package,as_of_utc=as_of_utc)
        calendar=TradingCalendar.from_record(package['calendar'])
        # A calendar/version identity cannot acquire different content later.
        identity=content_hash({'id':calendar.calendar_id,'version':calendar.calendar_version})
        pointer=self.directory/'calendar-identities'/(identity+'.json')
        binding={'calendar_hash':calendar.content_hash,'package_hash':content_hash(package)}
        if pointer.exists():
            if bounded_json(pointer)!=binding:raise TraderError('E_DUPLICATE_CONFLICT','Calendar identity already binds a different package')
        result=self._put('calendars',package)
        if not pointer.exists():publish_new_bytes(canonical_bytes(binding),pointer)
        return result|review

    def _put(self,kind,record):
        digest=content_hash(record);path=self.directory/kind/(digest+'.json')
        if path.exists():
            if bounded_json(path)!=record:raise TraderError('E_DUPLICATE_CONFLICT','Reference content was altered')
        else:publish_new_bytes(canonical_bytes(record),path)
        return {'path':str(path.resolve()),'content_hash':digest,'runtime_activation':False}

    def daily_csv(self,digest,path):
        if type(digest) is not str or not re.fullmatch('[0-9a-f]{64}',digest):_error('Invalid history identity')
        record=bounded_json(self.directory/'daily'/(digest+'.json'))
        if content_hash(record)!=digest or record.get('kind')!='DAILY':_error('History integrity differs')
        import csv,io
        text=io.StringIO(newline='');writer=csv.DictWriter(text,fieldnames=('series_id','date','high','low','close'))
        writer.writeheader();writer.writerows(record['rows']);raw=text.getvalue().encode('utf-8')
        if DailyDataset.from_csv(raw).content_hash!=record['dataset_hash']:_error('History dataset hash differs')
        return publish_new_bytes(raw,path)
