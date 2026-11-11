"""Bounded external CSV comparison; matching never creates a fill or a fee.

DSV's four-column file can be reviewed, but a fee proposal requires exact owned
trade identity, explicit account/environment/day, side, effect, price and volume.
"""

from collections import Counter, defaultdict
import copy
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import io
from pathlib import Path
import re

from online_trader.contracts.codec import canonical_bytes, content_hash
from online_trader.domain.values import decimal_text, decimal_value, identifier, quantity, sequence
from online_trader.errors import TraderError


MAX_STATEMENT_BYTES=8*1024*1024
MAX_STATEMENT_ROWS=20000
MAX_STATEMENT_COLUMNS=64
MAX_CELL_CHARACTERS=4096
STATUSES=('INCOMPLETE','INVALID_ROW','SCOPE_MISMATCH','DUPLICATE_EXACT','DUPLICATE_CONFLICT',
          'EXTERNAL_ONLY','AMBIGUOUS','CONFLICT','MATCHED_FEE_UNKNOWN','ALREADY_CONFIRMED',
          'FEE_CONFLICT','MATCHED_FEE_NEW','MATCHED_ESTIMATED_REPLACEMENT')
_FIELDS=('account_key','environment','trading_day','trade_key','contract_id','side','effect',
         'quantity','price','fee','time','currency')
_ALIASES={
    'account_key':('account_key','账户','账号','资金账号'),
    'environment':('environment','环境'),
    'trading_day':('trading_day','交易日','交易日期'),
    'trade_key':('trade_key','trade_id','成交编号','成交序号'),
    'contract_id':('contract_id','symbol','合约','合约代码','代码'),
    'side':('side','买卖','买卖方向'),
    'effect':('effect','开平','开平标志'),
    'quantity':('quantity','volume','手数','成交量'),
    'price':('price','成交价','价格'),
    'fee':('fee','commission','手续费'),
    'time':('time','成交时间','时间'),
    'currency':('currency','币种','货币'),
}
_HEADERS={alias.casefold():field for field,aliases in _ALIASES.items() for alias in aliases}
_ENV={'online_sim':'ONLINE_SIM','模拟':'ONLINE_SIM','simulation':'ONLINE_SIM',
      'live':'LIVE','实盘':'LIVE'}
_SIDES={'buy':'BUY','买':'BUY','买入':'BUY','sell':'SELL','卖':'SELL','卖出':'SELL'}
_EFFECTS={'open':'OPEN','开':'OPEN','开仓':'OPEN','reduce':'REDUCE','close':'REDUCE',
          '平':'REDUCE','平仓':'REDUCE','平今':'REDUCE','平昨':'REDUCE',
          'close_today':'REDUCE','close_yesterday':'REDUCE'}
_NUMBER=re.compile(r'-?[0-9]+(?:\.[0-9]+)?\Z')
_UTC=re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\Z')
_LOCAL_TIME=re.compile(r'(?:(?:[0-9]{4}-[0-9]{2}-[0-9]{2})[ T])?([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,6}))?\Z')
_BINDING=('trade_key','contract_id','direction','effect','quantity','price','trading_day','event_seq')
_LOCAL_COLUMNS=_BINDING+('position_cycle_id','observed_at_utc','fee','fee_quality','fee_revision','fee_evidence_id')


def _error(message,code='E_STATEMENT_INPUT'):
    raise TraderError(code,message)


def _day(value):
    if type(value) is not str:_error('Expected an explicit trading day')
    if re.fullmatch('[0-9]{8}',value):value=value[:4]+'-'+value[4:6]+'-'+value[6:]
    try:
        if date.fromisoformat(value).isoformat()!=value:raise ValueError()
    except ValueError:_error('Invalid trading day')
    return value


def _decimal(value,*,positive=False):
    if type(value) is not str or len(value)>128 or not _NUMBER.fullmatch(value):
        _error('Expected a plain decimal statement value')
    try:
        result=decimal_text(Decimal(value));decimal_value(result,positive=positive)
    except (InvalidOperation,TraderError):_error('Statement amount is outside its bounded domain')
    return result


def _time(value):
    if _UTC.fullmatch(value):
        try:return datetime.fromisoformat(value).isoformat(timespec='microseconds').replace('+00:00','Z')
        except ValueError:_error('Invalid statement UTC timestamp')
    matched=_LOCAL_TIME.fullmatch(value)
    if not matched:_error('Invalid statement time')
    hour,minute,second=(int(matched[i]) for i in (1,2,3))
    if hour>23 or minute>59 or second>59:_error('Invalid statement clock time')
    if len(value)>15:
        try:date.fromisoformat(value[:10])
        except ValueError:_error('Invalid statement date')
    return value


def _row(cells,columns,row_number):
    result={key:None for key in _FIELDS};issues=[]
    for field,index in columns.items():
        value=cells[index].strip() if index<len(cells) else ''
        if not value:continue
        try:
            if field in {'account_key','trade_key','contract_id'}:normalized=identifier(value)
            elif field=='environment':
                normalized=_ENV.get(value.casefold())
                if normalized is None:raise ValueError()
            elif field=='trading_day':normalized=_day(value)
            elif field=='side':
                normalized=_SIDES.get(value.casefold())
                if normalized is None:raise ValueError()
            elif field=='effect':
                normalized=_EFFECTS.get(value.casefold())
                if normalized is None:raise ValueError()
            elif field=='quantity':
                if not re.fullmatch('[0-9]+',value) or len(value)>10:raise ValueError()
                normalized=quantity(int(value),positive=True)
            elif field in {'price','fee'}:
                normalized=_decimal(value,positive=field=='price')
                if field=='fee' and Decimal(normalized)<0:raise ValueError()
            elif field=='time':normalized=_time(value)
            else:
                if value.casefold() not in {'cny','rmb','人民币'}:raise ValueError()
                normalized='CNY'
            result[field]=normalized
        except (TraderError,ValueError,TypeError):
            issues.append('UNSUPPORTED_CURRENCY' if field=='currency' else 'INVALID_'+field.upper())
    for field in ('contract_id','quantity','price'):
        if result[field] is None and 'INVALID_'+field.upper() not in issues:issues.append('MISSING_'+field.upper())
    return {'row_number':row_number,**result,'issues':issues}


def _economic(row):
    return {field:row.get(field) for field in _FIELDS}|{'issues':copy.deepcopy(row.get('issues',[]))}


def _statement_hash(rows):
    unique={canonical_bytes(_economic(row)):_economic(row) for row in rows}
    return content_hash([unique[encoded] for encoded in sorted(unique)])


def load_statement(path):
    """Read only recognized CSV columns; never preserve arbitrary source cells."""
    try:
        path=Path(path)
        if not path.is_file():_error('Statement file does not exist')
        with path.open('rb') as stream:raw=stream.read(MAX_STATEMENT_BYTES+1)
    except (OSError,TypeError,ValueError) as error:
        raise TraderError('E_STATEMENT_INPUT','Statement file cannot be read') from error
    if not raw or len(raw)>MAX_STATEMENT_BYTES:_error('Statement exceeds the bounded file size','E_STATEMENT_LIMIT')
    try:text=raw.decode('utf-8-sig',errors='strict');encoding='utf-8-sig'
    except UnicodeDecodeError:
        try:text=raw.decode('gb18030',errors='strict');encoding='gb18030'
        except UnicodeDecodeError as error:raise TraderError('E_STATEMENT_ENCODING','Expected UTF-8 or strict GB18030') from error
    if '\x00' in text:_error('NUL is not a supported statement character','E_STATEMENT_FORMAT')
    first=text.splitlines()[0] if text.splitlines() else ''
    delimiter='\t' if '\t' in first else ','
    rows=[]
    try:
        reader=csv.reader(io.StringIO(text,newline=''),delimiter=delimiter,strict=True)
        headers=next(reader)
        if not headers or len(headers)>MAX_STATEMENT_COLUMNS or any(len(cell)>MAX_CELL_CHARACTERS for cell in headers):
            _error('Statement columns exceed the supported boundary','E_STATEMENT_LIMIT')
        columns={}
        for index,header in enumerate(headers):
            field=_HEADERS.get(header.strip().casefold())
            if field is None:continue
            if field in columns:_error('Several columns claim the same statement field','E_STATEMENT_COLUMNS')
            columns[field]=index
        if 'contract_id' not in columns:_error('No supported contract column was found','E_STATEMENT_COLUMNS')
        for cells in reader:
            if not cells or not any(cell.strip() for cell in cells):continue
            if len(rows)>=MAX_STATEMENT_ROWS:_error('Statement has too many records','E_STATEMENT_LIMIT')
            if len(cells)>MAX_STATEMENT_COLUMNS or any(len(cell)>MAX_CELL_CHARACTERS for cell in cells):
                _error('Statement cell boundary exceeded','E_STATEMENT_LIMIT')
            row=_row(cells,columns,reader.line_num)
            if len(cells)!=len(headers):row['issues'].append('COLUMN_COUNT_MISMATCH')
            rows.append(row)
    except (csv.Error,StopIteration) as error:
        raise TraderError('E_STATEMENT_FORMAT','Statement is not a complete supported CSV table') from error
    result={'schema_version':'EXTERNAL_FILLS/1','source_sha256':sha256(raw).hexdigest(),
            'statement_hash':_statement_hash(rows),'encoding':encoding,'delimiter':delimiter,
            'columns':list(columns),'rows':rows}
    canonical_bytes(result);return result


def _validate_statement(statement):
    if type(statement) is not dict or statement.get('schema_version')!='EXTERNAL_FILLS/1':
        _error('Unsupported normalized statement')
    canonical_bytes(statement)
    if not re.fullmatch('[0-9a-f]{64}',statement.get('source_sha256','')):_error('Invalid statement source hash')
    rows=statement.get('rows')
    if type(rows) is not list or len(rows)>MAX_STATEMENT_ROWS:_error('Invalid normalized statement rows')
    for row in rows:
        if type(row) is not dict or set(row)!={'row_number','issues',*_FIELDS}:_error('Invalid normalized statement row')
        quantity(row['row_number'],positive=True)
        allowed_issues={'COLUMN_COUNT_MISMATCH','UNSUPPORTED_CURRENCY'}|{'INVALID_'+field.upper() for field in _FIELDS}|{
            'MISSING_CONTRACT_ID','MISSING_QUANTITY','MISSING_PRICE'}
        if (type(row['issues']) is not list or len(row['issues'])>len(_FIELDS)+1 or
                any(issue not in allowed_issues for issue in row['issues'])):_error('Invalid statement row issues')
        if row['price'] is not None:decimal_value(row['price'],positive=True)
        if row['fee'] is not None and decimal_value(row['fee'])<0:_error('Invalid normalized fee')
        if row['quantity'] is not None:quantity(row['quantity'],positive=True)
        for field in ('account_key','contract_id','trade_key'):
            if row[field] is not None:identifier(row[field])
        if row['environment'] not in {'ONLINE_SIM','LIVE',None}:_error('Invalid normalized environment')
        if row['side'] not in {'BUY','SELL',None} or row['effect'] not in {'OPEN','REDUCE',None}:_error('Invalid normalized direction or effect')
        if row['currency'] not in {'CNY',None}:_error('Invalid normalized currency')
        if row['trading_day'] is not None and _day(row['trading_day'])!=row['trading_day']:_error('Trading day is not normalized')
        if row['time'] is not None and _time(row['time'])!=row['time']:_error('Time is not normalized')
    if statement.get('statement_hash')!=_statement_hash(rows):
        _error('Normalized statement content changed','E_STATEMENT_INTEGRITY')


def _local_record(raw):
    if type(raw) is not dict or not set(_BINDING)<=raw.keys():_error('Incomplete local confirmed fill','E_RECONCILIATION_INPUT')
    result={field:copy.deepcopy(raw[field]) for field in _LOCAL_COLUMNS if field in raw}
    for field in ('trade_key','contract_id'):identifier(result[field])
    if result['direction'] not in {'LONG','SHORT'} or result['effect'] not in {'OPEN','REDUCE'}:
        _error('Invalid local direction or effect','E_RECONCILIATION_INPUT')
    quantity(result['quantity'],positive=True);decimal_value(result['price'],positive=True)
    _day(result['trading_day']);sequence(result['event_seq'],positive=True)
    return result


def _side(local):
    return 'BUY' if (local['direction']=='LONG')==(local['effect']=='OPEN') else 'SELL'


def _before_fee(local):
    amount=local.get('fee');quality=local.get('fee_quality','UNKNOWN')
    if amount is None and quality=='UNKNOWN':return None
    if quality not in {'CONFIRMED','ESTIMATED'} or amount is None:_error('Invalid current local fee','E_RECONCILIATION_INPUT')
    if decimal_value(amount)<0:_error('Invalid current local fee','E_RECONCILIATION_INPUT')
    revision=local.get('fee_revision')
    if revision is None:return {'amount':amount,'quality':quality,'revision':None,'evidence_id':local.get('fee_evidence_id')}
    quantity(revision,positive=True)
    return {'amount':amount,'quality':quality,'revision':revision,'evidence_id':local.get('fee_evidence_id')}


def compare_statement(statement,report,*,environment,trading_day,source_account=None):
    """Prepare reviewable, scope-bound fee proposals without any ledger mutation."""
    _validate_statement(statement)
    if type(report) is not dict or report.get('schema_version')!='MONEY_REPORT/1':_error('Expected a local money report','E_RECONCILIATION_INPUT')
    canonical_bytes(report);account=identifier(report.get('account_key'))
    if environment not in {'ONLINE_SIM','LIVE'} or report.get('environment')!=environment:
        _error('Report environment differs from the requested source scope','E_SCOPE')
    if report.get('currency')!='CNY':_error('Statement reconciliation requires a CNY report','E_CURRENCY')
    day=_day(trading_day);source_account=identifier(account if source_account is None else source_account)
    checkpoint=report.get('checkpoint_event_seq');sequence(checkpoint)
    raw_locals=report.get('trades')
    if type(raw_locals) is not list or len(raw_locals)>100000:_error('Invalid local confirmed fill set','E_RECONCILIATION_INPUT')
    locals_=[_local_record(row) for row in raw_locals]
    by_id=defaultdict(list)
    for index,local in enumerate(locals_):by_id[local['trade_key']].append((index,local))
    external_groups=defaultdict(list)
    for row in statement['rows']:
        if row['trade_key'] is not None:
            external_groups[(row['account_key'],row['environment'],row['trading_day'],row['trade_key'])].append(row)
    conflicts={key for key,group in external_groups.items() if len({canonical_bytes(_economic(row)) for row in group})>1}
    seen=set();matched=set();rows=[];actions=[];blocking=False
    for source in statement['rows']:
        row=copy.deepcopy(source)
        row.update(local_trade_key=None,candidate_trade_keys=[],local_price=None,local_quantity=None,
                   local_fee=None,local_fee_quality=None,fee_delta=None)
        issues=row['issues'];key=(row['account_key'],row['environment'],row['trading_day'],row['trade_key'])
        missing=[field for field in ('account_key','environment','trading_day','trade_key','side','effect') if row[field] is None]
        if issues:
            status='INVALID_ROW';blocking=True
        elif missing:
            issues.extend('MISSING_'+field.upper() for field in missing);status='INCOMPLETE';blocking=True
        elif row['account_key']!=source_account or row['environment']!=environment or row['trading_day']!=day:
            status='SCOPE_MISMATCH';blocking=True
            for field,expected in (('account_key',source_account),('environment',environment),('trading_day',day)):
                if row[field]!=expected:issues.append(field.upper()+'_MISMATCH')
        elif key in conflicts:
            status='DUPLICATE_CONFLICT';issues.append('SAME_SOURCE_ID_CHANGED_CONTENT');blocking=True
        elif key in seen:
            status='DUPLICATE_EXACT';issues.append('EXACT_SOURCE_DUPLICATE_IGNORED')
        else:
            seen.add(key);candidates=by_id.get(row['trade_key'],[])
            if not candidates:status='EXTERNAL_ONLY'
            elif len(candidates)>1:
                status='AMBIGUOUS';issues.append('AMBIGUOUS_LOCAL_IDENTITY');blocking=True
                row['candidate_trade_keys']=[candidate['trade_key'] for _,candidate in candidates[:3]]
            else:
                index,local=candidates[0]
                row.update(local_trade_key=local['trade_key'],local_price=local['price'],local_quantity=local['quantity'],
                           local_fee=local.get('fee'),local_fee_quality=local.get('fee_quality','UNKNOWN'))
                for field,expected in (('contract_id',local['contract_id']),('trading_day',local['trading_day']),
                                       ('side',_side(local)),('effect',local['effect']),('quantity',local['quantity'])):
                    if row[field]!=expected:issues.append(field.upper()+'_MISMATCH')
                if Decimal(row['price'])!=Decimal(local['price']):issues.append('PRICE_MISMATCH')
                if issues:status='CONFLICT';blocking=True
                else:
                    matched.add(index);before=_before_fee(local)
                    if row['fee'] is None:status='MATCHED_FEE_UNKNOWN'
                    elif before and before['quality']=='CONFIRMED':
                        row['fee_delta']=decimal_text(Decimal(row['fee'])-Decimal(before['amount']))
                        if Decimal(row['fee'])==Decimal(before['amount']):status='ALREADY_CONFIRMED'
                        else:status='FEE_CONFLICT';issues.append('CONFIRMED_FEE_DIFFERS');blocking=True
                    elif before and before['revision'] is None:
                        status='CONFLICT';issues.append('FEE_REVISION_UNPROVEN');blocking=True
                    elif before and before['revision']==2147483647:
                        status='CONFLICT';issues.append('FEE_REVISION_EXHAUSTED');blocking=True
                    else:
                        status='MATCHED_ESTIMATED_REPLACEMENT' if before else 'MATCHED_FEE_NEW'
                        if before:row['fee_delta']=decimal_text(Decimal(row['fee'])-Decimal(before['amount']))
                        actions.append({'trade_key':local['trade_key'],'amount':row['fee'],'quality':'CONFIRMED',
                            'revision':1 if before is None else before['revision']+1,
                            'evidence_id':'statement-fee-'+content_hash({'statement_hash':statement['statement_hash'],'trade_key':local['trade_key']}),
                            'before_fee':before,'fill_binding_hash':content_hash({field:local[field] for field in _BINDING}),
                            'row_number':row['row_number']})
        row['status']=status;rows.append(row)
    local_only=[{'status':'LOCAL_ONLY',**copy.deepcopy(local)} for index,local in enumerate(locals_)
                if index not in matched and local['trading_day']==day]
    counts=Counter(row['status'] for row in rows)
    with localcontext() as context:
        context.prec=80;total=decimal_text(sum((Decimal(action['amount']) for action in actions),Decimal(0)))
    try:decimal_value(total)
    except TraderError:total=None;blocking=True
    summary={'external_rows':len(rows),'local_rows':sum(local['trading_day']==day for local in locals_),
        'matched_rows':len(matched),'local_only_rows':len(local_only),'external_only_rows':counts['EXTERNAL_ONLY'],
        'duplicate_rows':counts['DUPLICATE_EXACT']+counts['DUPLICATE_CONFLICT'],
        'blocking_rows':sum(counts[name] for name in ('INCOMPLETE','INVALID_ROW','SCOPE_MISMATCH','DUPLICATE_CONFLICT','AMBIGUOUS','CONFLICT','FEE_CONFLICT')),
        'fee_action_count':len(actions),'total_fee_amount':total,'by_status':dict(sorted(counts.items()))}
    preview={'schema_version':'FILL_RECONCILIATION/1','account_key':account,'environment':environment,
        'trading_day':day,'source_account':source_account,'currency':'CNY','source_sha256':statement['source_sha256'],
        'statement_hash':statement['statement_hash'],'checkpoint_event_seq':checkpoint,'rows':rows,
        'local_only':local_only,'actions':actions,'blocking':blocking,'summary':summary}
    preview['preview_hash']=content_hash(preview);canonical_bytes(preview);return preview
