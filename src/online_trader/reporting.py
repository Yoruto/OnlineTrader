"""Read-only journal reports and bounded, explicit exports.

The reader neither boots a kernel nor opens a writer. An explicit owner watermark
keeps SQLite-committed but not yet owner-accepted facts outside the chosen report.
"""

from contextlib import closing
import csv
from hashlib import sha256
import io
import json
from pathlib import Path
import re
import sqlite3

from online_trader.accounting.book import MoneyBook
from online_trader.contracts.codec import canonical_bytes, strict_loads
from online_trader.domain.values import decimal_value, identifier, sequence
from online_trader.errors import TraderError
from online_trader.scheduling.clock import ClockSample


MAX_JOURNAL_EVENTS=100000
MAX_JOURNAL_BYTES=128*1024*1024
MAX_EVENT_BYTES=16*1024*1024
MAX_EXPORT_BYTES=64*1024*1024

_AMOUNTS=('gross_realized_pnl','confirmed_fees','estimated_fees','fees','net_realized_pnl',
          'estimated_net_realized_pnl','unrealized_pnl','total_pnl','entry_quantity',
          'exit_quantity','fill_count','unknown_fee_count','estimated_fee_count','quality')
_SUMMARY=_AMOUNTS+('closed_cycles','win_count','loss_count','win_rate','win_rate_basis','win_rate_known_cycles')
_ACCOUNT=('account_key','currency','trading_day','observed_at_utc','event_seq','evidence_id',
          'source_epoch','revision','complete','balance','pre_balance','available','margin',
          'frozen_margin','close_profit','position_profit','float_profit','commission',
          'net_cash_flow','equity_change','daily_pnl','daily_pnl_basis','trading_pnl',
          'today_pnl','profit_ratio','today_pnl_ratio',
          'scope','local_fill_reconciliation','quality')
_TABLES={
    'by_contract':('contract_id',)+_AMOUNTS,
    'by_day':('trading_day',)+_AMOUNTS,
    'cycles':('position_cycle_id','contract_id','direction','trading_day','last_fill_trading_day',
              'theoretical_entry_price','remaining_quantity','closed','valuation_trading_day',
              'valuation_observed_at_utc')+_AMOUNTS,
    'trades':('trade_key','fill_id','child_order_id','order_intent_id','position_cycle_id',
              'contract_id','direction','effect','quantity','price','multiplier',
              'theoretical_entry_price','trading_day','observed_at_utc','event_seq','time_basis',
              'realized_pnl','fee','fee_quality','fee_revision','fee_evidence_id','directional_slippage_points'),
    'equity':_ACCOUNT,
    'nominal':('contract_id','trading_day','daily_pnl_points','net_loss_points','stop_count',
               'profit_count','trade_count','total_volume','stop_streak','day_stopped',
               'day_stopped_reason','continuation_stopped','month_stop'),
}
_META=('account_key','currency','accounting_basis','checkpoint_event_seq','kernel_boot_id','as_of_utc','boundary','environment')
_TOP=('schema_version',)+_META+('scope','rounding_policy')
_ISSUE=('code','position_cycle_id','contract_id','trade_key','evidence_id','field','trading_day','scope','error_code')


def _checkpoint(value):
    if type(value) is int:
        value=str(value)
    try:return sequence(value)
    except TraderError as error:raise TraderError('E_REPORT_CHECKPOINT','Expected a nonnegative report checkpoint') from error


def read_report(ledger_path,account_key,*,as_of_utc=None,checkpoint_event_seq=None,expected_kernel_boot_id=None):
    """Rebuild exactly one consistent, account-scoped journal prefix.

    Without a checkpoint the boundary means database committed, never owner
    accepted. No projection table is consulted and no missing file is created.
    """
    try:
        account_key=identifier(account_key)
        path=Path(ledger_path).resolve()
    except (TraderError,TypeError,ValueError,OSError) as error:
        raise TraderError('E_REPORT_INPUT','Invalid report ledger or account') from error
    if as_of_utc is not None:ClockSample(as_of_utc,'0')
    requested=None if checkpoint_event_seq is None else _checkpoint(checkpoint_event_seq)
    if expected_kernel_boot_id is not None:
        try:identifier(expected_kernel_boot_id)
        except TraderError as error:raise TraderError('E_BOOT_MISMATCH','Invalid expected report boot identity') from error
        if requested is None or requested==0:
            raise TraderError('E_BOOT_MISMATCH','A boot-bound report needs a positive explicit checkpoint')
    if not path.is_file():raise TraderError('E_REPORT_INPUT','Report ledger does not exist')
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,isolation_level=None,timeout=2)) as conn:
            conn.row_factory=sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            if conn.execute('PRAGMA query_only').fetchone()[0]!=1:
                raise TraderError('E_REPORT_INPUT','Report connection is not read only')
            conn.execute('PRAGMA trusted_schema=OFF')
            conn.execute('BEGIN')
            scope=conn.execute('SELECT singleton,account_key,environment,schema_version FROM ledger_scope').fetchall()
            if (len(scope)!=1 or scope[0]['singleton']!=1 or scope[0]['account_key']!=account_key or
                    scope[0]['environment'] not in {'ONLINE_SIM','LIVE'}):
                raise TraderError('E_SCOPE','Report ledger scope does not match')
            if scope[0]['schema_version']!=1:
                raise TraderError('E_SCHEMA_MISMATCH','Unsupported report ledger schema')
            maximum=conn.execute('SELECT COALESCE(MAX(event_seq),0) FROM event_journal').fetchone()[0]
            if type(maximum) is not int or maximum<0:
                raise TraderError('E_REPORT_INTEGRITY','Invalid journal boundary')
            checkpoint=maximum if requested is None else requested
            if checkpoint>maximum:
                raise TraderError('E_REPORT_CHECKPOINT','Report checkpoint is beyond committed facts')
            book=MoneyBook(account_key);expected=1;total=0;checkpoint_boot=None
            cursor=conn.execute('SELECT event_seq,kernel_boot_id,payload_json,payload_hash,observed_at_utc FROM event_journal '
                                'WHERE event_seq<=? ORDER BY event_seq LIMIT ?',
                                (checkpoint,MAX_JOURNAL_EVENTS+1))
            for count,row in enumerate(cursor,1):
                if count>MAX_JOURNAL_EVENTS:
                    raise TraderError('E_REPORT_LIMIT','Journal exceeds the bounded report capacity')
                if type(row['event_seq']) is not int or row['event_seq']!=expected:
                    raise TraderError('E_REPORT_INTEGRITY','Journal prefix is not contiguous')
                raw=row['payload_json']
                if type(raw) is not str:
                    raise TraderError('E_REPORT_INTEGRITY','Journal payload is not UTF-8 JSON text')
                encoded=raw.encode('utf-8');total+=len(encoded)
                if len(encoded)>MAX_EVENT_BYTES or total>MAX_JOURNAL_BYTES:
                    raise TraderError('E_REPORT_LIMIT','Journal payload exceeds the bounded report capacity')
                digest=row['payload_hash']
                if (type(digest) is not str or not re.fullmatch('[0-9a-f]{64}',digest) or
                        sha256(encoded).hexdigest()!=digest):
                    raise TraderError('E_REPORT_INTEGRITY','Journal payload hash does not match')
                try:
                    payload=strict_loads(raw)
                    if canonical_bytes(payload)!=encoded:raise ValueError()
                    if (type(payload) is not dict or set(payload)!={'data','commit'} or
                            type(payload['data']) is not dict or type(payload['commit']) is not dict or
                            set(payload['commit'])!={'state_revision'}):raise ValueError()
                    sequence(payload['commit']['state_revision'],positive=True)
                    ClockSample(row['observed_at_utc'],'0')
                except (TraderError,ValueError,TypeError,KeyError) as error:
                    raise TraderError('E_REPORT_INTEGRITY','Journal payload is not the supported canonical contract') from error
                data=payload['data']
                if data.get('account_key',account_key)!=account_key:
                    raise TraderError('E_SCOPE','Journal fact belongs to another account')
                book.accept(data,row['event_seq'],row['observed_at_utc']);expected+=1
                checkpoint_boot=row['kernel_boot_id']
            if expected-1!=checkpoint:
                raise TraderError('E_REPORT_INTEGRITY','Journal does not prove the requested prefix')
            if expected_kernel_boot_id is not None:
                boot=conn.execute('SELECT account_key FROM kernel_boots WHERE kernel_boot_id=?',
                                  (expected_kernel_boot_id,)).fetchall()
                if checkpoint_boot!=expected_kernel_boot_id or len(boot)!=1 or boot[0]['account_key']!=account_key:
                    raise TraderError('E_BOOT_MISMATCH','Report checkpoint belongs to another kernel boot or ledger copy')
            valuation={} if expected_kernel_boot_id is None else {'valuation_boot_id':expected_kernel_boot_id}
            report=book.report(checkpoint_event_seq=str(checkpoint),as_of_utc=as_of_utc,**valuation)
            report['boundary']='DATABASE_COMMITTED' if requested is None else 'OWNER_CHECKPOINT'
            report['environment']=scope[0]['environment']
            report['kernel_boot_id']=checkpoint_boot
            return report
    except TraderError:raise
    except (sqlite3.Error,OSError,UnicodeError,KeyError,TypeError,ValueError) as error:
        raise TraderError('E_REPORT_INPUT','Report ledger cannot be read safely') from error


def _flat(row,fields):
    if type(row) is not dict:raise TraderError('E_REPORT_SCHEMA','Expected a report row')
    result={}
    for name in fields:
        if name not in row:continue
        value=row[name]
        if value is not None and type(value) not in {str,int,bool}:
            raise TraderError('E_REPORT_SCHEMA','Report columns must contain scalar values')
        result[name]=value
    return result


def _export_material(report):
    if type(report) is not dict or report.get('schema_version')!='MONEY_REPORT/1':
        raise TraderError('E_REPORT_SCHEMA','Unsupported report schema')
    canonical_bytes(report)
    for key in ('account_key','currency','accounting_basis','checkpoint_event_seq','as_of_utc','scope','rounding_policy'):
        if type(report.get(key)) is not str:raise TraderError('E_REPORT_SCHEMA','Incomplete report metadata')
    identifier(report['account_key']);_checkpoint(report['checkpoint_event_seq']);ClockSample(report['as_of_utc'],'0')
    if report['currency']!='CNY':raise TraderError('E_REPORT_SCHEMA','Unsupported report currency')
    result=_flat(report,_TOP)
    result['summary']=_flat(report.get('summary'),_SUMMARY)
    for table,fields in _TABLES.items():
        rows=report.get(table)
        if type(rows) is not list or len(rows)>MAX_JOURNAL_EVENTS:
            raise TraderError('E_REPORT_SCHEMA','Expected a bounded report table')
        result[table]=[_flat(row,fields) for row in rows]
    account=report.get('account')
    result['account']=None if account is None else _flat(account,_ACCOUNT)
    issues=report.get('issues')
    if type(issues) is not list or len(issues)>MAX_JOURNAL_EVENTS:
        raise TraderError('E_REPORT_SCHEMA','Expected bounded report issues')
    result['issues']=[_flat(issue,_ISSUE) for issue in issues]
    imports=report.get('fee_imports',[])
    if type(imports) is not list or len(imports)>MAX_JOURNAL_EVENTS:
        raise TraderError('E_REPORT_SCHEMA','Expected bounded fee import receipts')
    result['fee_imports']=[_flat(row,('import_id','account_key','environment','trading_day','source_account',
        'source_sha256','statement_hash','preview_hash','checkpoint_event_seq','fee_count','total_fee_amount',
        'event_seq','scope','registered_at_utc','ledger_boot_id','ledger_facts_hash')) for row in imports]
    return result


def _cell(value):
    if value is None:return ''
    if type(value) is bool:return 'true' if value else 'false'
    text=str(value)
    if type(value) is str:
        try:decimal_value(text);numeric=True
        except TraderError:numeric=False
        if not numeric and (text.startswith(('\t','\r','\n')) or text.lstrip(' \t\r\n').startswith(('=','+','-','@'))):
            text="'"+text
    return text


def export_report(report,destination,*,table='trades',format=None):
    """Export trusted columns only, to a new CSV or JSON file.

    CSV metadata travels with every row. Unknown amounts are empty cells and
    spreadsheet formulas are escaped without converting genuine negative money.
    """
    try:path=Path(destination).resolve()
    except (TypeError,ValueError,OSError) as error:raise TraderError('E_REPORT_PATH','Invalid export destination') from error
    kind=path.suffix.lower().lstrip('.') if format is None else format.lower() if type(format) is str else None
    if kind not in {'csv','json'}:raise TraderError('E_REPORT_FORMAT','Expected CSV or JSON export format')
    if kind=='csv' and (type(table) is not str or table not in _TABLES):
        raise TraderError('E_REPORT_FORMAT','Unsupported report table')
    if not path.parent.is_dir():raise TraderError('E_REPORT_PATH','Export directory does not exist')
    material=_export_material(report)
    if kind=='json':
        payload=(json.dumps(material,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    else:
        columns=tuple(dict.fromkeys((*_META,*_TABLES[table])))
        stream=io.StringIO(newline='');writer=csv.writer(stream,lineterminator='\r\n')
        writer.writerow(columns)
        for row in material[table]:
            writer.writerow([_cell(material.get(name) if name in _META else row.get(name)) for name in columns])
        payload=stream.getvalue().encode('utf-8-sig')
    if len(payload)>MAX_EXPORT_BYTES:
        raise TraderError('E_REPORT_LIMIT','Serialized export exceeds the bounded capacity')
    created=False
    try:
        with path.open('xb') as output:
            created=True;output.write(payload)
    except FileExistsError as error:raise TraderError('E_REPORT_EXISTS','Export destination already exists') from error
    except OSError as error:
        if created:
            try:path.unlink()
            except OSError:pass
        raise TraderError('E_REPORT_PATH','Export could not be written') from error
    return path
