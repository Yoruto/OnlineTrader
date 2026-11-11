"""Explicit CSV review and closed-ledger, non-trading fee registration.

Comparison is read only. Registration consumes the original file and review
hash again; it never imports prices, quantities, positions or entry authority.
"""

import copy
from decimal import Decimal,localcontext
import hashlib
from pathlib import Path
import sqlite3
from uuid import uuid4

from online_trader.accounting.book import utc_now,validate_fee
from online_trader.contracts.codec import content_hash
from online_trader.domain.values import decimal_text,identifier
from online_trader.errors import TraderError
from online_trader.ledger import Ledger
from online_trader.reconciliation import load_statement,compare_statement
from online_trader.reporting import read_report
from online_trader.runtime.instance import AccountLock


_BINDING=('trade_key','contract_id','direction','effect','quantity','price','trading_day','event_seq')


def preview_file(statement_path,ledger_path,account_key,*,environment,trading_day,
                 source_account=None,checkpoint_event_seq=None,expected_kernel_boot_id=None):
    statement=load_statement(statement_path)
    report=read_report(ledger_path,account_key,checkpoint_event_seq=checkpoint_event_seq,
                       expected_kernel_boot_id=expected_kernel_boot_id)
    preview=compare_statement(statement,report,environment=environment,trading_day=trading_day,
                              source_account=source_account)
    preview['ledger_boot_id']=report.get('kernel_boot_id')
    preview['ledger_facts_hash']=content_hash([{name:row.get(name) for name in (*_BINDING,
        'fee','fee_quality','fee_revision','fee_evidence_id')} for row in report['trades']])
    preview.pop('preview_hash',None);preview['preview_hash']=content_hash(preview)
    return preview


def _identity(preview):
    return 'statement-import-'+content_hash({key:preview[key] for key in
        ('account_key','environment','trading_day','source_account','statement_hash')})


def _receipt(report,import_id):
    return next((copy.deepcopy(row) for row in report.get('fee_imports',[]) if row['import_id']==import_id),None)


def _validate_review(preview,expected_hash):
    if type(expected_hash) is not str or expected_hash!=preview['preview_hash']:
        raise TraderError('E_PREVIEW_STALE','The source file or ledger changed; review a fresh comparison')
    if preview['blocking']:
        raise TraderError('E_RECONCILIATION_BLOCKED','Comparison has source-scope or identity conflicts')


def _fees(preview,report):
    locals_={row['trade_key']:row for row in report['trades']};result=[];seen=set()
    for action in preview['actions']:
        key=identifier(action['trade_key'])
        if key in seen or key not in locals_:raise TraderError('E_OWNERSHIP','Fee actions must identify unique owned fills')
        seen.add(key);local=locals_[key]
        if content_hash({field:local[field] for field in _BINDING})!=action['fill_binding_hash']:
            raise TraderError('E_FACT_BARRIER','Original fill facts changed after review')
        before=None if local['fee'] is None and local['fee_quality']=='UNKNOWN' else {
            'amount':local['fee'],'quality':local['fee_quality'],'revision':local.get('fee_revision'),
            'evidence_id':local.get('fee_evidence_id')}
        if before!=action['before_fee']:raise TraderError('E_FACT_BARRIER','Original fee revision changed after review')
        fee=validate_fee({field:action[field] for field in ('trade_key','amount','quality','revision','evidence_id')})
        if fee['quality']!='CONFIRMED' or (before and before['quality']=='CONFIRMED'):
            raise TraderError('E_SCOPE','Only unknown or estimated owned fees can be registered')
        if fee['revision']!=(1 if before is None else before['revision']+1):
            raise TraderError('E_REVISION_CONFLICT','Fee revision differs from its reviewed predecessor')
        result.append(fee)
    return result


def register_fees(statement_path,ledger_path,account_key,*,environment,trading_day,source_account=None,
                  expected_preview_hash):
    """Register one reviewed, exact fee batch while the execution owner is closed.

    This does not open a Kernel or a gateway. A single existing Ledger writer is
    acquired under the host account lock, then the review is recomputed before
    one atomic fee/journal/receipt transaction. Entry authority stays DISARMED.
    """
    account_key=identifier(account_key)
    if environment!='ONLINE_SIM':raise TraderError('E_SCOPE','This offline development writer cannot modify a LIVE ledger')
    try:path=Path(ledger_path).resolve(strict=True)
    except (OSError,TypeError,ValueError) as error:raise TraderError('E_REPORT_INPUT','An existing product ledger is required') from error
    preview=preview_file(statement_path,path,account_key,environment=environment,trading_day=trading_day,
                         source_account=source_account)
    report=read_report(path,account_key);import_id=_identity(preview)
    previous=_receipt(report,import_id)
    if previous is not None:
        if preview['blocking']:
            raise TraderError('E_RECONCILIATION_BLOCKED','Current source comparison contains conflicts')
        return {'status':'ALREADY_REGISTERED','receipt':previous,'trading_authority':False}
    _validate_review(preview,expected_preview_hash)
    if not preview['actions']:
        return {'status':'NO_FEE_CHANGES','registered_count':0,'trading_authority':False}
    try:
        return _register_transaction(statement_path,path,account_key,environment,trading_day,source_account,
                                     expected_preview_hash,import_id)
    except TraderError:raise
    except (sqlite3.Error,OSError,TimeoutError) as error:
        raise TraderError('E_STORAGE','Fee registration could not be confirmed; inspect the original import receipt') from error


def _register_transaction(statement_path,path,account_key,environment,trading_day,source_account,
                          expected_preview_hash,import_id):
    with AccountLock(account_key):
        with Ledger(path,account_key,existing_only=True) as ledger:
            scope=ledger.query('SELECT environment,broker_identity FROM ledger_scope WHERE singleton=1')[0]
            if scope!={'environment':'ONLINE_SIM','broker_identity':'development'}:
                raise TraderError('E_SCOPE','Only the existing isolated development ledger can receive statement fees')
            fresh=preview_file(statement_path,path,account_key,environment=environment,trading_day=trading_day,
                               source_account=source_account)
            _validate_review(fresh,expected_preview_hash)
            report=read_report(path,account_key)
            fees=_fees(fresh,report)
            if int(fresh['checkpoint_event_seq'])!=int(report['checkpoint_event_seq']):
                raise TraderError('E_FACT_BARRIER','Ledger advanced while preparing the fee batch')
            with localcontext() as context:
                context.prec=80;total=decimal_text(sum((Decimal(fee['amount']) for fee in fees),Decimal(0)))
            receipt={key:copy.deepcopy(fresh[key]) for key in ('account_key','environment','trading_day','source_account',
                'source_sha256','statement_hash','checkpoint_event_seq','preview_hash','ledger_boot_id','ledger_facts_hash')}
            receipt['fee_bindings']=[{key:copy.deepcopy(action[key]) for key in
                ('row_number','trade_key','fill_binding_hash','before_fee')} for action in fresh['actions']]
            receipt.update(schema_version='STATEMENT_FEE_IMPORT/1',import_id=import_id,scope='CONFIRMED_OWNED_FEES_ONLY',
                registered_at_utc=utc_now(),fee_count=len(fees),total_fee_amount=total)
            payload={'account_key':account_key,'money_fees':fees,'fee_import':receipt}
            boot_id='fee-maintenance-'+uuid4().hex
            expected_revision=ledger.query('SELECT state_revision FROM authority WHERE singleton=1')[0]['state_revision']+1
            ack=ledger.commit_statement_fees(payload,expected_sequence=int(fresh['checkpoint_event_seq']),boot_id=boot_id,
                executable_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()).result(timeout=10)
            if (ack.tx_id!=import_id or ack.kernel_boot_id!=boot_id or ack.event_seq!=int(fresh['checkpoint_event_seq'])+1 or
                    ack.durable_seq!=ack.event_seq or ack.state_revision!=expected_revision or ack.outcome!='COMMITTED'):
                raise TraderError('E_COMMIT_ACK','Fee transaction acknowledgement identity differs; reconcile the original import')
            receipt['event_seq']=str(ack.event_seq)
            return {'status':'REGISTERED','receipt':receipt,'registered_count':len(fees),'trading_authority':False}
