"""Read-only money reports respect journal snapshots, scope and export quality."""

import csv
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from online_trader.accounting.book import MoneyBook
from online_trader.contracts.codec import canonical_bytes
from online_trader.errors import TraderError
from online_trader.ledger import Ledger
from online_trader.reporting import read_report,export_report


AT='2026-11-10T01:00:02Z'
DAY='2026-11-10'


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name);self.path=self.folder/'account.db'
        self.key='DEV:report-'+uuid4().hex
        self.ledger=Ledger(self.path,self.key);self.addCleanup(self.ledger.close)
        self.ledger.bootstrap('boot-report-'+uuid4().hex,'a'*64)

    def data(self,effect,*,quantity=2,price=None,fee=None,quality='CONFIRMED'):
        suffix='entry' if effect=='OPEN' else 'exit'
        cycle={'position_cycle_id':'report-cycle','plan':{'account_key':self.key,
            'contract_id':'DCE.p2701','direction':'LONG','trading_day':DAY,
            'theoretical_entry_price':'100','fixed_stop_loss':{'distance_points':'10'}},
            'financial_instrument':{'currency':'CNY','multiplier':'10','basis':'EXPLICIT_DEVELOPMENT',
                'max_quote_age_ms':30000},'orders':[{'child_order_id':'entry-child'},{'child_order_id':'exit-child'}],
            'protection':{'quality':'CONTIGUOUS'},'last_quote':None}
        fill={'trade_key':'report-'+suffix,'fill_id':'fill-'+suffix,'child_order_id':suffix+'-child',
            'quantity':quantity,'price':price or ('100' if effect=='OPEN' else '110'),
            'effect':effect,'trading_day':DAY,'observed_at_utc':AT}
        data={'cycle':cycle,'fill':fill}
        if fee is not None:data['money_fee']={'trade_key':fill['trade_key'],'amount':fee,'quality':quality,
            'evidence_id':'fee-'+suffix,'revision':1}
        return data

    def submit(self,data):return self.ledger.submit('TX_FILL','REPORT_TEST_CONFIRMED_FILL',data).result(timeout=3)

    def seed(self,*,entry_fee=None,exit_fee=None,entry_quality='CONFIRMED'):
        self.submit(self.data('OPEN',fee=entry_fee,quality=entry_quality))
        self.submit(self.data('REDUCE',quantity=1,fee=exit_fee))
        return read_report(self.path,self.key,as_of_utc=AT)

    def corrupt(self,sql,params=()):
        self.ledger.close()
        with closing(sqlite3.connect(self.path)) as conn,conn:
            conn.execute('DROP TRIGGER IF EXISTS journal_no_update')
            conn.execute('DROP TRIGGER IF EXISTS journal_no_delete')
            conn.execute('DROP TRIGGER IF EXISTS scope_no_update')
            conn.execute('PRAGMA ignore_check_constraints=ON')
            conn.execute(sql,params)

    def test_missing_ledger_does_not_create_a_database_or_writer_lock(self):
        missing=self.folder/'missing.db'
        with self.assertRaises(TraderError):read_report(missing,self.key)
        self.assertFalse(missing.exists());self.assertFalse(missing.with_suffix('.db.writer.lock').exists())

    def test_reader_coexists_with_writer_and_does_not_boot_or_mutate(self):
        before=self.ledger.query('SELECT count(*) AS n FROM event_journal')[0]['n']
        lock=self.path.with_name(self.path.name+'.writer.lock');stamp=lock.stat().st_mtime_ns
        report=read_report(self.path,self.key,as_of_utc=AT)
        self.assertEqual(report['schema_version'],'MONEY_REPORT/1')
        self.assertEqual(report['boundary'],'DATABASE_COMMITTED')
        self.assertEqual(report['environment'],'ONLINE_SIM')
        self.assertEqual(report['checkpoint_event_seq'],'1')
        self.assertEqual(self.ledger.query('SELECT count(*) AS n FROM event_journal')[0]['n'],before)
        self.assertEqual(lock.stat().st_mtime_ns,stamp)
        self.assertIsNone(report['account'])

    def test_wrong_account_is_rejected(self):
        with self.assertRaises(TraderError) as error:read_report(self.path,'DEV:wrong')
        self.assertEqual(error.exception.code,'E_SCOPE')

    def test_unknown_environment_and_schema_are_not_accepted(self):
        for column,value,code in (('environment','UNKNOWN','E_SCOPE'),('schema_version',2,'E_SCHEMA_MISMATCH')):
            with self.subTest(column=column):
                self.corrupt('UPDATE ledger_scope SET '+column+'=?',(value,))
                with self.assertRaises(TraderError) as error:read_report(self.path,self.key)
                self.assertEqual(error.exception.code,code)
                with closing(sqlite3.connect(self.path)) as conn,conn:
                    conn.execute('PRAGMA ignore_check_constraints=ON')
                    conn.execute('UPDATE ledger_scope SET environment=?,schema_version=1',('ONLINE_SIM',))

    def test_changed_payload_without_matching_hash_is_rejected(self):
        self.submit(self.data('OPEN'))
        raw=canonical_bytes({'data':{},'commit':{'state_revision':'1'}}).decode()
        self.corrupt('UPDATE event_journal SET payload_json=? WHERE event_seq=2',(raw,))
        with self.assertRaises(TraderError) as error:read_report(self.path,self.key)
        self.assertEqual(error.exception.code,'E_REPORT_INTEGRITY')

    def test_even_matching_hash_cannot_make_noncanonical_json_valid(self):
        self.submit(self.data('OPEN'))
        raw=json.dumps({'data':{},'commit':{'state_revision':'1'}},indent=2)
        self.corrupt('UPDATE event_journal SET payload_json=?,payload_hash=? WHERE event_seq=2',
            (raw,sha256(raw.encode()).hexdigest()))
        with self.assertRaises(TraderError) as error:read_report(self.path,self.key)
        self.assertEqual(error.exception.code,'E_REPORT_INTEGRITY')

    def test_journal_gap_is_not_silently_treated_as_a_complete_report(self):
        self.seed();self.corrupt('DELETE FROM event_journal WHERE event_seq=2')
        with self.assertRaises(TraderError) as error:read_report(self.path,self.key)
        self.assertEqual(error.exception.code,'E_REPORT_INTEGRITY')

    def test_owner_checkpoint_excludes_newer_database_committed_fills(self):
        full=self.seed(entry_fee='0',exit_fee='2')
        prefix=read_report(self.path,self.key,as_of_utc=AT,checkpoint_event_seq='2')
        self.assertEqual(prefix['boundary'],'OWNER_CHECKPOINT')
        self.assertEqual(prefix['checkpoint_event_seq'],'2')
        self.assertEqual(len(prefix['trades']),1)
        self.assertEqual(prefix['summary']['gross_realized_pnl'],'0')
        self.assertEqual(prefix['cycles'][0]['remaining_quantity'],2)
        self.assertEqual(full['summary']['gross_realized_pnl'],'100')
        self.assertEqual(full['summary']['net_realized_pnl'],'98')

    def test_matching_owner_boot_and_new_account_fields_survive_json_and_csv(self):
        boot=self.ledger.kernel_boot_id
        fact={'account_key':self.key,'currency':'CNY','trading_day':DAY,'observed_at_utc':AT,
            'source_epoch':1,'revision':1,'evidence_id':'report-account','complete':True,
            'balance':'100200','pre_balance':'100000','available':None,'margin':'1000',
            'frozen_margin':'0','close_profit':'220','position_profit':'0','float_profit':'0',
            'commission':'20','net_cash_flow':'0','source_issues':[{'code':'ACCOUNT_MONEY_FIELD_INVALID',
                'field':'available','error_code':'E_DECIMAL'}]}
        self.ledger.submit('TX_OBSERVATION','REPORT_TEST_ACCOUNT',{'money_account':fact}).result(timeout=3)
        report=read_report(self.path,self.key,as_of_utc=AT,checkpoint_event_seq='2',expected_kernel_boot_id=boot)
        self.assertEqual(report['kernel_boot_id'],boot)
        self.assertEqual(report['boundary'],'OWNER_CHECKPOINT')
        path=export_report(report,self.folder/'account.json')
        exported=json.loads(path.read_text(encoding='utf-8'))
        for name,value in (('today_pnl','200'),('profit_ratio','0.002'),('today_pnl_ratio','0.002')):
            self.assertEqual(exported['account'][name],value)
            self.assertEqual(exported['equity'][0][name],value)
        self.assertEqual(exported['issues'][0]['error_code'],'E_DECIMAL')
        self.assertEqual(exported['kernel_boot_id'],boot)
        path=export_report(report,self.folder/'account.csv',table='equity')
        with path.open(encoding='utf-8-sig',newline='') as stream:row=next(csv.DictReader(stream))
        self.assertEqual(row['today_pnl'],'200');self.assertEqual(row['profit_ratio'],'0.002')
        self.assertEqual(row['today_pnl_ratio'],'0.002');self.assertEqual(row['kernel_boot_id'],boot)

    def test_owner_boot_must_match_checkpoint_and_registered_account_scope(self):
        self.seed();boot=self.ledger.kernel_boot_id
        with self.assertRaises(TraderError) as error:
            read_report(self.path,self.key,checkpoint_event_seq='3',expected_kernel_boot_id='boot-another-copy')
        self.assertEqual(error.exception.code,'E_BOOT_MISMATCH')
        self.corrupt('UPDATE kernel_boots SET account_key=? WHERE kernel_boot_id=?',('DEV:foreign',boot))
        with self.assertRaises(TraderError) as error:
            read_report(self.path,self.key,checkpoint_event_seq='3',expected_kernel_boot_id=boot)
        self.assertEqual(error.exception.code,'E_BOOT_MISMATCH')

    def test_expected_owner_boot_requires_a_positive_explicit_watermark(self):
        for checkpoint in (None,0,'0'):
            with self.subTest(checkpoint=checkpoint),self.assertRaises(TraderError) as error:
                read_report(self.path,self.key,checkpoint_event_seq=checkpoint,
                    expected_kernel_boot_id=self.ledger.kernel_boot_id)
            self.assertEqual(error.exception.code,'E_BOOT_MISMATCH')

    def test_fast_new_boot_cannot_reuse_old_quotes_as_current_but_history_can_value_them(self):
        old_boot=self.ledger.kernel_boot_id;data=self.data('OPEN',fee='0')
        data['cycle']['money_quote']={'price':'110','observed_at_utc':AT,'received_ns':'123','trading_day':DAY}
        data['cycle']['last_quote']={'kernel_boot_id':old_boot,'received_ns':'123','price':'110'}
        self.submit(data)
        self.ledger.close();self.ledger=Ledger(self.path,self.key);self.addCleanup(self.ledger.close)
        boot='boot-restarted-'+uuid4().hex;self.ledger.bootstrap(boot,'b'*64)
        live=read_report(self.path,self.key,as_of_utc=AT,checkpoint_event_seq='3',expected_kernel_boot_id=boot)
        history=read_report(self.path,self.key,as_of_utc=AT)
        self.assertEqual(live['kernel_boot_id'],boot)
        self.assertIsNone(live['summary']['unrealized_pnl'])
        self.assertIn('VALUATION_UNKNOWN',{issue['code'] for issue in live['issues']})
        self.assertEqual(history['summary']['unrealized_pnl'],'200')
        self.assertEqual(history['boundary'],'DATABASE_COMMITTED')

    def test_checkpoint_must_be_canonical_existing_prefix_and_zero_is_valid(self):
        for value in (True,-1,'01','1.0','9223372036854775808','2',[]):
            with self.subTest(value=value),self.assertRaises(TraderError):read_report(self.path,self.key,checkpoint_event_seq=value)
        empty=read_report(self.path,self.key,as_of_utc=AT,checkpoint_event_seq=0)
        self.assertEqual(empty['checkpoint_event_seq'],'0');self.assertEqual(empty['trades'],[])

    def test_database_snapshot_does_not_mix_in_a_writer_commit_during_replay(self):
        self.submit(self.data('OPEN',fee='0'));original=MoneyBook.accept;written=[]
        def accept(book,data,event_seq,observed):
            if not written:
                written.append(self.submit(self.data('REDUCE',quantity=1,fee='0')))
            return original(book,data,event_seq,observed)
        with patch.object(MoneyBook,'accept',accept):report=read_report(self.path,self.key,as_of_utc=AT)
        self.assertEqual(report['checkpoint_event_seq'],'2');self.assertEqual(len(report['trades']),1)
        self.assertEqual(self.ledger.query('SELECT max(event_seq) AS n FROM event_journal')[0]['n'],3)

    def test_missing_fee_stays_unknown_and_cannot_be_a_zero_net_profit(self):
        report=self.seed()
        self.assertEqual(report['summary']['gross_realized_pnl'],'100')
        self.assertIsNone(report['summary']['fees']);self.assertIsNone(report['summary']['net_realized_pnl'])
        self.assertEqual(report['trades'][0]['fee_quality'],'UNKNOWN')

    def test_explicit_zero_fees_are_confirmed_zero(self):
        report=self.seed(entry_fee='0',exit_fee='0')
        self.assertEqual(report['summary']['fees'],'0')
        self.assertEqual(report['summary']['net_realized_pnl'],'100')
        self.assertTrue(all(row['fee_quality']=='CONFIRMED' for row in report['trades']))

    def test_estimated_fees_remain_separate_from_confirmed_net(self):
        report=self.seed(entry_fee='1',exit_fee='2',entry_quality='ESTIMATED')
        self.assertEqual(report['summary']['confirmed_fees'],'2')
        self.assertEqual(report['summary']['estimated_fees'],'1')
        self.assertEqual(report['summary']['estimated_net_realized_pnl'],'97')
        self.assertIsNone(report['summary']['net_realized_pnl'])

    def test_csv_has_bom_unknown_empty_quality_and_scope_metadata(self):
        report=self.seed();path=export_report(report,self.folder/'成交.csv')
        self.assertTrue(path.read_bytes().startswith(b'\xef\xbb\xbf'))
        with path.open(encoding='utf-8-sig',newline='') as stream:rows=list(csv.DictReader(stream))
        self.assertEqual(rows[0]['fee'],'');self.assertEqual(rows[0]['fee_quality'],'UNKNOWN')
        self.assertEqual(rows[0]['currency'],'CNY');self.assertEqual(rows[0]['checkpoint_event_seq'],'3')
        self.assertEqual(rows[0]['accounting_basis'],'CONFIRMED_FILL_FIFO')
        self.assertEqual(rows[0]['boundary'],'DATABASE_COMMITTED')

    def test_csv_neutralizes_formula_strings_and_preserves_canonical_negative_money(self):
        report=self.seed();row=report['trades'][0]
        dangerous={'trade_key':'=1+1','contract_id':'+SUM(1,2)','fill_id':'-2+3','child_order_id':'@SUM(1)',
            'order_intent_id':'\t=1','position_cycle_id':'\r=2','time_basis':'  =3'}
        row.update(dangerous);row['realized_pnl']='-12.5'
        path=export_report(report,self.folder/'safe.csv')
        with path.open(encoding='utf-8-sig',newline='') as stream:exported=next(csv.DictReader(stream))
        for key,value in dangerous.items():self.assertEqual(exported[key],"'"+value)
        self.assertEqual(exported['realized_pnl'],'-12.5')

    def test_export_never_overwrites_an_existing_file(self):
        destination=self.folder/'existing.json';destination.write_text('original',encoding='utf-8')
        with self.assertRaises(TraderError) as error:export_report(self.seed(),destination)
        self.assertEqual(error.exception.code,'E_REPORT_EXISTS')
        self.assertEqual(destination.read_text(encoding='utf-8'),'original')

    def test_bad_formats_tables_schemas_and_destinations_do_not_leave_files(self):
        report=self.seed()
        invalids=[(report,self.folder/'bad.txt',{}),(report,self.folder/'bad.csv',{'table':'passwords'}),
            (report,self.folder/'bad.csv',{'table':[]}),(report,self.folder/'bad.csv',{'format':[]}),
            (report|{'schema_version':'other'},self.folder/'bad.json',{}),
            (report|{'trades':[{'price':{'password':'hidden'}}]},self.folder/'bad.csv',{}),
            (report,self.folder/'missing'/'bad.json',{})]
        for value,path,kwargs in invalids:
            with self.subTest(path=path,kwargs=kwargs),self.assertRaises(TraderError):export_report(value,path,**kwargs)
            self.assertFalse(path.exists())

    def test_json_exports_only_recursively_whitelisted_report_fields(self):
        report=self.seed();report['password']='DO_NOT_EXPORT'
        report['summary']['token']='DO_NOT_EXPORT';report['trades'][0]['webhook']='DO_NOT_EXPORT'
        report['nominal']=[{'contract_id':'DCE.p2701','trading_day':DAY,'daily_pnl_points':'10','secret':'DO_NOT_EXPORT'}]
        report['issues'].append({'code':'FEE_UNKNOWN','password':'DO_NOT_EXPORT'})
        path=export_report(report,self.folder/'all.json')
        text=path.read_text(encoding='utf-8');self.assertNotIn('DO_NOT_EXPORT',text)
        result=json.loads(text)
        self.assertEqual(result['schema_version'],'MONEY_REPORT/1');self.assertIsNone(result['trades'][0]['fee'])
        self.assertEqual(result['nominal'][0]['daily_pnl_points'],'10')

    def test_all_six_tables_export_with_fixed_columns_and_json_is_full_report(self):
        report=self.seed(entry_fee='0',exit_fee='0')
        for table in ('by_contract','by_day','cycles','trades','equity','nominal'):
            path=export_report(report,self.folder/(table+'.csv'),table=table)
            with path.open(encoding='utf-8-sig',newline='') as stream:header=next(csv.reader(stream))
            self.assertIn('checkpoint_event_seq',header);self.assertIn('currency',header)
        path=export_report(report,self.folder/'full.data',format='JSON')
        self.assertEqual(len(json.loads(path.read_text(encoding='utf-8'))['trades']),2)

    def test_journal_and_export_capacity_limits_fail_before_inventing_results(self):
        report=self.seed()
        for name,value in (('MAX_JOURNAL_EVENTS',2),('MAX_EVENT_BYTES',2),('MAX_JOURNAL_BYTES',2)):
            with self.subTest(name=name),patch('online_trader.reporting.'+name,value),self.assertRaises(TraderError) as error:
                read_report(self.path,self.key)
            self.assertEqual(error.exception.code,'E_REPORT_LIMIT')
        destination=self.folder/'bounded.csv'
        with patch('online_trader.reporting.MAX_EXPORT_BYTES',2),self.assertRaises(TraderError):export_report(report,destination)
        self.assertFalse(destination.exists())


if __name__=='__main__':unittest.main()
