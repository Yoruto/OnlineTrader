"""Financial projections follow accepted facts without granting trading rights."""

import copy
from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import unittest

from online_trader.accounting.book import ACCOUNT_FIELDS
from online_trader.contracts.codec import canonical_bytes
from online_trader.errors import TraderError
from online_trader.kernel import Kernel
from online_trader.kernel.projection import projection_batch
from qa.recording_port import RecordingPort
import test_statistics_runtime as fixtures


class MoneyRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.env=fixtures.StatisticsRuntimeTests();self.env.setUp()

    def tearDown(self):self.env.tearDown()

    def report(self):
        t=self.env
        return t.kernel.money.report(as_of_utc=(t.last_event or {}).get('source_time_utc'))

    def stop(self,*,fee=None):
        t=self.env;t.start();child=t.open(fill=0)
        t.kernel.record_fill(child,'financial-open',3,'1055',commission=fee,
            observed_at_utc=t.last_event['source_time_utc']);t.kernel.wait_idle()
        t.exit_fill('1035',actual_price='1034')
        return next(key for key,fill in t.kernel.money.book.fills.items() if fill['effect']=='REDUCE')

    def account(self,**changes):
        t=self.env
        fact={key:None for key in ACCOUNT_FIELDS}
        fact.update(account_key=t.key,trading_day='2026-11-09',observed_at_utc='2026-11-09T01:06:00Z',
            source_epoch=1,revision=1,evidence_id='money-account-proof',currency='CNY',complete=True,
            balance='100100',pre_balance='100000',available='80000',margin='10000',commission='10',
            net_cash_flow='50')
        fact.update(changes);return fact

    def test_actual_fill_prices_quantities_and_multiplier_are_separate_from_nominal_points(self):
        t=self.env;exit_key=self.stop(fee='3')
        t.kernel.money.record_fee(exit_key,'6',evidence_id='confirmed-exit-fee');t.kernel.wait_idle();t.close()
        report=self.report();summary=report['summary']
        self.assertEqual(summary['gross_realized_pnl'],'-630')
        self.assertEqual(summary['fees'],'9');self.assertEqual(summary['net_realized_pnl'],'-639')
        self.assertEqual(t.field('daily_pnl_points'),'-20');self.assertEqual(t.field('net_loss_points'),'16')
        self.assertEqual(report['account']['balance'],'100000')
        self.assertIsNone(report['account']['today_pnl'])
        self.assertEqual(summary['entry_quantity'],3);self.assertEqual(summary['exit_quantity'],3)
        canonical_bytes(report)

    def test_missing_fees_never_become_a_confirmed_zero_net_profit(self):
        self.stop();summary=self.report()['summary']
        self.assertEqual(summary['gross_realized_pnl'],'-630')
        self.assertIsNone(summary['fees']);self.assertIsNone(summary['net_realized_pnl'])

    def test_explicit_zero_is_a_known_fee_and_opening_fee_is_not_lost(self):
        t=self.env;t.start();child=t.open(fill=0)
        t.kernel.record_fill(child,'zero-fee-entry',3,'1055',commission='0');t.kernel.wait_idle()
        summary=self.report()['summary']
        self.assertEqual(summary['fees'],'0');self.assertEqual(summary['net_realized_pnl'],'0')

    def test_estimates_are_replaced_by_confirmed_fees_without_double_deduction(self):
        t=self.env;exit_key=self.stop()
        for key,amount in (('financial-open','2'),(exit_key,'3')):
            t.kernel.money.record_fee(key,amount,evidence_id='estimate-'+key,quality='ESTIMATED');t.kernel.wait_idle()
        summary=self.report()['summary']
        self.assertEqual(summary['estimated_net_realized_pnl'],'-635');self.assertIsNone(summary['net_realized_pnl'])
        for key,amount in (('financial-open','0'),(exit_key,'4')):
            t.kernel.money.record_fee(key,amount,evidence_id='confirmed-'+key,revision=2);t.kernel.wait_idle()
        summary=self.report()['summary']
        self.assertEqual(summary['net_realized_pnl'],'-634');self.assertEqual(summary['estimated_fees'],'0')

    def test_fee_evidence_scope_conflict_and_downgrade_are_rejected(self):
        t=self.env;t.start();child=t.open(fill=0)
        t.kernel.record_fill(child,'financial-open',3,'1055');t.kernel.wait_idle()
        with self.assertRaises(TraderError):t.kernel.money.record_fee('not-owned','1',evidence_id='foreign-fee')
        t.kernel.money.record_fee('financial-open','0',evidence_id='fee-proof');t.kernel.wait_idle()
        before=self.report()['summary']
        self.assertEqual(t.kernel.money.record_fee('financial-open','0',evidence_id='fee-proof').result()['status'],'DUPLICATE_FEE')
        with self.assertRaises(TraderError):t.kernel.money.record_fee('financial-open','2',evidence_id='fee-proof')
        with self.assertRaises(TraderError):t.kernel.money.record_fee('financial-open','2',evidence_id='downgrade',revision=2,quality='ESTIMATED')
        self.assertEqual(self.report()['summary'],before)

    def test_fee_changes_publish_only_after_matching_owner_commit_ack(self):
        t=self.env;exit_key=self.stop();submit=t.kernel.ledger.submit;held=[]
        def hold(kind,name,data,mutate=None,**kwargs):
            actual=submit(kind,name,data,mutate,**kwargs)
            if data.get('money_fee'):
                actual.result(timeout=3);visible=Future();held.append((visible,actual.result()));return visible
            return actual
        with patch.object(t.kernel.ledger,'submit',side_effect=hold):
            t.kernel.money.record_fee(exit_key,'5',evidence_id='held-fee')
        self.assertIsNone(t.kernel.money.book.fees.get(exit_key))
        held[0][0].set_result(held[0][1]);t.kernel.wait_idle()
        self.assertEqual(t.kernel.money.book.fees[exit_key]['amount'],'5')

    def test_wrong_fee_ack_failstops_without_publishing_new_financial_facts(self):
        t=self.env;exit_key=self.stop();submit=t.kernel.ledger.submit
        def wrong(kind,name,data,mutate=None,**kwargs):
            actual=submit(kind,name,data,mutate,**kwargs)
            if data.get('money_fee'):
                visible=Future();visible.set_result(replace(actual.result(timeout=3),kernel_boot_id='other-boot'));return visible
            return actual
        with patch.object(t.kernel.ledger,'submit',side_effect=wrong):
            t.kernel.money.record_fee(exit_key,'5',evidence_id='wrong-fee')
            with self.assertRaises(TraderError):t.kernel.wait_idle()
        self.assertTrue(t.kernel._failed);self.assertNotIn(exit_key,t.kernel.money.book.fees)

    def test_fill_and_its_money_fee_roll_back_together(self):
        t=self.env;t.start();child=t.open(fill=0);submit=t.kernel.ledger.submit
        def fail(kind,name,data,mutate=None,**kwargs):
            if data.get('fill'):
                original=mutate
                def mutation(conn,seq):
                    original(conn,seq);conn.execute('INSERT INTO financial_missing_table VALUES (1)')
                mutate=mutation
            return submit(kind,name,data,mutate,**kwargs)
        with patch.object(t.kernel.ledger,'submit',side_effect=fail):
            t.kernel.record_fill(child,'rolled-back-money-fill',3,'1055',commission='2')
            with self.assertRaises(TraderError):t.kernel.wait_idle()
        self.assertNotIn('rolled-back-money-fill',t.kernel.money.book.fills)
        self.assertEqual(t.kernel.ledger.query("SELECT trade_key FROM fill_facts WHERE trade_key='rolled-back-money-fill'"),[])

    def test_strategy_checkpoint_binds_account_history_without_copying_rows(self):
        owner=self.env.kernel.money
        initial=owner.business_state()
        for index in range(1,257):
            fact=self.account(evidence_id=f'checkpoint-account-{index}',revision=index)
            owner.accept({'money_account':fact},index,fact['observed_at_utc'])
        checkpoint=owner.business_state()
        self.assertIsNone(owner.error)
        self.assertEqual(checkpoint['account_fact_count'],256)
        self.assertNotEqual(checkpoint['account_facts_hash'],initial['account_facts_hash'])
        self.assertNotEqual(checkpoint['account_source_boots_hash'],initial['account_source_boots_hash'])
        self.assertLess(len(canonical_bytes(checkpoint)),1024)
        self.assertEqual(len(owner.book.account_facts),256)

    def test_account_pnl_uses_source_equity_and_keeps_cash_flow_adjustment_separate(self):
        t=self.env;t.kernel.money.record_account(self.account());t.kernel.wait_idle()
        row=self.report()['account']
        self.assertEqual(row['today_pnl'],'100');self.assertEqual(row['trading_pnl'],'50')
        self.assertEqual(self.report()['summary']['gross_realized_pnl'],'0')
        self.assertFalse(t.kernel._armed);self.assertEqual(t.port.orders,[])

    def test_fresh_known_account_projection_and_incomplete_source_quality(self):
        t=self.env;now=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
        t.kernel.money.record_account(self.account(observed_at_utc=now,pre_balance=None));t.kernel.wait_idle()
        view=projection_batch(t.kernel.snapshot())['records'][0]
        self.assertEqual(view['funds_quality'],'KNOWN');self.assertEqual(view['equity'],'100100')
        t.kernel.money.record_account(self.account(revision=2,evidence_id='incomplete-source',observed_at_utc=now,complete=False));t.kernel.wait_idle()
        self.assertEqual(projection_batch(t.kernel.snapshot())['records'][0]['funds_quality'],'UNKNOWN')
        self.assertIsNone(self.report()['account']['today_pnl'])

    def test_source_identity_account_currency_and_time_cannot_be_silently_changed(self):
        t=self.env;t.kernel.money.record_account(self.account());t.kernel.wait_idle()
        for changed in ({'account_key':'another-account'},{'currency':'USD'},{'balance':'123'},
                        {'observed_at_utc':'2026-11-09T01:00:00','revision':2,'evidence_id':'bad-time'}):
            with self.subTest(changed=changed),self.assertRaises(TraderError):t.kernel.money.record_account(self.account(**changed))
        self.assertEqual(len(t.kernel.money.book.account_facts),1)

    def test_current_valuation_expires_without_order_or_fee_events_refreshing_its_price(self):
        t=self.env;t.start();child=t.open(fill=0)
        t.kernel.record_fill(child,'valued-entry',3,'1055',commission='0');t.kernel.wait_idle()
        t.observe('1065',at='2026-11-09T01:06:01Z')
        self.assertEqual(self.report()['summary']['unrealized_pnl'],'300')
        saved=copy.deepcopy(t.cycle()['money_quote'])
        t.kernel.money.record_fee('valued-entry','1',evidence_id='fee-update',revision=2);t.kernel.wait_idle()
        self.assertEqual(t.cycle()['money_quote'],saved)
        expired=t.kernel.money.report(as_of_utc='2026-11-09T01:06:31.001Z')
        self.assertIsNone(expired['summary']['unrealized_pnl'])

    def test_cross_day_close_uses_explicit_actual_fill_day_without_relabelling_original_plan(self):
        t=self.env;t.start();child=t.open(fill=0)
        t.kernel.record_fill(child,'cross-day-entry',3,'1055',commission='0');t.kernel.wait_idle()
        t.observe('1035',at='2026-11-09T01:06:01Z')
        order=next(o for o in t.cycle()['orders'] if o['effect']=='REDUCE')
        t.kernel.record_fill(order['child_order_id'],'cross-day-exit',3,'1034',commission='0',trading_day='2026-11-10',
            observed_at_utc='2026-11-10T01:00:00Z');t.kernel.wait_idle()
        report=t.kernel.money.report(as_of_utc='2026-11-10T01:00:00Z')
        days={row['trading_day']:row for row in report['by_day']}
        self.assertEqual(days['2026-11-10']['gross_realized_pnl'],'-630')
        self.assertEqual(t.cycle()['plan']['trading_day'],'2026-11-09')
        self.assertEqual(t.kernel.ledger.query("SELECT trading_day FROM fill_facts WHERE trade_key='cross-day-exit'")[0]['trading_day'],'2026-11-10')

    def test_financial_projection_failure_preserves_trade_and_protection_exit(self):
        t=self.env;t.start();child=t.open(fill=0)
        with patch.object(t.kernel.money.book,'accept',side_effect=TraderError('E_MONEY_INPUT','Derived failure')):
            t.kernel.record_fill(child,'financial-view-failure',3,'1055');t.kernel.wait_idle()
        self.assertFalse(t.kernel._failed);self.assertEqual(t.cycle()['protection']['owned_qty'],3)
        self.assertEqual(t.kernel.ledger.query("SELECT quantity FROM fill_facts WHERE trade_key='financial-view-failure'"),[{'quantity':3}])
        with self.assertRaises(TraderError):self.report()
        t.exit_fill('1035',actual_price='1034')
        self.assertEqual(t.cycle()['protection']['owned_qty'],0)
        self.assertTrue(any(o['effect']=='REDUCE' for o in t.port.orders))

    def test_restart_reuses_frozen_multiplier_and_fee_evidence(self):
        t=self.env;exit_key=self.stop(fee='0')
        t.kernel.money.record_fee(exit_key,'4',evidence_id='restart-fee');t.kernel.wait_idle()
        before=self.report()['summary'];t.kernel.close();t.port=RecordingPort()
        t.kernel=Kernel(t.path,t.key,development=True,port=t.port,risk_budget='1000000',multiplier='100')
        self.assertEqual(self.report()['summary'],before)
        self.assertEqual(t.kernel.money.record_fee(exit_key,'4',evidence_id='restart-fee').result()['status'],'DUPLICATE_FEE')
        self.assertEqual(t.port.orders,[]);self.assertFalse(t.kernel._armed)

    def test_default_kernel_rejects_financial_injection_and_never_creates_trading_authority(self):
        t=self.env;t.kernel.close();t.kernel=Kernel(t.path,t.key)
        with self.assertRaises(TraderError):t.kernel.money.record_account(self.account())
        self.assertFalse(t.kernel._armed);self.assertEqual(t.kernel.money.report()['equity'],[])

    def test_quick_restart_does_not_restore_old_boot_funds_as_current_known(self):
        t=self.env;now=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
        t.kernel.money.record_account(self.account(observed_at_utc=now));t.kernel.wait_idle()
        self.assertEqual(t.kernel.money.account_projection()['funds_quality'],'KNOWN')
        old_boot=t.kernel.boot_id;t.kernel.close();t.port=RecordingPort();t.kernel=t.new_kernel()
        self.assertNotEqual(t.kernel.boot_id,old_boot)
        self.assertEqual(t.kernel.money.account_projection()['funds_quality'],'UNKNOWN')
        self.assertEqual(t.kernel.money.book.account_facts[-1]['balance'],'100100')
        self.assertEqual(t.kernel.money._account_boots['money-account-proof'],old_boot)
        # A duplicate original receipt cannot renew the old source's boot.
        result=t.kernel.money.record_account(self.account(observed_at_utc=now))
        self.assertEqual(result.result()['status'],'DUPLICATE_ACCOUNT_MONEY')
        self.assertEqual(t.kernel.money.account_projection()['funds_quality'],'UNKNOWN')

    def test_restart_quote_is_unknown_for_live_owner_but_historical_valuation_remains_available(self):
        t=self.env;t.start();child=t.open(fill=0)
        t.kernel.record_fill(child,'boot-valued-entry',3,'1055',commission='0');t.kernel.wait_idle()
        t.observe('1065',at='2026-11-09T01:06:01Z')
        self.assertEqual(self.report()['summary']['unrealized_pnl'],'300')
        t.kernel.close();t.port=RecordingPort();t.kernel=t.new_kernel()
        live=t.kernel.money.report(as_of_utc='2026-11-09T01:06:01Z')
        history=t.kernel.money.book.report(checkpoint_event_seq=t.kernel._durable_seq,
            as_of_utc='2026-11-09T01:06:01Z')
        self.assertIsNone(live['summary']['unrealized_pnl'])
        self.assertEqual(history['summary']['unrealized_pnl'],'300')
        self.assertFalse(t.kernel._armed);self.assertEqual(t.port.orders,[])

    def test_bad_financial_extension_fields_do_not_interrupt_strategy_market_processing(self):
        t=self.env;t.start();t.kernel.arm_development();t.kernel.wait_idle()
        event,account,instrument=fixtures.facts(t.key)
        account.update(commission='invalid-secret-commission',pre_balance='invalid-secret-baseline')
        future=t.runtime.on_market(event,account,instrument);t.kernel.wait_idle()
        self.assertEqual(future.result()['status'],'ENTRY_COMMITTED')
        self.assertEqual(len(t.port.orders),1)
        report=t.kernel.money.report(as_of_utc=event['source_time_utc'])
        self.assertIsNone(t.kernel.money.error)
        self.assertEqual(report['account']['balance'],'100000')
        self.assertFalse(report['account']['complete'])
        self.assertEqual(report['account']['quality'],'INCOMPLETE')
        self.assertIsNone(report['account']['commission']);self.assertIsNone(report['account']['today_pnl'])
        self.assertEqual({row['field'] for row in report['issues'] if row['code']=='ACCOUNT_MONEY_FIELD_INVALID'},
                         {'commission','pre_balance'})
        raw=canonical_bytes(report)
        self.assertNotIn(b'invalid-secret-commission',raw);self.assertNotIn(b'invalid-secret-baseline',raw)

    def test_financial_extension_bad_value_keeps_existing_source_revision_conflict_checks(self):
        t=self.env;t.start();t.kernel.arm_development();t.kernel.wait_idle()
        event,account,instrument=fixtures.facts(t.key)
        future=t.runtime.on_market(event,account,instrument);t.kernel.wait_idle();future.result()
        before=copy.deepcopy(t.kernel.money.book.account_facts)
        with self.assertRaises(TraderError) as error:
            t.runtime.update_facts(event,account|{'commission':'invalid-current-revision'},instrument)
        self.assertEqual(error.exception.code,'E_DUPLICATE_CONFLICT')
        self.assertEqual(t.kernel.money.book.account_facts,before)
        self.assertIsNone(t.kernel.money.error)
        later=event|{'sequence':2,'source_time_utc':'2026-11-09T01:06:00Z'}
        observed=t.runtime.on_market(later,account|{'revision':2,'commission':'0','pre_balance':'100000'},instrument)
        t.kernel.wait_idle();observed.result()
        row=t.kernel.money.report(as_of_utc=later['source_time_utc'])['account']
        self.assertTrue(row['complete']);self.assertEqual(row['today_pnl'],'0')

    def test_bad_optional_fill_fees_preserve_confirmed_quantities_and_protection_exits(self):
        t=self.env;t.start();child=t.open(fill=0)
        options=({'commission':0.1},{'commission':'-1'},
                 {'commission':'1','estimated_commission':'2'})
        for index,arguments in enumerate(options):
            t.kernel.record_fill(child,'invalid-optional-fee-'+str(index),1,'1055',
                observed_at_utc=t.last_event['source_time_utc'],**arguments)
            t.kernel.wait_idle()
        self.assertEqual(t.cycle()['protection']['owned_qty'],3)
        self.assertEqual(t.kernel.ledger.query("SELECT COUNT(*) AS n FROM fill_facts WHERE trade_key LIKE 'invalid-optional-fee-%'"),[{'n':3}])
        report=self.report()
        self.assertEqual(report['summary']['entry_quantity'],3)
        self.assertIsNone(report['summary']['fees'])
        self.assertEqual(sum(row['code']=='FEE_INPUT_INVALID' for row in report['issues']),3)
        self.assertIsNone(t.kernel.money.error);self.assertFalse(t.kernel._failed)
        t.exit_fill('1035',actual_price='1034')
        self.assertEqual(t.cycle()['protection']['owned_qty'],0)
        self.assertTrue(any(order['effect']=='REDUCE' for order in t.port.orders))

    def test_source_expiry_publishes_a_new_committed_projection_revision(self):
        t=self.env;now=datetime.now(timezone.utc)
        t.kernel.money.record_account(self.account(observed_at_utc=now.isoformat().replace('+00:00','Z')))
        t.kernel.wait_idle();before=t.kernel._durable_seq
        self.assertEqual(projection_batch(t.kernel.snapshot())['records'][0]['funds_quality'],'KNOWN')
        with patch('online_trader.kernel.money.datetime',wraps=datetime) as clock:
            clock.now.return_value=now+timedelta(seconds=31)
            t.kernel.poll();t.kernel.wait_idle()
            after=projection_batch(t.kernel.snapshot())['records'][0]
        self.assertEqual(after['funds_quality'],'UNKNOWN')
        self.assertEqual(t.kernel._durable_seq,before+1)
        self.assertEqual(after['record_revision'],str(before+1))
        self.assertEqual(t.port.orders,[])

    def test_committed_clock_barrier_invalidates_original_valuation_until_a_new_quote(self):
        t=self.env;t.start();child=t.open(fill=0)
        t.kernel.record_fill(child,'clock-report-entry',3,'1055',commission='0',observed_at_utc=t.last_event['source_time_utc'])
        t.kernel.wait_idle();t.observe('1065',at='2026-11-09T01:06:01Z')
        self.assertEqual(self.report()['summary']['unrealized_pnl'],'300')
        t.kernel._submit('TX_OBSERVATION','ANONYMOUS_CLOCK_FAULT_EVIDENCE',{'reasons':['CLOCK_WALL_REGRESSION']},lambda conn,seq:None)
        t.kernel.wait_idle()
        self.assertIsNone(self.report()['summary']['unrealized_pnl'])
        t.observe('1065',at='2026-11-09T01:06:02Z')
        self.assertEqual(self.report()['summary']['unrealized_pnl'],'300')

    def test_identical_fill_fee_callback_is_idempotent_while_its_ack_is_pending(self):
        t=self.env;t.start();child=t.open(fill=0);submit=t.kernel.ledger.submit;held=[]
        def hold(kind,name,data,mutate=None,**kwargs):
            actual=submit(kind,name,data,mutate,**kwargs)
            if data.get('fill'):
                actual.result(timeout=3);visible=Future();held.append((visible,actual.result()));return visible
            return actual
        with patch.object(t.kernel.ledger,'submit',side_effect=hold):
            self.assertTrue(t.kernel.record_fill(child,'pending-identical-fee',3,'1055',commission='2'))
            self.assertFalse(t.kernel.record_fill(child,'pending-identical-fee',3,'1055',commission='2'))
        self.assertNotIn('pending-identical-fee',t.kernel.money.book.fills)
        held[0][0].set_result(held[0][1]);t.kernel.wait_idle()
        self.assertEqual(self.report()['summary']['fill_count'],1)
        self.assertEqual(self.report()['summary']['fees'],'2')

    def test_fee_correction_does_not_make_the_original_callback_a_new_conflict_after_restart(self):
        t=self.env;t.start();child=t.open(fill=0)
        at=t.last_event['source_time_utc']
        t.kernel.record_fill(child,'original-fee-callback',3,'1055',commission='2',observed_at_utc=at);t.kernel.wait_idle()
        t.kernel.money.record_fee('original-fee-callback','3',evidence_id='fee-correction',revision=2);t.kernel.wait_idle()
        self.assertFalse(t.kernel.record_fill(child,'original-fee-callback',3,'1055',commission='2',observed_at_utc=at))
        t.kernel.close();t.port=RecordingPort();t.kernel=t.new_kernel()
        self.assertFalse(t.kernel.record_fill(child,'original-fee-callback',3,'1055',commission='2',observed_at_utc=at))
        self.assertEqual(self.report()['summary']['fees'],'3')
        self.assertEqual(self.report()['summary']['fill_count'],1)


if __name__=='__main__':unittest.main()
