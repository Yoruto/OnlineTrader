"""Explicit source review registers only atomic fees on closed native ledgers."""

import copy
import csv
from concurrent.futures import Future
from contextlib import closing
from dataclasses import replace
import hashlib
from importlib.resources import files
import json
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest.mock import patch

from online_trader.errors import TraderError
from online_trader.kernel import Kernel
from online_trader.ledger import Ledger
from online_trader.reconciliation_export import export_comparison
from online_trader.reporting import read_report
from online_trader.statement_workflow import preview_file,register_fees
from qa.recording_port import RecordingPort
import test_statistics_runtime as fixtures


HEADERS=('account_key','environment','trading_day','trade_key','contract_id','side','effect','quantity','price','fee')
PROTECTED=('fill_facts','fill_allocations','position_cycles','protection_plans','protection_state',
    'child_orders','order_intents','send_attempts','cancel_actions','exit_targets','reservations','day_blocks')


class StatementWorkflowTests(unittest.TestCase):
    def test_an_invalid_new_file_cannot_reuse_a_previously_registered_economic_identity(self):
        self.seed();original=self.preview();self.register(original)
        before=self.report()['checkpoint_event_seq']
        for row in self.rows:row['currency']='USD'
        self.write(self.rows,headers=(*HEADERS,'currency'))
        current=self.preview()
        self.assertNotEqual(current['statement_hash'],original['statement_hash'])
        self.assertTrue(current['blocking'])
        with self.assertRaises(TraderError) as error:self.register(original)
        self.assertEqual(error.exception.code,'E_PREVIEW_STALE')
        with self.assertRaises(TraderError) as error:self.register(current)
        self.assertEqual(error.exception.code,'E_RECONCILIATION_BLOCKED')
        self.assertEqual(self.report()['checkpoint_event_seq'],before)

    def setUp(self):
        self.env=fixtures.StatisticsRuntimeTests();self.env.setUp()
        self.folder=Path(self.env.temp.name);self.source=self.folder/'statement.csv'
        self.closed=False

    def tearDown(self):self.env.tearDown()

    def report(self):return read_report(self.env.path,self.env.key,as_of_utc='2026-11-09T01:06:02Z')

    def seed(self,*,entry_fee=None,exit_fee=None,estimated=False,stage='CLOSED',close_kernel=True):
        t=self.env;t.start();child=t.open(fill=0)
        options={} if entry_fee is None else ({'estimated_commission':entry_fee} if estimated else {'commission':entry_fee})
        t.kernel.record_fill(child,'statement-entry',3,'1055',observed_at_utc=t.last_event['source_time_utc'],**options)
        t.kernel.wait_idle()
        if stage!='HOLDING':
            t.observe('1035',at='2026-11-09T01:06:01Z')
            order=next(row for row in t.cycle()['orders'] if row['effect']=='REDUCE')
            options={} if exit_fee is None else ({'estimated_commission':exit_fee} if estimated else {'commission':exit_fee})
            t.kernel.record_fill(order['child_order_id'],'statement-exit',3,'1034',
                observed_at_utc=t.last_event['source_time_utc'],**options)
            t.kernel.wait_idle()
            if stage=='CLOSED':t.close()
        self.rows=[{'account_key':t.key,'environment':'ONLINE_SIM','trading_day':t.day,'trade_key':'statement-entry',
            'contract_id':fixtures.CONTRACT,'side':'BUY','effect':'OPEN','quantity':'3','price':'1055','fee':'3'}]
        if stage!='HOLDING':self.rows.append(dict(self.rows[0],trade_key='statement-exit',side='SELL',effect='REDUCE',price='1034',fee='6'))
        self.write(self.rows)
        if close_kernel:t.kernel.close();self.closed=True

    def write(self,rows,*,path=None,encoding='utf-8-sig',headers=HEADERS):
        path=path or self.source
        with path.open('w',encoding=encoding,newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=headers);writer.writeheader();writer.writerows(rows)
        return path

    def preview(self,**changes):
        args={'statement_path':self.source,'ledger_path':self.env.path,'account_key':self.env.key,
              'environment':'ONLINE_SIM','trading_day':'2026-11-09'};args.update(changes)
        return preview_file(**args)

    def register(self,preview=None,**changes):
        material=preview or self.preview()
        args={'statement_path':self.source,'ledger_path':self.env.path,'account_key':self.env.key,
              'environment':'ONLINE_SIM','trading_day':'2026-11-09','expected_preview_hash':material['preview_hash']}
        args.update(changes);return register_fees(**args)

    def query(self,sql,params=()):
        with closing(sqlite3.connect(self.env.path)) as conn:
            conn.row_factory=sqlite3.Row
            return [dict(row) for row in conn.execute(sql,params)]

    def snapshot(self,tables=None):
        with closing(sqlite3.connect(self.env.path)) as conn:
            tables=tables or [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            return {name:conn.execute('SELECT * FROM '+name+' ORDER BY rowid').fetchall() for name in tables}

    def test_preview_is_read_only_preserves_database_bytes_and_never_creates_a_boot(self):
        self.seed();before=self.env.path.read_bytes();tables=self.snapshot()
        preview=self.preview()
        self.assertFalse(preview['blocking']);self.assertEqual(len(preview['actions']),2)
        self.assertEqual(preview['summary']['total_fee_amount'],'9')
        self.assertEqual(self.env.path.read_bytes(),before);self.assertEqual(self.snapshot(),tables)

    def test_registers_two_fees_in_one_transaction_without_changing_original_trading_facts(self):
        self.seed();before=self.snapshot(PROTECTED);events=self.query('SELECT COUNT(*) AS n FROM event_journal')[0]['n']
        result=self.register();report=self.report()
        self.assertEqual(result['status'],'REGISTERED');self.assertEqual(result['registered_count'],2)
        self.assertFalse(result['trading_authority']);self.assertEqual(self.snapshot(PROTECTED),before)
        self.assertEqual(self.query('SELECT COUNT(*) AS n FROM event_journal')[0]['n'],events+1)
        self.assertEqual(report['summary']['gross_realized_pnl'],'-630')
        self.assertEqual(report['summary']['fees'],'9');self.assertEqual(report['summary']['net_realized_pnl'],'-639')
        fees=self.query('SELECT source_kind,quality,amount,recorded_event_seq FROM fee_observations')
        self.assertEqual(len(fees),2);self.assertEqual({row['source_kind'] for row in fees},{'STATEMENT'})
        self.assertEqual(len({row['recorded_event_seq'] for row in fees}),1)
        self.assertEqual(self.query('SELECT new_open_status,strategy_epoch FROM authority')[0],{'new_open_status':'DISARMED','strategy_epoch':None})

    def test_explicit_zero_fees_are_confirmed_and_do_not_mean_missing(self):
        self.seed();self.write([dict(row,fee='0') for row in self.rows]);self.register()
        summary=self.report()['summary']
        self.assertEqual(summary['fees'],'0');self.assertEqual(summary['net_realized_pnl'],'-630')
        self.assertTrue(all(row['fee_quality']=='CONFIRMED' for row in self.report()['trades']))

    def test_confirmed_statement_replaces_estimates_by_revision_without_double_charge(self):
        self.seed(entry_fee='1',exit_fee='2',estimated=True)
        preview=self.preview()
        self.assertEqual({row['status'] for row in preview['rows']},{'MATCHED_ESTIMATED_REPLACEMENT'})
        self.assertEqual({row['revision'] for row in preview['actions']},{2})
        self.register(preview);report=self.report()
        self.assertEqual(report['summary']['estimated_fees'],'0');self.assertEqual(report['summary']['fees'],'9')
        self.assertEqual({row['fee_revision'] for row in report['trades']},{2})

    def test_same_file_retry_returns_original_receipt_without_a_second_fee_or_boot(self):
        self.seed();preview=self.preview();first=self.register(preview);before=self.snapshot()
        again=self.register(preview)
        self.assertEqual(again['status'],'ALREADY_REGISTERED')
        self.assertEqual(again['receipt']['import_id'],first['receipt']['import_id']);self.assertEqual(self.snapshot(),before)
        self.assertEqual(len(self.report()['fee_imports']),1)

    def test_renamed_and_reencoded_same_statement_do_not_charge_again(self):
        self.seed();original=self.preview();self.register(original);before=self.snapshot()
        renamed=self.folder/'renamed.csv';shutil.copyfile(self.source,renamed)
        self.assertEqual(self.register(original,statement_path=renamed)['status'],'ALREADY_REGISTERED')
        aliases={'account_key':'资金账号','environment':'环境','trading_day':'交易日期','trade_key':'成交编号',
                 'contract_id':'合约','side':'买卖','effect':'开平','quantity':'成交量','price':'成交价','fee':'手续费'}
        reencoded=self.folder/'gbk.csv'
        rows=[{aliases[key]:value for key,value in row.items()} for row in self.rows]
        self.write(rows,path=reencoded,encoding='gb18030',headers=tuple(aliases.values()))
        changed=self.preview(statement_path=reencoded)
        self.assertEqual(changed['statement_hash'],original['statement_hash']);self.assertNotEqual(changed['source_sha256'],original['source_sha256'])
        self.assertEqual(self.register(original,statement_path=reencoded)['status'],'ALREADY_REGISTERED')
        self.assertEqual(self.snapshot(),before)

    def test_account_environment_and_source_day_mismatch_block_the_entire_batch(self):
        self.seed();before=self.snapshot()
        for changed in ({'account_key':'DEV:other'},{'environment':'LIVE'},{'trading_day':'2026-11-10'}):
            with self.subTest(changed=changed):
                self.write([self.rows[0],dict(self.rows[1],**changed)])
                material=self.preview();self.assertTrue(material['blocking'])
                with self.assertRaises(TraderError):self.register(material)
                self.assertEqual(self.snapshot(),before)

    def test_modified_source_file_invalidates_the_specific_review_hash(self):
        self.seed();preview=self.preview();before=self.snapshot()
        self.write([self.rows[0],dict(self.rows[1],fee='7')])
        with self.assertRaises(TraderError) as error:self.register(preview)
        self.assertEqual(error.exception.code,'E_PREVIEW_STALE');self.assertEqual(self.snapshot(),before)

    def test_wrong_or_absent_review_token_does_not_create_a_maintenance_boot(self):
        self.seed();before=self.snapshot()
        for token in ('0'*64,'',None):
            with self.subTest(token=token),self.assertRaises(TraderError):self.register(expected_preview_hash=token)
        self.assertEqual(self.snapshot(),before)

    def test_fee_or_checkpoint_change_after_review_requires_a_fresh_preview(self):
        self.seed();preview=self.preview()
        t=self.env;t.port=RecordingPort();t.kernel=t.new_kernel();self.closed=False
        t.kernel.money.record_fee('statement-entry','1',evidence_id='later-confirmed-fee');t.kernel.wait_idle()
        t.kernel.close();self.closed=True;before=self.snapshot()
        with self.assertRaises(TraderError) as error:self.register(preview)
        self.assertEqual(error.exception.code,'E_PREVIEW_STALE');self.assertEqual(self.snapshot(),before)

    def test_confirmed_fee_conflict_blocks_unrelated_unknown_fees_too(self):
        self.seed(entry_fee='2');preview=self.preview();before=self.snapshot()
        self.assertTrue(preview['blocking']);self.assertEqual(len(preview['actions']),1)
        self.assertIn('FEE_CONFLICT',{row['status'] for row in preview['rows']})
        with self.assertRaises(TraderError):self.register(preview)
        self.assertEqual(self.snapshot(),before)

    def test_both_sided_missing_rows_remain_visible_and_only_exact_owned_fee_is_recorded(self):
        self.seed();external=dict(self.rows[0],trade_key='foreign-trade',contract_id='SHFE.rb2701',price='3200',fee='99')
        self.write([self.rows[0],external]);preview=self.preview()
        self.assertEqual(preview['summary']['external_only_rows'],1);self.assertEqual(preview['summary']['local_only_rows'],1)
        self.assertFalse(preview['blocking']);self.assertEqual(len(preview['actions']),1)
        self.register(preview);report=self.report()
        self.assertEqual(self.query('SELECT COUNT(*) AS n FROM fill_facts')[0]['n'],2)
        self.assertEqual(self.query('SELECT COUNT(*) AS n FROM fee_observations')[0]['n'],1)
        self.assertIsNone(report['summary']['net_realized_pnl'])
        self.assertEqual(report['summary']['confirmed_fees'],'3')

    def test_active_kernel_account_lock_refuses_registration_without_any_ledger_change(self):
        self.seed(close_kernel=False);preview=self.preview();before=self.snapshot()
        with self.assertRaises(TraderError) as error:self.register(preview)
        self.assertEqual(error.exception.code,'E_INSTANCE_EXISTS');self.assertEqual(self.snapshot(),before)
        self.assertEqual(len(self.env.port.orders),2)

    def test_owned_holding_cycle_refuses_offline_registration(self):
        self.seed(stage='HOLDING');preview=self.preview();before=self.snapshot()
        with self.assertRaises(TraderError) as error:self.register(preview)
        self.assertEqual(error.exception.code,'E_NOT_READY');self.assertEqual(self.snapshot(),before)

    def test_flat_but_unclosed_cycle_refuses_registration(self):
        self.seed(stage='FLAT');preview=self.preview();before=self.snapshot()
        self.assertEqual(self.query('SELECT remaining_qty,lifecycle FROM position_cycles')[0],
                         {'remaining_qty':0,'lifecycle':'FLAT_PENDING_CONFIRM'})
        with self.assertRaises(TraderError) as error:self.register(preview)
        self.assertEqual(error.exception.code,'E_NOT_READY');self.assertEqual(self.snapshot(),before)

    def test_zero_owned_but_unknown_or_active_orders_refuse_the_entire_batch(self):
        self.seed(close_kernel=False);t=self.env
        # Another native opening has no confirmed quantity; existing statement
        # rows still prove known closed fills and must not bypass active risk.
        t.kernel.arm_development();t.kernel.wait_idle();t.minute+=1
        report=t.observe('1055',at='2026-11-09T01:07:00Z')
        self.assertIsNotNone(report['child_order_id'])
        t.kernel.close();self.closed=True;material=self.preview();before=self.snapshot()
        with self.assertRaises(TraderError) as error:self.register(material)
        self.assertEqual(error.exception.code,'E_NOT_READY');self.assertEqual(self.snapshot(),before)

    def test_zero_owned_unknown_send_cannot_be_bypassed_by_known_closed_fee_rows(self):
        self.seed(close_kernel=False);t=self.env;t.port.fail_send=True
        t.kernel.arm_development();t.kernel.wait_idle();t.minute+=1
        result=t.observe('1055',at='2026-11-09T01:07:00Z')
        self.assertIsNotNone(result['child_order_id'])
        self.assertTrue(any(order['send_state']=='UNKNOWN' for cycle in t.kernel._committed.values() for order in cycle['orders']))
        self.assertTrue(all(cycle['protection']['owned_qty']==0 for cycle in t.kernel._committed.values()))
        t.kernel.close();self.closed=True;preview=self.preview();before=self.snapshot()
        with self.assertRaises(TraderError) as error:self.register(preview)
        self.assertEqual(error.exception.code,'E_NOT_READY');self.assertEqual(self.snapshot(),before)

    def test_missing_inputs_and_display_json_never_create_a_ledger_or_register_fees(self):
        self.seed();before=self.snapshot();material=self.preview()
        missing=self.folder/'missing'/'ledger.db'
        with self.assertRaises(TraderError):self.register(material,ledger_path=missing)
        self.assertFalse(missing.exists());self.assertFalse(missing.with_name('ledger.db.writer.lock').exists())
        with self.assertRaises(TraderError):self.register(material,statement_path=self.folder/'missing.csv')
        display=self.folder/'display.json';export_comparison(material,display)
        with self.assertRaises(TraderError):self.register(material,statement_path=display)
        self.assertEqual(self.snapshot(),before)

    def test_mid_batch_sql_failure_rolls_back_fees_receipt_boot_and_authority_atomically(self):
        self.seed();preview=self.preview();before=self.snapshot();original=Ledger._commit_statement_fees
        def collide(instance,conn,payload,*args):
            payload=copy.deepcopy(payload);payload['money_fees'][1]['evidence_id']=payload['money_fees'][0]['evidence_id']
            return original(instance,conn,payload,*args)
        with patch.object(Ledger,'_commit_statement_fees',new=collide):
            with self.assertRaises(TraderError) as error:self.register(preview)
        self.assertEqual(error.exception.code,'E_STORAGE')
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.register(preview)['status'],'REGISTERED')

    def test_wrong_commit_ack_is_uncertain_then_retry_recovers_the_original_import(self):
        for index,field in enumerate(('kernel_boot_id','state_revision','durable_seq')):
            with self.subTest(field=field):
                if index:self.env.tearDown();self.setUp()
                self.seed();preview=self.preview();original=Ledger.commit_statement_fees
                def wrong(instance,*args,**kwargs):
                    actual=original(instance,*args,**kwargs).result(timeout=5)
                    value='foreign-maintenance-boot' if field=='kernel_boot_id' else getattr(actual,field)+1
                    result=Future();result.set_result(replace(actual,**{field:value}));return result
                with patch.object(Ledger,'commit_statement_fees',new=wrong):
                    with self.assertRaises(TraderError) as error:self.register(preview)
                self.assertEqual(error.exception.code,'E_COMMIT_ACK');before=self.snapshot()
                self.assertEqual(self.register(preview)['status'],'ALREADY_REGISTERED');self.assertEqual(self.snapshot(),before)
                self.assertEqual(len(self.report()['fee_imports']),1)

    def test_restart_restores_receipt_fee_revisions_and_never_replays_orders(self):
        self.seed();receipt=self.register()['receipt'];t=self.env;t.port=RecordingPort();t.kernel=t.new_kernel()
        report=t.kernel.money.report(as_of_utc='2026-11-09T01:06:02Z')
        self.assertIsNone(t.kernel.money.error);self.assertEqual(report['summary']['net_realized_pnl'],'-639')
        self.assertEqual(report['fee_imports'][0]['import_id'],receipt['import_id'])
        self.assertEqual({row['fee_revision'] for row in report['trades']},{1})
        self.assertFalse(t.kernel._armed);self.assertEqual(t.port.orders,[])

    def test_live_scope_and_missing_existing_schema_cannot_be_opened_as_a_new_product(self):
        self.seed();before=self.snapshot()
        with self.assertRaises(TraderError) as error:self.register(environment='LIVE')
        self.assertEqual(error.exception.code,'E_SCOPE');self.assertEqual(self.snapshot(),before)
        empty=self.folder/'empty.db'
        with closing(sqlite3.connect(empty)) as conn:conn.execute('PRAGMA user_version=0')
        with self.assertRaises(TraderError):
            with Ledger(empty,self.env.key,existing_only=True):pass
        with closing(sqlite3.connect(empty)) as conn:
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),[])
        missing_scope=self.folder/'schema-without-scope.db'
        with closing(sqlite3.connect(missing_scope)) as conn:
            conn.executescript(files('online_trader').joinpath('resources/schema.sql').read_text(encoding='utf-8'))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],37)
        with self.assertRaises(TraderError) as error:
            with Ledger(missing_scope,self.env.key,existing_only=True):pass
        self.assertEqual(error.exception.code,'E_SCOPE')
        with closing(sqlite3.connect(missing_scope)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ledger_scope').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM kernel_boots').fetchone()[0],0)


if __name__=='__main__':unittest.main()
