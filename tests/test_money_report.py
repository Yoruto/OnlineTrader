"""Currency reports derive actual owned quantities, never nominal point totals."""

import copy
from decimal import Decimal
import unittest

from online_trader.accounting.report import build_report
from online_trader.contracts.codec import canonical_bytes
from online_trader.domain.values import MAX_QUANTITY
from online_trader.errors import TraderError


class MoneyReportTests(unittest.TestCase):
    def setUp(self):
        self.account='DEV:money-report';self.fills=[];self.fees=[];self.ordinal=0

    def fill(self, effect='OPEN', price='1000', qty=2, **changes):
        self.ordinal+=1
        row={'trade_key':f'trade-{self.ordinal}','position_cycle_id':'cycle-1',
            'contract_id':'DCE.p2701','direction':'LONG','effect':effect,'quantity':qty,
            'price':price,'multiplier':'10','theoretical_entry_price':'995',
            'trading_day':'2026-11-09','observed_at_utc':f'2026-11-09T01:00:{self.ordinal:02d}Z',
            'event_seq':str(self.ordinal)}
        row.update(changes);self.fills.append(row);return row

    def fee(self, fill, amount='0', quality='CONFIRMED', revision=1, **changes):
        row={'trade_key':fill['trade_key'],'amount':amount,'quality':quality,
             'evidence_id':fill['trade_key']+f'-fee-{revision}','revision':revision}
        row.update(changes);self.fees.append(row);return row

    def account_fact(self, **changes):
        row={'account_key':self.account,'trading_day':'2026-11-09',
            'observed_at_utc':'2026-11-09T01:10:00Z','event_seq':'5',
            'balance':'10020','pre_balance':'10000','available':'8000','margin':'2000',
            'frozen_margin':'0','close_profit':'25','position_profit':'0','float_profit':'0',
            'commission':'5','net_cash_flow':None,'currency':'CNY','evidence_id':'account-evidence',
            'source_epoch':'1','revision':1}
        row.update(changes);return row

    def report(self, **changes):
        args={'account_key':self.account,'checkpoint_event_seq':'100','fills':self.fills,
              'fees':self.fees,'as_of_utc':'2026-11-09T02:00:00Z'}
        args.update(changes);return build_report(**args)

    def amounts(self):return self.report()['summary']

    def close(self, *, direction='LONG', open_price='1000', close_price='1015', qty=2):
        entry=self.fill(direction=direction,price=open_price,qty=qty)
        exit_=self.fill('REDUCE',close_price,qty,direction=direction)
        return entry,exit_

    def test_actual_fill_prices_and_quantity_are_independent_of_nominal_points(self):
        self.close()
        nominal=[{'state':{'daily_pnl_points':'9000','net_loss_points':'-500'}}]
        report=self.report(nominal=nominal)
        self.assertEqual(report['summary']['gross_realized_pnl'],'300')
        self.assertIsNone(report['summary']['net_realized_pnl'])
        self.assertEqual(report['nominal'],nominal)

    def test_short_profit_and_loss_mirror_direction(self):
        self.close(direction='SHORT',close_price='985')
        self.assertEqual(self.amounts()['gross_realized_pnl'],'300')
        self.fills=[];self.close(direction='SHORT',close_price='1015')
        self.assertEqual(self.amounts()['gross_realized_pnl'],'-300')

    def test_partial_exit_matches_confirmed_fifo_lots(self):
        self.fill(qty=2);self.fill(price='1010',qty=1)
        self.fill('REDUCE','1005',2)
        report=self.report(marks={'cycle-1':{'price':'1012','valid':True,
            'observed_at_utc':'2026-11-09T01:30:00Z','trading_day':'2026-11-09'}})
        self.assertEqual(report['summary']['gross_realized_pnl'],'100')
        self.assertEqual(report['summary']['unrealized_pnl'],'20')
        self.assertEqual(report['cycles'][0]['remaining_quantity'],1)
        self.assertFalse(report['cycles'][0]['closed'])

    def test_final_partial_exit_only_then_closes_one_cycle(self):
        self.fill(qty=2);self.fill(price='1010',qty=1)
        self.fill('REDUCE','1005',2);self.fill('REDUCE','1020',1)
        report=self.report()
        self.assertEqual(report['summary']['gross_realized_pnl'],'200')
        self.assertEqual(report['summary']['unrealized_pnl'],'0')
        self.assertEqual(report['summary']['closed_cycles'],1)
        self.assertTrue(report['cycles'][0]['closed'])

    def test_missing_fee_stays_unknown_without_hiding_known_gross(self):
        entry,exit_=self.close();self.fee(entry,'2')
        summary=self.amounts()
        self.assertEqual(summary['gross_realized_pnl'],'300')
        self.assertEqual(summary['confirmed_fees'],'2')
        self.assertEqual(summary['unknown_fee_count'],1)
        self.assertIsNone(summary['fees']);self.assertIsNone(summary['net_realized_pnl'])
        self.assertIsNone(summary['estimated_net_realized_pnl'])

    def test_explicit_zero_fee_is_confirmed_and_produces_known_net(self):
        entry,exit_=self.close();self.fee(entry);self.fee(exit_)
        summary=self.amounts()
        self.assertEqual(summary['fees'],'0');self.assertEqual(summary['net_realized_pnl'],'300')
        self.assertEqual(summary['quality'],'CONFIRMED_COST_BASIS')

    def test_estimated_fees_do_not_become_confirmed_net(self):
        entry,exit_=self.close();self.fee(entry,'2');self.fee(exit_,'3','ESTIMATED')
        summary=self.amounts()
        self.assertEqual(summary['confirmed_fees'],'2');self.assertEqual(summary['estimated_fees'],'3')
        self.assertIsNone(summary['net_realized_pnl'])
        self.assertEqual(summary['estimated_net_realized_pnl'],'295')

    def test_confirmed_fee_replaces_estimate_and_cannot_be_downgraded(self):
        entry,exit_=self.close();self.fee(entry)
        self.fee(exit_,'8','ESTIMATED',revision=1)
        self.fee(exit_,'3','CONFIRMED',revision=2)
        self.fee(exit_,'99','ESTIMATED',revision=3)
        report=self.report()
        self.assertEqual(report['summary']['fees'],'3')
        self.assertEqual(report['summary']['net_realized_pnl'],'297')
        self.assertEqual(report['trades'][-1]['fee_quality'],'CONFIRMED')

    def test_same_fee_revision_conflict_is_refused(self):
        entry=self.fill();self.fee(entry,'1');self.fee(entry,'2')
        with self.assertRaises(TraderError) as error:self.report()
        self.assertEqual(error.exception.code,'E_DUPLICATE_CONFLICT')

    def test_open_entry_fees_are_paid_even_before_a_close(self):
        entry=self.fill();self.fee(entry,'4')
        result=self.amounts()
        self.assertEqual(result['gross_realized_pnl'],'0')
        self.assertEqual(result['net_realized_pnl'],'-4')
        self.assertIsNone(result['unrealized_pnl'])
        self.assertIsNone(result['total_pnl'])

    def test_marks_value_actual_remaining_quantity_for_long_and_short(self):
        self.fill(qty=3)
        marked=self.report(marks={'cycle-1':{'price':'1010','valid':True,
            'observed_at_utc':'2026-11-09T01:30:00Z','trading_day':'2026-11-09'}})
        self.assertEqual(marked['summary']['unrealized_pnl'],'300')
        self.fills=[];self.fill(direction='SHORT',qty=3)
        marked=self.report(marks={'cycle-1':{'price':'990','valid':True,
            'observed_at_utc':'2026-11-09T01:30:00Z','trading_day':'2026-11-09'}})
        self.assertEqual(marked['summary']['unrealized_pnl'],'300')

    def test_invalid_or_future_marks_keep_unrealized_unknown(self):
        self.fill()
        for mark in ({'price':'1010','valid':False,'observed_at_utc':'2026-11-09T01:00:00Z'},
                     {'price':'1010','valid':True,'observed_at_utc':'2026-11-09T02:00:01Z'}):
            with self.subTest(mark=mark):
                self.assertIsNone(self.report(marks={'cycle-1':mark})['summary']['unrealized_pnl'])

    def test_explicit_valuation_day_is_not_inferred_from_utc_date(self):
        self.fill()
        report=self.report(as_of_utc='2026-11-09T15:00:00Z',marks={'cycle-1':{
            'price':'1010','valid':True,'observed_at_utc':'2026-11-09T14:00:00Z','trading_day':'2026-11-10'}})
        self.assertEqual([r['trading_day'] for r in report['by_day']],['2026-11-09','2026-11-10'])
        self.assertEqual(report['by_day'][1]['unrealized_pnl'],'200')
        self.assertEqual(report['by_day'][1]['entry_quantity'],0)

    def test_missing_valuation_day_uses_last_fill_day_with_an_issue(self):
        self.fill();self.fill('REDUCE','1010',1,trading_day='2026-11-10')
        report=self.report(as_of_utc='2026-11-10T02:00:00Z',marks={'cycle-1':{
            'price':'1020','valid':True,'observed_at_utc':'2026-11-10T01:00:00Z'}})
        self.assertEqual(report['cycles'][0]['valuation_trading_day'],'2026-11-10')
        self.assertIn('VALUATION_TRADING_DAY_UNPROVEN',[row['code'] for row in report['issues']])

    def test_realized_profit_and_fees_follow_explicit_fill_trading_day(self):
        entry=self.fill();exit_=self.fill('REDUCE','1010',2,trading_day='2026-11-10')
        self.fee(entry,'2');self.fee(exit_,'3')
        days=self.report()['by_day']
        self.assertEqual((days[0]['gross_realized_pnl'],days[0]['net_realized_pnl']),('0','-2'))
        self.assertEqual((days[1]['gross_realized_pnl'],days[1]['net_realized_pnl']),('200','197'))
        self.assertEqual(self.amounts()['net_realized_pnl'],'195')

    def test_unknown_multiplier_does_not_fabricate_money(self):
        self.fill(multiplier=None);self.fill('REDUCE','1015',2,multiplier=None)
        result=self.amounts()
        self.assertIsNone(result['gross_realized_pnl']);self.assertIsNone(result['net_realized_pnl'])
        self.assertEqual(result['unrealized_pnl'],'0')

    def test_changed_frozen_multiplier_marks_cycle_incomplete(self):
        self.fill();self.fill('REDUCE','1015',2,multiplier='20')
        report=self.report()
        self.assertIsNone(report['summary']['gross_realized_pnl'])
        self.assertIsNone(report['cycles'][0]['remaining_quantity'])
        self.assertIn('CYCLE_MULTIPLIER_CONFLICT',[row['code'] for row in report['issues']])

    def test_unmatched_exit_cannot_look_like_proven_flat_profit(self):
        self.fill(qty=1);self.fill('REDUCE','1015',2)
        report=self.report()
        self.assertIsNone(report['summary']['gross_realized_pnl'])
        self.assertIsNone(report['cycles'][0]['remaining_quantity'])
        self.assertFalse(report['cycles'][0]['closed'])

    def test_multiple_cycles_and_contracts_aggregate_without_matching_each_other(self):
        self.close()
        self.fill(qty=1,position_cycle_id='cycle-2',contract_id='SHFE.rb2701')
        self.fill('REDUCE','980',1,position_cycle_id='cycle-2',contract_id='SHFE.rb2701')
        report=self.report()
        self.assertEqual(report['summary']['gross_realized_pnl'],'100')
        self.assertEqual(len(report['by_contract']),2)
        self.assertEqual((report['summary']['win_count'],report['summary']['loss_count']),(1,1))
        self.assertEqual(report['summary']['win_rate'],'0.5')

    def test_duplicate_fill_identity_is_idempotent_but_changed_quantity_conflicts(self):
        fill=self.fill();self.fills.append(copy.deepcopy(fill))
        self.assertEqual(self.amounts()['fill_count'],1)
        self.fills[-1]['quantity']=1
        with self.assertRaises(TraderError) as error:self.report()
        self.assertEqual(error.exception.code,'E_DUPLICATE_CONFLICT')

    def test_checkpoint_excludes_later_unpublished_facts(self):
        self.close();report=self.report(checkpoint_event_seq='1')
        self.assertEqual(report['summary']['fill_count'],1)
        self.assertEqual(report['summary']['gross_realized_pnl'],'0')
        self.assertEqual(report['cycles'][0]['remaining_quantity'],2)

    def test_account_daily_pnl_does_not_double_deduct_commission(self):
        report=self.report(account_facts=[self.account_fact()])
        self.assertEqual(report['account']['daily_pnl'],'20')
        self.assertEqual(report['account']['daily_pnl_basis'],'BALANCE_MINUS_PRE_BALANCE')
        self.assertIsNone(report['account']['trading_pnl'])
        self.assertEqual(report['summary']['gross_realized_pnl'],'0')

    def test_account_today_pnl_alias_matches_daily_pnl_for_report_display(self):
        report=self.report(account_facts=[self.account_fact()])
        self.assertEqual(report['account']['today_pnl'],report['account']['daily_pnl'])
        self.assertEqual(report['equity'][0]['today_pnl'],'20')

    def test_complete_account_source_publishes_quality_and_daily_ratio(self):
        row=self.report(account_facts=[self.account_fact(complete=True)])['account']
        self.assertTrue(row['complete']);self.assertEqual(row['quality'],'KNOWN')
        self.assertEqual(row['profit_ratio'],'0.002')
        self.assertEqual(row['today_pnl_ratio'],'0.002')

    def test_incomplete_account_keeps_raw_values_but_all_derived_profit_unknown(self):
        row=self.report(account_facts=[self.account_fact(complete=False,net_cash_flow='0')])['account']
        self.assertFalse(row['complete']);self.assertEqual(row['quality'],'INCOMPLETE')
        self.assertEqual(row['balance'],'10020')
        for name in ('equity_change','daily_pnl','today_pnl','trading_pnl','profit_ratio','today_pnl_ratio'):
            self.assertIsNone(row[name],name)

    def test_account_cash_flow_is_separate_from_equity_change(self):
        account=self.report(account_facts=[self.account_fact(net_cash_flow='50')])['account']
        self.assertEqual(account['equity_change'],'20')
        self.assertEqual(account['trading_pnl'],'-30')
        self.assertEqual(account['daily_pnl'],'20')

    def test_account_fallback_requires_all_explicit_profit_and_fee_components(self):
        source=self.account_fact(pre_balance='0',balance=None,close_profit='25',float_profit='7',commission='5')
        result=self.report(account_facts=[source])['account']
        self.assertEqual(result['daily_pnl'],'27')
        self.assertEqual(result['daily_pnl_basis'],'CLOSE_PLUS_FLOAT_MINUS_COMMISSION')
        source['commission']=None
        self.assertIsNone(self.report(account_facts=[source])['account']['daily_pnl'])

    def test_account_scope_currency_and_evidence_conflicts_are_refused(self):
        for changes in ({'account_key':'DEV:other'},{'currency':'USD'}):
            with self.subTest(changes=changes),self.assertRaises(TraderError):
                self.report(account_facts=[self.account_fact(**changes)])
        with self.assertRaises(TraderError) as error:
            self.report(account_facts=[self.account_fact(),self.account_fact(balance='10021')])
        self.assertEqual(error.exception.code,'E_DUPLICATE_CONFLICT')

    def test_invalid_floats_bool_decimal_and_time_inputs_are_refused(self):
        fill=self.fill()
        for changes in ({'price':1000.0},{'quantity':True},{'multiplier':'NaN'},
                        {'price':'1000.0'},{'observed_at_utc':'2026-11-09T01:00:00+08:00'},
                        {'trading_day':'2026-02-30'}):
            with self.subTest(changes=changes),self.assertRaises(TraderError):
                self.report(fills=[fill|changes])

    def test_money_overflow_is_unknown_with_issues_not_a_fake_zero(self):
        self.fill(price='1',multiplier='999999999999999999')
        self.fill('REDUCE','999999999999999999',2,multiplier='999999999999999999')
        report=self.report()
        self.assertIsNone(report['summary']['gross_realized_pnl'])
        self.assertIn('AMOUNT_DOMAIN_OVERFLOW',[row['code'] for row in report['issues']])
        canonical_bytes(report)

    def test_fractional_currency_is_exact_and_rounded_half_even_at_twelve_digits(self):
        entry=self.fill(price='1.000000000001',qty=1,multiplier='0.5')
        exit_=self.fill('REDUCE','1.000000000002',1,multiplier='0.5')
        self.fee(entry);self.fee(exit_)
        self.assertEqual(self.amounts()['gross_realized_pnl'],'0')
        self.fills[-1]['price']='1.000000000004'
        self.assertEqual(self.amounts()['gross_realized_pnl'],'0.000000000002')

    def test_output_drops_unrecognized_fill_fields_and_never_mutates_inputs(self):
        fill=self.fill();fill['password']='sensitive-value';fill['raw_account_snapshot']={'secret':'hidden'}
        before=copy.deepcopy(self.fills)
        report=self.report();report['trades'][0]['price']='999'
        self.assertEqual(self.fills,before)
        raw=canonical_bytes(self.report())
        self.assertNotIn(b'sensitive-value',raw);self.assertNotIn(b'hidden',raw)

    def test_fill_order_is_commit_sequence_and_not_price_or_input_order(self):
        self.fill(qty=1);self.fill(price='1010',qty=1);self.fill('REDUCE','1005',1)
        report=self.report(fills=list(reversed(self.fills)))
        self.assertEqual(report['summary']['gross_realized_pnl'],'50')
        self.assertEqual([r['event_seq'] for r in report['trades']],['1','2','3'])


if __name__=='__main__':unittest.main()
